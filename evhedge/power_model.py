"""Power-ratings pairwise win probability model, derived purely from
outright market prices (no head-to-head odds needed).

Use case: you have an outright ("win the whole tournament") price for two
teams, but no direct match odds between them (e.g. they haven't been drawn
against each other yet, or no book prices that specific matchup). This
model backs out a per-round "strength" from each team's outright price and
the number of knockout rounds separating them from the title, then
converts a strength ratio into a single-match win probability via a
variable-slope logistic curve.

Calibration boundaries (read before using this for anything):
    - Outright ratio < 3x between the two teams: expect ~1-2pp error.
    - Ratio 3x-20x: expect ~2-5pp error.
    - Ratio 50x+: this model SYSTEMATICALLY UNDERSTATES the favorite --
      do not use it out there. If you need a number for a 50x+ mismatch,
      get a real match price instead.
    - This model is a fallback for pairs with NO market price, never a
      substitute for one that exists.
    - For non-uniform tournament formats (bracket depth differs by path --
      e.g. some teams enter at the round of 16, others via a play-in) `n`
      (rounds_to_title) is not well-defined for a consistent comparison
      across teams, so this model is DISABLED there; use direct match
      odds or evhedge.strategies-level Stage probabilities instead.
"""

from __future__ import annotations

import math


def strength(outright_pct: float, rounds_to_title: int) -> float:
    """Back out a per-round strength from an outright ("wins the whole
    tournament") market price.

    Args:
        outright_pct: Outright win probability as a percentage (e.g. 3.6
            for 3.6%, not 0.036).
        rounds_to_title: Number of knockout rounds remaining between this
            team and the title (``n`` -- see module docstring on why this
            must be well-defined/uniform across the teams being compared).

    Returns:
        strength = (outright_pct / 100) ** (1 / rounds_to_title).

    Raises:
        ValueError: If outright_pct is not in (0, 100] or rounds_to_title
            is not a positive integer.
    """
    if not (0.0 < outright_pct <= 100.0):
        raise ValueError(f"outright_pct must be in (0, 100], got {outright_pct}")
    if rounds_to_title <= 0:
        raise ValueError(f"rounds_to_title must be a positive integer, got {rounds_to_title}")
    return (outright_pct / 100.0) ** (1.0 / rounds_to_title)


def _gamma_for(log_ratio: float) -> float:
    """Logistic slope, steeper for bigger strength gaps -- fit against the
    regression fixtures in tests/test_power_model.py, not derived
    analytically."""
    ad = abs(log_ratio)
    if ad < 0.3:
        return 1.0
    if ad < 0.7:
        return 1.85
    return 2.4


def pair_prob(strength_a: float, strength_b: float) -> float:
    """Probability that team A beats team B in a single confrontation,
    given their per-round strengths (see ``strength``).

    Args:
        strength_a: Team A's strength.
        strength_b: Team B's strength.

    Returns:
        P(A beats B), in (0, 1).

    Raises:
        ValueError: If either strength is <= 0.
    """
    if strength_a <= 0.0 or strength_b <= 0.0:
        raise ValueError(
            f"strengths must be > 0, got strength_a={strength_a}, strength_b={strength_b}"
        )
    d = math.log(strength_a / strength_b)
    gamma = _gamma_for(d)
    return 1.0 / (1.0 + math.exp(-gamma * d))
