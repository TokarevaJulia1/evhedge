"""Tests for evhedge.cli using click.testing.CliRunner."""

from pathlib import Path

from click.testing import CliRunner

from evhedge.cli import main

EXAMPLE_PATH = Path(__file__).resolve().parent.parent / "examples" / "football_example.yaml"


_RANK_CONFIG_TEMPLATE = """\
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


def _write_rank_config(dir_path, filename, team, win_prob, no_price):
    (dir_path / filename).write_text(
        _RANK_CONFIG_TEMPLATE.format(team=team, win_prob=win_prob, no_price=no_price),
        encoding="utf-8",
    )


def test_rank_command_prints_ordered_table(tmp_path):
    _write_rank_config(tmp_path, "team_a.yaml", "TeamA", win_prob=0.30, no_price=0.95)
    _write_rank_config(tmp_path, "team_b.yaml", "TeamB", win_prob=0.10, no_price=0.97)
    _write_rank_config(tmp_path, "team_c.yaml", "TeamC", win_prob=0.02, no_price=0.99)

    runner = CliRunner()
    result = runner.invoke(main, ["rank", str(tmp_path), "--mc", "1000", "--seed", "7"])

    assert result.exit_code == 0
    assert "TeamA" in result.output
    assert "TeamB" in result.output
    assert "TeamC" in result.output
    # Expected order (best EV first, see test_ranking.py for the math):
    # TeamC, TeamB, TeamA -- so their positions in the raw output text
    # should appear in that order.
    pos_c = result.output.index("TeamC")
    pos_b = result.output.index("TeamB")
    pos_a = result.output.index("TeamA")
    assert pos_c < pos_b < pos_a


def test_rank_command_reports_broken_configs_without_failing(tmp_path):
    _write_rank_config(tmp_path, "good.yaml", "TeamA", win_prob=0.3, no_price=0.95)
    (tmp_path / "broken.yaml").write_text("team: [unclosed", encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(main, ["rank", str(tmp_path), "--mc", "500"])

    assert result.exit_code == 0
    assert "TeamA" in result.output
    # The full path (including "broken.yaml") may be truncated by rich's
    # column width, so check for the error message text instead, which
    # confirms the failure was surfaced rather than silently dropped.
    assert "Пропущено" in result.output
    assert "не удалось распарсить YAML" in result.output


def test_rank_command_all_configs_broken_gives_clean_error(tmp_path):
    (tmp_path / "broken.yaml").write_text("team: [unclosed", encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(main, ["rank", str(tmp_path)])

    assert result.exit_code != 0
    assert "Traceback" not in result.output


def test_ev_command_prints_expected_value():
    runner = CliRunner()
    result = runner.invoke(main, ["ev", str(EXAMPLE_PATH)])

    assert result.exit_code == 0
    assert "18.62" in result.output


def test_ev_command_with_monte_carlo():
    runner = CliRunner()
    result = runner.invoke(main, ["ev", str(EXAMPLE_PATH), "--mc", "2000", "--seed", "42"])

    assert result.exit_code == 0
    assert "mean" in result.output
    assert "median" in result.output
    assert "prob_profit" in result.output


def test_ev_command_missing_file_gives_clean_error():
    runner = CliRunner()
    result = runner.invoke(main, ["ev", "does_not_exist.yaml"])

    assert result.exit_code != 0
    assert "Traceback" not in result.output
    assert "Ошибка конфигурации" in result.output


def test_ev_command_plot_without_mc_is_rejected():
    runner = CliRunner()
    result = runner.invoke(main, ["ev", str(EXAMPLE_PATH), "--plot"])

    assert result.exit_code != 0
    assert "--plot" in result.output
    assert "--mc" in result.output
    assert "Traceback" not in result.output


def test_example_then_ev_round_trip(tmp_path):
    out_path = tmp_path / "generated_example.yaml"
    runner = CliRunner()

    example_result = runner.invoke(main, ["example", "--out", str(out_path)])
    assert example_result.exit_code == 0
    assert out_path.exists()

    ev_result = runner.invoke(main, ["ev", str(out_path)])
    assert ev_result.exit_code == 0
    assert "Traceback" not in ev_result.output


def test_example_refuses_to_overwrite_without_force(tmp_path):
    out_path = tmp_path / "existing.yaml"
    out_path.write_text("placeholder", encoding="utf-8")
    runner = CliRunner()

    result = runner.invoke(main, ["example", "--out", str(out_path)], input="n\n")

    assert result.exit_code != 0
    assert out_path.read_text(encoding="utf-8") == "placeholder"


def test_example_overwrites_with_force(tmp_path):
    out_path = tmp_path / "existing.yaml"
    out_path.write_text("placeholder", encoding="utf-8")
    runner = CliRunner()

    result = runner.invoke(main, ["example", "--out", str(out_path), "--force"])

    assert result.exit_code == 0
    assert "placeholder" not in out_path.read_text(encoding="utf-8")


def test_example_unsupported_sport_gives_clean_error():
    runner = CliRunner()
    result = runner.invoke(main, ["example", "--sport", "tennis"])

    assert result.exit_code != 0
    assert "football" in result.output
    assert "Traceback" not in result.output
