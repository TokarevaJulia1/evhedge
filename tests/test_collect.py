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
        (yandex_no,) = store.snapshots("EWC 2026 Dota 2", team="Team Yandex", market="winner_no")
        assert yandex_no.price_pct == pytest.approx(77.5)
        assert yandex_no.token_id == "tokN"


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


def test_collect_match_markets_legs_and_resolves(tmp_path, monkeypatch):
    def fake_fetch(tag_slug, closed=False, start_date_min=None):
        return [CLOSED_MATCH] if closed else [OPEN_MATCH, OTHER_TOURNAMENT]

    monkeypatch.setattr("evhedge.collect.polymarket_ds.fetch_tournament_markets", fake_fetch)
    with Storage(tmp_path / "e.db") as store:
        summary = collect_match_markets(
            store, "EWC 2026 Dota 2", "dota-2", "Esports World Cup"
        )

        # open Match Winner -> one leg snapshot, team A vs counterparty B
        (leg,) = store.snapshots("EWC 2026 Dota 2", market="leg")
        assert leg.team == "Team Falcons"
        assert leg.counterparty == "BetBoom Team"
        assert leg.price_pct == pytest.approx(41.5)
        assert leg.token_id == "tokA"

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
        assert [l.team for l in legs] == ["Team Falcons"]  # only the pre-match one
