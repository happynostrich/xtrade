"""Offline tests for `xtrade.live.signal_runner.run_live_signal` (Phase 3 Task 6 / T6).

The actual testnet hop (Phase 1 `run_live`) is real-network and lives
behind an injectable `live_executor` callable. These tests exercise the
orchestration around it — signal lookup, strategy invocation, RiskGate,
ApprovalGate, manual polling, dry-run short-circuit, and summary
shape — by feeding a stub executor that records what it would have
submitted.

End-to-end testnet verification is a manual operator runbook
(`docs/phase3_runbook_testnet.md`); the brief intentionally forbids
automated network tests.
"""

from __future__ import annotations

import datetime as dt
import json
import threading
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

import xtrade.strategy  # noqa: F401  — side-effect: registers momentum_follow
from xtrade.approval import ApprovalQueue
from xtrade.live.signal_runner import (
    ApprovalRejectedError,
    ApprovalTimeoutError,
    LiveSignalError,
    NoMatchingSignalError,
    RiskRejectedError,
    StrategyEmittedNothingError,
    _signal_composite_id,
    run_live_signal,
)
from xtrade.research.signals import Signal, SignalQueue
from xtrade.risk.rules import MaxNotionalPerOrder


UTC = dt.timezone.utc
SYMBOL = "BTCUSDT-PERP.BINANCE"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_signal(
    signals_root: Path,
    *,
    direction: str = "LONG",
    last_price: str = "50000",
    when: dt.datetime | None = None,
    source: str = "momentum:deadbeef",
    symbol: str = SYMBOL,
) -> Signal:
    """Write a single signal to the queue. Returns the materialised Signal."""
    sig = Signal(
        symbol=symbol,
        venue="binance",
        direction=direction,  # type: ignore[arg-type]
        strength=0.6 if direction == "LONG" else (-0.6 if direction == "SHORT" else 0.0),
        generated_at=when or dt.datetime(2026, 5, 22, 12, 0, 0, tzinfo=UTC),
        source=source,
        metadata={"last_price": last_price},
    )
    q = SignalQueue(signals_root)
    q.append([sig])
    return sig


class _StubLiveResult:
    """Minimal stand-in for `xtrade.live.runner.LiveResult`."""

    def __init__(self, *, passed: bool = True, summary: dict[str, Any] | None = None) -> None:
        self.passed = passed
        self.summary = summary or {
            "run_id": "stub",
            "instrument_id": SYMBOL,
            "first_quote_iso": "2026-05-22T12:00:00+00:00",
            "order": {
                "accepted": True,
                "canceled": True,
                "rejected": False,
                "rejection_reason": None,
            },
            "account_snapshot": [],
        }


def _make_executor(
    *, passed: bool = True
) -> tuple[Any, list[dict[str, Any]]]:
    """Build a stub `live_executor` plus the call-log it writes to."""
    calls: list[dict[str, Any]] = []

    def _exec(venues_cfg, **kwargs):  # noqa: ANN001
        calls.append({"venues_cfg": venues_cfg, **kwargs})
        return _StubLiveResult(passed=passed)

    return _exec, calls


# ---------------------------------------------------------------------------
# Composite id helper
# ---------------------------------------------------------------------------


def test_signal_composite_id_matches_intent_source_id() -> None:
    sig = Signal(
        symbol="ETHUSDT-PERP",
        venue="binance",
        direction="LONG",
        strength=0.5,
        generated_at=dt.datetime(2026, 1, 1, tzinfo=UTC),
        source="momentum:abcd1234",
    )
    assert _signal_composite_id(sig) == (
        "2026-01-01T00:00:00+00:00|ETHUSDT-PERP|momentum:abcd1234"
    )


# ---------------------------------------------------------------------------
# Lookup failures
# ---------------------------------------------------------------------------


def test_no_matching_signal_raises(tmp_path: Path) -> None:
    signals = tmp_path / "signals"
    signals.mkdir()
    executor, _ = _make_executor()

    with pytest.raises(NoMatchingSignalError):
        run_live_signal(
            venues_cfg=object(),
            strategy_name="momentum_follow",
            signals_root=signals,
            instrument_id=SYMBOL,
            approval_mode="auto",
            approvals_root=tmp_path / "approvals",
            logs_root=tmp_path / "logs",
            live_executor=executor,
        )


def test_signal_id_not_in_queue_raises(tmp_path: Path) -> None:
    signals = tmp_path / "signals"
    _seed_signal(signals)
    executor, _ = _make_executor()

    with pytest.raises(NoMatchingSignalError):
        run_live_signal(
            venues_cfg=object(),
            strategy_name="momentum_follow",
            signals_root=signals,
            instrument_id=SYMBOL,
            approval_mode="auto",
            signal_id="9999-99-99T99:99:99+00:00|XYZ|nope",
            approvals_root=tmp_path / "approvals",
            logs_root=tmp_path / "logs",
            live_executor=executor,
        )


def test_signal_id_lookup_picks_named_signal(tmp_path: Path) -> None:
    signals = tmp_path / "signals"
    a = _seed_signal(
        signals,
        direction="LONG",
        when=dt.datetime(2026, 5, 22, 12, 0, 0, tzinfo=UTC),
        source="momentum:aaaaaaaa",
    )
    b = _seed_signal(
        signals,
        direction="SHORT",
        when=dt.datetime(2026, 5, 22, 12, 5, 0, tzinfo=UTC),
        source="momentum:bbbbbbbb",
    )
    executor, calls = _make_executor()

    # Pick the older (LONG) one explicitly even though it's not newest.
    result = run_live_signal(
        venues_cfg=object(),
        strategy_name="momentum_follow",
        signals_root=signals,
        instrument_id=SYMBOL,
        approval_mode="auto",
        signal_id=_signal_composite_id(a),
        approvals_root=tmp_path / "approvals",
        logs_root=tmp_path / "logs",
        live_executor=executor,
    )
    assert result.summary["signal"]["direction"] == "LONG"
    assert result.summary["signal"]["source"] == a.source
    # Sanity: the SHORT signal exists but wasn't used.
    assert b.source != a.source
    assert len(calls) == 1
    assert calls[0]["side"] == "BUY"


# ---------------------------------------------------------------------------
# Strategy-emits-nothing
# ---------------------------------------------------------------------------


def test_strategy_emits_nothing_raises_when_no_mark(tmp_path: Path) -> None:
    """MomentumFollow returns [] when it can't read a mark — we don't
    seed `last_price`, so the synthetic account has no mark."""
    signals = tmp_path / "signals"
    # Build a signal WITHOUT last_price metadata.
    sig = Signal(
        symbol=SYMBOL,
        venue="binance",
        direction="LONG",
        strength=0.6,
        generated_at=dt.datetime(2026, 5, 22, 12, 0, 0, tzinfo=UTC),
        source="momentum:nomark01",
        metadata={},
    )
    SignalQueue(signals).append([sig])
    executor, calls = _make_executor()

    with pytest.raises(StrategyEmittedNothingError):
        run_live_signal(
            venues_cfg=object(),
            strategy_name="momentum_follow",
            signals_root=signals,
            instrument_id=SYMBOL,
            approval_mode="auto",
            approvals_root=tmp_path / "approvals",
            logs_root=tmp_path / "logs",
            live_executor=executor,
        )
    assert not calls, "executor must not be called when strategy emits nothing"


# ---------------------------------------------------------------------------
# Risk
# ---------------------------------------------------------------------------


def test_risk_rejected_blocks_submission(tmp_path: Path) -> None:
    signals = tmp_path / "signals"
    _seed_signal(signals, direction="LONG", last_price="50000")
    executor, calls = _make_executor()

    # MomentumFollow default sizing = 100 USD notional; cap at 1 USD → reject.
    with pytest.raises(RiskRejectedError):
        run_live_signal(
            venues_cfg=object(),
            strategy_name="momentum_follow",
            signals_root=signals,
            instrument_id=SYMBOL,
            approval_mode="auto",
            risk_rules=[MaxNotionalPerOrder(Decimal("1"))],
            approvals_root=tmp_path / "approvals",
            logs_root=tmp_path / "logs",
            live_executor=executor,
        )
    assert not calls, "executor must not be called when RiskGate rejects"


# ---------------------------------------------------------------------------
# Dry-run
# ---------------------------------------------------------------------------


def test_dry_run_records_but_does_not_submit(tmp_path: Path) -> None:
    signals = tmp_path / "signals"
    _seed_signal(signals, direction="LONG", last_price="50000")
    approvals = tmp_path / "approvals"
    executor, calls = _make_executor()

    result = run_live_signal(
        venues_cfg=object(),
        strategy_name="momentum_follow",
        signals_root=signals,
        instrument_id=SYMBOL,
        approval_mode="dry_run",
        approvals_root=approvals,
        logs_root=tmp_path / "logs",
        live_executor=executor,
    )

    assert not calls, "dry_run must not invoke the testnet executor"
    s = result.summary
    assert s["approval_mode"] == "dry_run"
    assert s["passed"] is False
    assert "dry_run" in s["note"]
    # The intent should still be recorded in the approvals queue.
    rows = ApprovalQueue(approvals).list()
    assert rows, "dry_run should still write an approval row"
    assert rows[0].mode == "dry_run"
    # Summary file on disk matches in-memory.
    on_disk = json.loads(result.summary_path.read_text())
    assert on_disk == s


# ---------------------------------------------------------------------------
# Auto-mode happy path
# ---------------------------------------------------------------------------


def test_auto_mode_submits_to_testnet(tmp_path: Path) -> None:
    signals = tmp_path / "signals"
    sig = _seed_signal(signals, direction="LONG", last_price="50000")
    executor, calls = _make_executor(passed=True)

    result = run_live_signal(
        venues_cfg="venues-cfg-sentinel",
        strategy_name="momentum_follow",
        signals_root=signals,
        instrument_id=SYMBOL,
        approval_mode="auto",
        approvals_root=tmp_path / "approvals",
        logs_root=tmp_path / "logs",
        live_executor=executor,
        safety_multiplier=Decimal("0.5"),
        venue_timeout_s=12.0,
    )

    # One executor call with the intent's side/quantity.
    assert len(calls) == 1
    c = calls[0]
    assert c["venues_cfg"] == "venues-cfg-sentinel"
    assert c["instrument_id"] == SYMBOL
    assert c["strategy"] == "live_order_probe"
    assert c["side"] == "BUY"
    assert c["safety_multiplier"] == Decimal("0.5")
    assert c["timeout_s"] == 12.0
    # Summary plumbing.
    s = result.summary
    assert s["strategy"] == "momentum_follow"
    assert s["approval_mode"] == "auto"
    assert s["signal"]["source"] == sig.source
    assert s["approval"]["go"] is True
    assert s["approval"]["status"] == "confirmed"
    assert s["passed"] is True
    assert s["live_summary"] is not None
    assert result.summary_path.exists()


def test_auto_mode_records_failure_when_executor_fails(tmp_path: Path) -> None:
    signals = tmp_path / "signals"
    _seed_signal(signals, direction="LONG", last_price="50000")
    executor, _ = _make_executor(passed=False)

    result = run_live_signal(
        venues_cfg=object(),
        strategy_name="momentum_follow",
        signals_root=signals,
        instrument_id=SYMBOL,
        approval_mode="auto",
        approvals_root=tmp_path / "approvals",
        logs_root=tmp_path / "logs",
        live_executor=executor,
    )
    assert result.passed is False
    assert "incomplete" in result.summary["note"]


# ---------------------------------------------------------------------------
# Manual-mode polling
# ---------------------------------------------------------------------------


def test_manual_mode_confirm_unblocks_submission(tmp_path: Path) -> None:
    signals = tmp_path / "signals"
    _seed_signal(signals, direction="LONG", last_price="50000")
    approvals = tmp_path / "approvals"
    executor, calls = _make_executor()

    # Spawn a background thread that flips the (single) pending row to
    # `confirmed` after a brief delay.
    def _confirm_after_delay() -> None:
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            q = ApprovalQueue(approvals)
            rows = q.list(status="pending")
            if rows:
                q.patch(rows[0].id, status="confirmed")
                return
            time.sleep(0.05)

    worker = threading.Thread(target=_confirm_after_delay, daemon=True)
    worker.start()

    result = run_live_signal(
        venues_cfg=object(),
        strategy_name="momentum_follow",
        signals_root=signals,
        instrument_id=SYMBOL,
        approval_mode="manual",
        approvals_root=approvals,
        logs_root=tmp_path / "logs",
        live_executor=executor,
        approval_timeout_s=10.0,
        poll_interval_s=0.05,
    )
    worker.join(timeout=2.0)

    assert len(calls) == 1, "executor should fire after confirmation"
    assert result.summary["approval"]["status"] == "confirmed"
    assert result.summary["approval"]["go"] is True
    assert result.passed is True


def test_manual_mode_reject_raises_and_blocks(tmp_path: Path) -> None:
    signals = tmp_path / "signals"
    _seed_signal(signals, direction="LONG", last_price="50000")
    approvals = tmp_path / "approvals"
    executor, calls = _make_executor()

    def _reject_after_delay() -> None:
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            q = ApprovalQueue(approvals)
            rows = q.list(status="pending")
            if rows:
                q.patch(rows[0].id, status="rejected", reason="manual veto")
                return
            time.sleep(0.05)

    worker = threading.Thread(target=_reject_after_delay, daemon=True)
    worker.start()

    with pytest.raises(ApprovalRejectedError):
        run_live_signal(
            venues_cfg=object(),
            strategy_name="momentum_follow",
            signals_root=signals,
            instrument_id=SYMBOL,
            approval_mode="manual",
            approvals_root=approvals,
            logs_root=tmp_path / "logs",
            live_executor=executor,
            approval_timeout_s=10.0,
            poll_interval_s=0.05,
        )
    worker.join(timeout=2.0)
    assert not calls, "executor must not run when operator rejects"


def test_manual_mode_timeout_raises(tmp_path: Path) -> None:
    signals = tmp_path / "signals"
    _seed_signal(signals, direction="LONG", last_price="50000")
    approvals = tmp_path / "approvals"
    executor, calls = _make_executor()

    with pytest.raises(ApprovalTimeoutError):
        run_live_signal(
            venues_cfg=object(),
            strategy_name="momentum_follow",
            signals_root=signals,
            instrument_id=SYMBOL,
            approval_mode="manual",
            approvals_root=approvals,
            logs_root=tmp_path / "logs",
            live_executor=executor,
            approval_timeout_s=0.2,
            poll_interval_s=0.05,
        )
    assert not calls


# ---------------------------------------------------------------------------
# Public error hierarchy is exported
# ---------------------------------------------------------------------------


def test_error_classes_share_root_base() -> None:
    for exc in (
        NoMatchingSignalError,
        StrategyEmittedNothingError,
        RiskRejectedError,
        ApprovalRejectedError,
        ApprovalTimeoutError,
    ):
        assert issubclass(exc, LiveSignalError)


# ---------------------------------------------------------------------------
# Regression: dry_run audit must not satisfy a later manual run
# ---------------------------------------------------------------------------
#
# Observed during the Phase 3 runbook end-to-end test: running
# `signal-run --mode dry_run` first and then `signal-run --mode manual`
# on the same signal caused the manual run to skip the approval gate and
# submit straight to the venue (`approval.go=True` with no operator
# action). Root cause was `ApprovalQueue.submit()` matching on
# `intent.fingerprint()` only and returning the pre-existing
# dry_run-confirmed row.


def test_manual_mode_does_not_latch_onto_prior_dry_run_audit(
    tmp_path: Path,
) -> None:
    signals = tmp_path / "signals"
    _seed_signal(signals, direction="LONG", last_price="50000")
    approvals = tmp_path / "approvals"

    # 1) First run: dry_run. Writes an audit row with mode=dry_run,
    # status=confirmed.
    dry_executor, dry_calls = _make_executor()
    run_live_signal(
        venues_cfg=object(),
        strategy_name="momentum_follow",
        signals_root=signals,
        instrument_id=SYMBOL,
        approval_mode="dry_run",
        approvals_root=approvals,
        logs_root=tmp_path / "logs-dry",
        live_executor=dry_executor,
    )
    assert not dry_calls
    audit_rows = ApprovalQueue(approvals).list()
    assert len(audit_rows) == 1
    assert audit_rows[0].mode == "dry_run"
    assert audit_rows[0].status == "confirmed"

    # 2) Background worker flips the *manual* pending row only — proves
    # the manual run wrote its own row (didn't just reuse the dry_run
    # confirmation).
    manual_executor, manual_calls = _make_executor()

    def _confirm_manual_pending() -> None:
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            q = ApprovalQueue(approvals)
            pending = [r for r in q.list(status="pending") if r.mode == "manual"]
            if pending:
                q.patch(pending[0].id, status="confirmed")
                return
            time.sleep(0.05)

    worker = threading.Thread(target=_confirm_manual_pending, daemon=True)
    worker.start()

    result = run_live_signal(
        venues_cfg=object(),
        strategy_name="momentum_follow",
        signals_root=signals,
        instrument_id=SYMBOL,
        approval_mode="manual",
        approvals_root=approvals,
        logs_root=tmp_path / "logs-manual",
        live_executor=manual_executor,
        approval_timeout_s=10.0,
        poll_interval_s=0.05,
    )
    worker.join(timeout=2.0)

    assert len(manual_calls) == 1, "executor should fire after manual confirm"
    assert result.summary["approval"]["mode"] == "manual"
    assert result.summary["approval"]["status"] == "confirmed"
    assert result.summary["approval"]["go"] is True
    assert result.passed is True

    # Queue now contains both: the dry_run audit (untouched) AND the
    # manual decision.
    rows = ApprovalQueue(approvals).list()
    modes = sorted(r.mode for r in rows)
    assert modes == ["dry_run", "manual"]
    statuses_by_mode = {r.mode: r.status for r in rows}
    assert statuses_by_mode == {"dry_run": "confirmed", "manual": "confirmed"}
