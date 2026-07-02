"""Rank multiple (Bracket, MarketPrices, StrategyConfig) configs by EV.

Combines the exact EV from ``evhedge.engine.compute_ev`` with a Monte Carlo
cross-check from ``evhedge.montecarlo.simulate`` for each config, so ranking
by expected value can be paired with risk-adjusted metrics (Sharpe-style
mean/std, P(profit)).
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Union

from evhedge.config_io import ConfigError, load_full_config
from evhedge.engine import compute_ev
from evhedge.models import Bracket, MarketPrices, StrategyConfig
from evhedge.montecarlo import simulate

PathLike = Union[str, Path]

#: Valid values for rank_teams' sort_by.
SORT_KEYS = ("ev", "ev_pct", "sharpe")

ConfigTuple = tuple[Bracket, MarketPrices, StrategyConfig]


def _sharpe(mean: float, std: float) -> Optional[float]:
    """mean/std, or None if std == 0 -- not an error, just "not comparable
    this way" (distinct from a Sharpe of 0)."""
    return mean / std if std > 0 else None


def rank_teams(
    configs: list[ConfigTuple],
    mc_trials: int = 5000,
    seed: Optional[int] = None,
    sort_by: str = "ev",
) -> list[dict]:
    """Compute EV + Monte Carlo stats for each config and sort the results.

    Args:
        configs: List of ``(bracket, market, strategy)`` tuples, e.g. from
            ``load_configs_from_dir``.
        mc_trials: Number of Monte Carlo trials per config.
        seed: Seed passed to ``montecarlo.simulate`` for every config (same
            seed for all configs, so the comparison isn't confounded by
            different RNG draws).
        sort_by: One of ``"ev"``, ``"ev_pct"``, ``"sharpe"`` — sorted
            descending. Rows with ``sharpe is None`` always sort last,
            regardless of ``sort_by``.

    Returns:
        List of dicts (one per config), each with keys: ``team``,
        ``tournament``, ``sport``, ``ev`` (= EVResult.expected_value_usd),
        ``ev_pct`` (= ev / no_stake_usd * 100), ``total_risk``,
        ``ev_per_dollar_risk``, ``mc_prob_profit``, ``mc_worst_case``,
        ``mc_best_case``, ``sharpe`` (mc mean/std, or ``None`` if
        std == 0 -- not an error, just "not comparable this way").

    Raises:
        ValueError: If ``sort_by`` is not one of ``SORT_KEYS``.
    """
    if sort_by not in SORT_KEYS:
        raise ValueError(f"sort_by must be one of {SORT_KEYS}, got {sort_by!r}")

    rows: list[dict] = []
    for bracket, market, strategy in configs:
        result = compute_ev(bracket, market, strategy)
        mc = simulate(bracket, market, strategy, n_trials=mc_trials, seed=seed)
        sharpe = _sharpe(mc["mean"], mc["std"])

        rows.append(
            {
                "team": bracket.team,
                "tournament": bracket.tournament,
                "sport": bracket.sport,
                "ev": result.expected_value_usd,
                "ev_pct": result.expected_value_usd / strategy.no_stake_usd * 100,
                "total_risk": result.total_risk_usd,
                "ev_per_dollar_risk": result.ev_per_dollar_risk,
                "mc_prob_profit": mc["prob_profit"],
                "mc_worst_case": mc["worst_case"],
                "mc_best_case": mc["best_case"],
                "sharpe": sharpe,
            }
        )

    def sort_key(row: dict):
        value = row[sort_by]
        # (is_none, -value): non-None rows (is_none=False=0) always sort
        # before None rows (True=1); within each group, -value ascending
        # is value descending.
        return (value is None, -value if value is not None else 0.0)

    rows.sort(key=sort_key)
    return rows


def load_configs_from_dir(
    dir_path: PathLike,
) -> tuple[list[ConfigTuple], list[tuple[Path, str]]]:
    """Load every ``*.yaml``/``*.yml`` file directly in ``dir_path`` (not
    recursive) as a full config via ``config_io.load_full_config``.

    A single broken file does not abort the whole batch: it's collected
    into the second return value instead, so e.g. 31 out of 32 valid
    tournament configs can still be ranked even if one file is bad.

    Args:
        dir_path: Directory to scan for config files.

    Returns:
        ``(configs, failures)`` where ``configs`` is the list of
        successfully loaded ``(bracket, market, strategy)`` tuples and
        ``failures`` is a list of ``(path, error_message)`` for files that
        raised ``ConfigError``.

    Raises:
        ConfigError: If ``dir_path`` does not exist / is not a directory,
            or contains no ``.yaml``/``.yml`` files at all.
    """
    dir_path = Path(dir_path)
    if not dir_path.is_dir():
        raise ConfigError(f"{dir_path}: директория не найдена")

    yaml_files = sorted(
        p for p in dir_path.iterdir() if p.is_file() and p.suffix.lower() in (".yaml", ".yml")
    )
    if not yaml_files:
        raise ConfigError(f"{dir_path}: не найдено ни одного .yaml/.yml файла")

    configs: list[ConfigTuple] = []
    failures: list[tuple[Path, str]] = []

    for path in yaml_files:
        try:
            configs.append(load_full_config(path))
        except ConfigError as e:
            failures.append((path, str(e)))

    return configs, failures
