"""Tests for `xtrade.bridge.alerter.AlertBridge` (Phase 6 Task T9)."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any, Callable

import httpx
import pytest

from xtrade.bridge.alerter import (
    AlertAuditWriter,
    AlertBridge,
    AlertBridgeConfigError,
    AlertPayloadError,
)


UTC = dt.timezone.utc


# ---- helpers --------------------------------------------------------------


def _bridge(
    *,
    handler: Callable[[httpx.Request], httpx.Response],
    sleeps: list[float] | None = None,
    backoffs: tuple[float, ...] = (0.01, 0.01, 0.01, 0.01),
    audit_root: Path | None = None,
    now_ts: dt.datetime | None = None,
    alert_id: str = "AL-20260524T120000Z-deadbe",
) -> AlertBridge:
    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport)
    fake_sleep = (sleeps.append if sleeps is not None else (lambda _x: None))
    fixed_now = now_ts or dt.datetime(2026, 5, 24, 12, 0, tzinfo=UTC)
    writer = AlertAuditWriter(audit_root) if audit_root is not None else None
    return AlertBridge(
        gateway_url="https://openclaw.test",
        shared_secret="topsecret",
        audit_writer=writer,
        client=client,
        backoffs=backoffs,
        sleep=fake_sleep,
        now=lambda: fixed_now,
        alert_id_factory=lambda: alert_id,
    )


# ---- config validation ----------------------------------------------------


def test_constructor_rejects_bad_gateway() -> None:
    with pytest.raises(AlertBridgeConfigError):
        AlertBridge(gateway_url="openclaw.test", shared_secret="x")


def test_constructor_rejects_empty_secret() -> None:
    with pytest.raises(AlertBridgeConfigError):
        AlertBridge(gateway_url="https://openclaw.test", shared_secret="")


def test_constructor_rejects_empty_backoffs() -> None:
    with pytest.raises(AlertBridgeConfigError):
        AlertBridge(
            gateway_url="https://openclaw.test",
            shared_secret="x",
            backoffs=(),
        )


def test_constructor_rejects_bad_route() -> None:
    with pytest.raises(AlertBridgeConfigError):
        AlertBridge(
            gateway_url="https://openclaw.test",
            shared_secret="x",
            route="no-leading-slash",
        )


def test_from_env_requires_keys() -> None:
    with pytest.raises(AlertBridgeConfigError, match="OPENCLAW_GATEWAY"):
        AlertBridge.from_env({"OPENCLAW_SHARED_SECRET": "x"})
    with pytest.raises(AlertBridgeConfigError, match="OPENCLAW_SHARED_SECRET"):
        AlertBridge.from_env({"OPENCLAW_GATEWAY": "https://gw"})


def test_from_env_builds_bridge() -> None:
    transport = httpx.MockTransport(lambda req: httpx.Response(200))
    client = httpx.Client(transport=transport)
    bridge = AlertBridge.from_env(
        {"OPENCLAW_GATEWAY": "https://gw.example", "OPENCLAW_SHARED_SECRET": "s"},
        client=client,
    )
    try:
        # `_gateway` is normalised (no trailing slash) — sanity check.
        assert bridge._gateway == "https://gw.example"
        assert bridge._route == "/webhooks/xtrade/alerts"
    finally:
        bridge.close()
        client.close()


# ---- payload validation ---------------------------------------------------


def test_dispatch_rejects_bad_severity() -> None:
    bridge = _bridge(handler=lambda req: httpx.Response(200))
    try:
        with pytest.raises(AlertPayloadError):
            bridge.dispatch_alert(
                severity="emergency",  # type: ignore[arg-type]
                event="x",
                message="hi",
            )
    finally:
        bridge.close()


def test_dispatch_rejects_empty_event() -> None:
    bridge = _bridge(handler=lambda req: httpx.Response(200))
    try:
        with pytest.raises(AlertPayloadError):
            bridge.dispatch_alert(severity="info", event="", message="hi")
    finally:
        bridge.close()


def test_dispatch_rejects_oversize_message() -> None:
    bridge = _bridge(handler=lambda req: httpx.Response(200))
    try:
        with pytest.raises(AlertPayloadError, match="200"):
            bridge.dispatch_alert(
                severity="info",
                event="x.y",
                message="a" * 201,
            )
    finally:
        bridge.close()


def test_dispatch_rejects_non_scalar_field() -> None:
    bridge = _bridge(handler=lambda req: httpx.Response(200))
    try:
        with pytest.raises(AlertPayloadError, match="scalar"):
            bridge.dispatch_alert(
                severity="info",
                event="x.y",
                message="hi",
                fields={"nested": {"k": "v"}},
            )
    finally:
        bridge.close()


def test_dispatch_rejects_non_string_field_key() -> None:
    bridge = _bridge(handler=lambda req: httpx.Response(200))
    try:
        with pytest.raises(AlertPayloadError, match="keys"):
            bridge.dispatch_alert(
                severity="info",
                event="x.y",
                message="hi",
                fields={1: "v"},  # type: ignore[dict-item]
            )
    finally:
        bridge.close()


def test_dispatch_rejects_non_str_instrument() -> None:
    bridge = _bridge(handler=lambda req: httpx.Response(200))
    try:
        with pytest.raises(AlertPayloadError):
            bridge.dispatch_alert(
                severity="info",
                event="x.y",
                message="hi",
                instrument=123,  # type: ignore[arg-type]
            )
    finally:
        bridge.close()


# ---- secret scrub ---------------------------------------------------------


def test_dispatch_refuses_when_message_carries_secret() -> None:
    captured: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        captured.append(req)
        return httpx.Response(200)

    bridge = _bridge(handler=handler)
    try:
        # AWS-shaped string in the message must short-circuit dispatch.
        result = bridge.dispatch_alert(
            severity="warn",
            event="ops.test",
            message="AKIAIOSFODNN7EXAMPLE leaked",
        )
    finally:
        bridge.close()

    assert result.ok is False
    assert result.attempts == 0
    assert "secret-scrub" in (result.error or "")
    assert captured == []  # never made an HTTP call


def test_dispatch_refuses_when_field_carries_secret() -> None:
    bridge = _bridge(handler=lambda req: httpx.Response(200))
    try:
        result = bridge.dispatch_alert(
            severity="info",
            event="ops.test",
            message="ok",
            fields={"key": "sk-abcdef0123456789abcdef0123456789"},
        )
    finally:
        bridge.close()
    assert result.ok is False
    assert "secret-scrub" in (result.error or "")


# ---- happy path -----------------------------------------------------------


def test_dispatch_200_returns_ok_and_posts_correct_envelope() -> None:
    captured: dict[str, Any] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["url"] = str(req.url)
        captured["auth"] = req.headers.get("Authorization")
        captured["ct"] = req.headers.get("Content-Type")
        captured["body"] = json.loads(req.content.decode())
        return httpx.Response(200, json={"alert_received": True})

    bridge = _bridge(handler=handler)
    try:
        result = bridge.dispatch_alert(
            severity="crit",
            event="supervisor.mcap.softkill",
            message="mcap breach triggered",
            instrument="SPCXUSDT-PERP.BINANCE",
            fields={"mcap_usd": "3500000000000", "consecutive": 3},
        )
    finally:
        bridge.close()

    assert result.ok is True
    assert result.status_code == 200
    assert result.attempts == 1
    assert result.severity == "crit"
    assert result.event == "supervisor.mcap.softkill"

    # wire contract
    assert captured["url"] == "https://openclaw.test/webhooks/xtrade/alerts"
    assert captured["auth"] == "Bearer topsecret"
    assert captured["ct"] == "application/json"
    body = captured["body"]
    assert body["action"] == "create_alert"
    assert body["severity"] == "crit"
    assert body["event"] == "supervisor.mcap.softkill"
    assert body["message"] == "mcap breach triggered"
    assert body["instrument"] == "SPCXUSDT-PERP.BINANCE"
    assert body["fields"] == {"mcap_usd": "3500000000000", "consecutive": 3}
    assert body["dispatched_at"].endswith("+00:00")


def test_dispatch_omits_instrument_when_none() -> None:
    captured: dict[str, Any] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(req.content.decode())
        return httpx.Response(200)

    bridge = _bridge(handler=handler)
    try:
        bridge.dispatch_alert(severity="info", event="ops.test", message="hi")
    finally:
        bridge.close()

    assert "instrument" not in captured["body"]
    # `fields` always present (possibly empty) so the openclaw side has
    # one stable shape.
    assert captured["body"]["fields"] == {}


# ---- retry: 5xx then success ---------------------------------------------


def test_dispatch_retries_5xx_then_succeeds() -> None:
    calls: list[int] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) < 3:
            return httpx.Response(503, text="busy")
        return httpx.Response(200)

    sleeps: list[float] = []
    bridge = _bridge(handler=handler, sleeps=sleeps)
    try:
        result = bridge.dispatch_alert(
            severity="warn", event="ops.test", message="x"
        )
    finally:
        bridge.close()

    assert result.ok is True
    assert result.attempts == 3
    assert len(calls) == 3
    # 2 retries → 2 sleeps from the backoff schedule
    assert len(sleeps) == 2


# ---- retry: 4xx terminal (no retry) --------------------------------------


def test_dispatch_4xx_is_terminal_no_retry() -> None:
    calls: list[int] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(400, text="bad-payload")

    sleeps: list[float] = []
    bridge = _bridge(handler=handler, sleeps=sleeps)
    try:
        result = bridge.dispatch_alert(
            severity="warn", event="ops.test", message="x"
        )
    finally:
        bridge.close()

    assert result.ok is False
    assert result.attempts == 1
    assert result.status_code == 400
    assert "client error" in (result.error or "")
    assert len(calls) == 1
    assert sleeps == []


# ---- retry: 5xx exhausted -------------------------------------------------


def test_dispatch_exhausts_retries_on_persistent_5xx() -> None:
    calls: list[int] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(500, text="boom")

    sleeps: list[float] = []
    bridge = _bridge(
        handler=handler,
        sleeps=sleeps,
        backoffs=(0.01, 0.01, 0.01, 0.01),
    )
    try:
        result = bridge.dispatch_alert(
            severity="crit", event="ops.test", message="x"
        )
    finally:
        bridge.close()

    assert result.ok is False
    assert result.attempts == 4
    assert result.status_code == 500
    assert len(calls) == 4
    # 4 attempts → 3 inter-attempt sleeps (no sleep after last)
    assert len(sleeps) == 3


# ---- retry: network error -------------------------------------------------


def test_dispatch_handles_network_error_then_success() -> None:
    calls: list[int] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ConnectError("DNS exploded")
        return httpx.Response(200)

    sleeps: list[float] = []
    bridge = _bridge(handler=handler, sleeps=sleeps)
    try:
        result = bridge.dispatch_alert(
            severity="warn", event="ops.test", message="x"
        )
    finally:
        bridge.close()

    assert result.ok is True
    assert result.attempts == 2
    assert len(sleeps) == 1


# ---- audit jsonl ----------------------------------------------------------


def test_dispatch_writes_audit_row_on_ok(tmp_path: Path) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    bridge = _bridge(handler=handler, audit_root=tmp_path)
    try:
        bridge.dispatch_alert(
            severity="info", event="supervisor.start", message="started"
        )
    finally:
        bridge.close()

    shard = tmp_path / "alerts.2026-05-24.jsonl"
    assert shard.exists()
    rows = [json.loads(line) for line in shard.read_text().splitlines()]
    assert len(rows) == 1
    row = rows[0]
    assert row["kind"] == "ok"
    assert row["severity"] == "info"
    assert row["event"] == "supervisor.start"
    assert row["status_code"] == 200
    assert row["alert_id"] == "AL-20260524T120000Z-deadbe"


def test_dispatch_writes_audit_rows_for_retry_then_ok(tmp_path: Path) -> None:
    calls: list[int] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) < 2:
            return httpx.Response(503)
        return httpx.Response(200)

    bridge = _bridge(handler=handler, audit_root=tmp_path)
    try:
        bridge.dispatch_alert(
            severity="warn", event="ops.test", message="x"
        )
    finally:
        bridge.close()

    shard = tmp_path / "alerts.2026-05-24.jsonl"
    rows = [json.loads(line) for line in shard.read_text().splitlines()]
    kinds = [r["kind"] for r in rows]
    # one retry row, one ok row
    assert kinds == ["retry", "ok"]


def test_dispatch_writes_audit_row_on_refused(tmp_path: Path) -> None:
    bridge = _bridge(handler=lambda req: httpx.Response(200), audit_root=tmp_path)
    try:
        bridge.dispatch_alert(
            severity="info",
            event="x.y",
            message="AKIAIOSFODNN7EXAMPLE",
        )
    finally:
        bridge.close()
    shard = tmp_path / "alerts.2026-05-24.jsonl"
    rows = [json.loads(line) for line in shard.read_text().splitlines()]
    assert rows[0]["kind"] == "refused"
    assert "secret-scrub" in rows[0]["error"]


def test_audit_writer_rejects_naive_dispatched_at(tmp_path: Path) -> None:
    writer = AlertAuditWriter(tmp_path)
    with pytest.raises(ValueError, match="timezone-aware"):
        writer.write(
            alert_id="AL-1",
            severity="info",
            event="x",
            attempt=1,
            kind="ok",
            status_code=200,
            error=None,
            dispatched_at=dt.datetime(2026, 5, 24, 12, 0),  # naive
            elapsed_s=0.0,
        )


def test_audit_writer_rejects_bad_kind(tmp_path: Path) -> None:
    writer = AlertAuditWriter(tmp_path)
    with pytest.raises(ValueError, match="kind"):
        writer.write(
            alert_id="AL-1",
            severity="info",
            event="x",
            attempt=1,
            kind="weird",  # type: ignore[arg-type]
            status_code=200,
            error=None,
            dispatched_at=dt.datetime(2026, 5, 24, 12, 0, tzinfo=UTC),
            elapsed_s=0.0,
        )


def test_audit_writer_rejects_bad_severity(tmp_path: Path) -> None:
    writer = AlertAuditWriter(tmp_path)
    with pytest.raises(ValueError, match="severity"):
        writer.write(
            alert_id="AL-1",
            severity="emergency",  # type: ignore[arg-type]
            event="x",
            attempt=1,
            kind="ok",
            status_code=200,
            error=None,
            dispatched_at=dt.datetime(2026, 5, 24, 12, 0, tzinfo=UTC),
            elapsed_s=0.0,
        )
