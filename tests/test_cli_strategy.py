"""Tests for `xtrade strategy` CLI subcommands (Phase 3 Task 5 / T5)."""

from __future__ import annotations

import json

from typer.testing import CliRunner

from xtrade.cli import app


runner = CliRunner()


def test_strategy_list_includes_momentum_follow() -> None:
    result = runner.invoke(app, ["strategy", "list"])
    assert result.exit_code == 0, result.output
    assert "momentum_follow" in result.output


def test_strategy_describe_emits_json_payload() -> None:
    result = runner.invoke(app, ["strategy", "describe", "momentum_follow"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["name"] == "momentum_follow"


def test_strategy_describe_unknown_returns_config_error() -> None:
    result = runner.invoke(app, ["strategy", "describe", "no_such_strategy"])
    # Exit code 2 = configuration/precondition failure per Phase 1 contract.
    assert result.exit_code == 2
