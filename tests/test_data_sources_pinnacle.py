"""Tests for evhedge.data_sources.pinnacle."""

import pytest

from evhedge.data_sources.pinnacle import (
    american_to_decimal,
    devig,
    devig_3way,
    devig_range,
    fetch_odds,
)


def test_american_to_decimal_positive():
    assert american_to_decimal(150) == pytest.approx(2.5)


def test_american_to_decimal_negative():
    assert american_to_decimal(-200) == pytest.approx(1.5)


def test_devig_sums_to_one_and_favors_lower_odds():
    home_prob, away_prob = devig(1.5, 2.8)
    assert home_prob + away_prob == pytest.approx(1.0)
    assert home_prob > away_prob  # lower decimal odds = higher implied prob


def test_devig_3way_sums_home_away_below_one_draw_excluded():
    home_prob, away_prob = devig_3way(2.0, 3.4, 4.0)
    # draw's share isn't in the return value, so home+away < 1
    assert 0.0 < home_prob + away_prob < 1.0
    assert home_prob > away_prob


def test_devig_range_needs_at_least_two_outcomes():
    with pytest.raises(ValueError, match="at least 2"):
        devig_range([1.5])


def test_devig_range_rejects_odds_at_or_below_one():
    with pytest.raises(ValueError, match="> 1.0"):
        devig_range([1.5, 1.0])


def test_devig_range_two_way_all_margin_favors_favorite_more_than_proportional():
    """The all-margin-in-longshot estimate must give the favorite a
    probability >= the proportional estimate (it's the "generous to
    favorite" extreme of the range), and the two must bracket a range,
    not just be equal in normal cases with real vig."""
    proportional, all_margin = devig_range([1.5, 2.8])  # home is favorite

    assert sum(proportional) == pytest.approx(1.0)
    assert sum(all_margin) == pytest.approx(1.0)

    fav_proportional, fav_all_margin = proportional[0], all_margin[0]
    assert fav_all_margin >= fav_proportional


def test_devig_range_two_way_all_margin_favorite_equals_raw_implied():
    """For 2-way, all-margin-in-longshot leaves the favorite's raw implied
    probability untouched (all the correction lands on the underdog)."""
    home_dec, away_dec = 1.5, 2.8
    _, all_margin = devig_range([home_dec, away_dec])
    assert all_margin[0] == pytest.approx(1 / home_dec)


def test_devig_range_three_way_longshot_absorbs_margin():
    decimals = [2.0, 3.4, 4.0]  # home favorite, draw, away longshot (highest decimal)
    proportional, all_margin = devig_range(decimals)

    assert sum(proportional) == pytest.approx(1.0)
    assert sum(all_margin) == pytest.approx(1.0)

    # away (index 2) has the smallest raw implied prob -> it's the longshot
    raw = [1 / d for d in decimals]
    longshot_idx = raw.index(min(raw))
    assert longshot_idx == 2
    # the other two outcomes keep their raw implied probability exactly
    assert all_margin[0] == pytest.approx(raw[0])
    assert all_margin[1] == pytest.approx(raw[1])


def test_fetch_odds_is_a_stub():
    with pytest.raises(NotImplementedError, match="fragile"):
        fetch_odds(29, "England - Premier League")
