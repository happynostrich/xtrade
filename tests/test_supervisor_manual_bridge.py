"""Tests for supervisor manual-mode + bridge dispatch (Phase 4 Task 5 / T5).

These tests verify the non-blocking manual-approval state machine:

- A manual-mode signal parks an `_PendingIntent` and dispatches once
  via the bridge.
- The cursor advances even though the intent is parked (Phase 3 said
  the **batch** is committed once handled; "parked" is a terminal
  state for the current iteration).
- The next iteration, after the operator flips status=confirmed via
  `ApprovalQueue.patch(...)`, the supervisor promotes the row,
  invokes `live_executor`, and drops the slot from pending.
- A status=rejected flip drops the slot without invoking the executor.
- Restart re-discovers any still-pending rows so a crash mid-wait
  doesn't strand approvals (no double-dispatch via bridge here —
  on restart `dispatched` is set from `record.dispatch` annotation).
- Bridge dispatch failure is non-fatal: `_dispatch_via_bridge`
  catches exceptions, the slot remains parked, and the iteration
  completes normally.
"""

from __future__ import annotations

import datetime as dt
import threading
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

import xtrade.strategy  # noqa: F401 — registers momentum_follow
from xtrade.approval.queue import ApprovalQueue
from xtrade.bridge.openclaw_webhook import DispatchResult
from xtrade.live.supervisor import SupervisorConfig, run_supervisor
from xtrade.research.signals import Signal, SignalQueue


UTC = dt.timezone.utc
SYMBOL = "BTCUSDT-PERP.BINANCE"


# ---- stubs ---------------------------------------------------------------


class _StubBridge:
    """In-memory stand-in for OpenclawBridge.

    Mirrors the public surface the supervisor uses: `dispatch(record)`
    returning a `DispatchResult`, plus `close()`. Tests inspect
    `dispatched_ids` to assert the dispatch happened exactly once.
    """

    def __init__(self, *, ok: bool = True, raises: Exception | None = None) -> None:
        self.ok = ok
        self.raises = raises
        self.dispatched_ids: list[str] = []
        self.closed = False

    def dispatch(self, record, **kwargs) -> DispatchResult:  # noqa: ANN001
        if self.raises is not None:
            raise self.raises
        self.dispatched_ids.append(record.id)
        return DispatchResult(
            approval_id=record.id,
            ok=self.ok,
            status_code=200 if self.ok else 500,
            attempts=1,
            elapsed_s=0.01,
            error=None if self.ok else "stub-fail",
            response_excerpt=None,
            dispatched_at=dt.datetime(2026, 5, 24, 12, 0, tzinfo=UTC),
        )

    def close(self) -> None:
        self.closed = True


def _make_config(
    tmp_path: Path,
    *,
    bridge=None,
    sentinel_name: str = "paused.flag",
) -> SupervisorConfig:
    return SupervisorConfig(
        instrument_id=SYMBOL,
        strategy_name="momentum_follow",
        signals_root=tmp_path / "signals",
        approvals_root=tmp_path / "approvals",
        cursor_path=tmp_path / "cursor.json",
        sentinel_path=tmp_path / sentinel_name,
        logs_root=tmp_path / "logs",
        approval_mode="manual",
        strategy_config={"notional_usd": Decimal("100")},
        poll_interval_s=0.0,
        venue_timeout_s=5.0,
        safety_multiplier=Decimal("0.7"),
        risk_rules=(),
        venues_cfg=None,
        bridge=bridge,
    )


def _seed_signal(
    signals_root: Path,
    *,
    source: str = "momentum:manual-test",
    when: dt.datetime | None = None,
) -> Signal:
    sig = Signal(
        symbol=SYMBOL,
        venue="binance",
        direction="LONG",
        strength=0.6,
        generated_at=when or dt.datetime(2026, 5, 24, 12, 0, 0, tzinfo=UTC),
        source=source,
        metadata={"last_price": "50000"},
    )
    SignalQueue(signals_root).append([sig])
    return sig


def _make_executor():
    calls: list[dict[str, Any]] = []

    def _exec(venues_cfg, **kwargs):  # noqa: ANN001
        calls.append({"venues_cfg": venues_cfg, **kwargs})
        return {"passed": True, "summary": {"run_id": kwargs.get("run_id")}}

    return _exec, calls


# ---- park + dispatch -----------------------------------------------------


def test_manual_signal_parks_and_dispatches_once(tmp_path: Path) -> None:
    bridge = _StubBridge(ok=True)
    config = _make_config(tmp_path, bridge=bridge)
    _seed_signal(config.signals_root)
    executor, calls = _make_executor()

    results = run_supervisor(
        config, live_executor=executor, max_iterations=1,
    )

    r0 = results[0]
    assert r0.signals_seen == 1
    assert r0.intents_submitted == 0
    assert r0.intents_parked_manual == 1
    assert r0.pending_promoted == 0
    assert calls == []  # not submitted yet (awaiting human)
    assert len(bridge.dispatched_ids) == 1
    # bridge.close() runs on stop because the loop ends after max_iterations.
    assert bridge.closed is True


def _flip_after_first_iteration(
    approvals_root: Path, *, new_status: str
) -> threading.Event:
    """Return a `stop_event` whose `.wait()` flips the first pending row
    to `new_status` once (after iteration 1) before delegating to the
    real `Event.wait`. Lets us model "operator confirmed between polls"
    inside a single `run_supervisor(max_iterations=2)` call so the
    in-memory `pending` map persists across the flip.
    """
    state = {"flipped": False}
    queue = ApprovalQueue(approvals_root)
    event = threading.Event()
    original_wait = event.wait

    def _hooked_wait(timeout: float | None = None) -> bool:
        if not state["flipped"]:
            for row in queue:
                if row.status == "pending":
                    queue.patch(row.id, status=new_status, reason="hook-flipped")
                    state["flipped"] = True
                    break
        return original_wait(timeout)

    event.wait = _hooked_wait  # type: ignore[assignment]
    return event


def test_promotion_after_operator_confirms(tmp_path: Path) -> None:
    bridge = _StubBridge(ok=True)
    config = _make_config(tmp_path, bridge=bridge)
    _seed_signal(config.signals_root)
    executor, calls = _make_executor()

    stop_event = _flip_after_first_iteration(
        config.approvals_root, new_status="confirmed"
    )

    results = run_supervisor(
        config,
        stop_event=stop_event,
        live_executor=executor,
        max_iterations=2,
    )

    assert len(results) == 2
    # Iter 1: parked + dispatched
    assert results[0].intents_parked_manual == 1
    assert results[0].intents_submitted == 0
    assert len(bridge.dispatched_ids) == 1
    # Iter 2: pending drained → promoted → live_executor invoked once
    assert results[1].pending_promoted == 1
    assert results[1].pending_rejected == 0
    assert results[1].intents_submitted == 1  # promoted counts as submitted
    assert len(calls) == 1
    # Bridge dispatched exactly once (no re-dispatch on promotion).
    assert bridge.dispatched_ids == [bridge.dispatched_ids[0]]


def test_rejection_drops_slot_without_executor(tmp_path: Path) -> None:
    bridge = _StubBridge(ok=True)
    config = _make_config(tmp_path, bridge=bridge)
    _seed_signal(config.signals_root)
    executor, calls = _make_executor()

    stop_event = _flip_after_first_iteration(
        config.approvals_root, new_status="rejected"
    )

    results = run_supervisor(
        config,
        stop_event=stop_event,
        live_executor=executor,
        max_iterations=2,
    )

    assert len(results) == 2
    assert results[0].intents_parked_manual == 1
    # Iter 2: row flipped to rejected — slot drops without execution.
    assert results[1].pending_rejected == 1
    assert results[1].pending_promoted == 0
    assert results[1].intents_submitted == 0
    assert calls == []


# ---- no-bridge path ------------------------------------------------------


def test_manual_signal_without_bridge_still_parks(tmp_path: Path) -> None:
    """Brief §9 — supervisor must be usable without bridge in early soak."""
    config = _make_config(tmp_path, bridge=None)
    _seed_signal(config.signals_root)
    executor, calls = _make_executor()

    results = run_supervisor(
        config, live_executor=executor, max_iterations=1,
    )

    r0 = results[0]
    assert r0.intents_parked_manual == 1
    assert r0.intents_submitted == 0
    assert calls == []
    # Pending row exists for the operator to confirm via CLI.
    pending_rows = list(ApprovalQueue(config.approvals_root))
    assert len(pending_rows) == 1
    assert pending_rows[0].status == "pending"


# ---- restart re-discovery ------------------------------------------------


def test_restart_rediscovers_pending_without_redispatch(tmp_path: Path) -> None:
    """On restart the supervisor reloads pending rows from the queue.

    If the previous run already dispatched (queue row carries a
    `dispatch` annotation), the restart MUST NOT re-dispatch.
    """
    # Iteration A: park + dispatch + annotate-success (StubBridge doesn't
    # call ApprovalQueue.annotate; do it manually so the second run sees
    # `dispatch` populated).
    bridge_a = _StubBridge(ok=True)
    config_a = _make_config(tmp_path, bridge=bridge_a)
    _seed_signal(config_a.signals_root)
    executor_a, _ = _make_executor()
    run_supervisor(config_a, live_executor=executor_a, max_iterations=1)

    record_id = bridge_a.dispatched_ids[0]
    queue = ApprovalQueue(config_a.approvals_root)
    queue.annotate_dispatch_success(
        record_id,
        result={
            "approval_id": record_id,
            "status_code": 200,
            "attempts": 1,
            "elapsed_s": 0.01,
            "error": None,
            "response_excerpt": None,
            "dispatched_at": "2026-05-24T12:00:00+00:00",
        },
    )

    # Iteration B: fresh supervisor (simulated restart). Operator has not
    # decided yet. The supervisor should NOT re-dispatch the existing
    # pending row.
    bridge_b = _StubBridge(ok=True)
    config_b = _make_config(tmp_path, bridge=bridge_b)
    executor_b, calls_b = _make_executor()

    results = run_supervisor(
        config_b, live_executor=executor_b, max_iterations=1,
    )

    # No new signals (cursor was committed), no promotion (still
    # pending), no rejection — and crucially no re-dispatch.
    r0 = results[0]
    assert r0.signals_seen == 0
    assert r0.pending_promoted == 0
    assert r0.pending_rejected == 0
    assert bridge_b.dispatched_ids == []
    assert calls_b == []


# ---- bridge crash safety -------------------------------------------------


def test_bridge_exception_does_not_crash_supervisor(tmp_path: Path) -> None:
    """`_dispatch_via_bridge` must swallow exceptions so a misbehaving
    bridge cannot tear down the always-on loop."""
    bridge = _StubBridge(raises=RuntimeError("network on fire"))
    config = _make_config(tmp_path, bridge=bridge)
    _seed_signal(config.signals_root)
    executor, calls = _make_executor()

    results = run_supervisor(
        config, live_executor=executor, max_iterations=1,
    )

    r0 = results[0]
    # Intent still parked despite bridge blowing up.
    assert r0.intents_parked_manual == 1
    assert r0.errors == ()  # the loop didn't catch via the outer handler
    assert calls == []
