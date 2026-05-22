"""Offline CLI exit-code contract tests (Phase 1 Task 8 / P8 + P7).

Exercises the error branches of `xtrade.cli` that exit with code 2
(configuration / precondition failure) without spinning up a
`BacktestEngine` or `TradingNode`. The success path of the backtest is
covered separately by `test_backtest_smoke.py`; the live path is covered
by `test_live_runner.py` (mainnet refusal et al.).

We avoid invoking any subcommand that constructs a Nautilus kernel from
this file — the Rust logger is global per-process and would collide with
`test_backtest_smoke.py` if both ran here.
"""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from xtrade.cli import app


runner = CliRunner(mix_stderr=False)


# ---------------------------------------------------------------------------
# Top-level help
# ---------------------------------------------------------------------------


def test_top_level_help_lists_subcommand_groups() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    out = result.stdout
    # Phase 1 brief Task 1 acceptance: `xtrade --help` lists data/backtest/live.
    assert "data" in out
    assert "backtest" in out
    assert "live" in out


def test_data_help_runs() -> None:
    result = runner.invoke(app, ["data", "--help"])
    assert result.exit_code == 0
    assert "ingest" in result.stdout
    assert "inspect" in result.stdout


def test_backtest_help_runs() -> None:
    result = runner.invoke(app, ["backtest", "--help"])
    assert result.exit_code == 0
    assert "run" in result.stdout


def test_live_help_runs() -> None:
    result = runner.invoke(app, ["live", "--help"])
    assert result.exit_code == 0
    assert "health" in result.stdout
    assert "run" in result.stdout


# ---------------------------------------------------------------------------
# `xtrade data ingest` — config / precondition errors (exit code 2)
# ---------------------------------------------------------------------------


def test_data_ingest_rejects_unknown_venue() -> None:
    result = runner.invoke(
        app,
        [
            "data", "ingest",
            "--venue", "kraken",
            "--symbol", "BTCUSDT",
            "--bar", "1m",
            "--since", "2026-01-01",
            "--until", "2026-01-02",
        ],
    )
    assert result.exit_code == 2
    assert "binance" in result.stderr.lower() or "hyperliquid" in result.stderr.lower()


def test_data_ingest_rejects_bad_bar_spec() -> None:
    result = runner.invoke(
        app,
        [
            "data", "ingest",
            "--venue", "binance",
            "--symbol", "BTCUSDT",
            "--bar", "garbage",
            "--since", "2026-01-01",
            "--until", "2026-01-02",
        ],
    )
    assert result.exit_code == 2


def test_data_ingest_rejects_inverted_window() -> None:
    result = runner.invoke(
        app,
        [
            "data", "ingest",
            "--venue", "binance",
            "--symbol", "BTCUSDT",
            "--bar", "1m",
            "--since", "2026-01-02",
            "--until", "2026-01-01",
        ],
    )
    assert result.exit_code == 2
    assert "after" in result.stderr.lower() or "until" in result.stderr.lower()


# ---------------------------------------------------------------------------
# `xtrade backtest run` — config errors only (no kernel construction)
# ---------------------------------------------------------------------------


def test_backtest_run_rejects_unknown_strategy() -> None:
    result = runner.invoke(
        app,
        [
            "backtest", "run",
            "--strategy", "does_not_exist",
            "--instrument", "BTCUSDT-PERP.BINANCE",
            "--bar", "1m",
        ],
    )
    assert result.exit_code == 2
    assert "strategy" in result.stderr.lower()


def test_backtest_run_rejects_bad_trade_size() -> None:
    result = runner.invoke(
        app,
        [
            "backtest", "run",
            "--strategy", "demo_ema",
            "--instrument", "BTCUSDT-PERP.BINANCE",
            "--bar", "1m",
            "--trade-size", "not-a-decimal",
        ],
    )
    assert result.exit_code == 2
    assert "trade-size" in result.stderr.lower() or "decimal" in result.stderr.lower()


def test_backtest_run_rejects_fast_ge_slow_ema() -> None:
    result = runner.invoke(
        app,
        [
            "backtest", "run",
            "--strategy", "demo_ema",
            "--instrument", "BTCUSDT-PERP.BINANCE",
            "--bar", "1m",
            "--fast-ema", "20",
            "--slow-ema", "10",
        ],
    )
    assert result.exit_code == 2
    assert "fast" in result.stderr.lower() and "slow" in result.stderr.lower()


def test_backtest_run_rejects_inverted_window() -> None:
    result = runner.invoke(
        app,
        [
            "backtest", "run",
            "--strategy", "demo_ema",
            "--instrument", "BTCUSDT-PERP.BINANCE",
            "--bar", "1m",
            "--since", "2026-02-01",
            "--until", "2026-01-01",
        ],
    )
    assert result.exit_code == 2


# ---------------------------------------------------------------------------
# `xtrade live health` — config errors (no kernel construction)
# ---------------------------------------------------------------------------


def test_live_health_rejects_unknown_venue_key(tmp_path: Path) -> None:
    # No yaml needed: venue-key validation happens before yaml load.
    result = runner.invoke(
        app,
        [
            "live", "health",
            "--venues", "ftx",
            "--timeout", "5",
            "--venues-yaml", str(tmp_path / "nope.yaml"),
        ],
    )
    assert result.exit_code == 2
    assert "ftx" in result.stderr.lower() or "venue" in result.stderr.lower()


def test_live_health_rejects_missing_yaml(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "live", "health",
            "--venues", "binance_spot",
            "--timeout", "5",
            "--venues-yaml", str(tmp_path / "does-not-exist.yaml"),
        ],
    )
    assert result.exit_code == 2
    assert "does not exist" in result.stderr.lower() or "config" in result.stderr.lower()


def test_live_health_rejects_empty_venues_list() -> None:
    result = runner.invoke(
        app,
        [
            "live", "health",
            "--venues", "",
            "--timeout", "5",
        ],
    )
    assert result.exit_code == 2


# ---------------------------------------------------------------------------
# `xtrade live run` — config errors (no kernel construction)
# ---------------------------------------------------------------------------


def test_live_run_rejects_unknown_strategy(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "live", "run",
            "--strategy", "does_not_exist",
            "--instrument", "BTCUSDT.BINANCE",
            "--venues-yaml", str(tmp_path / "nope.yaml"),
        ],
    )
    assert result.exit_code == 2
    assert "strategy" in result.stderr.lower()


def test_live_run_rejects_bad_side(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "live", "run",
            "--instrument", "BTCUSDT.BINANCE",
            "--side", "FLOAT",
            "--venues-yaml", str(tmp_path / "nope.yaml"),
        ],
    )
    assert result.exit_code == 2
    assert "side" in result.stderr.lower()


def test_live_run_rejects_bad_quantity(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "live", "run",
            "--instrument", "BTCUSDT.BINANCE",
            "--quantity", "not-a-decimal",
            "--venues-yaml", str(tmp_path / "nope.yaml"),
        ],
    )
    assert result.exit_code == 2


def test_live_run_rejects_non_positive_safety_multiplier(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "live", "run",
            "--instrument", "BTCUSDT.BINANCE",
            "--safety-multiplier", "-0.5",
            "--venues-yaml", str(tmp_path / "nope.yaml"),
        ],
    )
    assert result.exit_code == 2
    assert "safety" in result.stderr.lower() or "multiplier" in result.stderr.lower()


def test_live_run_rejects_missing_yaml(tmp_path: Path) -> None:
    # Strategy, side, qty all valid: failure must come from yaml resolution.
    result = runner.invoke(
        app,
        [
            "live", "run",
            "--instrument", "BTCUSDT.BINANCE",
            "--venues-yaml", str(tmp_path / "does-not-exist.yaml"),
        ],
    )
    assert result.exit_code == 2
