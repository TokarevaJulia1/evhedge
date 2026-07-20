"""Tests for evhedge.storage (SQLite memory between runs)."""

from datetime import datetime, timedelta, timezone

import pytest

from evhedge.storage import (
    SCHEMA_VERSION,
    PriceSnapshot,
    Resolve,
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


def test_snapshot_rejects_out_of_range_bid_or_ask():
    with pytest.raises(StorageError, match="bid_pct"):
        _snap(bid_pct=0.0, ask_pct=50.0)
    with pytest.raises(StorageError, match="ask_pct"):
        _snap(bid_pct=50.0, ask_pct=100.0)


def test_snapshot_rejects_crossed_book():
    with pytest.raises(StorageError, match="crossed"):
        _snap(bid_pct=60.0, ask_pct=40.0)


def test_snapshot_rejects_negative_volume():
    with pytest.raises(StorageError, match="volume_usd"):
        _snap(volume_usd=-1.0)


def test_snapshot_bid_ask_volume_default_to_none():
    snap = _snap()
    assert snap.bid_pct is None
    assert snap.ask_pct is None
    assert snap.volume_usd is None


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
        assert loaded.bid_pct is None
        assert loaded.ask_pct is None
        assert loaded.volume_usd is None


def test_snapshot_round_trip_with_book_fields(tmp_path):
    db = tmp_path / "evhedge.db"
    with Storage(db) as store:
        store.record_snapshot(_snap(
            source="book", bid_pct=95.5, ask_pct=97.0, volume_usd=12345.0,
        ))
        (loaded,) = store.snapshots("FIFA World Cup 2026", team="Morocco")
        assert loaded.source == "book"
        assert loaded.bid_pct == pytest.approx(95.5)
        assert loaded.ask_pct == pytest.approx(97.0)
        assert loaded.volume_usd == pytest.approx(12345.0)


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


def test_v1_database_migrates_forward_and_keeps_data(tmp_path):
    """A DB created by the snapshots-only evhedge must open in this one,
    get the new tables, and keep its old rows."""
    import sqlite3

    from evhedge.storage import _MIGRATIONS

    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.executescript(_MIGRATIONS[0])
    conn.execute(
        "INSERT INTO price_snapshots (ts_utc, tournament, team, market, price_pct, source)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (T0.isoformat(), "FIFA World Cup 2026", "Morocco", "winner_no", 97.0, "board"),
    )
    conn.execute("PRAGMA user_version = 1")
    conn.commit()
    conn.close()

    with Storage(db) as store:
        (version,) = store._conn.execute("PRAGMA user_version").fetchone()
        assert version == SCHEMA_VERSION
        assert len(store.snapshots("FIFA World Cup 2026")) == 1
        assert store.runs() == []  # new table exists and is empty


# --- scan passports --------------------------------------------------------------

def _mini_scan_reports():
    """Real CandidateReports from a real scan (storage must fit the actual
    types, not hand-mocked ones)."""
    from evhedge.scanner import ScannerConfig, StageMeta, scan

    config = ScannerConfig(
        tournament="Four Team Cup",
        stages_meta=[StageMeta("playoff", "single_elim", "bo3", True)],
        teams={"TeamA": 40.0, "TeamB": 5.0, "TeamC": 3.0, "TeamD": 45.0},
        bracket=[["TeamA", "TeamB"], ["TeamC", "TeamD"]],
        target_market="winner",
        no_prices={"TeamB": 91.0, "TeamC": 93.0},
    )
    return scan(config)


def test_record_scan_round_trip(tmp_path):
    reports = _mini_scan_reports()
    with Storage(tmp_path / "e.db") as store:
        run_id = store.record_scan(
            "Four Team Cup", "winner", reports,
            config_path="configs/four_team.yaml", ts_utc=T0,
        )

        run = store.latest_run("Four Team Cup")
        assert run.id == run_id
        assert run.target_market == "winner"
        assert run.ts_utc == T0
        assert run.config_path == "configs/four_team.yaml"

        passports = store.passports(run_id)
        assert [p.team for p in passports] == [r.team for r in reports]
        for passport, report in zip(passports, reports):
            assert passport.fuel_verdict == report.fuel_verdict
            assert passport.data_complete == report.data_complete
            assert passport.deadness == pytest.approx(report.deadness)
            assert passport.ev_lockin == pytest.approx(report.ev_lockin)
            # the archive carries the nested parts the scalars don't
            assert passport.report["sensitivity"].keys() == report.sensitivity.keys()
            assert passport.report["sources_breakdown"] == report.sources_breakdown
            assert passport.report["liquidity"]["status"] == report.liquidity.status


def test_record_scan_empty_run_is_still_recorded(tmp_path):
    with Storage(tmp_path / "e.db") as store:
        run_id = store.record_scan("Empty Cup", "winner", [], ts_utc=T0)
        assert store.passports(run_id) == []
        assert store.latest_run("Empty Cup") is not None


def test_runs_newest_first_per_tournament(tmp_path):
    with Storage(tmp_path / "e.db") as store:
        store.record_scan("Cup", "winner", [], ts_utc=T0)
        newer = store.record_scan("Cup", "winner", [], ts_utc=T0 + timedelta(hours=6))
        store.record_scan("Other Cup", "winner", [], ts_utc=T0 + timedelta(hours=9))

        cup_runs = store.runs("Cup")
        assert [r.id for r in cup_runs][0] == newer
        assert len(cup_runs) == 2
        assert len(store.runs()) == 3


def test_record_scan_rejects_naive_timestamp(tmp_path):
    with Storage(tmp_path / "e.db") as store:
        with pytest.raises(StorageError, match="timezone-aware"):
            store.record_scan("Cup", "winner", [], ts_utc=datetime(2026, 7, 6))


# --- resolves ---------------------------------------------------------------------

def test_resolve_round_trip_and_filtering(tmp_path):
    with Storage(tmp_path / "e.db") as store:
        store.record_resolve(Resolve(
            tournament="FIFA World Cup 2026", team="Morocco", market="reach_final_yes",
            outcome="no", ts_utc=T0, note="eliminated in QF",
        ))
        store.record_resolve(Resolve(
            tournament="FIFA World Cup 2026", team="Norway", market="winner_yes",
            outcome="no", ts_utc=T0 + timedelta(days=3),
        ))

        (morocco,) = store.resolves("FIFA World Cup 2026", team="Morocco")
        assert morocco.outcome == "no"
        assert morocco.note == "eliminated in QF"
        assert morocco.ts_utc == T0
        assert len(store.resolves("FIFA World Cup 2026")) == 2
        assert store.resolves("FIFA World Cup 2026", market="winner_yes")[0].team == "Norway"


def test_price_velocity_pp_per_hour(tmp_path):
    with Storage(tmp_path / "e.db") as store:
        store.record_snapshots([
            _snap(ts_utc=T0, price_pct=97.0),
            _snap(ts_utc=T0 + timedelta(hours=2), price_pct=93.6),
        ])
        v = store.price_velocity(
            "FIFA World Cup 2026", "Morocco", "winner_no",
            window=timedelta(hours=24), now=T0 + timedelta(hours=2),
        )
        # NO falls 97.0 -> 93.6 over 2h = -1.7 pp/h (team rising)
        assert v == pytest.approx(-1.7)


def test_price_velocity_honest_none_on_thin_history(tmp_path):
    with Storage(tmp_path / "e.db") as store:
        # one point only -> None
        store.record_snapshot(_snap(ts_utc=T0))
        assert store.price_velocity(
            "FIFA World Cup 2026", "Morocco", "winner_no",
            window=timedelta(hours=24), now=T0,
        ) is None

        # second point exists but OUTSIDE the window -> still None
        store.record_snapshot(_snap(ts_utc=T0 + timedelta(hours=30), price_pct=95.0))
        assert store.price_velocity(
            "FIFA World Cup 2026", "Morocco", "winner_no",
            window=timedelta(hours=24), now=T0 + timedelta(hours=30),
        ) is None

        # two points at the SAME instant (zero span) -> None, not a div/0
        store.record_snapshot(_snap(ts_utc=T0 + timedelta(hours=30), price_pct=94.0))
        assert store.price_velocity(
            "FIFA World Cup 2026", "Morocco", "winner_no",
            window=timedelta(hours=1), now=T0 + timedelta(hours=30),
        ) is None


def test_resolve_rejects_bad_outcome_and_naive_ts():
    with pytest.raises(StorageError, match="outcome"):
        Resolve(tournament="t", team="A", market="winner_yes", outcome="won", ts_utc=T0)
    with pytest.raises(StorageError, match="timezone-aware"):
        Resolve(tournament="t", team="A", market="winner_yes", outcome="yes",
                ts_utc=datetime(2026, 7, 6))


# --- resolve dedup (schema v6) -----------------------------------------------

def test_record_resolve_is_idempotent_on_same_outcome(tmp_path):
    """A poller re-observing an already-closed market on every cycle must
    not pile up a fresh row every time (the ewc_watch.bat 15-min bug:
    2,566 rows for 56 actual outcomes)."""
    with Storage(tmp_path / "e.db") as store:
        first_id = store.record_resolve(Resolve(
            tournament="T", team="Morocco", market="winner_no", outcome="no", ts_utc=T0,
        ))
        second_id = store.record_resolve(Resolve(
            tournament="T", team="Morocco", market="winner_no", outcome="no",
            ts_utc=T0 + timedelta(minutes=15),
        ))

        assert second_id == first_id
        assert len(store.resolves("T", team="Morocco", market="winner_no")) == 1


def test_record_resolve_conflicting_outcome_raises(tmp_path):
    with Storage(tmp_path / "e.db") as store:
        store.record_resolve(Resolve(
            tournament="T", team="Morocco", market="winner_no", outcome="no", ts_utc=T0,
        ))
        with pytest.raises(StorageError, match="conflicting"):
            store.record_resolve(Resolve(
                tournament="T", team="Morocco", market="winner_no", outcome="yes",
                ts_utc=T0 + timedelta(minutes=15),
            ))
        # the original row must survive untouched
        (resolve,) = store.resolves("T", team="Morocco", market="winner_no")
        assert resolve.outcome == "no"


def test_record_resolve_same_team_market_different_tournament_is_not_a_duplicate(tmp_path):
    with Storage(tmp_path / "e.db") as store:
        store.record_resolve(Resolve(
            tournament="T1", team="Falcons", market="winner_no", outcome="no", ts_utc=T0,
        ))
        store.record_resolve(Resolve(
            tournament="T2", team="Falcons", market="winner_no", outcome="yes", ts_utc=T0,
        ))
        assert len(store.resolves("T1", team="Falcons")) == 1
        assert len(store.resolves("T2", team="Falcons")) == 1


def test_v5_to_v6_migration_dedups_existing_resolve_rows(tmp_path):
    """Simulates a v5 database with 3 duplicate resolve rows for the same
    (tournament, team, market) -- exactly the shape the live watcher bug
    produced -- and checks the migration collapses them to one and the
    unique index is actually enforced afterward."""
    import sqlite3

    from evhedge.storage import _MIGRATIONS, SCHEMA_VERSION

    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    for i in range(5):  # v0 -> v5
        step = _MIGRATIONS[i]
        if callable(step):
            step(conn)
        else:
            conn.executescript(step)
    for minute in (0, 15, 30):
        conn.execute(
            "INSERT INTO resolves (ts_utc, tournament, team, market, outcome, note)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            ((T0 + timedelta(minutes=minute)).isoformat(), "EWC", "Falcons", "winner_no", "no", None),
        )
    conn.execute("PRAGMA user_version = 5")
    conn.commit()
    conn.close()

    with Storage(db) as store:
        (version,) = store._conn.execute("PRAGMA user_version").fetchone()
        assert version == SCHEMA_VERSION
        assert len(store.resolves("EWC", team="Falcons", market="winner_no")) == 1

        # the unique index must actually be in place now, not just a one-time cleanup
        with pytest.raises(StorageError, match="conflicting"):
            store.record_resolve(Resolve(
                tournament="EWC", team="Falcons", market="winner_no", outcome="yes", ts_utc=T0,
            ))


def test_v6_to_v7_migration_creates_predictions_table_and_keeps_data(tmp_path):
    """Simulates a v6 database with existing snapshot/resolve data and
    checks the migration adds ``predictions`` without touching anything
    else, and that re-running (no-op, already at SCHEMA_VERSION) is safe."""
    import sqlite3

    from evhedge.storage import _MIGRATIONS, Prediction, SCHEMA_VERSION

    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    for i in range(6):  # v0 -> v6
        step = _MIGRATIONS[i]
        if callable(step):
            step(conn)
        else:
            conn.executescript(step)
    conn.execute(
        "INSERT INTO resolves (ts_utc, tournament, team, market, outcome, note)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (T0.isoformat(), "EWC", "Falcons", "winner_no", "no", None),
    )
    conn.execute("PRAGMA user_version = 6")
    conn.commit()
    conn.close()

    with Storage(db) as store:
        (version,) = store._conn.execute("PRAGMA user_version").fetchone()
        assert version == SCHEMA_VERSION  # whatever the latest schema version now is

        # pre-existing data untouched
        assert len(store.resolves("EWC", team="Falcons", market="winner_no")) == 1

        # predictions table is usable
        store.record_prediction(Prediction(
            tournament="EWC", team="Falcons", market="winner_no",
            ts_utc=T0, p_model=0.1,
        ))
        assert len(store.predictions(tournament="EWC")) == 1

    # idempotent: re-opening an already-migrated database is a no-op
    with Storage(db) as store:
        (version,) = store._conn.execute("PRAGMA user_version").fetchone()
        assert version == SCHEMA_VERSION
        assert len(store.predictions(tournament="EWC")) == 1


def test_v7_to_v8_migration_creates_ps_results_table_and_keeps_data(tmp_path):
    """Simulates a v7 database with existing predictions data and checks
    the migration adds ``ps_results`` without touching anything else,
    plus that record_ps_result's upsert-by-ps_match_id actually works
    (re-recording the same match_id updates in place, no duplicate)."""
    import sqlite3

    from evhedge.storage import PSResult, _MIGRATIONS, SCHEMA_VERSION

    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    for i in range(7):  # v0 -> v7
        step = _MIGRATIONS[i]
        if callable(step):
            step(conn)
        else:
            conn.executescript(step)
    conn.execute(
        "INSERT INTO predictions (ts_utc, tournament, team, market, p_model)"
        " VALUES (?, ?, ?, ?, ?)",
        (T0.isoformat(), "EWC", "Falcons", "winner_no", 0.1),
    )
    conn.execute("PRAGMA user_version = 7")
    conn.commit()
    conn.close()

    with Storage(db) as store:
        (version,) = store._conn.execute("PRAGMA user_version").fetchone()
        assert version == SCHEMA_VERSION == 8

        # pre-existing data untouched
        assert len(store.predictions(tournament="EWC")) == 1

        # ps_results table is usable, and upserts by ps_match_id
        store.record_ps_result(PSResult(
            ps_match_id=42, tournament="EWC", team_a="A", team_b="B",
            stage="Group A", best_of=3, status="not_started", ts_utc=T0,
        ))
        store.record_ps_result(PSResult(
            ps_match_id=42, tournament="EWC", team_a="A", team_b="B",
            stage="Group A", best_of=3, status="finished", winner="A",
            score_a=2, score_b=0, ts_utc=T0,
        ))
        rows = store.ps_results(tournament="EWC")
        assert len(rows) == 1
        assert rows[0].status == "finished"
        assert rows[0].winner == "A"

    # idempotent
    with Storage(db) as store:
        (version,) = store._conn.execute("PRAGMA user_version").fetchone()
        assert version == SCHEMA_VERSION
        assert len(store.ps_results(tournament="EWC")) == 1
