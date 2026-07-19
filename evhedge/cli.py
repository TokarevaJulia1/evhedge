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

from evhedge.auto_predict import load_stage_ranks, status_report
from evhedge.collect import CollectError, collect_board, collect_match_markets
from evhedge.config_io import ConfigError, load_full_config
from evhedge.consistency import (
    VERIFY_BOOK_CAVEAT,
    ConsistencyError,
    load_board_config,
    run_board_checks,
)
from evhedge.data_sources import polymarket as polymarket_ds
from evhedge.data_sources.pinnacle import devig_range
from evhedge.data_sources.polymarket import PolymarketAPIError
from evhedge.engine import compute_ev
from evhedge.montecarlo import plot_distribution, simulate
from evhedge.ranking import load_configs_from_dir, rank_teams
from evhedge.scanner import (
    HYPE_VELOCITY_WINDOW_HOURS,
    ScannerError,
    bracket_teams,
    load_scanner_config,
    scan,
    sort_candidates,
)
from evhedge.storage import (
    Prediction,
    Resolve,
    Storage,
    StorageError,
    board_snapshots,
    no_market_label,
    utcnow,
)
from evhedge.team_aliases import canonical_name, load_default_aliases, suggest_aliases

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
@click.option(
    "--verify-book/--no-verify-book",
    default=True,
    show_default=True,
    help="Fetch real order-book bid/ask per side instead of trusting Gamma's "
    "board price (which for a Yes/No pair is a single derived number, not an "
    "independent second observation -- see collect.collect_board). Costs one "
    "extra request per side; falls back to the board price on failure.",
)
@click.option(
    "--stage-ranks",
    "stage_ranks_path",
    type=click.Path(path_type=Path),
    default=None,
    help="auto_predict.load_stage_ranks YAML ({team: rounds_to_title}) for the "
    "model half of auto-recorded --matches predictions. Omit to auto-record "
    "market-only predictions (p_model=NULL) -- never a crash.",
)
def pull_command(
    tournament: str,
    boards: tuple[str, ...],
    matches_spec: Optional[str],
    matches_since: Optional[str],
    db_path: Path,
    verify_book: bool,
    stage_ranks_path: Optional[Path],
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

    stage_ranks = None
    if stage_ranks_path is not None:
        try:
            stage_ranks = load_stage_ranks(stage_ranks_path)
        except ConfigError as e:
            error_console.print(f"Ошибка --stage-ranks: {e}")
            sys.exit(1)

    summaries = []
    try:
        with Storage(db_path) as store:
            for slug, label in parsed_boards:
                summaries.append(collect_board(store, tournament, slug, label, verify_book=verify_book))
            if matches_spec is not None:
                summaries.append(collect_match_markets(
                    store, tournament, tag, title_filter, start_date_min=matches_since,
                    verify_book=verify_book, stage_ranks=stage_ranks,
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
    table.add_column("Book->board fallback", justify="right")
    table.add_column("Прогнозов (нов./дубль/NULL)", justify="right")
    for s in summaries:
        table.add_row(
            "; ".join(s.labels),
            str(s.markets_seen),
            str(s.snapshots_written),
            str(s.resolves_written),
            f"{s.skipped_placeholders}/{s.skipped_shape}/{s.skipped_unresolved}"
            f"/{s.skipped_price_range}/{s.skipped_live}",
            str(s.book_fallback_to_board),
            f"{s.predictions_written}/{s.predictions_skipped_duplicate}/{s.predictions_model_null}",
        )
    console.print(table)


@main.command("webapp")
@click.option("--port", type=int, default=8787, show_default=True, help="Local port to serve on.")
def webapp_command(port: int) -> None:
    """Launch the local Roll-Over Chain dashboard at http://127.0.0.1:PORT.

    Serves the calculator page plus a thin JSON API over live Polymarket
    data (open positions by wallet address, order book by token id) --
    localhost-only, read-only, no order placement.
    """
    from evhedge.webapp import run_server

    run_server(port=port)


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


@main.command("predict")
@click.option(
    "--db", "db_path", type=click.Path(path_type=Path), default=Path("evhedge.db"),
    show_default=True, help="Snapshot DB file.",
)
@click.option("--tournament", required=True, help="Tournament label.")
@click.option("--team", required=True, help="Any known spelling -- canonicalized on write.")
@click.option(
    "--market", required=True,
    help="Same format as resolve's MARKET (JOIN-compatible with resolves.market).",
)
@click.option(
    "--model-p", "model_p", type=float, default=None,
    help="Model probability for this outcome, 0..1 (e.g. 0.62).",
)
@click.option(
    "--model", "model_auto", type=click.Choice(["auto"]), default=None,
    help="Auto-compute p_model via power_model.pair_prob -- NOT YET IMPLEMENTED "
    "(needs bracket/counterparty/rounds-to-title scaffolding this command doesn't "
    "have); use --model-p manually for now.",
)
@click.option(
    "--poly", default=None,
    help='Polymarket YES book at fixation time: "bid/ask" as 0..1 fractions, or '
    '"auto" to pull best_bid_ask via the latest snapshot\'s token_id.',
)
@click.option(
    "--pin", default=None,
    help='Pinnacle decimal odds for this market\'s two outcomes, "yes_dec/no_dec" '
    "-- always passed through devig_range, never computed by hand.",
)
@click.option("--note", default=None, help="Free-text note.")
def predict_command(
    db_path: Path,
    tournament: str,
    team: str,
    market: str,
    model_p: Optional[float],
    model_auto: Optional[str],
    poly: Optional[str],
    pin: Optional[str],
    note: Optional[str],
) -> None:
    """Fix a forecast for TEAM's MARKET before it resolves. Immutable: a
    second predict on the same (tournament, team, market) is an error, not
    an update -- fix a typo by hand in sqlite if you truly must."""
    if model_auto is not None:
        raise click.UsageError(
            "--model auto пока не реализован (TODO) -- используйте --model-p вручную"
        )
    p_model = model_p

    canon_team = canonical_name(team, load_default_aliases())

    p_bid: Optional[float] = None
    p_ask: Optional[float] = None
    if poly is not None:
        if poly == "auto":
            with Storage(db_path) as store:
                snaps = [
                    s for s in store.snapshots(tournament, team=canon_team, market=market)
                    if s.token_id
                ]
            if not snaps:
                raise click.UsageError(
                    f"--poly auto: в БД нет снапшота с token_id для "
                    f"{canon_team!r}/{market!r} ({tournament!r})"
                )
            token_id = snaps[-1].token_id
            try:
                book = polymarket_ds.fetch_order_book(token_id)
            except PolymarketAPIError as e:
                error_console.print(f"Ошибка: {e}")
                sys.exit(1)
            p_bid, p_ask = polymarket_ds.best_bid_ask(book)
            if p_bid is None or p_ask is None:
                raise click.UsageError(
                    "--poly auto: пустая книга (bid или ask отсутствует) -- задайте вручную"
                )
        else:
            bid_str, sep, ask_str = poly.partition("/")
            if not sep:
                raise click.UsageError(f'--poly ожидает "bid/ask" или "auto", получено {poly!r}')
            try:
                p_bid, p_ask = float(bid_str), float(ask_str)
            except ValueError:
                raise click.UsageError(f"--poly: не удалось разобрать числа из {poly!r}")

    p_pin_low: Optional[float] = None
    p_pin_high: Optional[float] = None
    if pin is not None:
        yes_str, sep, no_str = pin.partition("/")
        if not sep:
            raise click.UsageError(f'--pin ожидает "yes_dec/no_dec", получено {pin!r}')
        try:
            d_yes, d_no = float(yes_str), float(no_str)
        except ValueError:
            raise click.UsageError(f"--pin: не удалось разобрать числа из {pin!r}")
        try:
            proportional, all_margin = devig_range([d_yes, d_no])
        except ValueError as e:
            raise click.UsageError(f"--pin: {e}")
        p_pin_low = min(proportional[0], all_margin[0])
        p_pin_high = max(proportional[0], all_margin[0])

    try:
        with Storage(db_path) as store:
            pred = Prediction(
                tournament=tournament, team=canon_team, market=market, ts_utc=utcnow(),
                p_model=p_model, p_market_bid=p_bid, p_market_ask=p_ask,
                p_pin_low=p_pin_low, p_pin_high=p_pin_high, note=note,
            )
            store.record_prediction(pred)
    except StorageError as e:
        error_console.print(f"Ошибка: {e}")
        sys.exit(1)

    console.print(
        f"Прогноз записан: {tournament} / {canon_team} / {market} -- "
        f"p_model={p_model if p_model is not None else '—'}, "
        f"poly bid/ask={p_bid}/{p_ask}, "
        f"pin range={p_pin_low}-{p_pin_high}"
    )

    if p_model is not None and p_pin_low is not None and p_pin_high is not None:
        lo, hi = p_pin_low - 0.05, p_pin_high + 0.05
        if not (lo <= p_model <= hi):
            warn_console.print(
                f"WARNING: p_model={p_model:.3f} вне диапазона Pinnacle "
                f"[{p_pin_low:.3f}, {p_pin_high:.3f}] ± 5pp"
            )


@main.command("score")
@click.option(
    "--db", "db_path", type=click.Path(path_type=Path), default=Path("evhedge.db"),
    show_default=True, help="Snapshot DB file.",
)
@click.option("--tournament", default=None, help="Limit to one tournament.")
def score_command(db_path: Path, tournament: Optional[str]) -> None:
    """Score every recorded prediction against its resolve: Brier of the
    model, the Polymarket book mid, and the Pinnacle devig mid."""
    try:
        with Storage(db_path) as store:
            report = store.score_predictions(tournament=tournament)
    except StorageError as e:
        error_console.print(f"Ошибка: {e}")
        sys.exit(1)

    if report.pending:
        table = Table(title=f"PENDING — ещё не резолвнуто ({len(report.pending)})")
        table.add_column("Турнир")
        table.add_column("Команда")
        table.add_column("Рынок")
        for p in report.pending:
            table.add_row(p.tournament, p.team, p.market)
        console.print(table)

    if not report.scored:
        warn_console.print("Нет резолвнутых прогнозов для скоринга.")
        return

    table = Table(title=f"score — {tournament or 'все турниры'}")
    table.add_column("Турнир")
    table.add_column("Команда")
    table.add_column("Рынок")
    table.add_column("Исход")
    table.add_column("Brier модель", justify="right")
    table.add_column("Brier рынок", justify="right")
    table.add_column("Brier Pinnacle", justify="right")
    for s in report.scored:
        table.add_row(
            s.prediction.tournament, s.prediction.team, s.prediction.market, s.outcome,
            f"{s.brier_model:.4f}" if s.brier_model is not None else "—",
            f"{s.brier_market:.4f}" if s.brier_market is not None else "—",
            f"{s.brier_pin_mid:.4f}" if s.brier_pin_mid is not None else "—",
        )
    console.print(table)

    summary = Table(title="Сводка")
    summary.add_column("Метрика")
    summary.add_column("Значение", justify="right")
    summary.add_row("N (резолвнуто)", str(report.n))
    summary.add_row(
        "Mean Brier модель",
        f"{report.mean_brier_model:.4f} (N={report.n_model})"
        if report.mean_brier_model is not None else "—",
    )
    summary.add_row(
        "Mean Brier рынок",
        f"{report.mean_brier_market:.4f} (N={report.n_market})"
        if report.mean_brier_market is not None else "—",
    )
    summary.add_row(
        "Mean Brier Pinnacle",
        f"{report.mean_brier_pin_mid:.4f} (N={report.n_pin})"
        if report.mean_brier_pin_mid is not None else "—",
    )
    summary.add_row(
        "Δ (модель − рынок, + = рынок точнее)",
        f"{report.delta_model_minus_market:+.4f}"
        if report.delta_model_minus_market is not None else "—",
    )
    summary.add_row(
        "Pinnacle range hit rate",
        f"{report.pin_range_hit_rate * 100:.1f}% (N={report.n_range_checkable})"
        if report.pin_range_hit_rate is not None else "—",
    )
    console.print(summary)

    if report.n < 30:
        warn_console.print("выборка мала, различия незначимы (N < 30)")


@main.group("autopredict")
def autopredict_group() -> None:
    """Inspect the auto-recorded predictions written by ``pull --matches``
    (see ``evhedge.auto_predict``)."""


@autopredict_group.command("status")
@click.option(
    "--db", "db_path", type=click.Path(path_type=Path), default=Path("evhedge.db"),
    show_default=True, help="Snapshot DB file.",
)
@click.option("--tournament", default=None, help="Limit to one tournament.")
def autopredict_status_command(db_path: Path, tournament: Optional[str]) -> None:
    """How much of the calibration loop is on autopilot: predictions
    auto-recorded so far (model vs market-only), and resolved markets
    that never got a prediction at all (the selection-bias check this
    module exists to drive toward zero)."""
    try:
        with Storage(db_path) as store:
            status = status_report(store, tournament=tournament)
    except StorageError as e:
        error_console.print(f"Ошибка: {e}")
        sys.exit(1)

    summary = Table(title=f"autopredict status — {tournament or 'все турниры'}")
    summary.add_column("Метрика")
    summary.add_column("Значение", justify="right")
    summary.add_row("Покрыто авто-прогнозами", str(status.n_covered))
    summary.add_row("  из них с моделью", str(status.n_model))
    summary.add_row("  из них market-only (p_model=NULL)", str(status.n_model_null))
    summary.add_row(
        "Резолвнуто БЕЗ прогноза (selection bias)",
        str(status.n_resolved_without_prediction)
        if status.n_resolved_without_prediction is not None else "—",
    )
    console.print(summary)

    if status.recent:
        table = Table(title="Последние авто-прогнозы")
        table.add_column("Турнир")
        table.add_column("Команда")
        table.add_column("Рынок")
        table.add_column("p_model", justify="right")
        table.add_column("bid/ask", justify="right")
        table.add_column("note")
        for p in status.recent:
            table.add_row(
                p.tournament, p.team, p.market,
                f"{p.p_model:.3f}" if p.p_model is not None else "—",
                f"{p.p_market_bid:.3f}/{p.p_market_ask:.3f}",
                p.note or "",
            )
        console.print(table)
    else:
        warn_console.print("Авто-прогнозов пока нет.")


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


@main.group("stageranks")
def stageranks_group() -> None:
    """auto_predict stage-rank (rounds_to_title) map maintenance."""


@stageranks_group.command("init")
@click.option(
    "--db", "db_path", type=click.Path(path_type=Path), default=Path("evhedge.db"),
    show_default=True, help="Snapshot DB to read team names from.",
)
@click.option("--tournament", required=True, help="Tournament to generate the map for.")
@click.option("--n", "rounds", type=int, required=True, help="rounds_to_title to assign every team.")
@click.option(
    "--out", type=click.Path(path_type=Path), required=True,
    help="Output YAML path (auto_predict.load_stage_ranks format).",
)
@click.option("--force", is_flag=True, default=False, help="Overwrite --out without asking.")
def stageranks_init_command(
    db_path: Path, tournament: str, rounds: int, out: Path, force: bool
) -> None:
    """Generate a stage_ranks YAML: every team seen in --tournament's
    snapshots -> --n, ready to hand-edit as the bracket progresses.

    A one-shot starting point ONLY -- meant for the moment every team is
    genuinely the same distance from the title (e.g. Ro32 of a clean
    single-elim event). Re-running this later would flatten survivors and
    eliminated teams back to the same n, which is wrong past round one --
    from then on, edit the file by hand (see auto_predict.py's module
    docstring on the pair-level n rule).
    """
    if out.exists() and not force:
        click.confirm(f"Файл {out} уже существует. Перезаписать?", abort=True)

    try:
        with Storage(db_path) as store:
            teams = store.distinct_team_names(tournament)
    except StorageError as e:
        error_console.print(f"Ошибка: {e}")
        sys.exit(1)

    if not teams:
        error_console.print(
            f"Ошибка: в {db_path} нет ни одной команды для {tournament!r} -- "
            f"сначала выполните pull"
        )
        sys.exit(1)

    lines = [f"# stage_ranks: {tournament} -- сгенерировано `evhedge stageranks init`, все команды -> n={rounds}\n"]
    for team in teams:
        lines.append(f'"{team}": {rounds}\n')
    out.write_text("".join(lines), encoding="utf-8")
    console.print(f"Записано {len(teams)} команд -> {out}")


@main.group("aliases")
def aliases_group() -> None:
    """Team name alias tools: discover discrepancies, sanity-check a
    scanner config against a snapshot DB before a live scan."""


@aliases_group.command("suggest")
@click.option(
    "--db", "db_path", type=click.Path(path_type=Path), default=Path("evhedge.db"),
    show_default=True, help="Snapshot DB to scan for name discrepancies.",
)
@click.option("--tournament", default=None, help="Limit to one tournament's names.")
def aliases_suggest_command(db_path: Path, tournament: Optional[str]) -> None:
    """Suggest candidate team-name aliases from names seen in the DB.

    Never merges anything -- only proposes candidates for
    evhedge/data/team_aliases.yaml (or a project-local override) after a
    human confirms them.
    """
    try:
        candidates = suggest_aliases(db_path, tournament)
    except StorageError as e:
        error_console.print(f"Ошибка: {e}")
        sys.exit(1)

    if not candidates:
        console.print("Кандидатов не найдено.")
        return

    table = Table(title="Кандидаты на алиасы (только предложены, не склеены)")
    table.add_column("Имя A")
    table.add_column("Имя B")
    table.add_column("Score", justify="right")
    for a, b, score in candidates:
        table.add_row(a, b, f"{score:.2f}")
    console.print(table)


@aliases_group.command("check")
@click.argument("config", type=click.Path(path_type=Path))
@click.option(
    "--db", "db_path", type=click.Path(path_type=Path), default=Path("evhedge.db"),
    show_default=True, help="Snapshot DB to check the config's team names against.",
)
def aliases_check_command(config: Path, db_path: Path) -> None:
    """Pre-flight: compare CONFIG.yaml's (canonicalized) team names
    against names already seen in --db, for the same tournament.

    Run this before a live scan (e.g. the EWC decision window) to catch
    naming gaps while there's still time to fix them, instead of finding
    out from a silently-empty join.
    """
    try:
        scanner_config = load_scanner_config(config)
    except (ConfigError, ScannerError) as e:
        error_console.print(f"Ошибка: {e}")
        sys.exit(1)

    config_names = set(scanner_config.teams)
    if scanner_config.bracket is not None:
        config_names |= bracket_teams(scanner_config.bracket)

    if not db_path.exists():
        error_console.print(f"Ошибка: файл БД {db_path} не найден")
        sys.exit(1)

    try:
        with Storage(db_path) as store:
            db_names = set(store.distinct_team_names(scanner_config.tournament))
    except StorageError as e:
        error_console.print(f"Ошибка: {e}")
        sys.exit(1)

    only_in_config = sorted(config_names - db_names)
    only_in_db = sorted(db_names - config_names)
    matched = sorted(config_names & db_names)

    table = Table(title=f"aliases check — {scanner_config.tournament}")
    table.add_column("Команда")
    table.add_column("Статус")
    for name in matched:
        table.add_row(name, "OK — есть и в конфиге, и в БД")
    for name in only_in_config:
        table.add_row(name, "нет в БД")
    for name in only_in_db:
        table.add_row(name, "нет в конфиге")
    console.print(table)

    if only_in_config or only_in_db:
        warn_console.print(
            f"Несматченных имён: {len(only_in_config)} только в конфиге, "
            f"{len(only_in_db)} только в БД -- проверьте team_aliases.yaml "
            f"или сам конфиг перед боевым сканом."
        )


if __name__ == "__main__":
    main()
