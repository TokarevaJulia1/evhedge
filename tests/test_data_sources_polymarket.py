"""Tests for evhedge.data_sources.polymarket.

No real network calls: httpx.get is monkeypatched everywhere. These tests
cover retry/backoff, pagination, and the order-book depth math -- not live
API behavior.
"""

import httpx
import pytest

from evhedge.data_sources.polymarket import (
    BookLevel,
    OrderBook,
    PolymarketAPIError,
    _get,
    _get_paginated,
    best_bid_ask,
    executable_size,
    fetch_event_by_slug,
    fetch_order_book,
    fetch_positions,
    fetch_tournament_markets,
)


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def test_get_retries_then_succeeds(monkeypatch):
    calls = {"n": 0}

    def fake_get(url, params=None, timeout=None):
        calls["n"] += 1
        if calls["n"] < 3:
            raise httpx.HTTPError("boom")
        return FakeResponse({"ok": True})

    monkeypatch.setattr(httpx, "get", fake_get)
    monkeypatch.setattr("evhedge.data_sources.polymarket.time.sleep", lambda s: None)

    result = _get("https://example.test", retries=2)
    assert result == {"ok": True}
    assert calls["n"] == 3


def test_get_raises_polymarket_api_error_after_exhausting_retries(monkeypatch):
    def fake_get(url, params=None, timeout=None):
        raise httpx.HTTPError("always fails")

    monkeypatch.setattr(httpx, "get", fake_get)
    monkeypatch.setattr("evhedge.data_sources.polymarket.time.sleep", lambda s: None)

    with pytest.raises(PolymarketAPIError, match="example.test"):
        _get("https://example.test", retries=2)


def test_get_paginated_stops_on_short_batch(monkeypatch):
    pages = [
        [{"id": i} for i in range(100)],
        [{"id": i} for i in range(100, 150)],  # short batch -> last page
    ]
    calls = {"n": 0}

    def fake_get_paginated(url, params=None, timeout=None, retries=None):
        idx = calls["n"]
        calls["n"] += 1
        return pages[idx]

    monkeypatch.setattr("evhedge.data_sources.polymarket._get", fake_get_paginated)

    results = _get_paginated("https://example.test/events", {"tag_slug": "cs2"})
    assert len(results) == 150
    assert calls["n"] == 2


def test_get_paginated_stops_on_empty_batch(monkeypatch):
    def fake_get_paginated(url, params=None, timeout=None, retries=None):
        return []

    monkeypatch.setattr("evhedge.data_sources.polymarket._get", fake_get_paginated)

    results = _get_paginated("https://example.test/events", {"tag_slug": "cs2"})
    assert results == []


def test_fetch_tournament_markets_uses_correct_params(monkeypatch):
    captured = {}

    def fake_get_paginated(url, params):
        captured["url"] = url
        captured["params"] = params
        return [{"slug": "some-event"}]

    monkeypatch.setattr("evhedge.data_sources.polymarket._get_paginated", fake_get_paginated)

    result = fetch_tournament_markets("cs2")
    assert result == [{"slug": "some-event"}]
    assert captured["params"] == {"tag_slug": "cs2", "closed": "false"}
    assert captured["url"].endswith("/events")


def test_fetch_event_by_slug_returns_none_when_empty(monkeypatch):
    monkeypatch.setattr("evhedge.data_sources.polymarket._get", lambda url, params=None: [])
    assert fetch_event_by_slug("does-not-exist") is None


def test_fetch_event_by_slug_returns_first_match(monkeypatch):
    monkeypatch.setattr(
        "evhedge.data_sources.polymarket._get", lambda url, params=None: [{"slug": "x"}]
    )
    assert fetch_event_by_slug("x") == {"slug": "x"}


def test_fetch_positions_passes_address_and_returns_raw_list(monkeypatch):
    captured = {}

    def fake_get(url, params=None):
        captured["url"] = url
        captured["params"] = params
        return [{"title": "T", "outcome": "Yes", "size": 10.0, "asset": "tok1"}]

    monkeypatch.setattr("evhedge.data_sources.polymarket._get", fake_get)

    positions = fetch_positions("0xabc")
    assert positions == [{"title": "T", "outcome": "Yes", "size": 10.0, "asset": "tok1"}]
    assert captured["url"].endswith("/positions")
    assert captured["params"] == {"user": "0xabc", "limit": 500}


def test_fetch_positions_raises_on_bad_shape(monkeypatch):
    monkeypatch.setattr("evhedge.data_sources.polymarket._get", lambda url, params=None: {"not": "a list"})
    with pytest.raises(PolymarketAPIError, match="неожиданный формат"):
        fetch_positions("0xabc")


def test_fetch_order_book_parses_levels(monkeypatch):
    payload = {
        "bids": [{"price": "0.40", "size": "100"}, {"price": "0.39", "size": "50"}],
        "asks": [{"price": "0.42", "size": "200"}, {"price": "0.44", "size": "300"}],
    }
    monkeypatch.setattr("evhedge.data_sources.polymarket._get", lambda url, params=None: payload)

    book = fetch_order_book("token123")
    assert book.token_id == "token123"
    assert book.bids == [BookLevel(0.40, 100.0), BookLevel(0.39, 50.0)]
    assert book.asks == [BookLevel(0.42, 200.0), BookLevel(0.44, 300.0)]


def test_fetch_order_book_raises_on_bad_shape(monkeypatch):
    monkeypatch.setattr("evhedge.data_sources.polymarket._get", lambda url, params=None: ["not", "a", "dict"])
    with pytest.raises(PolymarketAPIError, match="token_id=badtoken"):
        fetch_order_book("badtoken")


def test_fetch_order_book_raises_on_unparseable_levels(monkeypatch):
    monkeypatch.setattr(
        "evhedge.data_sources.polymarket._get",
        lambda url, params=None: {"bids": [{"price": "oops"}], "asks": []},
    )
    with pytest.raises(PolymarketAPIError):
        fetch_order_book("token123")


# --- best_bid_ask: real, independently-traded top-of-book -------------------

def test_best_bid_ask_returns_top_of_book():
    book = OrderBook(
        token_id="t",
        bids=[BookLevel(0.40, 100.0), BookLevel(0.35, 500.0)],
        asks=[BookLevel(0.42, 200.0), BookLevel(0.44, 300.0)],
    )
    bid, ask = best_bid_ask(book)
    assert bid == pytest.approx(0.40)
    assert ask == pytest.approx(0.42)


def test_best_bid_ask_none_for_empty_side():
    book = OrderBook(token_id="t", bids=[BookLevel(0.40, 100.0)], asks=[])
    bid, ask = best_bid_ask(book)
    assert bid == pytest.approx(0.40)
    assert ask is None


def test_best_bid_ask_both_none_for_empty_book():
    assert best_bid_ask(OrderBook(token_id="t")) == (None, None)


# --- executable_size: the "board shows 2.4c, real asks start at 3.1c" case ---

def test_executable_size_buy_walks_asks_ascending():
    book = OrderBook(
        token_id="t",
        asks=[BookLevel(0.031, 0.27 / 0.031), BookLevel(0.05, 1000.0), BookLevel(0.08, 5000.0)],
    )
    # Only the first two levels are within worst_price=0.05.
    usd, avg_price = executable_size(book, "buy", worst_price=0.05)
    assert usd == pytest.approx(0.27 + 0.05 * 1000.0)
    assert avg_price == pytest.approx(usd / (0.27 / 0.031 + 1000.0))


def test_executable_size_buy_worst_price_below_best_ask_is_zero():
    """The board's last-traded price (2.4c) is NOT an executable ask -- if
    the real book starts at 3.1c, asking for 2.4c or better executes
    nothing."""
    book = OrderBook(token_id="t", asks=[BookLevel(0.031, 100.0), BookLevel(0.05, 1000.0)])
    usd, avg_price = executable_size(book, "buy", worst_price=0.024)
    assert usd == 0.0
    assert avg_price is None


def test_executable_size_sell_walks_bids_descending():
    book = OrderBook(token_id="t", bids=[BookLevel(0.40, 100.0), BookLevel(0.35, 500.0)])
    usd, avg_price = executable_size(book, "sell", worst_price=0.35)
    assert usd == pytest.approx(0.40 * 100.0 + 0.35 * 500.0)
    assert avg_price == pytest.approx(usd / 600.0)


def test_executable_size_invalid_side_raises():
    book = OrderBook(token_id="t")
    with pytest.raises(ValueError, match="side must be"):
        executable_size(book, "hold", worst_price=0.5)
