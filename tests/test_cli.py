"""Tests for evhedge.cli using click.testing.CliRunner."""

from pathlib import Path

from click.testing import CliRunner

from evhedge.cli import main

EXAMPLE_PATH = Path(__file__).resolve().parent.parent / "examples" / "football_example.yaml"


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
