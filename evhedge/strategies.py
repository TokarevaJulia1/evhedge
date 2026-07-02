"""Hedge stake sizing.

Turns a ``StrategyConfig`` + list of ``Stage`` into a concrete, deterministic
sequence of hedge stakes ``h_r`` — one per stage, in the same order as
``Bracket.stages``.

The sizing is deterministic (not path-dependent) even for the "reinvest"
and "lock_in" modes: to *reach* stage ``r`` a team must have won every
stage before it, so there is only one possible path leading up to stage
``r``, and the accumulated hedge profit going into stage ``r`` is
therefore a well-defined function of ``r`` alone.
"""

from __future__ import annotations

from evhedge.models import MarketPrices, Stage, StrategyConfig


def compute_hedge_plan(
    stages: list[Stage], strategy: StrategyConfig, market: MarketPrices
) -> list[float]:
    """Compute the hedge stake ``h_r`` for every stage.

    Args:
        stages: Ordered stages of a ``Bracket`` (see ``Bracket.stages``).
        strategy: Sizing configuration — ``no_stake_usd`` ("proportional"
            mode), ``bankroll`` ("kelly" mode), ``hedge_mode``,
            ``hedge_base_stake``, ``kelly_fraction``, ``max_hedge_stake``.
        market: Raw market prices. Needed ONLY by "lock_in" mode, which has
            to know ``net_no_win = no_stake_usd * (1 - no_price) /
            no_price`` (the same formula ``evhedge.engine`` uses) to size
            the very first stage's hedge from the NO position's own
            guaranteed payout. Every other mode ignores this parameter.
            (Earlier versions of this project threaded ``market`` through
            here by mistake for modes that didn't need it -- see
            CHANGELOG.md. This time it's a real dependency, not a repeat
            of that drift.)

    Returns:
        A list of USD hedge stakes, same length and order as ``stages``.
        Entries are 0.0 for stages with no ``hedge_decimal_odds`` or when
        ``strategy.hedge_mode == "none"``.
    """
    hedge_stakes: list[float] = []
    cum_hedge_profit = 0.0
    net_no_win = strategy.no_stake_usd * (1 - market.no_price) / market.no_price

    for stage in stages:
        stake = _stage_hedge_stake(stage, strategy, cum_hedge_profit, net_no_win)

        if strategy.max_hedge_stake is not None:
            # NOTE: for hedge_mode="lock_in", clipping here breaks the
            # EXACT-zero-floor guarantee -- min() can only ever shrink
            # h_r below the "ideal" locked_value_r * kelly_fraction, never
            # grow it, so elimination at the capped stage keeps some of
            # the reserve unstaked and pays a small PROFIT instead of
            # exactly $0 (never a loss, at kelly_fraction<=1: h_r <=
            # locked_value_r always holds once capped downward). The
            # trade-off is losing the *precision* of the guarantee (no
            # longer exactly break-even), not its safety -- see
            # StrategyConfig.hedge_mode docstring and
            # test_lock_in_max_hedge_stake_breaks_exact_zero_floor.
            stake = min(stake, strategy.max_hedge_stake)
        stake = max(stake, 0.0)

        hedge_stakes.append(stake)

        if stage.hedge_decimal_odds is not None:
            cum_hedge_profit += stake * (stage.hedge_decimal_odds - 1)

    return hedge_stakes


def _stage_hedge_stake(
    stage: Stage, strategy: StrategyConfig, cum_hedge_profit: float, net_no_win: float
) -> float:
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

    if strategy.hedge_mode == "lock_in":
        locked_value = net_no_win + cum_hedge_profit
        return locked_value * strategy.kelly_fraction

    raise ValueError(f"Unknown hedge_mode: {strategy.hedge_mode!r}")
