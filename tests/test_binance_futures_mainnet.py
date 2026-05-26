"""Tests for `xtrade.live.binance_futures_mainnet` (Phase 6 T1 hook).

Covers:
  - parse_instrument_overrides: yaml-shape validation & error messages
  - InstrumentOverride dataclass-level validation
  - apply_instrument_overrides: happy path, leverage echo mismatch,
    REST error surface, idempotent -4046 margin-already-set.
"""

from __future__ import annotations

from typing import Any, Mapping

import pytest

from xtrade.live.binance_futures_mainnet import (
    InstrumentOverride,
    MainnetVenueBootstrapError,
    apply_instrument_overrides,
    parse_instrument_overrides,
)


# ---------------------------------------------------------------------------
# Fake client — captures calls, returns configurable payloads
# ---------------------------------------------------------------------------


class _FakeBinanceAPIException(Exception):
    """Stand-in for `binance.exceptions.BinanceAPIException` —
    duck-typed by attribute `code` so the hook does not need the real
    library at test time."""

    def __init__(self, code: int, message: str = "") -> None:
        super().__init__(message)
        self.code = code


class _FakeFuturesClient:
    def __init__(
        self,
        *,
        leverage_responses: dict[str, Mapping[str, Any]] | None = None,
        margin_type_responses: dict[str, Mapping[str, Any]] | None = None,
        leverage_raises: dict[str, Exception] | None = None,
        margin_type_raises: dict[str, Exception] | None = None,
    ) -> None:
        self.leverage_calls: list[tuple[str, int]] = []
        self.margin_type_calls: list[tuple[str, str]] = []
        self._lev_resp = leverage_responses or {}
        self._mt_resp = margin_type_responses or {}
        self._lev_raises = leverage_raises or {}
        self._mt_raises = margin_type_raises or {}

    def futures_change_leverage(
        self, *, symbol: str, leverage: int
    ) -> Mapping[str, Any]:
        self.leverage_calls.append((symbol, leverage))
        if symbol in self._lev_raises:
            raise self._lev_raises[symbol]
        return self._lev_resp.get(
            symbol, {"symbol": symbol, "leverage": leverage}
        )

    def futures_change_margin_type(
        self, *, symbol: str, marginType: str
    ) -> Mapping[str, Any]:
        self.margin_type_calls.append((symbol, marginType))
        if symbol in self._mt_raises:
            raise self._mt_raises[symbol]
        return self._mt_resp.get(symbol, {"code": 200, "msg": "success"})


# ---------------------------------------------------------------------------
# parse_instrument_overrides
# ---------------------------------------------------------------------------


def test_parse_empty_returns_empty_tuple() -> None:
    assert parse_instrument_overrides(None) == ()
    assert parse_instrument_overrides({}) == ()


def test_parse_single_entry() -> None:
    raw = {"SPCXUSDT": {"leverage": 1, "margin_type": "ISOLATED"}}
    overrides = parse_instrument_overrides(raw)
    assert len(overrides) == 1
    assert overrides[0] == InstrumentOverride(
        symbol="SPCXUSDT", leverage=1, margin_type="ISOLATED"
    )


def test_parse_uppercases_margin_type() -> None:
    raw = {"SPCXUSDT": {"leverage": 1, "margin_type": "isolated"}}
    overrides = parse_instrument_overrides(raw)
    assert overrides[0].margin_type == "ISOLATED"


def test_parse_multiple_entries_preserves_yaml_order() -> None:
    raw = {
        "SPCXUSDT": {"leverage": 1, "margin_type": "ISOLATED"},
        "BTCUSDT": {"leverage": 3, "margin_type": "CROSSED"},
    }
    overrides = parse_instrument_overrides(raw)
    symbols = [o.symbol for o in overrides]
    assert symbols == ["SPCXUSDT", "BTCUSDT"]


def test_parse_rejects_non_mapping_root() -> None:
    with pytest.raises(MainnetVenueBootstrapError, match="must be a mapping"):
        parse_instrument_overrides([{"leverage": 1}])  # type: ignore[arg-type]


def test_parse_rejects_non_mapping_entry() -> None:
    with pytest.raises(MainnetVenueBootstrapError, match="SPCXUSDT"):
        parse_instrument_overrides({"SPCXUSDT": "not-a-mapping"})  # type: ignore[dict-item]


def test_parse_rejects_missing_required_field() -> None:
    with pytest.raises(MainnetVenueBootstrapError, match="margin_type"):
        parse_instrument_overrides({"SPCXUSDT": {"leverage": 1}})


def test_parse_rejects_bad_leverage_value() -> None:
    with pytest.raises(MainnetVenueBootstrapError, match="leverage"):
        parse_instrument_overrides(
            {"SPCXUSDT": {"leverage": 0, "margin_type": "ISOLATED"}}
        )
    with pytest.raises(MainnetVenueBootstrapError, match="leverage"):
        parse_instrument_overrides(
            {"SPCXUSDT": {"leverage": 200, "margin_type": "ISOLATED"}}
        )


def test_parse_rejects_bad_margin_type() -> None:
    with pytest.raises(MainnetVenueBootstrapError, match="margin_type"):
        parse_instrument_overrides(
            {"SPCXUSDT": {"leverage": 1, "margin_type": "HEDGED"}}
        )


# ---------------------------------------------------------------------------
# InstrumentOverride dataclass-level validation
# ---------------------------------------------------------------------------


def test_instrument_override_rejects_empty_symbol() -> None:
    with pytest.raises(ValueError, match="symbol"):
        InstrumentOverride(symbol="", leverage=1, margin_type="ISOLATED")


def test_instrument_override_rejects_non_int_leverage() -> None:
    with pytest.raises(ValueError, match="leverage"):
        InstrumentOverride(
            symbol="X", leverage=1.5, margin_type="ISOLATED"  # type: ignore[arg-type]
        )


def test_instrument_override_rejects_out_of_range_leverage() -> None:
    with pytest.raises(ValueError, match="leverage"):
        InstrumentOverride(symbol="X", leverage=0, margin_type="ISOLATED")
    with pytest.raises(ValueError, match="leverage"):
        InstrumentOverride(symbol="X", leverage=126, margin_type="ISOLATED")


# ---------------------------------------------------------------------------
# apply_instrument_overrides — happy paths
# ---------------------------------------------------------------------------


def test_apply_calls_both_apis_in_order() -> None:
    client = _FakeFuturesClient()
    overrides = parse_instrument_overrides(
        {"SPCXUSDT": {"leverage": 1, "margin_type": "ISOLATED"}}
    )
    apply_instrument_overrides(client, overrides)
    assert client.leverage_calls == [("SPCXUSDT", 1)]
    assert client.margin_type_calls == [("SPCXUSDT", "ISOLATED")]


def test_apply_handles_multiple_symbols() -> None:
    client = _FakeFuturesClient()
    overrides = parse_instrument_overrides(
        {
            "SPCXUSDT": {"leverage": 1, "margin_type": "ISOLATED"},
            "BTCUSDT": {"leverage": 5, "margin_type": "CROSSED"},
        }
    )
    apply_instrument_overrides(client, overrides)
    assert client.leverage_calls == [("SPCXUSDT", 1), ("BTCUSDT", 5)]
    assert client.margin_type_calls == [
        ("SPCXUSDT", "ISOLATED"),
        ("BTCUSDT", "CROSSED"),
    ]


def test_apply_skips_calls_for_empty_overrides() -> None:
    client = _FakeFuturesClient()
    apply_instrument_overrides(client, ())
    assert client.leverage_calls == []
    assert client.margin_type_calls == []


# ---------------------------------------------------------------------------
# apply_instrument_overrides — leverage echo-back verification
# ---------------------------------------------------------------------------


def test_apply_raises_on_leverage_echo_mismatch() -> None:
    client = _FakeFuturesClient(
        leverage_responses={
            "SPCXUSDT": {"symbol": "SPCXUSDT", "leverage": 5}
        },  # requested 1, server says 5
    )
    overrides = parse_instrument_overrides(
        {"SPCXUSDT": {"leverage": 1, "margin_type": "ISOLATED"}}
    )
    with pytest.raises(MainnetVenueBootstrapError, match="echo-back mismatch"):
        apply_instrument_overrides(client, overrides)


def test_apply_accepts_missing_leverage_in_response() -> None:
    """Some venues may not echo `leverage` back. The hook must not
    require it — only verify if present."""
    client = _FakeFuturesClient(
        leverage_responses={"SPCXUSDT": {"symbol": "SPCXUSDT"}},
    )
    overrides = parse_instrument_overrides(
        {"SPCXUSDT": {"leverage": 1, "margin_type": "ISOLATED"}}
    )
    apply_instrument_overrides(client, overrides)  # no raise


# ---------------------------------------------------------------------------
# apply_instrument_overrides — error surface
# ---------------------------------------------------------------------------


def test_apply_wraps_leverage_rest_error() -> None:
    client = _FakeFuturesClient(
        leverage_raises={"SPCXUSDT": RuntimeError("rate limited")}
    )
    overrides = parse_instrument_overrides(
        {"SPCXUSDT": {"leverage": 1, "margin_type": "ISOLATED"}}
    )
    with pytest.raises(MainnetVenueBootstrapError, match="futures_change_leverage"):
        apply_instrument_overrides(client, overrides)


def test_apply_wraps_margin_type_rest_error() -> None:
    client = _FakeFuturesClient(
        margin_type_raises={"SPCXUSDT": RuntimeError("internal server error")}
    )
    overrides = parse_instrument_overrides(
        {"SPCXUSDT": {"leverage": 1, "margin_type": "ISOLATED"}}
    )
    with pytest.raises(MainnetVenueBootstrapError, match="futures_change_margin_type"):
        apply_instrument_overrides(client, overrides)


def test_apply_treats_neg4046_as_idempotent() -> None:
    """Binance returns code -4046 when margin type is already set to
    the requested value. The hook must treat that as benign."""
    client = _FakeFuturesClient(
        margin_type_raises={
            "SPCXUSDT": _FakeBinanceAPIException(
                code=-4046, message="No need to change margin type."
            )
        }
    )
    overrides = parse_instrument_overrides(
        {"SPCXUSDT": {"leverage": 1, "margin_type": "ISOLATED"}}
    )
    apply_instrument_overrides(client, overrides)  # no raise


def test_apply_raises_on_non_4046_api_code() -> None:
    """Any other API exception with a code must still raise."""
    client = _FakeFuturesClient(
        margin_type_raises={
            "SPCXUSDT": _FakeBinanceAPIException(
                code=-1021, message="Timestamp out of recvWindow"
            )
        }
    )
    overrides = parse_instrument_overrides(
        {"SPCXUSDT": {"leverage": 1, "margin_type": "ISOLATED"}}
    )
    with pytest.raises(MainnetVenueBootstrapError, match="-1021"):
        apply_instrument_overrides(client, overrides)


def test_apply_raises_on_non_ok_response_code() -> None:
    """If the venue returns a code != 200 / -4046 in the body, raise."""
    client = _FakeFuturesClient(
        margin_type_responses={
            "SPCXUSDT": {"code": -2010, "msg": "Account has insufficient balance."}
        }
    )
    overrides = parse_instrument_overrides(
        {"SPCXUSDT": {"leverage": 1, "margin_type": "ISOLATED"}}
    )
    with pytest.raises(MainnetVenueBootstrapError, match="non-OK code"):
        apply_instrument_overrides(client, overrides)
