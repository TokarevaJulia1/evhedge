# Changelog

Все существенные изменения проекта фиксируются в этом файле.
Формат основан на [Keep a Changelog](https://keepachangelog.com/), проект
пока не версионируется тегами (ранняя стадия разработки, `0.1.0`).

## [Unreleased]

### Изменено
- `scanner.py`: заполнение `no_data`-дыр нейтральным 0.5 больше не
  единственный выход в отчёт — 0.5 протекал в агрегаты (deadness,
  p_stays_dead, произведение множителей FUEL CHECK), и кандидат с
  несколькими дырами получал численно правдоподобный паспорт, построенный
  на подброшенных монетках. Теперь при наличии хотя бы одной `no_data`-пары
  на пути кандидата `scan()` пересчитывает эти агрегаты с заполнением 0.2
  и 0.8 (`NO_DATA_FILL_LOW`/`NO_DATA_FILL_HIGH`) и отдаёт диапазоны
  (`deadness_range`, `p_stays_dead_range`, `available_multiplier_range`)
  рядом с точечным 0.5-значением; если вердикт FUEL CHECK не совпадает на
  всех трёх заполнениях — вердикт становится `INSUFFICIENT_DATA`. Новый
  флаг `CandidateReport.data_complete` отделяет таких кандидатов от полных
  при ранжировании. Заполнение применяется к паре в ориентации первого
  запроса — диапазон это стресс чувствительности, не строгая граница.
- `scanner.py`: поле `exit_now` удалено из `EconomicsResult` и
  `CandidateReport` — константный `0.0` в отчёте читался как «выход стоит
  ноль» и провоцировал неверные сравнения с `ev_hold`. Сканер оценивает
  кандидатов на вход, а не открытые позиции; оценка выхода вернётся вместе
  с position-трекингом (который принесёт cost basis).

### Добавлено
- `evhedge/consistency.py` (Модуль 5) — board-level проверки внутренней
  согласованности цен доски (данные Модуля 1), сегодняшние находки как
  класс сигнала. Каждый результат несёт обязательное поле
  `caveat = VERIFY_BOOK_CAVEAT` («verify book before trading») — caveat
  является частью данных, а не документацией, по PROJECT RULE из
  `data_sources/polymarket.py`. Три проверки:
  - `basket_check(markets, slots)` — корзина NO-асков доски с
    фиксированным числом слотов против гарантированной выплаты
    `(n - slots) * 100` (находка «+1.2% корзина»);
  - `identity_check(parent_market, member_markets)` — агрегатный рынок
    должен стоить как сумма взаимоисключающих членов, маппинг членов —
    руками в конфиге, автовывода нет намеренно (находка «CONCACAF=USA
    +0.6%»); DESIGN CHOICE: `IDENTITY_MIN_EDGE_PCT = 0.5` — зазор ниже
    полупункта не отличим от несвежей витрины и спреда;
  - `vertical_check(team, ladder)` — цепочка reach_X цен команды
    монотонна по глубине, условные `p_cond` правдоподобны: `p_cond >= 1`
    — жёсткое нарушение (сигнал), экстремум — мягкий флаг; DESIGN CHOICE:
    границы экстремумов `VERTICAL_EXTREME_LOW/HIGH = 0.05/0.95`.
  `tests/test_consistency.py` — фикстуры воспроизводят числа реальных
  находок (+1.2%, +0.6%).
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
