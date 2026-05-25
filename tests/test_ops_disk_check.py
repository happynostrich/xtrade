"""Tests for `xtrade.ops.disk.check_disk` (Phase 5 Track A4).

Coverage
--------
- Normal range: used_pct below warn → both flags False.
- Warn band: warn_pct <= used_pct < halt_pct → `warning=True`, `halt=False`.
- Halt band: used_pct >= halt_pct → both flags True.
- Out-of-bounds fallback: `shutil.disk_usage` raises OSError (path
  missing / network volume hiccup) → safe defaults, both flags False
  so we never trip the supervisor on a transient OS-level error.
- Integration: a fake `check_disk` returning `halt=True` from inside
  `_supervisor_iteration` writes the pause sentinel atomically and
  emits a `supervisor.disk.exhausted` event at ERROR level.
"""

from __future__ import annotations

import collections
import datetime as dt
import json
import logging
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

# Side-effect: registers `momentum_follow` so `load_strategy` works.
import xtrade.strategy  # noqa: F401
from xtrade.live.sentinel import Sentinel
from xtrade.live.supervisor import SupervisorConfig, run_supervisor
from xtrade.ops import disk as disk_module
from xtrade.ops.disk import DiskState, check_disk


UTC = dt.timezone.utc
SUPERVISOR_LOG = "xtrade.supervisor"


# ---- helpers -------------------------------------------------------------


_Usage = collections.namedtuple("_Usage", ("total", "used", "free"))


def _fake_usage(total: int, used: int) -> _Usage:
    """Build a `shutil.disk_usage` impostor with fixed total/used."""
    return _Usage(total=total, used=used, free=total - used)


def _events_of(caplog: pytest.LogCaptureFixture, name: str) -> list[dict[str, Any]]:
    """Parse caplog records emitted by `xtrade.supervisor` matching `event==name`."""
    out: list[dict[str, Any]] = []
    for rec in caplog.records:
        if rec.name != SUPERVISOR_LOG:
            continue
        try:
            payload = json.loads(rec.getMessage())
        except (TypeError, ValueError):
            continue
        if payload.get("event") == name:
            out.append(payload)
    return out


# ---- check_disk: pure unit tests -----------------------------------------


def test_check_disk_normal_band(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """used_pct < warn_pct → no flags raised."""
    # 50 % used: well below the 80 % warn threshold.
    monkeypatch.setattr(
        disk_module.shutil,
        "disk_usage",
        lambda _path: _fake_usage(total=1000, used=500),
    )
    state = check_disk(tmp_path, warn_pct=80, halt_pct=90)
    assert state.used_pct == 50
    assert state.free_bytes == 500
    assert state.warning is False
    assert state.halt is False
    assert state.path == tmp_path


def test_check_disk_warn_band(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """warn_pct <= used_pct < halt_pct → warning True, halt False."""
    # 85 % used: between 80 (warn) and 90 (halt).
    monkeypatch.setattr(
        disk_module.shutil,
        "disk_usage",
        lambda _path: _fake_usage(total=1000, used=850),
    )
    state = check_disk(tmp_path, warn_pct=80, halt_pct=90)
    assert state.used_pct == 85
    assert state.warning is True
    assert state.halt is False


def test_check_disk_halt_band(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """used_pct >= halt_pct → both flags True (halt implies warning)."""
    # 95 % used: above the 90 % halt threshold.
    monkeypatch.setattr(
        disk_module.shutil,
        "disk_usage",
        lambda _path: _fake_usage(total=1000, used=950),
    )
    state = check_disk(tmp_path, warn_pct=80, halt_pct=90)
    assert state.used_pct == 95
    assert state.warning is True
    assert state.halt is True


def test_check_disk_boundary_inclusive(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """A value equal to the halt threshold trips the halt flag.

    The brief defines the thresholds as `>=`, so 90 % used at a 90 %
    halt_pct must be a halt. We pin this so a future refactor that
    accidentally flips the comparison to `>` is caught here.
    """
    monkeypatch.setattr(
        disk_module.shutil,
        "disk_usage",
        lambda _path: _fake_usage(total=100, used=90),
    )
    state = check_disk(tmp_path, warn_pct=80, halt_pct=90)
    assert state.used_pct == 90
    assert state.warning is True
    assert state.halt is True


def test_check_disk_fallback_on_oserror(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """OSError from shutil.disk_usage → safe defaults, no flags raised.

    A transient OS-level error on the data volume (e.g. NFS bouncing,
    path disappearing during a remount) MUST NOT trip the supervisor's
    halt — that would amplify the outage. We return zeros + both flags
    False so the operator sees `disk used_pct=0 halt=false` in
    `xtrade ops status` and can investigate without the supervisor
    auto-pausing on top of the disk problem.
    """
    def _boom(_path: str) -> _Usage:
        raise OSError("path not found")

    monkeypatch.setattr(disk_module.shutil, "disk_usage", _boom)

    state = check_disk(tmp_path / "missing", warn_pct=80, halt_pct=90)
    assert state.used_pct == 0
    assert state.free_bytes == 0
    assert state.warning is False
    assert state.halt is False


def test_check_disk_fallback_on_zero_total(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """A reported `total=0` is treated as a probe failure (no div-by-zero)."""
    monkeypatch.setattr(
        disk_module.shutil,
        "disk_usage",
        lambda _path: _fake_usage(total=0, used=0),
    )
    state = check_disk(tmp_path, warn_pct=80, halt_pct=90)
    assert state.used_pct == 0
    assert state.warning is False
    assert state.halt is False


def test_check_disk_clamps_over_one_hundred(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """A `used > total` race (network volume remount) clamps to 100."""
    monkeypatch.setattr(
        disk_module.shutil,
        "disk_usage",
        lambda _path: _fake_usage(total=1000, used=1100),
    )
    state = check_disk(tmp_path, warn_pct=80, halt_pct=90)
    assert state.used_pct == 100
    assert state.halt is True


# ---- supervisor integration ---------------------------------------------


SYMBOL = "BTCUSDT-PERP.BINANCE"


def _supervisor_config(tmp_path: Path, *, var_root: Path) -> SupervisorConfig:
    return SupervisorConfig(
        instrument_id=SYMBOL,
        strategy_name="momentum_follow",
        signals_root=tmp_path / "signals",
        approvals_root=tmp_path / "approvals",
        cursor_path=tmp_path / "cursor.json",
        sentinel_path=tmp_path / "paused.flag",
        logs_root=tmp_path / "logs",
        approval_mode="auto",
        strategy_config={"notional_usd": Decimal("100")},
        poll_interval_s=0.0,
        venue_timeout_s=5.0,
        safety_multiplier=Decimal("0.7"),
        risk_rules=(),
        venues_cfg=None,
        bridge=None,
        var_root=var_root,
        disk_warn_pct=80,
        disk_halt_pct=90,
    )


def _noop_executor(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return {"passed": True, "summary": {}}


def test_supervisor_iteration_writes_sentinel_when_disk_halt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A `halt=True` probe inside the iteration writes the pause sentinel
    atomically and emits a `supervisor.disk.exhausted` event at ERROR.
    """
    var_root = tmp_path / "var"
    var_root.mkdir()
    config = _supervisor_config(tmp_path, var_root=var_root)

    # Inject a halt-True probe by replacing the resolver the supervisor
    # imports lazily inside `_supervisor_iteration`.
    def _fake_check_disk(path: Path, *, warn_pct: int, halt_pct: int) -> DiskState:
        assert path == var_root
        assert warn_pct == 80
        assert halt_pct == 90
        return DiskState(
            path=path,
            used_pct=95,
            free_bytes=42,
            warning=True,
            halt=True,
        )

    monkeypatch.setattr("xtrade.ops.disk.check_disk", _fake_check_disk)

    caplog.set_level(logging.INFO, logger="xtrade.supervisor")

    results = run_supervisor(
        config, live_executor=_noop_executor, max_iterations=1,
    )

    # The iteration first writes the sentinel, then re-reads it and
    # takes the paused branch (so signals_seen stays 0 and the
    # iteration result reports paused=True).
    assert len(results) == 1
    assert results[0].paused is True

    # Sentinel file is materialised with the expected reason + body.
    sentinel = Sentinel(config.sentinel_path)
    assert sentinel.paused() is True
    body = sentinel.state()
    assert body is not None
    assert body.get("reason") == "disk-exhausted"
    assert isinstance(body.get("paused_at"), str)

    # Structured event was emitted exactly once at ERROR level with the
    # probe data the operator needs to diagnose.
    exhausted = _events_of(caplog, "supervisor.disk.exhausted")
    assert len(exhausted) == 1
    payload = exhausted[0]
    assert payload["level"] == "ERROR"
    assert payload["used_pct"] == 95
    assert payload["free_bytes"] == 42
    assert payload["halt_pct"] == 90
    assert payload["path"] == str(var_root)


def test_supervisor_does_not_pause_when_disk_below_halt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Below halt → no sentinel write; below warn → no warning event."""
    var_root = tmp_path / "var"
    var_root.mkdir()
    config = _supervisor_config(tmp_path, var_root=var_root)

    def _fake_check_disk(path: Path, *, warn_pct: int, halt_pct: int) -> DiskState:
        return DiskState(
            path=path,
            used_pct=50,
            free_bytes=10_000,
            warning=False,
            halt=False,
        )

    monkeypatch.setattr("xtrade.ops.disk.check_disk", _fake_check_disk)
    caplog.set_level(logging.INFO, logger="xtrade.supervisor")

    results = run_supervisor(
        config, live_executor=_noop_executor, max_iterations=1,
    )

    assert results[0].paused is False
    assert Sentinel(config.sentinel_path).paused() is False
    assert _events_of(caplog, "supervisor.disk.exhausted") == []
    assert _events_of(caplog, "supervisor.disk.warning") == []


def test_supervisor_emits_warning_event_in_warn_band(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """In the warn band (warn <= used_pct < halt) the supervisor emits a
    `supervisor.disk.warning` event at WARNING level but does NOT pause.
    """
    var_root = tmp_path / "var"
    var_root.mkdir()
    config = _supervisor_config(tmp_path, var_root=var_root)

    def _fake_check_disk(path: Path, *, warn_pct: int, halt_pct: int) -> DiskState:
        return DiskState(
            path=path,
            used_pct=85,
            free_bytes=1_500,
            warning=True,
            halt=False,
        )

    monkeypatch.setattr("xtrade.ops.disk.check_disk", _fake_check_disk)
    caplog.set_level(logging.WARNING, logger="xtrade.supervisor")

    results = run_supervisor(
        config, live_executor=_noop_executor, max_iterations=1,
    )

    assert results[0].paused is False
    assert Sentinel(config.sentinel_path).paused() is False
    warn = _events_of(caplog, "supervisor.disk.warning")
    assert len(warn) == 1
    payload = warn[0]
    assert payload["level"] == "WARNING"
    assert payload["used_pct"] == 85
    assert payload["warn_pct"] == 80


def test_supervisor_skips_disk_probe_when_var_root_unset(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """`var_root=None` (default) keeps Phase 4 behaviour: probe never runs."""
    # Build a config without `var_root` (default None).
    config = SupervisorConfig(
        instrument_id=SYMBOL,
        strategy_name="momentum_follow",
        signals_root=tmp_path / "signals",
        approvals_root=tmp_path / "approvals",
        cursor_path=tmp_path / "cursor.json",
        sentinel_path=tmp_path / "paused.flag",
        logs_root=tmp_path / "logs",
        approval_mode="auto",
        strategy_config={"notional_usd": Decimal("100")},
        poll_interval_s=0.0,
        safety_multiplier=Decimal("0.7"),
        venues_cfg=None,
        bridge=None,
    )

    called: list[Path] = []

    def _trip(path: Path, *, warn_pct: int, halt_pct: int) -> DiskState:
        called.append(path)
        return DiskState(
            path=path, used_pct=99, free_bytes=0, warning=True, halt=True,
        )

    monkeypatch.setattr("xtrade.ops.disk.check_disk", _trip)
    caplog.set_level(logging.INFO, logger="xtrade.supervisor")

    results = run_supervisor(
        config, live_executor=_noop_executor, max_iterations=1,
    )

    assert called == []  # probe never invoked
    assert results[0].paused is False
    assert Sentinel(config.sentinel_path).paused() is False


# ---- yaml plumbing ------------------------------------------------------


def test_load_supervisor_config_parses_disk_fields(tmp_path: Path) -> None:
    """The yaml loader picks up `var_root` + override thresholds."""
    from xtrade.live.supervisor import load_supervisor_config

    yaml_text = """
instrument_id: BTCUSDT-PERP.BINANCE
strategy_name: momentum_follow
signals_root: /var/lib/xtrade/signals
approvals_root: /var/lib/xtrade/approvals
cursor_path: /var/lib/xtrade/signals/.cursor
sentinel_path: /run/xtrade/paused.flag
logs_root: /var/lib/xtrade/logs
var_root: /var/lib/xtrade
disk_warn_pct: 75
disk_halt_pct: 88
"""
    yaml_path = tmp_path / "supervisor.yaml"
    yaml_path.write_text(yaml_text, encoding="utf-8")

    config = load_supervisor_config(yaml_path)
    assert config.var_root == Path("/var/lib/xtrade")
    assert config.disk_warn_pct == 75
    assert config.disk_halt_pct == 88


def test_load_supervisor_config_defaults_when_omitted(tmp_path: Path) -> None:
    """Omitted fields keep Phase 4 behaviour: var_root None + 80/90 thresholds."""
    from xtrade.live.supervisor import load_supervisor_config

    yaml_text = """
instrument_id: BTCUSDT-PERP.BINANCE
strategy_name: momentum_follow
signals_root: /var/lib/xtrade/signals
approvals_root: /var/lib/xtrade/approvals
cursor_path: /var/lib/xtrade/signals/.cursor
sentinel_path: /run/xtrade/paused.flag
logs_root: /var/lib/xtrade/logs
"""
    yaml_path = tmp_path / "supervisor.yaml"
    yaml_path.write_text(yaml_text, encoding="utf-8")

    config = load_supervisor_config(yaml_path)
    assert config.var_root is None
    assert config.disk_warn_pct == 80
    assert config.disk_halt_pct == 90


# ---- collect_status integration -----------------------------------------


def test_collect_status_includes_disk_field(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`collect_status` populates `OpsStatus.disk` from `check_disk`.

    The JSON renderer must surface the same fields so `xtrade ops status
    --json` stays grep-able.
    """
    from xtrade.ops import OpsPaths, SupervisorState, collect_status, render_status_json

    var_root = tmp_path / "var"
    var_root.mkdir()

    monkeypatch.setattr(
        disk_module.shutil,
        "disk_usage",
        lambda _path: _fake_usage(total=1000, used=630),
    )

    paths = OpsPaths(
        signals_root=tmp_path / "signals",
        approvals_root=tmp_path / "approvals",
        cursor_path=tmp_path / "cursor",
        sentinel_path=tmp_path / "paused.flag",
        logs_root=tmp_path / "logs",
        audit_root=tmp_path / "audit",
        var_root=var_root,
    )

    status = collect_status(
        paths, probe_systemd=lambda _u: SupervisorState(state="unknown"),
    )
    assert status.disk.used_pct == 63
    assert status.disk.warning is False
    assert status.disk.halt is False

    body = json.loads(render_status_json(status))
    assert body["disk"]["used_pct"] == 63
    assert body["disk"]["warning"] is False
    assert body["disk"]["halt"] is False
    assert body["disk"]["path"] == str(var_root)


def test_render_status_text_has_disk_line() -> None:
    """The text renderer prints the brief-specified `xtrade.ops disk ...` line."""
    from xtrade.ops import (
        BridgeStatus, MLGateStatus, OpsStatus, SupervisorState, render_status_text,
    )

    status = OpsStatus(
        paused=False,
        paused_at=None,
        paused_reason=None,
        supervisor=SupervisorState(state="active"),
        last_signal_id=None,
        last_signal_age_s=None,
        last_cursor_update_age_s=None,
        pending_approvals=0,
        last_fill_run_id=None,
        last_fill_age_s=None,
        last_fill_passed=None,
        bridge=BridgeStatus(),
        ml_gate=MLGateStatus(),
        disk=DiskState(
            path=Path("/var/lib/xtrade"),
            used_pct=63,
            free_bytes=100_000,
            warning=False,
            halt=False,
        ),
        collected_at="2026-05-25T00:00:00+00:00",
    )
    rendered = render_status_text(status)
    assert "xtrade.ops disk used_pct=63 warning=false halt=false" in rendered
