"""Tests for evhedge.storage (SQLite memory between runs)."""

from datetime import datetime, timedelta, timezone

import pytest

from evhedge.storage import (
    SCHEMA_VERSION,
    PriceSnapshot,
    Storage,
    StorageError,
)

T0 = datetime(2026, 7, 6, 12, 0, tzinfo=timezone.utc)


def _snap(**overrides):
    kwargs = dict(
        tournament="FIFA World Cup 2026", team="Morocco", market="winner_no",
        price_pct=97.0, source="board", ts_utc=T0,
    )
    kwargs.update(overrides)
    return PriceSnapshot(**kwargs)


# --- PriceSnapshot validation ---------------------------------------------------

def test_snapshot_rejects_out_of_range_price():
    with pytest.raises(StorageError, match=r"\(0, 100\)"):
        _snap(price_pct=100.0)


def test_snapshot_rejects_unknown_source():
    with pytest.raises(StorageError, match="source"):
        _snap(source="gut_feeling")


def test_snapshot_rejects_naive_timestamp():
    with pytest.raises(StorageError, match="timezone-aware"):
        _snap(ts_utc=datetime(2026, 7, 6, 12, 0))


def test_snapshot_normalizes_timestamp_to_utc():
    plus3 = timezone(timedelta(hours=3))
    snap = _snap(ts_utc=datetime(2026, 7, 6, 15, 0, tzinfo=plus3))
    assert snap.ts_utc == T0


# --- Storage: round trip and persistence between opens ---------------------------

def test_snapshot_round_trip(tmp_path):
    db = tmp_path / "evhedge.db"
    with Storage(db) as store:
        row_id = store.record_snapshot(_snap(token_id="tok123"))
        assert row_id is not None

        (loaded,) = store.snapshots("FIFA World Cup 2026", team="Morocco")
        assert loaded.price_pct == pytest.approx(97.0)
        assert loaded.market == "winner_no"
        assert loaded.source == "board"
        assert loaded.ts_utc == T0
        assert loaded.token_id == "tok123"
        assert loaded.id == row_id


def test_memory_survives_between_opens(tmp_path):
    """The whole point of the module: a second run sees the first run's data."""
    db = tmp_path / "evhedge.db"
    with Storage(db) as store:
        store.record_snapshot(_snap())

    with Storage(db) as store:  # fresh connection, same file
        assert len(store.snapshots("FIFA World Cup 2026")) == 1


def test_snapshots_ordered_and_filtered(tmp_path):
    with Storage(tmp_path / "e.db") as store:
        store.record_snapshots([
            _snap(ts_utc=T0 + timedelta(hours=2), price_pct=95.0),
            _snap(ts_utc=T0, price_pct=97.0),
            _snap(ts_utc=T0 + timedelta(hours=1), price_pct=96.0),
            _snap(team="Norway", price_pct=95.3),
            _snap(market="winner_yes", price_pct=3.0),
            _snap(tournament="EWC 2025 Dota 2", team="PARIVISION", price_pct=91.9),
        ])

        rows = store.snapshots("FIFA World Cup 2026", team="Morocco", market="winner_no")
        assert [r.price_pct for r in rows] == [97.0, 96.0, 95.0]  # ts ascending

        recent = store.snapshots(
            "FIFA World Cup 2026", team="Morocco", market="winner_no",
            since=T0 + timedelta(hours=1),
        )
        assert [r.price_pct for r in recent] == [96.0, 95.0]

        assert len(store.snapshots("EWC 2025 Dota 2")) == 1


def test_snapshots_since_must_be_aware(tmp_path):
    with Storage(tmp_path / "e.db") as store:
        with pytest.raises(StorageError, match="timezone-aware"):
            store.snapshots("t", since=datetime(2026, 7, 6))


def test_leg_snapshot_carries_counterparty(tmp_path):
    with Storage(tmp_path / "e.db") as store:
        store.record_snapshot(
            _snap(market="leg", counterparty="Canada", price_pct=72.0)
        )
        (leg,) = store.snapshots("FIFA World Cup 2026", market="leg")
        assert leg.counterparty == "Canada"


# --- migrations -------------------------------------------------------------------

def test_migrate_sets_user_version_and_reopen_is_noop(tmp_path):
    db = tmp_path / "e.db"
    with Storage(db) as store:
        (version,) = store._conn.execute("PRAGMA user_version").fetchone()
        assert version == SCHEMA_VERSION
    with Storage(db):  # re-open: migrations must not re-run/fail
        pass


def test_newer_schema_is_rejected_not_guessed(tmp_path):
    import sqlite3

    db = tmp_path / "e.db"
    with Storage(db):
        pass
    conn = sqlite3.connect(db)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 7}")
    conn.close()

    with pytest.raises(StorageError, match="более"):
        Storage(db)
