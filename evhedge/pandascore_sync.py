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
from pathlib import Path
from typing import Optional, Union

from evhedge.config_io import ConfigError, _read_yaml_file
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


# ---------------------------------------------------------------------------
# Tournament structure -> stage_ranks (semi-automatic)
# ---------------------------------------------------------------------------

def load_stage_map(path: Union[str, Path]) -> dict[str, int]:
    """Load a ``{stage_name_substring: n}`` YAML map -- PandaScore's own
    ``tournament.name`` (their "Tournament" = our "stage", see module
    docstring) matched by case-insensitive SUBSTRING against these keys,
    longest match wins on a tie (so "Grand Final" beats a looser "Final"
    entry, if both are present). One config per tournament FORMAT, hand-
    written -- see ``configs/blast_bounty_s2_stage_map.yaml`` for BLAST
    Bounty S2's Ro32/Ro16/QF/SF/GF -> 5/4/3/2/1.

    DESIGN CHOICE: substring matching here is NOT the same kind of
    "fuzzy" matching ``team_aliases`` forbids -- there the risk was
    silently merging two DIFFERENT real-world entities; here the keys
    are a small, hand-curated, reviewed vocabulary for ONE tournament's
    own round names, not a guess at which of many teams a string might
    mean. A stage name matching nothing is left unmapped (excluded from
    suggestions), never guessed at.

    Raises:
        ConfigError: Malformed YAML, or a non-positive-int value.
    """
    path = Path(path)
    data = _read_yaml_file(path)
    result: dict[str, int] = {}
    for stage, n in data.items():
        try:
            n_int = int(n)
        except (TypeError, ValueError):
            raise ConfigError(f"{path}: stage_map[{stage!r}] must be an int, got {n!r}") from None
        if n_int <= 0:
            raise ConfigError(f"{path}: stage_map[{stage!r}] must be a positive int, got {n_int}")
        result[str(stage)] = n_int
    return result


def _match_stage(stage_name: str, stage_map: dict[str, int]) -> Optional[int]:
    """Longest case-insensitive substring match of ``stage_name`` against
    ``stage_map``'s keys, or ``None`` if nothing matches."""
    lowered = stage_name.lower()
    best_key: Optional[str] = None
    for key in stage_map:
        if key.lower() in lowered and (best_key is None or len(key) > len(best_key)):
            best_key = key
    return stage_map[best_key] if best_key is not None else None


def compute_stage_ranks(
    store: Storage, tournament: str, stage_map: dict[str, int]
) -> dict[str, int]:
    """Derive current ``{team: rounds_to_title}`` from ``ps_results``:
    for each team, look at their MOST RECENT match (by begin_at/
    scheduled_at) whose stage matches ``stage_map``:

    - Match not yet finished -> team is AT that stage, n = the mapped
      value directly.
    - Match finished, team WON -> team has ADVANCED past that stage;
      n = mapped_value - 1 (one fewer win needed), computed arithmetically
      so a team that just won doesn't need PandaScore to have created
      the NEXT stage's matches yet. n reaching 0 means champion --
      excluded (no more stages to bet on).
    - Match finished, team LOST -> eliminated, excluded entirely
      (absence in the map is what makes auto_predict degrade to
      p_model=None honestly -- see auto_predict.py's module docstring).

    Teams whose only matches have an unmapped stage name are excluded,
    not guessed at.
    """
    rows = store.ps_results(tournament=tournament)

    # team -> (reference_time, mapped_n, finished, won) for the LATEST
    # mapped-stage match seen so far
    latest: dict[str, tuple[datetime, int, bool, bool]] = {}

    for row in rows:
        n = _match_stage(row.stage, stage_map)
        if n is None:
            continue
        reference_time = row.begin_at or row.scheduled_at or row.ts_utc
        for team in (row.team_a, row.team_b):
            prev = latest.get(team)
            if prev is not None and prev[0] >= reference_time:
                continue
            finished = row.status == "finished"
            won = finished and row.winner == team
            latest[team] = (reference_time, n, finished, won)

    result: dict[str, int] = {}
    for team, (_, n, finished, won) in latest.items():
        if finished and not won:
            continue  # eliminated
        suggested_n = (n - 1) if (finished and won) else n
        if suggested_n <= 0:
            continue  # champion -- no further stage to bet on
        result[team] = suggested_n

    return result


@dataclass
class StageRanksDiff:
    """What ``suggest_stage_ranks`` found vs the currently-loaded file."""

    added: dict[str, int] = field(default_factory=dict)      # new team -> n
    changed: dict[str, tuple[int, int]] = field(default_factory=dict)  # team -> (old, new)
    removed: list[str] = field(default_factory=list)         # team no longer live/mapped
    unchanged_count: int = 0


def suggest_stage_ranks(
    store: Storage, tournament: str, stage_map: dict[str, int], current_ranks: dict[str, int]
) -> StageRanksDiff:
    """Diff ``compute_stage_ranks``'s fresh suggestion against
    ``current_ranks`` (the already-loaded stage_ranks file) -- never
    applies anything itself (see ``evhedge stageranks suggest``'s
    ``--apply`` flag: auto-editing without confirmation is explicitly
    forbidden, predictions are immutable and a stale n is cheaper than a
    wrong one)."""
    suggested = compute_stage_ranks(store, tournament, stage_map)
    diff = StageRanksDiff()

    for team, n in suggested.items():
        if team not in current_ranks:
            diff.added[team] = n
        elif current_ranks[team] != n:
            diff.changed[team] = (current_ranks[team], n)
        else:
            diff.unchanged_count += 1

    for team in current_ranks:
        if team not in suggested:
            diff.removed.append(team)

    return diff


# ---------------------------------------------------------------------------
# Matching a live Polymarket leg back to a PandaScore Tournament id
# ---------------------------------------------------------------------------

def find_tournament_id_for_matchup(
    team_a_raw: str, team_b_raw: str, budget: Optional[RequestBudget] = None,
) -> Optional[int]:
    """Given two team names as they came off a Polymarket leg position
    (``outcome``/``oppositeOutcome`` -- Polymarket's own spelling, not
    necessarily PandaScore's), find the PandaScore Tournament id of the
    real match between them -- so the dashboard can show the whole known
    bracket for a loaded position without the user hand-typing a
    tournament id.

    Structural verification, not a name-similarity guess (same
    discipline as ``team_aliases``): searches PandaScore's team registry
    for candidates matching either spelling of team_a, then for EACH
    candidate (there can be several same-ish-named orgs -- see
    ``data_sources.pandascore.fetch_teams``'s Falcons/Aurora example)
    checks that team's own matches for one whose OTHER opponent
    canonicalizes to team_b. Only a confirmed, real scheduled match
    counts.

    Returns:
        The PandaScore Tournament id of the confirmed match, or
        ``None`` if nothing could be confirmed (team not found, no
        match between exactly these two teams yet, ...) -- never a
        best-effort guess.
    """
    budget = budget or RequestBudget()
    alias_map = load_default_aliases()
    team_a = canonical_name(team_a_raw, alias_map)
    team_b = canonical_name(team_b_raw, alias_map)

    # PandaScore's own search matches "does THEIR name contain my query",
    # not the other way round -- confirmed live: search[name]="Team
    # Spirit" returns nothing even though PandaScore's own entry (id
    # 124523) is named exactly "Spirit". Try, in order: the raw name as
    # given, its canonical form, and (cheap, safe heuristic -- the actual
    # match is still verified structurally below, this only widens the
    # SEARCH candidates) the raw name with a leading "Team " stripped.
    search_terms = [team_a_raw, team_a]
    if team_a_raw.startswith("Team "):
        search_terms.append(team_a_raw[len("Team "):])

    candidates: list[dict] = []
    seen_ids: set = set()
    for search_term in search_terms:
        for c in pandascore_ds.fetch_teams(search_term, budget):
            if c.get("id") not in seen_ids:
                seen_ids.add(c.get("id"))
                candidates.append(c)

    for candidate in candidates:
        team_id = candidate.get("id")
        if team_id is None:
            continue
        for status in ("upcoming", "running", "past"):
            matches = pandascore_ds.fetch_matches(
                status, budget, opponent_id=team_id, max_pages=1,
            )
            for m in matches:
                opponents = m.get("opponents") or []
                if len(opponents) != 2:
                    continue
                names = {
                    canonical_name(o.get("opponent", {}).get("name", ""), alias_map)
                    for o in opponents
                }
                if names == {team_a, team_b}:
                    tid = (m.get("tournament") or {}).get("id")
                    if tid is not None:
                        return tid
    return None
