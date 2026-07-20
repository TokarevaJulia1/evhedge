"""PandaScore -> ``storage.ps_results`` sync, plus the reconciliation
and deadline-flagging that consume it.

DESIGN CHOICE: matches ``collect.py``'s canonicalization (same
``team_aliases.canonical_name`` + ``load_default_aliases``), not a
second matcher -- a PandaScore-side spelling of a team must resolve to
the exact same canonical name a Polymarket-side spelling would, or
``evhedge reconcile``'s join against ``resolves``/``predictions`` breaks
silently. Real spellings confirmed live against PandaScore's team
registry while building this (2026-07-20): "Spirit" (not "Team
Spirit"), "Team Falcons" (there is ALSO an unrelated lower-tier team
called just "Falcons", id 131216 vs 130564 -- see
``data_sources.pandascore.fetch_teams``'s docstring), "Vitality",
"MOUZ", "GamerLegion", "Aurora Gaming" all already match either the
existing default alias map or evhedge's own canon directly -- no new
aliases were needed for BLAST's PandaScore side, on top of the ones
already added for its Polymarket side.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from evhedge.data_sources import pandascore as pandascore_ds
from evhedge.data_sources.pandascore import RequestBudget
from evhedge.storage import PSResult, Storage
from evhedge.team_aliases import canonical_name, load_default_aliases

logger = logging.getLogger(__name__)

#: DESIGN CHOICE: a leg starting within this many hours with no
#: predictions row is flagged by both `evhedge deadlines` and the
#: watcher loop -- a direct signal that auto_predict hasn't (yet, or
#: won't) catch this leg's book. Not a hard alarm: some legs simply
#: won't get a Polymarket market at all.
DEFAULT_DEADLINE_HOURS = 3.0

#: The watcher's own in-loop warning fires tighter than the CLI default
#: -- by <2h there's realistically no time left to act manually.
WATCHER_WARNING_HOURS = 2.0


def _parse_ps_datetime(raw: Optional[str]) -> Optional[datetime]:
    if not raw:
        return None
    return datetime.fromisoformat(raw.replace("Z", "+00:00"))


def match_to_ps_result(
    match: dict, tournament: str, alias_map: dict[str, str], ts_utc: datetime
) -> Optional[PSResult]:
    """Convert one raw PandaScore match dict (see
    ``data_sources.pandascore.fetch_matches``) into a ``PSResult``,
    canonicalizing both teams (and the winner, if decided) through the
    SAME alias map ``collect.py`` uses.

    Returns:
        ``None`` for a match that isn't a clean two-team confrontation
        (e.g. a bye/TBD slot) -- skipped, not guessed.
    """
    opponents = match.get("opponents") or []
    if len(opponents) != 2:
        return None
    try:
        team_a_raw = opponents[0]["opponent"]["name"]
        team_b_raw = opponents[1]["opponent"]["name"]
        team_a_id = opponents[0]["opponent"]["id"]
        team_b_id = opponents[1]["opponent"]["id"]
    except (KeyError, TypeError):
        return None

    team_a = canonical_name(team_a_raw, alias_map)
    team_b = canonical_name(team_b_raw, alias_map)

    winner_id = match.get("winner_id")
    winner: Optional[str] = None
    if winner_id == team_a_id:
        winner = team_a
    elif winner_id == team_b_id:
        winner = team_b

    scores = {r.get("team_id"): r.get("score") for r in (match.get("results") or [])}
    score_a = scores.get(team_a_id)
    score_b = scores.get(team_b_id)

    stage_name = ((match.get("tournament") or {}).get("name")) or "?"

    return PSResult(
        ps_match_id=match["id"],
        tournament=tournament,
        team_a=team_a,
        team_b=team_b,
        winner=winner,
        score_a=score_a,
        score_b=score_b,
        stage=stage_name,
        best_of=match.get("number_of_games") or 0,
        status=match.get("status") or "unknown",
        ts_utc=ts_utc,
        begin_at=_parse_ps_datetime(match.get("begin_at")),
        scheduled_at=_parse_ps_datetime(match.get("scheduled_at")),
    )


@dataclass
class SyncSummary:
    """What one ``sync_matches`` pass did."""

    matches_seen: int = 0
    matches_written: int = 0
    skipped_shape: int = 0     # not a clean 2-team match (bye/TBD)
    requests_made: int = 0
    rate_limit_remaining: Optional[int] = None


def sync_matches(
    store: Storage,
    tournament: str,
    league_id: Optional[int] = None,
    serie_id: Optional[int] = None,
    statuses: tuple[str, ...] = ("upcoming", "running", "past"),
    ts_utc: Optional[datetime] = None,
    max_pages_per_status: Optional[int] = None,
) -> SyncSummary:
    """Pull matches for one tournament from PandaScore (across the given
    match statuses) and upsert each into ``ps_results``, canonicalized.

    Args:
        league_id/serie_id: PandaScore's own filter ids -- at least one
            should be given, or every match on the game is walked.
        statuses: Which of upcoming/running/past to pull -- a live
            watcher pass typically wants all three (an "upcoming" match
            can flip to "running" then "past" between polls).
    """
    ts = ts_utc or datetime.now(timezone.utc)
    alias_map = load_default_aliases()
    budget = RequestBudget()
    summary = SyncSummary()

    for status in statuses:
        matches = pandascore_ds.fetch_matches(
            status, budget, league_id=league_id, serie_id=serie_id,
            max_pages=max_pages_per_status,
        )
        for match in matches:
            summary.matches_seen += 1
            result = match_to_ps_result(match, tournament, alias_map, ts)
            if result is None:
                summary.skipped_shape += 1
                continue
            store.record_ps_result(result)
            summary.matches_written += 1

    summary.requests_made = budget.requests_made
    summary.rate_limit_remaining = budget.last_remaining
    return summary


# ---------------------------------------------------------------------------
# Reconciliation: ps_results (sporting truth) vs resolves (market truth)
# ---------------------------------------------------------------------------

@dataclass
class ReconcileRow:
    """One PS-vs-Gamma comparison row."""

    team_a: str
    team_b: str
    stage: str
    ps_winner: Optional[str]
    ps_status: str
    begin_at: Optional[datetime]
    gamma_resolved: bool
    warning: Optional[str] = None


@dataclass
class ReconcileReport:
    rows: list[ReconcileRow] = field(default_factory=list)
    n_ok: int = 0
    n_warnings: int = 0


#: DESIGN CHOICE: a finished PS match with no Gamma resolve yet is only
#: flagged past this lag -- Gamma legitimately takes some time to settle
#: (the same "book stays live a while after the real result" pattern
#: already documented for auto_predict's live-skip). Not a contradiction
#: signal by itself, just a "look at this" signal.
RECONCILE_LAG_HOURS = 2.0


def reconcile(store: Storage, tournament: str, now: Optional[datetime] = None) -> ReconcileReport:
    """Compare every ``ps_results`` row for ``tournament`` against
    ``resolves``: does a PS-finished match have a matching Gamma
    resolve? Never writes anything, never "fixes" a gap -- see
    ``PSResult``'s docstring on why ``resolves`` stays the market's own
    truth."""
    now = now or datetime.now(timezone.utc)
    report = ReconcileReport()

    for ps in store.ps_results(tournament=tournament):
        if ps.status != "finished" or ps.winner is None:
            continue  # nothing to reconcile yet -- not a warning

        gamma_rows = store.resolves(tournament, team=ps.winner)
        # A resolve for THIS specific pairing: match on team + opponent
        # showing up somewhere in the market label/note isn't reliable
        # (labels are slug-based, not opponent-based) -- the honest
        # check available here is simply "does Gamma have ANY resolve
        # for the winning team around this match's stage", not a
        # guaranteed row-for-row join. Documented, not silently assumed
        # exact.
        gamma_resolved = len(gamma_rows) > 0

        warning = None
        if not gamma_resolved:
            reference_time = ps.begin_at or ps.scheduled_at
            lag_hours = (
                (now - reference_time).total_seconds() / 3600.0
                if reference_time is not None else None
            )
            if lag_hours is None or lag_hours >= RECONCILE_LAG_HOURS:
                warning = (
                    f"PandaScore says finished ({ps.team_a} {ps.score_a}-{ps.score_b} "
                    f"{ps.team_b}, winner={ps.winner}), no Gamma resolve found yet"
                    + (f" ({lag_hours:.1f}h ago)" if lag_hours is not None else "")
                )

        report.rows.append(ReconcileRow(
            team_a=ps.team_a, team_b=ps.team_b, stage=ps.stage,
            ps_winner=ps.winner, ps_status=ps.status, begin_at=ps.begin_at,
            gamma_resolved=gamma_resolved, warning=warning,
        ))
        if warning:
            report.n_warnings += 1
        else:
            report.n_ok += 1

    return report


# ---------------------------------------------------------------------------
# Deadlines
# ---------------------------------------------------------------------------

@dataclass
class DeadlineRow:
    team_a: str
    team_b: str
    stage: str
    best_of: int
    scheduled_at: Optional[datetime]
    hours_until: Optional[float]
    has_prediction: bool


def upcoming_deadlines(
    store: Storage,
    tournament: str,
    hours_threshold: float = DEFAULT_DEADLINE_HOURS,
    now: Optional[datetime] = None,
) -> list[DeadlineRow]:
    """Every not-yet-started ``ps_results`` match for ``tournament``,
    ordered by how soon it starts, with a flag for legs starting within
    ``hours_threshold`` that have NO ``predictions`` row yet -- a direct
    "auto_predict hasn't caught this leg's book / there's no book yet"
    signal. Placing a hedge is a human decision -- this only surfaces
    the list and the clock, never a recommendation.
    """
    now = now or datetime.now(timezone.utc)
    rows: list[DeadlineRow] = []

    for ps in store.ps_results(tournament=tournament):
        if ps.status in ("finished", "canceled"):
            continue
        reference_time = ps.scheduled_at or ps.begin_at
        hours_until = (
            (reference_time - now).total_seconds() / 3600.0
            if reference_time is not None else None
        )

        # APPROXIMATE, documented: PSResult has no Gamma slug to join
        # predictions.market on exactly (PandaScore doesn't know
        # Polymarket's slug), so this checks "any prediction exists for
        # either team in this tournament at all" -- if a team plays
        # twice in the window this can under-flag (an older match's
        # prediction masks a missing one for THIS match). Good enough to
        # prioritize attention, not a guaranteed-exact join.
        has_prediction = bool(
            store.predictions(tournament=tournament, team=ps.team_a) or
            store.predictions(tournament=tournament, team=ps.team_b)
        )

        rows.append(DeadlineRow(
            team_a=ps.team_a, team_b=ps.team_b, stage=ps.stage, best_of=ps.best_of,
            scheduled_at=reference_time, hours_until=hours_until,
            has_prediction=has_prediction,
        ))

    rows.sort(key=lambda r: (r.hours_until is None, r.hours_until))
    return rows
