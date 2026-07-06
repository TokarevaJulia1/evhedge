"""Tests for evhedge.power_model, including the three regression fixtures
from the design doc (World Cup 2026 outright prices, tolerance 0.5pp)."""

import pytest

from evhedge.power_model import pair_prob, strength

# (team_a_pct, team_b_pct, n, expected P(A beats B), tolerance in probability points)
REGRESSION_FIXTURES = [
    ("Mexico-England", 3.6, 7.9, 4, 0.451),
    ("USA-Belgium", 2.7, 1.6, 4, 0.533),
    ("Morocco-France", 2.6, 34.5, 4, 0.232),
]


@pytest.mark.parametrize("name,pct_a,pct_b,n,expected", REGRESSION_FIXTURES)
def test_regression_fixtures(name, pct_a, pct_b, n, expected):
    sa = strength(pct_a, n)
    sb = strength(pct_b, n)
    p = pair_prob(sa, sb)
    assert p == pytest.approx(expected, abs=0.005), f"{name}: got {p:.4f}, expected ~{expected}"


def test_strength_rejects_out_of_range_pct():
    with pytest.raises(ValueError, match="outright_pct"):
        strength(0.0, 4)
    with pytest.raises(ValueError, match="outright_pct"):
        strength(150.0, 4)


def test_strength_rejects_nonpositive_rounds():
    with pytest.raises(ValueError, match="rounds_to_title"):
        strength(10.0, 0)


def test_pair_prob_equal_strength_is_fifty_fifty():
    s = strength(10.0, 4)
    assert pair_prob(s, s) == pytest.approx(0.5)


def test_pair_prob_symmetric():
    sa = strength(20.0, 4)
    sb = strength(5.0, 4)
    p_ab = pair_prob(sa, sb)
    p_ba = pair_prob(sb, sa)
    assert p_ab + p_ba == pytest.approx(1.0)


def test_pair_prob_rejects_nonpositive_strength():
    with pytest.raises(ValueError, match="strengths must be > 0"):
        pair_prob(0.0, 0.5)
    with pytest.raises(ValueError, match="strengths must be > 0"):
        pair_prob(0.5, -1.0)


def test_gamma_thresholds_are_continuous_enough_not_to_invert_ordering():
    """Bigger gaps between strengths should never produce a LOWER win
    probability for the stronger team, even across the gamma
    threshold boundaries (0.3, 0.7) -- a basic sanity check on the
    piecewise slope, since it was fit to fixtures rather than derived."""
    base = strength(10.0, 4)
    weaker_pcts = [9.0, 6.0, 3.0, 1.0, 0.5, 0.1]
    probs = [pair_prob(base, strength(p, 4)) for p in weaker_pcts]
    assert probs == sorted(probs)
