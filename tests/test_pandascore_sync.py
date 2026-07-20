"""Tests for evhedge.pandascore_sync: canonicalization matcher, sync,
reconcile, and deadline flagging. No live network calls --
data_sources.pandascore.fetch_matches is monkeypatched; the match
fixtures below are REAL PandaScore match shapes (captured live
2026-07-20 while building this module, team names/ids/fields as
actually returned -- not invented)."""

from datetime import datetime, timedelta, timezone

import pytest

from evhedge.pandascore_sync import (
    DEFAULT_DEADLINE_HOURS,
    RECONCILE_LAG_HOURS,
    match_to_ps_result,
    reconcile,
    sync_matches,
    upcoming_deadlines,
)
from evhedge.storage import Resolve, Storage
from evhedge.team_aliases import load_default_aliases

T0 = datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)

# Real PandaScore match shape, captured live -- Team Spirit (PandaScore
# calls it "Spirit") vs Imperial, BLAST Open Lisbon Spring 2025.
REAL_MATCH_SPIRIT_IMPERIAL = {
    "id": 1133645,
    "name": "Upper bracket quarterfinal 1: TS vs IMP",
    "status": "canceled",
    "winner_id": None,
    "begin_at": None,
    "scheduled_at": "2025-03-19T11:00:00Z",
    "number_of_games": 3,
    "match_type": "best_of",
    "tournament": {"id": 16106, "name": "Group A", "tier": "a"},
    "results": [{"team_id": 124523, "score": 0}, {"team_id": 126377, "score": 0}],
    "opponents": [
        {"type": "Team", "opponent": {"id": 124523, "name": "Spirit", "acronym": "TS"}},
        {"type": "Team", "opponent": {"id": 126377, "name": "Imperial", "acronym": "IMP"}},
    ],
}

# Real shape, a decided match (TYLOO vs Lynn Vision, BLAST Open playoffs).
REAL_MATCH_DECIDED = {
    "id": 1579768,
    "name": "Grand final: TYLOO vs LV",
    "status": "finished",
    "winner_id": 999,
    "begin_at": "2026-07-12T06:49:51Z",
    "scheduled_at": "2026-07-12T06:50:00Z",
    "number_of_games": 3,
    "match_type": "best_of",
    "tournament": {"id": 1, "name": "Playoffs", "tier": "a"},
    "results": [{"team_id": 888, "score": 1}, {"team_id": 999, "score": 2}],
    "opponents": [
        {"type": "Team", "opponent": {"id": 888, "name": "TYLOO", "acronym": "TYL"}},
        {"type": "Team", "opponent": {"id": 999, "name": "Lynn Vision", "acronym": "LV"}},
    ],
}

MATCH_MISSING_OPPONENT = {
    "id": 42, "opponents": [{"type": "Team", "opponent": {"id": 1, "name": "OnlyOne"}}],
}


# --- match_to_ps_result -------------------------------------------------------------

def test_match_to_ps_result_canonicalizes_real_spirit_fixture():
    """Real fixture: PandaScore says "Spirit", must resolve to evhedge's
    existing canon "Team Spirit" -- same alias map collect.py uses, no
    second matcher."""
    alias_map = load_default_aliases()
    result = match_to_ps_result(REAL_MATCH_SPIRIT_IMPERIAL, "BLAST Open test", alias_map, T0)

    assert result is not None
    assert result.team_a == "Team Spirit"
    assert result.team_b == "Imperial"
    assert result.ps_match_id == 1133645
    assert result.stage == "Group A"
    assert result.best_of == 3
    assert result.status == "canceled"
    assert result.winner is None  # winner_id is None
    assert result.scheduled_at == datetime(2025, 3, 19, 11, 0, tzinfo=timezone.utc)
    assert result.begin_at is None


def test_match_to_ps_result_resolves_winner_by_id():
    alias_map = load_default_aliases()
    result = match_to_ps_result(REAL_MATCH_DECIDED, "BLAST Open test", alias_map, T0)

    assert result.winner == "Lynn Vision"
    assert result.score_a == 1
    assert result.score_b == 2
    assert result.status == "finished"


def test_match_to_ps_result_skips_non_two_team_match():
    alias_map = load_default_aliases()
    assert match_to_ps_result(MATCH_MISSING_OPPONENT, "T", alias_map, T0) is None


# --- sync_matches --------------------------------------------------------------------

def test_sync_matches_writes_and_upserts(tmp_path, monkeypatch):
    calls = []

    def fake_fetch_matches(status, budget, league_id=None, serie_id=None, max_pages=None):
        calls.append(status)
        budget.requests_made += 1
        budget.last_remaining = 900
        if status == "past":
            return [REAL_MATCH_DECIDED]
        return []

    monkeypatch.setattr("evhedge.pandascore_sync.pandascore_ds.fetch_matches", fake_fetch_matches)

    with Storage(tmp_path / "e.db") as store:
        summary = sync_matches(
            store, "BLAST Open test", league_id=5370, statuses=("upcoming", "past"), ts_utc=T0,
        )
        assert summary.matches_seen == 1
        assert summary.matches_written == 1
        assert summary.requests_made == 2
        assert set(calls) == {"upcoming", "past"}

        rows = store.ps_results(tournament="BLAST Open test")
        assert len(rows) == 1
        assert rows[0].winner == "Lynn Vision"

        # re-sync: same match id -> upsert, not a duplicate row
        summary2 = sync_matches(
            store, "BLAST Open test", league_id=5370, statuses=("past",), ts_utc=T0,
        )
        assert summary2.matches_written == 1
        assert len(store.ps_results(tournament="BLAST Open test")) == 1


def test_sync_matches_skips_malformed_and_counts_it(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "evhedge.pandascore_sync.pandascore_ds.fetch_matches",
        lambda status, budget, league_id=None, serie_id=None, max_pages=None: (
            [MATCH_MISSING_OPPONENT] if status == "past" else []
        ),
    )
    with Storage(tmp_path / "e.db") as store:
        summary = sync_matches(store, "T", league_id=1, statuses=("past",), ts_utc=T0)
        assert summary.matches_seen == 1
        assert summary.matches_written == 0
        assert summary.skipped_shape == 1


# --- reconcile ------------------------------------------------------------------------

def test_reconcile_flags_finished_without_gamma_resolve_past_lag(tmp_path):
    with Storage(tmp_path / "e.db") as store:
        alias_map = load_default_aliases()
        result = match_to_ps_result(REAL_MATCH_DECIDED, "T", alias_map, T0)
        store.record_ps_result(result)

        now = T0 + timedelta(hours=RECONCILE_LAG_HOURS + 1)
        report = reconcile(store, "T", now=now)

        assert report.n_warnings == 1
        assert report.n_ok == 0
        assert "no Gamma resolve" in report.rows[0].warning


def test_reconcile_no_warning_within_lag_grace_period(tmp_path):
    with Storage(tmp_path / "e.db") as store:
        alias_map = load_default_aliases()
        result = match_to_ps_result(REAL_MATCH_DECIDED, "T", alias_map, T0)
        result.begin_at = T0  # match just finished at T0, not the fixture's fixed 2026-07-12 date
        store.record_ps_result(result)

        now = T0 + timedelta(minutes=30)  # well under RECONCILE_LAG_HOURS
        report = reconcile(store, "T", now=now)

        assert report.n_warnings == 0
        assert report.n_ok == 1
        assert report.rows[0].warning is None


def test_reconcile_ok_when_gamma_resolve_exists(tmp_path):
    with Storage(tmp_path / "e.db") as store:
        alias_map = load_default_aliases()
        result = match_to_ps_result(REAL_MATCH_DECIDED, "T", alias_map, T0)
        store.record_ps_result(result)
        store.record_resolve(Resolve(
            tournament="T", team="Lynn Vision", market="result:x:Match Winner",
            outcome="yes", ts_utc=T0,
        ))

        now = T0 + timedelta(hours=RECONCILE_LAG_HOURS + 1)
        report = reconcile(store, "T", now=now)

        assert report.n_warnings == 0
        assert report.n_ok == 1
        assert report.rows[0].gamma_resolved is True


def test_reconcile_ignores_unfinished_matches(tmp_path):
    with Storage(tmp_path / "e.db") as store:
        alias_map = load_default_aliases()
        result = match_to_ps_result(REAL_MATCH_SPIRIT_IMPERIAL, "T", alias_map, T0)  # status=canceled, no winner
        store.record_ps_result(result)

        report = reconcile(store, "T", now=T0 + timedelta(hours=100))
        assert report.rows == []
        assert report.n_ok == 0
        assert report.n_warnings == 0


# --- upcoming_deadlines ------------------------------------------------------------

def test_upcoming_deadlines_sorts_and_flags_missing_predictions(tmp_path):
    with Storage(tmp_path / "e.db") as store:
        alias_map = load_default_aliases()

        soon = dict(REAL_MATCH_SPIRIT_IMPERIAL)
        soon["id"] = 1
        soon["status"] = "not_started"
        soon["scheduled_at"] = (T0 + timedelta(hours=1)).isoformat().replace("+00:00", "Z")
        store.record_ps_result(match_to_ps_result(soon, "T", alias_map, T0))

        later = dict(REAL_MATCH_SPIRIT_IMPERIAL)
        later["id"] = 2
        later["status"] = "not_started"
        later["scheduled_at"] = (T0 + timedelta(hours=10)).isoformat().replace("+00:00", "Z")
        store.record_ps_result(match_to_ps_result(later, "T", alias_map, T0))

        rows = upcoming_deadlines(store, "T", hours_threshold=DEFAULT_DEADLINE_HOURS, now=T0)

        assert [r.hours_until for r in rows] == pytest.approx([1.0, 10.0])
        assert rows[0].has_prediction is False  # within threshold, no prediction -> flaggable


def test_upcoming_deadlines_excludes_finished_matches(tmp_path):
    with Storage(tmp_path / "e.db") as store:
        alias_map = load_default_aliases()
        result = match_to_ps_result(REAL_MATCH_DECIDED, "T", alias_map, T0)  # status=finished
        store.record_ps_result(result)

        rows = upcoming_deadlines(store, "T", now=T0)
        assert rows == []
