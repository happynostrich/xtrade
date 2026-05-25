"""CLI surface tests for `xtrade live supervise` (Phase 4 Task 5 / T5).

Real end-to-end soak is the operator runbook (`docs/phase4_runbook_vps.md`
once written); here we only verify:

- `--help` lists the public flags.
- Missing `--config` is a usage error.
- Non-existent config path returns a config error (exit 2).
- A minimal supervisor.yaml runs the loop for `--max-iterations 1` and
  exits 0 with a "stopped after 1 iterations" summary on stdout.
- The CLI does NOT build a bridge when `OPENCLAW_*` env vars are absent.

Phase 5 A6(c) extension
-----------------------
Bug 8 root cause was that `_write_supervisor_yaml` never populated
`risk_yaml` / `venues_yaml`, so `load_supervisor_config`'s risk loader +
venues loader paths were never exercised offline. A Path-vs-str coercion
bug in `load_rules_from_yaml` slipped through every Phase 4 test as a
result. This file now also covers the full prod loader chain via
`_write_risk_yaml` + `_write_venues_yaml` helpers plus a unit test on
`load_supervisor_config` and a CLI smoke test that forces both loaders
to run.
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


# Env-var names referenced by the test venues.yaml. Kept distinct from
# real `BINANCE_*_TESTNET_*` env vars so a developer machine that has
# real credentials loaded cannot accidentally make the test pass for
# the wrong reason.
_TEST_BINANCE_API_KEY_ENV = "XTRADE_TEST_BINANCE_SPOT_API_KEY"
_TEST_BINANCE_API_SECRET_ENV = "XTRADE_TEST_BINANCE_SPOT_API_SECRET"


def _write_supervisor_yaml(
    tmp_path: Path,
    *,
    risk_yaml: Path | None = None,
    venues_yaml: Path | None = None,
    persistent_node: bool = True,
) -> Path:
    """Build a minimal supervisor.yaml pointing at tmp paths.

    When `risk_yaml` / `venues_yaml` are provided the supervisor.yaml
    references them so `load_supervisor_config` exercises the risk +
    venues loader paths (Phase 5 A6(c) coverage). `persistent_node` is
    exposed so callers wiring a real `venues_cfg` can flip it off to
    avoid `run_supervisor` building a real `PersistentLiveExecutor`
    (which would try to dial Binance).
    """
    extras: list[str] = []
    if risk_yaml is not None:
        extras.append(f"risk_yaml: {risk_yaml}")
    if venues_yaml is not None:
        extras.append(f"venues_yaml: {venues_yaml}")
    extras.append(f"persistent_node: {str(persistent_node).lower()}")
    extras_blob = "\n".join(extras)

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
{extras_blob}
""",
        encoding="utf-8",
    )
    return yaml_path


def _write_risk_yaml(tmp_path: Path) -> Path:
    """Build a risk.yaml exercising every loader branch.

    Mirrors `config/risk.example.yaml` so the loader installs all four
    rule classes (`MaxNotionalPerOrder`, `MaxPositionPerSymbol`,
    `MaxTotalNotional`, `MaxDrawdownPct`). Values are deliberately small
    enough to be obviously a test config.
    """
    path = tmp_path / "risk.yaml"
    path.write_text(
        """\
max_notional_per_order_usd: 1000
max_position_per_symbol_usd: 5000
max_total_notional_usd: 20000
max_drawdown_pct: 0.10
""",
        encoding="utf-8",
    )
    return path


def _write_venues_yaml(tmp_path: Path) -> Path:
    """Build a binance-spot-testnet venues.yaml.

    References test-specific env-var NAMES (`XTRADE_TEST_BINANCE_*`) so
    each test can monkeypatch them without colliding with whatever real
    `BINANCE_SPOT_TESTNET_API_*` the developer has in `.env`.
    """
    path = tmp_path / "venues.yaml"
    path.write_text(
        f"""\
binance:
  environment: TESTNET
  spot:
    api_key_env: {_TEST_BINANCE_API_KEY_ENV}
    api_secret_env: {_TEST_BINANCE_API_SECRET_ENV}
    key_type: HMAC
    account_type: SPOT
""",
        encoding="utf-8",
    )
    return path


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


# ---------------------------------------------------------------------------
# Phase 5 A6(c) — exercise the real risk_yaml + venues_yaml loader chain.
#
# Bug 8 (Path-vs-str coercion in `load_rules_from_yaml`) slipped through
# the entire Phase 4 offline suite because the existing supervisor smoke
# test never wrote a `risk_yaml:` or `venues_yaml:` key. These tests close
# that gap: a direct unit test on `load_supervisor_config` plus a CLI
# smoke test that forces `--max-iterations 1` to run with both loaders
# wired.
# ---------------------------------------------------------------------------


def test_load_supervisor_config_loads_real_risk_yaml(tmp_path: Path) -> None:
    """`load_supervisor_config` must install every risk rule from a real
    risk.yaml. Regression guard for Bug 8 (Path-vs-str coercion).
    """
    from decimal import Decimal

    from xtrade.live.supervisor import load_supervisor_config
    from xtrade.risk.rules import (
        MaxDrawdownPct,
        MaxNotionalPerOrder,
        MaxPositionPerSymbol,
        MaxTotalNotional,
    )

    risk_yaml = _write_risk_yaml(tmp_path)
    yaml_path = _write_supervisor_yaml(
        tmp_path,
        risk_yaml=risk_yaml,
        persistent_node=False,
    )

    cfg = load_supervisor_config(yaml_path)

    # All four rule classes from config/risk.example.yaml must be present.
    rule_types = {type(r) for r in cfg.risk_rules}
    assert rule_types == {
        MaxNotionalPerOrder,
        MaxPositionPerSymbol,
        MaxTotalNotional,
        MaxDrawdownPct,
    }, f"loader dropped rules: {rule_types}"

    # Decimal values round-tripped through yaml (no float coercion).
    rules_by_type = {type(r): r for r in cfg.risk_rules}
    assert rules_by_type[MaxNotionalPerOrder].usd_cap == Decimal("1000")
    assert rules_by_type[MaxPositionPerSymbol].usd_cap == Decimal("5000")
    assert rules_by_type[MaxTotalNotional].usd_cap == Decimal("20000")
    assert rules_by_type[MaxDrawdownPct].pct == Decimal("0.10")


def test_load_supervisor_config_loads_real_venues_yaml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`load_supervisor_config` must resolve `venues_yaml:` into a real
    `VenuesConfig` (binance TESTNET spot).
    """
    from xtrade.live.supervisor import load_supervisor_config

    monkeypatch.setenv(_TEST_BINANCE_API_KEY_ENV, "test-dummy-key")
    monkeypatch.setenv(_TEST_BINANCE_API_SECRET_ENV, "test-dummy-secret")

    venues_yaml = _write_venues_yaml(tmp_path)
    yaml_path = _write_supervisor_yaml(
        tmp_path,
        venues_yaml=venues_yaml,
        persistent_node=False,
    )

    cfg = load_supervisor_config(yaml_path)

    assert cfg.venues_cfg is not None
    assert cfg.venues_cfg.binance is not None
    assert cfg.venues_cfg.binance.spot is not None
    assert cfg.venues_cfg.binance.spot.api_key == "test-dummy-key"
    assert cfg.venues_cfg.binance.spot.api_secret == "test-dummy-secret"
    # Cross-check the venue env classification (TESTNET → not mainnet).
    assert cfg.venues_cfg.binance.spot.environment == "TESTNET"
    # `source_path` echoes the yaml location (Task 7 audit hook).
    assert cfg.venues_cfg.source_path == venues_yaml


def test_supervisor_loads_real_risk_yaml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end CLI smoke that forces both the risk loader *and* the
    venues loader to run. Per brief §A6(c) this is the regression guard
    that would have caught Bug 8 (Path-vs-str coercion) in Phase 4.

    `persistent_node: false` keeps the supervisor on the legacy
    one-intent-one-`run_live` path so it never tries to dial Binance with
    the dummy creds. With manual approval mode and no signals queued
    `run_live` is never invoked at all.
    """
    monkeypatch.setenv(_TEST_BINANCE_API_KEY_ENV, "test-dummy-key")
    monkeypatch.setenv(_TEST_BINANCE_API_SECRET_ENV, "test-dummy-secret")

    risk_yaml = _write_risk_yaml(tmp_path)
    venues_yaml = _write_venues_yaml(tmp_path)
    yaml_path = _write_supervisor_yaml(
        tmp_path,
        risk_yaml=risk_yaml,
        venues_yaml=venues_yaml,
        persistent_node=False,
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
    assert "supervisor: stopped after 1 iterations" in result.output
    assert "submitted=0" in result.output
    assert "parked_manual=0" in result.output
