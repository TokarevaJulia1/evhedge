# Changelog

Все существенные изменения проекта фиксируются в этом файле.
Формат основан на [Keep a Changelog](https://keepachangelog.com/), проект
пока не версионируется тегами (ранняя стадия разработки, `0.1.0`).

## [Unreleased]

### Добавлено
- Каркас проекта: `pyproject.toml` (Python >=3.10, зависимости numpy,
  pyyaml, click, rich, matplotlib, httpx), `requirements.txt`, `README.md`,
  структура директорий `evhedge/`, `evhedge/sports/`,
  `evhedge/data_sources/`, `tests/`.
- `evhedge/models.py` — dataclasses `Stage`, `Bracket`, `MarketPrices`,
  `StrategyConfig`, `OutcomeRow`, `EVResult` с валидацией в `__post_init__`.
- `evhedge/strategies.py` — `compute_hedge_plan(stages, strategy) ->
  list[float]`: детерминированный расчёт хедж-ставок `h_r` по стадиям для
  режимов `none` / `fixed` / `proportional` / `reinvest` / `kelly`, с клипом
  по `max_hedge_stake`.
- `evhedge/engine.py` — `compute_ev(bracket, market, strategy) -> EVResult`:
  точный (без Monte Carlo) расчёт EV NO-позиции + хедж-плана по формулам
  `profit(k)`, `profit(win)`, вероятностям путей по стадиям бракета;
  возвращает полную таблицу исходов, EV, риск, EV на доллар риска,
  дисперсию и std профита.
- `tests/test_engine.py`, `tests/test_strategies.py` — тесты на руками
  посчитанных примерах (2-раундовый бракет без хеджа и с fixed-хеджем на
  обеих стадиях) и на всех режимах `hedge_mode`.

### Изменено
- `StrategyConfig.kelly_fraction`: дефолт изменён с `1.0` (full Kelly) на
  `0.5` (half Kelly) — вероятности `p_r` являются собственной оценкой
  модели, а не рыночной котировкой, и full Kelly чрезмерно чувствителен к
  ошибке этой оценки. Риск full Kelly теперь нужно запрашивать явно
  (`kelly_fraction=1.0`).
- Границы ответственности между `MarketPrices` и `StrategyConfig`
  пересмотрены дважды:
  1. Изначально `no_stake_usd`/`bankroll` были частью `MarketPrices`.
  2. Затем перенесены в `StrategyConfig`, а `MarketPrices` сведён только к
     сырым рыночным ценам (`no_price`, `yes_price`) — чтобы в будущем
     `data_sources/polymarket.py` мог наполнять `MarketPrices` напрямую с
     биржевого API, не смешивая рыночные данные с решением о размере
     позиции.
- `compute_hedge_plan` соответственно менял сигнатуру: сначала
  `(stages, strategy)`, затем `(stages, strategy, market)` (когда
  `no_stake_usd`/`bankroll` временно жили в `MarketPrices`), и в итоге
  вернулся к `(stages, strategy)` после переноса этих полей обратно в
  `StrategyConfig`. Позже `market` добавлен обратно в четвёртый раз — на
  этот раз осознанно и навсегда: режиму `hedge_mode="lock_in"` реально
  нужна `market.no_price` для расчёта `net_no_win` на первом шаге, а не
  по ошибке, как в предыдущих итерациях.

### Пока не реализовано
- `evhedge/montecarlo.py`, `evhedge/ranking.py`, `evhedge/config_io.py`,
  `evhedge/cli.py`.
- `evhedge/sports/{football,tennis,golf,esports}.py`.
- `evhedge/data_sources/{polymarket,pinnacle}.py`.
- `tests/test_montecarlo.py`.
