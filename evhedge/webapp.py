"""Local Roll-Over Chain dashboard: a small stdlib-only HTTP server that
serves the dashboard page and a thin JSON API over live Polymarket data
(open positions, order book).

DESIGN CHOICE -- stdlib ``http.server``, not Flask/FastAPI: this is a
single-user, localhost-only tool (position lookups are read-only public
data, nothing here places orders), and the project's existing
dependency list is deliberately small (numpy/pyyaml/click/rich/
matplotlib/httpx) -- a whole web framework for two GET endpoints and a
static file would be a heavier dependency than the problem needs.

DESIGN CHOICE -- binds to 127.0.0.1 only, never 0.0.0.0: nothing here
needs to be reachable from another machine, and there's no reason to
expose it to the local network by default.

The dashboard itself (``webapp_static/index.html``) never talks to
Polymarket directly -- browser-side fetches only ever hit this same
localhost server's ``/api/...`` routes, which is what makes live data
possible at all: a page published as a claude.ai Artifact is sandboxed
(no arbitrary outbound fetch), but a page served by your own local
Python process has no such restriction.
"""

from __future__ import annotations

import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from evhedge.data_sources import pandascore as pandascore_ds
from evhedge.data_sources import polymarket as polymarket_ds
from evhedge.data_sources.pandascore import PandaScoreError
from evhedge.data_sources.polymarket import PolymarketAPIError
from evhedge.team_aliases import canonical_name, load_default_aliases

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "webapp_static"

#: Position fields the dashboard actually uses -- trimmed from the Data
#: API's full response (see ``polymarket.fetch_positions``) so the
#: browser never depends on fields that might change/disappear upstream.
_POSITION_FIELDS = (
    "title", "outcome", "oppositeOutcome", "eventSlug", "slug", "size", "avgPrice",
    "initialValue", "currentValue", "curPrice", "asset", "redeemable",
)


def positions_payload(address: str) -> list[dict]:
    """Fetch + trim live positions for one wallet address -- a pure
    function of ``fetch_positions``'s output, kept separate from the
    HTTP handler so it's testable without a running server."""
    raw = polymarket_ds.fetch_positions(address)
    return [{field: p.get(field) for field in _POSITION_FIELDS} for p in raw]


def book_payload(token_id: str) -> dict:
    """Live top-of-book bid/ask for one CLOB token -- same pure-function
    shape as ``positions_payload``."""
    book = polymarket_ds.fetch_order_book(token_id)
    bid, ask = polymarket_ds.best_bid_ask(book)
    return {"bid": bid, "ask": ask}


def bracket_payload(tournament_id: int) -> list[dict]:
    """The full known bracket for one PandaScore Tournament (id, NOT a
    Series -- see ``data_sources.pandascore.fetch_tournament_brackets``):
    every already-paired match AND every not-yet-decided future round as
    a TBD placeholder, team names canonicalized through the SAME alias
    map ``collect.py``/``pandascore_sync.py`` use -- one matcher, not a
    second one just for the dashboard."""
    budget = pandascore_ds.RequestBudget()
    raw = pandascore_ds.fetch_tournament_brackets(tournament_id, budget)
    alias_map = load_default_aliases()

    out = []
    for m in raw:
        opponents = m.get("opponents") or []
        tbd = len(opponents) < 2
        if tbd:
            team_a, team_b = "TBD", "TBD"
        else:
            team_a = canonical_name(opponents[0]["opponent"]["name"], alias_map)
            team_b = canonical_name(opponents[1]["opponent"]["name"], alias_map)
        out.append({
            "name": m.get("name"),
            "team_a": team_a,
            "team_b": team_b,
            "status": m.get("status"),
            "scheduled_at": m.get("scheduled_at"),
            "best_of": m.get("number_of_games"),
            "tbd": tbd,
        })
    return out


def pandascore_match_payload(team_a: str, team_b: str) -> dict:
    """``{tournament_id: N}`` (or ``{tournament_id: None}`` if not
    confirmed) for the real PandaScore match between two teams --
    thin wrapper over ``pandascore_sync.find_tournament_id_for_matchup``
    so the dashboard can auto-fill the bracket panel from a loaded
    Polymarket leg position instead of the user hand-typing an id."""
    from evhedge.pandascore_sync import find_tournament_id_for_matchup

    tournament_id = find_tournament_id_for_matchup(team_a, team_b)
    return {"tournament_id": tournament_id}


_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
}


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "evhedge-dashboard/1"

    def _send_json(self, status: int, payload) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_static(self, rel_path: str) -> None:
        path = (STATIC_DIR / rel_path.lstrip("/")).resolve()
        if STATIC_DIR.resolve() not in path.parents and path != STATIC_DIR.resolve():
            self._send_json(404, {"error": "not found"})
            return
        if not path.is_file():
            self._send_json(404, {"error": "not found"})
            return
        content_type = _CONTENT_TYPES.get(path.suffix, "application/octet-stream")
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 (stdlib method name)
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)

        try:
            if parsed.path in ("/", "/index.html"):
                self._send_static("index.html")
                return
            if parsed.path == "/api/positions":
                address = (qs.get("address") or [None])[0]
                if not address:
                    self._send_json(400, {"error": "укажите ?address=0x..."})
                    return
                self._send_json(200, positions_payload(address))
                return
            if parsed.path == "/api/book":
                token_id = (qs.get("token_id") or [None])[0]
                if not token_id:
                    self._send_json(400, {"error": "укажите ?token_id=..."})
                    return
                self._send_json(200, book_payload(token_id))
                return
            if parsed.path == "/api/bracket":
                tournament_id = (qs.get("tournament_id") or [None])[0]
                if not tournament_id or not tournament_id.isdigit():
                    self._send_json(400, {"error": "укажите ?tournament_id=<число>"})
                    return
                self._send_json(200, bracket_payload(int(tournament_id)))
                return
            if parsed.path == "/api/pandascore-match":
                team_a = (qs.get("team_a") or [None])[0]
                team_b = (qs.get("team_b") or [None])[0]
                if not team_a or not team_b:
                    self._send_json(400, {"error": "укажите ?team_a=...&team_b=..."})
                    return
                self._send_json(200, pandascore_match_payload(team_a, team_b))
                return
            self._send_static(parsed.path)
        except PolymarketAPIError as e:
            self._send_json(502, {"error": str(e)})
        except PandaScoreError as e:
            self._send_json(502, {"error": str(e)})
        except Exception as e:  # last resort -- never leak a raw traceback to the browser
            logger.exception("unhandled error serving %s", self.path)
            self._send_json(500, {"error": f"internal error: {e}"})

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        logger.info("%s - %s", self.address_string(), format % args)


def run_server(port: int = 8787) -> None:
    """Serve the dashboard at http://127.0.0.1:PORT until Ctrl+C."""
    server = ThreadingHTTPServer(("127.0.0.1", port), DashboardHandler)
    print(f"evhedge dashboard: http://127.0.0.1:{port}  (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
