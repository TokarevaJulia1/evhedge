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


# --- predict / score (calibration loop) -----------------------------------------

from datetime import datetime, timedelta, timezone  # noqa: E402

T0 = datetime(2026, 7, 6, 12, 0, tzinfo=timezone.utc)


def test_predict_command_immutable_second_call_errors(tmp_path):
    db = tmp_path / "e.db"
    runner = CliRunner()
    args = [
        "predict", "--db", str(db), "--tournament", "EWC", "--team", "Nemesis",
        "--market", "winner_no", "--model-p", "0.3",
    ]
    result1 = runner.invoke(main, args)
    assert result1.exit_code == 0

    result2 = runner.invoke(main, args)
    assert result2.exit_code != 0
    assert "Traceback" not in result2.output

    from evhedge.storage import Storage

    with Storage(db) as store:
        rows = store.predictions(tournament="EWC", team="Nemesis")
        assert len(rows) == 1
        assert rows[0].p_model == 0.3


def test_predict_command_canonicalizes_team_from_loaded_map(tmp_path):
    from evhedge.storage import Storage
    from evhedge.team_aliases import canonical_name, load_default_aliases

    # Whatever the packaged alias map resolves this to RIGHT NOW -- not
    # hardcoded, since the map is allowed to change (see team_aliases.yaml).
    expected_canon = canonical_name("Inner Circle", load_default_aliases())

    db = tmp_path / "e.db"
    runner = CliRunner()
    result = runner.invoke(main, [
        "predict", "--db", str(db), "--tournament", "EWC", "--team", "Inner Circle",
        "--market", "winner_no", "--model-p", "0.4",
    ])

    assert result.exit_code == 0
    with Storage(db) as store:
        (pred,) = store.predictions(tournament="EWC")
        assert pred.team == expected_canon


def test_predict_command_pin_uses_devig_range_directly(tmp_path, monkeypatch):
    """The CLI must call pinnacle.devig_range itself, not reimplement the
    devig arithmetic -- proven by spying on the real call and comparing
    what got stored against what the module itself returns."""
    from evhedge.data_sources.pinnacle import devig_range as real_devig_range

    calls = []

    def spy(decimal_odds):
        calls.append(list(decimal_odds))
        return real_devig_range(decimal_odds)

    monkeypatch.setattr("evhedge.cli.devig_range", spy)

    db = tmp_path / "e.db"
    runner = CliRunner()
    result = runner.invoke(main, [
        "predict", "--db", str(db), "--tournament", "T", "--team", "A", "--market", "m",
        "--pin", "1.85/1.95",
    ])

    assert result.exit_code == 0
    assert calls == [[1.85, 1.95]]

    proportional, all_margin = real_devig_range([1.85, 1.95])
    expected_low = min(proportional[0], all_margin[0])
    expected_high = max(proportional[0], all_margin[0])

    from evhedge.storage import Storage

    with Storage(db) as store:
        (pred,) = store.predictions(tournament="T")
        assert abs(pred.p_pin_low - expected_low) < 1e-9
        assert abs(pred.p_pin_high - expected_high) < 1e-9


def test_predict_command_poly_manual_bid_ask(tmp_path):
    db = tmp_path / "e.db"
    runner = CliRunner()
    result = runner.invoke(main, [
        "predict", "--db", str(db), "--tournament", "T", "--team", "A", "--market", "m",
        "--poly", "0.21/0.24",
    ])

    assert result.exit_code == 0
    from evhedge.storage import Storage

    with Storage(db) as store:
        (pred,) = store.predictions(tournament="T")
        assert pred.p_market_bid == 0.21
        assert pred.p_market_ask == 0.24


def test_score_command_france_morocco_fixture_brier(tmp_path):
    """Real fixture: World Cup 2026, France-Morocco (09.07.2026).
    p_model=0.232 (Morocco advances), Polymarket YES book 0.212/0.224
    (mid 0.218), outcome=no. Brier model 0.232^2=0.053824, Brier market
    0.218^2=0.047524, delta (model-market)=+0.0063 -- market beat model."""
    from evhedge.storage import Prediction, Resolve, Storage

    db = tmp_path / "e.db"
    with Storage(db) as store:
        store.record_prediction(Prediction(
            tournament="FIFA World Cup 2026", team="Morocco", market="advance_no",
            ts_utc=T0, p_model=0.232, p_market_bid=0.212, p_market_ask=0.224,
        ))
        store.record_resolve(Resolve(
            tournament="FIFA World Cup 2026", team="Morocco", market="advance_no",
            outcome="no", ts_utc=T0 + timedelta(hours=1),
        ))

        report = store.score_predictions(tournament="FIFA World Cup 2026")

    (scored,) = report.scored
    assert abs(scored.brier_model - 0.053824) < 1e-6
    assert abs(scored.brier_market - 0.047524) < 1e-6
    assert abs(report.delta_model_minus_market - 0.0063) < 1e-6

    runner = CliRunner()
    result = runner.invoke(main, [
        "score", "--db", str(db), "--tournament", "FIFA World Cup 2026",
    ])
    assert result.exit_code == 0
    assert "0.0538" in result.output
    assert "0.0475" in result.output


def test_score_command_synthetic_aggregate_hand_computed_means(tmp_path):
    from evhedge.storage import Prediction, Resolve, Storage

    db = tmp_path / "e.db"
    with Storage(db) as store:
        # brier_model=(0.5-1)^2=0.25,    market mid=0.45, brier_market=(0.45-1)^2=0.3025
        store.record_prediction(Prediction(
            tournament="T", team="A", market="m", ts_utc=T0,
            p_model=0.5, p_market_bid=0.4, p_market_ask=0.5,
        ))
        store.record_resolve(Resolve(tournament="T", team="A", market="m", outcome="yes", ts_utc=T0))

        # brier_model=(0.2-0)^2=0.04,    market mid=0.2,  brier_market=(0.2-0)^2=0.04
        store.record_prediction(Prediction(
            tournament="T", team="B", market="m", ts_utc=T0,
            p_model=0.2, p_market_bid=0.1, p_market_ask=0.3,
        ))
        store.record_resolve(Resolve(tournament="T", team="B", market="m", outcome="no", ts_utc=T0))

        # brier_model=(0.9-1)^2=0.01,    market mid=0.85, brier_market=(0.85-1)^2=0.0225
        store.record_prediction(Prediction(
            tournament="T", team="C", market="m", ts_utc=T0,
            p_model=0.9, p_market_bid=0.8, p_market_ask=0.9,
        ))
        store.record_resolve(Resolve(tournament="T", team="C", market="m", outcome="yes", ts_utc=T0))

        report = store.score_predictions(tournament="T")

    mean_model = (0.25 + 0.04 + 0.01) / 3
    mean_market = (0.3025 + 0.04 + 0.0225) / 3

    assert report.n == 3
    assert abs(report.mean_brier_model - mean_model) < 1e-9
    assert abs(report.mean_brier_market - mean_market) < 1e-9
    assert abs(report.delta_model_minus_market - (mean_model - mean_market)) < 1e-9


def test_score_command_pending_excluded_from_metrics(tmp_path):
    from evhedge.storage import Prediction, Resolve, Storage

    db = tmp_path / "e.db"
    with Storage(db) as store:
        store.record_prediction(Prediction(
            tournament="T", team="Resolved", market="m", ts_utc=T0,
            p_model=0.5, p_market_bid=0.4, p_market_ask=0.5,
        ))
        store.record_resolve(Resolve(tournament="T", team="Resolved", market="m", outcome="yes", ts_utc=T0))

        store.record_prediction(Prediction(
            tournament="T", team="StillPending", market="m2", ts_utc=T0, p_model=0.3,
        ))

    runner = CliRunner()
    result = runner.invoke(main, ["score", "--db", str(db), "--tournament", "T"])

    assert result.exit_code == 0
    assert "PENDING" in result.output
    assert "StillPending" in result.output

    from evhedge.storage import Storage

    with Storage(db) as store:
        report = store.score_predictions(tournament="T")
        assert report.n == 1
        assert len(report.pending) == 1
        assert report.pending[0].team == "StillPending"
    assert "Traceback" not in result.output
