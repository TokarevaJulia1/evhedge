"""Pinnacle odds math: American/decimal conversion and de-vig.

Ported from ``C:\\polymarket_v3\\pinnacle_fetcher.py`` (``_devig``,
``_devig_3way``, ``_american_to_decimal``) -- pure math only, made public
(no longer prefixed with ``_``) since it's meant to be called directly
here, not just used internally by a fetcher.

The live network fetcher is deliberately NOT ported. v3's version worked by
hitting Pinnacle's guest API through an anti-bot session-cookie warm-up
(GET the homepage first to get cookies, or the API 401s) -- an unofficial,
fragile workaround that can break the moment Pinnacle changes its bot
protection. Odds go in by hand instead, via CLI/config, until/unless a
stable source is worth wiring up. ``fetch_odds`` exists only as a stub that
raises ``NotImplementedError`` with that explanation, so the reason isn't a
silent surprise.
"""

from __future__ import annotations


def american_to_decimal(price: int | float) -> float:
    """Convert American odds (e.g. +150, -200) to decimal odds."""
    if price > 0:
        return 1 + price / 100
    return 1 + 100 / abs(price)


def devig(home_dec: float, away_dec: float) -> tuple[float, float]:
    """Proportional (multiplicative) de-vig of a 2-way market.

    Returns (home_prob, away_prob) summing to 1.0.
    """
    rh = 1 / home_dec
    ra = 1 / away_dec
    total = rh + ra
    return rh / total, ra / total


def devig_3way(home_dec: float, draw_dec: float, away_dec: float) -> tuple[float, float]:
    """Proportional de-vig of a 3-way market (with a draw). Returns
    (home_win_prob, away_win_prob) -- draw is excluded from the return
    value (same convention as v3), though it's still used in the
    normalization denominator.
    """
    rh = 1 / home_dec
    rd = 1 / draw_dec
    ra = 1 / away_dec
    total = rh + rd + ra
    return rh / total, ra / total


def devig_range(decimal_odds: list[float]) -> tuple[list[float], list[float]]:
    """Two de-vig assumptions bracketing the true fair probabilities, for
    an N-way market -- we've been working with a RANGE all week, not a
    point estimate, because there's no way to know from the odds alone how
    a book actually distributes its margin.

    - Proportional: the standard assumption, margin spread across every
      outcome in proportion to its raw implied probability (same math as
      ``devig``/``devig_3way`` generalized to N outcomes).
    - All-margin-in-the-longshot: the opposite extreme assumption --
      every outcome except the single biggest longshot (smallest raw
      implied probability) keeps its raw implied probability exactly as
      quoted, and the longshot alone absorbs the entire overround
      (``fair_longshot = 1 - sum(raw probs of every other outcome)``).
      This is the highest plausible fair probability for the favorite(s)
      the odds can support.

    Args:
        decimal_odds: Decimal odds for every outcome of one market, at
            least 2.

    Returns:
        ``(proportional_probs, all_margin_in_longshot_probs)`` -- two
        lists, same order and length as ``decimal_odds``, each summing to
        1.0.

    Raises:
        ValueError: If fewer than 2 odds are given, or any is <= 1.0.
    """
    if len(decimal_odds) < 2:
        raise ValueError(f"devig_range needs at least 2 outcomes, got {len(decimal_odds)}")
    if any(d <= 1.0 for d in decimal_odds):
        raise ValueError(f"all decimal odds must be > 1.0, got {decimal_odds}")

    raw = [1.0 / d for d in decimal_odds]
    total = sum(raw)
    proportional = [r / total for r in raw]

    longshot_idx = min(range(len(raw)), key=lambda i: raw[i])
    others_sum = sum(r for i, r in enumerate(raw) if i != longshot_idx)
    all_margin_in_longshot = list(raw)
    all_margin_in_longshot[longshot_idx] = max(0.0, 1.0 - others_sum)

    return proportional, all_margin_in_longshot


def fetch_odds(sport_id: int, league_filter: str | None = None) -> list[dict]:
    """Not implemented -- see module docstring.

    v3's live fetcher depended on an unofficial guest-API session-cookie
    warm-up that can silently stop working whenever Pinnacle's anti-bot
    protection changes. Rather than port something that quietly rots,
    enter odds by hand (CLI/config) until a stable source exists.
    """
    raise NotImplementedError(
        "evhedge.data_sources.pinnacle.fetch_odds is a stub: the live Pinnacle "
        "guest-API fetcher from polymarket_v3 depends on a fragile, unofficial "
        "anti-bot session warm-up and was deliberately not ported. Enter "
        "Pinnacle odds manually via the CLI/config instead."
    )
