"""Tests for evhedge.strategies.compute_hedge_plan across all hedge modes."""

import pytest

from evhedge.engine import compute_ev
from evhedge.models import Bracket, MarketPrices, Stage, StrategyConfig
from evhedge.strategies import compute_hedge_plan


@pytest.fixture
def stages():
    return [
        Stage("R1", 0.6, hedge_decimal_odds=2.0),
        Stage("R2", 0.5, hedge_decimal_odds=1.5),
        Stage("R3", 0.4, hedge_decimal_odds=None),  # no hedge market available
    ]


@pytest.fixture
def market():
    return MarketPrices(no_price=0.9, yes_price=0.1)


def test_none_mode_never_hedges(stages, market):
    strategy = StrategyConfig("none", no_stake_usd=100.0, hedge_mode="none")
    assert compute_hedge_plan(stages, strategy, market) == [0.0, 0.0, 0.0]


def test_fixed_mode(stages, market):
    strategy = StrategyConfig("fixed", no_stake_usd=100.0, hedge_mode="fixed", hedge_base_stake=10.0)
    assert compute_hedge_plan(stages, strategy, market) == [10.0, 10.0, 0.0]


def test_proportional_mode(stages, market):
    strategy = StrategyConfig(
        "prop", no_stake_usd=100.0, hedge_mode="proportional", hedge_base_stake=0.1
    )
    assert compute_hedge_plan(stages, strategy, market) == [10.0, 10.0, 0.0]


def test_reinvest_mode(stages, market):
    """h1 = base (no prior profit).
    h2 = base + (h1 * (d1 - 1)) * kelly_fraction = 5 + (5 * 1.0) * 1.0 = 10
    h3 = 0 (no hedge odds on R3), regardless of accumulated profit.

    kelly_fraction=1.0 passed explicitly here: this test is about the
    reinvest arithmetic (verifying the deterministic cum_hedge_profit
    propagation), not about the half-Kelly default, so it pins the
    "reinvest everything" case rather than depending on the default.
    """
    strategy = StrategyConfig(
        "reinvest", no_stake_usd=100.0, hedge_mode="reinvest", hedge_base_stake=5.0, kelly_fraction=1.0
    )
    plan = compute_hedge_plan(stages, strategy, market)
    assert plan[0] == pytest.approx(5.0)
    assert plan[1] == pytest.approx(10.0)
    assert plan[2] == pytest.approx(0.0)


def test_kelly_mode_uses_bankroll(stages, market):
    """f* = (p*d - 1) / (d - 1); stage R1: p=0.6, d=2.0 -> f* = (1.2-1)/1 = 0.2
    h1 = bankroll * f* * kelly_fraction = 100 * 0.2 * 1.0 = 20

    kelly_fraction=1.0 passed explicitly here: we're checking full-Kelly
    arithmetic against the formula, not the half-Kelly default.
    """
    strategy = StrategyConfig("kelly", no_stake_usd=100.0, hedge_mode="kelly", kelly_fraction=1.0)
    plan = compute_hedge_plan(stages, strategy, market)
    assert plan[0] == pytest.approx(20.0)
    assert plan[2] == pytest.approx(0.0)


def test_kelly_mode_default_is_half_kelly(stages, market):
    """Same setup as test_kelly_mode_uses_bankroll but relying on the
    StrategyConfig default (kelly_fraction=0.5) instead of passing it
    explicitly.

    f* = (0.6*2.0 - 1) / (2.0 - 1) = 0.2
    h1 = bankroll * f* * kelly_fraction = 100 * 0.2 * 0.5 = 10  (half of the
    full-Kelly h1=20 from test_kelly_mode_uses_bankroll)
    """
    strategy = StrategyConfig("kelly_default", no_stake_usd=100.0, hedge_mode="kelly")
    assert strategy.kelly_fraction == pytest.approx(0.5)
    plan = compute_hedge_plan(stages, strategy, market)
    assert plan[0] == pytest.approx(10.0)
    assert plan[2] == pytest.approx(0.0)


def test_max_hedge_stake_caps_all_modes(stages, market):
    strategy = StrategyConfig(
        "fixed_capped",
        no_stake_usd=100.0,
        hedge_mode="fixed",
        hedge_base_stake=10.0,
        max_hedge_stake=7.0,
    )
    plan = compute_hedge_plan(stages, strategy, market)
    assert plan == [7.0, 7.0, 0.0]


def test_negative_kelly_edge_clips_to_zero(market):
    """When the hedge odds imply negative edge, Kelly sizing must not go
    negative — true regardless of kelly_fraction sign/magnitude since the
    default (0.5) only scales the (already negative) f*, it doesn't flip
    its sign."""
    stages = [Stage("R1", 0.3, hedge_decimal_odds=1.5)]  # f* = (0.45-1)/0.5 < 0
    strategy = StrategyConfig("kelly", no_stake_usd=100.0, hedge_mode="kelly")
    plan = compute_hedge_plan(stages, strategy, market)
    assert plan == [0.0]


def test_bankroll_defaults_to_no_stake_usd():
    strategy = StrategyConfig("kelly", no_stake_usd=250.0, hedge_mode="kelly")
    assert strategy.bankroll == pytest.approx(250.0)


# --- lock_in mode -----------------------------------------------------


def _lock_in_bracket_and_market():
    bracket = Bracket(
        team="TeamA",
        tournament="Test Cup",
        sport="football",
        stages=[
            Stage("R1", 0.6, hedge_decimal_odds=2.0),
            Stage("R2", 0.5, hedge_decimal_odds=1.5),
            Stage("R3", 0.55, hedge_decimal_odds=2.4),
            Stage("R4", 0.45, hedge_decimal_odds=1.8),
        ],
    )
    market = MarketPrices(no_price=0.92, yes_price=0.08)
    return bracket, market


def test_lock_in_zero_floor_algebraic():
    """At kelly_fraction=1.0, eliminating at ANY stage k must pay exactly
    $0 -- the whole guaranteed reserve was staked back into the hedge every
    round, so what we lose on the NO position is exactly recovered by
    hedge winnings. Checked across every stage, not just one example."""
    bracket, market = _lock_in_bracket_and_market()
    strategy = StrategyConfig(
        "lock_in_full", no_stake_usd=1000.0, hedge_mode="lock_in", kelly_fraction=1.0
    )

    result = compute_ev(bracket, market, strategy)

    elimination_rows = [row for row in result.outcome_rows if row.stage_index is not None]
    assert len(elimination_rows) == len(bracket.stages)
    for row in elimination_rows:
        assert row.profit_usd == pytest.approx(0.0, abs=1e-9), (row.scenario, row.profit_usd)


def test_lock_in_kelly_fraction_half():
    """At kelly_fraction=0.5, only half the reserve is restaked each round,
    so elimination at any stage must leave a strictly positive profit (the
    unstaked half of the reserve), not zero and not a loss."""
    bracket, market = _lock_in_bracket_and_market()
    strategy = StrategyConfig(
        "lock_in_half", no_stake_usd=1000.0, hedge_mode="lock_in", kelly_fraction=0.5
    )

    result = compute_ev(bracket, market, strategy)

    elimination_rows = [row for row in result.outcome_rows if row.stage_index is not None]
    assert len(elimination_rows) == len(bracket.stages)
    for row in elimination_rows:
        assert row.profit_usd > 0.0, (row.scenario, row.profit_usd)


def test_lock_in_max_hedge_stake_breaks_exact_zero_floor():
    """A severely capped max_hedge_stake prevents the full reserve from
    being restaked. Algebraically this can only ever *shrink* h_r below
    locked_value_r (min() never grows it), so profit_k = locked_value_k -
    h_k stays >= 0 -- capping makes elimination MORE profitable, never
    negative, at kelly_fraction<=1.0. What actually breaks is the EXACT
    $0 guarantee: with a low enough cap, every elimination stage pays a
    strictly positive surplus instead of precisely $0. (The original
    assumption that capping causes a *loss* doesn't hold algebraically --
    that would require kelly_fraction > 1.0, i.e. staking more than the
    guaranteed reserve, which is a different misuse than this cap.)"""
    bracket, market = _lock_in_bracket_and_market()
    strategy = StrategyConfig(
        "lock_in_capped",
        no_stake_usd=1000.0,
        hedge_mode="lock_in",
        kelly_fraction=1.0,
        max_hedge_stake=1.0,  # far below what full lock_in would stake
    )

    result = compute_ev(bracket, market, strategy)

    elimination_rows = [row for row in result.outcome_rows if row.stage_index is not None]
    assert len(elimination_rows) == len(bracket.stages)
    for row in elimination_rows:
        assert row.profit_usd > 1e-6, (row.scenario, row.profit_usd)


def test_lock_in_kelly_fraction_above_one_can_produce_a_loss():
    """The actual way to break the zero floor toward a LOSS: kelly_fraction
    > 1.0 stakes more than the guaranteed reserve (h_r > locked_value_r),
    so elimination at that stage necessarily pays negative. This is the
    real failure mode the "breaks the guarantee" language in the design
    doc was describing -- it's driven by kelly_fraction, not by
    max_hedge_stake (which can only ever shrink a stake, never grow it
    past the reserve)."""
    bracket, market = _lock_in_bracket_and_market()
    strategy = StrategyConfig(
        "lock_in_overbet", no_stake_usd=1000.0, hedge_mode="lock_in", kelly_fraction=1.5
    )

    result = compute_ev(bracket, market, strategy)

    elimination_rows = [row for row in result.outcome_rows if row.stage_index is not None]
    assert any(row.profit_usd < 0.0 for row in elimination_rows)


def test_lock_in_none_hedge_odds():
    """A stage with hedge_decimal_odds=None gets h_r=0 under lock_in and
    doesn't break the rest of the plan."""
    bracket = Bracket(
        team="TeamB",
        tournament="Test Cup",
        sport="football",
        stages=[
            Stage("R1", 0.6, hedge_decimal_odds=2.0),
            Stage("R2", 0.5, hedge_decimal_odds=None),
            Stage("R3", 0.4, hedge_decimal_odds=1.9),
        ],
    )
    market = MarketPrices(no_price=0.9, yes_price=0.1)
    strategy = StrategyConfig("lock_in", no_stake_usd=100.0, hedge_mode="lock_in", kelly_fraction=1.0)

    plan = compute_hedge_plan(bracket.stages, strategy, market)

    assert plan[1] == pytest.approx(0.0)
    assert plan[0] > 0.0
    assert plan[2] > 0.0


def test_lock_in_integration_ev_equals_win_scenario_only():
    """With kelly_fraction=1.0 every elimination-stage profit is exactly 0
    (see test_lock_in_zero_floor_algebraic), so by compute_ev's own
    P*profit weighted sum, EV must collapse to exactly
    P(team wins the whole tournament) * profit_win.

    NOTE: profit_win is NOT simply net_no_loss (-no_stake_usd) -- it also
    includes the hedge profit compounded across every stage the team won,
    which under full lock_in can be substantial (in this example it nearly
    cancels the NO stake loss entirely). This test recomputes profit_win
    independently via strategies.compute_hedge_plan rather than assuming
    it equals net_no_loss, since that assumption turned out to be wrong
    for this bracket (EV came out at +$2.97, not -$74.25, when first
    tried with the naive net_no_loss-only formula)."""
    bracket, market = _lock_in_bracket_and_market()
    strategy = StrategyConfig(
        "lock_in_full", no_stake_usd=1000.0, hedge_mode="lock_in", kelly_fraction=1.0
    )

    hedge_stakes = compute_hedge_plan(bracket.stages, strategy, market)
    net_no_loss = -strategy.no_stake_usd
    cum_hedge_profit = sum(
        h * (stage.hedge_decimal_odds - 1)
        for h, stage in zip(hedge_stakes, bracket.stages)
        if stage.hedge_decimal_odds is not None
    )
    profit_win = net_no_loss + cum_hedge_profit
    expected_ev = bracket.title_prob * profit_win

    result = compute_ev(bracket, market, strategy)
    assert result.expected_value_usd == pytest.approx(expected_ev, abs=1e-9)
