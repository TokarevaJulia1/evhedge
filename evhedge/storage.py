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

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional, Union

#: Where a snapshot price came from. "board" is a display price (Gamma
#: outcomePrices / config YAML); "book" is an order-book-verified price
#: (see the PROJECT RULE in data_sources/polymarket.py). Velocity math
#: accepts both, but only "book" prices are tradable.
SNAPSHOT_SOURCES = ("board", "book")


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
