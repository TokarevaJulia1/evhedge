"""YAML config loading into evhedge.models objects.

Sport-agnostic: the ``sport`` field is stored on ``Bracket.sport`` as plain
metadata. There is no branching on sport here — sport-specific bracket
construction (stage templates, etc.) lives in ``evhedge.sports.*``, not
here.

Two ways to load a config:

- ``load_full_config`` reads one YAML file with ``team``/``sport``/
  ``tournament``/``stages`` at the top level plus nested ``market:`` and
  ``strategy:`` sections, and returns all three objects.
- ``load_bracket_yaml`` / ``load_market_yaml`` / ``load_strategy_yaml``
  read a single-purpose file (just the bracket fields, just the market
  fields, or just the strategy fields, at the top level) and return one
  object each — e.g. so the same ``market.yaml`` can be reused across
  several teams' bracket files.

Both paths share the same per-section parsing helpers, so a given chunk of
YAML produces the same object whether it arrived embedded in a full config
or as a standalone file.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Union

import yaml

from evhedge.models import Bracket, MarketPrices, Stage, StrategyConfig

PathLike = Union[str, Path]


class ConfigError(Exception):
    """Raised for any problem loading or validating a YAML config file:
    malformed YAML, missing required fields, fields in the wrong section,
    or values rejected by the underlying models.py validation."""


def _read_yaml_file(path: Path) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError as e:
        raise ConfigError(f"не удалось распарсить YAML в файле {path}: {e}") from e
    except OSError as e:
        raise ConfigError(f"не удалось прочитать файл {path}: {e}") from e

    if not isinstance(data, dict):
        raise ConfigError(
            f"{path}: ожидался YAML-документ верхнего уровня в виде словаря (mapping), "
            f"получено {type(data).__name__}"
        )
    return data


def _require(d: dict, key: str, path: Path, prefix: str = "") -> Any:
    if key not in d or d[key] is None:
        raise ConfigError(f"{path}: отсутствует обязательное поле {prefix + key!r}")
    return d[key]


def _parse_bracket(data: dict, path: Path) -> Bracket:
    team = _require(data, "team", path)
    tournament = _require(data, "tournament", path)
    sport = _require(data, "sport", path)
    stages_raw = _require(data, "stages", path)

    if not isinstance(stages_raw, list) or len(stages_raw) == 0:
        raise ConfigError(f"{path}: поле 'stages' должно быть непустым списком этапов")

    stages: list[Stage] = []
    for i, item in enumerate(stages_raw):
        prefix = f"stages[{i}]."
        name = _require(item, "name", path, prefix=prefix)
        win_prob = _require(item, "win_prob", path, prefix=prefix)
        hedge_decimal_odds = item.get("hedge_decimal_odds")
        try:
            stages.append(
                Stage(name=name, win_prob=win_prob, hedge_decimal_odds=hedge_decimal_odds)
            )
        except ValueError as e:
            raise ConfigError(f"{path}: некорректный этап stages[{i}] ({name!r}): {e}") from e

    try:
        return Bracket(team=team, tournament=tournament, sport=sport, stages=stages)
    except ValueError as e:
        raise ConfigError(f"{path}: {e}") from e


def _parse_market(data: dict, path: Path) -> MarketPrices:
    if "no_stake_usd" in data or "bankroll" in data:
        raise ConfigError(
            f"{path}: no_stake_usd/bankroll принадлежат секции strategy, а не market "
            f"(см. README, раздел «Идея»)"
        )

    no_price = _require(data, "no_price", path, prefix="market.")
    yes_price = data.get("yes_price")
    if yes_price is None:
        # yes_price is optional in YAML even though MarketPrices.yes_price
        # is a required field -- default to the complementary probability.
        yes_price = 1.0 - no_price

    try:
        return MarketPrices(no_price=no_price, yes_price=yes_price)
    except ValueError as e:
        raise ConfigError(f"{path}: {e}") from e


def _parse_strategy(data: dict, path: Path) -> StrategyConfig:
    name = _require(data, "name", path, prefix="strategy.")
    no_stake_usd = _require(data, "no_stake_usd", path, prefix="strategy.")

    optional_kwargs = {}
    for key in ("bankroll", "hedge_mode", "hedge_base_stake", "kelly_fraction", "max_hedge_stake"):
        if data.get(key) is not None:
            optional_kwargs[key] = data[key]

    try:
        return StrategyConfig(name=name, no_stake_usd=no_stake_usd, **optional_kwargs)
    except ValueError as e:
        raise ConfigError(f"{path}: {e}") from e


def load_full_config(path: PathLike) -> tuple[Bracket, MarketPrices, StrategyConfig]:
    """Load a single YAML file containing the bracket, market, and strategy
    sections together (see module docstring for the layout).

    Args:
        path: Path to the YAML config file.

    Returns:
        ``(bracket, market, strategy)``.

    Raises:
        ConfigError: On malformed YAML, missing required top-level fields
            (``team``, ``sport``, ``tournament``, ``stages``, ``market``,
            ``strategy``), missing required fields within a section, or
            values rejected by ``models.py`` validation.
    """
    path = Path(path)
    data = _read_yaml_file(path)

    for key in ("team", "sport", "tournament", "stages", "market", "strategy"):
        if key not in data or data[key] is None:
            raise ConfigError(f"{path}: отсутствует обязательное поле {key!r}")

    market_section = data["market"]
    if not isinstance(market_section, dict):
        raise ConfigError(f"{path}: секция 'market' должна быть словарём")

    strategy_section = data["strategy"]
    if not isinstance(strategy_section, dict):
        raise ConfigError(f"{path}: секция 'strategy' должна быть словарём")

    bracket = _parse_bracket(data, path)
    market = _parse_market(market_section, path)
    strategy = _parse_strategy(strategy_section, path)
    return bracket, market, strategy


def load_bracket_yaml(path: PathLike) -> Bracket:
    """Load a standalone bracket YAML file (``team``/``sport``/
    ``tournament``/``stages`` at the top level, no ``market``/``strategy``
    sections)."""
    path = Path(path)
    data = _read_yaml_file(path)
    return _parse_bracket(data, path)


def load_market_yaml(path: PathLike) -> MarketPrices:
    """Load a standalone market YAML file (``no_price``/``yes_price`` at
    the top level). Useful for sharing one market snapshot across several
    teams' bracket files."""
    path = Path(path)
    data = _read_yaml_file(path)
    return _parse_market(data, path)


def load_strategy_yaml(path: PathLike) -> StrategyConfig:
    """Load a standalone strategy YAML file (``name``/``no_stake_usd``/...
    at the top level)."""
    path = Path(path)
    data = _read_yaml_file(path)
    return _parse_strategy(data, path)
