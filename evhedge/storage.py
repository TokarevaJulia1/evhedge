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
   runs).

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
        price_pct: Price in percent (scanner convention), in (0, 100).
        source: "board" or "book".
        ts_utc: Timezone-aware UTC timestamp of the observation.
        counterparty: Opponent name for ``market="leg"`` snapshots;
            None for outright/milestone markets.
        token_id: Optional CLOB token id the price was read from.
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


#: Append-only migration scripts; index i upgrades user_version i -> i+1.
#: NEVER edit an entry that has shipped -- add a new one.
_MIGRATIONS: list[str] = [
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
                self._conn.executescript(_MIGRATIONS[i])
                self._conn.execute(f"PRAGMA user_version = {i + 1}")

    # -- price snapshots ------------------------------------------------------

    def record_snapshot(self, snapshot: PriceSnapshot) -> int:
        """Store one snapshot; returns its row id (also set on the object)."""
        with self._conn:
            cursor = self._conn.execute(
                """
                INSERT INTO price_snapshots
                    (ts_utc, tournament, team, market, price_pct, source,
                     counterparty, token_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
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
                id=row["id"],
            )
            for row in self._conn.execute(query, params)
        ]

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
        """Store one market resolution; returns its row id."""
        with self._conn:
            cursor = self._conn.execute(
                "INSERT INTO resolves (ts_utc, tournament, team, market, outcome, note)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (resolve.ts_utc.isoformat(), resolve.tournament, resolve.team,
                 resolve.market, resolve.outcome, resolve.note),
            )
        resolve.id = cursor.lastrowid
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
