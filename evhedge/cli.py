"""Command-line interface for evhedge.

Thin wrapper over evhedge.config_io / evhedge.engine / evhedge.montecarlo —
no business logic lives here, only argument parsing, error presentation,
and formatting results as rich tables.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Optional

import click
from rich.console import Console
from rich.table import Table

# On Windows, the console codepage is often not UTF-8, which garbles the
# Cyrillic labels used throughout this CLI. Force UTF-8 stdout/stderr
# regardless of the ambient locale (equivalent to PYTHONIOENCODING=utf-8).
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

from evhedge.config_io import ConfigError, load_full_config
from evhedge.engine import compute_ev
from evhedge.montecarlo import plot_distribution, simulate
from evhedge.ranking import load_configs_from_dir, rank_teams

console = Console(width=120)
error_console = Console(width=120, stderr=True, style="bold red")
warn_console = Console(width=120, style="yellow")

#: Sports evhedge example currently knows how to generate a template for.
SUPPORTED_EXAMPLE_SPORTS = ("football",)


@click.group()
def main() -> None:
    """evhedge — EV analysis and hedging planner for tournament outright bets."""


def _stage_label(bracket, row) -> str:
    if row.stage_index is None:
        return "Выигран турнир"
    return bracket.stages[row.stage_index].name


def _print_outcome_table(bracket, result) -> None:
    table = Table(title=f"{bracket.team} — {bracket.tournament}")
    table.add_column("Стадия")
    table.add_column("Вероятность", justify="right")
    table.add_column("Профит, $", justify="right")

    for row in result.outcome_rows:
        table.add_row(
            _stage_label(bracket, row),
            f"{row.probability * 100:.2f}%",
            f"{row.profit_usd:+.2f}",
        )

    console.print(table)


def _print_summary_table(result) -> None:
    table = Table(title="Сводка (аналитический EV)")
    table.add_column("Метрика")
    table.add_column("Значение", justify="right")

    table.add_row("Ожидаемая доходность (EV)", f"{result.expected_value_usd:+.2f} $")
    table.add_row("Суммарный риск", f"{result.total_risk_usd:.2f} $")
    table.add_row("EV на $ риска", f"{result.ev_per_dollar_risk:+.4f}")

    console.print(table)


def _print_mc_summary_table(mc_result: dict, n_trials: int, seed: Optional[int]) -> None:
    table = Table(title=f"Monte Carlo (n_trials={n_trials}, seed={seed})")
    table.add_column("Метрика")
    table.add_column("Значение", justify="right")

    table.add_row("mean", f"{mc_result['mean']:+.2f} $")
    table.add_row("median", f"{mc_result['median']:+.2f} $")
    table.add_row("std", f"{mc_result['std']:.2f} $")
    table.add_row("prob_profit", f"{mc_result['prob_profit'] * 100:.2f}%")
    table.add_row("var_5pct", f"{mc_result['var_5pct']:+.2f} $")
    table.add_row("cvar_5pct", f"{mc_result['cvar_5pct']:+.2f} $")
    table.add_row("worst_case", f"{mc_result['worst_case']:+.2f} $")
    table.add_row("best_case", f"{mc_result['best_case']:+.2f} $")

    console.print(table)


def _print_mc_stage_counts_table(mc_result: dict, n_trials: int) -> None:
    table = Table(title="Частоты по исходам (Monte Carlo)")
    table.add_column("Исход")
    table.add_column("Count", justify="right")
    table.add_column("Частота", justify="right")

    for label, count in mc_result["eliminated_at_stage_counts"].items():
        table.add_row(label, str(count), f"{count / n_trials * 100:.2f}%")

    console.print(table)


def _sanitize_filename(text: str) -> str:
    return re.sub(r"[^\w\-.]+", "_", text).strip("_")


@main.command("ev")
@click.argument("config", type=click.Path(exists=False, path_type=Path))
@click.option("--mc", "n_trials", type=int, default=None, help="Run Monte Carlo with N trials.")
@click.option("--plot", "make_plot", is_flag=True, default=False, help="Save a profit distribution plot (requires --mc).")
@click.option("--seed", type=int, default=None, help="Seed for Monte Carlo (omit for a random run).")
@click.option(
    "--out-dir",
    type=click.Path(path_type=Path),
    default=Path("./output/"),
    help="Directory to save plots into (default: ./output/).",
)
def ev_command(
    config: Path,
    n_trials: Optional[int],
    make_plot: bool,
    seed: Optional[int],
    out_dir: Path,
) -> None:
    """Compute EV (and optionally Monte Carlo stats/plot) for CONFIG.yaml."""
    if make_plot and n_trials is None:
        raise click.UsageError("--plot requires --mc N (need Monte Carlo samples to plot).")

    try:
        bracket, market, strategy = load_full_config(config)
    except ConfigError as e:
        error_console.print(f"Ошибка конфигурации: {e}")
        sys.exit(1)

    result = compute_ev(bracket, market, strategy)
    _print_outcome_table(bracket, result)
    _print_summary_table(result)

    if n_trials is not None:
        mc_result = simulate(bracket, market, strategy, n_trials=n_trials, seed=seed)
        _print_mc_summary_table(mc_result, n_trials, seed)
        _print_mc_stage_counts_table(mc_result, n_trials)

        if make_plot:
            out_dir.mkdir(parents=True, exist_ok=True)
            filename = _sanitize_filename(f"{bracket.team}_{bracket.tournament}") + "_distribution.png"
            save_path = out_dir / filename
            plot_distribution(
                mc_result, result.expected_value_usd, str(save_path), bracket=bracket
            )
            console.print(f"График сохранён: {save_path}")


_FOOTBALL_EXAMPLE_YAML = """\
# Пример конфига для evhedge ev — числа ниже ДЕМОНСТРАЦИОННЫЕ.
# Замените стадии/вероятности/коэффициенты на актуальные для реального
# турнира и текущего рынка перед использованием.
team: "Team X"
sport: football
tournament: "Some Cup 2027"

stages:
  - name: "1/8 финала"
    win_prob: 0.65
    hedge_decimal_odds: 2.1
  - name: "1/4 финала"
    win_prob: 0.55
    hedge_decimal_odds: null
  - name: "1/2 финала"
    win_prob: 0.50
    hedge_decimal_odds: 1.9
  - name: "Финал"
    win_prob: 0.45
    hedge_decimal_odds: 2.6

market:
  no_price: 0.91
  yes_price: 0.09     # опционально, можно не указывать -- по умолчанию 1 - no_price

strategy:
  name: "reinvest base20 kelly0.5"
  no_stake_usd: 1000
  bankroll: null       # опционально; если null/не указано -> = no_stake_usd
  hedge_mode: reinvest
  hedge_base_stake: 20
  kelly_fraction: 0.5
  max_hedge_stake: 200
"""

_EXAMPLE_TEMPLATES = {"football": _FOOTBALL_EXAMPLE_YAML}


@main.command("example")
@click.option("--sport", default="football", help="Sport template to generate (currently: football).")
@click.option(
    "--out",
    type=click.Path(path_type=Path),
    default=Path("evhedge_example.yaml"),
    help="Output file path (default: evhedge_example.yaml).",
)
@click.option("--force", is_flag=True, default=False, help="Overwrite --out without asking.")
def example_command(sport: str, out: Path, force: bool) -> None:
    """Generate an example YAML config that evhedge ev can load as-is."""
    if sport not in _EXAMPLE_TEMPLATES:
        raise click.UsageError(
            f"Неизвестный --sport {sport!r}. Поддерживается сейчас: "
            f"{list(SUPPORTED_EXAMPLE_SPORTS)}."
        )

    if out.exists() and not force:
        click.confirm(f"Файл {out} уже существует. Перезаписать?", abort=True)

    out.write_text(_EXAMPLE_TEMPLATES[sport], encoding="utf-8")
    console.print(f"Пример конфига сохранён: {out}")


@main.command("rank")
@click.argument("configs_dir", type=click.Path(path_type=Path))
@click.option(
    "--sort-by",
    type=click.Choice(["ev", "ev_pct", "sharpe"]),
    default="ev",
    help="Metric to sort by (default: ev).",
)
@click.option("--mc", "mc_trials", type=int, default=5000, help="Monte Carlo trials per config.")
@click.option("--seed", type=int, default=None, help="Seed for Monte Carlo (same seed for all configs).")
@click.option("--top", type=int, default=None, help="Only show the top K rows.")
def rank_command(
    configs_dir: Path,
    sort_by: str,
    mc_trials: int,
    seed: Optional[int],
    top: Optional[int],
) -> None:
    """Rank every *.yaml/*.yml config in CONFIGS_DIR by EV."""
    try:
        configs, failures = load_configs_from_dir(configs_dir)
    except ConfigError as e:
        error_console.print(f"Ошибка: {e}")
        sys.exit(1)

    if not configs:
        error_console.print(
            f"Не удалось загрузить ни одного валидного конфига из {configs_dir}."
        )
        sys.exit(1)

    rows = rank_teams(configs, mc_trials=mc_trials, seed=seed, sort_by=sort_by)
    if top is not None:
        rows = rows[:top]

    table = Table(title=f"Ранжирование по {sort_by} (n_trials={mc_trials})")
    table.add_column("#", justify="right")
    table.add_column("Команда")
    table.add_column("Турнир")
    table.add_column("EV, $", justify="right")
    table.add_column("EV, %", justify="right")
    table.add_column("EV/$ риска", justify="right")
    table.add_column("P(профит) MC", justify="right")
    table.add_column("Sharpe", justify="right")

    for i, row in enumerate(rows, start=1):
        sharpe_str = f"{row['sharpe']:.3f}" if row["sharpe"] is not None else "—"
        table.add_row(
            str(i),
            row["team"],
            row["tournament"],
            f"{row['ev']:+.2f}",
            f"{row['ev_pct']:+.2f}%",
            f"{row['ev_per_dollar_risk']:+.4f}",
            f"{row['mc_prob_profit'] * 100:.2f}%",
            sharpe_str,
        )

    console.print(table)

    if failures:
        warn_table = Table(title="Пропущено (ошибки конфигурации)")
        warn_table.add_column("Файл")
        warn_table.add_column("Причина")
        for path, message in failures:
            warn_table.add_row(str(path), message)
        warn_console.print(warn_table)


if __name__ == "__main__":
    main()
