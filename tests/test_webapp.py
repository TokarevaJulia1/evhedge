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
from evhedge.webapp import DashboardHandler, book_payload, bracket_payload, positions_payload
from http.server import ThreadingHTTPServer


# --- pure payload builders ---------------------------------------------------------

def test_positions_payload_trims_to_known_fields(monkeypatch):
    raw = [{
        "title": "T", "outcome": "Yes", "eventSlug": "e", "slug": "s",
        "size": 10.0, "avgPrice": 0.5, "initialValue": 5.0, "currentValue": 6.0,
        "curPrice": 0.6, "asset": "tok1", "redeemable": False, "somethingElseEntirely": "ignored",
    }]
    monkeypatch.setattr("evhedge.webapp.polymarket_ds.fetch_positions", lambda addr, limit=500: raw)

    payload = positions_payload("0xabc")
    assert payload == [{
        "title": "T", "outcome": "Yes", "eventSlug": "e", "slug": "s",
        "size": 10.0, "avgPrice": 0.5, "initialValue": 5.0, "currentValue": 6.0,
        "curPrice": 0.6, "asset": "tok1", "redeemable": False,
    }]


def test_positions_payload_surfaces_redeemable_resolved_positions(monkeypatch):
    """A market that resolved but wasn't redeemed yet still shows up as
    a position (real behavior, confirmed live) -- redeemable=True is the
    signal the dashboard uses to badge it as no longer a LIVE position."""
    raw = [{
        "title": "Dota 2: Team Yandex vs PARIVISION (BO3) - Esports World Cup Playoffs",
        "outcome": "Team Yandex", "eventSlug": "e", "slug": "s",
        "size": 1233.9931, "avgPrice": 0.53, "initialValue": 654.0163,
        "currentValue": 0, "curPrice": 0, "asset": "tok1", "redeemable": True,
    }]
    monkeypatch.setattr("evhedge.webapp.polymarket_ds.fetch_positions", lambda addr, limit=500: raw)

    (payload,) = positions_payload("0xabc")
    assert payload["redeemable"] is True
    assert payload["curPrice"] == 0


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
                                   "curPrice": 0.5, "eventSlug": "e", "slug": "s", "redeemable": False}],
    )
    status, body = _get_json(running_server + "/api/positions?address=0xabc")
    assert status == 200
    assert body == [{"title": "T", "outcome": "Yes", "eventSlug": "e", "slug": "s",
                      "size": 1.0, "avgPrice": 0.5, "initialValue": 0.5, "currentValue": 0.5,
                      "curPrice": 0.5, "asset": "tok1", "redeemable": False}]


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


# --- bracket_payload / /api/bracket ---------------------------------------------------

_REAL_BRACKET_FIXTURE = [
    {
        "id": 1, "name": "Round of 32 match 2: TS vs OG", "status": "not_started",
        "scheduled_at": "2026-07-23T15:00:00Z", "number_of_games": 3,
        "opponents": [
            {"opponent": {"id": 1, "name": "Spirit"}},   # PandaScore's short spelling
            {"opponent": {"id": 2, "name": "OG"}},
        ],
    },
    {
        "id": 2, "name": "Round of 16 match 1: TBD vs TBD", "status": "not_started",
        "scheduled_at": "2026-07-26T10:00:00Z", "number_of_games": 3,
        "opponents": [],
    },
]


def test_bracket_payload_canonicalizes_and_flags_tbd(monkeypatch):
    monkeypatch.setattr(
        "evhedge.webapp.pandascore_ds.fetch_tournament_brackets",
        lambda tid, budget: _REAL_BRACKET_FIXTURE,
    )
    rows = bracket_payload(21474)
    assert rows[0]["team_a"] == "Team Spirit"  # canonicalized, same alias map as collect.py
    assert rows[0]["team_b"] == "OG"
    assert rows[0]["tbd"] is False
    assert rows[1]["team_a"] == "TBD" and rows[1]["team_b"] == "TBD"
    assert rows[1]["tbd"] is True
    assert rows[1]["scheduled_at"] == "2026-07-26T10:00:00Z"


def test_server_bracket_endpoint_requires_tournament_id(running_server):
    status, body = _get_json(running_server + "/api/bracket")
    assert status == 400
    assert "tournament_id" in body["error"]


def test_server_bracket_endpoint_rejects_non_numeric_id(running_server):
    status, body = _get_json(running_server + "/api/bracket?tournament_id=abc")
    assert status == 400


def test_server_bracket_endpoint_returns_data(running_server, monkeypatch):
    monkeypatch.setattr(
        "evhedge.webapp.pandascore_ds.fetch_tournament_brackets",
        lambda tid, budget: _REAL_BRACKET_FIXTURE,
    )
    status, body = _get_json(running_server + "/api/bracket?tournament_id=21474")
    assert status == 200
    assert len(body) == 2
    assert body[0]["team_a"] == "Team Spirit"


def test_server_bracket_endpoint_maps_pandascore_error_to_502(running_server, monkeypatch):
    from evhedge.data_sources.pandascore import PandaScoreError

    def boom(tid, budget):
        raise PandaScoreError("boom")

    monkeypatch.setattr("evhedge.webapp.pandascore_ds.fetch_tournament_brackets", boom)
    status, body = _get_json(running_server + "/api/bracket?tournament_id=21474")
    assert status == 502
    assert body["error"] == "boom"
