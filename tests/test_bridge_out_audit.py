"""Tests for `xtrade.bridge.audit.BridgeAuditWriter` (Phase 5 / Track A2).

What this proves
----------------
* The audit writer creates the daily UTC shard at
  `audit_root/bridge_out.<YYYY-MM-DD>.jsonl` with mode 0640.
* All 5 dispatch paths produce the expected audit row(s):
    1. HTTP 200 success  → one `kind="ok"` row
    2. HTTP 5xx → 200    → one `kind="retry"` row then one `kind="ok"`
    3. HTTP 5xx exhausted → N-1 `kind="retry"` rows + one `kind="fail"`
    4. HTTP 4xx          → one `kind="fail"` row (no retries)
    5. Network exception → at least one `kind="retry"` + one `kind="fail"`
    6. SecretLeakError   → one `kind="refused"` row, attempt=0
* `error` field on the `refused` path NEVER includes the raw secret
  (it's redacted to `"secret-scrub: <ExceptionClass>"`).
* Concurrent appends (100 threads × 1 row) do not tear lines and the
  final line count is exactly 100.
* Audit fields are stable and JSON-parseable.

Notes
-----
We reuse the `_bridge()` MockTransport pattern from
`test_bridge_openclaw_webhook.py` rather than importing the helper —
keeping a small focused duplicate keeps the test self-contained when
the upstream helper changes.
"""

from __future__ import annotations

import concurrent.futures
import datetime as dt
import json
from decimal import Decimal
from pathlib import Path
from typing import Callable

import httpx
import pytest

from xtrade.approval.queue import ApprovalRecord
from xtrade.bridge.audit import BridgeAuditWriter
from xtrade.bridge.openclaw_webhook import OpenclawBridge
from xtrade.strategy.intent import OrderIntent


UTC = dt.timezone.utc
NOW = dt.datetime(2026, 5, 24, 12, 0, 0, tzinfo=UTC)


# ---- helpers --------------------------------------------------------------


def _intent(source: str = "manual:audit-test") -> OrderIntent:
    return OrderIntent(
        venue="binance",
        symbol="BTCUSDT-PERP.BINANCE",
        side="BUY",
        order_type="MARKET",
        quantity=Decimal("0.001"),
        limit_price=None,
        reduce_only=False,
        time_in_force="IOC",
        source_signal_id=source,
        created_at=NOW,
    )


def _record(source: str = "manual:audit-test") -> ApprovalRecord:
    it = _intent(source)
    return ApprovalRecord(
        id=it.fingerprint(),
        intent=it,
        status="pending",
        created_at=NOW,
        decided_at=None,
        reason="",
        mode="manual",
    )


def _bridge_with_audit(
    *,
    handler: Callable[[httpx.Request], httpx.Response],
    audit_root: Path,
    backoffs: tuple[float, ...] = (0.0, 0.0, 0.0, 0.0),
) -> OpenclawBridge:
    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport)
    return OpenclawBridge(
        gateway_url="https://openclaw.test",
        shared_secret="topsecret",
        callback_base_url="http://127.0.0.1:18080",
        client=client,
        backoffs=backoffs,
        sleep=lambda _s: None,
        now=lambda: NOW,
        audit_writer=BridgeAuditWriter(audit_root),
    )


def _read_shard(audit_root: Path) -> list[dict]:
    shard = audit_root / "bridge_out.2026-05-24.jsonl"
    assert shard.exists(), f"expected shard at {shard}, got {list(audit_root.iterdir())}"
    return [json.loads(line) for line in shard.read_text().splitlines() if line.strip()]


# ---- 5 dispatch paths -----------------------------------------------------


def test_dispatch_200_writes_single_ok_row(tmp_path: Path) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"flow_id": "TF-001"})

    bridge = _bridge_with_audit(handler=handler, audit_root=tmp_path)
    bridge.dispatch(_record("manual:ok"))

    rows = _read_shard(tmp_path)
    assert len(rows) == 1, rows
    row = rows[0]
    assert row["kind"] == "ok"
    assert row["status_code"] == 200
    assert row["attempt"] == 1
    assert row["error"] is None
    assert "dispatched_at" in row
    assert isinstance(row["elapsed_s"], float)


def test_dispatch_5xx_then_200_writes_retry_plus_ok(tmp_path: Path) -> None:
    state = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        state["n"] += 1
        if state["n"] < 3:
            return httpx.Response(503, text="busy")
        return httpx.Response(200, json={"flow_id": "TF-002"})

    bridge = _bridge_with_audit(handler=handler, audit_root=tmp_path)
    bridge.dispatch(_record("manual:retry-recover"))

    rows = _read_shard(tmp_path)
    kinds = [r["kind"] for r in rows]
    # Two retry rows (attempts 1 and 2 failed → retry audit), then ok.
    assert kinds == ["retry", "retry", "ok"], rows
    assert rows[0]["status_code"] == 503
    assert rows[1]["status_code"] == 503
    assert rows[2]["status_code"] == 200
    assert rows[2]["attempt"] == 3
    # All rows share the same approval_id.
    assert len({r["approval_id"] for r in rows}) == 1


def test_dispatch_5xx_exhausted_writes_retry_rows_plus_fail(tmp_path: Path) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="bad gateway")

    bridge = _bridge_with_audit(handler=handler, audit_root=tmp_path)
    bridge.dispatch(_record("manual:fail-5xx"))

    rows = _read_shard(tmp_path)
    kinds = [r["kind"] for r in rows]
    # 4 attempts → 3 retry rows (between attempts) + 1 terminal fail.
    assert kinds == ["retry", "retry", "retry", "fail"], rows
    assert rows[-1]["status_code"] == 502
    assert "http-502" in rows[-1]["error"]
    assert rows[-1]["attempt"] == 4


def test_dispatch_4xx_writes_single_fail_no_retry(tmp_path: Path) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="unauthorized")

    bridge = _bridge_with_audit(handler=handler, audit_root=tmp_path)
    bridge.dispatch(_record("manual:fail-4xx"))

    rows = _read_shard(tmp_path)
    # 4xx breaks immediately — no retry rows, one fail row.
    assert len(rows) == 1, rows
    assert rows[0]["kind"] == "fail"
    assert rows[0]["status_code"] == 401
    assert rows[0]["attempt"] == 1
    assert "http-401" in rows[0]["error"]


def test_dispatch_network_exception_writes_retry_plus_fail(tmp_path: Path) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("ECONNREFUSED", request=req)

    bridge = _bridge_with_audit(handler=handler, audit_root=tmp_path)
    bridge.dispatch(_record("manual:net-exc"))

    rows = _read_shard(tmp_path)
    kinds = [r["kind"] for r in rows]
    assert kinds == ["retry", "retry", "retry", "fail"], rows
    for r in rows:
        # status_code is None when the request never landed.
        assert r["status_code"] is None
        assert "ConnectError" in (r["error"] or "")


def test_dispatch_secret_leak_writes_refused_with_redacted_error(
    tmp_path: Path,
) -> None:
    # The secret value: an AWS-key-shaped string the schema blocks.
    leak = "AKIA" + "A" * 16
    it = _intent("manual:leak")
    # We piggyback the secret onto the intent's source_signal_id; the
    # schema scrubber walks the whole payload tree.
    leaky_intent = OrderIntent(
        venue=it.venue,
        symbol=it.symbol,
        side=it.side,
        order_type=it.order_type,
        quantity=it.quantity,
        limit_price=it.limit_price,
        reduce_only=it.reduce_only,
        time_in_force=it.time_in_force,
        source_signal_id=f"sig|{leak}",
        created_at=it.created_at,
    )
    record = ApprovalRecord(
        id=leaky_intent.fingerprint(),
        intent=leaky_intent,
        status="pending",
        created_at=NOW,
        decided_at=None,
        reason="",
        mode="manual",
    )

    # Handler should NOT be called; if it is, force a test failure.
    called = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        called["n"] += 1
        return httpx.Response(200)

    bridge = _bridge_with_audit(handler=handler, audit_root=tmp_path)
    bridge.dispatch(record)

    assert called["n"] == 0, "secret-scrub must short-circuit before HTTP"
    rows = _read_shard(tmp_path)
    assert len(rows) == 1, rows
    assert rows[0]["kind"] == "refused"
    assert rows[0]["attempt"] == 0
    assert rows[0]["status_code"] is None
    # Error must be redacted: no raw secret, class name only.
    err = rows[0]["error"] or ""
    assert "secret-scrub:" in err
    assert "SecretLeakError" in err
    assert leak not in err, f"raw secret leaked into audit: {err!r}"


# ---- atomicity ------------------------------------------------------------


def test_audit_writer_creates_audit_root_and_file_mode(tmp_path: Path) -> None:
    audit_root = tmp_path / "audit-new"
    writer = BridgeAuditWriter(audit_root)
    writer.write(
        approval_id="AP-1",
        attempt=1,
        kind="ok",
        status_code=200,
        error=None,
        dispatched_at=NOW,
        elapsed_s=0.1,
        response_excerpt=None,
    )
    assert audit_root.is_dir()
    shard = audit_root / "bridge_out.2026-05-24.jsonl"
    assert shard.exists()
    mode = shard.stat().st_mode & 0o777
    # We requested 0o640 but umask may strip group bits; assert at least
    # the owner-readable subset and that "world" is not granted.
    assert mode & 0o400, f"owner read missing: {oct(mode)}"
    assert not (mode & 0o007), f"world bits set: {oct(mode)}"


def test_audit_writer_concurrent_appends_no_tearing(tmp_path: Path) -> None:
    """100 threads × 1 row → file has exactly 100 well-formed JSON lines."""
    writer = BridgeAuditWriter(tmp_path)

    def _one(i: int) -> None:
        writer.write(
            approval_id=f"AP-{i:04d}",
            attempt=1,
            kind="ok",
            status_code=200,
            error=None,
            dispatched_at=NOW,
            elapsed_s=0.001,
            response_excerpt=None,
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=32) as ex:
        list(ex.map(_one, range(100)))

    shard = tmp_path / "bridge_out.2026-05-24.jsonl"
    lines = shard.read_text().splitlines()
    assert len(lines) == 100, len(lines)
    seen_ids: set[str] = set()
    for ln in lines:
        row = json.loads(ln)  # would raise on torn line
        assert row["kind"] == "ok"
        seen_ids.add(row["approval_id"])
    assert len(seen_ids) == 100, "duplicate or lost approval_ids"


def test_audit_writer_rolls_per_utc_day(tmp_path: Path) -> None:
    writer = BridgeAuditWriter(tmp_path)
    when_a = dt.datetime(2026, 5, 24, 23, 59, tzinfo=UTC)
    when_b = dt.datetime(2026, 5, 25, 0, 1, tzinfo=UTC)
    writer.write(
        approval_id="AP-A",
        attempt=1,
        kind="ok",
        status_code=200,
        error=None,
        dispatched_at=when_a,
        elapsed_s=0.0,
        response_excerpt=None,
    )
    writer.write(
        approval_id="AP-B",
        attempt=1,
        kind="ok",
        status_code=200,
        error=None,
        dispatched_at=when_b,
        elapsed_s=0.0,
        response_excerpt=None,
    )
    assert (tmp_path / "bridge_out.2026-05-24.jsonl").exists()
    assert (tmp_path / "bridge_out.2026-05-25.jsonl").exists()


def test_audit_writer_rejects_naive_datetime(tmp_path: Path) -> None:
    writer = BridgeAuditWriter(tmp_path)
    with pytest.raises(ValueError, match="timezone-aware"):
        writer.write(
            approval_id="AP",
            attempt=1,
            kind="ok",
            status_code=200,
            error=None,
            dispatched_at=dt.datetime(2026, 5, 24, 12, 0, 0),  # naive
            elapsed_s=0.0,
            response_excerpt=None,
        )


def test_audit_writer_rejects_bad_kind(tmp_path: Path) -> None:
    writer = BridgeAuditWriter(tmp_path)
    with pytest.raises(ValueError, match="invalid kind"):
        writer.write(
            approval_id="AP",
            attempt=1,
            kind="bogus",  # type: ignore[arg-type]
            status_code=200,
            error=None,
            dispatched_at=NOW,
            elapsed_s=0.0,
            response_excerpt=None,
        )


# ---- audit writer optionality --------------------------------------------


def test_bridge_without_audit_writer_does_not_write(tmp_path: Path) -> None:
    """Sanity: omitting audit_writer keeps Phase 4 behaviour (no jsonl)."""
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"flow_id": "TF-x"})

    transport = httpx.MockTransport(handler)
    bridge = OpenclawBridge(
        gateway_url="https://openclaw.test",
        shared_secret="topsecret",
        callback_base_url="http://127.0.0.1:18080",
        client=httpx.Client(transport=transport),
        backoffs=(0.0,),
        sleep=lambda _s: None,
        now=lambda: NOW,
        # No audit_writer — must work transparently.
    )
    bridge.dispatch(_record("manual:no-audit"))
    # tmp_path must contain no jsonl at all.
    assert list(tmp_path.glob("*.jsonl")) == []


def test_audit_write_failure_does_not_break_dispatch(tmp_path: Path) -> None:
    """A blown audit fd must not propagate out of dispatch."""

    class _BrokenWriter:
        def write(self, **_kw):  # noqa: ANN003
            raise OSError("disk-full simulation")

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"flow_id": "TF-y"})

    transport = httpx.MockTransport(handler)
    bridge = OpenclawBridge(
        gateway_url="https://openclaw.test",
        shared_secret="topsecret",
        callback_base_url="http://127.0.0.1:18080",
        client=httpx.Client(transport=transport),
        backoffs=(0.0,),
        sleep=lambda _s: None,
        now=lambda: NOW,
    )
    bridge._audit_writer = _BrokenWriter()  # type: ignore[assignment]

    # Must not raise.
    result = bridge.dispatch(_record("manual:broken-audit"))
    assert result.ok is True
