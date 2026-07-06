"""Integration: the shipped example configs must load and scan end to end.

These are the same files the README/CLI point users at, so they double as
living documentation -- if a scanner format change breaks them, this is
where it shows up first.
"""

from pathlib import Path

from evhedge.scanner import (
    FUEL_VERDICT_SORT_ORDER,
    load_scanner_config,
    rounds_to_title,
    scan,
    sort_candidates,
)

EXAMPLES_DIR = Path(__file__).resolve().parent.parent / "examples"


def test_wc2026_example_scans_end_to_end():
    config = load_scanner_config(EXAMPLES_DIR / "wc2026_bracket.yaml")

    # Pure knockout remainder: power model enabled, nothing excluded.
    assert config.power_model_enabled is True
    assert config.excluded_stages == []

    reports = scan(config)
    assert {r.team for r in reports} == {"England", "Portugal", "Norway", "Morocco"}

    # Model fills every unquoted pair -> no holes anywhere.
    for r in reports:
        assert r.data_complete is True
        assert r.sources_breakdown["no_data"] == 0
        assert r.fuel_verdict in ("SOLID", "THIN", "FAILS")

    # Mixed-depth bracket: Morocco is already in the QF (3 rounds to the
    # title), England still has its R16 to play (4 rounds).
    assert rounds_to_title(config.bracket, "Morocco") == 3
    assert rounds_to_title(config.bracket, "England") == 4

    # sort_candidates yields non-decreasing verdict rank.
    ranks = [FUEL_VERDICT_SORT_ORDER.index(r.fuel_verdict) for r in sort_candidates(reports)]
    assert ranks == sorted(ranks)


def test_ewc_dota_example_demonstrates_disabled_model_and_exclusions():
    config = load_scanner_config(EXAMPLES_DIR / "ewc_dota_bracket.yaml")

    # round_robin group -> model off for the whole tournament; group is
    # also not hedge_suitable -> excluded from the roll chain.
    assert config.power_model_enabled is False
    assert config.excluded_stages == ["group"]

    reports = scan(config)
    assert {r.team for r in reports} == {"PARIVISION", "Tundra", "Liquid"}

    # With the model off and only one quoted leg on the whole board, every
    # candidate's path has no_data holes -> banded aggregates, no point
    # values masquerading as knowledge.
    for r in reports:
        assert r.data_complete is False
        assert r.sources_breakdown["no_data"] > 0
        assert r.available_multiplier_range is not None
        low, high = r.available_multiplier_range
        assert low <= r.available_multiplier <= high

    # PARIVISION: quoted R1 leg, holes deeper; the FUEL verdict flips
    # across the 0.2/0.8 band (SOLID at cheap legs, FAILS at pricey ones)
    # -> honest INSUFFICIENT_DATA instead of a plausible number.
    parivision = next(r for r in reports if r.team == "PARIVISION")
    assert parivision.fuel_verdict == "INSUFFICIENT_DATA"
    assert parivision.hype_flag is not None  # recent_upset
