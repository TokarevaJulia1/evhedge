"""Tests for evhedge.ranking."""

import pytest

from evhedge.config_io import ConfigError, load_full_config
from evhedge.ranking import _sharpe, load_configs_from_dir, rank_teams

_CONFIG_TEMPLATE = """\
team: "{team}"
sport: football
tournament: "Test Cup"
stages:
  - name: "Final"
    win_prob: {win_prob}
market:
  no_price: {no_price}
strategy:
  name: "none"
  no_stake_usd: 100
  hedge_mode: none
"""


def _write_config(tmp_path, filename, team, win_prob, no_price):
    path = tmp_path / filename
    path.write_text(
        _CONFIG_TEMPLATE.format(team=team, win_prob=win_prob, no_price=no_price),
        encoding="utf-8",
    )
    return path


@pytest.fixture
def three_configs(tmp_path):
    # Long shots (low win_prob) at increasingly favorable NO pricing give
    # increasingly less-negative EV: TeamC > TeamB > TeamA.
    path_a = _write_config(tmp_path, "team_a.yaml", "TeamA", win_prob=0.30, no_price=0.95)
    path_b = _write_config(tmp_path, "team_b.yaml", "TeamB", win_prob=0.10, no_price=0.97)
    path_c = _write_config(tmp_path, "team_c.yaml", "TeamC", win_prob=0.02, no_price=0.99)
    configs = [load_full_config(p) for p in (path_a, path_b, path_c)]
    return configs


def test_rank_teams_sorts_by_ev_descending(three_configs):
    rows = rank_teams(three_configs, mc_trials=3000, seed=1, sort_by="ev")

    evs = [row["ev"] for row in rows]
    assert evs == sorted(evs, reverse=True)
    assert [row["team"] for row in rows] == ["TeamC", "TeamB", "TeamA"]


def test_rank_teams_sort_by_ev_pct_is_consistent(three_configs):
    rows = rank_teams(three_configs, mc_trials=3000, seed=1, sort_by="ev_pct")

    pcts = [row["ev_pct"] for row in rows]
    assert pcts == sorted(pcts, reverse=True)
    # Same no_stake_usd for all three configs here, so ev and ev_pct rank
    # identically -- what matters is that sorting by ev_pct actually uses
    # ev_pct, not that the order necessarily differs from sort_by="ev".
    for row in rows:
        assert row["ev_pct"] == pytest.approx(row["ev"] / 100 * 100)


def test_rank_teams_invalid_sort_by_raises(three_configs):
    with pytest.raises(ValueError, match="sort_by"):
        rank_teams(three_configs, sort_by="not_a_real_key")


def test_rank_teams_sharpe_is_a_float_in_the_normal_case(three_configs):
    rows = rank_teams(three_configs, mc_trials=3000, seed=1, sort_by="ev")
    for row in rows:
        assert isinstance(row["sharpe"], float)


def test_sharpe_helper_returns_none_for_zero_std():
    """Direct unit test of the None-if-std==0 edge case: a real
    Bernoulli-per-stage Monte Carlo profit distribution can't naturally
    produce std == 0 (it always has at least two distinct outcome values
    with positive probability as long as 0 < win_prob < 1), so this is
    tested directly against the extracted _sharpe() helper instead of
    trying to contrive an unreachable simulate() scenario."""
    assert _sharpe(mean=10.0, std=0.0) is None
    assert _sharpe(mean=-5.0, std=0.0) is None


def test_sharpe_helper_normal_case():
    assert _sharpe(mean=10.0, std=5.0) == pytest.approx(2.0)


def test_rank_teams_sharpe_none_sorts_last(three_configs, monkeypatch):
    """Rows with sharpe=None must sort after all non-None rows, regardless
    of sort_by, rather than crashing the sort or landing in the middle."""
    import evhedge.ranking as ranking_module

    original_sharpe = ranking_module._sharpe
    # Force the first config's sharpe to None to check it lands last.
    calls = {"n": 0}

    def fake_sharpe(mean, std):
        calls["n"] += 1
        if calls["n"] == 1:
            return None
        return original_sharpe(mean, std)

    monkeypatch.setattr(ranking_module, "_sharpe", fake_sharpe)

    rows = rank_teams(three_configs, mc_trials=1000, seed=1, sort_by="sharpe")

    assert rows[-1]["sharpe"] is None
    non_none = [r["sharpe"] for r in rows[:-1]]
    assert all(v is not None for v in non_none)
    assert non_none == sorted(non_none, reverse=True)


def test_load_configs_from_dir_skips_broken_files(tmp_path):
    _write_config(tmp_path, "good_a.yaml", "TeamA", win_prob=0.3, no_price=0.95)
    _write_config(tmp_path, "good_b.yaml", "TeamB", win_prob=0.1, no_price=0.97)

    broken_path = tmp_path / "broken.yaml"
    broken_path.write_text(
        """
team: "Broken"
sport: football
tournament: "Test Cup"
stages:
  - name: "Final"
    win_prob: 0.5
market:
  no_price: 0.9
strategy:
  name: "none"
""",  # missing required strategy.no_stake_usd
        encoding="utf-8",
    )

    configs, failures = load_configs_from_dir(tmp_path)

    assert len(configs) == 2
    assert len(failures) == 1
    failed_path, message = failures[0]
    assert failed_path == broken_path
    assert "no_stake_usd" in message


def test_load_configs_from_dir_missing_directory_raises(tmp_path):
    with pytest.raises(ConfigError, match="директория не найдена"):
        load_configs_from_dir(tmp_path / "does_not_exist")


def test_load_configs_from_dir_no_yaml_files_raises(tmp_path):
    (tmp_path / "not_a_config.txt").write_text("hello", encoding="utf-8")
    with pytest.raises(ConfigError, match="не найдено"):
        load_configs_from_dir(tmp_path)
