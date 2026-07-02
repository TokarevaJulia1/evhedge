"""Tests for evhedge.sports.football."""

import math

import pytest

from evhedge.sports.football import (
    KNOCKOUT_TEMPLATES,
    knockout_bracket,
    template_stage_names,
)


def test_knockout_bracket_builds_and_title_prob_is_product():
    stage_names = ["1/8 финала", "1/4 финала", "1/2 финала", "Финал"]
    stage_probs = [0.7, 0.6, 0.5, 0.4]

    bracket = knockout_bracket("Team X", "Some Cup 2027", stage_names, stage_probs)

    assert bracket.team == "Team X"
    assert bracket.tournament == "Some Cup 2027"
    assert bracket.sport == "football"
    assert len(bracket.stages) == 4
    assert [s.name for s in bracket.stages] == stage_names
    assert [s.win_prob for s in bracket.stages] == stage_probs

    assert bracket.title_prob == pytest.approx(math.prod(stage_probs))


def test_mismatched_stage_names_and_probs_raises_value_error():
    with pytest.raises(ValueError, match=r"stage_names.*3.*stage_probs.*2"):
        knockout_bracket(
            "Team X",
            "Some Cup 2027",
            stage_names=["QF", "SF", "Final"],
            stage_probs=[0.6, 0.5],
        )


def test_mismatched_hedge_odds_length_raises_value_error():
    with pytest.raises(ValueError, match=r"hedge_odds.*2.*stage_probs.*3"):
        knockout_bracket(
            "Team X",
            "Some Cup 2027",
            stage_names=["QF", "SF", "Final"],
            stage_probs=[0.6, 0.5, 0.4],
            hedge_odds=[2.0, 1.8],
        )


def test_invalid_stage_prob_names_the_offending_stage():
    with pytest.raises(ValueError, match=r"stage_probs\[1\].*'SF'"):
        knockout_bracket(
            "Team X",
            "Some Cup 2027",
            stage_names=["QF", "SF", "Final"],
            stage_probs=[0.6, 0.0, 0.4],
        )


@pytest.mark.parametrize("template_key", sorted(KNOCKOUT_TEMPLATES))
def test_template_stage_names_returns_expected_length(template_key):
    names = template_stage_names(template_key)
    assert isinstance(names, list)
    assert all(isinstance(n, str) for n in names)
    assert len(names) == len(KNOCKOUT_TEMPLATES[template_key])


def test_template_stage_names_unknown_key_raises():
    with pytest.raises(ValueError, match="Unknown template_key"):
        template_stage_names("does_not_exist")


def test_knockout_bracket_without_hedge_odds():
    bracket = knockout_bracket(
        "Team X",
        "Some Cup 2027",
        stage_names=["QF", "SF", "Final"],
        stage_probs=[0.6, 0.5, 0.4],
    )
    assert all(stage.hedge_decimal_odds is None for stage in bracket.stages)


def test_knockout_bracket_with_partial_hedge_odds():
    bracket = knockout_bracket(
        "Team X",
        "Some Cup 2027",
        stage_names=["QF", "SF", "Final"],
        stage_probs=[0.6, 0.5, 0.4],
        hedge_odds=[2.0, None, 1.5],
    )
    assert bracket.stages[0].hedge_decimal_odds == pytest.approx(2.0)
    assert bracket.stages[1].hedge_decimal_odds is None
    assert bracket.stages[2].hedge_decimal_odds == pytest.approx(1.5)
