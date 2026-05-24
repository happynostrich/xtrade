"""Tests for `ApprovalQueue.annotate_dispatch_{success,failure}` (Phase 4 Task 2)."""

from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal
from pathlib import Path

import pytest

from xtrade.approval.queue import ApprovalQueue, ApprovalQueueError
from xtrade.strategy.intent import OrderIntent


UTC = dt.timezone.utc


def _intent(qty: str = "0.01", source: str = "sig-1") -> OrderIntent:
    return OrderIntent(
        venue="binance",
        symbol="BTCUSDT-PERP.BINANCE",
        side="BUY",
        order_type="MARKET",
        quantity=Decimal(qty),
        limit_price=None,
        reduce_only=False,
        time_in_force="IOC",
        source_signal_id=source,
        created_at=dt.datetime(2026, 5, 22, 10, 0, 0, tzinfo=UTC),
    )


def test_annotate_success_attaches_dispatch_field(tmp_path: Path) -> None:
    q = ApprovalQueue(tmp_path)
    intent = _intent()
    rec = q.submit(intent, mode="manual")
    updated = q.annotate_dispatch_success(
        rec.id, result={"status_code": 200, "attempts": 1}
    )
    assert updated.dispatch is not None
    assert updated.dispatch["ok"] is True
    assert updated.dispatch["status_code"] == 200
    # status untouched
    assert updated.status == "pending"


def test_annotate_failure_attaches_and_persists(tmp_path: Path) -> None:
    q = ApprovalQueue(tmp_path)
    intent = _intent()
    rec = q.submit(intent, mode="manual")
    q.annotate_dispatch_failure(
        rec.id, result={"error": "ConnectError: ECONNREFUSED", "attempts": 4}
    )

    # Re-read from disk in a fresh queue handle.
    q2 = ApprovalQueue(tmp_path)
    reread = q2.get(rec.id)
    assert reread is not None
    assert reread.dispatch is not None
    assert reread.dispatch["ok"] is False
    assert reread.dispatch["attempts"] == 4
    assert "ECONNREFUSED" in reread.dispatch["error"]
    assert reread.status == "pending"


def test_annotate_missing_id_raises(tmp_path: Path) -> None:
    q = ApprovalQueue(tmp_path)
    with pytest.raises(ApprovalQueueError, match="not found"):
        q.annotate_dispatch_success("deadbeef", result={"status_code": 200})


def test_annotate_prefers_pending_row_when_dry_run_audit_coexists(
    tmp_path: Path,
) -> None:
    """A dry_run row and a manual pending row may share the same fingerprint.
    Annotation should target the row a human is waiting on (the pending one).
    """
    q = ApprovalQueue(tmp_path)
    intent = _intent()
    audit = q.submit(intent, mode="dry_run", status="confirmed",
                     decided_at=dt.datetime(2026, 5, 22, 10, 1, tzinfo=UTC))
    pending = q.submit(intent, mode="manual")
    assert audit.id == pending.id  # same fingerprint

    q.annotate_dispatch_success(pending.id, result={"status_code": 200})
    rows = [r for r in q if r.id == pending.id]
    annotated = [r for r in rows if r.dispatch is not None]
    assert len(annotated) == 1
    assert annotated[0].mode == "manual"
    assert annotated[0].status == "pending"


def test_dispatch_field_round_trips_through_jsonl(tmp_path: Path) -> None:
    q = ApprovalQueue(tmp_path)
    intent = _intent()
    rec = q.submit(intent, mode="manual")
    q.annotate_dispatch_failure(rec.id, result={"error": "http-503", "attempts": 4})

    shards = list(tmp_path.glob("*.jsonl"))
    assert len(shards) == 1
    raw = shards[0].read_text().splitlines()
    decoded = json.loads(raw[0])
    assert decoded["dispatch"]["ok"] is False
    assert decoded["dispatch"]["error"] == "http-503"
