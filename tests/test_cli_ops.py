"""Tests for `xtrade ops {status,pause,resume,kill}` (Phase 4 \u00a75 Task 5 / T7).

The ops surface is pure-filesystem: every status field is derived from
a file on disk (sentinel, signal cursor, approvals jsonl, log-directory
summary json). These tests stand up a tmp_path-rooted layout, populate
the relevant files, and assert both the structured :class:`OpsStatus`
and the CLI's text / JSON output.

systemd probing is monkey-patched out across the board so the tests
are deterministic on Linux CI (where ``systemctl is-active`` would
return some real state) and on macOS dev hosts (where systemctl is
absent).
"""

from __future__ import annotations

import datetime as dt
import json
import subprocess
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from xtrade.approval.queue import ApprovalQueue
from xtrade.cli import app
from xtrade.ops import (
    BridgeStatus,
    OpsPaths,
    OpsStatus,
    SupervisorState,
    collect_status,
    render_status_json,
    render_status_text,
)
from xtrade.ops import status as ops_status_module
from xtrade.strategy.intent import OrderIntent


UTC = dt.timezone.utc
runner = CliRunner()


# ---- fixtures -------------------------------------------------------------


def _paths(tmp_path: Path) -> OpsPaths:
    return OpsPaths(
        signals_root=tmp_path / "signals",
        approvals_root=tmp_path / "approvals",
        cursor_path=tmp_path / "signals" / ".cursor",
        sentinel_path=tmp_path / "paused.flag",
        logs_root=tmp_path / "logs",
        supervisor_unit="xtrade-supervisor.service",
    )


def _intent(*, fp_seed: str = "default") -> OrderIntent:
    return OrderIntent(
        venue="binance",
        symbol="BTCUSDT-PERP.BINANCE",
        side="BUY",
        order_type="MARKET",
        quantity=Decimal("0.002"),
        limit_price=None,
        reduce_only=False,
        time_in_force="IOC",
        source_signal_id=f"manual:{fp_seed}",
        created_at=dt.datetime(2026, 5, 24, 12, 0, 0, tzinfo=UTC),
    )


def _seed_pending(
    approvals_root: Path,
    *,
    created_at: dt.datetime | None = None,
    fp_seed: str = "default",
) -> str:
    queue = ApprovalQueue(approvals_root)
    when = created_at or dt.datetime(2026, 5, 24, 12, 0, 0, tzinfo=UTC)
    rec = queue.submit(_intent(fp_seed=fp_seed), mode="manual", status="pending", now=when)
    return rec.id


def _write_cursor(
    path: Path,
    *,
    seen: list[list[str]],
    updated_at: str = "2026-05-24T11:59:00+00:00",
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"version": 1, "updated_at": updated_at, "seen": seen}),
        encoding="utf-8",
    )


def _write_summary(
    logs_root: Path,
    *,
    run_id: str,
    completed_at: str,
    passed: bool = True,
) -> Path:
    run_dir = logs_root / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    summary = run_dir / "live_signal_summary.json"
    summary.write_text(
        json.dumps(
            {
                "run_id": run_id,
                "completed_at": completed_at,
                "passed": passed,
                "mode": "live_signal",
            }
        ),
        encoding="utf-8",
    )
    return summary


def _stub_probe(state: str = "unknown", uptime_s: float | None = None) -> Any:
    def _probe(unit_name: str) -> SupervisorState:  # noqa: ARG001
        return SupervisorState(state=state, uptime_s=uptime_s)

    return _probe


_FIXED_NOW = dt.datetime(2026, 5, 24, 12, 5, 0, tzinfo=UTC)


# ---- pure collector tests --------------------------------------------------


def test_collect_status_empty(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    status = collect_status(paths, now=_FIXED_NOW, probe_systemd=_stub_probe())

    assert status.paused is False
    assert status.paused_at is None
    assert status.paused_reason is None
    assert status.supervisor.state == "unknown"
    assert status.supervisor.uptime_s is None
    assert status.last_signal_id is None
    assert status.last_signal_age_s is None
    assert status.last_cursor_update_age_s is None
    assert status.pending_approvals == 0
    assert status.last_fill_run_id is None
    assert status.last_fill_age_s is None
    assert status.last_fill_passed is None
    assert status.bridge == BridgeStatus()
    assert status.collected_at == "2026-05-24T12:05:00+00:00"


def test_collect_status_reads_sentinel(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    paths.sentinel_path.parent.mkdir(parents=True, exist_ok=True)
    paths.sentinel_path.write_text(
        json.dumps({"paused_at": "2026-05-24T12:00:00+00:00", "reason": "maintenance"}),
        encoding="utf-8",
    )

    status = collect_status(paths, now=_FIXED_NOW, probe_systemd=_stub_probe())
    assert status.paused is True
    assert status.paused_at == "2026-05-24T12:00:00+00:00"
    assert status.paused_reason == "maintenance"


def test_collect_status_reads_cursor(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    _write_cursor(
        paths.cursor_path,
        seen=[
            ["2026-05-24T11:55:00+00:00", "ETHUSDT-PERP.BINANCE", "momentum:abc"],
            ["2026-05-24T12:00:00+00:00", "BTCUSDT-PERP.BINANCE", "momentum:abc"],
        ],
        updated_at="2026-05-24T12:00:30+00:00",
    )

    status = collect_status(paths, now=_FIXED_NOW, probe_systemd=_stub_probe())
    # most recent of two seen rows
    assert status.last_signal_id == "2026-05-24T12:00:00+00:00|BTCUSDT-PERP.BINANCE|momentum:abc"
    assert status.last_signal_age_s == pytest.approx(300.0)
    assert status.last_cursor_update_age_s == pytest.approx(270.0)


def test_collect_status_corrupt_cursor_degrades(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    paths.cursor_path.parent.mkdir(parents=True, exist_ok=True)
    paths.cursor_path.write_text("{not json", encoding="utf-8")

    status = collect_status(paths, now=_FIXED_NOW, probe_systemd=_stub_probe())
    assert status.last_signal_id is None
    assert status.last_signal_age_s is None
    assert status.last_cursor_update_age_s is None


def test_collect_status_counts_pending_only(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    a = _seed_pending(paths.approvals_root, fp_seed="alpha")
    _seed_pending(paths.approvals_root, fp_seed="beta")
    # Flip one of them so only one is pending
    ApprovalQueue(paths.approvals_root).patch(a, status="confirmed")

    status = collect_status(paths, now=_FIXED_NOW, probe_systemd=_stub_probe())
    assert status.pending_approvals == 1


def test_collect_status_reads_last_fill(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    # Older
    _write_summary(
        paths.logs_root,
        run_id="live-20260524T100000Z",
        completed_at="2026-05-24T10:00:30+00:00",
        passed=True,
    )
    # Newer (this one wins by mtime)
    newer = _write_summary(
        paths.logs_root,
        run_id="live-20260524T120000Z",
        completed_at="2026-05-24T12:00:30+00:00",
        passed=False,
    )
    # Bump mtime explicitly so order is deterministic regardless of FS.
    later = newer.stat().st_mtime + 5
    import os as _os
    _os.utime(newer, (later, later))

    status = collect_status(paths, now=_FIXED_NOW, probe_systemd=_stub_probe())
    assert status.last_fill_run_id == "live-20260524T120000Z"
    assert status.last_fill_age_s == pytest.approx(270.0)
    assert status.last_fill_passed is False


def test_collect_status_corrupt_summary_degrades(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    run_dir = paths.logs_root / "live-20260524T120000Z"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "live_signal_summary.json").write_text("{corrupt", encoding="utf-8")

    status = collect_status(paths, now=_FIXED_NOW, probe_systemd=_stub_probe())
    assert status.last_fill_run_id is None
    assert status.last_fill_age_s is None
    assert status.last_fill_passed is None


def test_collect_status_reads_bridge_dispatch(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    aid = _seed_pending(paths.approvals_root, fp_seed="gamma")
    ApprovalQueue(paths.approvals_root).annotate_dispatch_success(
        aid,
        result={
            "approval_id": aid,
            "ok": True,
            "status_code": 200,
            "attempts": 1,
            "elapsed_s": 0.2,
            "error": None,
            "response_excerpt": None,
            "dispatched_at": "2026-05-24T12:01:00+00:00",
        },
    )

    status = collect_status(paths, now=_FIXED_NOW, probe_systemd=_stub_probe())
    assert status.bridge.last_dispatch_ok is True
    assert status.bridge.last_dispatch_status_code == 200
    assert status.bridge.last_dispatch_approval_id == aid
    assert status.bridge.last_dispatch_at == "2026-05-24T12:01:00+00:00"
    assert status.bridge.last_dispatch_attempts == 1
    assert status.bridge.last_dispatch_error is None


def test_collect_status_picks_latest_bridge_dispatch(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    a = _seed_pending(paths.approvals_root, fp_seed="alpha")
    b = _seed_pending(paths.approvals_root, fp_seed="beta")
    queue = ApprovalQueue(paths.approvals_root)
    queue.annotate_dispatch_failure(
        a,
        result={
            "approval_id": a,
            "ok": False,
            "status_code": 500,
            "attempts": 4,
            "elapsed_s": 30.0,
            "error": "boom",
            "response_excerpt": "server error",
            "dispatched_at": "2026-05-24T11:30:00+00:00",
        },
    )
    queue.annotate_dispatch_success(
        b,
        result={
            "approval_id": b,
            "ok": True,
            "status_code": 200,
            "attempts": 1,
            "elapsed_s": 0.1,
            "error": None,
            "response_excerpt": None,
            "dispatched_at": "2026-05-24T12:01:00+00:00",
        },
    )

    status = collect_status(paths, now=_FIXED_NOW, probe_systemd=_stub_probe())
    # Latest by dispatched_at is `b`
    assert status.bridge.last_dispatch_approval_id == b
    assert status.bridge.last_dispatch_ok is True


def test_collect_status_uses_probe_systemd_default_signature() -> None:
    # When probe_systemd isn't passed, the module default is invoked.
    # We don't actually run it (it'd shell out); we just ensure it's
    # importable and callable with a single string arg.
    assert callable(ops_status_module.probe_systemd_default)
    # On the test host (macOS or CI without the unit) the default
    # collapses to state="unknown".
    state = ops_status_module.probe_systemd_default("xtrade-supervisor.service")
    assert state.state in {"unknown", "active", "inactive", "activating", "deactivating", "failed"}


# ---- renderer tests --------------------------------------------------------


def _rich_status() -> OpsStatus:
    return OpsStatus(
        paused=True,
        paused_at="2026-05-24T12:00:00+00:00",
        paused_reason="rolling secret",
        supervisor=SupervisorState(state="active", uptime_s=3600.0, started_at="2026-05-24T11:05:00+00:00"),
        last_signal_id="2026-05-24T12:00:00+00:00|BTCUSDT-PERP.BINANCE|momentum:abc",
        last_signal_age_s=300.0,
        last_cursor_update_age_s=120.0,
        pending_approvals=2,
        last_fill_run_id="live-20260524T120000Z",
        last_fill_age_s=270.0,
        last_fill_passed=True,
        bridge=BridgeStatus(
            last_dispatch_ok=True,
            last_dispatch_status_code=200,
            last_dispatch_at="2026-05-24T12:01:00+00:00",
            last_dispatch_approval_id="abc123",
            last_dispatch_attempts=1,
        ),
        collected_at="2026-05-24T12:05:00+00:00",
    )


def test_render_status_json_has_all_keys() -> None:
    body = json.loads(render_status_json(_rich_status()))
    assert body["paused"] is True
    assert body["paused_at"] == "2026-05-24T12:00:00+00:00"
    assert body["paused_reason"] == "rolling secret"
    assert body["supervisor"] == {
        "state": "active",
        "uptime_s": 3600.0,
        "started_at": "2026-05-24T11:05:00+00:00",
    }
    assert body["pending_approvals"] == 2
    assert body["last_fill_run_id"] == "live-20260524T120000Z"
    assert body["bridge"]["last_dispatch_ok"] is True
    assert body["bridge"]["last_dispatch_approval_id"] == "abc123"


def test_render_status_text_one_liner_is_first_line() -> None:
    rendered = render_status_text(_rich_status())
    first = rendered.split("\n", 1)[0]
    # The first line is the grep-friendly one-liner per brief \u00a75 Task 5.
    assert first.startswith("paused=true")
    assert "supervisor=active" in first
    assert "pending=2" in first
    assert "bridge=" in first


# ---- CLI: status ----------------------------------------------------------


@pytest.fixture(autouse=True)
def _stub_systemd_for_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    # Make the CLI's default systemd probe deterministic across hosts.
    monkeypatch.setattr(
        ops_status_module,
        "probe_systemd_default",
        lambda unit: SupervisorState(state="unknown"),
    )


def _cli_status(tmp_path: Path, *, json_out: bool = False) -> Any:
    paths = _paths(tmp_path)
    args = [
        "ops", "status",
        "--sentinel-path", str(paths.sentinel_path),
        "--signals-root", str(paths.signals_root),
        "--cursor-path", str(paths.cursor_path),
        "--approvals-root", str(paths.approvals_root),
        "--logs-root", str(paths.logs_root),
    ]
    if json_out:
        args.append("--json")
    return runner.invoke(app, args)


def test_cli_ops_status_empty_text(tmp_path: Path) -> None:
    result = _cli_status(tmp_path)
    assert result.exit_code == 0, result.output
    assert result.output.startswith("paused=false")
    assert "supervisor=unknown" in result.output
    assert "pending: 0\npending_approvals: 0" not in result.output  # sanity: no doubled line
    assert "pending_approvals: 0" in result.output


def test_cli_ops_status_empty_json_is_valid(tmp_path: Path) -> None:
    result = _cli_status(tmp_path, json_out=True)
    assert result.exit_code == 0, result.output
    body = json.loads(result.output)
    assert body["paused"] is False
    assert body["pending_approvals"] == 0
    assert body["bridge"]["last_dispatch_ok"] is None


def test_cli_ops_status_after_pause_shows_paused(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    pause_res = runner.invoke(
        app,
        ["ops", "pause", "--reason", "drill", "--sentinel-path", str(paths.sentinel_path)],
    )
    assert pause_res.exit_code == 0, pause_res.output

    status_res = _cli_status(tmp_path, json_out=True)
    body = json.loads(status_res.output)
    assert body["paused"] is True
    assert body["paused_reason"] == "drill"
    assert body["paused_at"].startswith("20")  # ISO-ish


# ---- CLI: pause / resume --------------------------------------------------


def test_cli_ops_pause_writes_sentinel(tmp_path: Path) -> None:
    sentinel = tmp_path / "paused.flag"
    result = runner.invoke(
        app,
        ["ops", "pause", "--reason", "manual_check", "--sentinel-path", str(sentinel)],
    )
    assert result.exit_code == 0, result.output
    assert sentinel.exists()
    body = json.loads(sentinel.read_text(encoding="utf-8"))
    assert body["reason"] == "manual_check"
    assert "paused_at" in body
    assert "paused: at=" in result.output


def test_cli_ops_resume_removes_sentinel(tmp_path: Path) -> None:
    sentinel = tmp_path / "paused.flag"
    sentinel.write_text(
        json.dumps({"paused_at": "2026-05-24T12:00:00+00:00", "reason": ""}),
        encoding="utf-8",
    )
    result = runner.invoke(app, ["ops", "resume", "--sentinel-path", str(sentinel)])
    assert result.exit_code == 0, result.output
    assert not sentinel.exists()
    assert "resumed: removed" in result.output


def test_cli_ops_resume_when_not_paused_is_noop(tmp_path: Path) -> None:
    sentinel = tmp_path / "paused.flag"
    result = runner.invoke(app, ["ops", "resume", "--sentinel-path", str(sentinel)])
    assert result.exit_code == 0, result.output
    assert "not_paused" in result.output


def test_cli_ops_pause_idempotent(tmp_path: Path) -> None:
    sentinel = tmp_path / "paused.flag"
    runner.invoke(app, ["ops", "pause", "--sentinel-path", str(sentinel), "--reason", "first"])
    second = runner.invoke(
        app, ["ops", "pause", "--sentinel-path", str(sentinel), "--reason", "second"],
    )
    assert second.exit_code == 0, second.output
    body = json.loads(sentinel.read_text(encoding="utf-8"))
    assert body["reason"] == "second"


# ---- CLI: kill ------------------------------------------------------------


def test_cli_ops_kill_requires_yes(tmp_path: Path) -> None:
    result = runner.invoke(app, ["ops", "kill"])
    assert result.exit_code == 2
    assert "refusing to stop" in result.output or "destructive" in result.output


def test_cli_ops_kill_invokes_systemctl(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    class _Proc:
        returncode = 0
        stderr = ""
        stdout = ""

    def _fake_run(cmd, **kwargs):  # noqa: ANN001
        calls.append(list(cmd))
        return _Proc()

    monkeypatch.setattr(subprocess, "run", _fake_run)
    result = runner.invoke(app, ["ops", "kill", "--yes"])
    assert result.exit_code == 0, result.output
    assert calls == [["systemctl", "stop", "xtrade-supervisor.service"]]
    assert "stopped: xtrade-supervisor.service" in result.output


def test_cli_ops_kill_propagates_systemctl_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Proc:
        returncode = 5
        stderr = "Unit xtrade-supervisor.service not loaded."
        stdout = ""

    def _fake_run(cmd, **kwargs):  # noqa: ANN001, ARG001
        return _Proc()

    monkeypatch.setattr(subprocess, "run", _fake_run)
    result = runner.invoke(app, ["ops", "kill", "--yes"])
    assert result.exit_code == 5
    assert "not loaded" in result.output


def test_cli_ops_kill_handles_missing_systemctl(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fake_run(cmd, **kwargs):  # noqa: ANN001, ARG001
        raise FileNotFoundError("no systemctl on this host")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    result = runner.invoke(app, ["ops", "kill", "--yes"])
    assert result.exit_code == 2
    assert "systemctl not found" in result.output


def test_cli_ops_kill_custom_unit(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    class _Proc:
        returncode = 0
        stderr = ""
        stdout = ""

    def _fake_run(cmd, **kwargs):  # noqa: ANN001
        calls.append(list(cmd))
        return _Proc()

    monkeypatch.setattr(subprocess, "run", _fake_run)
    result = runner.invoke(
        app, ["ops", "kill", "--yes", "--supervisor-unit", "xtrade-bridge.service"],
    )
    assert result.exit_code == 0, result.output
    assert calls == [["systemctl", "stop", "xtrade-bridge.service"]]
