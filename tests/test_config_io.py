"""Tests for evhedge.config_io."""

from pathlib import Path

import pytest

from evhedge.config_io import ConfigError, load_full_config
from evhedge.engine import compute_ev
from evhedge.models import Bracket, MarketPrices, StrategyConfig

EXAMPLE_PATH = Path(__file__).resolve().parent.parent / "examples" / "football_example.yaml"


def test_load_full_config_from_example_file():
    bracket, market, strategy = load_full_config(EXAMPLE_PATH)

    assert isinstance(bracket, Bracket)
    assert bracket.team == "Team X"
    assert bracket.tournament == "Some Cup 2027"
    assert bracket.sport == "football"
    assert [s.name for s in bracket.stages] == [
        "1/8 финала",
        "1/4 финала",
        "1/2 финала",
        "Финал",
    ]
    assert [s.win_prob for s in bracket.stages] == [0.65, 0.55, 0.50, 0.45]
    assert bracket.stages[0].hedge_decimal_odds == pytest.approx(2.1)
    assert bracket.stages[1].hedge_decimal_odds is None
    assert bracket.stages[2].hedge_decimal_odds == pytest.approx(1.9)
    assert bracket.stages[3].hedge_decimal_odds == pytest.approx(2.6)

    assert isinstance(market, MarketPrices)
    assert market.no_price == pytest.approx(0.91)
    assert market.yes_price == pytest.approx(0.09)

    assert isinstance(strategy, StrategyConfig)
    assert strategy.name == "reinvest base20 kelly0.5"
    assert strategy.no_stake_usd == pytest.approx(1000.0)
    assert strategy.bankroll == pytest.approx(1000.0)  # null in YAML -> defaults to no_stake_usd
    assert strategy.hedge_mode == "reinvest"
    assert strategy.hedge_base_stake == pytest.approx(20.0)
    assert strategy.kelly_fraction == pytest.approx(0.5)
    assert strategy.max_hedge_stake == pytest.approx(200.0)


def test_missing_market_no_price_raises_config_error(tmp_path):
    yaml_text = """
team: "Team X"
sport: football
tournament: "Some Cup 2027"
stages:
  - name: "Final"
    win_prob: 0.5
market:
  yes_price: 0.5
strategy:
  name: "flat"
  no_stake_usd: 100
"""
    path = tmp_path / "config.yaml"
    path.write_text(yaml_text, encoding="utf-8")

    with pytest.raises(ConfigError, match="no_price"):
        load_full_config(path)


def test_missing_strategy_no_stake_usd_raises_config_error(tmp_path):
    yaml_text = """
team: "Team X"
sport: football
tournament: "Some Cup 2027"
stages:
  - name: "Final"
    win_prob: 0.5
market:
  no_price: 0.9
strategy:
  name: "flat"
"""
    path = tmp_path / "config.yaml"
    path.write_text(yaml_text, encoding="utf-8")

    with pytest.raises(ConfigError, match="no_stake_usd"):
        load_full_config(path)


def test_empty_stages_raises_config_error(tmp_path):
    yaml_text = """
team: "Team X"
sport: football
tournament: "Some Cup 2027"
stages: []
market:
  no_price: 0.9
strategy:
  name: "flat"
  no_stake_usd: 100
"""
    path = tmp_path / "config.yaml"
    path.write_text(yaml_text, encoding="utf-8")

    with pytest.raises(ConfigError, match="stages"):
        load_full_config(path)


def test_no_stake_usd_in_market_section_raises_config_error(tmp_path):
    """Regression guard: no_stake_usd/bankroll must never be accepted under
    market: -- this exact mistake has happened twice before in this
    project's history (see CHANGELOG.md)."""
    yaml_text = """
team: "Team X"
sport: football
tournament: "Some Cup 2027"
stages:
  - name: "Final"
    win_prob: 0.5
market:
  no_price: 0.9
  no_stake_usd: 100
strategy:
  name: "flat"
  no_stake_usd: 100
"""
    path = tmp_path / "config.yaml"
    path.write_text(yaml_text, encoding="utf-8")

    with pytest.raises(ConfigError, match="strategy"):
        load_full_config(path)


def test_invalid_hedge_mode_raises_config_error(tmp_path):
    yaml_text = """
team: "Team X"
sport: football
tournament: "Some Cup 2027"
stages:
  - name: "Final"
    win_prob: 0.5
market:
  no_price: 0.9
strategy:
  name: "flat"
  no_stake_usd: 100
  hedge_mode: not_a_real_mode
"""
    path = tmp_path / "config.yaml"
    path.write_text(yaml_text, encoding="utf-8")

    with pytest.raises(ConfigError, match="hedge_mode"):
        load_full_config(path)


def test_malformed_yaml_raises_config_error(tmp_path):
    path = tmp_path / "broken.yaml"
    path.write_text("team: [unclosed", encoding="utf-8")

    with pytest.raises(ConfigError, match="не удалось распарсить YAML"):
        load_full_config(path)


def test_bankroll_defaults_to_no_stake_usd_when_unset(tmp_path):
    yaml_text = """
team: "Team X"
sport: football
tournament: "Some Cup 2027"
stages:
  - name: "Final"
    win_prob: 0.5
market:
  no_price: 0.9
strategy:
  name: "flat"
  no_stake_usd: 250
"""
    path = tmp_path / "config.yaml"
    path.write_text(yaml_text, encoding="utf-8")

    _, _, strategy = load_full_config(path)
    assert strategy.bankroll == pytest.approx(250.0)


def test_end_to_end_example_config_feeds_compute_ev():
    bracket, market, strategy = load_full_config(EXAMPLE_PATH)

    result = compute_ev(bracket, market, strategy)

    assert isinstance(result.expected_value_usd, float)
    assert result.total_risk_usd > 0
