"""SQLite-backed memory between runs: price snapshots, scan passports,
market resolves.

Everything else in evhedge is deliberately stateless -- a config goes in,
a report comes out, nothing survives the process. This module is the one
place state lives, so three rules keep it honest:

1. Units follow the producer, not the database: snapshot prices are in
   PERCENT (91.9 = 91.9c), same convention as ``scanner.ScannerConfig``
   -- because that's what gets snapshotted. No unit conversion at the
   storage boundary.
2. Timestamps are timezone-aware UTC, stored as ISO 8601 text. Naive
   datetimes are rejected, not guessed at -- a snapshot whose time zone
   is ambiguous is worse than no snapshot (velocity math would silently
   be off by hours).
3. Schema changes ship as append-only migrations over ``PRAGMA
   user_version`` -- a database created by an older evhedge must open
   cleanly in a newer one (that's the whole point of memory BETWEEN
   runs). Since v5, ``team``/``counterparty`` are canonical names (see
   ``evhedge.team_aliases``): every writer canonicalizes before calling
   ``record_snapshot`` so a team's outright history and its leg prices
   join on the same key regardless of which spelling Polymarket happened
   to use on which board.

Snapshot ``market`` labels are free text by design, but stick to the
conventions used across the project so velocity lookups match writes:
``winner_no`` / ``winner_yes`` for outright markets, ``reach_final_no`` /
``reach_final_yes`` for milestone markets, and ``leg`` with
``counterparty`` set for a specific-match price.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional, Union

#: Where a snapshot price came from. "board" is a display price (Gamma
#: outcomePrices / config YAML); "book" is an order-book-verified price
#: (see the PROJECT RULE in data_sources/polymarket.py). Velocity math
#: accepts both, but only "book" prices are tradable.
SNAPSHOT_SOURCES = ("board", "book")

#: How a binary market can resolve.
RESOLVE_OUTCOMES = ("yes", "no")


class StorageError(Exception):
    """Raised for storage-layer problems: invalid snapshot fields, a
    database file that can't be opened, or a schema newer than this
    version of evhedge understands."""


@dataclass
class PriceSnapshot:
    """One observed price at one moment.

    Attributes:
        tournament: Tournament label, same string used in scanner configs.
        team: Team/market subject name.
        market: Market label -- see module docstring for conventions.
        price_pct: Price in percent (scanner convention), in (0, 100). The
            tradable buy price (= ``ask_pct``) when ``source == "book"``;
            Gamma's display value when ``source == "board"``.
        source: "board" (Gamma's ``outcomePrices`` -- for a binary Yes/No
            market this is a SINGLE derived number, not two independently
            traded prices: every board snapshot pair sums to exactly 100.0
            by construction, which is why it carries no spread information
            at all) or "book" (real order-book best bid/ask via
            ``data_sources.polymarket.fetch_order_book`` -- has a genuine,
            usually non-complementary spread).
        bid_pct: Best bid, in percent, when ``source == "book"``. None for
            "board" snapshots (Gamma doesn't expose it).
        ask_pct: Best ask, in percent, when ``source == "book"``. None for
            "board" snapshots.
        volume_usd: Market/event volume in USD at snapshot time, when
            known. Without this, a price move and a single $20 trade in an
            empty book look identical.
        ts_utc: Timezone-aware UTC timestamp of the observation.
        counterparty: Opponent name for ``market="leg"`` snapshots;
            None for outright/milestone markets.
        token_id: Optional CLOB token id the price was read from.
        raw_team: The team name exactly as the source gave it, before
            ``team_aliases.canonical_name`` ran, when that differs from
            ``team``. None if the source name already was the canonical
            form (or the row predates schema v5 and has never been
            touched). Kept for debugging/backfilling the alias map, never
            used as a lookup key.
        id: Database row id; None until stored.
    """

    tournament: str
    team: str
    market: str
    price_pct: float
    source: str
    ts_utc: datetime
    counterparty: Optional[str] = None
    token_id: Optional[str] = None
    bid_pct: Optional[float] = None
    ask_pct: Optional[float] = None
    volume_usd: Optional[float] = None
    raw_team: Optional[str] = None
    id: Optional[int] = None

    def __post_init__(self) -> None:
        if not (0.0 < self.price_pct < 100.0):
            raise StorageError(
                f"PriceSnapshot({self.team!r}, {self.market!r}).price_pct must be "
                f"in (0, 100), got {self.price_pct}"
            )
        if self.source not in SNAPSHOT_SOURCES:
            raise StorageError(
                f"PriceSnapshot.source must be one of {SNAPSHOT_SOURCES}, got {self.source!r}"
            )
        if self.ts_utc.tzinfo is None:
            raise StorageError(
                f"PriceSnapshot({self.team!r}, {self.market!r}).ts_utc must be "
                f"timezone-aware (naive datetimes are ambiguous; velocity math "
                f"would silently be off)"
            )
        self.ts_utc = self.ts_utc.astimezone(timezone.utc)
        for label, value in (("bid_pct", self.bid_pct), ("ask_pct", self.ask_pct)):
            if value is not None and not (0.0 < value < 100.0):
                raise StorageError(
                    f"PriceSnapshot({self.team!r}, {self.market!r}).{label} must be "
                    f"in (0, 100), got {value}"
                )
        if self.bid_pct is not None and self.ask_pct is not None and self.ask_pct < self.bid_pct:
            raise StorageError(
                f"PriceSnapshot({self.team!r}, {self.market!r}): ask_pct ({self.ask_pct}) "
                f"< bid_pct ({self.bid_pct}) -- a crossed/invalid book"
            )
        if self.volume_usd is not None and self.volume_usd < 0.0:
            raise StorageError(
                f"PriceSnapshot({self.team!r}, {self.market!r}).volume_usd must be >= 0, "
                f"got {self.volume_usd}"
            )


def utcnow() -> datetime:
    """Timezone-aware UTC now -- the only timestamp factory this module
    endorses (see rule 2 in the module docstring)."""
    return datetime.now(timezone.utc)


def no_market_label(target_market: str) -> str:
    """Snapshot market label for a scanner config's NO prices:
    "winner" -> "winner_no", "reach_final" -> "reach_final_no". Keep
    writes and velocity lookups on this one function so they can't drift
    apart."""
    return f"{target_market}_no"


def board_snapshots(config, ts_utc: Optional[datetime] = None) -> list[PriceSnapshot]:
    """Board-source snapshots of everything a scanner config quotes:
    one per ``no_prices`` entry (market per ``no_market_label``) and one
    per ``leg_prices`` entry (market "leg", counterparty set).

    ``config`` is duck-typed (needs ``tournament``, ``target_market``,
    ``no_prices``, ``leg_prices``) so storage stays import-free of
    scanner. All snapshots share one timestamp -- they're one observation
    of one board.
    """
    ts = ts_utc or utcnow()
    market = no_market_label(config.target_market)
    snaps = [
        PriceSnapshot(
            tournament=config.tournament, team=team, market=market,
            price_pct=price, source="board", ts_utc=ts,
        )
        for team, price in config.no_prices.items()
    ]
    for (team_a, team_b), ask_pct in config.leg_prices.items():
        snaps.append(PriceSnapshot(
            tournament=config.tournament, team=team_a, market="leg",
            price_pct=ask_pct, source="board", ts_utc=ts, counterparty=team_b,
        ))
    return snaps


@dataclass
class Resolve:
    """How one market actually resolved.

    ``market`` uses the same labels as ``PriceSnapshot.market`` (see
    module docstring) so resolves join cleanly against snapshots and
    passports by (tournament, team, market).

    Attributes:
        outcome: "yes" or "no" -- the market's resolution, NOT whether our
            position won (a NO position wins on outcome "no").
        note: Free text, e.g. "eliminated in QF by France, 0:2".
    """

    tournament: str
    team: str
    market: str
    outcome: str
    ts_utc: datetime
    note: Optional[str] = None
    id: Optional[int] = None

    def __post_init__(self) -> None:
        if self.outcome not in RESOLVE_OUTCOMES:
            raise StorageError(
                f"Resolve({self.team!r}, {self.market!r}).outcome must be one of "
                f"{RESOLVE_OUTCOMES}, got {self.outcome!r}"
            )
        if self.ts_utc.tzinfo is None:
            raise StorageError(
                f"Resolve({self.team!r}, {self.market!r}).ts_utc must be timezone-aware"
            )
        self.ts_utc = self.ts_utc.astimezone(timezone.utc)


@dataclass
class ScanRun:
    """One recorded ``scanner.scan()`` invocation. A run with zero
    passports is still recorded -- "the scan found nothing" is
    information, not an error."""

    tournament: str
    target_market: str
    ts_utc: datetime
    config_path: Optional[str] = None
    id: Optional[int] = None


@dataclass
class ScanPassport:
    """One candidate's archived report from one run.

    Scalar fields are lifted straight from ``scanner.CandidateReport``
    (same names, same units) for SQL-friendly querying. ``report`` is the
    ENTIRE report as decoded JSON -- with the usual JSON round-trip
    caveats: dict keys become strings (``bench_depth``'s round numbers
    included) and tuples become lists. For analysis use the scalar
    columns; ``report`` is the archive of record.
    """

    run_id: int
    team: str
    fuel_verdict: str
    data_complete: bool
    no_price: float
    premium_pct: float
    required_multiplier: float
    available_multiplier: float
    deadness: float
    p_stays_dead: float
    ev_lockin: float
    ev_hold: float
    report: dict
    id: Optional[int] = None


@dataclass
class Prediction:
    """A forecast fixed BEFORE a market resolves: model probability,
    Polymarket book, and Pinnacle devig range, all captured at one
    moment. IMMUTABLE once recorded -- see ``Storage.record_prediction``.

    ``market`` uses the same labels as ``Resolve.market`` (see the module
    docstring); scoring is a plain JOIN on (tournament, team, market).

    Units: p_market_bid/p_market_ask/p_pin_low/p_pin_high are 0..1
    FRACTIONS, not percent -- deliberately different from
    ``PriceSnapshot.price_pct`` (0..100), because they come straight out
    of ``data_sources.polymarket.best_bid_ask`` and
    ``data_sources.pinnacle.devig_range``, both of which already work in
    0..1, and this module shouldn't invent a conversion those callers
    don't need.

    Attributes:
        p_model: power_model probability, None if the model didn't apply
            to this pair (see power_model.py's calibration limits).
        p_market_bid/p_market_ask: Polymarket YES order-book best
            bid/ask at fixation time (from the BOOK, not the display
            board -- see the PROJECT RULE in data_sources/polymarket.py).
            Either both set or both None.
        p_pin_low/p_pin_high: ``data_sources.pinnacle.devig_range``
            bracket for this team's outcome, None if no Pinnacle odds
            were entered. Either both set or both None.
        outcome/outcome_ts: Filled in by ``Storage.score_predictions``
            (a JOIN against ``resolves``), NEVER at prediction time and
            never touched by ``record_prediction`` -- this is the one
            exception to immutability: it's a cache of "this forecast is
            now scoreable", not a change to the forecast itself.
        note: Free text.
    """

    tournament: str
    team: str
    market: str
    ts_utc: datetime
    p_model: Optional[float] = None
    p_market_bid: Optional[float] = None
    p_market_ask: Optional[float] = None
    p_pin_low: Optional[float] = None
    p_pin_high: Optional[float] = None
    note: Optional[str] = None
    outcome: Optional[str] = None
    outcome_ts: Optional[datetime] = None
    id: Optional[int] = None

    def __post_init__(self) -> None:
        if self.ts_utc.tzinfo is None:
            raise StorageError(
                f"Prediction({self.team!r}, {self.market!r}).ts_utc must be timezone-aware"
            )
        self.ts_utc = self.ts_utc.astimezone(timezone.utc)

        for label, value in (
            ("p_model", self.p_model),
            ("p_market_bid", self.p_market_bid), ("p_market_ask", self.p_market_ask),
            ("p_pin_low", self.p_pin_low), ("p_pin_high", self.p_pin_high),
        ):
            if value is not None and not (0.0 < value < 1.0):
                raise StorageError(
                    f"Prediction({self.team!r}, {self.market!r}).{label} must be "
                    f"in (0, 1), got {value}"
                )
        if self.p_pin_low is not None and self.p_pin_high is not None:
            if self.p_pin_high < self.p_pin_low:
                raise StorageError(
                    f"Prediction({self.team!r}, {self.market!r}): p_pin_high "
                    f"({self.p_pin_high}) < p_pin_low ({self.p_pin_low})"
                )
        if self.outcome is not None and self.outcome not in RESOLVE_OUTCOMES:
            raise StorageError(
                f"Prediction({self.team!r}, {self.market!r}).outcome must be one of "
                f"{RESOLVE_OUTCOMES} or None, got {self.outcome!r}"
            )
        if self.outcome_ts is not None:
            if self.outcome_ts.tzinfo is None:
                raise StorageError(
                    f"Prediction({self.team!r}, {self.market!r}).outcome_ts must be "
                    f"timezone-aware"
                )
            self.outcome_ts = self.outcome_ts.astimezone(timezone.utc)


@dataclass
class ScoredPrediction:
    """One prediction joined against its (now known) resolve."""

    prediction: Prediction
    outcome: str
    y: int   # 1 if outcome == "yes" else 0
    brier_model: Optional[float]
    brier_market: Optional[float]
    brier_pin_mid: Optional[float]
    pin_range_hit: Optional[bool]   # p_model in [p_pin_low, p_pin_high]; None if not checkable


@dataclass
class ScoreReport:
    """Output of ``Storage.score_predictions``.

    Each ``mean_brier_*``/``n_*`` pair is computed only over the scored
    predictions where that particular source's inputs were present --
    the three sources don't necessarily share the same N. ``pending``
    (no resolve yet) is never part of any metric here, only listed.
    """

    tournament: Optional[str]
    scored: list[ScoredPrediction]
    pending: list[Prediction]
    n: int
    n_model: int
    mean_brier_model: Optional[float]
    n_market: int
    mean_brier_market: Optional[float]
    n_pin: int
    mean_brier_pin_mid: Optional[float]
    delta_model_minus_market: Optional[float]   # positive = market beat model
    n_range_checkable: int
    pin_range_hit_rate: Optional[float]


@dataclass
class PSResult:
    """A mirror of one PandaScore match's current state -- schedule,
    stage, and (once decided) result. Independent of Polymarket: this is
    "sporting truth" for speed and reconciliation, NOT the truth this
    project trades on. DESIGN CHOICE: ``Storage.record_ps_result`` never
    writes into ``resolves`` -- ``resolves`` stays exactly what it always
    was, a record of how the MARKET resolved (Gamma), because a market
    can in principle resolve differently from (or later than) the
    real-world sporting outcome (disputes, technical forfeits, an oracle
    lag). ``evhedge reconcile`` is what compares the two and flags a gap
    -- as a warning to look at, never as an automatic correction.

    Unlike ``Prediction``, this is NOT immutable: it's a refreshable
    cache of PandaScore's own current state, re-upserted by
    ``ps_match_id`` on every sync pass (a match's score/status/winner
    genuinely change as it's played -- there is no calibration integrity
    to protect here, only currency).

    Attributes:
        ps_match_id: PandaScore's own match id -- the natural key.
        tournament: OUR canonical tournament label (matches
            ``Resolve.tournament``/``Prediction.tournament``), not
            PandaScore's own League/Series/Tournament naming.
        team_a/team_b: Canonical team names (``team_aliases``).
        winner: Canonical name of the winning team, or ``None``
            (unplayed, draw, or forfeit-with-no-winner).
        score_a/score_b: Map score (games won), or ``None`` before any
            game completes.
        stage: PandaScore's ``tournament.name`` (their "Tournament" =
            our "stage", e.g. "Group A", "Playoffs" -- see
            ``data_sources.pandascore`` module docstring).
        best_of: ``number_of_games`` from PandaScore (3 for Bo3, ...).
        status: PandaScore's own match status string
            ("not_started"/"running"/"finished"/"canceled"/...), passed
            through verbatim -- never re-interpreted here.
        begin_at: Actual start once the match has genuinely begun,
            ``None`` before that.
        scheduled_at: PandaScore's PLANNED start time -- set as soon as
            a match is created, well before ``begin_at``. This is what
            ``evhedge deadlines`` counts hours against; ``begin_at``
            alone would be useless for an upcoming match (it's null
            until the match is actually live).
    """

    ps_match_id: int
    tournament: str
    team_a: str
    team_b: str
    stage: str
    best_of: int
    status: str
    ts_utc: datetime
    winner: Optional[str] = None
    score_a: Optional[int] = None
    score_b: Optional[int] = None
    begin_at: Optional[datetime] = None
    scheduled_at: Optional[datetime] = None
    id: Optional[int] = None

    def __post_init__(self) -> None:
        if self.ts_utc.tzinfo is None:
            raise StorageError(f"PSResult(ps_match_id={self.ps_match_id}).ts_utc must be timezone-aware")
        self.ts_utc = self.ts_utc.astimezone(timezone.utc)
        for label in ("begin_at", "scheduled_at"):
            value = getattr(self, label)
            if value is not None:
                if value.tzinfo is None:
                    raise StorageError(
                        f"PSResult(ps_match_id={self.ps_match_id}).{label} must be timezone-aware"
                    )
                setattr(self, label, value.astimezone(timezone.utc))


def _row_to_ps_result(row: sqlite3.Row) -> PSResult:
    return PSResult(
        ps_match_id=row["ps_match_id"], tournament=row["tournament"],
        team_a=row["team_a"], team_b=row["team_b"], stage=row["stage"],
        best_of=row["best_of"], status=row["status"],
        ts_utc=datetime.fromisoformat(row["ts_utc"]),
        winner=row["winner"], score_a=row["score_a"], score_b=row["score_b"],
        begin_at=datetime.fromisoformat(row["begin_at"]) if row["begin_at"] else None,
        scheduled_at=datetime.fromisoformat(row["scheduled_at"]) if row["scheduled_at"] else None,
        id=row["id"],
    )


def _row_to_prediction(row: sqlite3.Row) -> Prediction:
    return Prediction(
        tournament=row["tournament"], team=row["team"], market=row["market"],
        ts_utc=datetime.fromisoformat(row["ts_utc"]),
        p_model=row["p_model"], p_market_bid=row["p_market_bid"],
        p_market_ask=row["p_market_ask"], p_pin_low=row["p_pin_low"],
        p_pin_high=row["p_pin_high"], note=row["note"], outcome=row["outcome"],
        outcome_ts=datetime.fromisoformat(row["outcome_ts"]) if row["outcome_ts"] else None,
        id=row["id"],
    )


def _migrate_v4_to_v5(conn: sqlite3.Connection) -> None:
    """v4 -> v5: add ``raw_team`` and canonicalize every existing row's
    ``team``/``counterparty`` (and ``resolves.team``) against the packaged
    default alias map (see ``evhedge.team_aliases``). The actual rewrite
    is ``team_aliases.recanonicalize`` -- also callable later, on demand,
    if the alias map grows after this migration has already run once (see
    ``Storage.recanonicalize_teams``)."""
    from evhedge.team_aliases import load_default_aliases, recanonicalize  # avoid import cycle

    conn.executescript("ALTER TABLE price_snapshots ADD COLUMN raw_team TEXT;")
    recanonicalize(conn, load_default_aliases())


#: Append-only migration scripts; index i upgrades user_version i -> i+1.
#: Each entry is either a raw SQL script (str) or a callable(conn) for
#: migrations that also need to rewrite data (see v4->v5).
#: NEVER edit an entry that has shipped -- add a new one.
_MIGRATIONS: list = [
    # v0 -> v1: price snapshots.
    """
    CREATE TABLE price_snapshots (
        id           INTEGER PRIMARY KEY,
        ts_utc       TEXT NOT NULL,
        tournament   TEXT NOT NULL,
        team         TEXT NOT NULL,
        market       TEXT NOT NULL,
        price_pct    REAL NOT NULL,
        source       TEXT NOT NULL,
        counterparty TEXT,
        token_id     TEXT
    );
    CREATE INDEX idx_snapshots_lookup
        ON price_snapshots (tournament, team, market, ts_utc);
    """,
    # v1 -> v2: scan passports (one run = one scan() invocation; one
    # passport = one CandidateReport). Scalar columns mirror the report
    # fields worth querying in SQL; report_json archives the WHOLE report.
    """
    CREATE TABLE scan_runs (
        id            INTEGER PRIMARY KEY,
        ts_utc        TEXT NOT NULL,
        tournament    TEXT NOT NULL,
        target_market TEXT NOT NULL,
        config_path   TEXT
    );
    CREATE INDEX idx_runs_tournament ON scan_runs (tournament, ts_utc);

    CREATE TABLE scan_passports (
        id                   INTEGER PRIMARY KEY,
        run_id               INTEGER NOT NULL REFERENCES scan_runs (id),
        team                 TEXT NOT NULL,
        fuel_verdict         TEXT NOT NULL,
        data_complete        INTEGER NOT NULL,
        no_price             REAL NOT NULL,
        premium_pct          REAL NOT NULL,
        required_multiplier  REAL NOT NULL,
        available_multiplier REAL NOT NULL,
        deadness             REAL NOT NULL,
        p_stays_dead         REAL NOT NULL,
        ev_lockin            REAL NOT NULL,
        ev_hold              REAL NOT NULL,
        report_json          TEXT NOT NULL
    );
    CREATE INDEX idx_passports_run ON scan_passports (run_id);
    """,
    # v2 -> v3: market resolves -- how it actually ended. Joined to
    # passports by (tournament, team, market) later, this is what turns
    # archived verdicts into a calibration report.
    """
    CREATE TABLE resolves (
        id         INTEGER PRIMARY KEY,
        ts_utc     TEXT NOT NULL,
        tournament TEXT NOT NULL,
        team       TEXT NOT NULL,
        market     TEXT NOT NULL,
        outcome    TEXT NOT NULL,
        note       TEXT
    );
    CREATE INDEX idx_resolves_lookup ON resolves (tournament, team, market);
    """,
    # v3 -> v4: real bid/ask + volume on price_snapshots. Board snapshots
    # (Gamma outcomePrices) leave bid_pct/ask_pct NULL -- they were never
    # two independently-traded prices to begin with (a binary market's
    # Yes/No pair sums to exactly 100.0 by construction). Only "book"
    # snapshots (data_sources.polymarket.fetch_order_book) populate them.
    """
    ALTER TABLE price_snapshots ADD COLUMN bid_pct REAL;
    ALTER TABLE price_snapshots ADD COLUMN ask_pct REAL;
    ALTER TABLE price_snapshots ADD COLUMN volume_usd REAL;
    """,
    # v4 -> v5: canonical team names (evhedge.team_aliases). Polymarket
    # names the same team differently across market types of the same
    # tournament ("1W" on the winner board vs "1win" on match legs),
    # which silently breaks any join between a team's outright history and
    # its leg prices. This is a DATA migration, not just schema: every
    # existing row's `team` is rewritten to its canonical form (against
    # the packaged default alias map) and the original is preserved in
    # the new `raw_team` column -- a Python function, not a SQL string,
    # since it needs evhedge.team_aliases.canonical_name.
    _migrate_v4_to_v5,
    # v5 -> v6: dedup resolves. Nothing enforced (team, market) to resolve
    # once per tournament, so a poller re-observing an already-closed
    # market on every cycle (e.g. ewc_watch.bat, 15-min interval) wrote a
    # fresh row every time -- 2,566 rows for 56 actual outcomes on the
    # live EWC database. Natural key is (tournament, team, market), NOT
    # (team, market, outcome): outcome is the resolved VALUE, not part of
    # the identity, and dropping tournament would wrongly collide same-
    # named teams across different tournaments. Existing duplicates are
    # collapsed to the earliest row (MIN(id)) per key before the unique
    # index is created, since CREATE UNIQUE INDEX fails outright on a
    # table that already has duplicates.
    """
    DELETE FROM resolves WHERE id NOT IN (
        SELECT MIN(id) FROM resolves GROUP BY tournament, team, market
    );
    CREATE UNIQUE INDEX idx_resolves_unique ON resolves (tournament, team, market);
    """,
    # v6 -> v7: predictions -- a forecast fixed BEFORE a market resolves
    # (model probability, Polymarket book, Pinnacle devig range), so it can
    # later be scored against the ``resolves`` row for the same
    # (tournament, team, market). UNIQUE on that triple makes a prediction
    # IMMUTABLE by construction: Storage.record_prediction never UPDATEs an
    # existing row, a repeat is a StorageError -- see its docstring for why
    # (a calibration record you can quietly edit after the fact isn't one).
    # outcome/outcome_ts are the one exception: filled in later by
    # score_predictions() as a cache of "this is now scoreable", not a
    # revision of the forecast itself.
    """
    CREATE TABLE predictions (
        id            INTEGER PRIMARY KEY,
        ts_utc        TEXT NOT NULL,
        tournament    TEXT NOT NULL,
        team          TEXT NOT NULL,
        market        TEXT NOT NULL,
        p_model       REAL,
        p_market_bid  REAL,
        p_market_ask  REAL,
        p_pin_low     REAL,
        p_pin_high    REAL,
        note          TEXT,
        outcome       TEXT,
        outcome_ts    TEXT
    );
    CREATE UNIQUE INDEX idx_predictions_unique ON predictions (tournament, team, market);
    """,
    # v7 -> v8: ps_results -- a refreshable mirror of PandaScore match
    # state (schedule/stage/result), independent of Polymarket. UNIQUE on
    # ps_match_id makes Storage.record_ps_result an upsert (unlike
    # predictions, this table is NOT immutable -- a match's score/status
    # genuinely changes as it's played). Never written into `resolves`
    # -- see PSResult's docstring on why the two stay separate sources
    # of truth (market resolution vs sporting result), reconciled by
    # `evhedge reconcile`, not merged.
    """
    CREATE TABLE ps_results (
        id           INTEGER PRIMARY KEY,
        ts_utc       TEXT NOT NULL,
        ps_match_id  INTEGER NOT NULL,
        tournament   TEXT NOT NULL,
        team_a       TEXT NOT NULL,
        team_b       TEXT NOT NULL,
        winner       TEXT,
        score_a      INTEGER,
        score_b      INTEGER,
        stage        TEXT NOT NULL,
        best_of      INTEGER NOT NULL,
        status       TEXT NOT NULL,
        begin_at     TEXT,
        scheduled_at TEXT
    );
    CREATE UNIQUE INDEX idx_ps_results_unique ON ps_results (ps_match_id);
    """,
]

SCHEMA_VERSION = len(_MIGRATIONS)


class Storage:
    """One evhedge SQLite database. Usable as a context manager::

        with Storage("evhedge.db") as store:
            store.record_snapshot(snap)

    Opening a path that doesn't exist creates and migrates it; opening an
    older database migrates it forward; opening a NEWER database (written
    by a later evhedge) raises ``StorageError`` instead of guessing.
    """

    def __init__(self, path: Union[str, Path]):
        self.path = Path(path)
        try:
            self._conn = sqlite3.connect(self.path)
        except sqlite3.Error as e:
            raise StorageError(f"не удалось открыть БД {self.path}: {e}") from e
        self._conn.row_factory = sqlite3.Row
        self._migrate()

    # -- lifecycle ----------------------------------------------------------

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Storage":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def _migrate(self) -> None:
        (version,) = self._conn.execute("PRAGMA user_version").fetchone()
        if version > SCHEMA_VERSION:
            raise StorageError(
                f"{self.path}: схема БД версии {version}, эта сборка evhedge "
                f"понимает максимум {SCHEMA_VERSION} — база создана более "
                f"новой версией, обновите evhedge, а не откатывайте базу"
            )
        for i in range(version, SCHEMA_VERSION):
            with self._conn:
                step = _MIGRATIONS[i]
                if callable(step):
                    step(self._conn)
                else:
                    self._conn.executescript(step)
                self._conn.execute(f"PRAGMA user_version = {i + 1}")

    # -- price snapshots ------------------------------------------------------

    def record_snapshot(self, snapshot: PriceSnapshot) -> int:
        """Store one snapshot; returns its row id (also set on the object)."""
        with self._conn:
            cursor = self._conn.execute(
                """
                INSERT INTO price_snapshots
                    (ts_utc, tournament, team, market, price_pct, source,
                     counterparty, token_id, bid_pct, ask_pct, volume_usd, raw_team)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot.ts_utc.isoformat(),
                    snapshot.tournament,
                    snapshot.team,
                    snapshot.market,
                    snapshot.price_pct,
                    snapshot.source,
                    snapshot.counterparty,
                    snapshot.token_id,
                    snapshot.bid_pct,
                    snapshot.ask_pct,
                    snapshot.volume_usd,
                    snapshot.raw_team,
                ),
            )
        snapshot.id = cursor.lastrowid
        return snapshot.id

    def record_snapshots(self, snapshots: Iterable[PriceSnapshot]) -> list[int]:
        return [self.record_snapshot(s) for s in snapshots]

    def snapshots(
        self,
        tournament: str,
        team: Optional[str] = None,
        market: Optional[str] = None,
        since: Optional[datetime] = None,
    ) -> list[PriceSnapshot]:
        """Snapshots for a tournament, optionally narrowed to one team
        and/or market, optionally only at/after ``since`` (must be
        timezone-aware). Ordered by timestamp ascending -- oldest first,
        ready for velocity math."""
        query = "SELECT * FROM price_snapshots WHERE tournament = ?"
        params: list = [tournament]
        if team is not None:
            query += " AND team = ?"
            params.append(team)
        if market is not None:
            query += " AND market = ?"
            params.append(market)
        if since is not None:
            if since.tzinfo is None:
                raise StorageError("snapshots(since=...) must be timezone-aware")
            query += " AND ts_utc >= ?"
            params.append(since.astimezone(timezone.utc).isoformat())
        query += " ORDER BY ts_utc ASC"

        return [
            PriceSnapshot(
                tournament=row["tournament"],
                team=row["team"],
                market=row["market"],
                price_pct=row["price_pct"],
                source=row["source"],
                ts_utc=datetime.fromisoformat(row["ts_utc"]),
                counterparty=row["counterparty"],
                token_id=row["token_id"],
                bid_pct=row["bid_pct"],
                ask_pct=row["ask_pct"],
                volume_usd=row["volume_usd"],
                raw_team=row["raw_team"],
                id=row["id"],
            )
            for row in self._conn.execute(query, params)
        ]

    def distinct_team_names(self, tournament: Optional[str] = None) -> list[str]:
        """Every distinct name seen as either ``team`` or ``counterparty``
        in ``price_snapshots``, optionally scoped to one tournament --
        the raw-name universe ``team_aliases.suggest_aliases`` and the
        `evhedge aliases` CLI audit against."""
        query = "SELECT team AS name FROM price_snapshots"
        params: list = []
        if tournament is not None:
            query += " WHERE tournament = ?"
            params.append(tournament)
        query += " UNION SELECT counterparty AS name FROM price_snapshots WHERE counterparty IS NOT NULL"
        if tournament is not None:
            query += " AND tournament = ?"
            params.append(tournament)
        return sorted({row["name"] for row in self._conn.execute(query, params)})

    def recanonicalize_teams(self, alias_map: Optional[dict] = None) -> dict:
        """Re-apply team name canonicalization to every row already in
        this database (``team_aliases.recanonicalize``) -- for when the
        alias map has grown SINCE this database's v4->v5 migration
        already ran once (a schema migration only ever canonicalizes
        against the alias map that existed at that moment; new entries
        added later don't retroactively apply on their own).

        Args:
            alias_map: Defaults to ``team_aliases.load_default_aliases()``
                if omitted.

        Returns:
            Counts of rows actually changed per column -- see
            ``team_aliases.recanonicalize``.
        """
        from evhedge.team_aliases import load_default_aliases, recanonicalize

        if alias_map is None:
            alias_map = load_default_aliases()
        with self._conn:
            return recanonicalize(self._conn, alias_map)

    def price_velocity(
        self,
        tournament: str,
        team: str,
        market: str,
        window: timedelta,
        now: Optional[datetime] = None,
    ) -> Optional[float]:
        """Price movement speed over the window, in percentage points per
        hour: (newest - oldest) / hours between them, over the snapshots
        inside ``[now - window, now]``.

        Returns ``None`` -- an honest "don't know", never a guess -- when
        the window holds fewer than 2 snapshots, or when they all share
        one timestamp (zero span). Sign follows the market snapshotted:
        a NEGATIVE velocity on a ``*_no`` market means the NO price is
        falling, i.e. the team's chances are rising.
        """
        now = now or utcnow()
        if now.tzinfo is None:
            raise StorageError("price_velocity(now=...) must be timezone-aware")

        rows = self.snapshots(tournament, team=team, market=market, since=now - window)
        if len(rows) < 2:
            return None
        first, last = rows[0], rows[-1]
        hours = (last.ts_utc - first.ts_utc).total_seconds() / 3600.0
        if hours <= 0.0:
            return None
        return (last.price_pct - first.price_pct) / hours

    # -- scan passports -------------------------------------------------------

    def record_scan(
        self,
        tournament: str,
        target_market: str,
        reports: Iterable,
        config_path: Optional[str] = None,
        ts_utc: Optional[datetime] = None,
    ) -> int:
        """Archive one ``scanner.scan()`` invocation: a ``scan_runs`` row
        plus one passport per ``CandidateReport``. Returns the run id.

        ``reports`` are ``scanner.CandidateReport`` dataclasses (typed
        loosely to keep storage import-free of scanner); an empty iterable
        still records the run.
        """
        ts = ts_utc or utcnow()
        if ts.tzinfo is None:
            raise StorageError("record_scan(ts_utc=...) must be timezone-aware")

        with self._conn:
            cursor = self._conn.execute(
                "INSERT INTO scan_runs (ts_utc, tournament, target_market, config_path)"
                " VALUES (?, ?, ?, ?)",
                (ts.astimezone(timezone.utc).isoformat(), tournament, target_market,
                 str(config_path) if config_path is not None else None),
            )
            run_id = cursor.lastrowid
            for r in reports:
                self._conn.execute(
                    """
                    INSERT INTO scan_passports
                        (run_id, team, fuel_verdict, data_complete, no_price,
                         premium_pct, required_multiplier, available_multiplier,
                         deadness, p_stays_dead, ev_lockin, ev_hold, report_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id, r.team, r.fuel_verdict, int(r.data_complete),
                        r.no_price, r.premium_pct, r.required_multiplier,
                        r.available_multiplier, r.deadness, r.p_stays_dead,
                        r.ev_lockin, r.ev_hold,
                        json.dumps(asdict(r), ensure_ascii=False),
                    ),
                )
        return run_id

    def runs(self, tournament: Optional[str] = None) -> list[ScanRun]:
        """Recorded runs, newest first, optionally for one tournament."""
        query = "SELECT * FROM scan_runs"
        params: list = []
        if tournament is not None:
            query += " WHERE tournament = ?"
            params.append(tournament)
        query += " ORDER BY ts_utc DESC, id DESC"
        return [
            ScanRun(
                tournament=row["tournament"], target_market=row["target_market"],
                ts_utc=datetime.fromisoformat(row["ts_utc"]),
                config_path=row["config_path"], id=row["id"],
            )
            for row in self._conn.execute(query, params)
        ]

    def latest_run(self, tournament: str) -> Optional[ScanRun]:
        found = self.runs(tournament)
        return found[0] if found else None

    def passports(self, run_id: int) -> list[ScanPassport]:
        """Passports of one run, in the order the scan produced them."""
        return [
            ScanPassport(
                run_id=row["run_id"], team=row["team"],
                fuel_verdict=row["fuel_verdict"],
                data_complete=bool(row["data_complete"]),
                no_price=row["no_price"], premium_pct=row["premium_pct"],
                required_multiplier=row["required_multiplier"],
                available_multiplier=row["available_multiplier"],
                deadness=row["deadness"], p_stays_dead=row["p_stays_dead"],
                ev_lockin=row["ev_lockin"], ev_hold=row["ev_hold"],
                report=json.loads(row["report_json"]), id=row["id"],
            )
            for row in self._conn.execute(
                "SELECT * FROM scan_passports WHERE run_id = ? ORDER BY id ASC",
                (run_id,),
            )
        ]

    # -- resolves ---------------------------------------------------------------

    def record_resolve(self, resolve: Resolve) -> int:
        """Store one market resolution. Idempotent on (tournament, team,
        market) -- a market resolves once; a poller re-observing an
        already-closed market on every cycle (e.g. ewc_watch.bat) must
        not pile up a fresh row every time. Returns the EXISTING row's id
        (unchanged) if this exact (tournament, team, market) was already
        recorded with the same outcome.

        Raises:
            StorageError: If (tournament, team, market) was already
                recorded with a DIFFERENT outcome -- a market resolving
                two different ways is a genuine data problem, not
                something to silently dedupe away.
        """
        with self._conn:
            cursor = self._conn.execute(
                "INSERT OR IGNORE INTO resolves (ts_utc, tournament, team, market, outcome, note)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (resolve.ts_utc.isoformat(), resolve.tournament, resolve.team,
                 resolve.market, resolve.outcome, resolve.note),
            )
            if cursor.rowcount == 1:
                resolve.id = cursor.lastrowid
                return resolve.id

            existing = self._conn.execute(
                "SELECT id, outcome FROM resolves WHERE tournament = ? AND team = ? AND market = ?",
                (resolve.tournament, resolve.team, resolve.market),
            ).fetchone()

        if existing["outcome"] != resolve.outcome:
            raise StorageError(
                f"Resolve({resolve.team!r}, {resolve.market!r}) already resolved as "
                f"{existing['outcome']!r}, now reported as {resolve.outcome!r} -- "
                f"conflicting data, not deduped"
            )
        resolve.id = existing["id"]
        return resolve.id

    def resolves(
        self,
        tournament: str,
        team: Optional[str] = None,
        market: Optional[str] = None,
    ) -> list[Resolve]:
        """Resolves for a tournament, optionally narrowed to one team
        and/or market, ordered by timestamp ascending."""
        query = "SELECT * FROM resolves WHERE tournament = ?"
        params: list = [tournament]
        if team is not None:
            query += " AND team = ?"
            params.append(team)
        if market is not None:
            query += " AND market = ?"
            params.append(market)
        query += " ORDER BY ts_utc ASC"
        return [
            Resolve(
                tournament=row["tournament"], team=row["team"], market=row["market"],
                outcome=row["outcome"], ts_utc=datetime.fromisoformat(row["ts_utc"]),
                note=row["note"], id=row["id"],
            )
            for row in self._conn.execute(query, params)
        ]

    # -- predictions / calibration --------------------------------------------

    def record_prediction(self, prediction: Prediction) -> int:
        """Store one prediction. IMMUTABLE on (tournament, team, market):
        a second call for the same key is always a ``StorageError``, never
        an update -- no ``--force``, no way to silently revise a forecast
        after the fact (see ``Prediction``'s docstring). If a prediction
        genuinely has a typo, fix it by hand in sqlite; this method will
        not help you do that, on purpose.

        Returns the new row's id (also set on the object).
        """
        try:
            with self._conn:
                cursor = self._conn.execute(
                    """
                    INSERT INTO predictions
                        (ts_utc, tournament, team, market, p_model,
                         p_market_bid, p_market_ask, p_pin_low, p_pin_high, note)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        prediction.ts_utc.isoformat(), prediction.tournament,
                        prediction.team, prediction.market, prediction.p_model,
                        prediction.p_market_bid, prediction.p_market_ask,
                        prediction.p_pin_low, prediction.p_pin_high, prediction.note,
                    ),
                )
        except sqlite3.IntegrityError as e:
            raise StorageError(
                f"Prediction({prediction.team!r}, {prediction.market!r}) already recorded "
                f"for tournament {prediction.tournament!r} -- predictions are immutable, "
                f"a prediction is never overwritten (fix typos by hand in sqlite if truly "
                f"needed)"
            ) from e
        prediction.id = cursor.lastrowid
        return prediction.id

    def predictions(
        self,
        tournament: Optional[str] = None,
        team: Optional[str] = None,
        market: Optional[str] = None,
    ) -> list[Prediction]:
        """Predictions, optionally narrowed, ordered by timestamp ascending."""
        query = "SELECT * FROM predictions WHERE 1 = 1"
        params: list = []
        if tournament is not None:
            query += " AND tournament = ?"
            params.append(tournament)
        if team is not None:
            query += " AND team = ?"
            params.append(team)
        if market is not None:
            query += " AND market = ?"
            params.append(market)
        query += " ORDER BY ts_utc ASC"
        return [_row_to_prediction(row) for row in self._conn.execute(query, params)]

    def score_predictions(self, tournament: Optional[str] = None) -> ScoreReport:
        """Join every prediction against ``resolves`` on (tournament, team,
        market) and score it: Brier of the model, of the Polymarket book
        mid, and of the Pinnacle devig mid (each only where that source's
        inputs were actually recorded). Predictions with no matching
        resolve yet go into ``ScoreReport.pending`` instead -- never into
        any metric.

        As a side effect, backfills ``outcome``/``outcome_ts`` on newly-
        scoreable rows (a cache, not a revision -- see ``Prediction``).
        """
        preds = self.predictions(tournament=tournament)
        scored: list[ScoredPrediction] = []
        pending: list[Prediction] = []

        for pred in preds:
            resolve_rows = self.resolves(pred.tournament, team=pred.team, market=pred.market)
            if not resolve_rows:
                pending.append(pred)
                continue
            resolve = resolve_rows[0]

            if pred.outcome != resolve.outcome:
                with self._conn:
                    self._conn.execute(
                        "UPDATE predictions SET outcome = ?, outcome_ts = ? WHERE id = ?",
                        (resolve.outcome, resolve.ts_utc.isoformat(), pred.id),
                    )
                pred.outcome = resolve.outcome
                pred.outcome_ts = resolve.ts_utc

            y = 1 if resolve.outcome == "yes" else 0

            brier_model = (pred.p_model - y) ** 2 if pred.p_model is not None else None

            brier_market = None
            if pred.p_market_bid is not None and pred.p_market_ask is not None:
                mid = (pred.p_market_bid + pred.p_market_ask) / 2
                brier_market = (mid - y) ** 2

            brier_pin_mid = None
            if pred.p_pin_low is not None and pred.p_pin_high is not None:
                pin_mid = (pred.p_pin_low + pred.p_pin_high) / 2
                brier_pin_mid = (pin_mid - y) ** 2

            pin_range_hit = None
            if (
                pred.p_model is not None
                and pred.p_pin_low is not None
                and pred.p_pin_high is not None
            ):
                pin_range_hit = pred.p_pin_low <= pred.p_model <= pred.p_pin_high

            scored.append(
                ScoredPrediction(
                    prediction=pred, outcome=resolve.outcome, y=y,
                    brier_model=brier_model, brier_market=brier_market,
                    brier_pin_mid=brier_pin_mid, pin_range_hit=pin_range_hit,
                )
            )

        def _mean(values: list[float]) -> Optional[float]:
            return sum(values) / len(values) if values else None

        model_briers = [s.brier_model for s in scored if s.brier_model is not None]
        market_briers = [s.brier_market for s in scored if s.brier_market is not None]
        pin_briers = [s.brier_pin_mid for s in scored if s.brier_pin_mid is not None]
        range_hits = [s.pin_range_hit for s in scored if s.pin_range_hit is not None]

        mean_model = _mean(model_briers)
        mean_market = _mean(market_briers)
        delta = None
        if mean_model is not None and mean_market is not None:
            delta = mean_model - mean_market

        return ScoreReport(
            tournament=tournament,
            scored=scored,
            pending=pending,
            n=len(scored),
            n_model=len(model_briers),
            mean_brier_model=mean_model,
            n_market=len(market_briers),
            mean_brier_market=mean_market,
            n_pin=len(pin_briers),
            mean_brier_pin_mid=_mean(pin_briers),
            delta_model_minus_market=delta,
            n_range_checkable=len(range_hits),
            pin_range_hit_rate=_mean([1.0 if h else 0.0 for h in range_hits]),
        )

    # -- PandaScore mirror ------------------------------------------------------

    def record_ps_result(self, result: PSResult) -> int:
        """Upsert one PandaScore match's current state, keyed on
        ``ps_match_id`` -- unlike ``record_prediction``, this OVERWRITES
        on a repeat call (see ``PSResult``'s docstring: a refreshable
        mirror, not an immutable calibration record). Returns the row id
        (also set on the object).
        """
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO ps_results
                    (ts_utc, ps_match_id, tournament, team_a, team_b, winner,
                     score_a, score_b, stage, best_of, status, begin_at, scheduled_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (ps_match_id) DO UPDATE SET
                    ts_utc = excluded.ts_utc, tournament = excluded.tournament,
                    team_a = excluded.team_a, team_b = excluded.team_b,
                    winner = excluded.winner, score_a = excluded.score_a,
                    score_b = excluded.score_b, stage = excluded.stage,
                    best_of = excluded.best_of, status = excluded.status,
                    begin_at = excluded.begin_at, scheduled_at = excluded.scheduled_at
                """,
                (
                    result.ts_utc.isoformat(), result.ps_match_id, result.tournament,
                    result.team_a, result.team_b, result.winner, result.score_a,
                    result.score_b, result.stage, result.best_of, result.status,
                    result.begin_at.isoformat() if result.begin_at else None,
                    result.scheduled_at.isoformat() if result.scheduled_at else None,
                ),
            )
            row = self._conn.execute(
                "SELECT id FROM ps_results WHERE ps_match_id = ?", (result.ps_match_id,)
            ).fetchone()
        result.id = row["id"]
        return result.id

    def ps_results(self, tournament: Optional[str] = None) -> list[PSResult]:
        """PandaScore mirror rows, optionally scoped to one tournament,
        ordered by ``begin_at`` (falling back to ``scheduled_at`` for
        matches that haven't started) ascending, nulls -- neither known
        -- last."""
        query = "SELECT * FROM ps_results"
        params: list = []
        if tournament is not None:
            query += " WHERE tournament = ?"
            params.append(tournament)
        query += (
            " ORDER BY (COALESCE(begin_at, scheduled_at) IS NULL),"
            " COALESCE(begin_at, scheduled_at) ASC"
        )
        return [_row_to_ps_result(row) for row in self._conn.execute(query, params)]
