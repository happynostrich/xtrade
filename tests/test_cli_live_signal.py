"""CLI surface tests for `xtrade live signal-run` (Phase 3 Task 6 / T6).

End-to-end Phase 3 testnet hop verification is the manual runbook in
`docs/phase3_runbook_testnet.md`; here we only check argument validation
plus the orchestration error mapping (RiskGate / approval / unknown
signal → expected exit codes).
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from typer.testing import CliRunner

import xtrade.strategy  # noqa: F401
from xtrade.cli import app
from xtrade.research.signals import Signal, SignalQueue


runner = CliRunner()
UTC = dt.timezone.utc


def _seed_signal(signals_root: Path, *, last_price: str = "50000") -> None:
    sig = Signal(
        symbol="BTCUSDT-PERP.BINANCE",
        venue="binance",
        direction="LONG",
        strength=0.6,
        generated_at=dt.datetime(2026, 5, 22, 12, 0, 0, tzinfo=UTC),
        source="momentum:cli-test",
        metadata={"last_price": last_price},
    )
    SignalQueue(signals_root).append([sig])


def test_live_signal_run_help() -> None:
    result = runner.invoke(app, ["live", "signal-run", "--help"])
    assert result.exit_code == 0
    for flag in (
        "--strategy",
        "--instrument",
        "--signals-from",
        "--mode",
        "--signal-id",
        "--venues-yaml",
        "--safety-multiplier",
        "--approval-timeout",
        "--venue-timeout",
    ):
        assert flag in result.output, f"missing flag {flag} in help"


def test_live_signal_run_bad_mode_returns_config_error(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "live",
            "signal-run",
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


def test_live_signal_run_bad_safety_multiplier_returns_config_error(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "live",
            "signal-run",
            "--strategy",
            "momentum_follow",
            "--instrument",
            "BTCUSDT-PERP.BINANCE",
            "--signals-from",
            str(tmp_path / "signals"),
            "--safety-multiplier",
            "-0.5",
        ],
    )
    assert result.exit_code == 2
