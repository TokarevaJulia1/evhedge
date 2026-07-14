"""Tests for evhedge.auto_predict (the book-quality trigger and the
model-probability inputs it feeds)."""

from datetime import datetime, timezone

import pytest

from evhedge.auto_predict import (
    MODEL_VERSION,
    book_quality_trigger,
    compute_model_probability,
    format_note,
    load_stage_ranks,
    result_market_label,
    status_report,
)
from evhedge.config_io import ConfigError
from evhedge.storage import Prediction, PriceSnapshot, Resolve, Storage

T0 = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)


# --- book_quality_trigger --------------------------------------------------------

def test_trigger_rejects_board_source():
    assert book_quality_trigger("board", 40.0, 42.0) is False


def test_trigger_rejects_missing_side():
    assert book_quality_trigger("book", None, 42.0) is False
    assert book_quality_trigger("book", 40.0, None) is False


def test_trigger_rejects_wide_spread():
    assert book_quality_trigger("book", 40.0, 46.0) is False  # 6pp


def test_trigger_accepts_tight_spread():
    assert book_quality_trigger("book", 59.0, 60.5) is True  # 1.5pp


def test_trigger_boundary_exactly_5pp_fires():
    assert book_quality_trigger("book", 40.0, 45.0) is True  # exactly 5pp


def test_trigger_rejects_listing_placeholder():
    """The real EWC case that motivated the trigger: a freshly-listed
    book at 4.0/96.0 -- technically both sides present, but nowhere near
    tradable."""
    assert book_quality_trigger("book", 4.0, 96.0) is False


# --- load_stage_ranks -------------------------------------------------------------

def test_load_stage_ranks_flat_map(tmp_path):
    path = tmp_path / "ranks.yaml"
    path.write_text("Team Falcons: 2\nBetBoom Team: 2\n", encoding="utf-8")
    assert load_stage_ranks(path) == {"Team Falcons": 2, "BetBoom Team": 2}


def test_load_stage_ranks_rejects_non_int(tmp_path):
    path = tmp_path / "ranks.yaml"
    path.write_text("Team Falcons: not_a_number\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="must be an int"):
        load_stage_ranks(path)


def test_load_stage_ranks_rejects_non_positive(tmp_path):
    path = tmp_path / "ranks.yaml"
    path.write_text("Team Falcons: 0\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="positive"):
        load_stage_ranks(path)


# --- compute_model_probability -----------------------------------------------------

def _book_snap(team, bid_pct, ask_pct, ts=T0, tournament="EWC"):
    return PriceSnapshot(
        tournament=tournament, team=team, market="winner_yes",
        price_pct=ask_pct, bid_pct=bid_pct, ask_pct=ask_pct,
        source="book", ts_utc=ts,
    )


def test_model_probability_none_without_stage_ranks(tmp_path):
    with Storage(tmp_path / "e.db") as store:
        store.record_snapshot(_book_snap("A", 20.0, 22.0))
        store.record_snapshot(_book_snap("B", 10.0, 12.0))
        p, ts, n = compute_model_probability(store, "EWC", "A", "B", None)
        assert (p, ts, n) == (None, None, None)


def test_model_probability_none_on_heterogeneous_n(tmp_path):
    with Storage(tmp_path / "e.db") as store:
        store.record_snapshot(_book_snap("A", 20.0, 22.0))
        store.record_snapshot(_book_snap("B", 10.0, 12.0))
        p, ts, n = compute_model_probability(store, "EWC", "A", "B", {"A": 2, "B": 3})
        assert (p, ts, n) == (None, None, None)


def test_model_probability_none_on_missing_team_in_map(tmp_path):
    with Storage(tmp_path / "e.db") as store:
        store.record_snapshot(_book_snap("A", 20.0, 22.0))
        store.record_snapshot(_book_snap("B", 10.0, 12.0))
        p, ts, n = compute_model_probability(store, "EWC", "A", "B", {"A": 2})
        assert (p, ts, n) == (None, None, None)


def test_model_probability_none_on_mismatched_winner_book_ts(tmp_path):
    from datetime import timedelta

    with Storage(tmp_path / "e.db") as store:
        store.record_snapshot(_book_snap("A", 20.0, 22.0, ts=T0))
        store.record_snapshot(_book_snap("B", 10.0, 12.0, ts=T0 + timedelta(hours=1)))
        p, ts, n = compute_model_probability(store, "EWC", "A", "B", {"A": 2, "B": 2})
        assert (p, ts, n) == (None, None, None)


def test_model_probability_none_without_book_verified_winner_snapshot(tmp_path):
    """A board (not book) winner_yes snapshot doesn't count -- see the
    module's 'winner-book mids' requirement."""
    with Storage(tmp_path / "e.db") as store:
        store.record_snapshot(PriceSnapshot(
            tournament="EWC", team="A", market="winner_yes", price_pct=20.0,
            source="board", ts_utc=T0,
        ))
        store.record_snapshot(_book_snap("B", 10.0, 12.0))
        p, ts, n = compute_model_probability(store, "EWC", "A", "B", {"A": 2, "B": 2})
        assert (p, ts, n) == (None, None, None)


def test_model_probability_real_world_cup_semifinal_fixture(tmp_path):
    """Real fixture from the board of 14.07: France 38.9 / Spain 21.1,
    n=2 -> strength 0.6237/0.4593 -> pair_prob=0.638 (1e-3) -- the ~4.3pp
    gap against the market's 59.5 this module exists to accumulate."""
    with Storage(tmp_path / "e.db") as store:
        store.record_snapshot(_book_snap("France", 38.0, 39.8, tournament="FIFA World Cup 2026"))
        store.record_snapshot(_book_snap("Spain", 20.6, 21.6, tournament="FIFA World Cup 2026"))
        p, ts, n = compute_model_probability(
            store, "FIFA World Cup 2026", "France", "Spain", {"France": 2, "Spain": 2},
        )
        assert p == pytest.approx(0.638, abs=1e-3)
        assert n == 2
        assert ts is not None


# --- format_note --------------------------------------------------------------------

def test_format_note_with_model_contains_version_and_fields():
    note = format_note(T0, 2)
    assert f"model={MODEL_VERSION}" in note
    assert "board_ts=" in note
    assert "n=2" in note


def test_format_note_null_model_still_contains_version():
    note = format_note(None, None)
    assert f"model={MODEL_VERSION}" in note
    assert "p_model=NULL" in note


# --- result_market_label -------------------------------------------------------------

def test_result_market_label_format():
    event = {"slug": "dota2-flc-bb4-2026-07-07"}
    market = {"groupItemTitle": "Match Winner"}
    assert result_market_label(event, market) == "result:dota2-flc-bb4-2026-07-07:Match Winner"


# --- factual check: pair-level n uniformity on a real bracket ------------------------

def test_uniform_n_within_bracket_pairs():
    """power_model.py's own caveat is about non-uniform n across a
    non-uniform BRACKET (some teams enter deeper than others); the claim
    this module leans on is narrower -- within one PAIR playing each
    other right now, n is always equal, even on a real bracket that is
    non-uniform overall. Checked against the real (documented-demo-
    provenance) examples/wc2026_bracket.yaml: 4 pending Round-of-16 pairs
    share n=4 each, while a team already through to the quarterfinal
    (Brazil) has a different n=3 -- exactly the shape the DESIGN CHOICE
    in the module docstring claims."""
    from evhedge.scanner import load_scanner_config, rounds_to_title

    config = load_scanner_config("examples/wc2026_bracket.yaml")
    pairs = [("Spain", "Uruguay"), ("England", "Senegal"),
             ("Argentina", "Mexico"), ("Portugal", "Japan")]
    for a, b in pairs:
        n_a = rounds_to_title(config.bracket, a)
        n_b = rounds_to_title(config.bracket, b)
        assert n_a == n_b == 4

    # a team already past this round has a DIFFERENT n -- proves the
    # restriction must be checked per-pair, not assumed tournament-wide.
    assert rounds_to_title(config.bracket, "Brazil") == 3


# --- status_report --------------------------------------------------------------------

def test_status_report_counts_and_selection_bias(tmp_path):
    with Storage(tmp_path / "e.db") as store:
        store.record_prediction(Prediction(
            tournament="EWC", team="A", market="m1", ts_utc=T0,
            p_model=0.6, p_market_bid=0.5, p_market_ask=0.52,
            note=format_note(T0, 2),
        ))
        store.record_prediction(Prediction(
            tournament="EWC", team="B", market="m2", ts_utc=T0,
            p_market_bid=0.5, p_market_ask=0.52,
            note=format_note(None, None),
        ))
        # manual (non-auto) prediction -- must not count as "covered"
        store.record_prediction(Prediction(
            tournament="EWC", team="C", market="m3", ts_utc=T0,
            p_market_bid=0.4, p_market_ask=0.42, note="manual entry",
        ))
        # a resolve with NO prediction at all -- the selection-bias gap
        store.record_resolve(Resolve(
            tournament="EWC", team="D", market="m4", outcome="no", ts_utc=T0,
        ))

        status = status_report(store, tournament="EWC")
        assert status.n_covered == 2
        assert status.n_model == 1
        assert status.n_model_null == 1
        assert status.n_resolved_without_prediction == 1
        assert len(status.recent) == 2
