"""Stage-2 fixed-bracket scenario tree.

Once BLAST Bounty's stage 2 seeds (8 teams, standard single-elim, NO
more reseeding between rounds -- unlike stage 1's Ro32/Ro16), the whole
outcome tree is deterministic to enumerate: exactly 3 rounds (QF, SF,
GF) = 7 matches = 2^7 = 128 possible full-bracket outcomes. This module
enumerates all of them from real winner-book mids +
``power_model.pair_prob``, aggregates each team's title probability and
per-stage marginal win probability, and can emit ready-to-run
``evhedge ev`` configs for flagged teams.

Bracket shape (fixed, given as 4 QF pairs in order)::

    QF1: pairs[0]         SF1: winner(QF1) vs winner(QF2)
    QF2: pairs[1]     ->                                  -> GF
    QF3: pairs[2]         SF2: winner(QF3) vs winner(QF4)
    QF4: pairs[3]

n (rounds_to_title) is uniform per stage for BOTH participants of any
one match, by construction of a clean single-elim bracket with no byes
past this point -- QF=3, SF=2, GF=1 for BLAST (see
``configs/blast_bounty_s2_stage_map.yaml``), passed in by the caller,
not hardcoded here (this module doesn't know which tournament it's for).
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional, Union

from evhedge.auto_predict import latest_book_winner_mid
from evhedge.power_model import pair_prob, strength
from evhedge.storage import Storage

#: DESIGN CHOICE: a small, separate heuristic from the general
#: stage_map (pandascore_sync.load_stage_map) -- detect_qf_pairs only
#: needs to recognize ONE specific round (the quarterfinal), not build a
#: full stage->n table, so it doesn't require the caller to have a
#: stage_map file at hand just to auto-detect seeding.
QF_STAGE_KEYWORDS = ("quarterfinal", "qf")

STAGE_NAMES = ("QF", "SF", "GF")


class ScenarioError(Exception):
    """Raised for malformed bracket input or missing model data --
    never silently defaults a missing pairwise probability to 0.5."""


@dataclass
class ScenarioMatch:
    team_a: str
    team_b: str
    stage: str
    p_a: float
    winner: str


@dataclass
class ScenarioPath:
    champion: str
    probability: float
    matches: list[ScenarioMatch]


@dataclass
class TeamOutlook:
    team: str
    p_title: float
    #: stage -> P(team wins that stage | team reached it), marginalized
    #: over which specific opponent shows up -- None if the team never
    #: reaches that stage in any path (shouldn't happen pre-QF, but kept
    #: honest rather than assumed).
    stage_win_prob: dict[str, Optional[float]]


WinProbFn = Callable[[str, str, str], float]


def make_win_prob_fn(store: Storage, tournament: str, stage_n: dict[str, int]) -> WinProbFn:
    """Build a ``(team_a, team_b, stage) -> P(team_a wins)`` function from
    real book-verified winner-market mids (``auto_predict.latest_book_winner_mid``)
    and ``power_model``, memoized per (team, stage) mid lookup within one
    call (a handful of unique teams, cheap either way, but avoids
    redundant DB round-trips across the up-to-128 scenario paths).

    Raises:
        ScenarioError: If a team has no book-verified winner mid yet, or
            ``stage`` isn't in ``stage_n`` -- never falls back to a
            guessed probability.
    """
    mid_cache: dict[str, float] = {}

    def _mid(team: str) -> float:
        if team not in mid_cache:
            mid, _ts = latest_book_winner_mid(store, tournament, team)
            if mid is None:
                raise ScenarioError(
                    f"нет book-verified winner_yes снапшота для {team!r} в {tournament!r} "
                    f"-- нечем моделировать пару"
                )
            mid_cache[team] = mid
        return mid_cache[team]

    def win_prob(team_a: str, team_b: str, stage: str) -> float:
        if stage not in stage_n:
            raise ScenarioError(f"стадия {stage!r} не задана в stage_n ({sorted(stage_n)})")
        n = stage_n[stage]
        return pair_prob(strength(_mid(team_a), n), strength(_mid(team_b), n))

    return win_prob


def enumerate_stage2_scenarios(
    pairs: list[tuple[str, str]], win_prob_fn: WinProbFn
) -> list[ScenarioPath]:
    """All 2^7 = 128 full-bracket outcomes for a seeded 8-team,
    3-round single-elim stage.

    Args:
        pairs: Exactly 4 (team_a, team_b) QF pairs, in bracket order
            [QF1, QF2, QF3, QF4] -- QF1/QF2 winners meet in SF1,
            QF3/QF4 winners meet in SF2.
        win_prob_fn: ``(team_a, team_b, stage) -> P(team_a wins)``, e.g.
            from ``make_win_prob_fn``.

    Raises:
        ScenarioError: If ``pairs`` isn't exactly 4 pairs of 8 distinct teams.
    """
    if len(pairs) != 4:
        raise ScenarioError(f"нужно ровно 4 пары QF, получено {len(pairs)}")
    all_teams = [t for pair in pairs for t in pair]
    if len(set(all_teams)) != 8:
        raise ScenarioError(f"ожидалось 8 различных команд в 4 парах, получено {all_teams}")

    paths: list[ScenarioPath] = []
    for outcome in itertools.product((0, 1), repeat=7):
        matches: list[ScenarioMatch] = []
        prob = 1.0
        qf_winners = []
        for i, (a, b) in enumerate(pairs):
            p_a = win_prob_fn(a, b, "QF")
            winner = a if outcome[i] == 0 else b
            prob *= p_a if outcome[i] == 0 else (1.0 - p_a)
            matches.append(ScenarioMatch(a, b, "QF", p_a, winner))
            qf_winners.append(winner)

        sf1_a, sf1_b = qf_winners[0], qf_winners[1]
        p_sf1 = win_prob_fn(sf1_a, sf1_b, "SF")
        sf1_winner = sf1_a if outcome[4] == 0 else sf1_b
        prob *= p_sf1 if outcome[4] == 0 else (1.0 - p_sf1)
        matches.append(ScenarioMatch(sf1_a, sf1_b, "SF", p_sf1, sf1_winner))

        sf2_a, sf2_b = qf_winners[2], qf_winners[3]
        p_sf2 = win_prob_fn(sf2_a, sf2_b, "SF")
        sf2_winner = sf2_a if outcome[5] == 0 else sf2_b
        prob *= p_sf2 if outcome[5] == 0 else (1.0 - p_sf2)
        matches.append(ScenarioMatch(sf2_a, sf2_b, "SF", p_sf2, sf2_winner))

        gf_a, gf_b = sf1_winner, sf2_winner
        p_gf = win_prob_fn(gf_a, gf_b, "GF")
        champion = gf_a if outcome[6] == 0 else gf_b
        prob *= p_gf if outcome[6] == 0 else (1.0 - p_gf)
        matches.append(ScenarioMatch(gf_a, gf_b, "GF", p_gf, champion))

        paths.append(ScenarioPath(champion=champion, probability=prob, matches=matches))

    return paths


def team_outlooks(paths: list[ScenarioPath], all_teams: list[str]) -> dict[str, TeamOutlook]:
    """Aggregate the enumerated paths into per-team title probability and
    per-stage marginal win probability (see ``TeamOutlook``).

    The per-stage numbers fall out of simple probability-mass
    accounting: summing ``path.probability`` over every path where a
    team appears in a given stage's match gives exactly
    P(team reaches that stage) -- correct by total-probability law,
    since every path is a complete, mutually-exclusive outcome. No
    separate closed-form derivation needed.
    """
    p_title = {t: 0.0 for t in all_teams}
    reach = {t: {s: 0.0 for s in STAGE_NAMES} for t in all_teams}
    win = {t: {s: 0.0 for s in STAGE_NAMES} for t in all_teams}

    for path in paths:
        p_title[path.champion] = p_title.get(path.champion, 0.0) + path.probability
        for m in path.matches:
            reach[m.team_a][m.stage] += path.probability
            reach[m.team_b][m.stage] += path.probability
            win[m.winner][m.stage] += path.probability

    outlooks: dict[str, TeamOutlook] = {}
    for team in all_teams:
        stage_win_prob: dict[str, Optional[float]] = {}
        for stage in STAGE_NAMES:
            r = reach[team][stage]
            stage_win_prob[stage] = (win[team][stage] / r) if r > 0 else None
        outlooks[team] = TeamOutlook(team=team, p_title=p_title[team], stage_win_prob=stage_win_prob)

    return outlooks


def stage_conflict(pairs: list[tuple[str, str]], team_x: str, team_y: str) -> Optional[str]:
    """Which stage (if any) ``team_x``/``team_y`` could face each other
    in, purely from the fixed bracket SLOTS -- deterministic, no
    probability involved. ``None`` if they're never on a collision
    course (shouldn't happen in an 8-team bracket, but returned honestly
    rather than assumed)."""
    if team_x == team_y:
        return None
    idx_x = idx_y = None
    for i, (a, b) in enumerate(pairs):
        if team_x in (a, b):
            idx_x = i
        if team_y in (a, b):
            idx_y = i
    if idx_x is None or idx_y is None:
        return None
    if idx_x == idx_y:
        return "QF"
    if idx_x // 2 == idx_y // 2:
        return "SF"
    return "GF"


#: Frozen with power_model.py -- see auto_predict.MODEL_VERSION's
#: docstring for why a model-version tag is mandatory on every emitted
#: MODEL-ESTIMATE number.
MODEL_VERSION = "power_model_v1"


def emit_bracket_config(
    team: str,
    outlook: TeamOutlook,
    tournament: str,
    sport: str,
    no_price: float,
    out_dir: Union[str, Path],
) -> Path:
    """Write a draft ``evhedge ev``-ready YAML for ``team`` -- stage
    win_prob values are MODEL-ESTIMATEs (marginalized over possible
    opponents, see ``team_outlooks``), clearly marked in comments for
    replacement with real book asks before trading (same "board vs
    book" discipline as everywhere else in this project).

    Returns:
        The path written, ``out_dir / "{team_slug}_stage2_scenario.yaml"``.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    slug = team.lower().replace(" ", "_").replace(".", "")
    out_path = out_dir / f"{slug}_stage2_scenario.yaml"

    stage_labels = {"QF": "Quarterfinal", "SF": "Semifinal", "GF": "Grand Final"}
    lines = [
        f"# АВТО-СГЕНЕРИРОВАНО evhedge scenarios --emit-configs ({MODEL_VERSION}).\n",
        f"# win_prob по стадиям -- MODEL-ESTIMATE (power_model.pair_prob, замер по\n",
        f"# реальным winner-book мидам на момент генерации), НЕ книжная цена.\n",
        f"# Соперник на SF/GF ещё не известен -- вероятность усреднена по всем\n",
        f"# возможным соперникам этой ветки сетки. Заменить на реальный ask перед\n",
        f"# торговлей (см. PROJECT RULE, data_sources/polymarket.py).\n",
        f'team: "{team}"\n',
        f'sport: "{sport}"\n',
        f'tournament: "{tournament}"\n',
        "\n",
        "stages:\n",
    ]
    for stage in STAGE_NAMES:
        p = outlook.stage_win_prob.get(stage)
        label = stage_labels[stage]
        if p is None:
            lines.append(f'  - name: "{label}"  # {team} does not reach this stage in any modeled path\n')
            lines.append("    win_prob: 0.01\n")
        else:
            lines.append(f'  - name: "{label}"\n')
            lines.append(f"    win_prob: {p:.4f}  # MODEL-ESTIMATE ({MODEL_VERSION})\n")
        lines.append("    hedge_decimal_odds: null\n")
    lines.append("\n")
    lines.append("market:\n")
    lines.append(f"  no_price: {no_price:.4f}\n")
    lines.append("\n")
    lines.append("strategy:\n")
    lines.append('  name: "manual review"\n')
    lines.append("  no_stake_usd: 1000\n")
    lines.append("  hedge_mode: none\n")

    out_path.write_text("".join(lines), encoding="utf-8")
    return out_path


def latest_book_no_ask(store: Storage, tournament: str, team: str) -> Optional[float]:
    """Latest book-verified ``winner_no`` ask (0..1 fraction), or
    ``None`` -- used to seed an emitted config's ``market.no_price``
    with a REAL price when one exists, rather than only ever a modeled
    complement."""
    snaps = [
        s for s in store.snapshots(tournament, team=team, market="winner_no")
        if s.source == "book" and s.ask_pct is not None
    ]
    if not snaps:
        return None
    return snaps[-1].ask_pct / 100.0


def detect_qf_pairs(store: Storage, tournament: str) -> list[tuple[str, str]]:
    """Best-effort auto-detect of the 4 QF pairs from ``ps_results``: rows
    whose ``stage`` contains "quarterfinal" or "qf" (case-insensitive).
    Requires exactly 4 such rows covering 8 distinct teams -- anything
    else (0 matches because the bracket hasn't seeded yet, a stray extra
    row, teams appearing twice) is reported, not guessed at.

    Raises:
        ScenarioError: If the QF rows found don't cleanly form 4 pairs
            of 8 distinct teams -- caller should fall back to an
            explicit team list.
    """
    candidates = [
        row for row in store.ps_results(tournament=tournament)
        if any(kw in row.stage.lower() for kw in QF_STAGE_KEYWORDS)
    ]
    if len(candidates) != 4:
        raise ScenarioError(
            f"найдено {len(candidates)} матчей со стадией QF в {tournament!r} "
            f"(ожидалось 4) -- сетка ещё не посеяна или неоднозначна, задайте --teams вручную"
        )
    pairs = [(row.team_a, row.team_b) for row in candidates]
    all_teams = [t for pair in pairs for t in pair]
    if len(set(all_teams)) != 8:
        raise ScenarioError(
            f"4 QF-матча дали не 8 различных команд ({all_teams}) -- задайте --teams вручную"
        )
    return pairs
