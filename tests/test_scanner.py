"""Tests for evhedge.scanner."""

import pytest

from evhedge.data_sources.polymarket import BookLevel, OrderBook, PolymarketAPIError
from evhedge.power_model import pair_prob, strength
from evhedge.scanner import (
    ScannerConfig,
    ScannerError,
    StageMeta,
    TournamentModel,
    bracket_teams,
    candidate_pool,
    check_liquidity,
    compute_economics,
    deadness,
    fuel_check,
    hype_flag,
    leg_profile_flag,
    load_scanner_config,
    rounds_to_boss,
    rounds_to_title,
    scan,
)

SINGLE_ELIM_STAGE = [StageMeta("playoff", "single_elim", "bo3", True)]


# --- StageMeta / ScannerConfig validation -----------------------------------

def test_stage_meta_rejects_unknown_type():
    with pytest.raises(ScannerError, match="type"):
        StageMeta("playoff", "swiss", "bo3", True)


def test_scanner_config_rejects_bad_target_market():
    with pytest.raises(ScannerError, match="target_market"):
        ScannerConfig(
            tournament="t", stages_meta=SINGLE_ELIM_STAGE, teams={"A": 10.0, "B": 10.0},
            bracket=["A", "B"], target_market="semis",
        )


def test_scanner_config_rejects_unknown_bracket_team():
    with pytest.raises(ScannerError, match="not present"):
        ScannerConfig(
            tournament="t", stages_meta=SINGLE_ELIM_STAGE, teams={"A": 10.0},
            bracket=["A", "Ghost"], target_market="winner",
        )


def test_scanner_config_rejects_bad_node_arity():
    with pytest.raises(ScannerError, match="exactly 2 children"):
        ScannerConfig(
            tournament="t", stages_meta=SINGLE_ELIM_STAGE, teams={"A": 10.0, "B": 10.0, "C": 10.0},
            bracket=["A", "B", "C"], target_market="winner",
        )


def test_power_model_enabled_false_with_round_robin_stage():
    stages = [StageMeta("group", "round_robin", "bo2", False), StageMeta("playoff", "single_elim", "bo3", True)]
    config = ScannerConfig(
        tournament="t", stages_meta=stages, teams={"A": 10.0, "B": 10.0},
        bracket=["A", "B"], target_market="winner",
    )
    assert config.power_model_enabled is False
    assert "group" in config.excluded_stages


def test_power_model_enabled_true_for_single_elim_and_gauntlet_only():
    stages = [StageMeta("survival", "gauntlet", "bo3", True), StageMeta("playoff", "single_elim", "bo3", True)]
    config = ScannerConfig(
        tournament="t", stages_meta=stages, teams={"A": 10.0, "B": 10.0},
        bracket=["A", "B"], target_market="winner",
    )
    assert config.power_model_enabled is True
    assert config.excluded_stages == []


# --- bracket tree helpers ----------------------------------------------------

FOUR_TEAM_BRACKET = [["TeamA", "TeamB"], ["TeamC", "TeamD"]]
FOUR_TEAM_TEAMS = {"TeamA": 40.0, "TeamB": 5.0, "TeamC": 3.0, "TeamD": 45.0}


def _four_team_config(**overrides):
    kwargs = dict(
        tournament="Four Team Cup", stages_meta=SINGLE_ELIM_STAGE, teams=FOUR_TEAM_TEAMS,
        bracket=FOUR_TEAM_BRACKET, target_market="winner",
        no_prices={"TeamB": 91.0, "TeamC": 93.0},
    )
    kwargs.update(overrides)
    return ScannerConfig(**kwargs)


def test_bracket_teams_flattens_all_leaves():
    assert bracket_teams(FOUR_TEAM_BRACKET) == {"TeamA", "TeamB", "TeamC", "TeamD"}


def test_rounds_to_title_four_team_bracket():
    assert rounds_to_title(FOUR_TEAM_BRACKET, "TeamB") == 2
    assert rounds_to_title(FOUR_TEAM_BRACKET, "TeamC") == 2


def test_rounds_to_title_missing_team_raises():
    with pytest.raises(ScannerError, match="not found"):
        rounds_to_title(FOUR_TEAM_BRACKET, "Ghost")


def test_candidate_pool_round1_is_direct_opponent():
    assert candidate_pool(FOUR_TEAM_BRACKET, "TeamB", 1) == {"TeamA"}


def test_candidate_pool_round2_is_other_half():
    assert candidate_pool(FOUR_TEAM_BRACKET, "TeamB", 2) == {"TeamC", "TeamD"}


def test_candidate_pool_out_of_range_raises():
    with pytest.raises(ScannerError, match="out of range"):
        candidate_pool(FOUR_TEAM_BRACKET, "TeamB", 3)


# --- TournamentModel: pairwise probability sourcing -------------------------

def test_pair_prob_sourced_uses_market_leg_price_when_present():
    config = _four_team_config(leg_prices={("TeamA", "TeamB"): 85.0})
    model = TournamentModel(config)
    p, src = model.pair_prob_sourced("TeamA", "TeamB")
    assert src == "market"
    assert p == pytest.approx(0.85)
    # reverse lookup must be consistent (1 - p)
    p_rev, src_rev = model.pair_prob_sourced("TeamB", "TeamA")
    assert src_rev == "market"
    assert p_rev == pytest.approx(0.15)


def test_pair_prob_sourced_falls_back_to_model():
    config = _four_team_config()
    model = TournamentModel(config)
    p, src = model.pair_prob_sourced("TeamA", "TeamB")
    assert src == "model"
    expected = pair_prob(strength(40.0, 2), strength(5.0, 2))
    assert p == pytest.approx(expected)


def test_pair_prob_sourced_no_data_when_model_disabled_and_no_leg_price():
    stages = [StageMeta("group", "round_robin", "bo2", False), StageMeta("playoff", "single_elim", "bo3", True)]
    config = _four_team_config(stages_meta=stages)
    model = TournamentModel(config)
    p, src = model.pair_prob_sourced("TeamA", "TeamB")
    assert src == "no_data"
    assert p == pytest.approx(0.5)


def test_winner_distribution_sums_to_one():
    config = _four_team_config()
    model = TournamentModel(config)
    dist = model.winner_distribution(FOUR_TEAM_BRACKET)
    assert sum(dist.values()) == pytest.approx(1.0)
    assert set(dist) == {"TeamA", "TeamB", "TeamC", "TeamD"}


def test_round_opponent_distribution_round2_sums_to_one():
    config = _four_team_config()
    model = TournamentModel(config)
    dist = model.round_opponent_distribution("TeamB", 2)
    assert set(dist) == {"TeamC", "TeamD"}
    assert sum(dist.values()) == pytest.approx(1.0)


# --- diagnostics on the four-team bracket -----------------------------------

def test_deadness_is_nonnegative():
    config = _four_team_config()
    model = TournamentModel(config)
    assert deadness(model, "TeamB", depth=2) >= 0.0


def test_p_stays_dead_ignores_deterministic_single_candidate_rounds():
    """Round 1's pool is always a single, already-fixed opponent (not a
    draw) -- it must not force p_stays_dead to 0 just because that lone
    candidate is trivially both the only option and the "weakest"."""
    from evhedge.scanner import p_stays_dead

    config = _four_team_config()
    model = TournamentModel(config)
    assert p_stays_dead(model, "TeamB", depth=2) > 0.0


def test_rounds_to_boss_finds_first_round_with_strong_opponent():
    config = _four_team_config()
    model = TournamentModel(config)
    # TeamB's round-1 opponent is TeamA (40% outright) -- already a "boss" (>10%).
    assert rounds_to_boss(model, "TeamB", depth=2, threshold_pct=10.0) == 1


def test_rounds_to_boss_none_when_no_boss_in_range():
    config = _four_team_config()
    model = TournamentModel(config)
    # TeamC's round-1 opponent is TeamD (45%) -- also a boss immediately.
    assert rounds_to_boss(model, "TeamC", depth=2, threshold_pct=50.0) is None


# --- FUEL CHECK: hand-verified 2-team fixture -------------------------------

def test_fuel_check_hand_verified_two_team_case():
    config = ScannerConfig(
        tournament="Two Team Cup", stages_meta=SINGLE_ELIM_STAGE,
        teams={"TeamX": 8.0, "TeamY": 40.0}, bracket=["TeamX", "TeamY"],
        target_market="winner", no_prices={"TeamX": 93.0},
    )
    model = TournamentModel(config)
    fuel = fuel_check(model, "TeamX", depth=1, no_price_pct=93.0)

    assert fuel.premium_pct == pytest.approx(7.0)
    assert fuel.required_multiplier == pytest.approx(13.2857, abs=1e-3)
    assert fuel.available_multiplier == pytest.approx(48.591, abs=1e-2)
    assert fuel.verdict == "SOLID"


def test_fuel_check_verdict_thresholds_direct():
    # A synthetic model isn't needed for the verdict boundary itself --
    # exercise it through fuel_check with leg_prices tuned to hit each band.
    def make(no_price_pct, leg_ask_pct):
        config = ScannerConfig(
            tournament="t", stages_meta=SINGLE_ELIM_STAGE, teams={"A": 5.0, "B": 5.0},
            bracket=["A", "B"], target_market="winner", no_prices={"A": no_price_pct},
            leg_prices={("A", "B"): leg_ask_pct},
        )
        model = TournamentModel(config)
        return fuel_check(model, "A", depth=1, no_price_pct=no_price_pct)

    # required_multiplier for no_price=90 -> premium=10 -> required=9.0
    solid = make(90.0, 5.0)   # available = 1/0.05 = 20 -> ratio 2.22 -> SOLID
    assert solid.verdict == "SOLID"

    thin = make(90.0, 10.5)   # available = 1/0.105 = 9.52 -> ratio 1.06 -> THIN
    assert thin.verdict == "THIN"

    fails = make(90.0, 20.0)  # available = 1/0.20 = 5.0 -> ratio 0.56 -> FAILS
    assert fails.verdict == "FAILS"


# --- LEG PROFILE / HYPE flags ------------------------------------------------

def test_leg_profile_flag_none_without_known_leg_prices():
    config = _four_team_config()
    model = TournamentModel(config)
    assert leg_profile_flag(model, "TeamB", depth=2) is None


def test_leg_profile_flag_favorite_pattern_when_median_high():
    config = _four_team_config(leg_prices={("TeamA", "TeamB"): 55.0})
    model = TournamentModel(config)
    # only one known leg price (55.0, TeamA's ask) -> TeamB's own leg price = 100-55=45 > 40
    assert leg_profile_flag(model, "TeamB", depth=2) == "FAVORITE_PATTERN"


def test_hype_flag_present_only_for_recent_upset_teams():
    config = _four_team_config(recent_upset={"TeamB"})
    assert hype_flag(config, "TeamB") is not None
    assert hype_flag(config, "TeamC") is None


# --- LIQUIDITY --------------------------------------------------------------

def test_check_liquidity_unknown_without_token_id():
    info = check_liquidity(None, volume_usd=1234.0, worst_price=0.9)
    assert info.status == "unknown"
    assert info.volume_usd == 1234.0
    assert info.executable_usd is None


def test_check_liquidity_unknown_on_api_error(monkeypatch):
    def fake_fetch_order_book(token_id):
        raise PolymarketAPIError("boom")

    monkeypatch.setattr("evhedge.scanner.polymarket_ds.fetch_order_book", fake_fetch_order_book)
    info = check_liquidity("token123", volume_usd=500.0, worst_price=0.9)
    assert info.status == "unknown"


def test_check_liquidity_checked_when_book_available(monkeypatch):
    book = OrderBook(token_id="t", asks=[BookLevel(0.90, 100.0)])
    monkeypatch.setattr("evhedge.scanner.polymarket_ds.fetch_order_book", lambda token_id: book)
    info = check_liquidity("token123", volume_usd=500.0, worst_price=0.90)
    assert info.status == "checked"
    assert info.executable_usd == pytest.approx(90.0)
    assert info.executable_avg_price == pytest.approx(0.90)


# --- ECONOMICS ---------------------------------------------------------------

def test_compute_economics_returns_all_fields_and_sensitivity_scenarios():
    config = _four_team_config(leg_prices={("TeamA", "TeamB"): 85.0})
    model = TournamentModel(config)
    econ = compute_economics(model, "TeamB", depth=2, no_price_pct=91.0)

    assert isinstance(econ.ev_lockin, float)
    assert isinstance(econ.ev_hold, float)
    assert econ.exit_now == 0.0
    assert set(econ.sensitivity) == {"current", "legs_cheaper", "legs_pricier"}
    assert econ.sensitivity["current"] == pytest.approx(econ.ev_lockin)


# --- scan() end to end --------------------------------------------------------

def test_scan_only_includes_longshots_with_no_price():
    config = _four_team_config()
    reports = scan(config)
    teams_reported = {r.team for r in reports}
    # TeamA (40%) and TeamD (45%) are above the default 10% threshold -> excluded.
    # TeamB and TeamC are below threshold AND have no_prices entries.
    assert teams_reported == {"TeamB", "TeamC"}


def test_scan_skips_longshot_without_no_price():
    config = _four_team_config(no_prices={"TeamB": 91.0})  # TeamC has no NO price
    reports = scan(config)
    assert {r.team for r in reports} == {"TeamB"}


def test_scan_report_fields_are_populated():
    config = _four_team_config()
    reports = scan(config)
    report = next(r for r in reports if r.team == "TeamB")

    assert report.fuel_verdict in ("SOLID", "THIN", "FAILS")
    assert report.sources_breakdown["market"] + report.sources_breakdown["model"] + report.sources_breakdown["no_data"] > 0
    assert report.liquidity.status == "unknown"  # no token_ids passed
    assert report.min_opp_strength is not None
    # Round 1's candidate pool is a single team (TeamA) -- there's no "2nd
    # strongest" for a pool of size 1, so bench_depth only has an entry for
    # round 2 (candidate pool {TeamC, TeamD}).
    assert set(report.bench_depth) == {2}


# --- YAML loading --------------------------------------------------------------

def test_load_scanner_config_round_trip(tmp_path):
    yaml_text = """
tournament: "Test Cup"
stages_meta:
  - {name: playoff, type: single_elim, match_format: bo3, hedge_suitable: true}
teams: {TeamA: 40.0, TeamB: 5.0, TeamC: 3.0, TeamD: 45.0}
target_market: winner
no_prices: {TeamB: 91.0, TeamC: 93.0}
bracket:
  - [TeamA, TeamB]
  - [TeamC, TeamD]
leg_prices:
  - {teams: [TeamA, TeamB], ask_pct: 85.0}
recent_upset: [TeamB]
outright_threshold_pct: 10.0
"""
    path = tmp_path / "scanner_config.yaml"
    path.write_text(yaml_text, encoding="utf-8")

    config = load_scanner_config(path)
    assert config.tournament == "Test Cup"
    assert config.teams["TeamA"] == pytest.approx(40.0)
    assert config.leg_prices[("TeamA", "TeamB")] == pytest.approx(85.0)
    assert config.recent_upset == {"TeamB"}
    assert bracket_teams(config.bracket) == {"TeamA", "TeamB", "TeamC", "TeamD"}


def test_load_scanner_config_missing_required_field_raises(tmp_path):
    path = tmp_path / "bad.yaml"
    path.write_text("tournament: 'Test Cup'\nstages_meta: []\n", encoding="utf-8")
    from evhedge.config_io import ConfigError
    with pytest.raises(ConfigError, match="teams"):
        load_scanner_config(path)
