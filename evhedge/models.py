"""Data model for evhedge.

This module defines the pure data structures shared across the package:
a tournament bracket broken into sequential stages, the market prices used
to size the primary ("NO") position, the hedging strategy configuration,
and the result types produced by the EV engine.

No business logic (EV math, hedge sizing, simulation) lives here — see
``evhedge.strategies``, ``evhedge.engine`` and ``evhedge.montecarlo``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Stage:
    """One round of a single-elimination (or group-to-knockout) bracket
    for a specific team/player.

    Attributes:
        name: Human readable stage label, e.g. "Round of 16", "Quarterfinal".
        win_prob: Conditional probability that the team wins/advances this
            stage, *given* that they reached it. Must be in (0, 1].
        hedge_decimal_odds: Decimal odds (European, e.g. 2.50) at which a
            hedge bet against the team (i.e. betting the team is eliminated
            at this stage) can be placed. ``None`` if no hedge market is
            configured/available for this stage.
    """

    name: str
    win_prob: float
    hedge_decimal_odds: Optional[float] = None

    def __post_init__(self) -> None:
        if not (0.0 < self.win_prob <= 1.0):
            raise ValueError(
                f"Stage({self.name!r}).win_prob must be in (0, 1], got {self.win_prob}"
            )
        if self.hedge_decimal_odds is not None and self.hedge_decimal_odds <= 1.0:
            raise ValueError(
                f"Stage({self.name!r}).hedge_decimal_odds must be > 1.0, "
                f"got {self.hedge_decimal_odds}"
            )


@dataclass
class Bracket:
    """A team/player's path through a tournament, expressed as an ordered
    sequence of stages that must all be won to take the title.

    Attributes:
        team: Team or player name.
        sport: Sport/competition identifier, e.g. "football", "tennis".
        stages: Ordered list of stages from the team's next match through
            the final. Order matters — ``stages[0]`` is resolved first.
        outright_decimal_odds: Optional sportsbook decimal odds for the
            team to win the whole tournament, kept for reference/sanity
            checks against the implied probability of ``stages``.
    """

    team: str
    sport: str
    stages: list[Stage] = field(default_factory=list)
    outright_decimal_odds: Optional[float] = None

    def __post_init__(self) -> None:
        if not self.stages:
            raise ValueError(f"Bracket({self.team!r}) must have at least one stage")
        if self.outright_decimal_odds is not None and self.outright_decimal_odds <= 1.0:
            raise ValueError(
                f"Bracket({self.team!r}).outright_decimal_odds must be > 1.0, "
                f"got {self.outright_decimal_odds}"
            )

    @property
    def title_prob(self) -> float:
        """Probability of winning the whole tournament: the product of
        every stage's conditional ``win_prob``."""
        result = 1.0
        for stage in self.stages:
            result *= stage.win_prob
        return result


@dataclass
class MarketPrices:
    """Raw market prices for the "team wins the tournament outright" market
    (e.g. a Polymarket outright), independent of how much we choose to bet.

    Deliberately holds *only* market data — no position sizing (that lives
    on ``StrategyConfig``) — so this can be populated directly from an
    exchange API (see ``evhedge.data_sources.polymarket``) without mixing in
    the separate "how much do we stake" decision.

    Attributes:
        no_price: Price of the NO share, in (0, 1). This is the amount of
            USD paid per share that redeems for $1 if the team fails to
            win the tournament.
        yes_price: Price of the YES share, in (0, 1). This is the amount of
            USD paid per share that redeems for $1 if the team wins the
            tournament.
    """

    no_price: float
    yes_price: float

    def __post_init__(self) -> None:
        if not (0.0 < self.no_price < 1.0):
            raise ValueError(f"MarketPrices.no_price must be in (0, 1), got {self.no_price}")
        if not (0.0 < self.yes_price < 1.0):
            raise ValueError(f"MarketPrices.yes_price must be in (0, 1), got {self.yes_price}")


#: Valid values for ``StrategyConfig.hedge_mode``.
HEDGE_MODES = ("none", "fixed", "proportional", "reinvest", "kelly")


@dataclass
class StrategyConfig:
    """Configuration for how hedge stakes ``h_r`` are sized on each stage
    of a bracket. See ``evhedge.strategies.compute_hedge_plan`` for the
    exact per-mode formulas; this class only holds the parameters.

    Attributes:
        name: Strategy identifier, e.g. "flat_10", "kelly_half".
        no_stake_usd: USD amount staked on the primary NO position. This is
            our own position-sizing decision, not market data, which is why
            it lives here rather than on ``MarketPrices``.
        bankroll: Total USD bankroll available for hedging. Defaults to
            ``no_stake_usd`` if not supplied.
        hedge_mode: One of:
            - "none": never hedge, pure NO position.
            - "fixed": ``h_r = hedge_base_stake`` (USD) on every stage
              that has ``hedge_decimal_odds`` set.
            - "proportional": ``hedge_base_stake`` is read as a *fraction*
              (0..1) of ``no_stake_usd``, i.e.
              ``h_r = hedge_base_stake * no_stake_usd``.
            - "reinvest": ``h_r = hedge_base_stake + cum_hedge_profit *
              kelly_fraction``, where ``cum_hedge_profit`` is the USD
              profit accumulated from hedge stakes won on stages before
              ``r`` (deterministic, since the path to stage ``r`` is
              unique). Here ``kelly_fraction`` is the *reinvested share
              of accumulated hedge profit*, not a classical Kelly
              fraction.
            - "kelly": classical fractional Kelly sizing per stage,
              ``f* = (win_prob * odds - 1) / (odds - 1)``,
              ``h_r = clip(bankroll * f* * kelly_fraction, 0,
              max_hedge_stake)``, using ``bankroll`` above.
        hedge_base_stake: Base stake parameter, interpreted per
            ``hedge_mode`` as described above. Must be >= 0.
        kelly_fraction: Fraction of full Kelly stake ("kelly" mode) or
            reinvested share of accumulated hedge profit ("reinvest"
            mode). Must be > 0. Defaults to 0.5 (half-Kelly): full Kelly
            (1.0) maximizes theoretical long-run growth but is highly
            sensitive to errors in the win-probability estimates ``p_r``,
            which are our own model output, not a market-quoted price —
            so they carry estimation error. Taking on that extra risk
            should be an explicit choice (pass ``kelly_fraction=1.0``),
            not the default.
        max_hedge_stake: Optional USD cap applied to every computed hedge
            stake, regardless of mode. Must be > 0 if set.
    """

    name: str
    no_stake_usd: float
    bankroll: Optional[float] = None
    hedge_mode: str = "none"
    hedge_base_stake: float = 0.0
    kelly_fraction: float = 0.5
    max_hedge_stake: Optional[float] = None

    def __post_init__(self) -> None:
        if self.no_stake_usd <= 0.0:
            raise ValueError(
                f"StrategyConfig({self.name!r}).no_stake_usd must be > 0, "
                f"got {self.no_stake_usd}"
            )
        if self.bankroll is None:
            self.bankroll = self.no_stake_usd
        elif self.bankroll <= 0.0:
            raise ValueError(
                f"StrategyConfig({self.name!r}).bankroll must be > 0, got {self.bankroll}"
            )
        if self.hedge_mode not in HEDGE_MODES:
            raise ValueError(
                f"StrategyConfig({self.name!r}).hedge_mode must be one of "
                f"{HEDGE_MODES}, got {self.hedge_mode!r}"
            )
        if self.hedge_base_stake < 0.0:
            raise ValueError(
                f"StrategyConfig({self.name!r}).hedge_base_stake must be >= 0, "
                f"got {self.hedge_base_stake}"
            )
        if self.kelly_fraction <= 0.0:
            raise ValueError(
                f"StrategyConfig({self.name!r}).kelly_fraction must be > 0, "
                f"got {self.kelly_fraction}"
            )
        if self.max_hedge_stake is not None and self.max_hedge_stake <= 0.0:
            raise ValueError(
                f"StrategyConfig({self.name!r}).max_hedge_stake must be > 0, "
                f"got {self.max_hedge_stake}"
            )


@dataclass
class OutcomeRow:
    """One row of the discretized outcome table produced by the EV engine:
    a single mutually-exclusive scenario (team eliminated at stage *k*, or
    team wins the whole tournament) together with its probability and net
    profit.

    Attributes:
        scenario: Human readable label, e.g. "Eliminated at Quarterfinal"
            or "Wins tournament".
        stage_index: Index into ``Bracket.stages`` at which the team is
            eliminated, or ``None`` if this row represents the team
            winning the whole tournament.
        probability: Probability of this exact scenario occurring.
        profit_usd: Net profit (can be negative) realized under this
            scenario, accounting for the NO stake and all hedge stakes.
    """

    scenario: str
    stage_index: Optional[int]
    probability: float
    profit_usd: float

    def __post_init__(self) -> None:
        if not (0.0 <= self.probability <= 1.0):
            raise ValueError(
                f"OutcomeRow({self.scenario!r}).probability must be in [0, 1], "
                f"got {self.probability}"
            )


@dataclass
class EVResult:
    """Aggregated output of ``evhedge.engine.compute_ev``.

    Attributes:
        team: Team/player the result applies to.
        expected_value_usd: Probability-weighted net profit across all
            outcome rows.
        total_risk_usd: Total USD capital committed (NO stake + all hedge
            stakes across stages).
        ev_per_dollar_risk: ``expected_value_usd / total_risk_usd``, used
            to rank opportunities of different sizes.
        outcome_rows: The full discretized outcome table backing the EV
            calculation.
        variance_usd: Probability-weighted variance of profit, if
            computed.
        std_dev_usd: Square root of ``variance_usd``, if computed.
    """

    team: str
    expected_value_usd: float
    total_risk_usd: float
    ev_per_dollar_risk: float
    outcome_rows: list[OutcomeRow] = field(default_factory=list)
    variance_usd: Optional[float] = None
    std_dev_usd: Optional[float] = None

    def __post_init__(self) -> None:
        if self.total_risk_usd < 0.0:
            raise ValueError(
                f"EVResult({self.team!r}).total_risk_usd must be >= 0, "
                f"got {self.total_risk_usd}"
            )
