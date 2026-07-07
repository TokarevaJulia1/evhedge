"""Automated board collection: Gamma API -> storage.

Closes the seam left after Module 6: until now every price entered the
system by hand (YAML). This module walks live Gamma events and records
what it sees into ``storage``:

- ``collect_board``: a Yes/No-per-team event (an outright "Winner" board,
  a region aggregate, a "reach X" board) -> two ``PriceSnapshot`` rows per
  team (``<label>_yes`` / ``<label>_no``), token ids attached so a book
  check can follow.
- ``collect_match_markets``: team-vs-team match events under a tag ->
  OPEN series prices become ``leg`` snapshots (team A vs counterparty B,
  A's price); CLOSED game/series markets become ``Resolve`` rows -- the
  running record of results.

Everything recorded here is source="board" (display prices). The PROJECT
RULE from ``data_sources/polymarket.py`` stands: no signal is tradable
until checked against the order book; collection is history-keeping, not
signal generation.

Placeholder markets: team boards carry unopened slots ("A".."E",
"Other") quoted at exactly 0.5/0.5 with zero volume. DESIGN CHOICE: a
market is treated as a placeholder if it has NO recorded volume AND every
outcome price is exactly 0.5 -- a data rule, not a name list, so a real
team that happens to be called "Other" but has traded is kept. Skipped
placeholders are counted in the summary, never silently dropped.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from evhedge.data_sources import polymarket as polymarket_ds
from evhedge.data_sources.polymarket import PolymarketAPIError
from evhedge.storage import PriceSnapshot, Resolve, Storage, utcnow

#: Match-event markets whose outcomes are the two team names and whose
#: result we archive. DESIGN CHOICE: series + per-game winners only;
#: props (First Blood, Roshan, kill totals, ...) are deliberately not
#: collected -- they're not on any current path to a hedge leg.
RESULT_MARKET_TITLES = ("Match Winner",)
RESULT_MARKET_PREFIX = "Game "
RESULT_MARKET_SUFFIX = " Winner"


class CollectError(Exception):
    """Raised when an event/market can't be interpreted at all (missing
    event, malformed market JSON). Per-market shape surprises are counted
    in the summary instead of raising."""


@dataclass
class CollectSummary:
    """What one collection pass did -- every skip is counted, nothing is
    silently dropped (see the no-silent-caps rule)."""

    snapshots_written: int = 0
    resolves_written: int = 0
    markets_seen: int = 0
    skipped_placeholders: int = 0
    skipped_shape: int = 0        # outcomes not in the expected form
    skipped_unresolved: int = 0   # closed but not cleanly 1/0 (e.g. Bo2 draw)
    skipped_price_range: int = 0  # price at exactly 0 or 100 -- unquotable as (0,100)
    skipped_live: int = 0         # match already live: pre-match price history ends here
    book_fallback_to_board: int = 0  # verify_book requested but no usable book (error/empty side)
    labels: list[str] = field(default_factory=list)


def _market_prices(market: dict) -> list[float]:
    return [float(p) for p in json.loads(market.get("outcomePrices") or "[]")]


def _market_outcomes(market: dict) -> list[str]:
    return list(json.loads(market.get("outcomes") or "[]"))


def _market_tokens(market: dict) -> list[str]:
    return list(json.loads(market.get("clobTokenIds") or "[]"))


def _volume(market: dict) -> float:
    try:
        return float(market.get("volume") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _is_placeholder(market: dict) -> bool:
    prices = _market_prices(market)
    return (
        _volume(market) == 0.0
        and bool(prices)
        and all(p == 0.5 for p in prices)
    )


def _resolve_price(
    token_id: Optional[str], board_price_pct: float, verify_book: bool
) -> tuple[float, Optional[float], Optional[float], str, bool]:
    """Real bid/ask via the order book when asked for and available;
    Gamma's board price otherwise.

    Returns ``(price_pct, bid_pct, ask_pct, source, fell_back)``.
    ``price_pct`` is the tradable buy price (= ``ask_pct``) when
    ``source == "book"``; Gamma's display value otherwise. ``fell_back``
    is True whenever ``verify_book`` was requested but a book snapshot
    wasn't actually obtained (network error, or one side of the book
    empty) -- counted in ``CollectSummary.book_fallback_to_board``.
    """
    if verify_book and token_id:
        try:
            book = polymarket_ds.fetch_order_book(token_id)
            bid, ask = polymarket_ds.best_bid_ask(book)
            if bid is not None and ask is not None:
                return ask * 100.0, bid * 100.0, ask * 100.0, "book", False
        except PolymarketAPIError:
            pass
        return board_price_pct, None, None, "board", True
    return board_price_pct, None, None, "board", False


# ---------------------------------------------------------------------------
# Yes/No team boards (winner outright, region aggregate, ...)
# ---------------------------------------------------------------------------

def collect_board(
    store: Storage,
    tournament: str,
    event_slug: str,
    market_label: str,
    ts_utc: Optional[datetime] = None,
    verify_book: bool = False,
) -> CollectSummary:
    """Snapshot every traded Yes/No market of one Gamma event.

    Each market becomes two snapshots: ``<market_label>_yes`` and
    ``<market_label>_no``, team taken from ``groupItemTitle``, token ids
    attached per side. Prices at exactly 0/100 are skipped and counted
    (they can't be represented in the (0, 100) snapshot range and carry
    no velocity information anyway).

    Args:
        verify_book: If True, fetch the real order book for each side's
            token and record its best bid/ask (``source="book"``) instead
            of Gamma's ``outcomePrices`` value. WITHOUT this, ``..._no``
            is not an independent observation: Gamma's Yes/No pair sums
            to exactly 100.0 by construction, so the "no" row is just
            ``100 - yes``, carrying no additional information and no
            spread. Falls back to the board value (counted in
            ``book_fallback_to_board``) on a network error or an empty
            book side. Default False to keep this function network-free
            unless asked; the CLI (``evhedge pull``) defaults the flag on.

    Raises:
        CollectError: If the event doesn't exist.
    """
    event = polymarket_ds.fetch_event_by_slug(event_slug)
    if event is None:
        raise CollectError(f"событие {event_slug!r} не найдено в Gamma")

    ts = ts_utc or utcnow()
    summary = CollectSummary(labels=[f"{market_label}: {event.get('title', event_slug)}"])

    for market in event.get("markets", []):
        summary.markets_seen += 1
        if _is_placeholder(market):
            summary.skipped_placeholders += 1
            continue
        outcomes = _market_outcomes(market)
        if outcomes != ["Yes", "No"]:
            summary.skipped_shape += 1
            continue

        team = market.get("groupItemTitle") or market.get("question", "?")
        prices = _market_prices(market)
        tokens = _market_tokens(market)
        volume = _volume(market)
        for side, price, token_idx in (("yes", prices[0], 0), ("no", prices[1], 1)):
            board_price_pct = price * 100.0
            token = tokens[token_idx] if len(tokens) > token_idx else None
            price_pct, bid_pct, ask_pct, source, fell_back = _resolve_price(
                token, board_price_pct, verify_book
            )
            if fell_back:
                summary.book_fallback_to_board += 1
            if not (0.0 < price_pct < 100.0):
                summary.skipped_price_range += 1
                continue
            store.record_snapshot(PriceSnapshot(
                tournament=tournament,
                team=team,
                market=f"{market_label}_{side}",
                price_pct=price_pct,
                bid_pct=bid_pct,
                ask_pct=ask_pct,
                volume_usd=volume,
                source=source,
                ts_utc=ts,
                token_id=token,
            ))
            summary.snapshots_written += 1

    return summary


# ---------------------------------------------------------------------------
# Match events: open series -> leg snapshots, closed games -> resolves
# ---------------------------------------------------------------------------

def _is_result_market(market: dict) -> bool:
    title = market.get("groupItemTitle") or ""
    return title in RESULT_MARKET_TITLES or (
        title.startswith(RESULT_MARKET_PREFIX) and title.endswith(RESULT_MARKET_SUFFIX)
    )


def _event_is_live(event: dict, now: datetime) -> bool:
    """True once the match has started: Gamma's ``live`` flag OR
    ``startTime <= now`` (belt and braces -- the flag can lag the clock).
    Once live, the pre-match price series is complete; in-play drift is
    noise for entry pricing and is deliberately not recorded."""
    if event.get("live"):
        return True
    start_time = event.get("startTime")
    if not start_time:
        return False
    try:
        started = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
    except ValueError:
        return False
    return started <= now


def collect_match_markets(
    store: Storage,
    tournament: str,
    tag_slug: str,
    title_filter: str,
    ts_utc: Optional[datetime] = None,
    start_date_min: Optional[str] = None,
    verify_book: bool = False,
) -> CollectSummary:
    """Walk every match event under a Gamma tag whose event title contains
    ``title_filter`` (e.g. "Esports World Cup"), and record:

    - OPEN "Match Winner" markets -> TWO ``leg`` snapshots: team A vs
      counterparty B (A's price) AND team B vs counterparty A (B's price)
      -- both directions, not just the first outcome. For a two-outcome
      market B's board price is close to A's complement, but not exactly
      it (unlike a Yes/No pair, these are two independently quoted team
      tokens), so the mirror row is a real second observation, not a
      derived one. Matches that have gone LIVE are skipped
      (``skipped_live``): the pre-match series ends at throw-in, in-play
      prices are not entry prices.
    - CLOSED result markets (series + per-game) -> two ``Resolve`` rows,
      market label ``result:<event_slug>:<market title>``: "yes" for the
      team whose side settled at 1, "no" for the other. A closed market
      NOT settled cleanly at 1/0 (a Bo2 draw splits the pot) is counted
      as ``skipped_unresolved``, never guessed.

    Both open and closed events are fetched -- results live on closed
    ones. Pass ``start_date_min`` (ISO date, e.g. the tournament's start
    week) whenever the tag has history: Gamma hard-rejects deep
    pagination over settled events (422, see
    ``data_sources.polymarket.fetch_tournament_markets``).

    Args:
        verify_book: If True, fetch each leg's real order book and record
            its best bid/ask (``source="book"``) instead of the board
            price -- same rationale and fallback behavior as
            ``collect_board``'s ``verify_book``.
    """
    ts = ts_utc or utcnow()
    summary = CollectSummary(labels=[f"matches: tag={tag_slug!r} filter={title_filter!r}"])

    events = polymarket_ds.fetch_tournament_markets(
        tag_slug, closed=False, start_date_min=start_date_min
    )
    events += polymarket_ds.fetch_tournament_markets(
        tag_slug, closed=True, start_date_min=start_date_min
    )

    for event in events:
        if title_filter not in (event.get("title") or ""):
            continue
        for market in event.get("markets", []):
            if not _is_result_market(market):
                continue
            summary.markets_seen += 1

            outcomes = _market_outcomes(market)
            prices = _market_prices(market)
            if len(outcomes) != 2 or "Yes" in outcomes or len(prices) != 2:
                summary.skipped_shape += 1
                continue
            team_a, team_b = outcomes

            if not market.get("closed"):
                if (market.get("groupItemTitle") or "") not in RESULT_MARKET_TITLES:
                    continue  # open per-game props of the series: prices not needed
                if _event_is_live(event, ts):
                    summary.skipped_live += 1
                    continue
                tokens = _market_tokens(market)
                volume = _volume(market)
                for team_x, team_y, price, token_idx in (
                    (team_a, team_b, prices[0], 0), (team_b, team_a, prices[1], 1)
                ):
                    board_price_pct = price * 100.0
                    token = tokens[token_idx] if len(tokens) > token_idx else None
                    price_pct, bid_pct, ask_pct, source, fell_back = _resolve_price(
                        token, board_price_pct, verify_book
                    )
                    if fell_back:
                        summary.book_fallback_to_board += 1
                    if not (0.0 < price_pct < 100.0):
                        summary.skipped_price_range += 1
                        continue
                    store.record_snapshot(PriceSnapshot(
                        tournament=tournament, team=team_x, market="leg",
                        price_pct=price_pct, bid_pct=bid_pct, ask_pct=ask_pct,
                        volume_usd=volume, source=source, ts_utc=ts,
                        counterparty=team_y, token_id=token,
                    ))
                    summary.snapshots_written += 1
                continue

            # Closed: settle only on a clean 1/0.
            if sorted(prices) != [0.0, 1.0]:
                summary.skipped_unresolved += 1
                continue
            winner = team_a if prices[0] == 1.0 else team_b
            label = f"result:{event.get('slug', '?')}:{market.get('groupItemTitle', '?')}"
            for team in (team_a, team_b):
                store.record_resolve(Resolve(
                    tournament=tournament, team=team, market=label,
                    outcome="yes" if team == winner else "no", ts_utc=ts,
                    note=event.get("title"),
                ))
                summary.resolves_written += 1

    return summary
