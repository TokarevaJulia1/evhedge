"""Hedge stake sizing.

Turns a ``StrategyConfig`` + list of ``Stage`` into a concrete, deterministic
sequence of hedge stakes ``h_r`` — one per stage, in the same order as
``Bracket.stages``.

The sizing is deterministic (not path-dependent) even for the "reinvest"
mode: to *reach* stage ``r`` a team must have won every stage before it, so
there is only one possible path leading up to stage ``r``, and the
accumulated hedge profit going into stage ``r`` is therefore a well-defined
function of ``r`` alone.
"""

from __future__ import annotations

from evhedge.models import Stage, StrategyConfig


def compute_hedge_plan(stages: list[Stage], strategy: StrategyConfig) -> list[float]:
    """Compute the hedge stake ``h_r`` for every stage.

    Args:
        stages: Ordered stages of a ``Bracket`` (see ``Bracket.stages``).
        strategy: Sizing configuration — ``no_stake_usd`` ("proportional"
            mode), ``bankroll`` ("kelly" mode), ``hedge_mode``,
            ``hedge_base_stake``, ``kelly_fraction``, ``max_hedge_stake``.

    Returns:
        A list of USD hedge stakes, same length and order as ``stages``.
        Entries are 0.0 for stages with no ``hedge_decimal_odds`` or when
        ``strategy.hedge_mode == "none"``.
    """
    hedge_stakes: list[float] = []
    cum_hedge_profit = 0.0

    for stage in stages:
        stake = _stage_hedge_stake(stage, strategy, cum_hedge_profit)

        if strategy.max_hedge_stake is not None:
            stake = min(stake, strategy.max_hedge_stake)
        stake = max(stake, 0.0)

        hedge_stakes.append(stake)

        if stage.hedge_decimal_odds is not None:
            cum_hedge_profit += stake * (stage.hedge_decimal_odds - 1)

    return hedge_stakes


def _stage_hedge_stake(stage: Stage, strategy: StrategyConfig, cum_hedge_profit: float) -> float:
    if stage.hedge_decimal_odds is None or strategy.hedge_mode == "none":
        return 0.0

    if strategy.hedge_mode == "fixed":
        return strategy.hedge_base_stake

    if strategy.hedge_mode == "proportional":
        return strategy.hedge_base_stake * strategy.no_stake_usd

    if strategy.hedge_mode == "reinvest":
        return strategy.hedge_base_stake + cum_hedge_profit * strategy.kelly_fraction

    if strategy.hedge_mode == "kelly":
        d = stage.hedge_decimal_odds
        f_star = (stage.win_prob * d - 1) / (d - 1)
        return strategy.bankroll * f_star * strategy.kelly_fraction

    raise ValueError(f"Unknown hedge_mode: {strategy.hedge_mode!r}")
