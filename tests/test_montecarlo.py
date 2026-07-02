"""Tests for evhedge.montecarlo.simulate — convergence to the closed-form
EV from evhedge.engine, and basic sanity checks on the returned stats."""

import numpy as np
import pytest

from evhedge.engine import compute_ev
from evhedge.models import Bracket, MarketPrices, Stage, StrategyConfig
from evhedge.montecarlo import simulate

REL_TOL = 0.05


def _no_hedge_setup():
    bracket = Bracket(
        team="TeamA",
        tournament="Test Cup",
        sport="football",
        stages=[Stage("R1", 0.6), Stage("R2", 0.5), Stage("QF", 0.55)],
    )
    market = MarketPrices(no_price=0.9, yes_price=0.1)
    strategy = StrategyConfig("none", no_stake_usd=100.0, hedge_mode="none")
    return bracket, market, strategy


def _fixed_hedge_setup():
    bracket = Bracket(
        team="TeamB",
        tournament="Test Cup",
        sport="football",
        stages=[
            Stage("R1", 0.6, hedge_decimal_odds=2.0),
            Stage("R2", 0.5, hedge_decimal_odds=1.5),
            Stage("QF", 0.55, hedge_decimal_odds=1.8),
        ],
    )
    market = MarketPrices(no_price=0.9, yes_price=0.1)
    strategy = StrategyConfig(
        "fixed10", no_stake_usd=100.0, hedge_mode="fixed", hedge_base_stake=10.0
    )
    return bracket, market, strategy


def test_mc_converges_to_analytical_ev_no_hedge():
    bracket, market, strategy = _no_hedge_setup()
    analytical_ev = compute_ev(bracket, market, strategy).expected_value_usd

    mc = simulate(bracket, market, strategy, n_trials=50000, seed=42)

    rel_error = abs(mc["mean"] - analytical_ev) / abs(analytical_ev)
    assert rel_error < REL_TOL


def test_mc_converges_to_analytical_ev_fixed_hedge():
    bracket, market, strategy = _fixed_hedge_setup()
    analytical_ev = compute_ev(bracket, market, strategy).expected_value_usd

    mc = simulate(bracket, market, strategy, n_trials=50000, seed=42)

    rel_error = abs(mc["mean"] - analytical_ev) / abs(analytical_ev)
    assert rel_error < REL_TOL


def test_eliminated_at_stage_counts_match_analytical_probabilities():
    bracket, market, strategy = _fixed_hedge_setup()
    analytical = compute_ev(bracket, market, strategy)
    n_trials = 50000

    mc = simulate(bracket, market, strategy, n_trials=n_trials, seed=7)
    counts = mc["eliminated_at_stage_counts"]

    assert sum(counts.values()) == n_trials

    for row in analytical.outcome_rows:
        label = "won" if row.stage_index is None else bracket.stages[row.stage_index].name
        observed_freq = counts[label] / n_trials
        if row.probability > 0:
            rel_error = abs(observed_freq - row.probability) / row.probability
            assert rel_error < REL_TOL, (label, observed_freq, row.probability)


def test_reproducibility_with_same_seed():
    bracket, market, strategy = _fixed_hedge_setup()

    mc1 = simulate(bracket, market, strategy, n_trials=1000, seed=123)
    mc2 = simulate(bracket, market, strategy, n_trials=1000, seed=123)

    assert np.array_equal(mc1["profits"], mc2["profits"])


def test_percentile_ordering_is_sane():
    bracket, market, strategy = _fixed_hedge_setup()
    mc = simulate(bracket, market, strategy, n_trials=20000, seed=1)

    assert mc["worst_case"] <= mc["var_5pct"]
    assert mc["var_5pct"] <= mc["median"]
    assert mc["median"] <= mc["best_case"]
