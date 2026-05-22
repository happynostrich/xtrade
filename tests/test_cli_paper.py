"""CLI surface tests for `xtrade paper run` (Phase 3 Task 5 / T5).

The full end-to-end `paper run` path lives in `tests/test_paper_runner.py`
and runs through a subprocess (Nautilus `BacktestEngine` cannot be
instantiated twice in one Python process). These tests cover only the
argument-validation surface, which doesn't touch the engine.
"""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from xtrade.cli import app


runner = CliRunner()


def test_paper_run_help() -> None:
    result = runner.invoke(app, ["paper", "run", "--help"])
    assert result.exit_code == 0
    assert "--strategy" in result.output
    assert "--signals-from" in result.output
    assert "--mode" in result.output


def test_paper_run_invalid_mode_returns_config_error(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "paper",
            "run",
            "--strategy",
            "momentum_follow",
            "--instrument",
            "BTCUSDT-PERP.BINANCE",
            "--signals-from",
            str(tmp_path / "signals"),
            "--mode",
            "bogus",
        ],
    )
    assert result.exit_code == 2


def test_paper_run_until_before_since_returns_config_error(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "paper",
            "run",
            "--strategy",
            "momentum_follow",
            "--instrument",
            "BTCUSDT-PERP.BINANCE",
            "--signals-from",
            str(tmp_path / "signals"),
            "--since",
            "2026-05-22T00:00:00Z",
            "--until",
            "2026-05-21T00:00:00Z",
        ],
    )
    assert result.exit_code == 2
