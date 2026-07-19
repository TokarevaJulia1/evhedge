"""Polymarket market data: Gamma API (event/market listing) + CLOB API
(order book depth).

Ported from ``C:\\polymarket_v3`` (``polymarket_fetcher.py`` /
``backtest_favorite_combo.py``), with two changes:

1. HTTP client is ``httpx`` (evhedge's declared dependency), not
   ``requests`` (what v3 used) -- same retry/backoff behavior, different
   library.
2. New: ``fetch_order_book`` / ``executable_size``. v3 only ever looked at
   Gamma's ``outcomePrices`` (the last traded price), never the order book.
   That's a showcase price, not a tradable one -- on a real CONCACAF
   qualifier market the board showed 2.4c while the actual asks started at
   3.1c with only $0.27 of depth before the next level. PROJECT RULE: no
   scanner signal in this codebase is valid until it's been checked against
   ``fetch_order_book`` -- board prices are a display, only asks (bids, for
   selling) are tradable.

Nothing in ``evhedge.models``/``engine``/``strategies``/``montecarlo``
imports this module -- the rest of evhedge works fully offline via
``config_io``. Every function here raises ``PolymarketAPIError`` (not a
raw ``httpx`` exception) on network/response problems, with a message that
says which endpoint failed and why.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import httpx

GAMMA_API_URL = "https://gamma-api.polymarket.com"
CLOB_API_URL = "https://clob.polymarket.com"
DATA_API_URL = "https://data-api.polymarket.com"

DEFAULT_TIMEOUT = 10.0
DEFAULT_RETRIES = 2
PAGE_SIZE = 100


class PolymarketAPIError(Exception):
    """Raised for any problem fetching data from Polymarket's Gamma or CLOB
    API: network failure, timeout, non-2xx response, or an unexpected
    response shape -- after retries are exhausted."""


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _get(
    url: str,
    params: dict | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    retries: int = DEFAULT_RETRIES,
) -> dict | list:
    """GET with retry + backoff, raising ``PolymarketAPIError`` (not a raw
    httpx exception) once retries are exhausted."""
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            resp = httpx.get(url, params=params, timeout=timeout)
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError as e:
            last_exc = e
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
                continue

    raise PolymarketAPIError(
        f"не удалось получить данные с {url} (params={params}) после "
        f"{retries + 1} попыток: {last_exc}"
    ) from last_exc


def _get_paginated(url: str, params: dict, page_size: int = PAGE_SIZE) -> list[dict]:
    """Offset pagination for Gamma API list endpoints (hard cap
    page_size=100/page server-side)."""
    results: list[dict] = []
    offset = 0
    while True:
        page_params = {**params, "limit": page_size, "offset": offset}
        batch = _get(url, params=page_params)
        if not isinstance(batch, list) or not batch:
            break
        results.extend(batch)
        if len(batch) < page_size:
            break
        offset += page_size
    return results


# ---------------------------------------------------------------------------
# Gamma API: event/market listing
# ---------------------------------------------------------------------------

def fetch_tournament_markets(
    tag_slug: str, closed: bool = False, start_date_min: str | None = None
) -> list[dict]:
    """Fetch every event for a Gamma ``tag_slug`` (paginated).

    Args:
        tag_slug: Gamma tag slug, e.g. "cs2", "league-of-legends".
        closed: False (default) for active/live markets, True for settled
            ones.
        start_date_min: Optional ISO date(-time) server-side filter,
            e.g. "2026-07-01T00:00:00Z". PRACTICALLY REQUIRED for
            ``closed=True`` on a busy tag: Gamma hard-rejects deep
            pagination (422 at offset ~2100, observed live on "dota-2"),
            so an unfiltered walk over years of settled events dies
            mid-listing. Filter to the tournament window instead.

    Returns:
        Raw Gamma event dicts, unmodified. Each has (at least) ``slug``,
        ``title``, ``volume``, and a ``markets`` list where each market has
        ``outcomePrices`` (JSON-encoded string) and ``clobTokenIds``
        (JSON-encoded string of CLOB token ids, one per outcome) --
        ``clobTokenIds[i]`` is the token to pass to ``fetch_order_book`` for
        the outcome priced at ``outcomePrices[i]``.

    Raises:
        PolymarketAPIError: On a network/HTTP failure after retries.
    """
    params = {"tag_slug": tag_slug, "closed": "true" if closed else "false"}
    if start_date_min is not None:
        params["start_date_min"] = start_date_min
    return _get_paginated(f"{GAMMA_API_URL}/events", params)


def fetch_event_by_slug(slug: str, closed: bool = False) -> dict | None:
    """Fetch a single event by slug, or None if it doesn't exist (active
    or closed, per ``closed``)."""
    params: dict = {"slug": slug}
    if closed:
        params["closed"] = "true"
    data = _get(f"{GAMMA_API_URL}/events", params=params)
    if isinstance(data, list) and data:
        return data[0]
    return None


def fetch_positions(address: str, limit: int = 500) -> list[dict]:
    """Every OPEN position currently held by a public Polymarket
    (Polygon) wallet address -- the public Data API, no authentication:
    this is on-chain-derived read data, not a private-account endpoint
    (unlike CLOB order placement/cancellation, which needs a signed
    API key derived from the wallet's private key and is deliberately
    NOT wrapped here).

    Args:
        address: Polygon wallet address (0x...), as used to sign in on
            Polymarket -- not an email or username.
        limit: Max positions returned. DESIGN CHOICE: a single
            unpaginated call, not ``_get_paginated`` -- 500 comfortably
            covers a real portfolio and the endpoint's own response
            carries no next-page cursor to walk. A wallet with more than
            500 simultaneous open positions would silently see only the
            first 500; not observed in practice, noted rather than
            solved speculatively.

    Returns:
        Raw Data API position dicts, unmodified. Each has (at least)
        ``title`` (market question), ``outcome`` (side held, e.g. a team
        name for a Yes/No team market), ``size`` (shares held),
        ``avgPrice``, ``initialValue`` (cost basis = size * avgPrice),
        ``currentValue``, ``curPrice``, ``asset`` (CLOB token id -- feed
        this to ``fetch_order_book`` for a live price), ``eventSlug``,
        ``slug``.

    Raises:
        PolymarketAPIError: On a network/HTTP failure after retries, or
            a malformed address the API itself rejects (400).
    """
    data = _get(f"{DATA_API_URL}/positions", params={"user": address, "limit": limit})
    if not isinstance(data, list):
        raise PolymarketAPIError(
            f"неожиданный формат ответа Data API /positions для {address!r}: "
            f"ожидался список, получено {type(data).__name__}"
        )
    return data


# ---------------------------------------------------------------------------
# CLOB API: order book depth
# ---------------------------------------------------------------------------

@dataclass
class BookLevel:
    """One price level of an order book side."""

    price: float
    size: float  # shares available at this price


@dataclass
class OrderBook:
    """Order book for one CLOB token (one outcome of one market).

    Attributes:
        token_id: The CLOB token id this book is for.
        bids: Buy-side levels (what you'd receive selling), any order.
        asks: Sell-side levels (what you'd pay buying), any order.
    """

    token_id: str
    bids: list[BookLevel] = field(default_factory=list)
    asks: list[BookLevel] = field(default_factory=list)


def fetch_order_book(token_id: str) -> OrderBook:
    """Fetch the live order book for a CLOB token.

    Args:
        token_id: CLOB token id (see ``fetch_tournament_markets`` docstring
            for where this comes from).

    Returns:
        An ``OrderBook`` with ``bids``/``asks`` as parsed ``BookLevel``s.

    Raises:
        PolymarketAPIError: On a network/HTTP failure, or if the response
            isn't the expected ``{"bids": [...], "asks": [...]}`` shape.
    """
    data = _get(f"{CLOB_API_URL}/book", params={"token_id": token_id})
    if not isinstance(data, dict):
        raise PolymarketAPIError(
            f"Неожиданный формат ответа CLOB /book для token_id={token_id}: "
            f"ожидался объект, получено {type(data).__name__}"
        )

    try:
        bids = [BookLevel(price=float(lvl["price"]), size=float(lvl["size"])) for lvl in data.get("bids", [])]
        asks = [BookLevel(price=float(lvl["price"]), size=float(lvl["size"])) for lvl in data.get("asks", [])]
    except (KeyError, TypeError, ValueError) as e:
        raise PolymarketAPIError(
            f"Не удалось разобрать уровни книги CLOB /book для token_id={token_id}: {e}"
        ) from e

    return OrderBook(token_id=token_id, bids=bids, asks=asks)


def best_bid_ask(book: OrderBook) -> tuple[float | None, float | None]:
    """Top-of-book (best bid, best ask), each ``None`` if that side is
    empty.

    This is the real, independently-traded bid/ask -- unlike a Gamma
    ``outcomePrices`` display value, which for a binary Yes/No market is a
    single derived number (Yes/No pair sums to exactly 1.0 by
    construction), not two prices with an actual spread between them.
    """
    best_bid = max((lvl.price for lvl in book.bids), default=None)
    best_ask = min((lvl.price for lvl in book.asks), default=None)
    return best_bid, best_ask


def executable_size(book: OrderBook, side: str, worst_price: float) -> tuple[float, float | None]:
    """How much USD is actually executable up to ``worst_price``, and at
    what volume-weighted average price -- the reason this function exists
    is that the top-of-book/last-traded price on the board is not what you
    actually pay; only walking the book tells you that.

    Args:
        side: "buy" (walks ``book.asks``, ascending price) or "sell"
            (walks ``book.bids``, descending price).
        worst_price: Do not include levels worse than this (higher than
            ``worst_price`` for "buy", lower than ``worst_price`` for
            "sell").

    Returns:
        ``(usd, avg_price)`` -- total USD notional executable within the
        price limit, and the volume-weighted average price actually paid
        (received) across those levels. ``avg_price`` is ``None`` if
        nothing is executable (no levels within the limit / empty book).

    Raises:
        ValueError: If ``side`` isn't "buy" or "sell".
    """
    if side not in ("buy", "sell"):
        raise ValueError(f"side must be 'buy' or 'sell', got {side!r}")

    levels = book.asks if side == "buy" else book.bids
    ordered = sorted(levels, key=lambda lvl: lvl.price, reverse=(side == "sell"))

    usd = 0.0
    shares = 0.0
    for lvl in ordered:
        if side == "buy" and lvl.price > worst_price:
            break
        if side == "sell" and lvl.price < worst_price:
            break
        usd += lvl.price * lvl.size
        shares += lvl.size

    avg_price = (usd / shares) if shares > 0 else None
    return usd, avg_price
