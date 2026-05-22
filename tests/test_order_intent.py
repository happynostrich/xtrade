"""Tests for `xtrade.strategy.intent` (Phase 3 Task 1 / T2)."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from xtrade.strategy.intent import (
    Fill,
    OrderIntent,
    OrderIntentError,
)


UTC = dt.timezone.utc


def _now() -> dt.datetime:
    return dt.datetime(2026, 5, 22, 10, 0, 0, tzinfo=UTC)


def _good_limit_intent(**overrides) -> OrderIntent:
    base = dict(
        venue="binance",
        symbol="BTCUSDT-PERP.BINANCE",
        side="BUY",
        order_type="LIMIT",
        quantity=Decimal("0.001"),
        limit_price=Decimal("50000.00"),
        reduce_only=False,
        time_in_force="GTC",
        source_signal_id="sig-1",
        created_at=_now(),
    )
    base.update(overrides)
    return OrderIntent(**base)


def _good_market_intent(**overrides) -> OrderIntent:
    base = dict(
        venue="binance",
        symbol="BTCUSDT-PERP.BINANCE",
        side="SELL",
        order_type="MARKET",
        quantity=Decimal("0.5"),
        limit_price=None,
        reduce_only=True,
        time_in_force="IOC",
        source_signal_id="",
        created_at=_now(),
    )
    base.update(overrides)
    return OrderIntent(**base)


# --- happy paths ----------------------------------------------------------


def test_limit_intent_happy_path() -> None:
    intent = _good_limit_intent()
    assert intent.side == "BUY"
    assert intent.limit_price == Decimal("50000.00")


def test_market_intent_happy_path() -> None:
    intent = _good_market_intent()
    assert intent.order_type == "MARKET"
    assert intent.limit_price is None


# --- construction-time validation ----------------------------------------


@pytest.mark.parametrize("bad_venue", ["", None, 0])
def test_empty_venue_is_rejected(bad_venue) -> None:
    with pytest.raises(OrderIntentError):
        _good_limit_intent(venue=bad_venue)


def test_empty_symbol_is_rejected() -> None:
    with pytest.raises(OrderIntentError):
        _good_limit_intent(symbol="")


def test_unknown_side_is_rejected() -> None:
    with pytest.raises(OrderIntentError):
        _good_limit_intent(side="HOLD")  # type: ignore[arg-type]


def test_unknown_order_type_is_rejected() -> None:
    with pytest.raises(OrderIntentError):
        _good_limit_intent(order_type="ICEBERG")  # type: ignore[arg-type]


def test_quantity_must_be_decimal() -> None:
    with pytest.raises(OrderIntentError):
        _good_limit_intent(quantity=0.5)  # type: ignore[arg-type]


def test_quantity_must_be_positive() -> None:
    with pytest.raises(OrderIntentError):
        _good_limit_intent(quantity=Decimal("0"))
    with pytest.raises(OrderIntentError):
        _good_limit_intent(quantity=Decimal("-1"))


def test_limit_order_requires_limit_price() -> None:
    with pytest.raises(OrderIntentError):
        _good_limit_intent(limit_price=None)


def test_limit_order_limit_price_must_be_positive() -> None:
    with pytest.raises(OrderIntentError):
        _good_limit_intent(limit_price=Decimal("0"))


def test_market_order_must_not_carry_limit_price() -> None:
    with pytest.raises(OrderIntentError):
        _good_market_intent(limit_price=Decimal("123"))


def test_unknown_time_in_force_is_rejected() -> None:
    with pytest.raises(OrderIntentError):
        _good_limit_intent(time_in_force="GTX")  # type: ignore[arg-type]


def test_reduce_only_must_be_bool() -> None:
    with pytest.raises(OrderIntentError):
        _good_limit_intent(reduce_only=1)  # type: ignore[arg-type]


def test_created_at_must_be_tz_aware() -> None:
    naive = dt.datetime(2026, 5, 22, 10, 0, 0)
    with pytest.raises(OrderIntentError):
        _good_limit_intent(created_at=naive)


def test_metadata_must_be_dict() -> None:
    with pytest.raises(OrderIntentError):
        _good_limit_intent(metadata=["not", "a", "dict"])  # type: ignore[arg-type]


# --- serialisation round-trip --------------------------------------------


def test_limit_intent_roundtrip_preserves_decimal_precision() -> None:
    intent = _good_limit_intent(
        quantity=Decimal("0.00012345"),
        limit_price=Decimal("12345.6789"),
    )
    payload = intent.to_dict()
    assert payload["quantity"] == "0.00012345"
    assert payload["limit_price"] == "12345.6789"
    restored = OrderIntent.from_dict(payload)
    assert restored == intent
    assert restored.quantity == Decimal("0.00012345")
    assert restored.limit_price == Decimal("12345.6789")


def test_market_intent_roundtrip_handles_none_limit_price() -> None:
    intent = _good_market_intent()
    restored = OrderIntent.from_dict(intent.to_dict())
    assert restored == intent
    assert restored.limit_price is None


def test_from_dict_requires_tz_aware_created_at() -> None:
    intent = _good_limit_intent()
    payload = intent.to_dict()
    payload["created_at"] = "2026-05-22T10:00:00"  # no tz
    with pytest.raises(OrderIntentError):
        OrderIntent.from_dict(payload)


def test_fingerprint_is_stable_and_unique() -> None:
    a = _good_limit_intent()
    b = _good_limit_intent()
    assert a.fingerprint() == b.fingerprint()
    c = _good_limit_intent(quantity=Decimal("0.002"))
    assert c.fingerprint() != a.fingerprint()
    assert len(a.fingerprint()) == 16


# --- Fill -----------------------------------------------------------------


def test_fill_happy_path_roundtrip() -> None:
    fill = Fill(
        venue="binance",
        symbol="BTCUSDT-PERP.BINANCE",
        side="BUY",
        quantity=Decimal("0.001"),
        price=Decimal("50000.5"),
        ts_event=_now(),
        intent_fingerprint="abcd1234deadbeef",
        order_id="order-1",
        metadata={"path": "paper"},
    )
    restored = Fill.from_dict(fill.to_dict())
    assert restored == fill


def test_fill_rejects_naive_ts_event() -> None:
    with pytest.raises(OrderIntentError):
        Fill(
            venue="binance",
            symbol="BTCUSDT-PERP.BINANCE",
            side="BUY",
            quantity=Decimal("0.001"),
            price=Decimal("50000.5"),
            ts_event=dt.datetime(2026, 5, 22, 10, 0, 0),  # naive
            intent_fingerprint="abc",
        )


def test_fill_requires_positive_quantity_and_price() -> None:
    base = dict(
        venue="binance",
        symbol="BTCUSDT-PERP.BINANCE",
        side="BUY",
        ts_event=_now(),
        intent_fingerprint="abc",
    )
    with pytest.raises(OrderIntentError):
        Fill(**base, quantity=Decimal("0"), price=Decimal("1"))
    with pytest.raises(OrderIntentError):
        Fill(**base, quantity=Decimal("1"), price=Decimal("0"))


def test_fill_requires_non_empty_fingerprint() -> None:
    with pytest.raises(OrderIntentError):
        Fill(
            venue="binance",
            symbol="BTCUSDT-PERP.BINANCE",
            side="BUY",
            quantity=Decimal("0.001"),
            price=Decimal("50000.5"),
            ts_event=_now(),
            intent_fingerprint="",
        )
