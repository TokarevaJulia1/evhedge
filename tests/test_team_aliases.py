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


# --- EWC group-stage duplicate identities (2026-07-10) ---------------------------
#
# Two teams lived in the real data/ewc2026.db under two names each:
#   IC x Insanity (winner board only) <-> Inner Circle (leg/resolves only)
#   Poor Rangers (winner board, always) <-> ZEDI Esports (leg/resolves, one
#       group-A slot's early matches only -- a deliberate slot alias, not a
#       claim of shared roster; see evhedge/data/team_aliases.yaml).
#
# Canon direction for "IC x Insanity"/"Inner Circle" follows the file's own
# documented rule ("Canonical form chosen = the winner-board spelling"):
# canonical = "IC x Insanity", NOT "Inner Circle" -- the inverse of a naive
# first guess, deliberately, for consistency with every other entry.

def test_canonical_name_ic_insanity_board_spelling_wins():
    from evhedge.team_aliases import load_default_aliases

    alias_map = load_default_aliases()
    assert canonical_name("Inner Circle", alias_map) == "IC x Insanity"
    assert canonical_name("IC x Insanity", alias_map) == "IC x Insanity"  # self-map


def test_canonical_name_zedi_poor_rangers():
    from evhedge.team_aliases import load_default_aliases

    alias_map = load_default_aliases()
    assert canonical_name("ZEDI Esports", alias_map) == "Poor Rangers"
    assert canonical_name("Poor Rangers", alias_map) == "Poor Rangers"


def test_default_aliases_previous_pairs_still_work():
    """The new entries must not disturb the three already-confirmed
    pairs."""
    from evhedge.team_aliases import load_default_aliases

    alias_map = load_default_aliases()
    assert canonical_name("1win", alias_map) == "1W"
    assert canonical_name("Aurora", alias_map) == "Aurora Gaming"
    assert canonical_name("L1ga Team", alias_map) == "L1GA TEAM"


def test_blast_cs2_short_board_names_canonicalize_to_existing_org(tmp_path):
    """BLAST Bounty 2026 Season 2 (CS2) onboarding: 'Spirit'/'Falcons' are
    the SAME organizations as EWC's 'Team Spirit'/'Team Falcons' -- the
    short CS2-board spelling must resolve to the existing Dota-side
    canon, not mint a second canonical name for the same org."""
    from evhedge.team_aliases import load_default_aliases

    alias_map = load_default_aliases()
    assert canonical_name("Spirit", alias_map) == "Team Spirit"
    assert canonical_name("Team Spirit", alias_map) == "Team Spirit"  # self-map
    assert canonical_name("Falcons", alias_map) == "Team Falcons"
    assert canonical_name("Team Falcons", alias_map) == "Team Falcons"  # self-map

    # every previously-confirmed Dota pair still works, untouched
    assert canonical_name("1win", alias_map) == "1W"
    assert canonical_name("Aurora", alias_map) == "Aurora Gaming"
    assert canonical_name("ZEDI Esports", alias_map) == "Poor Rangers"

    # "Aurora" is shared by both boards (CS2's own team is ALSO called
    # "Aurora" on Polymarket) -- the EXISTING alias already covers it,
    # no duplicate entry needed.
    assert canonical_name("Aurora", alias_map) == "Aurora Gaming"

    # teams that already spell identically on both boards -- no entry,
    # not an oversight.
    assert canonical_name("MOUZ", alias_map) == "MOUZ"
    assert canonical_name("GamerLegion", alias_map) == "GamerLegion"

    # a genuinely new CS2-only organization: canon = board spelling,
    # no alias needed (this is the DEFAULT path, not a special case).
    assert canonical_name("Vitality", alias_map) == "Vitality"
    assert canonical_name("FURIA", alias_map) == "FURIA"


def _resolve_row(tournament, team, market, outcome, ts):
    from evhedge.storage import Resolve
    return Resolve(tournament=tournament, team=team, market=market, outcome=outcome, ts_utc=ts)


def test_ewc_duplicate_identities_integration(tmp_path):
    """Reproduces the live evidence shape: ZEDI Esports' 2 resolved maps
    (0-2) + Poor Rangers' 4 resolved maps (1-3) must merge into one slot's
    6 maps (1-5); a price_snapshots row under the non-canonical spelling
    must get raw_team backfilled; group membership (here: a small
    synthetic "group D") must collapse from 7 raw names to 6 real teams;
    and a second recanonicalize pass must be a total no-op."""
    from evhedge.team_aliases import load_default_aliases

    alias_map = load_default_aliases()
    db = tmp_path / "e.db"
    tournament = "EWC 2026 Dota 2"

    with Storage(db) as store:
        # ZEDI Esports: 2 maps, 0-2 (both losses).
        store.record_resolve(_resolve_row(
            tournament, "ZEDI Esports", "result:dota2-zedies-gl:Game 1 Winner", "no", T0,
        ))
        store.record_resolve(_resolve_row(
            tournament, "ZEDI Esports", "result:dota2-zedies-gl:Game 2 Winner", "no", T0,
        ))
        # Poor Rangers: 4 maps across 2 matches, 1-3.
        store.record_resolve(_resolve_row(
            tournament, "Poor Rangers", "result:dota2-poorra-bb4:Game 1 Winner", "no", T0,
        ))
        store.record_resolve(_resolve_row(
            tournament, "Poor Rangers", "result:dota2-poorra-bb4:Game 2 Winner", "no", T0,
        ))
        store.record_resolve(_resolve_row(
            tournament, "Poor Rangers", "result:dota2-poorra-xtreme:Game 1 Winner", "yes", T0,
        ))
        store.record_resolve(_resolve_row(
            tournament, "Poor Rangers", "result:dota2-poorra-xtreme:Game 2 Winner", "no", T0,
        ))

        # A winner-board snapshot under the non-canonical spelling.
        store.record_snapshot(PriceSnapshot(
            tournament=tournament, team="Inner Circle", market="winner_no",
            price_pct=99.5, source="board", ts_utc=T0,
        ))

        # Synthetic "group D": 7 raw names, only 6 real teams (Inner
        # Circle == IC x Insanity's other match-day spelling) -- 5 other
        # teams + the 2 spellings of the 6th.
        for team, counterparty in [
            ("TeamA", "TeamB"), ("TeamC", "TeamD"),
            ("IC x Insanity", "TeamE"), ("Inner Circle", "TeamA"),
        ]:
            store.record_snapshot(PriceSnapshot(
                tournament=tournament, team=team, market="leg",
                price_pct=50.0, source="board", ts_utc=T0, counterparty=counterparty,
            ))
        group_d_raw = {"TeamA", "TeamB", "TeamC", "TeamD", "TeamE",
                        "IC x Insanity", "Inner Circle"}
        assert group_d_raw & set(store.distinct_team_names(tournament)) == group_d_raw
        assert len(group_d_raw) == 7  # 5 real other teams + 2 spellings of the 6th

        first_counts = store.recanonicalize_teams(alias_map)

        # Resolves merged under the canonical team.
        poor_rangers_resolves = store.resolves(tournament, team="Poor Rangers")
        assert len(poor_rangers_resolves) == 6
        wins = sum(1 for r in poor_rangers_resolves if r.outcome == "yes")
        assert wins == 1  # 1-5 record
        assert store.resolves(tournament, team="ZEDI Esports") == []

        # raw_team backfilled on the renamed snapshot.
        (snap,) = store.snapshots(tournament, team="IC x Insanity", market="winner_no")
        assert snap.team == "IC x Insanity"
        assert snap.raw_team == "Inner Circle"

        # Group D is now 6 real teams, not 7 raw spellings.
        canonical_group_d = {canonical_name(t, alias_map) for t in group_d_raw}
        assert canonical_group_d == {"TeamA", "TeamB", "TeamC", "TeamD", "TeamE", "IC x Insanity"}
        assert len(canonical_group_d) == 6

        # Idempotency: a second pass changes nothing.
        second_counts = store.recanonicalize_teams(alias_map)
        assert all(v == 0 for v in second_counts.values())
        assert first_counts != second_counts  # sanity: the first pass DID do work


def test_recanonicalize_resolves_unique_collision_drops_duplicate_safely(tmp_path, caplog):
    """An artificial UNIQUE(tournament, team, market) collision in
    resolves must not crash recanonicalize -- the pre-existing canonical
    row is kept, the row being renamed is dropped as a duplicate, other
    rows are untouched, and it's logged."""
    import logging

    alias_map = {"1win": "1W", "1W": "1W"}
    db = tmp_path / "e.db"

    with Storage(db) as store:
        # Already-canonical row for this exact (tournament, team, market).
        canonical_id = store.record_resolve(
            _resolve_row("T", "1W", "winner_no", "no", T0)
        )
        # A row under an unrelated market, must survive untouched.
        unrelated_id = store.record_resolve(
            _resolve_row("T", "1win", "reach_final_no", "yes", T0)
        )
        # Manually insert a colliding duplicate under the raw spelling for
        # the SAME (tournament, market) as the canonical row above --
        # bypasses record_resolve's own dedup (idx_resolves_unique blocks
        # a literal duplicate insert, so this simulates data written
        # before this alias existed, under the pre-alias key
        # (tournament, "1win", market), which was distinct at the time).
        store._conn.execute(
            "INSERT INTO resolves (ts_utc, tournament, team, market, outcome, note)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (T0.isoformat(), "T", "1win", "winner_no", "no", None),
        )
        store._conn.commit()
        dup_id = store._conn.execute(
            "SELECT id FROM resolves WHERE team = '1win' AND market = 'winner_no'"
        ).fetchone()[0]

        with caplog.at_level(logging.WARNING, logger="evhedge.team_aliases"):
            counts = store.recanonicalize_teams(alias_map)

        assert counts["resolves.duplicates_dropped"] == 1
        assert any("dropped duplicate" in r.message for r in caplog.records)

        remaining = store.resolves("T")
        assert {r.id for r in remaining} == {canonical_id, unrelated_id}
        assert dup_id not in {r.id for r in remaining}
        # the surviving canonical row is exactly the pre-existing one, untouched
        (kept,) = [r for r in remaining if r.market == "winner_no"]
        assert kept.id == canonical_id
        assert kept.team == "1W"
