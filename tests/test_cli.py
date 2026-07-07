"""Tests for evhedge.cli using click.testing.CliRunner."""

from pathlib import Path

import pytest
from click.testing import CliRunner

from evhedge.cli import main
from evhedge.data_sources.polymarket import BookLevel, OrderBook, PolymarketAPIError

EXAMPLES_DIR = Path(__file__).resolve().parent.parent / "examples"
EXAMPLE_PATH = EXAMPLES_DIR / "football_example.yaml"
WC2026_PATH = EXAMPLES_DIR / "wc2026_bracket.yaml"


_RANK_CONFIG_TEMPLATE = """\
team: "{team}"
sport: football
tournament: "Test Cup"
stages:
  - name: "Final"
    win_prob: {win_prob}
market:
  no_price: {no_price}
strategy:
  name: "none"
  no_stake_usd: 100
  hedge_mode: none
"""


def _write_rank_config(dir_path, filename, team, win_prob, no_price):
    (dir_path / filename).write_text(
        _RANK_CONFIG_TEMPLATE.format(team=team, win_prob=win_prob, no_price=no_price),
        encoding="utf-8",
    )


def test_rank_command_prints_ordered_table(tmp_path):
    _write_rank_config(tmp_path, "team_a.yaml", "TeamA", win_prob=0.30, no_price=0.95)
    _write_rank_config(tmp_path, "team_b.yaml", "TeamB", win_prob=0.10, no_price=0.97)
    _write_rank_config(tmp_path, "team_c.yaml", "TeamC", win_prob=0.02, no_price=0.99)

    runner = CliRunner()
    result = runner.invoke(main, ["rank", str(tmp_path), "--mc", "1000", "--seed", "7"])

    assert result.exit_code == 0
    assert "TeamA" in result.output
    assert "TeamB" in result.output
    assert "TeamC" in result.output
    # Expected order (best EV first, see test_ranking.py for the math):
    # TeamC, TeamB, TeamA -- so their positions in the raw output text
    # should appear in that order.
    pos_c = result.output.index("TeamC")
    pos_b = result.output.index("TeamB")
    pos_a = result.output.index("TeamA")
    assert pos_c < pos_b < pos_a


def test_rank_command_reports_broken_configs_without_failing(tmp_path):
    _write_rank_config(tmp_path, "good.yaml", "TeamA", win_prob=0.3, no_price=0.95)
    (tmp_path / "broken.yaml").write_text("team: [unclosed", encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(main, ["rank", str(tmp_path), "--mc", "500"])

    assert result.exit_code == 0
    assert "TeamA" in result.output
    # The full path (including "broken.yaml") may be truncated by rich's
    # column width, so check for the error message text instead, which
    # confirms the failure was surfaced rather than silently dropped.
    assert "Пропущено" in result.output
    assert "не удалось распарсить YAML" in result.output


def test_rank_command_all_configs_broken_gives_clean_error(tmp_path):
    (tmp_path / "broken.yaml").write_text("team: [unclosed", encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(main, ["rank", str(tmp_path)])

    assert result.exit_code != 0
    assert "Traceback" not in result.output


def test_ev_command_prints_expected_value():
    runner = CliRunner()
    result = runner.invoke(main, ["ev", str(EXAMPLE_PATH)])

    assert result.exit_code == 0
    assert "18.62" in result.output


def test_ev_command_with_monte_carlo():
    runner = CliRunner()
    result = runner.invoke(main, ["ev", str(EXAMPLE_PATH), "--mc", "2000", "--seed", "42"])

    assert result.exit_code == 0
    assert "mean" in result.output
    assert "median" in result.output
    assert "prob_profit" in result.output


def test_ev_command_missing_file_gives_clean_error():
    runner = CliRunner()
    result = runner.invoke(main, ["ev", "does_not_exist.yaml"])

    assert result.exit_code != 0
    assert "Traceback" not in result.output
    assert "Ошибка конфигурации" in result.output


def test_ev_command_plot_without_mc_is_rejected():
    runner = CliRunner()
    result = runner.invoke(main, ["ev", str(EXAMPLE_PATH), "--plot"])

    assert result.exit_code != 0
    assert "--plot" in result.output
    assert "--mc" in result.output
    assert "Traceback" not in result.output


def test_example_then_ev_round_trip(tmp_path):
    out_path = tmp_path / "generated_example.yaml"
    runner = CliRunner()

    example_result = runner.invoke(main, ["example", "--out", str(out_path)])
    assert example_result.exit_code == 0
    assert out_path.exists()

    ev_result = runner.invoke(main, ["ev", str(out_path)])
    assert ev_result.exit_code == 0
    assert "Traceback" not in ev_result.output


def test_example_refuses_to_overwrite_without_force(tmp_path):
    out_path = tmp_path / "existing.yaml"
    out_path.write_text("placeholder", encoding="utf-8")
    runner = CliRunner()

    result = runner.invoke(main, ["example", "--out", str(out_path)], input="n\n")

    assert result.exit_code != 0
    assert out_path.read_text(encoding="utf-8") == "placeholder"


def test_example_overwrites_with_force(tmp_path):
    out_path = tmp_path / "existing.yaml"
    out_path.write_text("placeholder", encoding="utf-8")
    runner = CliRunner()

    result = runner.invoke(main, ["example", "--out", str(out_path), "--force"])

    assert result.exit_code == 0
    assert "placeholder" not in out_path.read_text(encoding="utf-8")


def test_example_unsupported_sport_gives_clean_error():
    runner = CliRunner()
    result = runner.invoke(main, ["example", "--sport", "tennis"])

    assert result.exit_code != 0
    assert "football" in result.output
    assert "Traceback" not in result.output


# --- scan ---------------------------------------------------------------------

WC2026_CANDIDATES = ("England", "Portugal", "Norway", "Morocco")


def test_scan_command_prints_all_candidates_and_caveat():
    runner = CliRunner()
    result = runner.invoke(main, ["scan", str(WC2026_PATH)])

    assert result.exit_code == 0
    for team in WC2026_CANDIDATES:
        assert team in result.output
    assert "verify book before trading" in result.output


def test_scan_command_top_limits_rows():
    runner = CliRunner()
    result = runner.invoke(main, ["scan", str(WC2026_PATH), "--top", "1"])

    assert result.exit_code == 0
    shown = sum(team in result.output for team in WC2026_CANDIDATES)
    assert shown == 1


def test_scan_command_min_outright_filters_dust():
    runner = CliRunner()
    result = runner.invoke(main, ["scan", str(WC2026_PATH), "--min-outright", "5.0"])

    assert result.exit_code == 0
    assert "Morocco" not in result.output  # 3.4% outright < 5.0
    assert "Norway" in result.output       # 5.2% outright >= 5.0


def test_scan_command_missing_file_gives_clean_error():
    runner = CliRunner()
    result = runner.invoke(main, ["scan", "does_not_exist.yaml"])

    assert result.exit_code != 0
    assert "Traceback" not in result.output
    assert "Ошибка" in result.output


# --- book ---------------------------------------------------------------------

def _fake_book():
    return OrderBook(
        token_id="tok",
        asks=[BookLevel(0.031, 8.7), BookLevel(0.05, 200.0)],
        bids=[BookLevel(0.024, 50.0)],
    )


def test_book_command_prints_levels_and_executable(monkeypatch):
    monkeypatch.setattr(
        "evhedge.cli.polymarket_ds.fetch_order_book", lambda token_id: _fake_book()
    )
    runner = CliRunner()
    result = runner.invoke(main, ["book", "tok", "--side", "buy", "--depth-to", "0.031"])

    assert result.exit_code == 0
    assert "0.031" in result.output
    # executable up to 0.031: only the first ask level -> 0.031 * 8.7 = 0.27
    assert "$0.27" in result.output


def test_book_command_depth_to_must_be_fraction(monkeypatch):
    monkeypatch.setattr(
        "evhedge.cli.polymarket_ds.fetch_order_book", lambda token_id: _fake_book()
    )
    runner = CliRunner()
    result = runner.invoke(main, ["book", "tok", "--depth-to", "5"])

    assert result.exit_code != 0
    assert "0..1" in result.output
    assert "Traceback" not in result.output


def test_book_command_api_error_gives_clean_error(monkeypatch):
    def boom(token_id):
        raise PolymarketAPIError("нет сети")

    monkeypatch.setattr("evhedge.cli.polymarket_ds.fetch_order_book", boom)
    runner = CliRunner()
    result = runner.invoke(main, ["book", "tok"])

    assert result.exit_code != 0
    assert "Traceback" not in result.output


# --- check --------------------------------------------------------------------

_BOARD_YAML = """\
board: "CLI test board"
baskets:
  - name: "winner NO"
    slots: 1
    markets: {TeamA: 62.0, TeamB: 79.4, TeamC: 83.04, TeamD: 72.0}
identities:
  - parent: {name: "CONCACAF winner", yes_pct: 9.4}
    members: {USA: 8.5, Mexico: 0.2, Canada: 0.1}
verticals:
  - team: TeamX
    ladder:
      - {stage: reach_final, yes_pct: 8.0}
      - {stage: winner, yes_pct: 8.6}
"""


def test_check_command_prints_findings_and_caveat(tmp_path):
    path = tmp_path / "board.yaml"
    path.write_text(_BOARD_YAML, encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(main, ["check", str(path)])

    assert result.exit_code == 0
    assert "+1.20%" in result.output          # the basket finding
    assert "+0.60" in result.output           # the identity finding
    assert "p_cond=1.075" in result.output    # the vertical violation
    assert "verify book before trading" in result.output


def test_check_command_missing_file_gives_clean_error():
    runner = CliRunner()
    result = runner.invoke(main, ["check", "does_not_exist.yaml"])

    assert result.exit_code != 0
    assert "Traceback" not in result.output


# --- snapshot / resolve / scan --db (Заход 2) -----------------------------------

def test_snapshot_command_records_board(tmp_path):
    db = tmp_path / "e.db"
    runner = CliRunner()
    result = runner.invoke(main, ["snapshot", str(WC2026_PATH), "--db", str(db)])

    assert result.exit_code == 0
    from evhedge.storage import Storage

    with Storage(db) as store:
        rows = store.snapshots("FIFA World Cup 2026")
        # 4 no_prices + 4 leg_prices in the example
        assert len(rows) == 8
        legs = [r for r in rows if r.market == "leg"]
        assert len(legs) == 4
        assert all(r.source == "board" for r in rows)


def test_scan_with_db_computes_velocity_hype_and_records_run(tmp_path):
    from datetime import timedelta

    from evhedge.storage import PriceSnapshot, Storage, utcnow

    db = tmp_path / "e.db"
    # Seed: an hour ago Norway's winner-NO stood at 97.0; the config says
    # 95.3 now -> velocity ~ -1.7 pp/h -> computed HYPE.
    with Storage(db) as store:
        store.record_snapshot(PriceSnapshot(
            tournament="FIFA World Cup 2026", team="Norway", market="winner_no",
            price_pct=97.0, source="board", ts_utc=utcnow() - timedelta(hours=1),
        ))

    runner = CliRunner()
    result = runner.invoke(main, ["scan", str(WC2026_PATH), "--db", str(db)])

    assert result.exit_code == 0
    assert "HYPE(v)" in result.output   # Norway, computed from velocity
    assert "HYPE(m)" in result.output   # Morocco, manual fallback (no history)
    assert "run #" in result.output

    with Storage(db) as store:
        run = store.latest_run("FIFA World Cup 2026")
        assert run is not None
        passports = store.passports(run.id)
        assert {p.team for p in passports} == set(WC2026_CANDIDATES)
        norway = next(p for p in passports if p.team == "Norway")
        assert norway.report["hype_source"] == "computed"
        # this scan's board snapshot landed too (velocity's freshest point)
        assert len(store.snapshots("FIFA World Cup 2026", team="Norway", market="winner_no")) == 2


def test_scan_without_db_stays_stateless():
    runner = CliRunner()
    result = runner.invoke(main, ["scan", str(WC2026_PATH)])

    assert result.exit_code == 0
    assert "HYPE(m)" in result.output   # manual fallback only
    assert "HYPE(v)" not in result.output
    assert "run #" not in result.output


def test_resolve_command_round_trip(tmp_path):
    db = tmp_path / "e.db"
    runner = CliRunner()
    result = runner.invoke(main, [
        "resolve", "FIFA World Cup 2026", "Morocco", "reach_final_yes", "no",
        "--note", "eliminated in QF", "--db", str(db),
    ])

    assert result.exit_code == 0
    from evhedge.storage import Storage

    with Storage(db) as store:
        (resolve,) = store.resolves("FIFA World Cup 2026", team="Morocco")
        assert resolve.outcome == "no"
        assert resolve.note == "eliminated in QF"


def test_pull_command_verify_book_reports_no_fallback(tmp_path, monkeypatch):
    """winner_no must come from a real order book, not Gamma's forced
    Yes/No complement -- the --verify-book default path."""
    import json

    event = {
        "slug": "s", "title": "t",
        "markets": [{
            "groupItemTitle": "Norway", "outcomes": json.dumps(["Yes", "No"]),
            "outcomePrices": json.dumps(["0.05", "0.95"]),
            "clobTokenIds": json.dumps(["tokY", "tokN"]),
            "volume": "5000",
        }],
    }
    monkeypatch.setattr("evhedge.collect.polymarket_ds.fetch_event_by_slug", lambda slug: event)

    books = {
        "tokY": OrderBook("tokY", bids=[BookLevel(0.04, 10)], asks=[BookLevel(0.06, 10)]),
        "tokN": OrderBook("tokN", bids=[BookLevel(0.92, 10)], asks=[BookLevel(0.96, 10)]),
    }
    monkeypatch.setattr(
        "evhedge.collect.polymarket_ds.fetch_order_book", lambda token_id: books[token_id],
    )

    db = tmp_path / "e.db"
    runner = CliRunner()
    result = runner.invoke(main, [
        "pull", "--tournament", "T", "--board", "s:winner", "--db", str(db),
    ])

    assert result.exit_code == 0
    assert "Book->board fallback" in result.output

    from evhedge.storage import Storage
    with Storage(db) as store:
        (no,) = store.snapshots("T", team="Norway", market="winner_no")
        assert no.source == "book"
        assert no.ask_pct == pytest.approx(96.0)
        (yes,) = store.snapshots("T", team="Norway", market="winner_yes")
        # independently-quoted asks, not a forced 100 - yes complement
        assert yes.ask_pct + no.ask_pct != pytest.approx(100.0)


def test_pull_command_no_verify_book_uses_board_price(tmp_path, monkeypatch):
    import json

    event = {
        "slug": "s", "title": "t",
        "markets": [{
            "groupItemTitle": "Norway", "outcomes": json.dumps(["Yes", "No"]),
            "outcomePrices": json.dumps(["0.05", "0.95"]),
            "clobTokenIds": json.dumps(["tokY", "tokN"]),
            "volume": "5000",
        }],
    }
    monkeypatch.setattr("evhedge.collect.polymarket_ds.fetch_event_by_slug", lambda slug: event)

    def fail_fetch_order_book(token_id):
        raise AssertionError("fetch_order_book must not be called with --no-verify-book")

    monkeypatch.setattr(
        "evhedge.collect.polymarket_ds.fetch_order_book", fail_fetch_order_book,
    )

    db = tmp_path / "e.db"
    runner = CliRunner()
    result = runner.invoke(main, [
        "pull", "--tournament", "T", "--board", "s:winner", "--no-verify-book", "--db", str(db),
    ])

    assert result.exit_code == 0
    from evhedge.storage import Storage
    with Storage(db) as store:
        (no,) = store.snapshots("T", team="Norway", market="winner_no")
        assert no.source == "board"
        assert no.price_pct == pytest.approx(95.0)


def test_aliases_suggest_command_prints_candidates(tmp_path):
    from datetime import datetime, timezone

    from evhedge.storage import PriceSnapshot, Storage

    db = tmp_path / "e.db"
    ts = datetime(2026, 7, 6, 12, 0, tzinfo=timezone.utc)
    with Storage(db) as store:
        store.record_snapshot(PriceSnapshot(
            tournament="T", team="Foo Gaming", market="winner_no",
            price_pct=90.0, source="board", ts_utc=ts,
        ))
        store.record_snapshot(PriceSnapshot(
            tournament="T", team="Foo", market="leg", price_pct=40.0,
            source="board", ts_utc=ts, counterparty="Bar",
        ))

    runner = CliRunner()
    result = runner.invoke(main, ["aliases", "suggest", "--db", str(db)])

    assert result.exit_code == 0
    assert "Foo Gaming" in result.output
    assert "Foo" in result.output


def test_aliases_suggest_command_no_candidates(tmp_path):
    from datetime import datetime, timezone

    from evhedge.storage import PriceSnapshot, Storage

    db = tmp_path / "e.db"
    ts = datetime(2026, 7, 6, 12, 0, tzinfo=timezone.utc)
    with Storage(db) as store:
        store.record_snapshot(PriceSnapshot(
            tournament="T", team="Zebra Squad", market="winner_no",
            price_pct=90.0, source="board", ts_utc=ts,
        ))

    runner = CliRunner()
    result = runner.invoke(main, ["aliases", "suggest", "--db", str(db)])

    assert result.exit_code == 0
    assert "Кандидатов не найдено" in result.output


def test_aliases_check_command_reports_unmatched_names(tmp_path):
    from datetime import datetime, timezone

    from evhedge.storage import PriceSnapshot, Storage

    db = tmp_path / "e.db"
    ts = datetime(2026, 7, 6, 12, 0, tzinfo=timezone.utc)
    with Storage(db) as store:
        store.record_snapshot(PriceSnapshot(
            tournament="FIFA World Cup 2026", team="Morocco", market="winner_no",
            price_pct=90.0, source="board", ts_utc=ts,
        ))
        store.record_snapshot(PriceSnapshot(
            tournament="FIFA World Cup 2026", team="Some Ghost Team", market="leg",
            price_pct=40.0, source="board", ts_utc=ts, counterparty="Morocco",
        ))

    runner = CliRunner()
    result = runner.invoke(main, ["aliases", "check", str(WC2026_PATH), "--db", str(db)])

    assert result.exit_code == 0
    assert "OK" in result.output  # Morocco matched
    assert "Some Ghost Team" in result.output  # in DB, not in config
    assert "Несматченных имён" in result.output


def test_aliases_check_command_missing_db_gives_clean_error(tmp_path):
    runner = CliRunner()
    result = runner.invoke(main, [
        "aliases", "check", str(WC2026_PATH), "--db", str(tmp_path / "does_not_exist.db"),
    ])

    assert result.exit_code != 0
    assert "Traceback" not in result.output
