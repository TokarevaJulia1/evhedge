"""Exact (closed-form, non-Monte-Carlo) EV computation for a single bracket.

See the module docstring math summary below; the formulas mirror the design
doc exactly.

Let ``R`` be the number of stages, ``p_r`` the conditional win probability of
stage ``r``, ``h_r`` the hedge stake on stage ``r`` (0 if no hedge), ``d_r``
the hedge decimal odds on stage ``r``.

``N = no_stake_usd / no_price`` NO shares are bought.

If the team is eliminated (NO resolves in our favor):
    ``net_no_win = no_stake_usd * (1 - no_price) / no_price``

If the team wins the tournament (NO resolves against us):
    ``net_no_loss = -no_stake_usd``

Profit if eliminated at stage ``k`` (1-indexed):
    ``profit(k) = net_no_win + sum_{r=1}^{k-1} h_r * (d_r - 1) - h_k``

Profit if the team wins the whole tournament:
    ``profit(win) = net_no_loss + sum_{r=1}^{R} h_r * (d_r - 1)``

Path probabilities:
    ``P(eliminated at k) = (prod_{r=1}^{k-1} p_r) * (1 - p_k)``
    ``P(win) = prod_{r=1}^{R} p_r``

    ``EV = sum_k P(eliminated at k) * profit(k) + P(win) * profit(win)``
"""

from __future__ import annotations

from evhedge.models import Bracket, EVResult, MarketPrices, OutcomeRow, StrategyConfig
from evhedge.strategies import compute_hedge_plan


def compute_ev(bracket: Bracket, market: MarketPrices, strategy: StrategyConfig) -> EVResult:
    """Compute the exact EV of the NO position + hedge plan for one bracket.

    Args:
        bracket: Team and its ordered stages.
        market: Raw market prices (``no_price``/``yes_price``).
        strategy: Position sizing (``no_stake_usd``, ``bankroll``) and hedge
            sizing configuration.

    Returns:
        An ``EVResult`` with the full discretized outcome table (one row per
        elimination stage plus one "wins tournament" row), expected value,
        total capital at risk, EV per dollar of risk, and profit
        variance/std dev.
    """
    stages = bracket.stages
    hedge_stakes = compute_hedge_plan(stages, strategy)

    net_no_win = strategy.no_stake_usd * (1 - market.no_price) / market.no_price
    net_no_loss = -strategy.no_stake_usd

    outcome_rows: list[OutcomeRow] = []
    ev = 0.0
    cum_survive_prob = 1.0
    cum_hedge_profit = 0.0

    for k, stage in enumerate(stages):
        p_eliminated = cum_survive_prob * (1 - stage.win_prob)
        profit_k = net_no_win + cum_hedge_profit - hedge_stakes[k]

        outcome_rows.append(
            OutcomeRow(
                scenario=f"Eliminated at {stage.name}",
                stage_index=k,
                probability=p_eliminated,
                profit_usd=profit_k,
            )
        )
        ev += p_eliminated * profit_k

        if stage.hedge_decimal_odds is not None:
            cum_hedge_profit += hedge_stakes[k] * (stage.hedge_decimal_odds - 1)
        cum_survive_prob *= stage.win_prob

    p_win = cum_survive_prob
    profit_win = net_no_loss + cum_hedge_profit
    outcome_rows.append(
        OutcomeRow(
            scenario="Wins tournament",
            stage_index=None,
            probability=p_win,
            profit_usd=profit_win,
        )
    )
    ev += p_win * profit_win

    total_risk = strategy.no_stake_usd + sum(hedge_stakes)
    variance = sum(row.probability * (row.profit_usd - ev) ** 2 for row in outcome_rows)
    std_dev = variance**0.5

    return EVResult(
        team=bracket.team,
        expected_value_usd=ev,
        total_risk_usd=total_risk,
        ev_per_dollar_risk=(ev / total_risk) if total_risk > 0 else 0.0,
        outcome_rows=outcome_rows,
        variance_usd=variance,
        std_dev_usd=std_dev,
    )
