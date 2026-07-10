"""Team name canonicalization.

Polymarket names the same team differently across markets of the same
tournament: the EWC 2026 Dota 2 winner board says "1W" and "Aurora
Gaming"; the match-level leg markets for the exact same teams say "1win"
and "Aurora". Any join between a team's outright board history and its
match legs -- which is the core pre-entry analysis this project exists
for ("how did outright move vs how are the legs priced") -- silently
drops pairs whenever the two sides disagree on spelling.

``canonical_name`` deliberately does NOT do fuzzy/edit-distance matching:
silently merging two DIFFERENT teams is worse than leaving two spellings
of the SAME team unmerged (a silent merge corrupts data quietly; an
unmerged pair just under-counts, visibly, in a join). Only exact matches
(case/whitespace-insensitive) against an explicit, hand-curated alias map
are canonicalized. Anything not in the map comes back unchanged --
``suggest_aliases`` exists to surface CANDIDATES for that map, not to
apply them.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional, Union

from evhedge.config_io import ConfigError, _read_yaml_file

logger = logging.getLogger(__name__)

#: Packaged default alias map -- confirmed, real discrepancies only (see
#: the file itself and the module's shipping commit for how each entry was
#: verified against evhedge/data/../data/ewc2026.db).
DEFAULT_ALIASES_PATH = Path(__file__).parent / "data" / "team_aliases.yaml"

#: suggest_aliases heuristics (deliberately simple -- see module docstring
#: on why this isn't fuzzy/edit-distance matching).
MIN_COMMON_PREFIX = 4


def _normalize(s: str) -> str:
    """Whitespace-collapsed, casefolded form used ONLY for comparison --
    never returned as a canonical value (see ``canonical_name``)."""
    return " ".join(s.split()).casefold()


def canonical_name(raw: str, alias_map: Optional[dict[str, str]] = None) -> str:
    """Canonicalize one team name.

    Order:
    a) Case/whitespace-insensitive exact match against ``alias_map``'s
       keys -> return that entry's value (stored/returned exactly as
       written in the map, not casefolded).
    b) No ``alias_map`` given, or no match -> return ``raw`` completely
       unchanged. No stripping, no fuzzy matching -- an unrecognized name
       is data, not something to guess-normalize.

    Args:
        raw: Team name as it came from the source (Gamma, a scanner YAML,
            ...).
        alias_map: alias -> canonical, e.g. from ``load_aliases``. Every
            canonical value must also map to itself (``load_aliases``
            guarantees this) so ``canonical_name`` is idempotent:
            ``canonical_name(canonical_name(x, m), m) == canonical_name(x, m)``.

    Returns:
        The canonical name, or ``raw`` unchanged if nothing matched.
    """
    if alias_map:
        target = _normalize(raw)
        for alias, canonical in alias_map.items():
            if _normalize(alias) == target:
                return canonical
    return raw


def load_aliases(path: Union[str, Path]) -> dict[str, str]:
    """Load a YAML alias file: ``canonical: [alias1, alias2, ...]`` ->
    flat ``alias -> canonical`` map, ready for ``canonical_name``. Every
    canonical also maps to itself, so looking up an already-canonical name
    is a no-op (idempotency).

    Args:
        path: YAML file, top level a mapping of canonical name -> list of
            aliases (list may be empty).

    Returns:
        Flat map: every alias AND every canonical (as its own key) ->
        canonical, keyed by the strings exactly as written in the file
        (comparison is done case/whitespace-insensitively by
        ``canonical_name`` itself, not baked into this map's keys).

    Raises:
        ConfigError: Malformed YAML, a non-mapping top level, or one
            alias (case/whitespace-insensitively) pointing at two
            different canonicals.
    """
    path = Path(path)
    data = _read_yaml_file(path)  # already guarantees a dict, or raises ConfigError

    normalized_seen: dict[str, str] = {}  # normalized alias -> canonical, for conflict detection
    flat: dict[str, str] = {}  # alias-as-written -> canonical

    def _add(alias: str, canonical: str) -> None:
        norm = _normalize(alias)
        if norm in normalized_seen and normalized_seen[norm] != canonical:
            raise ConfigError(
                f"{path}: {alias!r} указывает и на {normalized_seen[norm]!r}, "
                f"и на {canonical!r} -- один alias не может иметь два canonical"
            )
        normalized_seen[norm] = canonical
        flat[alias] = canonical

    for canonical, aliases in data.items():
        canonical = str(canonical)
        _add(canonical, canonical)
        for alias in (aliases or []):
            _add(str(alias), canonical)

    return flat


def load_default_aliases() -> dict[str, str]:
    """Load ``DEFAULT_ALIASES_PATH``. Missing file -> ``{}`` (not an
    error): callers -- notably the v4->v5 storage migration, which must
    never fail a database open over a missing/misplaced data file --
    degrade to "no aliases known yet", not a crash."""
    if not DEFAULT_ALIASES_PATH.exists():
        return {}
    return load_aliases(DEFAULT_ALIASES_PATH)


def recanonicalize(conn, alias_map: dict[str, str]) -> dict[str, int]:
    """Re-apply ``canonical_name`` to every team-bearing column already
    stored in a snapshot database: ``price_snapshots.team``,
    ``price_snapshots.counterparty``, ``resolves.team``.

    Idempotent and safe to call anytime -- including repeatedly, e.g.
    after adding a NEW entry to team_aliases.yaml that wasn't known when
    older rows were first written (a one-time schema migration, by
    definition, only ever canonicalizes against the alias map that
    existed the moment it ran; this is how those rows catch up later).
    Only rows where canonicalization actually changes something are
    touched. ``raw_team`` is set (once) only when a ``team`` row is
    actually rewritten for the first time -- if it's already set from an
    earlier pass, it's left alone, so it always keeps the EARLIEST raw
    spelling ever observed for that row, not whatever it happened to be
    just before this particular call.

    ``resolves`` has a UNIQUE index on ``(tournament, team, market)``
    (schema v6+): renaming a row's ``team`` to its canonical form can
    collide with an already-canonical row for the same
    ``(tournament, market)`` (e.g. both the old and new spelling resolved
    the same market before this alias existed). On collision, the
    EXISTING canonical row is kept as-is and the row being renamed is
    DELETED as a duplicate instead of updated -- logged at WARNING, never
    a crash, and no other row is touched.

    Args:
        conn: An open sqlite3 connection to a database whose schema
            already has ``price_snapshots.raw_team`` (schema v5+).
        alias_map: e.g. from ``load_default_aliases()`` or
            ``load_aliases(custom_path)``.

    Returns:
        ``{"price_snapshots.team": n, "price_snapshots.counterparty": n,
        "resolves.team": n, "resolves.duplicates_dropped": n}`` -- how
        many rows were actually changed (or, for the last key, dropped as
        a UNIQUE collision) per column, not merely visited.
    """
    counts = {
        "price_snapshots.team": 0,
        "price_snapshots.counterparty": 0,
        "resolves.team": 0,
        "resolves.duplicates_dropped": 0,
    }

    for row_id, team, raw_team in conn.execute(
        "SELECT id, team, raw_team FROM price_snapshots"
    ).fetchall():
        canon = canonical_name(team, alias_map)
        if canon != team:
            new_raw_team = raw_team if raw_team is not None else team
            conn.execute(
                "UPDATE price_snapshots SET team = ?, raw_team = ? WHERE id = ?",
                (canon, new_raw_team, row_id),
            )
            counts["price_snapshots.team"] += 1

    for row_id, counterparty in conn.execute(
        "SELECT id, counterparty FROM price_snapshots WHERE counterparty IS NOT NULL"
    ).fetchall():
        canon = canonical_name(counterparty, alias_map)
        if canon != counterparty:
            conn.execute(
                "UPDATE price_snapshots SET counterparty = ? WHERE id = ?", (canon, row_id)
            )
            counts["price_snapshots.counterparty"] += 1

    for row_id, tournament, team, market in conn.execute(
        "SELECT id, tournament, team, market FROM resolves"
    ).fetchall():
        canon = canonical_name(team, alias_map)
        if canon == team:
            continue

        collision = conn.execute(
            "SELECT id FROM resolves WHERE tournament = ? AND team = ? AND market = ? AND id != ?",
            (tournament, canon, market, row_id),
        ).fetchone()
        if collision is not None:
            # idx_resolves_unique(tournament, team, market) already has a
            # canonical row for this slot -- keep it, drop this duplicate.
            conn.execute("DELETE FROM resolves WHERE id = ?", (row_id,))
            logger.warning(
                "recanonicalize: dropped duplicate resolves row id=%s (team=%r -> %r, "
                "tournament=%r, market=%r) -- canonical row id=%s already exists",
                row_id, team, canon, tournament, market, collision[0],
            )
            counts["resolves.duplicates_dropped"] += 1
            continue

        conn.execute("UPDATE resolves SET team = ? WHERE id = ?", (canon, row_id))
        counts["resolves.team"] += 1

    return counts


# ---------------------------------------------------------------------------
# Discovery: suggest, never apply
# ---------------------------------------------------------------------------

def _similarity(norm_a: str, norm_b: str) -> Optional[float]:
    """Simple, explainable heuristics only -- see module docstring on why
    this isn't edit-distance/fuzzy matching. Returns None if neither
    heuristic fires."""
    if norm_a in norm_b or norm_b in norm_a:
        shorter, longer = sorted((norm_a, norm_b), key=len)
        return len(shorter) / len(longer)

    common_prefix = 0
    for ca, cb in zip(norm_a, norm_b):
        if ca != cb:
            break
        common_prefix += 1
    if common_prefix >= MIN_COMMON_PREFIX:
        return common_prefix / max(len(norm_a), len(norm_b))
    return None


def _suggest_from_names(names: list[str]) -> list[tuple[str, str, float]]:
    candidates: list[tuple[str, str, float]] = []
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            if a == b:
                continue
            norm_a, norm_b = _normalize(a), _normalize(b)
            if norm_a == norm_b:
                # Same team, differs only by case/whitespace -- the
                # highest-confidence kind of candidate there is.
                candidates.append((a, b, 1.0))
                continue
            score = _similarity(norm_a, norm_b)
            if score is not None:
                candidates.append((a, b, score))
    candidates.sort(key=lambda t: -t[2])
    return candidates


def suggest_aliases(
    db_path: Union[str, Path], tournament: Optional[str] = None
) -> list[tuple[str, str, float]]:
    """Suggest candidate alias pairs from names actually seen in a
    snapshot database -- NEVER merges anything, only proposes (see module
    docstring). Sorted by descending score.

    Args:
        db_path: Snapshot database to scan (``price_snapshots.team`` and
            ``.counterparty``).
        tournament: Optional filter to one tournament's names.

    Returns:
        ``(name_a, name_b, score)`` triples, score in (0, 1], descending.
        Pairs already IDENTICAL are never suggested; heuristics are
        substring containment and a shared prefix of at least
        ``MIN_COMMON_PREFIX`` characters -- nothing cleverer, deliberately
        (an abbreviation like "IC x Insanity" for "Inner Circle" will NOT
        be found this way; that's an acceptable, honest gap, not a silent
        guess).
    """
    from evhedge.storage import Storage  # local import: breaks the storage<->team_aliases cycle

    with Storage(db_path) as store:
        names = store.distinct_team_names(tournament)
    return _suggest_from_names(names)
