"""Module 5: board-level price consistency checks (today's findings as a
class of signal).

Three internal-consistency checks over BOARD prices from Module 1
(``data_sources/polymarket.py``): a fixed-slot NO basket, a parent-vs-
members identity, and a per-team reach_X vertical. "Board-level" is the
operative word: everything here runs on display prices, and per the
PROJECT RULE in ``data_sources/polymarket.py`` no signal in this codebase
is valid until checked against ``fetch_order_book`` -- so every result
carries a mandatory ``caveat`` field saying exactly that. The caveat is
part of the data, not documentation, so it survives into any report the
result gets serialized into.

Units: all prices are in PERCENT (e.g. 91.9 for 91.9c), same convention
as ``scanner.ScannerConfig.no_prices``/``leg_prices`` -- which is also why
the basket payout reads as ``(n - slots) * 100``.

The three checks:

- ``basket_check``: on a board where exactly ``slots`` of ``n`` markets
  resolve YES, buying one NO share of every market pays exactly
  ``(n - slots) * 100`` points; if the sum of NO asks is below that, the
  basket locks in the difference regardless of outcome (the "+1.2%
  корзина" finding).
- ``identity_check``: an aggregate market ("a CONCACAF team wins") must
  price as the sum of its mutually-exclusive members (USA + Mexico + ...);
  any gap is a mispricing on one side or the other (the "CONCACAF=USA
  +0.6%" finding). The member mapping is supplied BY HAND in config --
  deliberately no auto-derivation, because deciding which markets are
  truly mutually-exclusive members of which aggregate is exactly the part
  that goes wrong silently when guessed.
- ``vertical_check``: one team's ladder of reach_X prices must be
  monotone non-increasing with depth (you can't be likelier to win the
  final than to reach it), and each implied conditional
  ``p_cond = P(deeper) / P(shallower)`` must be plausible: a violation
  (p_cond >= 1) is a hard signal, an extreme conditional is a soft flag
  (DESIGN CHOICE: "флаг на экстремумы" came without thresholds;
  ``VERTICAL_EXTREME_LOW``/``HIGH`` = 0.05/0.95 are documented, easily
  changed constants).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Union

from evhedge.config_io import ConfigError, _read_yaml_file
from evhedge.team_aliases import canonical_name

#: Mandatory caveat attached to EVERY result this module produces. Board
#: prices are a display; only order-book asks (bids, for selling) are
#: tradable -- see the PROJECT RULE in evhedge/data_sources/polymarket.py.
VERIFY_BOOK_CAVEAT = (
    "verify book before trading: board prices are a display, only order-book "
    "asks (bids, for selling) are tradable (data_sources/polymarket.py)"
)

#: DESIGN CHOICE: identity gaps below this (in percentage points) are
#: reported but not flagged as a signal -- sub-half-point gaps on a board
#: are indistinguishable from stale display prices and spread noise.
IDENTITY_MIN_EDGE_PCT = 0.5

#: DESIGN CHOICE: vertical conditional-probability extremes. p_cond at or
#: beyond these bounds is flagged (soft), not treated as a violation (hard,
#: p_cond >= 1 only).
VERTICAL_EXTREME_LOW = 0.05
VERTICAL_EXTREME_HIGH = 0.95


class ConsistencyError(Exception):
    """Raised for malformed check inputs: empty boards, out-of-range
    prices, an impossible slot count, or a too-short vertical ladder."""


def _validate_pct(label: str, value: float) -> None:
    if not (0.0 < value < 100.0):
        raise ConsistencyError(f"{label} must be in (0, 100), got {value}")


# ---------------------------------------------------------------------------
# basket_check
# ---------------------------------------------------------------------------

@dataclass
class BasketResult:
    n_markets: int
    slots: int
    cost_pct: float      # sum of NO asks across the board, points
    payout_pct: float    # (n_markets - slots) * 100, guaranteed
    edge_pct: float      # payout - cost, points (one NO share per market)
    return_pct: float    # edge / cost * 100 -- the headline "+1.2%" number
    is_signal: bool      # edge > 0
    caveat: str = VERIFY_BOOK_CAVEAT


def basket_check(markets: dict[str, float], slots: int) -> BasketResult:
    """Fixed-slot NO basket: with exactly ``slots`` of the ``n`` markets
    resolving YES, one NO share of every market redeems ``(n - slots) *
    100`` points no matter which teams take the slots. Sum of NO asks
    below that payout = a locked-in board-level edge.

    Args:
        markets: market/team -> NO ask price in percent. Must cover the
            ENTIRE board -- a partial basket has no guaranteed payout.
        slots: How many of these markets resolve YES (1 for a winner
            board, 2 for a finalists board, etc.).

    Raises:
        ConsistencyError: Fewer than 2 markets, ``slots`` not in
            ``1..n-1``, or any price outside (0, 100).
    """
    n = len(markets)
    if n < 2:
        raise ConsistencyError(f"basket_check needs at least 2 markets, got {n}")
    if not (1 <= slots < n):
        raise ConsistencyError(f"slots must be in 1..{n - 1} for {n} markets, got {slots}")
    for name, price in markets.items():
        _validate_pct(f"markets[{name!r}]", price)

    cost = sum(markets.values())
    payout = (n - slots) * 100.0
    edge = payout - cost
    return BasketResult(
        n_markets=n,
        slots=slots,
        cost_pct=cost,
        payout_pct=payout,
        edge_pct=edge,
        return_pct=edge / cost * 100.0,
        is_signal=edge > 0.0,
    )


# ---------------------------------------------------------------------------
# identity_check
# ---------------------------------------------------------------------------

@dataclass
class IdentityResult:
    parent: str
    parent_yes_pct: float
    members_sum_pct: float
    diff_pct: float      # parent - sum(members); positive = parent rich
    rich_side: str       # "parent" | "members" | "balanced"
    is_signal: bool      # |diff| >= IDENTITY_MIN_EDGE_PCT
    members_yes_pct: dict[str, float] = field(default_factory=dict)
    caveat: str = VERIFY_BOOK_CAVEAT


def identity_check(
    parent_market: tuple[str, float],
    member_markets: dict[str, float],
    alias_map: Optional[dict[str, str]] = None,
) -> IdentityResult:
    """Aggregate = sum of members: a parent market ("a CONCACAF team
    wins") over mutually-exclusive members must price as their sum; the
    gap, if any, is the finding ("CONCACAF=USA +0.6%").

    The member mapping comes hand-written from config -- this function
    trusts it completely and deliberately offers no auto-derivation (see
    module docstring). An incomplete member list shows up as the parent
    looking rich; that's on the mapping, not the market.

    Args:
        parent_market: (name, YES ask in percent) of the aggregate.
        member_markets: member name -> YES ask in percent.
        alias_map: Optional ``evhedge.team_aliases`` map (e.g. from
            ``load_default_aliases()``) to canonicalize the parent and
            member names before summing, so the same team spelled
            differently between the aggregate and a member board doesn't
            look like an incomplete mapping. None (default) skips
            canonicalization.

    Raises:
        ConsistencyError: Empty member mapping, any price outside
            (0, 100), or two distinct member names canonicalizing to the
            same name (ambiguous -- summing would silently drop one).
    """
    parent_name, parent_yes = parent_market
    parent_name = canonical_name(parent_name, alias_map)
    if not member_markets:
        raise ConsistencyError(f"identity_check({parent_name!r}): member_markets is empty")
    _validate_pct(f"parent {parent_name!r}", parent_yes)

    canon_members: dict[str, float] = {}
    raw_for_canon: dict[str, str] = {}
    for name, price in member_markets.items():
        _validate_pct(f"member_markets[{name!r}]", price)
        canon = canonical_name(name, alias_map)
        if canon in raw_for_canon:
            raise ConsistencyError(
                f"identity_check({parent_name!r}): member {name!r} and "
                f"{raw_for_canon[canon]!r} both canonicalize to {canon!r} -- "
                f"ambiguous duplicate, fix the input"
            )
        raw_for_canon[canon] = name
        canon_members[canon] = price
    member_markets = canon_members

    members_sum = sum(member_markets.values())
    diff = parent_yes - members_sum
    if abs(diff) < IDENTITY_MIN_EDGE_PCT:
        rich_side = "balanced"
    else:
        rich_side = "parent" if diff > 0 else "members"

    return IdentityResult(
        parent=parent_name,
        parent_yes_pct=parent_yes,
        members_sum_pct=members_sum,
        diff_pct=diff,
        rich_side=rich_side,
        is_signal=abs(diff) >= IDENTITY_MIN_EDGE_PCT,
        members_yes_pct=dict(member_markets),
    )


# ---------------------------------------------------------------------------
# vertical_check
# ---------------------------------------------------------------------------

@dataclass
class VerticalResult:
    team: str
    ladder: list[tuple[str, float]]                # as given, shallow -> deep
    conditionals: list[tuple[str, str, float]]     # (from, to, p_cond)
    violations: list[str]   # hard: monotonicity broken (p_cond >= 1)
    flags: list[str]        # soft: extreme but not impossible conditionals
    is_signal: bool         # bool(violations) -- flags alone don't signal
    caveat: str = VERIFY_BOOK_CAVEAT


def vertical_check(team: str, ladder: list[tuple[str, float]]) -> VerticalResult:
    """One team's reach_X price chain, shallow to deep (e.g. reach_semi
    -> reach_final -> winner): each deeper price must not exceed the
    shallower one, and each implied conditional ``p_cond = deeper /
    shallower`` must be plausible.

    A monotonicity break (p_cond >= 1) is a hard violation -- board-level
    it means "winning the final is priced as likelier than reaching it".
    A conditional at or beyond ``VERTICAL_EXTREME_LOW``/``HIGH`` is a soft
    flag: not impossible, but "the deeper rung is priced as (nearly) free
    / (nearly) unreachable given the shallower one" deserves eyes.

    Args:
        team: Team the ladder belongs to (labeling only).
        ladder: Ordered (stage_label, YES ask in percent) pairs, from the
            shallowest milestone to the deepest. At least 2 rungs.

    Raises:
        ConsistencyError: Fewer than 2 rungs, or any price outside
            (0, 100).
    """
    if len(ladder) < 2:
        raise ConsistencyError(
            f"vertical_check({team!r}) needs at least 2 rungs, got {len(ladder)}"
        )
    for label, price in ladder:
        _validate_pct(f"ladder[{label!r}]", price)

    conditionals: list[tuple[str, str, float]] = []
    violations: list[str] = []
    flags: list[str] = []

    for (from_label, from_pct), (to_label, to_pct) in zip(ladder, ladder[1:]):
        p_cond = to_pct / from_pct
        conditionals.append((from_label, to_label, p_cond))

        if p_cond >= 1.0:
            violations.append(
                f"{to_label} ({to_pct}) >= {from_label} ({from_pct}): "
                f"deeper rung priced above shallower (p_cond={p_cond:.3f})"
            )
        elif p_cond >= VERTICAL_EXTREME_HIGH:
            flags.append(
                f"EXTREME_HIGH {from_label}->{to_label}: p_cond={p_cond:.3f} "
                f">= {VERTICAL_EXTREME_HIGH} (deeper rung priced as nearly free)"
            )
        elif p_cond <= VERTICAL_EXTREME_LOW:
            flags.append(
                f"EXTREME_LOW {from_label}->{to_label}: p_cond={p_cond:.3f} "
                f"<= {VERTICAL_EXTREME_LOW} (deeper rung priced as nearly unreachable)"
            )

    return VerticalResult(
        team=team,
        ladder=list(ladder),
        conditionals=conditionals,
        violations=violations,
        flags=flags,
        is_signal=bool(violations),
    )


# ---------------------------------------------------------------------------
# Board config: YAML in, all checks out (for `evhedge check`)
# ---------------------------------------------------------------------------

@dataclass
class BoardConfig:
    """Parsed input for one board's worth of consistency checks.

    All three sections are optional in the YAML -- a config may carry just
    a basket, just identities, etc. Member mappings and ladders come
    hand-written (see module docstring on why there's no auto-derivation).
    """

    board: str
    baskets: list[tuple[str, dict[str, float], int]] = field(default_factory=list)
    identities: list[tuple[tuple[str, float], dict[str, float]]] = field(default_factory=list)
    verticals: list[tuple[str, list[tuple[str, float]]]] = field(default_factory=list)


@dataclass
class BoardCheckReport:
    """Everything ``run_board_checks`` found, per check type. Each nested
    result carries its own mandatory ``caveat``."""

    board: str
    baskets: list[tuple[str, BasketResult]] = field(default_factory=list)
    identities: list[IdentityResult] = field(default_factory=list)
    verticals: list[VerticalResult] = field(default_factory=list)


def load_board_config(path: Union[str, Path]) -> BoardConfig:
    """Load a board-check YAML config.

    Expected shape (every section after ``board`` optional)::

        board: "WC2026 winner board"
        baskets:
          - name: "full winner board NO"      # optional label
            slots: 1
            markets: {TeamA: 62.0, TeamB: 79.4}
        identities:
          - parent: {name: "CONCACAF winner", yes_pct: 9.4}
            members: {USA: 8.5, Mexico: 0.2, Canada: 0.1}
        verticals:
          - team: Morocco
            ladder:
              - {stage: reach_semi, yes_pct: 12.0}
              - {stage: reach_final, yes_pct: 8.0}
              - {stage: winner, yes_pct: 3.0}

    Raises:
        ConfigError: Malformed YAML, missing ``board``, or a section entry
            missing its required keys.
    """
    path = Path(path)
    data = _read_yaml_file(path)

    if not data.get("board"):
        raise ConfigError(f"{path}: отсутствует обязательное поле 'board'")

    baskets = []
    for i, entry in enumerate(data.get("baskets") or []):
        try:
            baskets.append((
                str(entry.get("name", f"basket #{i + 1}")),
                {str(k): float(v) for k, v in entry["markets"].items()},
                int(entry["slots"]),
            ))
        except (KeyError, TypeError, ValueError, AttributeError) as e:
            raise ConfigError(f"{path}: baskets[{i}]: {e}") from e

    identities = []
    for i, entry in enumerate(data.get("identities") or []):
        try:
            parent = entry["parent"]
            identities.append((
                (str(parent["name"]), float(parent["yes_pct"])),
                {str(k): float(v) for k, v in entry["members"].items()},
            ))
        except (KeyError, TypeError, ValueError, AttributeError) as e:
            raise ConfigError(f"{path}: identities[{i}]: {e}") from e

    verticals = []
    for i, entry in enumerate(data.get("verticals") or []):
        try:
            ladder = [(str(r["stage"]), float(r["yes_pct"])) for r in entry["ladder"]]
            verticals.append((str(entry["team"]), ladder))
        except (KeyError, TypeError, ValueError, AttributeError) as e:
            raise ConfigError(f"{path}: verticals[{i}]: {e}") from e

    return BoardConfig(
        board=str(data["board"]), baskets=baskets, identities=identities, verticals=verticals
    )


def run_board_checks(config: BoardConfig) -> BoardCheckReport:
    """Run every check listed in ``config`` and collect the results.

    Raises:
        ConsistencyError: Propagated from any individual check on
            malformed inputs (bad slot count, out-of-range price, ...).
    """
    return BoardCheckReport(
        board=config.board,
        baskets=[(name, basket_check(markets, slots)) for name, markets, slots in config.baskets],
        identities=[identity_check(parent, members) for parent, members in config.identities],
        verticals=[vertical_check(team, ladder) for team, ladder in config.verticals],
    )
