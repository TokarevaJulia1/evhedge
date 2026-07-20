"""Tests for evhedge.scenarios: the stage-2 fixed-bracket tree,
per-team outlooks, conflict map, and config emitter. No live network --
winner-book mids come from a real Storage populated with real BLAST
Bounty S2 board numbers captured live 2026-07-20."""

from datetime import datetime, timezone

import pytest

from evhedge.config_io import load_full_config
from evhedge.scenarios import (
    ScenarioError,
    detect_qf_pairs,
    emit_bracket_config,
    enumerate_stage2_scenarios,
    latest_book_no_ask,
    make_win_prob_fn,
    stage_conflict,
    team_outlooks,
)
from evhedge.storage import PriceSnapshot, PSResult, Storage

T0 = datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)

PAIRS = [("A", "B"), ("C", "D"), ("E", "F"), ("G", "H")]


# --- enumerate_stage2_scenarios -----------------------------------------------------

def test_enumerate_rejects_wrong_pair_count():
    with pytest.raises(ScenarioError, match="4 пары"):
        enumerate_stage2_scenarios([("A", "B")], lambda a, b, s: 0.5)


def test_enumerate_rejects_non_distinct_teams():
    bad_pairs = [("A", "B"), ("A", "D"), ("E", "F"), ("G", "H")]  # A repeats
    with pytest.raises(ScenarioError, match="8 различных"):
        enumerate_stage2_scenarios(bad_pairs, lambda a, b, s: 0.5)


def test_enumerate_128_paths_sum_to_one_and_uniform_at_half():
    """Sanity from the prompt: all pairwise probabilities = 0.5 -> each
    of the 8 teams has exactly 12.5% title probability."""
    paths = enumerate_stage2_scenarios(PAIRS, lambda a, b, s: 0.5)
    assert len(paths) == 128
    assert sum(p.probability for p in paths) == pytest.approx(1.0, abs=1e-9)

    all_teams = [t for pair in PAIRS for t in pair]
    outlooks = team_outlooks(paths, all_teams)
    for team in all_teams:
        assert outlooks[team].p_title == pytest.approx(0.125, abs=1e-9)
        assert outlooks[team].stage_win_prob == {"QF": 0.5, "SF": 0.5, "GF": 0.5}


def test_team_outlooks_hand_computed_asymmetric():
    """A has QF win_prob 0.8, everything else 0.5 -- hand-computed
    expectation: A reaches SF at 0.8, wins SF at 0.5 (still symmetric
    there), reaches GF at 0.8*0.5=0.4, wins GF at 0.5 -> p_title(A) =
    0.8*0.5*0.5 = 0.2."""
    def win_prob(a, b, stage):
        if stage == "QF" and {a, b} == {"A", "B"}:
            return 0.8 if a == "A" else 0.2
        return 0.5

    paths = enumerate_stage2_scenarios(PAIRS, win_prob)
    all_teams = [t for pair in PAIRS for t in pair]
    outlooks = team_outlooks(paths, all_teams)

    assert outlooks["A"].p_title == pytest.approx(0.8 * 0.5 * 0.5, abs=1e-9)
    assert outlooks["A"].stage_win_prob["QF"] == pytest.approx(0.8)
    assert outlooks["A"].stage_win_prob["SF"] == pytest.approx(0.5, abs=1e-9)
    assert outlooks["B"].p_title == pytest.approx(0.2 * 0.5 * 0.5, abs=1e-9)

    # every team's title probability must still sum to 1 across all 8
    assert sum(o.p_title for o in outlooks.values()) == pytest.approx(1.0, abs=1e-9)


# --- stage_conflict ------------------------------------------------------------------

def test_stage_conflict_qf_sf_gf_and_self():
    assert stage_conflict(PAIRS, "A", "B") == "QF"      # same QF pair
    assert stage_conflict(PAIRS, "A", "C") == "SF"      # same half, different QF
    assert stage_conflict(PAIRS, "A", "E") == "GF"       # different halves
    assert stage_conflict(PAIRS, "A", "A") is None       # self
    assert stage_conflict(PAIRS, "A", "Ghost") is None   # not in the bracket at all


# --- make_win_prob_fn / latest_book_no_ask (real Storage, real BLAST numbers) --------

def _book_yes(team, bid_pct, ask_pct, tournament="BLAST"):
    return PriceSnapshot(
        tournament=tournament, team=team, market="winner_yes", price_pct=ask_pct,
        bid_pct=bid_pct, ask_pct=ask_pct, source="book", ts_utc=T0,
    )


def _book_no(team, bid_pct, ask_pct, tournament="BLAST"):
    return PriceSnapshot(
        tournament=tournament, team=team, market="winner_no", price_pct=ask_pct,
        bid_pct=bid_pct, ask_pct=ask_pct, source="book", ts_utc=T0,
    )


def test_make_win_prob_fn_uses_real_book_mids(tmp_path):
    """Real winner-board numbers captured live 2026-07-20: Vitality
    28/29c, FURIA 8/9c."""
    with Storage(tmp_path / "e.db") as store:
        store.record_snapshot(_book_yes("Vitality", 28.0, 29.0))
        store.record_snapshot(_book_yes("FURIA", 8.0, 9.0))

        win_prob_fn = make_win_prob_fn(store, "BLAST", {"QF": 3, "SF": 2, "GF": 1})
        p = win_prob_fn("Vitality", "FURIA", "QF")
        assert 0.6 < p < 0.75  # Vitality clearly favored, model-plausible band


def test_make_win_prob_fn_raises_on_missing_mid(tmp_path):
    with Storage(tmp_path / "e.db") as store:
        store.record_snapshot(_book_yes("Vitality", 28.0, 29.0))
        win_prob_fn = make_win_prob_fn(store, "BLAST", {"QF": 3})
        with pytest.raises(ScenarioError, match="Ghost Team"):
            win_prob_fn("Vitality", "Ghost Team", "QF")


def test_make_win_prob_fn_raises_on_unknown_stage(tmp_path):
    with Storage(tmp_path / "e.db") as store:
        store.record_snapshot(_book_yes("A", 20.0, 21.0))
        store.record_snapshot(_book_yes("B", 20.0, 21.0))
        win_prob_fn = make_win_prob_fn(store, "BLAST", {"QF": 3})
        with pytest.raises(ScenarioError, match="SF"):
            win_prob_fn("A", "B", "SF")


def test_latest_book_no_ask_real_and_missing(tmp_path):
    with Storage(tmp_path / "e.db") as store:
        store.record_snapshot(_book_no("Vitality", 71.0, 72.0))
        assert latest_book_no_ask(store, "BLAST", "Vitality") == pytest.approx(0.72)
        assert latest_book_no_ask(store, "BLAST", "Nobody") is None


# --- detect_qf_pairs -----------------------------------------------------------------

def _ps(team_a, team_b, stage, ps_match_id):
    return PSResult(
        ps_match_id=ps_match_id, tournament="BLAST", team_a=team_a, team_b=team_b,
        stage=stage, best_of=3, status="not_started", ts_utc=T0,
    )


def test_detect_qf_pairs_finds_four_clean_pairs(tmp_path):
    with Storage(tmp_path / "e.db") as store:
        store.record_ps_result(_ps("A", "B", "Quarterfinal 1", 1))
        store.record_ps_result(_ps("C", "D", "Quarterfinal 2", 2))
        store.record_ps_result(_ps("E", "F", "QF 3", 3))
        store.record_ps_result(_ps("G", "H", "QF 4", 4))
        pairs = detect_qf_pairs(store, "BLAST")
        assert set(t for p in pairs for t in p) == {"A", "B", "C", "D", "E", "F", "G", "H"}


def test_detect_qf_pairs_wrong_count_raises(tmp_path):
    with Storage(tmp_path / "e.db") as store:
        store.record_ps_result(_ps("A", "B", "Quarterfinal 1", 1))
        with pytest.raises(ScenarioError, match="найдено 1"):
            detect_qf_pairs(store, "BLAST")


def test_detect_qf_pairs_duplicate_team_raises(tmp_path):
    with Storage(tmp_path / "e.db") as store:
        store.record_ps_result(_ps("A", "B", "Quarterfinal 1", 1))
        store.record_ps_result(_ps("A", "D", "Quarterfinal 2", 2))  # A repeats
        store.record_ps_result(_ps("E", "F", "Quarterfinal 3", 3))
        store.record_ps_result(_ps("G", "H", "Quarterfinal 4", 4))
        with pytest.raises(ScenarioError, match="8 различных"):
            detect_qf_pairs(store, "BLAST")


# --- emit_bracket_config ---------------------------------------------------------------

def test_emit_bracket_config_loads_via_load_full_config(tmp_path):
    paths = enumerate_stage2_scenarios(PAIRS, lambda a, b, s: 0.6)
    all_teams = [t for pair in PAIRS for t in pair]
    outlooks = team_outlooks(paths, all_teams)

    out_path = emit_bracket_config(
        "A", outlooks["A"], "BLAST Bounty 2026 Season 2", "esports", 0.75, tmp_path,
    )
    assert out_path.name == "a_stage2_scenario.yaml"
    assert "MODEL-ESTIMATE" in out_path.read_text(encoding="utf-8")

    bracket, market, strategy = load_full_config(out_path)
    assert bracket.team == "A"
    assert len(bracket.stages) == 3
    assert market.no_price == pytest.approx(0.75)
    assert strategy.no_stake_usd == 1000
