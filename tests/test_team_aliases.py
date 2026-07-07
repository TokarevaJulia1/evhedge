"""Tests for evhedge.team_aliases."""

from datetime import datetime, timezone

import pytest

from evhedge.config_io import ConfigError
from evhedge.storage import PriceSnapshot, Storage
from evhedge.team_aliases import (
    canonical_name,
    load_aliases,
    recanonicalize,
    suggest_aliases,
)

T0 = datetime(2026, 7, 6, 12, 0, tzinfo=timezone.utc)


# --- canonical_name -----------------------------------------------------------

def test_canonical_name_exact_match():
    alias_map = {"1win": "1W", "1W": "1W"}
    assert canonical_name("1win", alias_map) == "1W"


def test_canonical_name_case_insensitive_lookup():
    alias_map = {"1win": "1W", "1W": "1W"}
    assert canonical_name("1WIN", alias_map) == "1W"
    assert canonical_name("1Win", alias_map) == "1W"


def test_canonical_name_whitespace_insensitive_lookup():
    alias_map = {"Aurora": "Aurora Gaming", "Aurora Gaming": "Aurora Gaming"}
    assert canonical_name("  Aurora   ", alias_map) == "Aurora Gaming"


def test_canonical_name_unknown_returns_raw_unchanged():
    alias_map = {"1win": "1W", "1W": "1W"}
    assert canonical_name("Some Random Team", alias_map) == "Some Random Team"


def test_canonical_name_no_alias_map_returns_raw_unchanged():
    assert canonical_name("1win") == "1win"
    assert canonical_name("1win", None) == "1win"
    assert canonical_name("1win", {}) == "1win"


def test_canonical_name_is_idempotent():
    alias_map = {"1win": "1W", "1W": "1W", "Aurora": "Aurora Gaming", "Aurora Gaming": "Aurora Gaming"}
    for raw in ("1win", "1W", "Aurora", "Aurora Gaming", "Unknown Team"):
        once = canonical_name(raw, alias_map)
        twice = canonical_name(once, alias_map)
        assert twice == once


# --- load_aliases ---------------------------------------------------------------

def test_load_aliases_flattens_and_self_maps(tmp_path):
    path = tmp_path / "aliases.yaml"
    path.write_text(
        '"1W":\n  - "1win"\n"Aurora Gaming":\n  - "Aurora"\n', encoding="utf-8"
    )
    alias_map = load_aliases(path)
    assert alias_map["1win"] == "1W"
    assert alias_map["1W"] == "1W"  # canonical self-maps
    assert alias_map["Aurora"] == "Aurora Gaming"
    assert alias_map["Aurora Gaming"] == "Aurora Gaming"


def test_load_aliases_empty_alias_list_is_fine(tmp_path):
    path = tmp_path / "aliases.yaml"
    path.write_text('"Solo Team": []\n', encoding="utf-8")
    alias_map = load_aliases(path)
    assert alias_map == {"Solo Team": "Solo Team"}


def test_load_aliases_conflict_raises_config_error(tmp_path):
    path = tmp_path / "aliases.yaml"
    path.write_text(
        '"Team A":\n  - "Shared Name"\n"Team B":\n  - "Shared Name"\n', encoding="utf-8"
    )
    with pytest.raises(ConfigError, match="Shared Name"):
        load_aliases(path)


def test_load_aliases_conflict_detection_is_case_insensitive(tmp_path):
    path = tmp_path / "aliases.yaml"
    path.write_text(
        '"Team A":\n  - "shared name"\n"Team B":\n  - "Shared Name"\n', encoding="utf-8"
    )
    with pytest.raises(ConfigError):
        load_aliases(path)


# --- end-to-end: the actual join bug this module exists to fix ------------------

def test_end_to_end_snapshot_and_config_join_after_canonicalization(tmp_path):
    """A snapshot recorded as '1win' and a scanner config keyed by '1W'
    must resolve to the SAME canonical name -- the exact join that was
    silently empty before this module existed."""
    alias_map = {"1win": "1W", "1W": "1W"}

    with Storage(tmp_path / "e.db") as store:
        store.record_snapshot(PriceSnapshot(
            tournament="EWC 2026 Dota 2", team=canonical_name("1win", alias_map),
            market="winner_no", price_pct=91.9, source="board", ts_utc=T0,
        ))
        config_team = canonical_name("1W", alias_map)
        (found,) = store.snapshots("EWC 2026 Dota 2", team=config_team)
        assert found.price_pct == pytest.approx(91.9)


# --- suggest_aliases --------------------------------------------------------------

def _snap(tournament, team, ts=T0, counterparty=None):
    return PriceSnapshot(
        tournament=tournament, team=team, market="winner_no" if counterparty is None else "leg",
        price_pct=50.0, source="board", ts_utc=ts, counterparty=counterparty,
    )


def test_suggest_aliases_finds_substring_pair(tmp_path):
    db = tmp_path / "e.db"
    with Storage(db) as store:
        store.record_snapshot(_snap("T", "Foo Gaming"))
        store.record_snapshot(_snap("T", "Foo", counterparty="Bar"))

    candidates = suggest_aliases(db)
    pairs = {(a, b) for a, b, _ in candidates}
    assert ("Foo Gaming", "Foo") in pairs or ("Foo", "Foo Gaming") in pairs


def test_suggest_aliases_silent_on_unrelated_names(tmp_path):
    db = tmp_path / "e.db"
    with Storage(db) as store:
        store.record_snapshot(_snap("T", "Zebra Squad"))
        store.record_snapshot(_snap("T", "Quokka Kings", counterparty="Wombat United"))

    candidates = suggest_aliases(db)
    names_involved = {n for a, b, _ in candidates for n in (a, b)}
    assert "Zebra Squad" not in names_involved
    assert "Quokka Kings" not in names_involved


def test_suggest_aliases_case_only_difference_scores_highest(tmp_path):
    db = tmp_path / "e.db"
    with Storage(db) as store:
        store.record_snapshot(_snap("T", "L1GA TEAM"))
        store.record_snapshot(_snap("T", "L1ga Team", counterparty="Other"))

    candidates = suggest_aliases(db)
    assert candidates[0][2] == pytest.approx(1.0)
    assert {candidates[0][0], candidates[0][1]} == {"L1GA TEAM", "L1ga Team"}


def test_suggest_aliases_scoped_to_tournament(tmp_path):
    db = tmp_path / "e.db"
    with Storage(db) as store:
        store.record_snapshot(_snap("T1", "Foo Gaming"))
        store.record_snapshot(_snap("T2", "Foo", counterparty="Bar"))

    # different tournaments -- names never co-occur, so no candidate within either scope
    assert suggest_aliases(db, tournament="T1") == []
    assert suggest_aliases(db, tournament="T2") == []
    # unscoped sees both
    assert len(suggest_aliases(db)) >= 1


# --- recanonicalize / migration ---------------------------------------------------

def test_recanonicalize_rewrites_team_counterparty_and_resolves(tmp_path):
    from evhedge.storage import Resolve

    alias_map = {"1win": "1W", "1W": "1W"}
    db = tmp_path / "e.db"
    with Storage(db) as store:
        store.record_snapshot(PriceSnapshot(
            tournament="T", team="1win", market="leg", price_pct=40.0,
            source="board", ts_utc=T0, counterparty="Aurora",
        ))
        store.record_resolve(Resolve(
            tournament="T", team="1win", market="winner_no", outcome="no", ts_utc=T0,
        ))

        alias_map_full = {**alias_map, "Aurora": "Aurora Gaming", "Aurora Gaming": "Aurora Gaming"}
        counts = store.recanonicalize_teams(alias_map_full)
        assert counts["price_snapshots.team"] == 1
        assert counts["price_snapshots.counterparty"] == 1
        assert counts["resolves.team"] == 1

        (snap,) = store.snapshots("T", team="1W")
        assert snap.team == "1W"
        assert snap.raw_team == "1win"
        assert snap.counterparty == "Aurora Gaming"

        (resolve,) = store.resolves("T", team="1W")
        assert resolve.team == "1W"


def test_recanonicalize_is_idempotent(tmp_path):
    alias_map = {"1win": "1W", "1W": "1W"}
    db = tmp_path / "e.db"
    with Storage(db) as store:
        store.record_snapshot(PriceSnapshot(
            tournament="T", team="1win", market="winner_no", price_pct=91.9,
            source="board", ts_utc=T0,
        ))
        first = store.recanonicalize_teams(alias_map)
        second = store.recanonicalize_teams(alias_map)

        assert first["price_snapshots.team"] == 1
        assert second["price_snapshots.team"] == 0  # already canonical -- no-op

        (snap,) = store.snapshots("T", team="1W")
        assert snap.team == "1W"
        assert snap.raw_team == "1win"  # preserved, not overwritten with "1W"


def test_v4_to_v5_migration_canonicalizes_preexisting_rows(tmp_path, monkeypatch):
    """Simulates a v4 database (pre-alias) containing exactly the real
    discrepancy this module was built for, then opens it with current
    evhedge and checks the migration canonicalized it automatically."""
    import sqlite3

    from evhedge.storage import _MIGRATIONS, SCHEMA_VERSION

    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    for i in range(4):  # v0 -> v4, schema only (no data migrations before v5)
        conn.executescript(_MIGRATIONS[i])
    conn.execute(
        "INSERT INTO price_snapshots (ts_utc, tournament, team, market, price_pct, source, counterparty)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (T0.isoformat(), "EWC 2026 Dota 2", "1win", "leg", 40.0, "board", "Aurora"),
    )
    conn.execute("PRAGMA user_version = 4")
    conn.commit()
    conn.close()

    # Use a temporary alias file as the "packaged default" for this test,
    # so the assertion doesn't depend on evhedge/data/team_aliases.yaml's
    # real (and potentially changing) contents.
    alias_path = tmp_path / "aliases.yaml"
    alias_path.write_text('"1W":\n  - "1win"\n"Aurora Gaming":\n  - "Aurora"\n', encoding="utf-8")
    monkeypatch.setattr("evhedge.team_aliases.DEFAULT_ALIASES_PATH", alias_path)

    with Storage(db) as store:
        (version,) = store._conn.execute("PRAGMA user_version").fetchone()
        assert version == SCHEMA_VERSION

        (snap,) = store.snapshots("EWC 2026 Dota 2")
        assert snap.team == "1W"
        assert snap.raw_team == "1win"
        assert snap.counterparty == "Aurora Gaming"
