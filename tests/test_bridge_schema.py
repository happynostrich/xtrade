"""Tests for `xtrade.bridge.schema` (Phase 4 Task 2)."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from xtrade.approval.queue import ApprovalRecord
from xtrade.bridge.schema import (
    BridgePayload,
    CallbackUrls,
    SecretLeakError,
    build_payload,
    scrub_payload_for_secrets,
)
from xtrade.strategy.intent import OrderIntent


UTC = dt.timezone.utc


def _intent(**overrides: object) -> OrderIntent:
    defaults = dict(
        venue="binance",
        symbol="BTCUSDT-PERP.BINANCE",
        side="BUY",
        order_type="MARKET",
        quantity=Decimal("0.002"),
        limit_price=None,
        reduce_only=False,
        time_in_force="IOC",
        source_signal_id="manual:runbook",
        created_at=dt.datetime(2026, 5, 23, 1, 38, 52, tzinfo=UTC),
    )
    defaults.update(overrides)
    return OrderIntent(**defaults)  # type: ignore[arg-type]


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


# ---- CallbackUrls / BridgePayload structural shape ----------------------


def test_callback_urls_round_trip() -> None:
    cb = CallbackUrls(
        confirm_url="http://127.0.0.1:18080/approvals/abc/confirm",
        reject_url="http://127.0.0.1:18080/approvals/abc/reject",
        ttl_s=900,
    )
    d = cb.to_dict()
    assert d["confirm_url"].endswith("/confirm")
    assert d["reject_url"].endswith("/reject")
    assert d["ttl_s"] == 900


def test_build_payload_shape_and_callbacks() -> None:
    rec = _record()
    payload = build_payload(
        rec,
        callback_base_url="http://127.0.0.1:18080",
        ttl_s=600,
        risk_summary={"rules_passed": ["max_notional_per_order"]},
    )
    body = payload.to_dict()
    assert body["action"] == "create_flow"
    assert "BUY" in body["goal"] and rec.id in body["goal"]
    meta = body["metadata"]
    assert meta["approval_id"] == rec.id
    assert meta["intent"]["symbol"] == "BTCUSDT-PERP.BINANCE"
    assert meta["intent"]["quantity"] == "0.002"     # Decimal preserved as str
    assert meta["risk_summary"]["rules_passed"] == ["max_notional_per_order"]
    cb = meta["callback"]
    assert cb["confirm_url"].endswith(f"/approvals/{rec.id}/confirm")
    assert cb["reject_url"].endswith(f"/approvals/{rec.id}/reject")
    assert cb["ttl_s"] == 600


def test_build_payload_strips_trailing_slash_on_base() -> None:
    rec = _record()
    payload = build_payload(
        rec, callback_base_url="http://127.0.0.1:18080/", ttl_s=900
    )
    cb = payload.callback
    assert "//approvals" not in cb.confirm_url
    assert "//approvals" not in cb.reject_url


def test_build_payload_respects_goal_override() -> None:
    payload = build_payload(
        _record(),
        callback_base_url="http://127.0.0.1:18080",
        goal_override="custom",
    )
    assert payload.goal == "custom"


def test_payload_is_frozen() -> None:
    payload = build_payload(
        _record(), callback_base_url="http://127.0.0.1:18080"
    )
    with pytest.raises(dataclasses_FrozenError := Exception):
        payload.action = "mutated"  # type: ignore[misc]


# ---- credential scrub ----------------------------------------------------


def test_scrub_passes_clean_payload() -> None:
    payload = build_payload(
        _record(), callback_base_url="http://127.0.0.1:18080"
    )
    scrub_payload_for_secrets(payload)  # must not raise


@pytest.mark.parametrize(
    "leak",
    [
        "sk-abcdefghijklmnop1234",                              # OpenAI/Anthropic
        "0x" + "a" * 64,                                        # EVM private key
        "AKIAEXAMPLE123ABCDEF",                                 # AWS access key (AKIA + 16)
    ],
)
def test_scrub_rejects_credential_shaped_string_in_risk_summary(leak: str) -> None:
    rec = _record()
    payload = build_payload(
        rec,
        callback_base_url="http://127.0.0.1:18080",
        risk_summary={"oops_leak": leak},
    )
    with pytest.raises(SecretLeakError):
        scrub_payload_for_secrets(payload)


def test_scrub_walks_nested_structures() -> None:
    payload = BridgePayload(
        action="create_flow",
        goal="ok",
        approval_id="x",
        intent={"nested": {"deeper": ["clean", "0x" + "0" * 64]}},
        risk_summary={},
        callback=CallbackUrls(
            confirm_url="http://x/confirm",
            reject_url="http://x/reject",
            ttl_s=1,
        ),
    )
    with pytest.raises(SecretLeakError):
        scrub_payload_for_secrets(payload)


def test_scrub_accepts_raw_dict_input() -> None:
    scrub_payload_for_secrets({"action": "x", "metadata": {"clean": "value"}})
    with pytest.raises(SecretLeakError):
        scrub_payload_for_secrets({"leak": "AKIAEXAMPLE123ABCDEF"})


def test_scrub_ignores_bytes() -> None:
    # bytes are not walked (we only inspect str/dict/list/tuple/set)
    scrub_payload_for_secrets({"binary": b"AKIAEXAMPLE123ABCDEFG"})
