"""Smoke test di M0: il pacchetto si importa e la CLI risponde."""

from typer.testing import CliRunner

from tslow.cli.app import app
from tslow.config import load_settings


def test_cli_version() -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert "tslow" in result.stdout


def test_settings_load() -> None:
    settings = load_settings()
    assert settings.get("governor", "budget_cpu_percent") == 0.25
    assert settings.get("thresholds", "cpu", "critical_percent") == 95
