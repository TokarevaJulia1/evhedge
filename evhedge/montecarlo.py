"""Monte Carlo simulation of the profit distribution for a bracket + hedge
plan, as a cross-check on the closed-form ``evhedge.engine.compute_ev``.

Reuses ``evhedge.strategies.compute_hedge_plan`` for hedge sizing — the same
function ``evhedge.engine`` calls — so the "how much do we bet on stage r"
answer can never silently diverge between the analytical and simulated
paths. Do not reimplement hedge_mode logic here.
"""

from __future__ import annotations

from typing import Optional

import numpy as np

from evhedge.models import Bracket, MarketPrices, StrategyConfig
from evhedge.strategies import compute_hedge_plan


def simulate(
    bracket: Bracket,
    market: MarketPrices,
    strategy: StrategyConfig,
    n_trials: int = 20000,
    seed: Optional[int] = None,
) -> dict:
    """Simulate the NO position + hedge plan profit distribution by path.

    Args:
        bracket: Team and its ordered stages.
        market: Raw market prices (``no_price``/``yes_price``).
        strategy: Position sizing and hedge sizing configuration.
        n_trials: Number of independent tournament paths to simulate.
        seed: Seed for the random number generator; same seed => identical
            ``profits`` array across calls.

    Returns:
        dict with keys:
            profits: np.ndarray, shape (n_trials,) — simulated net profit.
            mean, median, std: float summary stats of ``profits``.
            prob_profit: float — fraction of trials with profit > 0.
            var_5pct: float — 5th percentile of profits (Value at Risk).
            cvar_5pct: float — mean of profits at or below ``var_5pct``.
            worst_case, best_case: float — min/max of ``profits``.
            eliminated_at_stage_counts: dict[str, int] — keys are stage
                names plus ``"won"``, values are how many of the n_trials
                paths ended there. Values sum to n_trials.
    """
    stages = bracket.stages
    n_stages = len(stages)

    # Hedge sizing is deterministic by stage index (the path leading up to
    # stage r is unique), so it's computed once, not per trial.
    hedge_stakes = compute_hedge_plan(stages, strategy, market)

    net_no_win = strategy.no_stake_usd * (1 - market.no_price) / market.no_price
    net_no_loss = -strategy.no_stake_usd

    rng = np.random.default_rng(seed)
    win_probs = np.array([stage.win_prob for stage in stages])
    uniforms = rng.random((n_trials, n_stages))
    stage_won = uniforms < win_probs  # shape (n_trials, n_stages)

    profits = np.empty(n_trials)
    stage_names = [stage.name for stage in stages]
    eliminated_at_stage_counts = {name: 0 for name in stage_names}
    eliminated_at_stage_counts["won"] = 0

    for i in range(n_trials):
        cum_hedge_profit = 0.0
        outcome_label = None
        profit = 0.0

        for r, stage in enumerate(stages):
            if not stage_won[i, r]:
                profit = net_no_win + cum_hedge_profit - hedge_stakes[r]
                outcome_label = stage.name
                break
            if stage.hedge_decimal_odds is not None:
                cum_hedge_profit += hedge_stakes[r] * (stage.hedge_decimal_odds - 1)
        else:
            profit = net_no_loss + cum_hedge_profit
            outcome_label = "won"

        profits[i] = profit
        eliminated_at_stage_counts[outcome_label] += 1

    var_5pct = float(np.percentile(profits, 5))
    below_var = profits[profits <= var_5pct]

    return {
        "profits": profits,
        "mean": float(profits.mean()),
        "median": float(np.median(profits)),
        "std": float(profits.std()),
        "prob_profit": float((profits > 0).mean()),
        "var_5pct": var_5pct,
        "cvar_5pct": float(below_var.mean()),
        "worst_case": float(profits.min()),
        "best_case": float(profits.max()),
        "eliminated_at_stage_counts": eliminated_at_stage_counts,
    }


def plot_distribution(
    mc_result: dict,
    analytical_ev: float,
    save_path: str,
    bracket: Optional[Bracket] = None,
) -> None:
    """Plot a histogram of the simulated profit distribution.

    Args:
        mc_result: Output of ``simulate``.
        analytical_ev: Expected value from ``evhedge.engine.compute_ev``
            (``EVResult.expected_value_usd``), drawn as a reference line.
        save_path: File path to save the figure to (format inferred from
            extension, e.g. ``.png``).
        bracket: Optional bracket, used only to label the plot title with
            the team/sport. Falls back to a generic title if omitted.
    """
    import matplotlib.pyplot as plt

    profits = mc_result["profits"]

    fig, ax = plt.subplots(figsize=(10, 6))
    ax.hist(profits, bins=60, color="steelblue", alpha=0.75, edgecolor="white")

    ax.axvline(0, color="gray", linestyle="--", linewidth=1.5, label="break-even")
    ax.axvline(
        analytical_ev, color="black", linestyle="-", linewidth=1.5, label="EV аналитический"
    )
    ax.axvline(
        mc_result["var_5pct"], color="red", linestyle="--", linewidth=1.5, label="VaR 5%"
    )

    ax.set_xlabel("Профит, $")
    ax.set_ylabel("Количество симуляций")
    title = "Распределение профита"
    if bracket is not None:
        title += f" — {bracket.team} ({bracket.sport})"
    ax.set_title(title)
    ax.legend()

    fig.tight_layout()
    fig.savefig(save_path)
    plt.close(fig)
