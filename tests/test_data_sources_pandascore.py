"""Tests for evhedge.data_sources.pandascore.

No real network calls: httpx.get is monkeypatched everywhere. Covers
pagination (Link header), the rate-limit budget, 429 backoff, and the
param shapes for each list endpoint -- not live API behavior (that was
verified once, live, with a real token, while building this module; see
CHANGELOG.md / the commit message for what was actually observed).
"""

import httpx
import pytest

from evhedge.data_sources.pandascore import (
    PandaScoreError,
    PandaScoreRateLimitError,
    RequestBudget,
    _get,
    _get_list,
    _parse_link_header,
    fetch_matches,
    fetch_series,
    fetch_teams,
)


class FakeResponse:
    def __init__(self, payload, status_code=200, headers=None):
        self._payload = payload
        self.status_code = status_code
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("boom", request=None, response=self)

    def json(self):
        return self._payload


@pytest.fixture(autouse=True)
def token(monkeypatch):
    monkeypatch.setenv("PANDASCORE_TOKEN", "test-token")


# --- auth --------------------------------------------------------------------------

def test_missing_token_raises_clear_error(monkeypatch):
    monkeypatch.delenv("PANDASCORE_TOKEN", raising=False)
    with pytest.raises(PandaScoreError, match="PANDASCORE_TOKEN"):
        _get("https://example.test", budget=RequestBudget())


def test_401_raises_clear_error(monkeypatch):
    monkeypatch.setattr(httpx, "get", lambda *a, **k: FakeResponse({"error": "Invalid credentials"}, 401))
    with pytest.raises(PandaScoreError, match="401"):
        _get("https://example.test", budget=RequestBudget())


# --- Link header pagination -----------------------------------------------------

def test_parse_link_header_extracts_next_and_last():
    header = (
        '<https://api.pandascore.co/csgo/matches/past?page=2>; rel="next", '
        '<https://api.pandascore.co/csgo/matches/past?page=597>; rel="last"'
    )
    links = _parse_link_header(header)
    assert links["next"] == "https://api.pandascore.co/csgo/matches/past?page=2"
    assert links["last"] == "https://api.pandascore.co/csgo/matches/past?page=597"


def test_parse_link_header_empty_when_missing():
    assert _parse_link_header(None) == {}


def test_get_list_walks_pages_via_link_header(monkeypatch):
    pages = {
        "https://example.test/a": ([{"id": 1}], {"Link": '<https://example.test/b>; rel="next"'}),
        "https://example.test/b": ([{"id": 2}], {}),
    }

    def fake_get(url, params=None, headers=None, timeout=None):
        payload, resp_headers = pages[url]
        return FakeResponse(payload, 200, {**resp_headers, "X-Rate-Limit-Remaining": "900"})

    monkeypatch.setattr(httpx, "get", fake_get)

    budget = RequestBudget()
    result = _get_list("https://example.test/a", {}, budget)
    assert result == [{"id": 1}, {"id": 2}]
    assert budget.requests_made == 2


def test_get_list_stops_when_rate_limit_low(monkeypatch):
    def fake_get(url, params=None, headers=None, timeout=None):
        return FakeResponse(
            [{"id": 1}], 200,
            {"Link": '<https://example.test/next>; rel="next"', "X-Rate-Limit-Remaining": "5"},
        )

    monkeypatch.setattr(httpx, "get", fake_get)

    budget = RequestBudget()
    result = _get_list("https://example.test/a", {}, budget)
    # stops after ONE page even though a next link exists -- safety margin (10) tripped
    assert result == [{"id": 1}]
    assert budget.requests_made == 1
    assert budget.last_remaining == 5


# --- 429 --------------------------------------------------------------------------

def test_429_retries_after_retry_after_header(monkeypatch):
    calls = {"n": 0}
    sleeps = []

    def fake_get(url, params=None, headers=None, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return FakeResponse({}, 429, {"Retry-After": "2"})
        return FakeResponse({"ok": True}, 200, {})

    monkeypatch.setattr(httpx, "get", fake_get)
    monkeypatch.setattr("evhedge.data_sources.pandascore.time.sleep", lambda s: sleeps.append(s))

    resp = _get("https://example.test", budget=RequestBudget(), retries=1)
    assert resp.json() == {"ok": True}
    assert sleeps == [2.0]


def test_429_raises_rate_limit_error_after_exhausting_retries(monkeypatch):
    monkeypatch.setattr(httpx, "get", lambda *a, **k: FakeResponse({}, 429, {}))
    monkeypatch.setattr("evhedge.data_sources.pandascore.time.sleep", lambda s: None)

    with pytest.raises(PandaScoreRateLimitError):
        _get("https://example.test", budget=RequestBudget(), retries=1)


# --- endpoint param shapes (real filter names confirmed live) -----------------------

def test_fetch_matches_rejects_unknown_status():
    with pytest.raises(PandaScoreError, match="status"):
        fetch_matches("finished", RequestBudget())


def test_fetch_matches_builds_league_filter(monkeypatch):
    captured = {}

    def fake_get(url, params=None, headers=None, timeout=None):
        captured["url"] = url
        captured["params"] = params
        return FakeResponse([{"id": 1}], 200, {"X-Rate-Limit-Remaining": "900"})

    monkeypatch.setattr(httpx, "get", fake_get)
    fetch_matches("past", RequestBudget(), league_id=5370, max_pages=1)

    assert captured["url"].endswith("/csgo/matches/past")
    assert captured["params"]["filter[league_id]"] == 5370


def test_fetch_series_builds_name_search(monkeypatch):
    captured = {}

    def fake_get(url, params=None, headers=None, timeout=None):
        captured["params"] = params
        return FakeResponse([], 200, {"X-Rate-Limit-Remaining": "900"})

    monkeypatch.setattr(httpx, "get", fake_get)
    fetch_series(RequestBudget(), name_search="Bounty", max_pages=1)

    assert captured["params"]["search[name]"] == "Bounty"


def test_fetch_teams_real_fixture_shows_two_distinct_falcons_entities(monkeypatch):
    """Real data captured live (2026-07-20): searching "Falcons" returns
    TWO real, unrelated organizations, not a single obvious match --
    proof this module doesn't (and shouldn't) guess which one is "the"
    team. Disambiguating (here: by recent match history/league tier) is
    the caller's job."""
    real_fixture = [
        {"id": 137508, "name": "Falcons Force", "acronym": "FAL.F", "slug": "falcons-force"},
        {"id": 131216, "name": "Falcons", "acronym": "FLC", "slug": "falcons"},
        {"id": 130564, "name": "Team Falcons", "acronym": "FAL", "slug": "falcons-esports"},
    ]
    monkeypatch.setattr(
        httpx, "get",
        lambda *a, **k: FakeResponse(real_fixture, 200, {"X-Rate-Limit-Remaining": "900"}),
    )

    teams = fetch_teams("Falcons", RequestBudget())
    assert teams == real_fixture
    names = {t["name"] for t in teams}
    assert {"Falcons", "Team Falcons"} <= names  # both present, module doesn't collapse them
