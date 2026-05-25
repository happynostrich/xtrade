"""Pure-filesystem status collector for `xtrade ops status`.

Why pure filesystem
-------------------
`xtrade ops status` is the operator's "what is going on?" command. It
must keep working when the supervisor is **dead**, so it can never go
through an in-process channel. Every field on :class:`OpsStatus` comes
from a file on disk that some other process is responsible for
maintaining:

============================  =====================================================
Field                          Source on disk
============================  =====================================================
``paused``                     ``/run/xtrade/paused.flag`` (sentinel file existence)
``paused_at`` / ``paused_reason`` ``/run/xtrade/paused.flag`` (json body)
``last_signal_id``             ``/var/lib/xtrade/signals/.cursor`` (`seen` last row)
``last_signal_age_s``          cursor row's ISO timestamp vs. now
``last_cursor_update_age_s``   cursor's top-level ``updated_at`` vs. now
``pending_approvals``          count of ``status == "pending"`` rows across all
                               ``approvals/<YYYY-MM-DD>.jsonl`` shards
``last_fill_run_id``           most recent ``logs/<run-id>/live_signal_summary.json``
``last_fill_age_s``            that file's ``completed_at``
``last_fill_passed``           that file's top-level ``passed`` boolean
``bridge.last_dispatch_*``     latest ``dispatch`` annotation across all approval
                               rows, sorted by ``dispatched_at``
``supervisor.state``           ``systemctl is-active <unit>`` (best-effort; falls
                               back to ``"unknown"`` if systemctl is unavailable)
``supervisor.uptime_s``        ``systemctl show -p ActiveEnterTimestamp <unit>``
============================  =====================================================

Failure mode
------------
Every helper here is "soft": missing files, corrupt JSON, missing keys
all degrade to ``None``. The ops CLI MUST NOT crash because a state file
got truncated by a crashed writer — that's exactly the scenario the
operator is investigating.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import subprocess
from pathlib import Path
from typing import Any, Callable

from xtrade.approval.queue import ApprovalQueue
from xtrade.live.sentinel import Sentinel
from xtrade.ops.disk import DiskState, check_disk


# ---- defaults & paths ---------------------------------------------------


DEFAULT_SENTINEL_PATH = Path("/run/xtrade/paused.flag")
DEFAULT_SIGNALS_ROOT = Path("/var/lib/xtrade/signals")
DEFAULT_APPROVALS_ROOT = Path("/var/lib/xtrade/approvals")
DEFAULT_CURSOR_PATH = Path("/var/lib/xtrade/signals/.cursor")
DEFAULT_LOGS_ROOT = Path("/var/lib/xtrade/logs")
DEFAULT_AUDIT_ROOT = Path("/var/lib/xtrade/audit")
DEFAULT_VAR_ROOT = Path("/var/lib/xtrade")
DEFAULT_SUPERVISOR_UNIT = "xtrade-supervisor.service"


@dataclasses.dataclass(frozen=True)
class OpsPaths:
    """Bundle of file-system locations ops reads.

    Tests pass a custom instance pointed at ``tmp_path``; production
    CLI defaults to the VPS layout from `docs/phase4_brief.md` §4.2.
    """

    signals_root: Path = DEFAULT_SIGNALS_ROOT
    approvals_root: Path = DEFAULT_APPROVALS_ROOT
    cursor_path: Path = DEFAULT_CURSOR_PATH
    sentinel_path: Path = DEFAULT_SENTINEL_PATH
    logs_root: Path = DEFAULT_LOGS_ROOT
    audit_root: Path = DEFAULT_AUDIT_ROOT
    var_root: Path = DEFAULT_VAR_ROOT
    supervisor_unit: str = DEFAULT_SUPERVISOR_UNIT

    @classmethod
    def vps_defaults(cls) -> "OpsPaths":
        return cls()


# ---- value types --------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class SupervisorState:
    """systemd-derived liveness summary.

    ``state`` is one of {``"active"``, ``"inactive"``, ``"activating"``,
    ``"deactivating"``, ``"failed"``, ``"unknown"``}. ``"unknown"`` is
    returned whenever systemctl is not on PATH or the unit name is not
    registered — i.e. in offline tests and developer workstations.
    """

    state: str
    uptime_s: float | None = None
    started_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "uptime_s": self.uptime_s,
            "started_at": self.started_at,
        }


@dataclasses.dataclass(frozen=True)
class BridgeStatus:
    """Most-recent bridge dispatch summary (any approval, any day).

    ``None`` everywhere means no bridge dispatch has been annotated to
    any approval row yet — either the bridge has never been wired up,
    or the supervisor only ran in auto/dry_run modes.
    """

    last_dispatch_ok: bool | None = None
    last_dispatch_status_code: int | None = None
    last_dispatch_error: str | None = None
    last_dispatch_at: str | None = None
    last_dispatch_approval_id: str | None = None
    last_dispatch_attempts: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "last_dispatch_ok": self.last_dispatch_ok,
            "last_dispatch_status_code": self.last_dispatch_status_code,
            "last_dispatch_error": self.last_dispatch_error,
            "last_dispatch_at": self.last_dispatch_at,
            "last_dispatch_approval_id": self.last_dispatch_approval_id,
            "last_dispatch_attempts": self.last_dispatch_attempts,
        }


@dataclasses.dataclass(frozen=True)
class MLGateStatus:
    """Aggregate of ML-gate decisions over the last 24 h.

    Source: ``<audit_root>/ml_gate.<YYYY-MM-DD>.jsonl`` (atomic-append
    jsonl written by `xtrade.strategy.ml_gate_audit.MLGateAuditWriter`).

    ``suppression_rate_24h`` is intentionally ``None`` when the sample
    size is below :data:`_MLGATE_RATE_MIN_SAMPLE` — an operator looking
    at "1 / 1 suppressed = 100 %" gets a noisier picture than "n/a".
    """

    suppressed_24h: int = 0
    allowed_24h: int = 0
    suppression_rate_24h: float | None = None
    last_event_age_s: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "suppressed_24h": self.suppressed_24h,
            "allowed_24h": self.allowed_24h,
            "suppression_rate_24h": self.suppression_rate_24h,
            "last_event_age_s": self.last_event_age_s,
        }


# Minimum decisions observed in the 24h window before reporting a rate.
_MLGATE_RATE_MIN_SAMPLE = 10


@dataclasses.dataclass(frozen=True)
class OpsStatus:
    """One-shot snapshot returned by :func:`collect_status`."""

    paused: bool
    paused_at: str | None
    paused_reason: str | None
    supervisor: SupervisorState
    last_signal_id: str | None
    last_signal_age_s: float | None
    last_cursor_update_age_s: float | None
    pending_approvals: int
    last_fill_run_id: str | None
    last_fill_age_s: float | None
    last_fill_passed: bool | None
    bridge: BridgeStatus
    ml_gate: MLGateStatus
    disk: DiskState
    collected_at: str

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "paused": self.paused,
            "paused_at": self.paused_at,
            "paused_reason": self.paused_reason,
            "supervisor": self.supervisor.to_dict(),
            "last_signal_id": self.last_signal_id,
            "last_signal_age_s": self.last_signal_age_s,
            "last_cursor_update_age_s": self.last_cursor_update_age_s,
            "pending_approvals": self.pending_approvals,
            "last_fill_run_id": self.last_fill_run_id,
            "last_fill_age_s": self.last_fill_age_s,
            "last_fill_passed": self.last_fill_passed,
            "bridge": self.bridge.to_dict(),
            "ml_gate": self.ml_gate.to_dict(),
            "disk": {
                "path": str(self.disk.path),
                "used_pct": self.disk.used_pct,
                "free_bytes": self.disk.free_bytes,
                "warning": self.disk.warning,
                "halt": self.disk.halt,
            },
            "collected_at": self.collected_at,
        }


# ---- top-level entry point ---------------------------------------------


def collect_status(
    paths: OpsPaths,
    *,
    now: dt.datetime | None = None,
    probe_systemd: Callable[[str], SupervisorState] | None = None,
) -> OpsStatus:
    """Read all status files and assemble an `OpsStatus`.

    Parameters
    ----------
    paths:
        Filesystem locations to inspect (production: ``OpsPaths.vps_defaults()``).
    now:
        Reference clock for all "age" computations. Tests inject a fixed
        UTC datetime; production passes ``None`` → ``datetime.now(UTC)``.
    probe_systemd:
        Optional override for the systemd liveness probe. Tests pass a
        stub returning a fixed :class:`SupervisorState` so they never
        actually shell out.
    """
    when = now or dt.datetime.now(tz=dt.timezone.utc)
    probe = probe_systemd or probe_systemd_default

    paused, paused_at, paused_reason = _read_sentinel(paths.sentinel_path)
    last_signal_id, last_signal_age_s, last_cursor_age_s = _read_cursor(
        paths.cursor_path, now=when,
    )
    pending = _count_pending_approvals(paths.approvals_root)
    last_fill_run_id, last_fill_age_s, last_fill_passed = _read_last_fill(
        paths.logs_root, now=when,
    )
    bridge = _read_last_bridge_dispatch(paths.approvals_root)
    ml_gate = _read_ml_gate_audit(paths.audit_root, now=when)
    disk = check_disk(paths.var_root)
    supervisor = probe(paths.supervisor_unit)

    return OpsStatus(
        paused=paused,
        paused_at=paused_at,
        paused_reason=paused_reason,
        supervisor=supervisor,
        last_signal_id=last_signal_id,
        last_signal_age_s=last_signal_age_s,
        last_cursor_update_age_s=last_cursor_age_s,
        pending_approvals=pending,
        last_fill_run_id=last_fill_run_id,
        last_fill_age_s=last_fill_age_s,
        last_fill_passed=last_fill_passed,
        bridge=bridge,
        ml_gate=ml_gate,
        disk=disk,
        collected_at=when.astimezone(dt.timezone.utc).isoformat(),
    )


# ---- individual readers -------------------------------------------------


def _read_sentinel(path: Path) -> tuple[bool, str | None, str | None]:
    sentinel = Sentinel(path)
    if not sentinel.paused():
        return False, None, None
    body = sentinel.state() or {}
    return True, body.get("paused_at"), body.get("reason")


def _read_cursor(
    path: Path, *, now: dt.datetime,
) -> tuple[str | None, float | None, float | None]:
    """Return ``(last_signal_id, last_signal_age_s, last_cursor_update_age_s)``.

    ``last_signal_id`` is the ``"<iso>|<symbol>|<source>"`` triple of
    the most recent ``seen`` row in the cursor file (sorted lex == sorted
    chronologically for ISO-8601 first elements). The cursor format is
    described in `xtrade.strategy.cursor`.
    """
    if not path.exists():
        return None, None, None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, None, None
    if not isinstance(payload, dict):
        return None, None, None

    cursor_age_s: float | None = None
    updated_at = payload.get("updated_at")
    if isinstance(updated_at, str):
        cursor_age_s = _age_from_iso(updated_at, now=now)

    seen = payload.get("seen")
    if not isinstance(seen, list) or not seen:
        return None, None, cursor_age_s

    valid_rows = [
        row for row in seen
        if isinstance(row, list)
        and len(row) == 3
        and all(isinstance(c, str) for c in row)
    ]
    if not valid_rows:
        return None, None, cursor_age_s

    # `seen` is sorted on write (see strategy.cursor.save) and the first
    # element is an ISO-8601 timestamp, so the lex-max row is the most
    # recent signal the consumer has acknowledged.
    last = max(valid_rows, key=lambda r: r[0])
    signal_id = "|".join(last)
    signal_age_s = _age_from_iso(last[0], now=now)
    return signal_id, signal_age_s, cursor_age_s


def _count_pending_approvals(approvals_root: Path) -> int:
    if not approvals_root.exists():
        return 0
    try:
        queue = ApprovalQueue(approvals_root)
        return sum(1 for record in queue if record.status == "pending")
    except (OSError, ValueError):
        return 0


def _read_last_fill(
    logs_root: Path, *, now: dt.datetime,
) -> tuple[str | None, float | None, bool | None]:
    """Most recent ``live_signal_summary.json`` across all run dirs.

    "Most recent" is by the file's ``completed_at`` field when readable,
    else by mtime (so a partially-written summary still degrades to the
    last successful one).
    """
    if not logs_root.exists():
        return None, None, None

    candidates: list[tuple[float, Path]] = []
    try:
        for run_dir in logs_root.iterdir():
            if not run_dir.is_dir():
                continue
            summary = run_dir / "live_signal_summary.json"
            if summary.is_file():
                try:
                    mtime = summary.stat().st_mtime
                except OSError:
                    continue
                candidates.append((mtime, summary))
    except OSError:
        return None, None, None

    if not candidates:
        return None, None, None

    # Prefer the file with the latest mtime; ties broken by path string.
    candidates.sort(key=lambda pair: (pair[0], str(pair[1])), reverse=True)
    _, latest = candidates[0]

    try:
        body = json.loads(latest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, None, None
    if not isinstance(body, dict):
        return None, None, None

    run_id = body.get("run_id") if isinstance(body.get("run_id"), str) else None
    completed_at = body.get("completed_at")
    age_s = _age_from_iso(completed_at, now=now) if isinstance(completed_at, str) else None
    passed_val = body.get("passed")
    passed = bool(passed_val) if isinstance(passed_val, bool) else None
    return run_id, age_s, passed


def _read_last_bridge_dispatch(approvals_root: Path) -> BridgeStatus:
    if not approvals_root.exists():
        return BridgeStatus()
    try:
        queue = ApprovalQueue(approvals_root)
        annotated = [r for r in queue if r.dispatch]
    except (OSError, ValueError):
        return BridgeStatus()
    if not annotated:
        return BridgeStatus()

    # Sort by `dispatched_at` (ISO-8601 lex == chrono). Missing field
    # sorts oldest so the most recent valid annotation wins.
    def _key(record) -> str:  # noqa: ANN001
        ts = record.dispatch.get("dispatched_at") if isinstance(record.dispatch, dict) else ""
        return ts if isinstance(ts, str) else ""

    annotated.sort(key=_key, reverse=True)
    latest = annotated[0]
    info = latest.dispatch if isinstance(latest.dispatch, dict) else {}
    return BridgeStatus(
        last_dispatch_ok=info.get("ok") if isinstance(info.get("ok"), bool) else None,
        last_dispatch_status_code=(
            info.get("status_code") if isinstance(info.get("status_code"), int) else None
        ),
        last_dispatch_error=info.get("error") if isinstance(info.get("error"), str) else None,
        last_dispatch_at=info.get("dispatched_at") if isinstance(info.get("dispatched_at"), str) else None,
        last_dispatch_approval_id=info.get("approval_id") if isinstance(info.get("approval_id"), str) else None,
        last_dispatch_attempts=info.get("attempts") if isinstance(info.get("attempts"), int) else None,
    )


# ---- systemd probe ------------------------------------------------------


def probe_systemd_default(unit_name: str) -> SupervisorState:
    """Best-effort systemd liveness probe.

    Returns ``state="unknown"`` when systemctl is missing (dev mac),
    when the call times out, or when the unit is not loaded — i.e.
    in every case where we are not sure what to report.
    """
    try:
        proc = subprocess.run(
            [
                "systemctl",
                "show",
                "-p", "ActiveState",
                "-p", "ActiveEnterTimestampMonotonic",
                "-p", "ActiveEnterTimestamp",
                "--value",
                unit_name,
            ],
            capture_output=True,
            text=True,
            timeout=2.0,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return SupervisorState(state="unknown")
    if proc.returncode != 0:
        return SupervisorState(state="unknown")

    lines = [ln.strip() for ln in proc.stdout.strip().split("\n") if ln.strip()]
    # `systemctl show --value` returns values in the order properties
    # were requested. We asked for 3 properties, so up to 3 lines.
    state = lines[0] if len(lines) >= 1 else "unknown"
    started_at_iso: str | None = None
    uptime_s: float | None = None
    if len(lines) >= 3:
        # ActiveEnterTimestamp is "Mon 2026-05-24 12:34:56 UTC". We don't
        # parse it; the monotonic counter (microseconds since boot) is
        # easier to reason about for uptime.
        active_enter = lines[2]
        if active_enter and active_enter not in {"n/a", "0", ""}:
            started_at_iso = active_enter
            uptime_s = _uptime_from_active_enter(active_enter)
    return SupervisorState(state=state or "unknown", uptime_s=uptime_s, started_at=started_at_iso)


def _uptime_from_active_enter(active_enter: str) -> float | None:
    """Best-effort parse of systemd's `ActiveEnterTimestamp` format.

    The format is locale-dependent (``"Mon 2026-05-24 12:34:56 UTC"``).
    We try a couple of common shapes and return ``None`` when nothing
    matches — operators will still see the literal string in
    ``started_at``.
    """
    for fmt in (
        "%a %Y-%m-%d %H:%M:%S %Z",
        "%a %Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M:%S %Z",
        "%Y-%m-%d %H:%M:%S",
    ):
        try:
            parsed = dt.datetime.strptime(active_enter, fmt)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=dt.timezone.utc)
            return max(0.0, (dt.datetime.now(tz=dt.timezone.utc) - parsed).total_seconds())
        except ValueError:
            continue
    return None


# ---- shared helpers -----------------------------------------------------


def _age_from_iso(iso: str | None, *, now: dt.datetime) -> float | None:
    if not isinstance(iso, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(iso)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return max(0.0, (now - parsed).total_seconds())


# ---- ML-gate audit reader (C2) ------------------------------------------


def _read_ml_gate_audit(audit_root: Path, *, now: dt.datetime) -> MLGateStatus:
    """Scan the last 2 day-shards of `ml_gate.<date>.jsonl` for 24h aggregates.

    Pure-filesystem; missing directory / corrupt rows degrade to zeros
    so `xtrade ops status` keeps working when the supervisor is dead.

    Two day-shards are enough to cover any 24 h window straddling
    midnight UTC. We never scan deeper to keep the call O(1) in disk
    history size.
    """
    empty = MLGateStatus()
    if not audit_root.exists() or not audit_root.is_dir():
        return empty

    today = now.astimezone(dt.timezone.utc).date()
    yesterday = today - dt.timedelta(days=1)
    cutoff = now - dt.timedelta(hours=24)

    suppressed = 0
    allowed = 0
    newest: dt.datetime | None = None

    for day in (yesterday, today):
        shard = audit_root / f"ml_gate.{day.isoformat()}.jsonl"
        if not shard.exists():
            continue
        try:
            text = shard.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts_raw = row.get("ts")
            if not isinstance(ts_raw, str):
                continue
            try:
                row_ts = dt.datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
            except ValueError:
                continue
            if row_ts.tzinfo is None:
                row_ts = row_ts.replace(tzinfo=dt.timezone.utc)
            if row_ts < cutoff:
                continue
            kind = row.get("kind")
            if kind == "allowed":
                allowed += 1
            elif kind == "suppressed":
                suppressed += 1
            else:
                continue
            if newest is None or row_ts > newest:
                newest = row_ts

    total = suppressed + allowed
    rate: float | None
    if total >= _MLGATE_RATE_MIN_SAMPLE:
        rate = round(100.0 * suppressed / total, 2)
    else:
        rate = None
    age = (now - newest).total_seconds() if newest is not None else None
    return MLGateStatus(
        suppressed_24h=suppressed,
        allowed_24h=allowed,
        suppression_rate_24h=rate,
        last_event_age_s=max(0.0, age) if age is not None else None,
    )


# ---- renderers ----------------------------------------------------------


def render_status_json(status: OpsStatus) -> str:
    """Pretty-printed JSON for ``--json`` mode (stable key order)."""
    return json.dumps(status.to_json_dict(), indent=2, sort_keys=True)


def render_status_text(status: OpsStatus) -> str:
    """Single-pass operator summary.

    Format is one line per logical group; the first line is grep-able
    one-liner (``paused=... supervisor=... pending=... last_signal=...``)
    matching the brief §5 Task 5 "单行+JSON 双格式" requirement.
    """
    one_liner = (
        f"paused={str(status.paused).lower()}"
        f" supervisor={status.supervisor.state}"
        f" uptime_s={_fmt_float(status.supervisor.uptime_s)}"
        f" pending={status.pending_approvals}"
        f" last_signal_age_s={_fmt_float(status.last_signal_age_s)}"
        f" last_fill_age_s={_fmt_float(status.last_fill_age_s)}"
        f" bridge={_fmt_dispatch(status.bridge)}"
    )
    lines: list[str] = [one_liner]
    lines.append(f"collected_at: {status.collected_at}")
    lines.append(f"supervisor.state: {status.supervisor.state}")
    if status.supervisor.uptime_s is not None:
        lines.append(f"supervisor.uptime_s: {status.supervisor.uptime_s:.1f}")
    if status.supervisor.started_at:
        lines.append(f"supervisor.started_at: {status.supervisor.started_at}")
    if status.paused:
        lines.append(f"paused: yes  at={status.paused_at}  reason={status.paused_reason!r}")
    else:
        lines.append("paused: no")
    lines.append(
        f"last_signal: id={status.last_signal_id}"
        f"  age_s={_fmt_float(status.last_signal_age_s)}"
    )
    if status.last_cursor_update_age_s is not None:
        lines.append(f"cursor_update_age_s: {status.last_cursor_update_age_s:.1f}")
    lines.append(f"pending_approvals: {status.pending_approvals}")
    lines.append(
        f"last_fill: run_id={status.last_fill_run_id}"
        f"  age_s={_fmt_float(status.last_fill_age_s)}"
        f"  passed={status.last_fill_passed}"
    )
    lines.append(f"bridge.last_dispatch: {_fmt_dispatch(status.bridge)}")
    lines.append(
        "ml_gate:"
        f" allowed_24h={status.ml_gate.allowed_24h}"
        f" suppressed_24h={status.ml_gate.suppressed_24h}"
        f" suppression_rate={_fmt_pct(status.ml_gate.suppression_rate_24h)}"
        f" last_event_age_s={_fmt_float(status.ml_gate.last_event_age_s)}"
    )
    lines.append(
        f"xtrade.ops disk used_pct={status.disk.used_pct}"
        f" warning={str(status.disk.warning).lower()}"
        f" halt={str(status.disk.halt).lower()}"
    )
    return "\n".join(lines)


def _fmt_pct(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:.1f}%"


def _fmt_float(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{value:.1f}"


def _fmt_dispatch(bridge: BridgeStatus) -> str:
    if bridge.last_dispatch_at is None:
        return "none"
    ok = bridge.last_dispatch_ok
    code = bridge.last_dispatch_status_code
    return (
        f"ok={ok} status={code}"
        f" approval_id={bridge.last_dispatch_approval_id}"
        f" at={bridge.last_dispatch_at}"
        f" attempts={bridge.last_dispatch_attempts}"
    )


__all__ = [
    "BridgeStatus",
    "DEFAULT_APPROVALS_ROOT",
    "DEFAULT_AUDIT_ROOT",
    "DEFAULT_CURSOR_PATH",
    "DEFAULT_LOGS_ROOT",
    "DEFAULT_SENTINEL_PATH",
    "DEFAULT_SIGNALS_ROOT",
    "DEFAULT_SUPERVISOR_UNIT",
    "DEFAULT_VAR_ROOT",
    "DiskState",
    "MLGateStatus",
    "OpsPaths",
    "OpsStatus",
    "SupervisorState",
    "check_disk",
    "collect_status",
    "probe_systemd_default",
    "render_status_json",
    "render_status_text",
]
