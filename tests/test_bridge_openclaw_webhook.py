"""Tests for `xtrade.bridge.openclaw_webhook.OpenclawBridge` (Phase 4 Task 2)."""

from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Iterator
from unittest.mock import MagicMock

import httpx
import pytest

from xtrade.approval.queue import ApprovalQueue, ApprovalRecord
from xtrade.bridge.openclaw_webhook import (
    BridgeConfigError,
    DispatchResult,
    OpenclawBridge,
)
from xtrade.strategy.intent import OrderIntent


UTC = dt.timezone.utc


# ---- fixtures ------------------------------------------------------------


def _intent(qty: str = "0.002") -> OrderIntent:
    return OrderIntent(
        venue="binance",
        symbol="BTCUSDT-PERP.BINANCE",
        side="BUY",
        order_type="MARKET",
        quantity=Decimal(qty),
        limit_price=None,
        reduce_only=False,
        time_in_force="IOC",
        source_signal_id="manual:test",
        created_at=dt.datetime(2026, 5, 23, 1, 38, 52, tzinfo=UTC),
    )


def _record(intent: OrderIntent | None = None) -> ApprovalRecord:
    intent = intent or _intent()
    return ApprovalRecord(
        id=intent.fingerprint(),
        intent=intent,
        status="pending",
        created_at=dt.datetime(2026, 5, 23, 1, 39, tzinfo=UTC),
        decided_at=None,
        reason="",
        mode="manual",
    )


def _bridge(
    *,
    handler: Callable[[httpx.Request], httpx.Response],
    approvals: ApprovalQueue | None = None,
    sleeps: list[float] | None = None,
    backoffs: tuple[float, ...] = (0.01, 0.01, 0.01, 0.01),
) -> OpenclawBridge:
    """Build a bridge wired to an httpx MockTransport."""
    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport)
    fake_sleep = (sleeps.append if sleeps is not None else (lambda _x: None))
    return OpenclawBridge(
        gateway_url="https://openclaw.test",
        shared_secret="topsecret",
        callback_base_url="http://127.0.0.1:18080",
        approvals_queue=approvals,
        client=client,
        backoffs=backoffs,
        sleep=fake_sleep,
        now=lambda: dt.datetime(2026, 5, 23, 3, 10, tzinfo=UTC),
    )


# ---- config validation ---------------------------------------------------


def test_constructor_rejects_bad_gateway() -> None:
    with pytest.raises(BridgeConfigError):
        OpenclawBridge(
            gateway_url="openclaw.test",  # missing scheme
            shared_secret="x",
            callback_base_url="http://127.0.0.1:18080",
        )


def test_constructor_rejects_empty_secret() -> None:
    with pytest.raises(BridgeConfigError):
        OpenclawBridge(
            gateway_url="https://openclaw.test",
            shared_secret="",
            callback_base_url="http://127.0.0.1:18080",
        )


def test_constructor_rejects_bad_callback() -> None:
    with pytest.raises(BridgeConfigError):
        OpenclawBridge(
            gateway_url="https://openclaw.test",
            shared_secret="x",
            callback_base_url="127.0.0.1:18080",  # missing scheme
        )


def test_constructor_rejects_empty_backoffs() -> None:
    with pytest.raises(BridgeConfigError):
        OpenclawBridge(
            gateway_url="https://openclaw.test",
            shared_secret="x",
            callback_base_url="http://127.0.0.1:18080",
            backoffs=(),
        )


def test_from_env_requires_keys() -> None:
    with pytest.raises(BridgeConfigError, match="OPENCLAW_GATEWAY"):
        OpenclawBridge.from_env({"OPENCLAW_SHARED_SECRET": "x"})
    with pytest.raises(BridgeConfigError, match="OPENCLAW_SHARED_SECRET"):
        OpenclawBridge.from_env({"OPENCLAW_GATEWAY": "https://gw"})


def test_from_env_defaults_callback_to_localhost() -> None:
    # Pass an injected client so the constructor does not build the default
    # httpx.Client (whose proxy detection may import optional deps absent
    # from minimal test envs).
    transport = httpx.MockTransport(lambda req: httpx.Response(200))
    client = httpx.Client(transport=transport)
    bridge = OpenclawBridge.from_env(
        {
            "OPENCLAW_GATEWAY": "https://gw.example",
            "OPENCLAW_SHARED_SECRET": "s",
        },
        client=client,
    )
    try:
        assert bridge._callback_base == "http://127.0.0.1:18080"
    finally:
        bridge.close()
        client.close()


# ---- happy path ----------------------------------------------------------


def test_dispatch_200_returns_ok_and_annotates_queue(tmp_path: Path) -> None:
    captured: dict[str, Any] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["url"] = str(req.url)
        captured["auth"] = req.headers.get("Authorization")
        captured["body"] = json.loads(req.content.decode())
        return httpx.Response(200, json={"flow_id": "TF-001"})

    queue = ApprovalQueue(tmp_path)
    intent = _intent()
    queue.submit(intent, mode="manual")
    rec = queue.get(intent.fingerprint())
    assert rec is not None

    sleeps: list[float] = []
    bridge = _bridge(handler=handler, approvals=queue, sleeps=sleeps)
    try:
        result = bridge.dispatch(rec, risk_summary={"rules_passed": ["x"]})
    finally:
        bridge.close()

    assert result.ok is True
    assert result.status_code == 200
    assert result.attempts == 1
    assert result.error is None
    assert sleeps == []
    assert "flow_id" in (result.response_excerpt or "")

    # wire contract
    assert captured["url"] == "https://openclaw.test/plugins/webhooks/xtrade"
    assert captured["auth"] == "Bearer topsecret"
    assert captured["body"]["action"] == "create_flow"
    assert captured["body"]["metadata"]["approval_id"] == rec.id

    # queue annotation
    annotated = queue.get(rec.id)
    assert annotated is not None
    assert annotated.dispatch is not None
    assert annotated.dispatch["ok"] is True
    assert annotated.dispatch["status_code"] == 200
    assert annotated.status == "pending"  # status untouched


# ---- 5xx retry then success ---------------------------------------------


def test_dispatch_retries_5xx_then_succeeds(tmp_path: Path) -> None:
    calls: list[int] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(len(calls))
        if len(calls) < 3:
            return httpx.Response(503, text="busy")
        return httpx.Response(200, json={"ok": True})

    queue = ApprovalQueue(tmp_path)
    intent = _intent()
    queue.submit(intent, mode="manual")
    rec = queue.get(intent.fingerprint())
    assert rec is not None

    sleeps: list[float] = []
    bridge = _bridge(handler=handler, approvals=queue, sleeps=sleeps)
    try:
        result = bridge.dispatch(rec)
    finally:
        bridge.close()

    assert result.ok is True
    assert result.attempts == 3
    assert len(calls) == 3
    # exponential backoff invoked between attempts 1-2 and 2-3 (not after 3)
    assert sleeps == [0.01, 0.01]


def test_dispatch_exhausts_5xx_writes_failure_annotation(tmp_path: Path) -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="bad gateway")

    queue = ApprovalQueue(tmp_path)
    intent = _intent()
    queue.submit(intent, mode="manual")
    rec = queue.get(intent.fingerprint())
    assert rec is not None

    sleeps: list[float] = []
    bridge = _bridge(handler=handler, approvals=queue, sleeps=sleeps)
    try:
        result = bridge.dispatch(rec)
    finally:
        bridge.close()

    assert result.ok is False
    assert result.attempts == 4
    assert result.status_code == 502
    assert "http-502" in (result.error or "")
    # 3 sleeps between 4 attempts
    assert sleeps == [0.01, 0.01, 0.01]

    annotated = queue.get(rec.id)
    assert annotated is not None
    assert annotated.dispatch is not None
    assert annotated.dispatch["ok"] is False
    assert annotated.dispatch["attempts"] == 4
    assert annotated.status == "pending"  # fail-default-deny


# ---- 4xx: no retry, immediate fail --------------------------------------


def test_dispatch_4xx_does_not_retry(tmp_path: Path) -> None:
    calls = {"n": 0}

    def handler(_req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(401, text="unauthorized")

    queue = ApprovalQueue(tmp_path)
    intent = _intent()
    queue.submit(intent, mode="manual")
    rec = queue.get(intent.fingerprint())
    assert rec is not None

    sleeps: list[float] = []
    bridge = _bridge(handler=handler, approvals=queue, sleeps=sleeps)
    try:
        result = bridge.dispatch(rec)
    finally:
        bridge.close()

    assert calls["n"] == 1
    assert result.ok is False
    assert result.attempts == 1
    assert result.status_code == 401
    assert "client error" in (result.error or "")
    assert sleeps == []


# ---- network errors -----------------------------------------------------


def test_dispatch_retries_on_timeout(tmp_path: Path) -> None:
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectTimeout("timed out", request=req)
        return httpx.Response(200, json={"ok": True})

    queue = ApprovalQueue(tmp_path)
    intent = _intent()
    queue.submit(intent, mode="manual")
    rec = queue.get(intent.fingerprint())
    assert rec is not None

    sleeps: list[float] = []
    bridge = _bridge(handler=handler, approvals=queue, sleeps=sleeps)
    try:
        result = bridge.dispatch(rec)
    finally:
        bridge.close()

    assert result.ok is True
    assert result.attempts == 2
    assert sleeps == [0.01]


def test_dispatch_records_terminal_network_failure(tmp_path: Path) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("ECONNREFUSED", request=req)

    queue = ApprovalQueue(tmp_path)
    intent = _intent()
    queue.submit(intent, mode="manual")
    rec = queue.get(intent.fingerprint())
    assert rec is not None

    sleeps: list[float] = []
    bridge = _bridge(handler=handler, approvals=queue, sleeps=sleeps)
    try:
        result = bridge.dispatch(rec)
    finally:
        bridge.close()

    assert result.ok is False
    assert result.attempts == 4
    assert result.status_code is None
    assert "ConnectError" in (result.error or "")
    assert sleeps == [0.01, 0.01, 0.01]

    annotated = queue.get(rec.id)
    assert annotated is not None
    assert annotated.dispatch is not None
    assert annotated.dispatch["ok"] is False
    assert annotated.dispatch["error"].startswith("ConnectError")


# ---- secret scrub: terminal, never sends --------------------------------


def test_dispatch_refuses_to_send_credential_payload(tmp_path: Path) -> None:
    calls = {"n": 0}

    def handler(_req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200)

    queue = ApprovalQueue(tmp_path)
    intent = _intent()
    queue.submit(intent, mode="manual")
    rec = queue.get(intent.fingerprint())
    assert rec is not None

    bridge = _bridge(handler=handler, approvals=queue)
    try:
        result = bridge.dispatch(
            rec, risk_summary={"oops": "AKIAEXAMPLE123ABCDEF"}
        )
    finally:
        bridge.close()

    assert calls["n"] == 0          # never sent over the wire
    assert result.ok is False
    assert result.attempts == 0
    assert result.status_code is None
    assert "secret-scrub" in (result.error or "")

    annotated = queue.get(rec.id)
    assert annotated is not None
    assert annotated.dispatch is not None
    assert annotated.dispatch["ok"] is False
    assert "secret-scrub" in annotated.dispatch["error"]


# ---- no queue wired: dispatch still returns a result --------------------


def test_dispatch_without_queue_returns_result(tmp_path: Path) -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    rec = _record()
    bridge = _bridge(handler=handler, approvals=None)
    try:
        result = bridge.dispatch(rec)
    finally:
        bridge.close()

    assert result.ok is True
    assert result.status_code == 200


# ---- DispatchResult.to_dict serialisable --------------------------------


def test_dispatch_result_to_dict_is_jsonable() -> None:
    result = DispatchResult(
        approval_id="abc",
        ok=True,
        status_code=200,
        attempts=1,
        elapsed_s=0.123,
        error=None,
        response_excerpt="{}",
        dispatched_at=dt.datetime(2026, 5, 23, 3, 10, tzinfo=UTC),
    )
    encoded = json.dumps(result.to_dict())
    decoded = json.loads(encoded)
    assert decoded["approval_id"] == "abc"
    assert decoded["elapsed_s"] == 0.123
    assert decoded["dispatched_at"].startswith("2026-05-23")
