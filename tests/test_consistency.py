"""Tests for evhedge.consistency (Module 5).

Fixture numbers reproduce the headline numbers of the real findings that
motivated each check (+1.2% basket, CONCACAF=USA +0.6%); the full original
boards live in the trading notes, these are compact reconstructions that
hit the same numbers.
"""

import pytest

from evhedge.consistency import (
    IDENTITY_MIN_EDGE_PCT,
    VERIFY_BOOK_CAVEAT,
    VERTICAL_EXTREME_HIGH,
    VERTICAL_EXTREME_LOW,
    ConsistencyError,
    basket_check,
    identity_check,
    vertical_check,
)


# --- basket_check: the "+1.2% корзина" finding --------------------------------

#: 4-market winner board (slots=1): NO asks sum to 296.44 vs a guaranteed
#: 300 payout -> +3.56 points on 296.44 cost = +1.2% locked in.
BASKET_PLUS_1_2 = {"TeamA": 62.0, "TeamB": 79.4, "TeamC": 83.04, "TeamD": 72.0}


def test_basket_check_reproduces_plus_1_2_pct_finding():
    result = basket_check(BASKET_PLUS_1_2, slots=1)

    assert result.n_markets == 4
    assert result.payout_pct == pytest.approx(300.0)
    assert result.cost_pct == pytest.approx(296.44)
    assert result.edge_pct == pytest.approx(3.56)
    assert result.return_pct == pytest.approx(1.2, abs=0.01)
    assert result.is_signal is True
    assert result.caveat == VERIFY_BOOK_CAVEAT


def test_basket_check_typical_board_is_negative():
    # A normal longshot board: NO asks in the 90s, sum 372.4 > 300 -> the
    # basket costs more than it pays, no signal.
    markets = {"A": 91.9, "B": 96.0, "C": 92.5, "D": 92.0}
    result = basket_check(markets, slots=1)
    assert result.edge_pct < 0
    assert result.is_signal is False


def test_basket_check_two_slots_payout():
    # Finalists board (2 slots fill): n=4 -> payout (4-2)*100 = 200.
    markets = {"A": 40.0, "B": 55.0, "C": 60.0, "D": 42.0}
    result = basket_check(markets, slots=2)
    assert result.payout_pct == pytest.approx(200.0)
    assert result.edge_pct == pytest.approx(200.0 - 197.0)
    assert result.is_signal is True


def test_basket_check_rejects_bad_inputs():
    with pytest.raises(ConsistencyError, match="at least 2"):
        basket_check({"A": 50.0}, slots=1)
    with pytest.raises(ConsistencyError, match="slots"):
        basket_check({"A": 50.0, "B": 50.0}, slots=2)  # slots must be < n
    with pytest.raises(ConsistencyError, match="slots"):
        basket_check({"A": 50.0, "B": 50.0}, slots=0)
    with pytest.raises(ConsistencyError, match=r"\(0, 100\)"):
        basket_check({"A": 50.0, "B": 100.0}, slots=1)


# --- identity_check: the "CONCACAF=USA +0.6%" finding --------------------------

def test_identity_check_reproduces_concacaf_plus_0_6_pct_finding():
    # Parent "CONCACAF team wins" at 9.4 vs hand-mapped members summing to
    # 8.8 (USA carries essentially all of it) -> parent rich by +0.6.
    result = identity_check(
        ("CONCACAF winner", 9.4),
        {"USA": 8.5, "Mexico": 0.2, "Canada": 0.1},
    )

    assert result.members_sum_pct == pytest.approx(8.8)
    assert result.diff_pct == pytest.approx(0.6)
    assert result.rich_side == "parent"
    assert result.is_signal is True
    assert result.caveat == VERIFY_BOOK_CAVEAT


def test_identity_check_members_rich_direction():
    result = identity_check(("Parent", 8.0), {"X": 5.0, "Y": 4.0})
    assert result.diff_pct == pytest.approx(-1.0)
    assert result.rich_side == "members"
    assert result.is_signal is True


def test_identity_check_sub_threshold_gap_is_noise():
    # Gap below IDENTITY_MIN_EDGE_PCT: reported, but not a signal.
    result = identity_check(("Parent", 9.0), {"X": 8.0, "Y": 0.7})
    assert abs(result.diff_pct) < IDENTITY_MIN_EDGE_PCT
    assert result.rich_side == "balanced"
    assert result.is_signal is False


def test_identity_check_rejects_bad_inputs():
    with pytest.raises(ConsistencyError, match="empty"):
        identity_check(("Parent", 9.4), {})
    with pytest.raises(ConsistencyError, match=r"\(0, 100\)"):
        identity_check(("Parent", 100.0), {"X": 8.8})


# --- vertical_check ------------------------------------------------------------

def test_vertical_check_healthy_ladder_no_signal():
    result = vertical_check(
        "TeamB", [("reach_semi", 12.0), ("reach_final", 8.0), ("winner", 3.0)]
    )

    assert result.violations == []
    assert result.flags == []
    assert result.is_signal is False
    # implied conditionals: 8/12 and 3/8
    (_, _, c1), (_, _, c2) = result.conditionals
    assert c1 == pytest.approx(8.0 / 12.0)
    assert c2 == pytest.approx(3.0 / 8.0)
    assert all(0.0 < c < 1.0 for _, _, c in result.conditionals)
    assert result.caveat == VERIFY_BOOK_CAVEAT


def test_vertical_check_monotonicity_break_is_hard_violation():
    # winner priced ABOVE reach_final -- impossible, hard signal.
    result = vertical_check("TeamX", [("reach_final", 8.0), ("winner", 8.6)])
    assert len(result.violations) == 1
    assert result.is_signal is True
    assert "p_cond=1.075" in result.violations[0]


def test_vertical_check_extreme_high_is_soft_flag():
    # 39/40 = 0.975 >= 0.95: the final is priced as nearly free given the
    # semi -- flagged, but not a violation, so not a signal by itself.
    result = vertical_check("TeamY", [("reach_semi", 40.0), ("reach_final", 39.0)])
    assert result.violations == []
    assert len(result.flags) == 1
    assert "EXTREME_HIGH" in result.flags[0]
    assert result.is_signal is False
    assert 39.0 / 40.0 >= VERTICAL_EXTREME_HIGH


def test_vertical_check_extreme_low_is_soft_flag():
    # 1/30 ~ 0.033 <= 0.05: the title is priced as nearly unreachable
    # given reaching the final.
    result = vertical_check("TeamZ", [("reach_final", 30.0), ("winner", 1.0)])
    assert result.violations == []
    assert len(result.flags) == 1
    assert "EXTREME_LOW" in result.flags[0]
    assert 1.0 / 30.0 <= VERTICAL_EXTREME_LOW


def test_vertical_check_rejects_bad_inputs():
    with pytest.raises(ConsistencyError, match="at least 2 rungs"):
        vertical_check("TeamX", [("winner", 3.0)])
    with pytest.raises(ConsistencyError, match=r"\(0, 100\)"):
        vertical_check("TeamX", [("reach_final", 0.0), ("winner", 3.0)])
