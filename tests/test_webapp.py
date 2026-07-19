"""Tests for evhedge.webapp: the pure JSON-payload builders directly, and
the actual HTTP handler end-to-end (real ThreadingHTTPServer on an
ephemeral port, real requests) -- no live Polymarket calls in either
case, ``polymarket_ds`` functions are monkeypatched."""

import json
import threading
import urllib.error
import urllib.request

import pytest

from evhedge.data_sources.polymarket import (
    BookLevel,
    OrderBook,
    PolymarketAPIError,
)
from evhedge.webapp import DashboardHandler, book_payload, positions_payload
from http.server import ThreadingHTTPServer


# --- pure payload builders ---------------------------------------------------------

def test_positions_payload_trims_to_known_fields(monkeypatch):
    raw = [{
        "title": "T", "outcome": "Yes", "eventSlug": "e", "slug": "s",
        "size": 10.0, "avgPrice": 0.5, "initialValue": 5.0, "currentValue": 6.0,
        "curPrice": 0.6, "asset": "tok1", "somethingElseEntirely": "ignored",
    }]
    monkeypatch.setattr("evhedge.webapp.polymarket_ds.fetch_positions", lambda addr, limit=500: raw)

    payload = positions_payload("0xabc")
    assert payload == [{
        "title": "T", "outcome": "Yes", "eventSlug": "e", "slug": "s",
        "size": 10.0, "avgPrice": 0.5, "initialValue": 5.0, "currentValue": 6.0,
        "curPrice": 0.6, "asset": "tok1",
    }]


def test_book_payload_uses_best_bid_ask(monkeypatch):
    book = OrderBook("tok1", bids=[BookLevel(0.40, 10)], asks=[BookLevel(0.42, 10)])
    monkeypatch.setattr("evhedge.webapp.polymarket_ds.fetch_order_book", lambda token_id: book)

    assert book_payload("tok1") == {"bid": 0.40, "ask": 0.42}


# --- real HTTP server, real requests -------------------------------------------------

@pytest.fixture
def running_server(monkeypatch):
    server = ThreadingHTTPServer(("127.0.0.1", 0), DashboardHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _get_json(url):
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_server_serves_index_html(running_server):
    with urllib.request.urlopen(running_server + "/", timeout=5) as resp:
        assert resp.status == 200
        assert "text/html" in resp.headers["Content-Type"]
        body = resp.read().decode("utf-8")
        assert "Roll-Over" in body


def test_server_positions_endpoint_requires_address(running_server):
    status, body = _get_json(running_server + "/api/positions")
    assert status == 400
    assert "address" in body["error"]


def test_server_positions_endpoint_returns_data(running_server, monkeypatch):
    monkeypatch.setattr(
        "evhedge.webapp.polymarket_ds.fetch_positions",
        lambda addr, limit=500: [{"title": "T", "outcome": "Yes", "size": 1.0, "asset": "tok1",
                                   "avgPrice": 0.5, "initialValue": 0.5, "currentValue": 0.5,
                                   "curPrice": 0.5, "eventSlug": "e", "slug": "s"}],
    )
    status, body = _get_json(running_server + "/api/positions?address=0xabc")
    assert status == 200
    assert body == [{"title": "T", "outcome": "Yes", "eventSlug": "e", "slug": "s",
                      "size": 1.0, "avgPrice": 0.5, "initialValue": 0.5, "currentValue": 0.5,
                      "curPrice": 0.5, "asset": "tok1"}]


def test_server_positions_endpoint_maps_api_error_to_502(running_server, monkeypatch):
    def boom(addr, limit=500):
        raise PolymarketAPIError("boom")

    monkeypatch.setattr("evhedge.webapp.polymarket_ds.fetch_positions", boom)
    status, body = _get_json(running_server + "/api/positions?address=0xabc")
    assert status == 502
    assert body["error"] == "boom"


def test_server_book_endpoint_requires_token_id(running_server):
    status, body = _get_json(running_server + "/api/book")
    assert status == 400
    assert "token_id" in body["error"]


def test_server_book_endpoint_returns_data(running_server, monkeypatch):
    book = OrderBook("tok1", bids=[BookLevel(0.30, 5)], asks=[BookLevel(0.32, 5)])
    monkeypatch.setattr("evhedge.webapp.polymarket_ds.fetch_order_book", lambda token_id: book)
    status, body = _get_json(running_server + "/api/book?token_id=tok1")
    assert status == 200
    assert body == {"bid": 0.30, "ask": 0.32}


def test_server_unknown_route_404s(running_server):
    status, body = _get_json(running_server + "/nope")
    assert status == 404


def test_server_path_traversal_is_rejected(running_server):
    status, body = _get_json(running_server + "/../../etc/passwd")
    assert status == 404
