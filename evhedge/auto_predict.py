"""Automatic prediction recording: the calibration loop's manual step
(``evhedge predict``) closes itself in the collector.

Two measured defects motivated this, both real, both found in the live
EWC 2026 database:

1. **Stale model inputs.** A prediction fixed by hand tends to get typed
   when someone happens to be looking, not the instant the leg market
   first has a real (non-listing) book -- e.g. a "63 vs 68" gap on a
   World Cup final that turned out to trace back to a manual entry made
   after a board had already pumped +22% since open. The model input was
   correct for the moment it was TYPED, not the moment that actually
   mattered.
2. **Selection bias.** Humans predict what's interesting. A prediction
   table built by hand quietly becomes "the matches someone remembered to
   look at", not "every match with a book" -- which corrupts exactly the
   Brier aggregate ``evhedge score`` exists to report honestly.

This module fixes both by making the FIRST live-book moment of every leg
market a deterministic, unattended trigger point.

DESIGN CHOICE -- the trigger (see ``book_quality_trigger``): fire on the
first snapshot where the real order book has both sides AND a spread
under ``BOOK_QUALITY_SPREAD_MAX_PP``, and the match isn't live. Two
alternatives were considered and rejected:
  - Fire on ANY book, immediately. Rejected: a freshly-listed market is
    routinely a placeholder quote, not a price -- the real EWC case is
    BetBoom-Poor Rangers listing 4.0/96.0 at 19:12 the night before,
    settling into a real live price only by morning. Predicting off a
    listing quote would record garbage, permanently (predictions are
    immutable -- see ``storage.Prediction``).
  - Fire "N hours before scheduled start". Rejected: needs a reliable
    start time, which isn't always present/accurate on Gamma, and adds a
    second clock to reason about. "First live book" needs nothing but
    the book itself, so it's reproducible from the same inputs every run.

DESIGN CHOICE -- ``p_model`` inputs: the pairwise power-model probability
uses each team's LATEST book-verified (``source="book"``) winner-market
mid, and REQUIRES both teams' mids to come from the exact same
snapshot timestamp (see ``latest_book_winner_mid`` / the ``same ts``
requirement in ``compute_model_probability``) -- comparing a stale mid
for one side against a fresh one for the other is exactly the kind of
apples-to-oranges gap this module exists to prevent, so it degrades to
``p_model=None`` rather than mixing timestamps.

DESIGN CHOICE -- ``rounds_to_title`` (n): ``power_model.py``'s own
calibration boundary says n must be well-defined for a "consistent
comparison ACROSS TEAMS" in non-uniform brackets. Read literally that
could mean the whole tournament must share one n at all times, which is
far stricter than necessary -- what actually breaks the model is
comparing two teams whose OWN n differs (a semifinalist against a
group-stage team has no comparable "per-round strength"). Two teams
playing each other in the SAME match, by construction, need the same
number of further wins to take the title (win this match, then whatever
lies beyond it is identical for both) -- so the restriction is enforced
at the PAIR level here: both team's n from ``stage_ranks`` must be
present AND equal, or ``p_model=None``. See
``tests/test_auto_predict.py::test_uniform_n_within_ewc_pairs`` for the
factual check against the real EWC bracket shape this claim rests on.

``stage_ranks`` is a small, separately maintained YAML: ``{team: n}``,
keyed by CANONICAL team name (same spelling as ``team_aliases``' canon).
DESIGN CHOICE: not folded into ``scanner.ScannerConfig.stages_meta`` --
that format describes one team's whole bracket TREE (for the scanner's
own long-shot analysis) and is heavier than what's needed here: at any
moment during a live tournament, all this module needs is "how many
wins does each team still need", a flat map the user bumps by hand as
each round completes, independent of any scanner config existing (or
not) for that team. Missing file/missing team -> ``p_model=None``, never
a crash -- the same "board vs book" honesty rule as everywhere else in
this project: no model number without real, matched inputs.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional, Union

from evhedge.config_io import ConfigError, _read_yaml_file
from evhedge.power_model import pair_prob, strength
from evhedge.storage import Prediction, Storage

logger = logging.getLogger(__name__)

#: DESIGN CHOICE: maximum acceptable (ask - bid) spread, in percentage
#: points (0..100 scale, matching PriceSnapshot.bid_pct/ask_pct), for a
#: book to be considered "real" rather than a placeholder/thin listing
#: quote. A documented constant, not a magic number inline.
BOOK_QUALITY_SPREAD_MAX_PP = 5.0

#: Frozen with the current power_model.py: the logistic slope constants in
#: ``_gamma_for`` are fit against tests/test_power_model.py's regression
#: fixtures. ANY future change to gamma or the pair_prob formula MUST bump
#: this string -- a calibration series mixing two model versions under one
#: label is not honestly interpretable (see ``evhedge score``).
MODEL_VERSION = "power_model_v1"

#: The market label this module (and ``collect.py``'s closed-market
#: branch) writes/expects, canonicalized in ONE place so a future format
#: change can't silently diverge between the two call sites -- see the
#: module docstring's "same slug" requirement.
def result_market_label(event: dict, market: dict) -> str:
    """The exact ``resolves.market`` / ``predictions.market`` label for
    one match's result market -- MUST match ``collect.collect_match_markets``'s
    closed-market branch byte for byte, or scoring's JOIN silently misses
    rows. Built from Gamma metadata that is present on OPEN markets too
    (``groupItemTitle``), so it's available before the market closes."""
    return f"result:{event.get('slug', '?')}:{market.get('groupItemTitle', '?')}"


def book_quality_trigger(
    source: str, bid_pct: Optional[float], ask_pct: Optional[float]
) -> bool:
    """True the first time a leg's book is good enough to fix a
    prediction on: a real order book (not a board-price fallback), both
    sides present, spread within ``BOOK_QUALITY_SPREAD_MAX_PP``.

    Pure function of one snapshot's already-fetched book fields --
    callers walk their own snapshot history and call this on each one;
    the first ``True`` is the trigger point (see module docstring).
    """
    if source != "book" or bid_pct is None or ask_pct is None:
        return False
    return (ask_pct - bid_pct) <= BOOK_QUALITY_SPREAD_MAX_PP


def load_stage_ranks(path: Union[str, Path]) -> dict[str, int]:
    """Load a ``{team: rounds_to_title}`` YAML map, keyed by CANONICAL
    team name (see module docstring). A flat, hand-maintained file the
    user bumps as the bracket progresses -- no auto-derivation, no
    default path (unlike ``team_aliases``' packaged default): which
    tournament/round this applies to is a decision only the caller has
    enough context to make.

    Returns:
        ``{}`` if every value parses, team -> positive int.

    Raises:
        ConfigError: Malformed YAML, or a value that isn't a positive
            int -- a corrupt stage-rank file should fail loudly, not
            silently degrade (unlike a MISSING file, which callers
            handle by passing ``None`` through entirely -- see
            ``compute_model_probability``).
    """
    path = Path(path)
    data = _read_yaml_file(path)
    result: dict[str, int] = {}
    for team, n in data.items():
        try:
            n_int = int(n)
        except (TypeError, ValueError):
            raise ConfigError(f"{path}: stage_ranks[{team!r}] must be an int, got {n!r}") from None
        if n_int <= 0:
            raise ConfigError(f"{path}: stage_ranks[{team!r}] must be a positive int, got {n_int}")
        result[str(team)] = n_int
    return result


def latest_book_winner_mid(
    store: Storage, tournament: str, team: str
) -> tuple[Optional[float], Optional[datetime]]:
    """Latest ``source="book"`` ``winner_yes`` snapshot for ``team``: its
    bid/ask mid (percent, 0..100) and timestamp, or ``(None, None)`` if
    no book-verified winner snapshot exists yet.
    """
    snaps = [
        s for s in store.snapshots(tournament, team=team, market="winner_yes")
        if s.source == "book" and s.bid_pct is not None and s.ask_pct is not None
    ]
    if not snaps:
        return None, None
    latest = snaps[-1]
    return (latest.bid_pct + latest.ask_pct) / 2.0, latest.ts_utc


def compute_model_probability(
    store: Storage,
    tournament: str,
    team: str,
    opponent: str,
    stage_ranks: Optional[dict[str, int]],
) -> tuple[Optional[float], Optional[datetime], Optional[int]]:
    """``power_model.pair_prob`` for ``team`` beating ``opponent``, from
    each side's latest book-verified winner-market mid -- ``None`` (with
    ``None`` board_ts/n) unless ALL of these hold:

    - ``stage_ranks`` is provided and has an entry for both teams, and
      those two n's are equal (see module docstring: pair-level, not
      tournament-level).
    - Both teams have a book-verified ``winner_yes`` snapshot, and their
      timestamps match EXACTLY (same collector pass -- see module
      docstring on why a mismatched ts isn't used).

    Returns:
        ``(p_model, board_ts, n)`` -- ``board_ts`` is the shared
        timestamp of the winner-book mids used (for the ``note``), ``n``
        the shared rounds_to_title. All three ``None`` together whenever
        the model doesn't apply.
    """
    if not stage_ranks:
        return None, None, None
    n_team = stage_ranks.get(team)
    n_opp = stage_ranks.get(opponent)
    if n_team is None or n_opp is None or n_team != n_opp:
        return None, None, None

    mid_team, ts_team = latest_book_winner_mid(store, tournament, team)
    mid_opp, ts_opp = latest_book_winner_mid(store, tournament, opponent)
    if mid_team is None or mid_opp is None or ts_team != ts_opp:
        return None, None, None

    p = pair_prob(strength(mid_team, n_team), strength(mid_opp, n_opp))
    return p, ts_team, n_team


def format_note(board_ts: Optional[datetime], n: Optional[int]) -> str:
    """``auto|model=<version>|board_ts=<ts>|n=<k>`` when the model fired,
    ``auto|model=<version>|p_model=NULL`` otherwise -- the version tag is
    mandatory in both forms (see ``MODEL_VERSION``): a calibration row
    with no model version attached can never be safely compared against
    a future model revision.
    """
    if board_ts is None or n is None:
        return f"auto|model={MODEL_VERSION}|p_model=NULL"
    return f"auto|model={MODEL_VERSION}|board_ts={board_ts.isoformat()}|n={n}"


#: Prefix every auto-recorded prediction's ``note`` starts with -- how
#: ``status_report`` (and anyone else) tells an auto-recorded row apart
#: from one entered by hand via ``evhedge predict``.
AUTO_NOTE_PREFIX = "auto|"


@dataclass
class AutoPredictStatus:
    """What ``status_report`` found, straight from persisted state -- no
    ephemeral per-collector-run counters here (those already print once,
    in the ``pull`` CLI's own summary table; this is what's left in the
    database afterward, cumulative across every run).

    Attributes:
        n_covered: Auto-recorded predictions (``note`` starts with
            ``AUTO_NOTE_PREFIX``), optionally narrowed to one tournament.
        n_model: Of those, how many have a non-NULL ``p_model``.
        n_model_null: The rest -- market-only (no/heterogeneous stage_ranks).
        n_resolved_without_prediction: Resolves rows with NO matching
            prediction (auto or manual) at the same (tournament, team,
            market) key -- the direct, measured selection-bias check this
            module exists to drive toward zero. ``None`` if it couldn't be
            computed (see ``status_report``'s ``tournament`` handling).
        recent: Up to 10 most recent auto-recorded predictions, newest first.
    """

    n_covered: int
    n_model: int
    n_model_null: int
    n_resolved_without_prediction: Optional[int]
    recent: list[Prediction] = field(default_factory=list)


def status_report(store: Storage, tournament: Optional[str] = None) -> AutoPredictStatus:
    """Build ``AutoPredictStatus`` from what's actually in ``store``.

    DESIGN CHOICE: the task asked for "covered / waiting for trigger /
    skipped (live, duplicate)". "Waiting" and "skipped" are per-collector-
    run counts (``CollectSummary.predictions_written`` etc.) that are
    already printed once by the ``pull`` CLI and not persisted anywhere
    to be queried back later -- inventing a new log table just for this
    status view was judged out of scope for a first cut. What IS
    persisted and queryable is covered-vs-not-covered, which is reported
    here instead, plus the resolved-without-prediction selection-bias
    check (arguably the more useful number, since it's the one this
    module's whole justification rests on).
    """
    all_preds = store.predictions(tournament=tournament)
    auto_preds = [p for p in all_preds if p.note and p.note.startswith(AUTO_NOTE_PREFIX)]
    n_model = sum(1 for p in auto_preds if p.p_model is not None)
    n_model_null = len(auto_preds) - n_model
    recent = sorted(auto_preds, key=lambda p: p.ts_utc, reverse=True)[:10]

    if tournament is not None:
        tournaments = [tournament]
    else:
        tournaments = [
            row[0] for row in store._conn.execute("SELECT DISTINCT tournament FROM resolves")
        ]

    covered_keys = {(p.tournament, p.team, p.market) for p in all_preds}
    n_resolved_without_prediction = 0
    for t in tournaments:
        for r in store.resolves(t):
            if (r.tournament, r.team, r.market) not in covered_keys:
                n_resolved_without_prediction += 1

    return AutoPredictStatus(
        n_covered=len(auto_preds),
        n_model=n_model,
        n_model_null=n_model_null,
        n_resolved_without_prediction=n_resolved_without_prediction,
        recent=recent,
    )
