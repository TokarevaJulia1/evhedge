"""PandaScore: a source of match schedules/results independent of
Polymarket -- fast scoring/reconciliation, leg deadlines, and (once a
bracket seeds) full scenario trees, none of which should have to wait
on Gamma listing a market first.

Everything here targets the free **Fixtures** plan: schedules, results,
pre-match team/tournament data, 1,000 requests/hour. Anything gated
behind Historical (single-match-by-id, in-game stats, ...) is
deliberately NOT wrapped -- only the list endpoints
(``/csgo/matches/{upcoming,past,running}``, ``/csgo/series``,
``/csgo/teams``) are used, confirmed "All plans" against the live
plan-reference doc (https://developers.pandascore.co/docs/plan-reference.md).

Data model (confirmed live, not guessed -- see
https://developers.pandascore.co/docs/fundamentals.md): League > Series
> Tournament > Match > Game. PandaScore's "Tournament" is a STAGE within
a Series (e.g. "Group A", "Playoffs") -- this is what this module means
whenever it says "stage".

Auth: ``PANDASCORE_TOKEN`` env var, ``Authorization: Bearer <token>``.
Checked lazily on first real request (an unrelated import must never
fail over a missing token), with a clear error naming the variable --
never a silent skip.

Rate limiting: every response carries ``X-Rate-Limit-Remaining`` --
authoritative, read fresh each call rather than trusting a client-side
counter that could drift. Pagination follows the ``Link`` response
header (``rel="next"``), same convention as GitHub's API. A 429 backs
off on ``Retry-After`` if present, else a fixed delay, capped retries --
same shape as ``data_sources.polymarket``'s ``_get``.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from typing import Optional

import httpx

BASE_URL = "https://api.pandascore.co"

#: PandaScore's URL path segment for Counter-Strike (2) -- confirmed live;
#: the videogame itself reports as "Counter-Strike 2" / slug "cs-2" in
#: response bodies, but the API route is still "/csgo/...".
GAME_PATH = "csgo"

DEFAULT_TIMEOUT = 10.0
DEFAULT_RETRIES = 2

#: DESIGN CHOICE: stop paginating (return what's collected so far, never
#: crash) once the server reports fewer than this many requests left in
#: the hourly window -- a safety margin, not a hard wall, so a caller
#: mid-walk backs off before actually hitting 0.
RATE_LIMIT_SAFETY_MARGIN = 10


class PandaScoreError(Exception):
    """Raised for PandaScore problems: missing token, network/HTTP
    failure after retries, or an unexpected response shape."""


class PandaScoreRateLimitError(PandaScoreError):
    """Raised when the server's own rate-limit budget is (near)
    exhausted -- distinct from a generic HTTP failure so callers (e.g.
    the watcher loop) can back off instead of treating it as a data
    problem."""


@dataclass
class RequestBudget:
    """Session-cumulative request count (for the CLI summary) plus the
    server's own last-reported remaining budget (authoritative for
    whether to keep going)."""

    requests_made: int = 0
    last_remaining: Optional[int] = None
    last_used: Optional[int] = None


def _token() -> str:
    token = os.environ.get("PANDASCORE_TOKEN")
    if not token:
        raise PandaScoreError(
            "PANDASCORE_TOKEN не задан -- нужен API-ключ PandaScore (бесплатный "
            "Fixtures-тариф подходит), задайте переменную окружения перед вызовом"
        )
    return token


def _parse_link_header(link_header: Optional[str]) -> dict[str, str]:
    """``Link: <url>; rel="next", <url>; rel="last"`` -> {"next": url, "last": url}."""
    if not link_header:
        return {}
    links: dict[str, str] = {}
    for part in link_header.split(","):
        match = re.match(r'\s*<([^>]+)>;\s*rel="([^"]+)"', part)
        if match:
            links[match.group(2)] = match.group(1)
    return links


def _get(
    url: str,
    params: Optional[dict] = None,
    budget: Optional[RequestBudget] = None,
    timeout: float = DEFAULT_TIMEOUT,
    retries: int = DEFAULT_RETRIES,
) -> httpx.Response:
    """GET with auth + retry/backoff (including 429's ``Retry-After``),
    raising ``PandaScoreError``/``PandaScoreRateLimitError`` instead of a
    raw httpx exception. Updates ``budget`` in place from the response
    headers when given."""
    headers = {"Authorization": f"Bearer {_token()}"}
    last_exc: Optional[Exception] = None

    for attempt in range(retries + 1):
        try:
            resp = httpx.get(url, params=params, headers=headers, timeout=timeout)
        except httpx.HTTPError as e:
            last_exc = e
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
                continue
            raise PandaScoreError(
                f"не удалось получить данные с {url} (params={params}) после "
                f"{retries + 1} попыток: {last_exc}"
            ) from last_exc

        if budget is not None:
            budget.requests_made += 1
            remaining = resp.headers.get("X-Rate-Limit-Remaining")
            used = resp.headers.get("X-Rate-Limit-Used")
            if remaining is not None:
                budget.last_remaining = int(remaining)
            if used is not None:
                budget.last_used = int(used)

        if resp.status_code == 429:
            if attempt < retries:
                retry_after = resp.headers.get("Retry-After")
                delay = float(retry_after) if retry_after else 5.0 * (attempt + 1)
                time.sleep(delay)
                continue
            raise PandaScoreRateLimitError(
                f"PandaScore 429 (rate limit) на {url} после {retries + 1} попыток"
            )

        if resp.status_code == 401:
            raise PandaScoreError(
                f"PandaScore вернул 401 (неверный PANDASCORE_TOKEN) на {url}"
            )

        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            raise PandaScoreError(f"PandaScore {resp.status_code} на {url}: {e}") from e

        return resp

    raise PandaScoreError(f"не удалось получить данные с {url}: исчерпаны попытки")


def _get_list(
    url: str,
    params: dict,
    budget: RequestBudget,
    max_pages: Optional[int] = None,
) -> list[dict]:
    """Walk a paginated list endpoint via the ``Link: rel="next"`` header,
    stopping early (never raising) if the rate-limit budget runs low --
    see ``RATE_LIMIT_SAFETY_MARGIN``."""
    results: list[dict] = []
    next_url: Optional[str] = url
    next_params: Optional[dict] = dict(params)
    pages = 0

    while next_url is not None:
        resp = _get(next_url, params=next_params, budget=budget)
        page = resp.json()
        if not isinstance(page, list):
            raise PandaScoreError(f"неожиданный формат ответа {next_url}: ожидался список")
        results.extend(page)
        pages += 1

        if budget.last_remaining is not None and budget.last_remaining < RATE_LIMIT_SAFETY_MARGIN:
            break
        if max_pages is not None and pages >= max_pages:
            break

        links = _parse_link_header(resp.headers.get("Link"))
        next_url = links.get("next")
        next_params = None  # already encoded into next_url

    return results


# ---------------------------------------------------------------------------
# Matches
# ---------------------------------------------------------------------------

_MATCH_STATUSES = ("upcoming", "past", "running")


def fetch_matches(
    status: str,
    budget: RequestBudget,
    league_id: Optional[int] = None,
    serie_id: Optional[int] = None,
    tournament_id: Optional[int] = None,
    opponent_id: Optional[int] = None,
    per_page: int = 50,
    max_pages: Optional[int] = None,
) -> list[dict]:
    """``GET /csgo/matches/{status}`` -- filtered by whichever of
    league/serie/tournament/opponent id is given (PandaScore's own
    filter params; ``opponent_id`` confirmed live -- used to look up a
    specific team's own matches directly, e.g. to disambiguate same-
    named teams by their real match history, or to find which
    tournament a live Polymarket matchup belongs to).

    Args:
        status: One of "upcoming", "past", "running".
        budget: Shared ``RequestBudget`` -- pass the SAME instance across
            calls in one collector pass so it tracks cumulative use.

    Returns:
        Raw match dicts, unmodified -- each has (at least) ``id``,
        ``name``, ``opponents`` (each ``{"opponent": {id, name,
        acronym, slug, ...}}``), ``winner_id``, ``winner_type``,
        ``results`` (``[{team_id, score}, ...]``), ``draw`` (bool),
        ``status``, ``scheduled_at``/``begin_at``/``end_at``,
        ``match_type`` ("best_of"), ``number_of_games``, ``tournament``
        (the stage: ``{id, name, tier, has_bracket, ...}``), ``serie``,
        ``league``.

    Raises:
        PandaScoreError: Unknown ``status``, or the usual network/auth
            failures.
    """
    if status not in _MATCH_STATUSES:
        raise PandaScoreError(f"status должен быть одним из {_MATCH_STATUSES}, получено {status!r}")

    params: dict = {"per_page": per_page}
    if league_id is not None:
        params["filter[league_id]"] = league_id
    if serie_id is not None:
        params["filter[serie_id]"] = serie_id
    if tournament_id is not None:
        params["filter[tournament_id]"] = tournament_id
    if opponent_id is not None:
        params["filter[opponent_id]"] = opponent_id

    return _get_list(f"{BASE_URL}/{GAME_PATH}/matches/{status}", params, budget, max_pages)


# ---------------------------------------------------------------------------
# Series (Series list embeds each child Tournament/stage -- confirmed
# live: no separate paid "tournament structure" endpoint is needed for
# the stage list, at least at the depth this project needs).
# ---------------------------------------------------------------------------

def fetch_series(
    budget: RequestBudget,
    league_id: Optional[int] = None,
    name_search: Optional[str] = None,
    per_page: int = 25,
    max_pages: Optional[int] = None,
) -> list[dict]:
    """``GET /csgo/series`` -- each series embeds its child
    ``tournaments`` (stages: ``{id, name, tier, has_bracket, ...}``).

    Returns:
        Raw series dicts, unmodified.
    """
    params: dict = {"per_page": per_page}
    if league_id is not None:
        params["filter[league_id]"] = league_id
    if name_search is not None:
        params["search[name]"] = name_search

    return _get_list(f"{BASE_URL}/{GAME_PATH}/series", params, budget, max_pages)


# ---------------------------------------------------------------------------
# Teams
# ---------------------------------------------------------------------------

def fetch_teams(
    name_search: str,
    budget: RequestBudget,
    per_page: int = 20,
) -> list[dict]:
    """``GET /csgo/teams?search[name]=...`` -- the team registry, keyed
    independently of any tournament/series (so this resolves a team's
    PandaScore spelling even before its next tournament is created --
    see the module docstring on BLAST Bounty S2 not existing yet as of
    onboarding).

    Returns:
        Raw team dicts: ``{id, name, acronym, slug, location, ...}``.
        DESIGN CHOICE: a name search can return multiple same-ish-named
        entities (confirmed live: "Falcons" id 131216, an unrelated
        lower-tier team, vs "Team Falcons" id 130564, the actual
        multi-title org) -- this function does NOT guess which one is
        "the" team; disambiguating by recent match history is the
        caller's job (see ``tests/test_data_sources_pandascore.py`` for
        the real disambiguation this project did for Falcons/Aurora).
    """
    params = {"search[name]": name_search, "per_page": per_page}
    return _get_list(f"{BASE_URL}/{GAME_PATH}/teams", params, budget, max_pages=1)


# ---------------------------------------------------------------------------
# Tournament brackets
# ---------------------------------------------------------------------------

def fetch_tournament_brackets(
    tournament_id: int, budget: RequestBudget, per_page: int = 100
) -> list[dict]:
    """``GET /tournaments/{id}/brackets`` -- the FULL known bracket for
    one PandaScore Tournament (= one stage of a Series, see the module
    docstring), in a single call: every match already paired AND every
    not-yet-decided future-round slot as a placeholder (``opponents: []``,
    ``name`` reads e.g. "Round of 16 match 3"). Confirmed live
    (2026-07-21) against BLAST Bounty's Qualifier stage (tournament id
    21474): 24 rows for a 32-team bracket -- 16 Round-of-32 pairs with
    real opponents plus 8 Round-of-16 slots waiting on them.

    Unlike every other endpoint in this module, the path is NOT
    game-prefixed (``/tournaments/{id}/brackets``, not
    ``/{GAME_PATH}/tournaments/...``) -- PandaScore tournament ids are
    unique across games, confirmed against the live OpenAPI example
    (``/tournaments/1590/brackets``) and by calling it successfully this
    way. "All plans" per the live plan-reference doc, like every other
    endpoint this module wraps.

    Args:
        tournament_id: A PandaScore Tournament id -- e.g. from a match's
            own ``tournament.id`` field (see ``fetch_matches``), NOT a
            Series id.

    Returns:
        Raw match dicts, same shape as ``fetch_matches``'s -- a
        placeholder future-round match has ``opponents: []`` and no
        ``id``-resolvable teams yet.
    """
    return _get_list(f"{BASE_URL}/tournaments/{tournament_id}/brackets", {"per_page": per_page}, budget)
