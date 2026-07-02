"""Hand-computed sanity checks for evhedge.engine.compute_ev."""

import pytest

from evhedge.engine import compute_ev
from evhedge.models import Bracket, MarketPrices, Stage, StrategyConfig


def test_ev_two_stage_no_hedge():
    """2 stages, no hedging at all — pure NO position.

    p1=0.6, p2=0.5, no_price=0.9, no_stake=100.

    net_no_win  = 100 * (1 - 0.9) / 0.9 = 11.111...
    net_no_loss = -100

    P(elim R1) = 1 - 0.6           = 0.4, profit = net_no_win
    P(elim R2) = 0.6 * (1 - 0.5)   = 0.3, profit = net_no_win
    P(win)     = 0.6 * 0.5         = 0.3, profit = net_no_loss

    EV = 0.7 * 11.111... + 0.3 * (-100) = -22.222...
    """
    bracket = Bracket("TeamA", "football", [Stage("R1", 0.6), Stage("R2", 0.5)])
    market = MarketPrices(no_price=0.9, yes_price=0.1)
    strategy = StrategyConfig("none", no_stake_usd=100.0, hedge_mode="none")

    result = compute_ev(bracket, market, strategy)

    net_no_win = 100.0 * (1 - 0.9) / 0.9
    assert result.outcome_rows[0].probability == pytest.approx(0.4)
    assert result.outcome_rows[0].profit_usd == pytest.approx(net_no_win)
    assert result.outcome_rows[1].probability == pytest.approx(0.3)
    assert result.outcome_rows[1].profit_usd == pytest.approx(net_no_win)
    assert result.outcome_rows[2].probability == pytest.approx(0.3)
    assert result.outcome_rows[2].profit_usd == pytest.approx(-100.0)

    assert result.expected_value_usd == pytest.approx(-22.2222222, abs=1e-5)
    assert result.total_risk_usd == pytest.approx(100.0)
    assert result.ev_per_dollar_risk == pytest.approx(-0.2222222, abs=1e-5)


def test_ev_two_stage_fixed_hedge():
    """Same bracket, fixed $10 hedge on both stages (odds 2.0 then 1.5).

    net_no_win = 11.111..., net_no_loss = -100
    h1=10, d1=2.0 -> h1*(d1-1) = 10
    h2=10, d2=1.5 -> h2*(d2-1) = 5

    profit(R1) = net_no_win - h1                     = 1.111...
    profit(R2) = net_no_win + h1*(d1-1) - h2          = 11.111...
    profit(win)= net_no_loss + h1*(d1-1) + h2*(d2-1)  = -85
    """
    bracket = Bracket(
        "TeamB",
        "football",
        [Stage("R1", 0.6, hedge_decimal_odds=2.0), Stage("R2", 0.5, hedge_decimal_odds=1.5)],
    )
    market = MarketPrices(no_price=0.9, yes_price=0.1)
    strategy = StrategyConfig("fixed10", no_stake_usd=100.0, hedge_mode="fixed", hedge_base_stake=10.0)

    result = compute_ev(bracket, market, strategy)

    net_no_win = 100.0 * (1 - 0.9) / 0.9
    assert result.outcome_rows[0].profit_usd == pytest.approx(net_no_win - 10.0)
    assert result.outcome_rows[1].profit_usd == pytest.approx(net_no_win + 10.0 - 10.0)
    assert result.outcome_rows[2].profit_usd == pytest.approx(-85.0)
    assert result.total_risk_usd == pytest.approx(120.0)
    assert result.expected_value_usd == pytest.approx(-21.72222, abs=1e-4)


def test_ev_hedge_skipped_without_odds():
    """Stages without hedge_decimal_odds never get a hedge stake, even in
    'fixed' mode."""
    bracket = Bracket("TeamC", "golf", [Stage("Cut", 0.7), Stage("Top20", 0.4)])
    market = MarketPrices(no_price=0.85, yes_price=0.15)
    strategy = StrategyConfig("fixed", no_stake_usd=50.0, hedge_mode="fixed", hedge_base_stake=25.0)

    result = compute_ev(bracket, market, strategy)

    assert result.total_risk_usd == pytest.approx(50.0)
