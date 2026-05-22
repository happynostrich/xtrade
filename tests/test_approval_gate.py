"""Tests for `xtrade.approval` (Phase 3 Task 3 / T4)."""

from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal
from pathlib import Path

import pytest

from xtrade.approval import (
    ApprovalGate,
    ApprovalQueue,
    ApprovalQueueError,
    ApprovalRecord,
)
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


def _t(h: int = 10, m: int = 0) -> dt.datetime:
    return dt.datetime(2026, 5, 22, h, m, 0, tzinfo=UTC)


# ---- ApprovalQueue: write / read / dedup --------------------------------


def test_submit_appends_and_returns_record(tmp_path: Path) -> None:
    q = ApprovalQueue(tmp_path)
    rec = q.submit(_intent(), mode="manual", now=_t())
    assert rec.status == "pending"
    assert rec.id == _intent().fingerprint()
    files = sorted(tmp_path.glob("*.jsonl"))
    assert len(files) == 1
    assert files[0].name == "2026-05-22.jsonl"


def test_submit_is_idempotent_on_fingerprint(tmp_path: Path) -> None:
    q = ApprovalQueue(tmp_path)
    a = q.submit(_intent(), mode="manual", now=_t())
    b = q.submit(_intent(), mode="manual", now=_t(h=11))
    assert a == b
    rows = list(q)
    assert len(rows) == 1


def test_get_returns_existing_record(tmp_path: Path) -> None:
    q = ApprovalQueue(tmp_path)
    rec = q.submit(_intent(), mode="manual", now=_t())
    assert q.get(rec.id) == rec
    assert q.get("nonexistent") is None


def test_list_filters_by_status_and_since(tmp_path: Path) -> None:
    q = ApprovalQueue(tmp_path)
    q.submit(_intent(qty="0.01"), mode="manual", now=_t(h=9))
    q.submit(_intent(qty="0.02"), mode="manual", now=_t(h=10))
    q.submit(_intent(qty="0.03"), mode="manual", now=_t(h=11))
    pending = q.list(status="pending")
    assert len(pending) == 3
    later = q.list(status="pending", since=_t(h=10))
    assert len(later) == 2


def test_list_since_requires_tz_aware(tmp_path: Path) -> None:
    q = ApprovalQueue(tmp_path)
    with pytest.raises(ApprovalQueueError):
        q.list(since=dt.datetime(2026, 5, 22, 10, 0, 0))  # naive


# ---- ApprovalQueue: patch flow ------------------------------------------


def test_patch_pending_to_confirmed(tmp_path: Path) -> None:
    q = ApprovalQueue(tmp_path)
    rec = q.submit(_intent(), mode="manual", now=_t(h=10))
    updated = q.patch(rec.id, status="confirmed", now=_t(h=11))
    assert updated.status == "confirmed"
    assert updated.decided_at == _t(h=11)
    # Persisted to disk
    fresh = ApprovalQueue(tmp_path)
    assert fresh.get(rec.id).status == "confirmed"  # type: ignore[union-attr]


def test_patch_pending_to_rejected_with_reason(tmp_path: Path) -> None:
    q = ApprovalQueue(tmp_path)
    rec = q.submit(_intent(), mode="manual", now=_t(h=10))
    updated = q.patch(
        rec.id, status="rejected", reason="too big", now=_t(h=11)
    )
    assert updated.status == "rejected"
    assert updated.reason == "too big"


def test_patch_rejects_double_decision(tmp_path: Path) -> None:
    q = ApprovalQueue(tmp_path)
    rec = q.submit(_intent(), mode="manual", now=_t())
    q.patch(rec.id, status="confirmed", now=_t(h=11))
    with pytest.raises(ApprovalQueueError):
        q.patch(rec.id, status="rejected", now=_t(h=12))


def test_patch_rejects_unknown_id(tmp_path: Path) -> None:
    q = ApprovalQueue(tmp_path)
    with pytest.raises(ApprovalQueueError):
        q.patch("deadbeef00000000", status="confirmed")


def test_patch_rejects_back_to_pending(tmp_path: Path) -> None:
    q = ApprovalQueue(tmp_path)
    rec = q.submit(_intent(), mode="manual", now=_t())
    with pytest.raises(ApprovalQueueError):
        q.patch(rec.id, status="pending")  # type: ignore[arg-type]


# ---- ApprovalQueue: persistence / corruption ----------------------------


def test_atomic_write_creates_no_leftover_tmp(tmp_path: Path) -> None:
    q = ApprovalQueue(tmp_path)
    q.submit(_intent(), mode="manual", now=_t())
    leftovers = list(tmp_path.glob("*.tmp"))
    assert not leftovers


def test_corrupted_line_is_skipped_with_warning(tmp_path: Path) -> None:
    shard = tmp_path / "2026-05-22.jsonl"
    rec = ApprovalRecord(
        id="abc1234567890def",
        intent=_intent(),
        status="pending",
        created_at=_t(),
        decided_at=None,
        reason="",
        mode="manual",
    )
    shard.write_text(
        json.dumps(rec.to_dict()) + "\n" + "this is not json\n",
        encoding="utf-8",
    )
    q = ApprovalQueue(tmp_path)
    with pytest.warns(RuntimeWarning, match="corrupt approval"):
        rows = list(q)
    assert len(rows) == 1
    assert rows[0].id == "abc1234567890def"


def test_cross_day_sharding(tmp_path: Path) -> None:
    q = ApprovalQueue(tmp_path)
    q.submit(_intent(qty="0.01"), mode="manual", now=dt.datetime(2026, 5, 22, 23, 59, tzinfo=UTC))
    q.submit(_intent(qty="0.02"), mode="manual", now=dt.datetime(2026, 5, 23, 0, 1, tzinfo=UTC))
    files = sorted(p.name for p in tmp_path.glob("*.jsonl"))
    assert files == ["2026-05-22.jsonl", "2026-05-23.jsonl"]


def test_record_dict_roundtrip() -> None:
    rec = ApprovalRecord(
        id="abc1234567890def",
        intent=_intent(),
        status="pending",
        created_at=_t(),
        decided_at=None,
        reason="",
        mode="manual",
    )
    restored = ApprovalRecord.from_dict(rec.to_dict())
    assert restored == rec


def test_record_from_dict_rejects_unknown_status() -> None:
    rec = ApprovalRecord(
        id="abc1234567890def",
        intent=_intent(),
        status="pending",
        created_at=_t(),
        decided_at=None,
        reason="",
        mode="manual",
    )
    payload = rec.to_dict()
    payload["status"] = "weird"
    with pytest.raises(ApprovalQueueError):
        ApprovalRecord.from_dict(payload)


# ---- ApprovalGate: three modes ------------------------------------------


def test_gate_auto_goes_through_and_records_confirmed(tmp_path: Path) -> None:
    gate = ApprovalGate("auto", tmp_path)
    decision = gate.decide(_intent(), now=_t())
    assert decision.go
    assert not decision.awaiting
    assert decision.status == "confirmed"
    assert decision.mode == "auto"
    # Audit row is in the queue
    rec = gate.queue.get(decision.record_id)
    assert rec is not None
    assert rec.mode == "auto"
    assert rec.status == "confirmed"


def test_gate_dry_run_records_but_blocks(tmp_path: Path) -> None:
    gate = ApprovalGate("dry_run", tmp_path)
    decision = gate.decide(_intent(), now=_t())
    assert not decision.go
    assert not decision.awaiting
    assert decision.status == "confirmed"
    assert decision.mode == "dry_run"
    rec = gate.queue.get(decision.record_id)
    assert rec is not None
    assert rec.mode == "dry_run"


def test_gate_manual_writes_pending_and_awaits(tmp_path: Path) -> None:
    gate = ApprovalGate("manual", tmp_path)
    decision = gate.decide(_intent(), now=_t())
    assert not decision.go
    assert decision.awaiting
    assert decision.status == "pending"
    pending = gate.pending()
    assert len(pending) == 1
    assert pending[0].id == decision.record_id


def test_gate_manual_after_confirm_goes_through(tmp_path: Path) -> None:
    gate = ApprovalGate("manual", tmp_path)
    first = gate.decide(_intent(), now=_t(h=10))
    assert first.awaiting
    gate.queue.patch(first.record_id, status="confirmed", now=_t(h=11))
    # Re-decide on the same intent — fingerprint matches, record is now
    # confirmed → gate clears.
    again = gate.decide(_intent(), now=_t(h=12))
    assert again.go
    assert not again.awaiting
    assert again.status == "confirmed"


def test_gate_rejects_unknown_mode(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        ApprovalGate("yolo", tmp_path)  # type: ignore[arg-type]


def test_gate_decide_is_idempotent(tmp_path: Path) -> None:
    gate = ApprovalGate("manual", tmp_path)
    a = gate.decide(_intent(), now=_t(h=10))
    b = gate.decide(_intent(), now=_t(h=11))
    assert a.record_id == b.record_id
    assert len(list(gate.queue)) == 1


def test_gate_pending_lists_only_pending(tmp_path: Path) -> None:
    gate = ApprovalGate("manual", tmp_path)
    d1 = gate.decide(_intent(qty="0.01"), now=_t(h=10))
    d2 = gate.decide(_intent(qty="0.02"), now=_t(h=10, m=1))
    gate.queue.patch(d1.record_id, status="confirmed", now=_t(h=11))
    pending = gate.pending()
    assert {r.id for r in pending} == {d2.record_id}
