"""Tests for `xtrade.research.scanners.threshold_ladder_entry` (Phase 6 T6).

Brief §T6 acceptance matrix:

  direction=above (SPCX-style short)
    - heavy fires once on the edge crossing
    - mark dips below + re-crosses → no re-emit
    - dca dedups within a UTC day, fires once per new day
    - signal direction is SHORT; intent_side metadata is SELL

  direction=below (long mirror)
    - heavy fires once on the edge crossing
    - dca dedups within a UTC day, fires once per new day
    - signal direction is LONG; intent_side metadata is BUY

  envelope contract
    - `qty` metadata satisfies qty_step quantization
    - signal source = "<prefix>.heavy.v1" or "<prefix>.dca.<yyyymmdd>"

  boundary (ctor)
    - fingerprint_prefix missing/empty → ctor raises
    - direction missing/invalid → ctor raises
    - instrument mismatch with meta.symbol → ctor raises
    - meta wrong type → ctor raises
    - direction-aware sanity: above requires dca < heavy / below requires dca > heavy
    - non-positive triggers / margins → ctor raises
    - non-tz-aware ts → on_mark raises
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from xtrade.instruments.meta import InstrumentMeta
from xtrade.research.scanners import (
    ScannerConfigError,
    ThresholdLadderEntryScanner,
)
from xtrade.research.signals import Signal


UTC = dt.timezone.utc
SYMBOL = "SPCXUSDT-PERP.BINANCE"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _meta() -> InstrumentMeta:
    return InstrumentMeta(
        symbol=SYMBOL,
        shares_outstanding=Decimal("11870000000"),
        min_qty=Decimal("0.001"),
        qty_step=Decimal("0.001"),
        tick_size=Decimal("0.01"),
        mark_source="oracle",
    )


def _above_config(**overrides) -> dict:
    cfg: dict = {
        "instrument": SYMBOL,
        "venue": "BINANCE",
        "direction": "above",
        "heavy_trigger_mark_usd": "225",
        "dca_trigger_mark_usd": "210",
        "heavy_margin_usd": "150",
        "dca_margin_usd": "10",
        "dca_window": "daily",
        "fingerprint_prefix": "spcxusdt.short_mcap.v1",
        "limit_price_bps_offset": "5",
    }
    cfg.update(overrides)
    return cfg


def _below_config(**overrides) -> dict:
    # Mirror: dca trigger sits *above* heavy trigger (later entry on the
    # way down).
    cfg: dict = {
        "instrument": SYMBOL,
        "venue": "BINANCE",
        "direction": "below",
        "heavy_trigger_mark_usd": "50",
        "dca_trigger_mark_usd": "55",
        "heavy_margin_usd": "100",
        "dca_margin_usd": "10",
        "dca_window": "daily",
        "fingerprint_prefix": "longinst.v1",
    }
    cfg.update(overrides)
    return cfg


def _ts(hour: int = 10, day: int = 22) -> dt.datetime:
    return dt.datetime(2026, 5, day, hour, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Ctor boundary
# ---------------------------------------------------------------------------


def test_direction_missing_raises() -> None:
    cfg = _above_config()
    del cfg["direction"]
    with pytest.raises(ScannerConfigError, match="direction"):
        ThresholdLadderEntryScanner(cfg, _meta())


def test_direction_invalid_raises() -> None:
    with pytest.raises(ScannerConfigError, match="direction"):
        ThresholdLadderEntryScanner(_above_config(direction="sideways"), _meta())


def test_fingerprint_prefix_missing_raises() -> None:
    cfg = _above_config()
    del cfg["fingerprint_prefix"]
    with pytest.raises(ScannerConfigError, match="fingerprint_prefix"):
        ThresholdLadderEntryScanner(cfg, _meta())


def test_fingerprint_prefix_empty_raises() -> None:
    with pytest.raises(ScannerConfigError, match="fingerprint_prefix"):
        ThresholdLadderEntryScanner(_above_config(fingerprint_prefix=""), _meta())


def test_instrument_mismatch_with_meta_raises() -> None:
    cfg = _above_config(instrument="WRONG-SYMBOL.BINANCE")
    with pytest.raises(ScannerConfigError, match="does not match meta.symbol"):
        ThresholdLadderEntryScanner(cfg, _meta())


def test_meta_wrong_type_raises() -> None:
    with pytest.raises(ScannerConfigError, match="meta"):
        ThresholdLadderEntryScanner(_above_config(), meta=object())  # type: ignore[arg-type]


def test_above_requires_dca_below_heavy() -> None:
    cfg = _above_config(heavy_trigger_mark_usd="210", dca_trigger_mark_usd="225")
    with pytest.raises(ScannerConfigError, match="dca_trigger.*<.*heavy_trigger"):
        ThresholdLadderEntryScanner(cfg, _meta())


def test_below_requires_dca_above_heavy() -> None:
    cfg = _below_config(heavy_trigger_mark_usd="55", dca_trigger_mark_usd="50")
    with pytest.raises(ScannerConfigError, match="dca_trigger.*>.*heavy_trigger"):
        ThresholdLadderEntryScanner(cfg, _meta())


def test_non_positive_trigger_raises() -> None:
    with pytest.raises(ScannerConfigError, match="heavy_trigger_mark_usd.*> 0"):
        ThresholdLadderEntryScanner(_above_config(heavy_trigger_mark_usd="0"), _meta())


def test_non_positive_margin_raises() -> None:
    with pytest.raises(ScannerConfigError, match="heavy_margin_usd.*> 0"):
        ThresholdLadderEntryScanner(_above_config(heavy_margin_usd="-1"), _meta())


def test_dca_window_other_than_daily_raises() -> None:
    with pytest.raises(ScannerConfigError, match="dca_window"):
        ThresholdLadderEntryScanner(_above_config(dca_window="hourly"), _meta())


def test_negative_bps_offset_raises() -> None:
    with pytest.raises(ScannerConfigError, match="limit_price_bps_offset"):
        ThresholdLadderEntryScanner(
            _above_config(limit_price_bps_offset="-1"), _meta()
        )


def test_default_bps_offset_is_zero() -> None:
    cfg = _above_config()
    del cfg["limit_price_bps_offset"]
    scanner = ThresholdLadderEntryScanner(cfg, _meta())
    # Construction succeeded; emit a heavy and check metadata.
    sigs = scanner.on_mark(_ts(), Decimal("225"))
    assert sigs[0].metadata["limit_price_bps_offset"] == "0"


# ---------------------------------------------------------------------------
# on_mark input validation
# ---------------------------------------------------------------------------


def test_on_mark_naive_ts_raises() -> None:
    scanner = ThresholdLadderEntryScanner(_above_config(), _meta())
    naive = dt.datetime(2026, 5, 22, 10, 0, 0)  # no tzinfo
    with pytest.raises(ValueError, match="tz-aware"):
        scanner.on_mark(naive, Decimal("225"))


def test_on_mark_non_datetime_ts_raises() -> None:
    scanner = ThresholdLadderEntryScanner(_above_config(), _meta())
    with pytest.raises(TypeError, match="ts must be datetime"):
        scanner.on_mark("2026-05-22T10:00:00Z", Decimal("225"))  # type: ignore[arg-type]


def test_on_mark_non_positive_mark_raises() -> None:
    scanner = ThresholdLadderEntryScanner(_above_config(), _meta())
    with pytest.raises(ValueError, match="mark must be > 0"):
        scanner.on_mark(_ts(), Decimal("0"))


def test_on_mark_coerces_float_mark_to_decimal() -> None:
    scanner = ThresholdLadderEntryScanner(_above_config(), _meta())
    # Use a mark between dca (210) and heavy (225) so only dca fires —
    # easier to assert single-signal coercion.
    sigs = scanner.on_mark(_ts(), 212.0)  # type: ignore[arg-type]
    assert len(sigs) == 1
    assert sigs[0].metadata["mark_usd"] == "212.0"


# ---------------------------------------------------------------------------
# direction=above (SPCX-style short)
# ---------------------------------------------------------------------------


def test_above_heavy_fires_on_edge_crossing() -> None:
    scanner = ThresholdLadderEntryScanner(_above_config(), _meta())

    # Well below both triggers → no emit.
    assert scanner.on_mark(_ts(hour=9), Decimal("200")) == []
    assert not scanner.heavy_fired

    # At / above heavy trigger → heavy signal emitted (dca also fires
    # since 225 > 210; we filter to heavy here).
    sigs = scanner.on_mark(_ts(hour=10), Decimal("225"))
    assert scanner.heavy_fired
    heavy = next(s for s in sigs if s.metadata["kind"] == "heavy")
    assert heavy.symbol == SYMBOL
    assert heavy.venue == "BINANCE"
    assert heavy.direction == "SHORT"
    assert heavy.strength == -1.0
    assert heavy.source == "spcxusdt.short_mcap.v1.heavy.v1"
    assert heavy.metadata["intent_side"] == "SELL"
    assert heavy.metadata["mark_usd"] == "225"


def test_above_heavy_does_not_re_emit_after_fallback() -> None:
    scanner = ThresholdLadderEntryScanner(_above_config(), _meta())

    # First crossing → heavy fires.
    first = scanner.on_mark(_ts(hour=10), Decimal("226"))
    assert any(s.metadata["kind"] == "heavy" for s in first)

    # Mark dips below trigger.
    assert scanner.on_mark(_ts(hour=11), Decimal("215")) == []

    # Mark crosses back above trigger → no second heavy.
    second = scanner.on_mark(_ts(hour=12), Decimal("228"))
    assert not any(s.metadata["kind"] == "heavy" for s in second)


def test_above_dca_dedups_same_utc_day() -> None:
    scanner = ThresholdLadderEntryScanner(_above_config(), _meta())

    # Mark is between dca (210) and heavy (225) triggers — only dca fires.
    morning = scanner.on_mark(_ts(hour=1), Decimal("212"))
    assert any(s.metadata["kind"] == "dca" for s in morning)
    assert not any(s.metadata["kind"] == "heavy" for s in morning)

    # Same UTC day, mark still above dca trigger → no re-emit.
    evening = scanner.on_mark(_ts(hour=22), Decimal("220"))
    assert evening == []


def test_above_dca_fires_on_new_utc_day() -> None:
    scanner = ThresholdLadderEntryScanner(_above_config(), _meta())

    day1 = scanner.on_mark(_ts(hour=1, day=22), Decimal("212"))
    day2 = scanner.on_mark(_ts(hour=1, day=23), Decimal("213"))

    dca1 = next(s for s in day1 if s.metadata["kind"] == "dca")
    dca2 = next(s for s in day2 if s.metadata["kind"] == "dca")
    assert dca1.source == "spcxusdt.short_mcap.v1.dca.20260522"
    assert dca2.source == "spcxusdt.short_mcap.v1.dca.20260523"
    assert scanner.dca_fired_dates() == {
        dt.date(2026, 5, 22),
        dt.date(2026, 5, 23),
    }


def test_above_heavy_and_dca_emit_together() -> None:
    # Mark spikes from below dca all the way above heavy in one tick →
    # both heavy and dca emit on the same call.
    scanner = ThresholdLadderEntryScanner(_above_config(), _meta())
    sigs = scanner.on_mark(_ts(hour=10), Decimal("230"))
    kinds = {s.metadata["kind"] for s in sigs}
    assert kinds == {"heavy", "dca"}


# ---------------------------------------------------------------------------
# direction=below (long mirror)
# ---------------------------------------------------------------------------


def test_below_heavy_fires_on_edge_crossing() -> None:
    scanner = ThresholdLadderEntryScanner(_below_config(), _meta())

    # Above trigger → no emit.
    assert scanner.on_mark(_ts(hour=9), Decimal("60")) == []

    # At / below trigger → one heavy signal.
    sigs = scanner.on_mark(_ts(hour=10), Decimal("50"))
    heavy = next(s for s in sigs if s.metadata["kind"] == "heavy")
    assert heavy.direction == "LONG"
    assert heavy.strength == 1.0
    assert heavy.metadata["intent_side"] == "BUY"
    assert heavy.source == "longinst.v1.heavy.v1"


def test_below_dca_dedups_same_utc_day_and_fires_on_new_day() -> None:
    scanner = ThresholdLadderEntryScanner(_below_config(), _meta())

    # Mark sits between heavy (50) and dca (55) → only dca fires.
    day1_morning = scanner.on_mark(_ts(hour=1, day=22), Decimal("54"))
    assert any(s.metadata["kind"] == "dca" for s in day1_morning)
    assert not any(s.metadata["kind"] == "heavy" for s in day1_morning)

    # Same day, no re-emit.
    assert scanner.on_mark(_ts(hour=22, day=22), Decimal("53")) == []

    # New UTC day → dca fires again with new fingerprint.
    day2 = scanner.on_mark(_ts(hour=1, day=23), Decimal("54"))
    dca2 = next(s for s in day2 if s.metadata["kind"] == "dca")
    assert dca2.source == "longinst.v1.dca.20260523"
    assert dca2.direction == "LONG"


def test_below_no_emit_when_mark_above_both_triggers() -> None:
    scanner = ThresholdLadderEntryScanner(_below_config(), _meta())
    # Mark well above dca trigger 55 → nothing fires.
    assert scanner.on_mark(_ts(hour=10), Decimal("80")) == []
    assert not scanner.heavy_fired


# ---------------------------------------------------------------------------
# Envelope shape contract
# ---------------------------------------------------------------------------


def test_signal_qty_metadata_is_quantized_to_qty_step() -> None:
    scanner = ThresholdLadderEntryScanner(_above_config(), _meta())
    sigs = scanner.on_mark(_ts(hour=10), Decimal("225"))
    heavy = next(s for s in sigs if s.metadata["kind"] == "heavy")

    # 150 / 225 = 0.6666...; qty_step=0.001 → quantize down to 0.666.
    assert heavy.metadata["qty"] == "0.666"

    # And the encoded qty is a clean multiple of qty_step.
    qty = Decimal(heavy.metadata["qty"])
    qty_step = _meta().qty_step
    assert (qty / qty_step) % 1 == 0


def test_signal_source_uses_fingerprint_prefix() -> None:
    scanner = ThresholdLadderEntryScanner(
        _above_config(fingerprint_prefix="custom.prefix.v2"), _meta()
    )
    sigs = scanner.on_mark(_ts(hour=10), Decimal("230"))
    sources = {s.source for s in sigs}
    assert "custom.prefix.v2.heavy.v1" in sources
    assert "custom.prefix.v2.dca.20260522" in sources


def test_signal_carries_intent_side_and_margin_metadata() -> None:
    scanner = ThresholdLadderEntryScanner(_above_config(), _meta())
    sigs = scanner.on_mark(_ts(hour=10), Decimal("230"))

    heavy = next(s for s in sigs if s.metadata["kind"] == "heavy")
    dca = next(s for s in sigs if s.metadata["kind"] == "dca")

    assert heavy.metadata["intent_side"] == "SELL"
    assert heavy.metadata["margin_usd"] == "150"
    assert heavy.metadata["limit_price_bps_offset"] == "5"

    assert dca.metadata["intent_side"] == "SELL"
    assert dca.metadata["margin_usd"] == "10"


def test_emit_produces_valid_signal_dataclass() -> None:
    # Belt-and-braces: emitted objects must satisfy Signal post-init
    # (already asserted by Signal itself, but pin the contract here).
    scanner = ThresholdLadderEntryScanner(_above_config(), _meta())
    sigs = scanner.on_mark(_ts(hour=10), Decimal("230"))
    for s in sigs:
        assert isinstance(s, Signal)
        assert s.generated_at.tzinfo is not None
        assert s.symbol == SYMBOL


# ---------------------------------------------------------------------------
# Lifetime introspection
# ---------------------------------------------------------------------------


def test_heavy_fired_and_dca_fired_dates_track_state() -> None:
    scanner = ThresholdLadderEntryScanner(_above_config(), _meta())
    assert scanner.heavy_fired is False
    assert scanner.dca_fired_dates() == frozenset()

    scanner.on_mark(_ts(hour=10, day=22), Decimal("230"))
    assert scanner.heavy_fired is True
    assert scanner.dca_fired_dates() == frozenset({dt.date(2026, 5, 22)})

    scanner.on_mark(_ts(hour=10, day=23), Decimal("220"))
    assert scanner.dca_fired_dates() == frozenset(
        {dt.date(2026, 5, 22), dt.date(2026, 5, 23)}
    )


def test_kind_class_attribute() -> None:
    assert ThresholdLadderEntryScanner.kind == "threshold_ladder_entry"
