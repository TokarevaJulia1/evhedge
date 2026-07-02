"""Tests for evhedge.strategies.compute_hedge_plan across all hedge modes."""

import pytest

from evhedge.models import Stage, StrategyConfig
from evhedge.strategies import compute_hedge_plan


@pytest.fixture
def stages():
    return [
        Stage("R1", 0.6, hedge_decimal_odds=2.0),
        Stage("R2", 0.5, hedge_decimal_odds=1.5),
        Stage("R3", 0.4, hedge_decimal_odds=None),  # no hedge market available
    ]


def test_none_mode_never_hedges(stages):
    strategy = StrategyConfig("none", no_stake_usd=100.0, hedge_mode="none")
    assert compute_hedge_plan(stages, strategy) == [0.0, 0.0, 0.0]


def test_fixed_mode(stages):
    strategy = StrategyConfig("fixed", no_stake_usd=100.0, hedge_mode="fixed", hedge_base_stake=10.0)
    assert compute_hedge_plan(stages, strategy) == [10.0, 10.0, 0.0]


def test_proportional_mode(stages):
    strategy = StrategyConfig(
        "prop", no_stake_usd=100.0, hedge_mode="proportional", hedge_base_stake=0.1
    )
    assert compute_hedge_plan(stages, strategy) == [10.0, 10.0, 0.0]


def test_reinvest_mode(stages):
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
    plan = compute_hedge_plan(stages, strategy)
    assert plan[0] == pytest.approx(5.0)
    assert plan[1] == pytest.approx(10.0)
    assert plan[2] == pytest.approx(0.0)


def test_kelly_mode_uses_bankroll(stages):
    """f* = (p*d - 1) / (d - 1); stage R1: p=0.6, d=2.0 -> f* = (1.2-1)/1 = 0.2
    h1 = bankroll * f* * kelly_fraction = 100 * 0.2 * 1.0 = 20

    kelly_fraction=1.0 passed explicitly here: we're checking full-Kelly
    arithmetic against the formula, not the half-Kelly default.
    """
    strategy = StrategyConfig("kelly", no_stake_usd=100.0, hedge_mode="kelly", kelly_fraction=1.0)
    plan = compute_hedge_plan(stages, strategy)
    assert plan[0] == pytest.approx(20.0)
    assert plan[2] == pytest.approx(0.0)


def test_kelly_mode_default_is_half_kelly(stages):
    """Same setup as test_kelly_mode_uses_bankroll but relying on the
    StrategyConfig default (kelly_fraction=0.5) instead of passing it
    explicitly.

    f* = (0.6*2.0 - 1) / (2.0 - 1) = 0.2
    h1 = bankroll * f* * kelly_fraction = 100 * 0.2 * 0.5 = 10  (half of the
    full-Kelly h1=20 from test_kelly_mode_uses_bankroll)
    """
    strategy = StrategyConfig("kelly_default", no_stake_usd=100.0, hedge_mode="kelly")
    assert strategy.kelly_fraction == pytest.approx(0.5)
    plan = compute_hedge_plan(stages, strategy)
    assert plan[0] == pytest.approx(10.0)
    assert plan[2] == pytest.approx(0.0)


def test_max_hedge_stake_caps_all_modes(stages):
    strategy = StrategyConfig(
        "fixed_capped",
        no_stake_usd=100.0,
        hedge_mode="fixed",
        hedge_base_stake=10.0,
        max_hedge_stake=7.0,
    )
    plan = compute_hedge_plan(stages, strategy)
    assert plan == [7.0, 7.0, 0.0]


def test_negative_kelly_edge_clips_to_zero():
    """When the hedge odds imply negative edge, Kelly sizing must not go
    negative — true regardless of kelly_fraction sign/magnitude since the
    default (0.5) only scales the (already negative) f*, it doesn't flip
    its sign."""
    stages = [Stage("R1", 0.3, hedge_decimal_odds=1.5)]  # f* = (0.45-1)/0.5 < 0
    strategy = StrategyConfig("kelly", no_stake_usd=100.0, hedge_mode="kelly")
    plan = compute_hedge_plan(stages, strategy)
    assert plan == [0.0]


def test_bankroll_defaults_to_no_stake_usd():
    strategy = StrategyConfig("kelly", no_stake_usd=250.0, hedge_mode="kelly")
    assert strategy.bankroll == pytest.approx(250.0)
