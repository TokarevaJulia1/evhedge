"""Football (soccer) knockout bracket builder.

Tournament formats change from edition to edition — the World Cup has run
with 32 teams and now expands to 48, the Champions League overhauled its
group stage into a league phase in 2024/25, and so on. Hard-coding a
specific round count for "World Cup" or "Champions League" would either go
stale by the next edition or, worse, be silently wrong if the format was
mis-remembered at the time the code was written.

So this module deliberately has no ``world_cup()``/``euro()``/
``champions_league()`` functions with baked-in stage counts. The only way
to build a football ``Bracket`` here is ``knockout_bracket``, where the
caller supplies the stage names and probabilities for the specific
tournament/edition they're analyzing, verified against the current
regulations at the time of use.
"""

from __future__ import annotations

from typing import Optional

from evhedge.models import Bracket, Stage

#: Named stage-name templates by round count, NOT tied to any specific
#: tournament or year. These are structural scaffolding only — always
#: verify the actual regulations (number of participants, group stage
#: presence, number of knockout rounds) for the tournament/edition you're
#: modeling before using one; formats change over time and are not tracked
#: automatically here.
KNOCKOUT_TEMPLATES: dict[str, list[str]] = {
    "single_elim_4_rounds": ["1/8 финала", "1/4 финала", "1/2 финала", "Финал"],
    "single_elim_5_rounds": [
        "1/16 финала",
        "1/8 финала",
        "1/4 финала",
        "1/2 финала",
        "Финал",
    ],
    "group_plus_knockout_4": [
        "Групповой этап",
        "1/8 финала",
        "1/4 финала",
        "1/2 финала",
        "Финал",
    ],
}


def template_stage_names(template_key: str) -> list[str]:
    """Return the stage-name list for a template key.

    IMPORTANT: this is only a structural scaffold (how many rounds and
    what to call them), NOT a source of truth for any specific
    tournament's format. Verify the actual regulations (number of
    participants, group stage presence, number of knockout rounds)
    yourself before using it — formats change from tournament to
    tournament and year to year, and this is not tracked automatically.

    Args:
        template_key: One of the keys in ``KNOCKOUT_TEMPLATES``.

    Returns:
        A fresh copy of the stage-name list for that template.

    Raises:
        ValueError: If ``template_key`` is not a known template.
    """
    if template_key not in KNOCKOUT_TEMPLATES:
        raise ValueError(
            f"Unknown template_key {template_key!r}; known templates: "
            f"{sorted(KNOCKOUT_TEMPLATES)}"
        )
    return list(KNOCKOUT_TEMPLATES[template_key])


def knockout_bracket(
    team: str,
    tournament: str,
    stage_names: list[str],
    stage_probs: list[float],
    hedge_odds: Optional[list[Optional[float]]] = None,
) -> Bracket:
    """Build a football ``Bracket`` from explicit, caller-supplied stages.

    This is the only bracket constructor in this module — there is no
    per-tournament shortcut. Verify ``stage_names``/``stage_probs`` (and
    ``hedge_odds``, if used) against the actual current format of the
    tournament/edition being analyzed before calling this.

    Args:
        team: Team name.
        tournament: Tournament/edition label, e.g. "Some Cup 2027" — free
            text, stored as ``Bracket.sport``.
        stage_names: Ordered stage labels, e.g.
            ``["Группа", "1/8 финала", "1/4 финала", "1/2 финала", "Финал"]``.
        stage_probs: Conditional win/advance probability per stage, same
            order and length as ``stage_names``.
        hedge_odds: Optional per-stage hedge decimal odds, same order and
            length as ``stage_probs``. Use ``None`` for a stage with no
            hedge market. Pass ``None`` (the default) to skip hedging
            entirely.

    Returns:
        A ``Bracket`` for ``team`` with one ``Stage`` per entry.

    Raises:
        ValueError: If ``stage_names``/``stage_probs``/``hedge_odds``
            lengths disagree, or if any probability is outside (0, 1].
    """
    if len(stage_names) != len(stage_probs):
        raise ValueError(
            f"stage_names has {len(stage_names)} entries but stage_probs has "
            f"{len(stage_probs)}; they must be the same length and in the "
            f"same order."
        )
    if hedge_odds is not None and len(hedge_odds) != len(stage_probs):
        raise ValueError(
            f"hedge_odds has {len(hedge_odds)} entries but stage_probs has "
            f"{len(stage_probs)}; they must be the same length and in the "
            f"same order (use None for stages without a hedge market)."
        )

    for i, (name, prob) in enumerate(zip(stage_names, stage_probs)):
        if not (0.0 < prob <= 1.0):
            raise ValueError(
                f"stage_probs[{i}] ({name!r}) must be in (0, 1], got {prob}"
            )

    odds_per_stage = hedge_odds if hedge_odds is not None else [None] * len(stage_names)

    stages = [
        Stage(name=name, win_prob=prob, hedge_decimal_odds=odds)
        for name, prob, odds in zip(stage_names, stage_probs, odds_per_stage)
    ]

    return Bracket(team=team, sport=tournament, stages=stages)
