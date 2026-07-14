"""Tests for evhedge.collect (Gamma -> storage collection), fixtures
shaped exactly like real Gamma responses (string-encoded JSON fields,
string volume) -- see the live EWC Dota 2 board."""

import json

import pytest

from evhedge.collect import (
    CollectError,
    collect_board,
    collect_match_markets,
)
from evhedge.data_sources.polymarket import PolymarketAPIError
from evhedge.storage import Storage


def _market(team, yes, no, volume="1000", outcomes=("Yes", "No"), closed=False,
            group_title=None, tokens=("tokY", "tokN")):
    return {
        "groupItemTitle": group_title if group_title is not None else team,
        "question": f"Will {team} win?",
        "outcomes": json.dumps(list(outcomes)),
        "outcomePrices": json.dumps([str(yes), str(no)]),
        "clobTokenIds": json.dumps(list(tokens)),
        "volume": volume,
        "closed": closed,
    }


WINNER_EVENT = {
    "slug": "ewc-dota-2-winner",
    "title": "EWC Dota 2 Winner",
    "markets": [
        _market("Team Yandex", 0.225, 0.775),
        _market("PARIVISION", 0.14, 0.86),
        # untraded placeholder slot: 0.5/0.5, zero volume -> skipped
        _market("A", 0.5, 0.5, volume="0"),
        # weird shape -> skipped_shape
        _market("Weird", 0.3, 0.7, outcomes=("Over", "Under")),
    ],
}


def test_collect_board_snapshots_yes_and_no_sides(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "evhedge.collect.polymarket_ds.fetch_event_by_slug",
        lambda slug: WINNER_EVENT,
    )
    with Storage(tmp_path / "e.db") as store:
        summary = collect_board(store, "EWC 2026 Dota 2", "ewc-dota-2-winner", "winner")

        assert summary.markets_seen == 4
        assert summary.snapshots_written == 4  # 2 teams x yes/no
        assert summary.skipped_placeholders == 1
        assert summary.skipped_shape == 1

        (yandex_yes,) = store.snapshots("EWC 2026 Dota 2", team="Team Yandex", market="winner_yes")
        assert yandex_yes.price_pct == pytest.approx(22.5)
        assert yandex_yes.source == "board"
        assert yandex_yes.token_id == "tokY"
        assert yandex_yes.volume_usd == pytest.approx(1000.0)
        assert yandex_yes.bid_pct is None and yandex_yes.ask_pct is None
        (yandex_no,) = store.snapshots("EWC 2026 Dota 2", team="Team Yandex", market="winner_no")
        assert yandex_no.price_pct == pytest.approx(77.5)
        assert yandex_no.token_id == "tokN"
        assert yandex_no.volume_usd == pytest.approx(1000.0)


def test_collect_board_verify_book_uses_real_bid_ask(tmp_path, monkeypatch):
    """winner_no must NOT just be 100 - yes: with verify_book=True, both
    sides come from the real order book (which need not be complementary),
    and source is "book", not "board"."""
    from evhedge.data_sources.polymarket import BookLevel, OrderBook

    monkeypatch.setattr(
        "evhedge.collect.polymarket_ds.fetch_event_by_slug", lambda slug: WINNER_EVENT,
    )

    books = {
        "tokY": OrderBook("tokY", bids=[BookLevel(0.215, 100)], asks=[BookLevel(0.225, 50)]),
        "tokN": OrderBook("tokN", bids=[BookLevel(0.76, 100)], asks=[BookLevel(0.78, 50)]),
    }

    def fake_fetch_order_book(token_id):
        return books[token_id]

    monkeypatch.setattr("evhedge.collect.polymarket_ds.fetch_order_book", fake_fetch_order_book)

    with Storage(tmp_path / "e.db") as store:
        summary = collect_board(
            store, "EWC 2026 Dota 2", "ewc-dota-2-winner", "winner", verify_book=True,
        )
        assert summary.book_fallback_to_board == 0

        (yes,) = store.snapshots("EWC 2026 Dota 2", team="Team Yandex", market="winner_yes")
        (no,) = store.snapshots("EWC 2026 Dota 2", team="Team Yandex", market="winner_no")

        assert yes.source == "book"
        assert yes.bid_pct == pytest.approx(21.5)
        assert yes.ask_pct == pytest.approx(22.5)
        assert yes.price_pct == pytest.approx(22.5)  # tradable buy price = ask

        assert no.source == "book"
        assert no.bid_pct == pytest.approx(76.0)
        assert no.ask_pct == pytest.approx(78.0)
        # yes.ask (22.5) + no.ask (78.0) != 100 -- the whole point of the fix
        assert yes.ask_pct + no.ask_pct != pytest.approx(100.0)


def test_collect_board_verify_book_falls_back_on_api_error(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "evhedge.collect.polymarket_ds.fetch_event_by_slug", lambda slug: WINNER_EVENT,
    )

    def fake_fetch_order_book(token_id):
        raise PolymarketAPIError("network down")

    monkeypatch.setattr("evhedge.collect.polymarket_ds.fetch_order_book", fake_fetch_order_book)

    with Storage(tmp_path / "e.db") as store:
        summary = collect_board(
            store, "EWC 2026 Dota 2", "ewc-dota-2-winner", "winner", verify_book=True,
        )
        # 2 teams x yes/no = 4 sides, all fell back
        assert summary.book_fallback_to_board == 4
        (yes,) = store.snapshots("EWC 2026 Dota 2", team="Team Yandex", market="winner_yes")
        assert yes.source == "board"
        assert yes.price_pct == pytest.approx(22.5)


def test_collect_board_skips_unquotable_extremes(tmp_path, monkeypatch):
    event = {"slug": "s", "title": "t", "markets": [_market("Settled", 0.0, 1.0)]}
    monkeypatch.setattr("evhedge.collect.polymarket_ds.fetch_event_by_slug", lambda slug: event)
    with Storage(tmp_path / "e.db") as store:
        summary = collect_board(store, "T", "s", "winner")
        assert summary.snapshots_written == 0
        assert summary.skipped_price_range == 2  # both 0% and 100% sides


def test_collect_board_missing_event_raises(tmp_path, monkeypatch):
    monkeypatch.setattr("evhedge.collect.polymarket_ds.fetch_event_by_slug", lambda slug: None)
    with Storage(tmp_path / "e.db") as store:
        with pytest.raises(CollectError, match="не найдено"):
            collect_board(store, "T", "ghost", "winner")


# --- match markets ---------------------------------------------------------------

OPEN_MATCH = {
    "slug": "dota2-flc-bb4",
    "title": "Dota 2: Team Falcons vs BetBoom Team (BO2) - Esports World Cup Group A",
    "markets": [
        _market("series", 0.415, 0.585, outcomes=("Team Falcons", "BetBoom Team"),
                group_title="Match Winner", tokens=("tokA", "tokB")),
        # open per-game market: not a series price, not collected while open
        _market("g1", 0.47, 0.53, outcomes=("Team Falcons", "BetBoom Team"),
                group_title="Game 1 Winner"),
        # prop: never a result market
        _market("prop", 0.51, 0.49, outcomes=("Yes", "No"), group_title="Ends in Daytime"),
    ],
}

CLOSED_MATCH = {
    "slug": "dota2-re-xtreme",
    "title": "Dota 2: Rune Eaters vs Xtreme Gaming (BO2) - Esports World Cup Group A",
    "markets": [
        _market("g1", 0.0, 1.0, outcomes=("Rune Eaters", "Xtreme Gaming"),
                group_title="Game 1 Winner", closed=True),
        # Bo2 draw: series settles 0.5/0.5 -> honestly skipped, not guessed
        _market("series", 0.5, 0.5, outcomes=("Rune Eaters", "Xtreme Gaming"),
                group_title="Match Winner", closed=True),
    ],
}

OTHER_TOURNAMENT = {
    "slug": "dota2-other",
    "title": "Dota 2: X vs Y (BO3) - DreamLeague",
    "markets": [
        _market("series", 0.5, 0.5, outcomes=("X", "Y"), group_title="Match Winner"),
    ],
}

# Match that has gone live (Gamma live flag): pre-match series is over,
# its price must NOT be recorded.
LIVE_MATCH = {
    "slug": "dota2-ts8-mouz",
    "title": "Dota 2: Team Spirit vs MOUZ (BO2) - Esports World Cup Group C",
    "live": True,
    "markets": [
        _market("series", 0.8, 0.2, outcomes=("Team Spirit", "MOUZ"),
                group_title="Match Winner"),
    ],
}

# Live flag lagging, but startTime already passed: belt-and-braces skip.
STARTED_MATCH = {
    "slug": "dota2-rnx-vg",
    "title": "Dota 2: REKONIX vs Vici Gaming (BO2) - Esports World Cup Group C",
    "live": False,
    "startTime": "2020-01-01T00:00:00Z",
    "markets": [
        _market("series", 0.1, 0.9, outcomes=("REKONIX", "Vici Gaming"),
                group_title="Match Winner"),
    ],
}


def test_collect_match_markets_verify_book_uses_real_bid_ask(tmp_path, monkeypatch):
    from evhedge.data_sources.polymarket import BookLevel, OrderBook

    def fake_fetch(tag_slug, closed=False, start_date_min=None):
        return [] if closed else [OPEN_MATCH]

    monkeypatch.setattr("evhedge.collect.polymarket_ds.fetch_tournament_markets", fake_fetch)

    books = {
        "tokA": OrderBook("tokA", bids=[BookLevel(0.40, 100)], asks=[BookLevel(0.42, 50)]),
        "tokB": OrderBook("tokB", bids=[BookLevel(0.57, 100)], asks=[BookLevel(0.60, 50)]),
    }
    monkeypatch.setattr(
        "evhedge.collect.polymarket_ds.fetch_order_book", lambda token_id: books[token_id],
    )

    with Storage(tmp_path / "e.db") as store:
        summary = collect_match_markets(
            store, "EWC 2026 Dota 2", "dota-2", "Esports World Cup", verify_book=True,
        )
        assert summary.book_fallback_to_board == 0

        legs = {leg.team: leg for leg in store.snapshots("EWC 2026 Dota 2", market="leg")}
        assert legs["Team Falcons"].source == "book"
        assert legs["Team Falcons"].ask_pct == pytest.approx(42.0)
        assert legs["BetBoom Team"].ask_pct == pytest.approx(60.0)
        # both asks come from independently-quoted books -- no forced complement
        assert legs["Team Falcons"].ask_pct + legs["BetBoom Team"].ask_pct != pytest.approx(100.0)


def test_collect_match_markets_legs_and_resolves(tmp_path, monkeypatch):
    def fake_fetch(tag_slug, closed=False, start_date_min=None):
        return [CLOSED_MATCH] if closed else [OPEN_MATCH, OTHER_TOURNAMENT]

    monkeypatch.setattr("evhedge.collect.polymarket_ds.fetch_tournament_markets", fake_fetch)
    with Storage(tmp_path / "e.db") as store:
        summary = collect_match_markets(
            store, "EWC 2026 Dota 2", "dota-2", "Esports World Cup"
        )

        # open Match Winner -> TWO leg snapshots, both directions.
        legs = store.snapshots("EWC 2026 Dota 2", market="leg")
        by_team = {leg.team: leg for leg in legs}
        assert set(by_team) == {"Team Falcons", "BetBoom Team"}

        falcons = by_team["Team Falcons"]
        assert falcons.counterparty == "BetBoom Team"
        assert falcons.price_pct == pytest.approx(41.5)
        assert falcons.token_id == "tokA"
        assert falcons.volume_usd == pytest.approx(1000.0)
        assert falcons.source == "board"
        assert falcons.bid_pct is None and falcons.ask_pct is None

        betboom = by_team["BetBoom Team"]
        assert betboom.counterparty == "Team Falcons"
        assert betboom.price_pct == pytest.approx(58.5)
        assert betboom.token_id == "tokB"

        # closed Game 1 -> resolves for both teams; drawn series -> skipped
        resolves = store.resolves("EWC 2026 Dota 2")
        assert {(r.team, r.outcome) for r in resolves} == {
            ("Xtreme Gaming", "yes"), ("Rune Eaters", "no"),
        }
        assert summary.resolves_written == 2
        assert summary.skipped_unresolved == 1
        # DreamLeague filtered out entirely
        assert all("DreamLeague" not in (r.note or "") for r in resolves)


def test_collect_match_markets_skips_live_matches(tmp_path, monkeypatch):
    """Once a match is live (flag OR clock past startTime), its pre-match
    series is complete -- no more leg snapshots for it."""

    def fake_fetch(tag_slug, closed=False, start_date_min=None):
        return [] if closed else [OPEN_MATCH, LIVE_MATCH, STARTED_MATCH]

    monkeypatch.setattr("evhedge.collect.polymarket_ds.fetch_tournament_markets", fake_fetch)
    with Storage(tmp_path / "e.db") as store:
        summary = collect_match_markets(
            store, "EWC 2026 Dota 2", "dota-2", "Esports World Cup"
        )

        assert summary.skipped_live == 2
        legs = store.snapshots("EWC 2026 Dota 2", market="leg")
        # only the pre-match OPEN_MATCH produced legs (both directions)
        assert {l.team for l in legs} == {"Team Falcons", "BetBoom Team"}


# --- auto_predict integration (collector-triggered predictions) -----------------------

def test_auto_predict_trigger_fires_on_first_quality_book(tmp_path, monkeypatch):
    """Real EWC-shaped sequence: a 4.0/96.0 listing placeholder, then a
    settling-but-still-wide 40.0/46.0 (6pp), then a live 59.0/60.5
    (1.5pp) -- only the third snapshot fires the trigger, and exactly
    one prediction row is recorded, priced off THAT snapshot. BetBoom's
    own book errors throughout (stays on board fallback) so it never
    triggers -- isolates the assertion to one row, as the task fixture
    describes."""
    from evhedge.data_sources.polymarket import BookLevel, OrderBook, PolymarketAPIError

    def fake_fetch(tag_slug, closed=False, start_date_min=None):
        return [] if closed else [OPEN_MATCH]

    monkeypatch.setattr("evhedge.collect.polymarket_ds.fetch_tournament_markets", fake_fetch)

    falcons_books = [
        OrderBook("tokA", bids=[BookLevel(0.04, 10)], asks=[BookLevel(0.96, 10)]),   # listing, 92pp
        OrderBook("tokA", bids=[BookLevel(0.40, 10)], asks=[BookLevel(0.46, 10)]),   # settling, 6pp
        OrderBook("tokA", bids=[BookLevel(0.59, 10)], asks=[BookLevel(0.605, 10)]),  # live, 1.5pp -> fires
    ]

    with Storage(tmp_path / "e.db") as store:
        for book in falcons_books:
            def fake_order_book(token_id, _book=book):
                if token_id == "tokA":
                    return _book
                raise PolymarketAPIError("BetBoom book unavailable")

            monkeypatch.setattr("evhedge.collect.polymarket_ds.fetch_order_book", fake_order_book)
            collect_match_markets(
                store, "EWC 2026 Dota 2", "dota-2", "Esports World Cup", verify_book=True,
            )

        preds = store.predictions(tournament="EWC 2026 Dota 2")
        assert len(preds) == 1
        (pred,) = preds
        assert pred.team == "Team Falcons"
        assert pred.market == "result:dota2-flc-bb4:Match Winner"
        assert pred.p_market_bid == pytest.approx(0.59)
        assert pred.p_market_ask == pytest.approx(0.605)


def test_auto_predict_idempotent_on_repeat_pass(tmp_path, monkeypatch):
    """A second collector pass over the same already-triggered market
    writes zero new rows and raises nothing -- the UNIQUE-skip is the
    expected steady state, not an error."""
    from evhedge.data_sources.polymarket import BookLevel, OrderBook

    def fake_fetch(tag_slug, closed=False, start_date_min=None):
        return [] if closed else [OPEN_MATCH]

    monkeypatch.setattr("evhedge.collect.polymarket_ds.fetch_tournament_markets", fake_fetch)

    books = {
        "tokA": OrderBook("tokA", bids=[BookLevel(0.59, 10)], asks=[BookLevel(0.605, 10)]),
        "tokB": OrderBook("tokB", bids=[BookLevel(0.395, 10)], asks=[BookLevel(0.41, 10)]),
    }
    monkeypatch.setattr(
        "evhedge.collect.polymarket_ds.fetch_order_book", lambda token_id: books[token_id],
    )

    with Storage(tmp_path / "e.db") as store:
        summary1 = collect_match_markets(
            store, "EWC 2026 Dota 2", "dota-2", "Esports World Cup", verify_book=True,
        )
        assert summary1.predictions_written == 2  # both directions trigger
        assert summary1.predictions_skipped_duplicate == 0

        summary2 = collect_match_markets(
            store, "EWC 2026 Dota 2", "dota-2", "Esports World Cup", verify_book=True,
        )
        assert summary2.predictions_written == 0
        assert summary2.predictions_skipped_duplicate == 2

        assert len(store.predictions(tournament="EWC 2026 Dota 2")) == 2


def test_auto_predict_heterogeneous_n_writes_null_model(tmp_path, monkeypatch):
    from evhedge.data_sources.polymarket import BookLevel, OrderBook

    def fake_fetch(tag_slug, closed=False, start_date_min=None):
        return [] if closed else [OPEN_MATCH]

    monkeypatch.setattr("evhedge.collect.polymarket_ds.fetch_tournament_markets", fake_fetch)

    books = {
        "tokA": OrderBook("tokA", bids=[BookLevel(0.59, 10)], asks=[BookLevel(0.605, 10)]),
        "tokB": OrderBook("tokB", bids=[BookLevel(0.395, 10)], asks=[BookLevel(0.41, 10)]),
    }
    monkeypatch.setattr(
        "evhedge.collect.polymarket_ds.fetch_order_book", lambda token_id: books[token_id],
    )

    with Storage(tmp_path / "e.db") as store:
        summary = collect_match_markets(
            store, "EWC 2026 Dota 2", "dota-2", "Esports World Cup", verify_book=True,
            stage_ranks={"Team Falcons": 2, "BetBoom Team": 3},  # heterogeneous
        )
        assert summary.predictions_written == 2
        assert summary.predictions_model_null == 2
        for pred in store.predictions(tournament="EWC 2026 Dota 2"):
            assert pred.p_model is None
            assert "p_model=NULL" in pred.note


def test_auto_predict_no_prediction_for_live_match(tmp_path, monkeypatch):
    from evhedge.data_sources.polymarket import BookLevel, OrderBook

    def fake_fetch(tag_slug, closed=False, start_date_min=None):
        return [] if closed else [LIVE_MATCH]

    monkeypatch.setattr("evhedge.collect.polymarket_ds.fetch_tournament_markets", fake_fetch)
    monkeypatch.setattr(
        "evhedge.collect.polymarket_ds.fetch_order_book",
        lambda token_id: OrderBook(token_id, bids=[BookLevel(0.59, 10)], asks=[BookLevel(0.605, 10)]),
    )

    with Storage(tmp_path / "e.db") as store:
        summary = collect_match_markets(
            store, "EWC 2026 Dota 2", "dota-2", "Esports World Cup", verify_book=True,
        )
        assert summary.predictions_written == 0
        assert store.predictions(tournament="EWC 2026 Dota 2") == []
