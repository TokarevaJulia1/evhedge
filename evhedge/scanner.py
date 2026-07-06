"""Long-shot bracket scanner: for every team priced as a long shot on the
outright market, build their remaining path to a target milestone (reach
the final / win it all), score how "dead" (hard) or "live" (easy) that path
is, and check whether the hedge economics can actually work given real leg
prices -- not just whether the story sounds good.

Manual YAML input for now (``load_scanner_config``); auto-parsing a bracket
from a live Polymarket tag is a future extension point, not implemented
here -- ``scan()`` takes an already-built ``ScannerConfig`` so a future
``parse_scanner_config_from_polymarket(...)`` can produce the same object
without touching this module.

READ THIS BEFORE TRUSTING THE OUTPUT -- several places in the design spec
for this module referred to prior context (specific past incidents/lessons)
this implementation doesn't have direct access to. Where the spec didn't
give an exact formula/threshold, a concrete, documented choice was made
instead of guessing silently. Every one of these is called out inline with
"DESIGN CHOICE:" and summarized again in the module's parent commit
message -- if any of them don't match what was actually meant, they're
narrow, single-function changes to fix.

Stage-type rules
----------------
- ``round_robin``: does not produce eliminations in the path model (except
  explicitly marked knockout slots, which aren't modeled here -- only the
  survivors entering the bracket matter). Never hedge-suitable for Bo2
  (draws break clean resolution) -- excluded from the roll chain and
  listed in ``CandidateReport.excluded_stages`` instead of being silently
  dropped.
- ``single_elim`` / ``gauntlet``: both are ordinary Stage chains -- a
  survival gauntlet is structurally "beat this opponent or go home", same
  as a single-elimination round, so both are handled identically by the
  bracket-tree model below.
- If ANY stage in ``stages_meta`` is ``round_robin``, the power model is
  disabled for the WHOLE tournament (DESIGN CHOICE: "heterogeneous stages"
  is read as "round_robin mixed with elimination stages", since round_robin
  is the type explicitly called out as breaking a clean rounds-to-title
  count -- a mix of single_elim + gauntlet only is still treated as
  homogeneous). With the model disabled, every pairwise probability must
  come from ``leg_prices``; gaps are tagged "no_data" and filled with a
  neutral 0.5 split ONLY so the arithmetic can proceed, never with a model
  estimate -- ``sources_breakdown`` reports exactly how many of them there
  were.

"no_data" gaps and aggregate metrics
------------------------------------
A neutral 0.5 point-fill leaks into aggregates (deadness, p_stays_dead,
the FUEL CHECK multiplier product) and produces a numerically plausible
passport built on coin flips. So whenever a candidate's path contains at
least one "no_data" pair, ``scan()`` re-computes those aggregates twice
more with the gaps filled at ``NO_DATA_FILL_LOW``/``NO_DATA_FILL_HIGH``
(0.2 / 0.8) and reports each affected metric as a (low, high) RANGE
alongside the 0.5-point value; if the FUEL CHECK verdict is not the same
at all three fills, the verdict becomes ``INSUFFICIENT_DATA`` -- the
candidate must not be ranked alongside data-complete ones (see
``CandidateReport.data_complete``). The fill is applied to the pair in
the orientation it is first queried, so the band is a sensitivity stress,
not a rigorous bound -- an honest "don't know", which beats a plausible
number.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Union

from evhedge.config_io import ConfigError, _read_yaml_file
from evhedge.data_sources import polymarket as polymarket_ds
from evhedge.data_sources.polymarket import PolymarketAPIError
from evhedge.engine import compute_ev
from evhedge.models import Bracket, MarketPrices, Stage, StrategyConfig
from evhedge.power_model import pair_prob, strength

VALID_STAGE_TYPES = ("round_robin", "gauntlet", "single_elim")

#: Boss threshold for TIMING (rounds to first outright% > this).
DEFAULT_BOSS_THRESHOLD_PCT = 10.0

#: FUEL CHECK verdict bands, as a ratio of available_multiplier / required_multiplier.
FUEL_SOLID_RATIO = 1.30   # >= this: SOLID
FUEL_FAILS_RATIO = 1.00   # < this: FAILS; in between: THIN

#: LEG PROFILE: median known leg ask price (in the same %-units as no_prices)
#: above this triggers FAVORITE_PATTERN.
LEG_PROFILE_FAVORITE_THRESHOLD_PCT = 40.0

#: Sensitivity scenarios for the economics section: shift every KNOWN leg
#: ask price by this many percentage points (DESIGN CHOICE: the spec says
#: "three price scenarios, the way we calculated by hand" without giving
#: numbers; +/-5pp is a plausible, clearly-labeled placeholder band).
LEG_PRICE_STRESS_PP = 5.0

#: Notional stake used to run engine.compute_ev (scanner works in
#: percentages/ratios, not real position sizing -- 100 makes the EV output
#: read directly as "dollars per $100 notional", i.e. a percent-like number).
NOTIONAL_STAKE_USD = 100.0

#: Fill values used to band aggregate metrics when a candidate's path
#: contains "no_data" pairwise gaps (see module docstring). The 0.5 point
#: value is still reported as the central estimate; these two produce the
#: (low, high) range around it.
NO_DATA_FILL_LOW = 0.2
NO_DATA_FILL_HIGH = 0.8

#: FUEL CHECK verdict when "no_data" gaps make the verdict flip across the
#: NO_DATA_FILL_LOW..NO_DATA_FILL_HIGH band -- the data doesn't support a
#: SOLID/THIN/FAILS call at all.
INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


class ScannerError(Exception):
    """Raised for any problem building or scanning a tournament bracket
    config: malformed YAML, an inconsistent bracket, or a team referenced
    that isn't in ``teams``."""


# ---------------------------------------------------------------------------
# Config model
# ---------------------------------------------------------------------------

@dataclass
class StageMeta:
    """One stage of the tournament, in order.

    Attributes:
        name: Human label, e.g. "group", "survival", "playoff".
        type: One of "round_robin", "gauntlet", "single_elim".
        match_format: Free text, e.g. "bo1", "bo2", "bo3", "bo5" -- kept
            for the passport/report, not parsed.
        hedge_suitable: False for stages where a clean hedge resolution
            isn't possible (e.g. round_robin Bo2, where a draw breaks a
            binary win/lose hedge bet).
    """

    name: str
    type: str
    match_format: str
    hedge_suitable: bool

    def __post_init__(self) -> None:
        if self.type not in VALID_STAGE_TYPES:
            raise ScannerError(
                f"stages_meta[{self.name!r}].type must be one of {VALID_STAGE_TYPES}, "
                f"got {self.type!r}"
            )


# A bracket node is either a team name (leaf) or a 2-element list of two
# child nodes (internal node) -- see load_scanner_config's docstring for
# the YAML shape.
BracketNode = Union[str, list]


@dataclass
class ScannerConfig:
    """Parsed scanner input for one tournament.

    Attributes:
        tournament: Tournament label.
        stages_meta: Ordered list of ``StageMeta``.
        teams: team name -> outright win %, e.g. {"TeamA": 34.5}.
        bracket: Nested-pair tree covering the elimination portion of the
            path (see ``load_scanner_config`` for the exact YAML shape).
            ``None`` if there's no elimination bracket left to model.
        target_market: "reach_final" or "winner".
        no_prices: team name -> current NO ask price, in percent
            (e.g. 91.9 for 91.9c). Only teams present here are scan-able
            (no NO price -> no candidate, per the FUEL CHECK).
        leg_prices: (team_a, team_b) -> ask %-price that team_a wins that
            SPECIFIC match, for known/quoted upcoming legs only (deeper,
            hypothetical rounds have no market price by construction --
            nobody prices "A vs whoever wins the other half"). Both key
            orders are checked at lookup time.
        recent_upset: Team names that recently knocked out a favorite --
            drives the HYPE FLAG.
        outright_threshold_pct: Only teams below this outright % are
            scanned (the whole point of this module is long shots).
    """

    tournament: str
    stages_meta: list[StageMeta]
    teams: dict[str, float]
    bracket: Optional[BracketNode]
    target_market: str
    no_prices: dict[str, float] = field(default_factory=dict)
    leg_prices: dict[tuple[str, str], float] = field(default_factory=dict)
    recent_upset: set[str] = field(default_factory=set)
    outright_threshold_pct: float = DEFAULT_BOSS_THRESHOLD_PCT

    def __post_init__(self) -> None:
        if self.target_market not in ("reach_final", "winner"):
            raise ScannerError(
                f"target_market must be 'reach_final' or 'winner', got {self.target_market!r}"
            )
        if self.bracket is not None:
            _validate_bracket(self.bracket, self.teams)

    @property
    def power_model_enabled(self) -> bool:
        """Disabled tournament-wide if any stage is round_robin (see
        module docstring for why)."""
        return not any(s.type == "round_robin" for s in self.stages_meta)

    @property
    def excluded_stages(self) -> list[str]:
        """Stages left out of the roll chain: round_robin, or anything
        explicitly marked not hedge_suitable."""
        return [s.name for s in self.stages_meta if s.type == "round_robin" or not s.hedge_suitable]


def _validate_bracket(node: BracketNode, teams: dict[str, float]) -> set[str]:
    """Recursively validate the bracket tree; returns the set of team
    names under this node."""
    if isinstance(node, str):
        if node not in teams:
            raise ScannerError(f"bracket references team {node!r}, not present in 'teams'")
        return {node}
    if isinstance(node, list):
        if len(node) != 2:
            raise ScannerError(f"every bracket node must have exactly 2 children, got {len(node)}")
        left_teams = _validate_bracket(node[0], teams)
        right_teams = _validate_bracket(node[1], teams)
        overlap = left_teams & right_teams
        if overlap:
            raise ScannerError(f"team(s) {overlap} appear in both halves of the same bracket node")
        return left_teams | right_teams
    raise ScannerError(f"bracket node must be a team name or a 2-element list, got {node!r}")


# ---------------------------------------------------------------------------
# Bracket tree helpers
# ---------------------------------------------------------------------------

def bracket_teams(node: BracketNode) -> set[str]:
    if isinstance(node, str):
        return {node}
    return bracket_teams(node[0]) | bracket_teams(node[1])


def _ancestors_of(node: BracketNode, team: str) -> Optional[list[BracketNode]]:
    """Ancestor internal nodes from the IMMEDIATE parent to the root,
    i.e. ancestors[0] is team's next match, ancestors[-1] is the final.
    None if team isn't found under node."""
    if isinstance(node, str):
        return [] if node == team else None
    for child in node:
        found = _ancestors_of(child, team)
        if found is not None:
            return found + [node]
    return None


def rounds_to_title(node: BracketNode, team: str) -> int:
    """Number of remaining elimination rounds for team, counting up to and
    including the final (root)."""
    ancestors = _ancestors_of(node, team)
    if ancestors is None:
        raise ScannerError(f"team {team!r} not found in bracket")
    return len(ancestors)


def candidate_pool(node: BracketNode, team: str, round_no: int) -> set[str]:
    """The pool of teams who COULD be team's opponent at round_no (1 =
    next match). This is the set of teams in the sibling subtree at that
    depth."""
    ancestors = _ancestors_of(node, team)
    if ancestors is None:
        raise ScannerError(f"team {team!r} not found in bracket")
    if not (1 <= round_no <= len(ancestors)):
        raise ScannerError(f"round_no {round_no} out of range for team {team!r} (1..{len(ancestors)})")

    parent = ancestors[round_no - 1]
    left, right = parent
    return bracket_teams(right) if team in bracket_teams(left) else bracket_teams(left)


# ---------------------------------------------------------------------------
# Tournament model: pairwise probabilities + memoized subtree winner distributions
# ---------------------------------------------------------------------------

class TournamentModel:
    """Wraps a ``ScannerConfig`` with memoized subtree-winner-distribution
    computation and a running tally of how many pairwise probabilities came
    from the market vs. the model vs. an unfilled gap.

    ``no_data_fill`` is the probability substituted for a pairwise gap
    (neither a leg price nor a model estimate available), applied in the
    orientation the pair is first queried. 0.5 is the neutral central
    estimate; ``scan()`` additionally builds models at
    ``NO_DATA_FILL_LOW``/``NO_DATA_FILL_HIGH`` to band the aggregates
    (see module docstring)."""

    def __init__(self, config: ScannerConfig, no_data_fill: float = 0.5):
        if not (0.0 < no_data_fill < 1.0):
            raise ScannerError(f"no_data_fill must be in (0, 1), got {no_data_fill}")
        self.config = config
        self.no_data_fill = no_data_fill
        self._dist_cache: dict[int, dict[str, float]] = {}
        self._pair_cache: dict[tuple[str, str], tuple[float, str]] = {}
        self.source_counts: dict[str, int] = {"market": 0, "model": 0, "no_data": 0}

    def _strength(self, team: str) -> float:
        n = rounds_to_title(self.config.bracket, team)
        return strength(self.config.teams[team], n)

    def pair_prob_sourced(self, a: str, b: str) -> tuple[float, str]:
        """P(a beats b), tagged with where it came from: 'market'
        (leg_prices), 'model' (power_model, only if enabled), or 'no_data'
        (neither available -- filled with ``self.no_data_fill``, NOT a
        model estimate, and counted so the gap is visible)."""
        key = (a, b)
        if key in self._pair_cache:
            return self._pair_cache[key]

        if (a, b) in self.config.leg_prices:
            p, src = self.config.leg_prices[(a, b)] / 100.0, "market"
        elif (b, a) in self.config.leg_prices:
            p, src = 1.0 - self.config.leg_prices[(b, a)] / 100.0, "market"
        elif self.config.power_model_enabled:
            p, src = pair_prob(self._strength(a), self._strength(b)), "model"
        else:
            p, src = self.no_data_fill, "no_data"

        self.source_counts[src] += 1
        self._pair_cache[(a, b)] = (p, src)
        self._pair_cache[(b, a)] = (1.0 - p, src)
        return p, src

    def winner_distribution(self, node: BracketNode) -> dict[str, float]:
        """P(each team under node ends up being node's eventual winner)."""
        key = id(node)
        if key in self._dist_cache:
            return self._dist_cache[key]

        if isinstance(node, str):
            dist = {node: 1.0}
        else:
            left, right = node
            left_dist = self.winner_distribution(left)
            right_dist = self.winner_distribution(right)
            dist = {}
            for a, pa in left_dist.items():
                for b, pb in right_dist.items():
                    p_ab, _ = self.pair_prob_sourced(a, b)
                    dist[a] = dist.get(a, 0.0) + pa * pb * p_ab
                    dist[b] = dist.get(b, 0.0) + pa * pb * (1.0 - p_ab)

        self._dist_cache[key] = dist
        return dist

    def round_opponent_distribution(self, team: str, round_no: int) -> dict[str, float]:
        """P(each candidate becomes team's ACTUAL round_no opponent) =
        that candidate's probability of winning the sibling subtree."""
        ancestors = _ancestors_of(self.config.bracket, team)
        parent = ancestors[round_no - 1]
        left, right = parent
        sibling = right if team in bracket_teams(left) else left
        return self.winner_distribution(sibling)

    def team_round_survival_prob(self, team: str, round_no: int) -> float:
        """P(team wins its round_no match), aggregated over the whole
        opponent distribution for that round."""
        opp_dist = self.round_opponent_distribution(team, round_no)
        total = 0.0
        for opp, p_opp in opp_dist.items():
            p_win, _ = self.pair_prob_sourced(team, opp)
            total += p_opp * p_win
        return total


# ---------------------------------------------------------------------------
# Per-candidate diagnostics
# ---------------------------------------------------------------------------

def _target_depth(config: ScannerConfig, team: str) -> int:
    total_rounds = rounds_to_title(config.bracket, team)
    if config.target_market == "winner":
        return total_rounds
    return max(0, total_rounds - 1)  # reach_final: everything except the final itself


def deadness(model: TournamentModel, team: str, depth: int) -> float:
    """Sum over remaining rounds of E[opponent strength] -- higher means a
    harder ("deader") path, not a favorable draw."""
    total = 0.0
    for r in range(1, depth + 1):
        opp_dist = model.round_opponent_distribution(team, r)
        for opp, p_opp in opp_dist.items():
            n_opp = rounds_to_title(model.config.bracket, opp)
            s_opp = strength(model.config.teams[opp], n_opp) if opp in model.config.teams else 0.0
            total += p_opp * s_opp
    return total


def p_stays_dead(model: TournamentModel, team: str, depth: int) -> float:
    """P(the bracket never gives an easy break) = P(no round draws the
    single WEAKEST candidate in that round's pool), across all remaining
    rounds.

    DESIGN CHOICE: "stays dead" isn't given a precise threshold in the
    spec. Reading "dead" as "never gets the best-case opponent" is the
    most literal, unambiguous definition available from bench_depth's own
    framing (2nd-strongest candidate) -- adjust the per-round "easy break"
    rule here if a different one was intended. Rounds whose candidate pool
    already has only one possible team are skipped: that opponent is
    already fixed, not a draw, so there's no "easy break" question to ask
    there (it would otherwise force this to 0.0 for every team, since the
    sole candidate is trivially both the only option and the "weakest").
    """
    p_no_easy_break = 1.0
    for r in range(1, depth + 1):
        opp_dist = model.round_opponent_distribution(team, r)
        if len(opp_dist) < 2:
            continue
        weakest = min(
            opp_dist,
            key=lambda o: strength(model.config.teams[o], rounds_to_title(model.config.bracket, o)),
        )
        p_no_easy_break *= (1.0 - opp_dist[weakest])
    return p_no_easy_break


def bench_depth(model: TournamentModel, team: str, depth: int) -> dict[int, tuple[str, float]]:
    """Per remaining round: the SECOND-strongest possible opponent
    (name, strength) -- who's the next danger if the top seed doesn't
    make it that round."""
    out: dict[int, tuple[str, float]] = {}
    for r in range(1, depth + 1):
        opp_dist = model.round_opponent_distribution(team, r)
        ranked = sorted(
            opp_dist,
            key=lambda o: strength(model.config.teams[o], rounds_to_title(model.config.bracket, o)),
            reverse=True,
        )
        if len(ranked) >= 2:
            second = ranked[1]
            out[r] = (second, strength(model.config.teams[second], rounds_to_title(model.config.bracket, second)))
    return out


def min_opponent_strength(model: TournamentModel, team: str, depth: int) -> Optional[float]:
    strengths = []
    for r in range(1, depth + 1):
        for opp in model.round_opponent_distribution(team, r):
            strengths.append(strength(model.config.teams[opp], rounds_to_title(model.config.bracket, opp)))
    return min(strengths) if strengths else None


def rounds_to_boss(model: TournamentModel, team: str, depth: int, threshold_pct: float) -> Optional[int]:
    for r in range(1, depth + 1):
        for opp in model.round_opponent_distribution(team, r):
            if model.config.teams[opp] > threshold_pct:
                return r
    return None


# ---------------------------------------------------------------------------
# FUEL CHECK
# ---------------------------------------------------------------------------

@dataclass
class FuelCheck:
    premium_pct: float
    required_multiplier: float
    available_multiplier: float
    verdict: str  # "SOLID" | "THIN" | "FAILS"


def fuel_check(model: TournamentModel, team: str, depth: int, no_price_pct: float) -> FuelCheck:
    no_price = no_price_pct / 100.0
    premium = 1.0 - no_price
    required_multiplier = no_price / premium if premium > 0 else float("inf")

    available_multiplier = 1.0
    for r in range(1, depth + 1):
        opp_dist = model.round_opponent_distribution(team, r)
        # Weighted-average leg decimal odds across the round's opponent
        # distribution: for each possible opponent, team's own win prob
        # against them gives that leg's decimal odds (1/p), weighted by
        # how likely that opponent actually is.
        weighted_decimal = sum(
            p_opp * (1.0 / model.pair_prob_sourced(team, opp)[0])
            for opp, p_opp in opp_dist.items()
            if model.pair_prob_sourced(team, opp)[0] > 0
        )
        available_multiplier *= weighted_decimal

    ratio = available_multiplier / required_multiplier if required_multiplier > 0 else float("inf")
    if ratio >= FUEL_SOLID_RATIO:
        verdict = "SOLID"
    elif ratio >= FUEL_FAILS_RATIO:
        verdict = "THIN"
    else:
        verdict = "FAILS"

    return FuelCheck(
        premium_pct=premium * 100.0,
        required_multiplier=required_multiplier,
        available_multiplier=available_multiplier,
        verdict=verdict,
    )


# ---------------------------------------------------------------------------
# LEG PROFILE + HYPE flags
# ---------------------------------------------------------------------------

FAVORITE_PATTERN_TEXT = (
    "не фри-ролл, а качели: NO проседает в середине пути, дешёвые ноги не покрывают"
)
HYPE_FLAG_TEXT = "окно входа — часы; ноги будут дорожать с каждым апсетом"

#: Hype v2 thresholds (DESIGN CHOICE: placeholder band, tune against real
#: episodes once the snapshot DB accumulates a few). Velocity is measured
#: on the team's NO market (see storage.price_velocity): NO falling
#: faster than this many pp/hour = the market is repricing the team up
#: RIGHT NOW = the entry window is closing.
HYPE_VELOCITY_THRESHOLD_PP_PER_HOUR = 1.0
#: Lookback window for the velocity computation (used by the CLI wiring).
HYPE_VELOCITY_WINDOW_HOURS = 24.0


def hype_assessment(
    config: ScannerConfig, team: str, no_velocity_pp_per_hour: Optional[float] = None
) -> tuple[Optional[str], Optional[str]]:
    """(flag, source) for the HYPE check.

    Computed data WINS when it exists: if a velocity is supplied, the
    decision is purely ``velocity <= -HYPE_VELOCITY_THRESHOLD_PP_PER_HOUR``
    (NO price falling fast) -- even for a team hand-listed in
    ``recent_upset`` (a manual flag contradicted by a flat price is stale
    news, not hype). Only when velocity is None (no/too little snapshot
    history -- an honest "don't know") does the manual ``recent_upset``
    fallback apply. Source is "computed" or "manual" accordingly.
    """
    if no_velocity_pp_per_hour is not None:
        if no_velocity_pp_per_hour <= -HYPE_VELOCITY_THRESHOLD_PP_PER_HOUR:
            return HYPE_FLAG_TEXT, "computed"
        return None, None
    if team in config.recent_upset:
        return HYPE_FLAG_TEXT, "manual"
    return None, None


def leg_profile_flag(model: TournamentModel, team: str, depth: int) -> Optional[str]:
    """Median KNOWN (market, not model/no_data) leg ask price across the
    remaining path, in percent. > LEG_PROFILE_FAVORITE_THRESHOLD_PCT ->
    FAVORITE_PATTERN."""
    known_leg_prices_pct = []
    for r in range(1, depth + 1):
        opp_dist = model.round_opponent_distribution(team, r)
        for opp in opp_dist:
            if (team, opp) in model.config.leg_prices:
                known_leg_prices_pct.append(model.config.leg_prices[(team, opp)])
            elif (opp, team) in model.config.leg_prices:
                known_leg_prices_pct.append(100.0 - model.config.leg_prices[(opp, team)])

    if not known_leg_prices_pct:
        return None
    known_leg_prices_pct.sort()
    mid = len(known_leg_prices_pct) // 2
    if len(known_leg_prices_pct) % 2:
        median = known_leg_prices_pct[mid]
    else:
        median = (known_leg_prices_pct[mid - 1] + known_leg_prices_pct[mid]) / 2.0

    return "FAVORITE_PATTERN" if median > LEG_PROFILE_FAVORITE_THRESHOLD_PCT else None


def hype_flag(config: ScannerConfig, team: str) -> Optional[str]:
    """Manual-only HYPE check (the pre-velocity fallback); prefer
    ``hype_assessment``, which also consumes a computed price velocity."""
    return HYPE_FLAG_TEXT if team in config.recent_upset else None


# ---------------------------------------------------------------------------
# LIQUIDITY (Module 1 integration, optional)
# ---------------------------------------------------------------------------

@dataclass
class LiquidityInfo:
    volume_usd: Optional[float] = None
    executable_usd: Optional[float] = None
    executable_avg_price: Optional[float] = None
    status: str = "unknown"  # "checked" | "unknown"


def check_liquidity(
    token_id: Optional[str], volume_usd: Optional[float], worst_price: float
) -> LiquidityInfo:
    """Best-effort order-book check via data_sources.polymarket. No
    token_id / no network -> "unknown", never a hard failure -- this is the
    field that decides tradability when reading the report ("нет книги —
    нет кандидата"), but scan() itself stays usable offline."""
    if token_id is None:
        return LiquidityInfo(volume_usd=volume_usd, status="unknown")
    try:
        book = polymarket_ds.fetch_order_book(token_id)
    except PolymarketAPIError:
        return LiquidityInfo(volume_usd=volume_usd, status="unknown")

    usd, avg_price = polymarket_ds.executable_size(book, "buy", worst_price)
    return LiquidityInfo(
        volume_usd=volume_usd, executable_usd=usd, executable_avg_price=avg_price, status="checked"
    )


# ---------------------------------------------------------------------------
# ECONOMICS (real engine/strategies integration)
# ---------------------------------------------------------------------------

@dataclass
class EconomicsResult:
    ev_lockin: float
    ev_hold: float
    terminal_branch_pnl: float
    sensitivity: dict[str, float]  # scenario label -> ev_lockin under that leg-price shift


def _build_stages_for_path(model: TournamentModel, team: str, depth: int) -> list[Stage]:
    stages = []
    for r in range(1, depth + 1):
        win_prob = model.team_round_survival_prob(team, r)
        win_prob = min(max(win_prob, 1e-9), 1.0)  # Stage requires (0, 1]

        opp_dist = model.round_opponent_distribution(team, r)
        hedge_odds = None
        for opp in opp_dist:
            if (team, opp) in model.config.leg_prices:
                hedge_odds = 100.0 / model.config.leg_prices[(team, opp)]
                break
            if (opp, team) in model.config.leg_prices:
                hedge_odds = 100.0 / (100.0 - model.config.leg_prices[(opp, team)])
                break

        stages.append(Stage(name=f"Round {r}", win_prob=win_prob, hedge_decimal_odds=hedge_odds))
    return stages


def compute_economics(
    model: TournamentModel, team: str, depth: int, no_price_pct: float
) -> EconomicsResult:
    """EV of two postures (rolling lock_in hedge / plain NO hold), plus
    the terminal (team-wins-it-all) branch and a 3-scenario leg price
    sensitivity band.

    There is deliberately NO "exit_now" field: this module scores
    CANDIDATES to enter, not open positions, so there's no cost basis to
    value an exit against. A constant 0.0 in the report read as "exiting
    costs nothing" and invited bogus comparisons with ``ev_hold``.
    Exit valuation comes back together with position tracking (which
    carries the cost basis this config doesn't).
    """
    no_price = no_price_pct / 100.0
    market = MarketPrices(no_price=no_price, yes_price=1.0 - no_price)
    bracket = Bracket(team=team, tournament=model.config.tournament, sport="scanner",
                       stages=_build_stages_for_path(model, team, depth))

    lockin_strategy = StrategyConfig(
        name=f"{team}_lockin", no_stake_usd=NOTIONAL_STAKE_USD, hedge_mode="lock_in", kelly_fraction=1.0
    )
    hold_strategy = StrategyConfig(
        name=f"{team}_hold", no_stake_usd=NOTIONAL_STAKE_USD, hedge_mode="none"
    )

    lockin_result = compute_ev(bracket, market, lockin_strategy)
    hold_result = compute_ev(bracket, market, hold_strategy)
    terminal_pnl = lockin_result.outcome_rows[-1].profit_usd if lockin_result.outcome_rows else 0.0

    sensitivity = {"current": lockin_result.expected_value_usd}
    for label, shift in (("legs_cheaper", -LEG_PRICE_STRESS_PP), ("legs_pricier", LEG_PRICE_STRESS_PP)):
        shifted_leg_prices = {
            k: min(max(v + shift, 1.0), 99.0) for k, v in model.config.leg_prices.items()
        }
        shifted_config = ScannerConfig(
            tournament=model.config.tournament, stages_meta=model.config.stages_meta,
            teams=model.config.teams, bracket=model.config.bracket,
            target_market=model.config.target_market, no_prices=model.config.no_prices,
            leg_prices=shifted_leg_prices, recent_upset=model.config.recent_upset,
            outright_threshold_pct=model.config.outright_threshold_pct,
        )
        shifted_model = TournamentModel(shifted_config)
        shifted_bracket = Bracket(team=team, tournament=model.config.tournament, sport="scanner",
                                   stages=_build_stages_for_path(shifted_model, team, depth))
        sensitivity[label] = compute_ev(shifted_bracket, market, lockin_strategy).expected_value_usd

    return EconomicsResult(
        ev_lockin=lockin_result.expected_value_usd,
        ev_hold=hold_result.expected_value_usd,
        terminal_branch_pnl=terminal_pnl,
        sensitivity=sensitivity,
    )


# ---------------------------------------------------------------------------
# CandidateReport + scan()
# ---------------------------------------------------------------------------

@dataclass
class CandidateReport:
    team: str
    # Liquidity is listed early deliberately -- it gates tradability before
    # any of the path/economics numbers below are worth reading at all.
    liquidity: LiquidityInfo
    # data_complete is second for the same reason: False means at least one
    # "no_data" pair leaked into the aggregates below -- read the *_range
    # bands, not the point values, and don't rank this candidate alongside
    # data-complete ones.
    data_complete: bool
    deadness: float
    p_stays_dead: float
    bench_depth: dict[int, tuple[str, float]]
    min_opp_strength: Optional[float]
    rounds_to_boss: Optional[int]
    no_price: float
    premium_pct: float
    required_multiplier: float
    available_multiplier: float
    fuel_verdict: str  # SOLID | THIN | FAILS | INSUFFICIENT_DATA
    leg_profile_flag: Optional[str]
    hype_flag: Optional[str]
    ev_lockin: float
    ev_hold: float
    terminal_branch_pnl: float
    sensitivity: dict[str, float]
    sources_breakdown: dict[str, int]
    excluded_stages: list[str]
    # (low, high) bands across the NO_DATA_FILL_LOW..HIGH re-computation;
    # None when data_complete (the point value is then the whole story).
    deadness_range: Optional[tuple[float, float]] = None
    p_stays_dead_range: Optional[tuple[float, float]] = None
    available_multiplier_range: Optional[tuple[float, float]] = None
    # "computed" (price velocity) or "manual" (recent_upset fallback) when
    # hype_flag is set; None otherwise. See hype_assessment.
    hype_source: Optional[str] = None


#: Verdict sort order for ``sort_candidates``. DESIGN CHOICE:
#: INSUFFICIENT_DATA sits between THIN and FAILS -- it must not rank
#: alongside data-complete candidates (that's its whole point), but a
#: data-complete FAILS is a conclusive "no" while INSUFFICIENT_DATA might
#: still become tradable once the missing legs get quoted, so it sorts
#: above FAILS, not below.
FUEL_VERDICT_SORT_ORDER = ("SOLID", "THIN", INSUFFICIENT_DATA, "FAILS")


def sort_candidates(reports: list[CandidateReport]) -> list[CandidateReport]:
    """Sort reports by fuel verdict (``FUEL_VERDICT_SORT_ORDER``), then by
    deadness ascending (an easier path first) within the same verdict."""
    order = {v: i for i, v in enumerate(FUEL_VERDICT_SORT_ORDER)}
    return sorted(
        reports, key=lambda r: (order.get(r.fuel_verdict, len(order)), r.deadness)
    )


def scan(
    config: ScannerConfig,
    token_ids: Optional[dict[str, str]] = None,
    volumes: Optional[dict[str, float]] = None,
    no_velocities_pp_per_hour: Optional[dict[str, float]] = None,
) -> list[CandidateReport]:
    """Scan every team priced below ``config.outright_threshold_pct`` that
    has a quoted NO price, and build a full ``CandidateReport`` for each.

    Args:
        config: Parsed tournament config.
        token_ids: Optional team -> CLOB token id, for the live liquidity
            check (Module 1). Omit entirely to run fully offline --
            liquidity comes back "unknown" for every candidate, per the
            "optional, no-network-required" rule this whole data layer
            follows.
        volumes: Optional team -> market volume in USD, reported alongside
            the (possibly unknown) executable-size check.
        no_velocities_pp_per_hour: Optional team -> velocity of the team's
            NO price in pp/hour (from ``storage.price_velocity``; negative
            = NO falling = team rising). Drives the computed HYPE flag;
            teams absent here fall back to the manual ``recent_upset``
            check (see ``hype_assessment``).

    Returns:
        One ``CandidateReport`` per scanned team, in no particular order.
    """
    token_ids = token_ids or {}
    volumes = volumes or {}
    no_velocities_pp_per_hour = no_velocities_pp_per_hour or {}
    reports = []

    for team, outright_pct in config.teams.items():
        if outright_pct >= config.outright_threshold_pct:
            continue
        if team not in config.no_prices:
            continue  # no NO price -> not a candidate (see FUEL CHECK)
        if config.bracket is None or team not in bracket_teams(config.bracket):
            continue  # nothing left to scan (already out, or no bracket modeled)

        model = TournamentModel(config)
        depth = _target_depth(config, team)
        no_price_pct = config.no_prices[team]

        fuel = fuel_check(model, team, depth, no_price_pct)
        econ = compute_economics(model, team, depth, no_price_pct)
        liquidity = check_liquidity(token_ids.get(team), volumes.get(team), worst_price=no_price_pct / 100.0)
        dead = deadness(model, team, depth)
        stays_dead = p_stays_dead(model, team, depth)

        # "no_data" gaps: re-run the 0.5-fill-sensitive aggregates at the
        # 0.2/0.8 band and, if the FUEL verdict flips anywhere across the
        # band, refuse to call it at all (see module docstring).
        data_complete = model.source_counts["no_data"] == 0
        fuel_verdict = fuel.verdict
        deadness_range = stays_dead_range = available_range = None
        if not data_complete:
            band_models = [
                TournamentModel(config, no_data_fill=fill)
                for fill in (NO_DATA_FILL_LOW, NO_DATA_FILL_HIGH)
            ]
            band_fuels = [fuel_check(m, team, depth, no_price_pct) for m in band_models]
            band_dead = [deadness(m, team, depth) for m in band_models]
            band_stays = [p_stays_dead(m, team, depth) for m in band_models]

            deadness_range = (min([dead, *band_dead]), max([dead, *band_dead]))
            stays_dead_range = (min([stays_dead, *band_stays]), max([stays_dead, *band_stays]))
            all_available = [fuel.available_multiplier] + [f.available_multiplier for f in band_fuels]
            available_range = (min(all_available), max(all_available))

            verdicts = {fuel.verdict} | {f.verdict for f in band_fuels}
            if len(verdicts) > 1:
                fuel_verdict = INSUFFICIENT_DATA

        hype, hype_source = hype_assessment(
            config, team, no_velocities_pp_per_hour.get(team)
        )

        reports.append(CandidateReport(
            team=team,
            liquidity=liquidity,
            data_complete=data_complete,
            deadness=dead,
            p_stays_dead=stays_dead,
            bench_depth=bench_depth(model, team, depth),
            min_opp_strength=min_opponent_strength(model, team, depth),
            rounds_to_boss=rounds_to_boss(model, team, depth, DEFAULT_BOSS_THRESHOLD_PCT),
            no_price=no_price_pct,
            premium_pct=fuel.premium_pct,
            required_multiplier=fuel.required_multiplier,
            available_multiplier=fuel.available_multiplier,
            fuel_verdict=fuel_verdict,
            leg_profile_flag=leg_profile_flag(model, team, depth),
            hype_flag=hype,
            ev_lockin=econ.ev_lockin,
            ev_hold=econ.ev_hold,
            terminal_branch_pnl=econ.terminal_branch_pnl,
            sensitivity=econ.sensitivity,
            sources_breakdown=dict(model.source_counts),
            excluded_stages=config.excluded_stages,
            deadness_range=deadness_range,
            p_stays_dead_range=stays_dead_range,
            available_multiplier_range=available_range,
            hype_source=hype_source,
        ))

    return reports


# ---------------------------------------------------------------------------
# YAML loading (manual input; auto-parsing is a future extension point)
# ---------------------------------------------------------------------------

def load_scanner_config(path: Union[str, Path]) -> ScannerConfig:
    """Load a scanner YAML config.

    Expected shape::

        tournament: "EWC 2025 Dota 2"
        stages_meta:
          - {name: group, type: round_robin, match_format: bo2, hedge_suitable: false}
          - {name: survival, type: gauntlet, match_format: bo3, hedge_suitable: true}
          - {name: playoff, type: single_elim, match_format: bo3, hedge_suitable: true}
        teams: {TeamA: 34.5, TeamB: 2.6}
        target_market: reach_final   # or: winner
        no_prices: {TeamB: 91.9}
        # Optional:
        bracket:                     # nested pairs, elimination portion only
          - - [TeamA, TeamH]
            - [TeamD, TeamE]
          - - [TeamC, TeamF]
            - [TeamB, TeamG]
        leg_prices:                  # known/quoted upcoming legs only
          - {teams: [TeamB, TeamG], ask_pct: 62.0}   # TeamB's ask to win that match
        recent_upset: [TeamB]
        outright_threshold_pct: 10.0

    Raises:
        ConfigError: Malformed YAML or missing required top-level fields.
        ScannerError: Bracket/team-reference inconsistencies.
    """
    path = Path(path)
    data = _read_yaml_file(path)

    for key in ("tournament", "stages_meta", "teams", "target_market"):
        if key not in data or data[key] is None:
            raise ConfigError(f"{path}: отсутствует обязательное поле {key!r}")

    stages_meta = [
        StageMeta(name=s["name"], type=s["type"], match_format=s["match_format"],
                   hedge_suitable=s["hedge_suitable"])
        for s in data["stages_meta"]
    ]

    bracket = data.get("bracket")

    leg_prices: dict[tuple[str, str], float] = {}
    for entry in data.get("leg_prices", []) or []:
        team_a, team_b = entry["teams"]
        leg_prices[(team_a, team_b)] = float(entry["ask_pct"])

    return ScannerConfig(
        tournament=data["tournament"],
        stages_meta=stages_meta,
        teams={k: float(v) for k, v in data["teams"].items()},
        bracket=bracket,
        target_market=data["target_market"],
        no_prices={k: float(v) for k, v in (data.get("no_prices") or {}).items()},
        leg_prices=leg_prices,
        recent_upset=set(data.get("recent_upset") or []),
        outright_threshold_pct=float(data.get("outright_threshold_pct", DEFAULT_BOSS_THRESHOLD_PCT)),
    )
