"""Command-line interface for evhedge.

Thin wrapper over evhedge.config_io / evhedge.engine / evhedge.montecarlo —
no business logic lives here, only argument parsing, error presentation,
and formatting results as rich tables.
"""

from __future__ import annotations

import re
import sys
from datetime import timedelta
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

from evhedge.collect import CollectError, collect_board, collect_match_markets
from evhedge.config_io import ConfigError, load_full_config
from evhedge.consistency import (
    VERIFY_BOOK_CAVEAT,
    ConsistencyError,
    load_board_config,
    run_board_checks,
)
from evhedge.data_sources import polymarket as polymarket_ds
from evhedge.data_sources.polymarket import PolymarketAPIError
from evhedge.engine import compute_ev
from evhedge.montecarlo import plot_distribution, simulate
from evhedge.ranking import load_configs_from_dir, rank_teams
from evhedge.scanner import (
    HYPE_VELOCITY_WINDOW_HOURS,
    ScannerError,
    load_scanner_config,
    scan,
    sort_candidates,
)
from evhedge.storage import (
    Resolve,
    Storage,
    StorageError,
    board_snapshots,
    no_market_label,
    utcnow,
)

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


def _fmt_range(point: float, rng, fmt: str = "{:.2f}") -> str:
    """Point value when data is complete, low–high band when it isn't."""
    if rng is None:
        return fmt.format(point)
    return f"{fmt.format(rng[0])}–{fmt.format(rng[1])}"


def _fmt_liquidity(liq) -> str:
    if liq.status == "checked":
        avg = f"@{liq.executable_avg_price:.2f}" if liq.executable_avg_price is not None else ""
        return f"${liq.executable_usd:.0f}{avg}"
    return "unknown"


@main.command("scan")
@click.argument("config", type=click.Path(path_type=Path))
@click.option(
    "--min-outright",
    type=float,
    default=None,
    help="Drop candidates with outright % below this (filter out dust; the "
    "config's outright_threshold_pct stays the UPPER bound).",
)
@click.option("--top", type=int, default=None, help="Only show the top K rows.")
@click.option(
    "--db",
    "db_path",
    type=click.Path(path_type=Path),
    default=None,
    help="Snapshot DB: record this board + the scan's passports, and "
    "compute the HYPE flag from price velocity history (without --db the "
    "manual recent_upset fallback applies).",
)
def scan_command(
    config: Path, min_outright: Optional[float], top: Optional[int], db_path: Optional[Path]
) -> None:
    """Scan CONFIG.yaml (scanner format) for long-shot bracket candidates."""
    try:
        scanner_config = load_scanner_config(config)
    except (ConfigError, ScannerError) as e:
        error_console.print(f"Ошибка: {e}")
        sys.exit(1)

    store: Optional[Storage] = None
    velocities: Optional[dict[str, float]] = None
    try:
        if db_path is not None:
            store = Storage(db_path)
            # Record today's board FIRST so the velocity window includes
            # the freshest point.
            store.record_snapshots(board_snapshots(scanner_config))
            market_label = no_market_label(scanner_config.target_market)
            window = timedelta(hours=HYPE_VELOCITY_WINDOW_HOURS)
            velocities = {}
            for team in scanner_config.no_prices:
                v = store.price_velocity(
                    scanner_config.tournament, team, market_label, window
                )
                if v is not None:
                    velocities[team] = v

        reports = scan(scanner_config, no_velocities_pp_per_hour=velocities)

        if store is not None:
            run_id = store.record_scan(
                scanner_config.tournament, scanner_config.target_market,
                reports, config_path=str(config),
            )
            console.print(f"БД {db_path}: снапшоты доски + паспорта записаны (run #{run_id})")
    except (ScannerError, StorageError) as e:
        error_console.print(f"Ошибка: {e}")
        sys.exit(1)
    finally:
        if store is not None:
            store.close()

    if min_outright is not None:
        reports = [r for r in reports if scanner_config.teams[r.team] >= min_outright]
    if not reports:
        warn_console.print("Ни одного кандидата (порог/фильтры/no_prices).")
        return

    reports = sort_candidates(reports)
    if top is not None:
        reports = reports[:top]

    table = Table(title=f"{scanner_config.tournament} — {scanner_config.target_market}")
    table.add_column("#", justify="right")
    table.add_column("Команда")
    table.add_column("FUEL")
    table.add_column("NO ask", justify="right")
    table.add_column("Прем.%", justify="right")
    table.add_column("Треб.×", justify="right")
    table.add_column("Дост.×", justify="right")
    table.add_column("Deadness", justify="right")
    table.add_column("Ликвидность", justify="right")
    table.add_column("рынок/модель/дыры", justify="right")
    table.add_column("Флаги")

    for i, r in enumerate(reports, start=1):
        flags = []
        if r.leg_profile_flag:
            flags.append("FAV")
        if r.hype_flag:
            flags.append("HYPE(v)" if r.hype_source == "computed" else "HYPE(m)")
        src = r.sources_breakdown
        table.add_row(
            str(i),
            r.team,
            r.fuel_verdict,
            f"{r.no_price:.1f}",
            f"{r.premium_pct:.1f}",
            f"{r.required_multiplier:.1f}",
            _fmt_range(r.available_multiplier, r.available_multiplier_range, "{:.1f}"),
            _fmt_range(r.deadness, r.deadness_range),
            _fmt_liquidity(r.liquidity),
            f"{src['market']}/{src['model']}/{src['no_data']}",
            " ".join(flags) or "—",
        )

    console.print(table)

    excluded = reports[0].excluded_stages
    if excluded:
        warn_console.print(f"Стадии вне roll-цепочки: {', '.join(excluded)}")
    warn_console.print(VERIFY_BOOK_CAVEAT)


@main.command("book")
@click.argument("token_id")
@click.option("--side", type=click.Choice(["buy", "sell"]), default="buy", show_default=True)
@click.option(
    "--depth-to",
    "depth_to",
    type=float,
    default=None,
    help="Worst acceptable price, in 0..1 shares (0.05 = 5c): print the "
    "executable USD size up to it.",
)
def book_command(token_id: str, side: str, depth_to: Optional[float]) -> None:
    """Show the live CLOB order book for TOKEN_ID (top 10 levels per side)."""
    if depth_to is not None and not (0.0 < depth_to < 1.0):
        raise click.UsageError(
            f"--depth-to задаётся в долях 0..1 (0.05 = 5c), получено {depth_to}"
        )

    try:
        book = polymarket_ds.fetch_order_book(token_id)
    except PolymarketAPIError as e:
        error_console.print(f"Ошибка: {e}")
        sys.exit(1)

    table = Table(title=f"CLOB book {token_id}")
    table.add_column("Side")
    table.add_column("Цена", justify="right")
    table.add_column("Размер, shares", justify="right")
    table.add_column("USD", justify="right")

    for lvl in sorted(book.asks, key=lambda l: l.price)[:10]:
        table.add_row("ask", f"{lvl.price:.3f}", f"{lvl.size:.1f}", f"{lvl.price * lvl.size:.2f}")
    for lvl in sorted(book.bids, key=lambda l: l.price, reverse=True)[:10]:
        table.add_row("bid", f"{lvl.price:.3f}", f"{lvl.size:.1f}", f"{lvl.price * lvl.size:.2f}")
    console.print(table)

    if depth_to is not None:
        usd, avg_price = polymarket_ds.executable_size(book, side, depth_to)
        if avg_price is None:
            warn_console.print(f"Исполнимо ({side} до {depth_to}): ничего (пустая книга/вне лимита)")
        else:
            console.print(f"Исполнимо ({side} до {depth_to}): ${usd:.2f} @ {avg_price:.4f} средняя")


@main.command("snapshot")
@click.argument("config", type=click.Path(path_type=Path))
@click.option(
    "--db",
    "db_path",
    type=click.Path(path_type=Path),
    default=Path("evhedge.db"),
    show_default=True,
    help="Snapshot DB file (created if missing).",
)
def snapshot_command(config: Path, db_path: Path) -> None:
    """Record CONFIG.yaml's board prices (no_prices + leg_prices) into the DB."""
    try:
        scanner_config = load_scanner_config(config)
    except (ConfigError, ScannerError) as e:
        error_console.print(f"Ошибка: {e}")
        sys.exit(1)

    try:
        snaps = board_snapshots(scanner_config)
        with Storage(db_path) as store:
            store.record_snapshots(snaps)
    except StorageError as e:
        error_console.print(f"Ошибка БД: {e}")
        sys.exit(1)

    console.print(
        f"Записано снапшотов: {len(snaps)} ({scanner_config.tournament}) -> {db_path}"
    )


@main.command("pull")
@click.option("--tournament", required=True, help="Tournament label to record under.")
@click.option(
    "--board",
    "boards",
    multiple=True,
    help="EVENT_SLUG:LABEL — snapshot a Yes/No-per-team Gamma event under "
    "market label LABEL (e.g. ewc-dota-2-winner-2026...:winner). Repeatable.",
)
@click.option(
    "--matches",
    "matches_spec",
    default=None,
    help='TAG:TITLE_FILTER — walk match events under Gamma TAG whose title '
    'contains TITLE_FILTER: open series -> leg snapshots, closed games -> '
    'resolves (e.g. "dota-2:Esports World Cup").',
)
@click.option(
    "--matches-since",
    "matches_since",
    default=None,
    help="ISO date filter for --matches (e.g. 2026-07-01). Practically "
    "required on busy tags: Gamma 422s deep pagination over settled events.",
)
@click.option(
    "--db",
    "db_path",
    type=click.Path(path_type=Path),
    default=Path("evhedge.db"),
    show_default=True,
    help="Snapshot DB file (created if missing).",
)
def pull_command(
    tournament: str,
    boards: tuple[str, ...],
    matches_spec: Optional[str],
    matches_since: Optional[str],
    db_path: Path,
) -> None:
    """Collect live Gamma board prices and match results into the DB."""
    if not boards and matches_spec is None:
        raise click.UsageError("нужно хотя бы одно из --board / --matches")

    parsed_boards = []
    for spec in boards:
        slug, sep, label = spec.partition(":")
        if not sep or not slug or not label:
            raise click.UsageError(f"--board ожидает EVENT_SLUG:LABEL, получено {spec!r}")
        parsed_boards.append((slug, label))
    if matches_spec is not None:
        tag, sep, title_filter = matches_spec.partition(":")
        if not sep or not tag or not title_filter:
            raise click.UsageError(f"--matches ожидает TAG:TITLE_FILTER, получено {matches_spec!r}")

    summaries = []
    try:
        with Storage(db_path) as store:
            for slug, label in parsed_boards:
                summaries.append(collect_board(store, tournament, slug, label))
            if matches_spec is not None:
                summaries.append(collect_match_markets(
                    store, tournament, tag, title_filter, start_date_min=matches_since,
                ))
    except (CollectError, PolymarketAPIError, StorageError) as e:
        error_console.print(f"Ошибка: {e}")
        sys.exit(1)

    table = Table(title=f"pull — {tournament} -> {db_path}")
    table.add_column("Источник")
    table.add_column("Рынков", justify="right")
    table.add_column("Снапшотов", justify="right")
    table.add_column("Резолвов", justify="right")
    table.add_column("Пропущено (плейсх./форма/нерешено/цена/лайв)", justify="right")
    for s in summaries:
        table.add_row(
            "; ".join(s.labels),
            str(s.markets_seen),
            str(s.snapshots_written),
            str(s.resolves_written),
            f"{s.skipped_placeholders}/{s.skipped_shape}/{s.skipped_unresolved}"
            f"/{s.skipped_price_range}/{s.skipped_live}",
        )
    console.print(table)


@main.command("resolve")
@click.argument("tournament")
@click.argument("team")
@click.argument("market")
@click.argument("outcome", type=click.Choice(["yes", "no"]))
@click.option("--note", default=None, help="Free-text note, e.g. how the elimination went.")
@click.option(
    "--db",
    "db_path",
    type=click.Path(path_type=Path),
    default=Path("evhedge.db"),
    show_default=True,
    help="Snapshot DB file.",
)
def resolve_command(
    tournament: str, team: str, market: str, outcome: str, note: Optional[str], db_path: Path
) -> None:
    """Record how TEAM's MARKET resolved: the MARKET's outcome (yes|no),
    not our position's (a NO position wins on "no")."""
    try:
        with Storage(db_path) as store:
            store.record_resolve(Resolve(
                tournament=tournament, team=team, market=market,
                outcome=outcome, ts_utc=utcnow(), note=note,
            ))
    except StorageError as e:
        error_console.print(f"Ошибка БД: {e}")
        sys.exit(1)

    console.print(f"Резолв записан: {tournament} / {team} / {market} = {outcome}")


@main.command("check")
@click.argument("config", type=click.Path(path_type=Path))
def check_command(config: Path) -> None:
    """Run board-level consistency checks from CONFIG.yaml."""
    try:
        report = run_board_checks(load_board_config(config))
    except (ConfigError, ConsistencyError) as e:
        error_console.print(f"Ошибка: {e}")
        sys.exit(1)

    if report.baskets:
        table = Table(title=f"{report.board} — корзины (NO, фиксированные слоты)")
        table.add_column("Корзина")
        table.add_column("Кост", justify="right")
        table.add_column("Выплата", justify="right")
        table.add_column("Эдж, п.", justify="right")
        table.add_column("Доходность", justify="right")
        table.add_column("Сигнал")
        for name, r in report.baskets:
            table.add_row(
                name, f"{r.cost_pct:.2f}", f"{r.payout_pct:.0f}",
                f"{r.edge_pct:+.2f}", f"{r.return_pct:+.2f}%", "ДА" if r.is_signal else "—",
            )
        console.print(table)

    if report.identities:
        table = Table(title=f"{report.board} — агрегат = сумма членов")
        table.add_column("Родитель")
        table.add_column("Родит.%", justify="right")
        table.add_column("Σ членов", justify="right")
        table.add_column("Разрыв, п.", justify="right")
        table.add_column("Богатая сторона")
        table.add_column("Сигнал")
        for r in report.identities:
            table.add_row(
                r.parent, f"{r.parent_yes_pct:.2f}", f"{r.members_sum_pct:.2f}",
                f"{r.diff_pct:+.2f}", r.rich_side, "ДА" if r.is_signal else "—",
            )
        console.print(table)

    if report.verticals:
        table = Table(title=f"{report.board} — вертикали reach_X")
        table.add_column("Команда")
        table.add_column("Ступени", justify="right")
        table.add_column("Нарушения", justify="right")
        table.add_column("Флаги", justify="right")
        table.add_column("Сигнал")
        for r in report.verticals:
            table.add_row(
                r.team, str(len(r.ladder)), str(len(r.violations)),
                str(len(r.flags)), "ДА" if r.is_signal else "—",
            )
        console.print(table)
        for r in report.verticals:
            for line in r.violations:
                error_console.print(f"  {r.team}: {line}")
            for line in r.flags:
                warn_console.print(f"  {r.team}: {line}")

    warn_console.print(VERIFY_BOOK_CAVEAT)


if __name__ == "__main__":
    main()
