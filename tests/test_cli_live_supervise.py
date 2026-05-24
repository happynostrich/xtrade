"""CLI surface tests for `xtrade live supervise` (Phase 4 Task 5 / T5).

Real end-to-end soak is the operator runbook (`docs/phase4_runbook_vps.md`
once written); here we only verify:

- `--help` lists the public flags.
- Missing `--config` is a usage error.
- Non-existent config path returns a config error (exit 2).
- A minimal supervisor.yaml runs the loop for `--max-iterations 1` and
  exits 0 with a "stopped after 1 iterations" summary on stdout.
- The CLI does NOT build a bridge when `OPENCLAW_*` env vars are absent.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

# Side-effect: registers `momentum_follow` so `load_supervisor_config`'s
# eventual `load_strategy("momentum_follow")` call succeeds inside the
# supervisor loop.
import xtrade.strategy  # noqa: F401
from xtrade.cli import app


runner = CliRunner()


def _write_supervisor_yaml(tmp_path: Path) -> Path:
    """Build a minimal supervisor.yaml pointing at tmp paths."""
    yaml_path = tmp_path / "supervisor.yaml"
    yaml_path.write_text(
        f"""\
instrument_id: BTCUSDT-PERP.BINANCE
strategy_name: momentum_follow
strategy_config:
  notional_usd: '100'
approval_mode: manual
signals_root: {tmp_path / "signals"}
approvals_root: {tmp_path / "approvals"}
cursor_path: {tmp_path / "cursor.json"}
sentinel_path: {tmp_path / "paused.flag"}
logs_root: {tmp_path / "logs"}
poll_interval_s: 0.0
venue_timeout_s: 5.0
safety_multiplier: '0.7'
""",
        encoding="utf-8",
    )
    return yaml_path


@pytest.fixture(autouse=True)
def _strip_openclaw_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure no real OPENCLAW_* env leak in to build a real bridge."""
    monkeypatch.delenv("OPENCLAW_GATEWAY", raising=False)
    monkeypatch.delenv("OPENCLAW_SHARED_SECRET", raising=False)
    monkeypatch.delenv("OPENCLAW_CALLBACK_BASE_URL", raising=False)


def test_supervise_help_lists_flags() -> None:
    result = runner.invoke(app, ["live", "supervise", "--help"])
    assert result.exit_code == 0
    for flag in ("--config", "--max-iterations", "--log-level"):
        assert flag in result.output, f"missing flag {flag} in help"


def test_supervise_missing_config_is_usage_error() -> None:
    result = runner.invoke(app, ["live", "supervise"])
    # typer treats a missing required Option as usage error (exit 2).
    assert result.exit_code != 0


def test_supervise_nonexistent_config_returns_config_error(tmp_path: Path) -> None:
    bogus = tmp_path / "missing.yaml"
    result = runner.invoke(
        app,
        ["live", "supervise", "--config", str(bogus), "--max-iterations", "1"],
    )
    assert result.exit_code != 0


def test_supervise_smoke_one_iteration(tmp_path: Path) -> None:
    yaml_path = _write_supervisor_yaml(tmp_path)

    result = runner.invoke(
        app,
        [
            "live",
            "supervise",
            "--config",
            str(yaml_path),
            "--max-iterations",
            "1",
            "--log-level",
            "WARNING",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "supervisor: stopped after 1 iterations" in result.output
    assert "submitted=0" in result.output
    assert "parked_manual=0" in result.output


def test_supervise_smoke_picks_up_seeded_signal(tmp_path: Path) -> None:
    """With a queued signal the supervisor parks it (manual mode)."""
    import datetime as dt

    from xtrade.research.signals import Signal, SignalQueue

    yaml_path = _write_supervisor_yaml(tmp_path)
    SignalQueue(tmp_path / "signals").append(
        [
            Signal(
                symbol="BTCUSDT-PERP.BINANCE",
                venue="binance",
                direction="LONG",
                strength=0.6,
                generated_at=dt.datetime(2026, 5, 24, 12, 0, tzinfo=dt.timezone.utc),
                source="momentum:cli-smoke",
                metadata={"last_price": "50000"},
            )
        ]
    )

    result = runner.invoke(
        app,
        [
            "live",
            "supervise",
            "--config",
            str(yaml_path),
            "--max-iterations",
            "1",
            "--log-level",
            "WARNING",
        ],
    )

    assert result.exit_code == 0, result.output
    # No bridge in env so the intent parks but isn't dispatched.
    assert "parked_manual=1" in result.output
    assert "submitted=0" in result.output
