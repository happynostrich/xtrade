"""Tests for `xtrade.strategy.plugins.mcap_anchored_ladder` (Phase 6 T5).

Brief §T5 acceptance matrix:

  short instance
    - rejects LONG signal
    - heavy fires once (per-process)
    - dca dedups within a UTC day, fires once per new day
    - heavy fill → emits descending-mcap TP ladder
    - TP fill → cancel-and-replace remaining rungs (rebalanced)
    - soft_kill → cancel all open TPs, do not enter; future signals no-op
    - leverage out-of-range raises in ctor (LeverageExceedsMcapCeilingError)

  long instance (mirror)
    - rejects SHORT signal
    - heavy fill → emits ascending-mcap TP ladder

  boundary
    - direction missing / invalid → ctor raises ValueError
    - tp_ladder non-monotonic → ctor raises ValueError
    - fractions not summing to 1 → ctor raises ValueError
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from xtrade.instruments.meta import InstrumentMeta
from xtrade.live.sizing import LeverageExceedsMcapCeilingError
from xtrade.research.signals import Signal
from xtrade.strategy import load_strategy
from xtrade.strategy.base import AccountSnapshot
from xtrade.strategy.intent import Fill
from xtrade.strategy.plugins.mcap_anchored_ladder import (
    McapAnchoredLadderStrategy,
)


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


def _short_tp_ladder() -> list[dict[str, str]]:
    # 4 equal rungs, descending mcap (short profits as mcap falls).
    return [
        {"mcap_usd": "2000000000000", "fraction": "0.25"},
        {"mcap_usd": "1600000000000", "fraction": "0.25"},
        {"mcap_usd": "1200000000000", "fraction": "0.25"},
        {"mcap_usd": "800000000000", "fraction": "0.25"},
    ]


def _long_tp_ladder() -> list[dict[str, str]]:
    # Mirror: 4 equal rungs, ascending mcap (long profits as mcap rises).
    return [
        {"mcap_usd": "800000000000", "fraction": "0.25"},
        {"mcap_usd": "1200000000000", "fraction": "0.25"},
        {"mcap_usd": "1600000000000", "fraction": "0.25"},
        {"mcap_usd": "2000000000000", "fraction": "0.25"},
    ]


def _short_config(**overrides) -> dict:
    cfg: dict = {
        "direction": "short",
        "instrument": SYMBOL,
        "venue": "BINANCE",
        "meta": _meta(),
        "leverage": "1",
        "mmr": "0.025",
        "target_mcap_liq_usd": "4000000000000",
        "heavy_entry": {"trigger_mark_usd": "225", "qty": "0.666"},
        "dca_entry": {"trigger_mark_usd": "210", "qty": "0.047"},
        "tp_ladder": _short_tp_ladder(),
        "soft_kill": {
            "boundary": "above",
            "trigger_mcap_usd": "3500000000000",
        },
        "fingerprint_prefix": "spcxusdt.short_mcap.v1",
    }
    cfg.update(overrides)
    return cfg


def _long_config(**overrides) -> dict:
    cfg: dict = {
        "direction": "long",
        "instrument": SYMBOL,
        "venue": "BINANCE",
        "meta": _meta(),
        "leverage": "1",
        "mmr": "0.025",
        # For a long instance, target_mcap_liq is BELOW entry. Use $50B
        # (≈ $4.21 price) so that L_max at heavy_entry=$50 is plenty wide.
        "target_mcap_liq_usd": "50000000000",
        "heavy_entry": {"trigger_mark_usd": "50", "qty": "1.000"},
        "dca_entry": {"trigger_mark_usd": "55", "qty": "0.100"},
        "tp_ladder": _long_tp_ladder(),
        "soft_kill": {
            "boundary": "below",
            "trigger_mcap_usd": "30000000000",
        },
        "fingerprint_prefix": "longinst.v1",
    }
    cfg.update(overrides)
    return cfg


def _signal(
    *,
    kind: str,
    direction: str = "SHORT",
    at: dt.datetime | None = None,
) -> Signal:
    when = at or dt.datetime(2026, 5, 22, 10, 0, 0, tzinfo=UTC)
    return Signal(
        symbol=SYMBOL,
        venue="binance",
        direction=direction,  # type: ignore[arg-type]
        strength=-0.5 if direction == "SHORT" else 0.5,
        generated_at=when,
        source="threshold_ladder_entry:test",
        metadata={"kind": kind},
    )


def _account() -> AccountSnapshot:
    return AccountSnapshot(
        cash_usd=Decimal("1000"),
        positions={SYMBOL: Decimal(0)},
        mark_prices={SYMBOL: Decimal("220")},
        nav_usd=Decimal("1000"),
        peak_nav_usd=Decimal("1000"),
    )


def _fill_from_intent(intent, *, price: str | Decimal | None = None) -> Fill:
    return Fill(
        venue=intent.venue,
        symbol=intent.symbol,
        side=intent.side,
        quantity=intent.quantity,
        price=Decimal(str(price)) if price is not None else intent.limit_price,
        ts_event=intent.created_at,
        intent_fingerprint=intent.fingerprint(),
        metadata=dict(intent.metadata),
    )


# ---------------------------------------------------------------------------
# Registry + ctor boundary
# ---------------------------------------------------------------------------


def test_plugin_is_registered() -> None:
    strat = load_strategy("mcap_anchored_ladder", config=_short_config())
    assert isinstance(strat, McapAnchoredLadderStrategy)
    assert strat.direction == "short"


def test_direction_missing_raises() -> None:
    cfg = _short_config()
    del cfg["direction"]
    with pytest.raises(ValueError, match="direction"):
        McapAnchoredLadderStrategy(cfg)


def test_direction_invalid_raises() -> None:
    with pytest.raises(ValueError, match="direction"):
        McapAnchoredLadderStrategy(_short_config(direction="sideways"))


def test_meta_missing_raises() -> None:
    cfg = _short_config()
    cfg["meta"] = None
    with pytest.raises(ValueError, match="meta"):
        McapAnchoredLadderStrategy(cfg)


def test_leverage_exceeds_ceiling_raises() -> None:
    # At entry=$225 with mmr=0.025 and $4T target, max leverage ≈ 1.94×.
    # 10× must raise.
    with pytest.raises(LeverageExceedsMcapCeilingError):
        McapAnchoredLadderStrategy(_short_config(leverage="10"))


def test_tp_ladder_non_monotonic_short_raises() -> None:
    bad = [
        {"mcap_usd": "1000000000000", "fraction": "0.5"},
        {"mcap_usd": "2000000000000", "fraction": "0.5"},  # ascending → invalid for short
    ]
    with pytest.raises(ValueError, match="strictly decrease"):
        McapAnchoredLadderStrategy(_short_config(tp_ladder=bad))


def test_tp_ladder_non_monotonic_long_raises() -> None:
    bad = [
        {"mcap_usd": "2000000000000", "fraction": "0.5"},
        {"mcap_usd": "1000000000000", "fraction": "0.5"},  # descending → invalid for long
    ]
    with pytest.raises(ValueError, match="strictly increase"):
        McapAnchoredLadderStrategy(_long_config(tp_ladder=bad))


def test_tp_fractions_must_sum_to_one_raises() -> None:
    bad = [
        {"mcap_usd": "2000000000000", "fraction": "0.4"},
        {"mcap_usd": "1500000000000", "fraction": "0.4"},
    ]
    with pytest.raises(ValueError, match="sum to exactly 1"):
        McapAnchoredLadderStrategy(_short_config(tp_ladder=bad))


# ---------------------------------------------------------------------------
# Direction guard
# ---------------------------------------------------------------------------


def test_short_rejects_long_signal() -> None:
    strat = McapAnchoredLadderStrategy(_short_config())
    intents = list(
        strat.on_signal(_signal(kind="heavy", direction="LONG"), _account())
    )
    assert intents == []
    assert not strat.heavy_fired  # not consumed


def test_long_rejects_short_signal() -> None:
    strat = McapAnchoredLadderStrategy(_long_config())
    intents = list(
        strat.on_signal(_signal(kind="heavy", direction="SHORT"), _account())
    )
    assert intents == []
    assert not strat.heavy_fired


# ---------------------------------------------------------------------------
# Heavy + DCA dispatch
# ---------------------------------------------------------------------------


def test_heavy_emits_sell_limit_for_short_direction() -> None:
    strat = McapAnchoredLadderStrategy(_short_config())
    intents = list(strat.on_signal(_signal(kind="heavy"), _account()))
    assert len(intents) == 1
    o = intents[0]
    assert o.side == "SELL"
    assert o.order_type == "LIMIT"
    assert o.limit_price == Decimal("225")
    assert o.quantity == Decimal("0.666")
    assert o.reduce_only is False
    assert o.metadata["intent_kind"] == "heavy"
    assert o.metadata["direction"] == "short"
    assert strat.heavy_fired


def test_heavy_emits_buy_limit_for_long_direction() -> None:
    strat = McapAnchoredLadderStrategy(_long_config())
    intents = list(
        strat.on_signal(_signal(kind="heavy", direction="LONG"), _account())
    )
    assert len(intents) == 1
    o = intents[0]
    assert o.side == "BUY"
    assert o.reduce_only is False
    assert o.metadata["direction"] == "long"


def test_heavy_fires_only_once() -> None:
    strat = McapAnchoredLadderStrategy(_short_config())
    # First call → fires.
    intents = list(strat.on_signal(_signal(kind="heavy"), _account()))
    assert len(intents) == 1
    # Second call → idempotent no-op.
    intents = list(strat.on_signal(_signal(kind="heavy"), _account()))
    assert intents == []


def test_dca_dedupes_same_utc_day() -> None:
    strat = McapAnchoredLadderStrategy(_short_config())
    day1_morning = dt.datetime(2026, 5, 22, 1, 0, 0, tzinfo=UTC)
    day1_evening = dt.datetime(2026, 5, 22, 22, 0, 0, tzinfo=UTC)
    a = list(strat.on_signal(_signal(kind="dca", at=day1_morning), _account()))
    b = list(strat.on_signal(_signal(kind="dca", at=day1_evening), _account()))
    assert len(a) == 1
    assert b == []


def test_dca_fires_on_new_utc_day() -> None:
    strat = McapAnchoredLadderStrategy(_short_config())
    day1 = dt.datetime(2026, 5, 22, 10, 0, 0, tzinfo=UTC)
    day2 = dt.datetime(2026, 5, 23, 10, 0, 0, tzinfo=UTC)
    a = list(strat.on_signal(_signal(kind="dca", at=day1), _account()))
    b = list(strat.on_signal(_signal(kind="dca", at=day2), _account()))
    assert len(a) == 1
    assert len(b) == 1
    # Distinct fingerprints (different daily suffix).
    assert a[0].source_signal_id != b[0].source_signal_id


def test_unknown_kind_ignored() -> None:
    strat = McapAnchoredLadderStrategy(_short_config())
    s = Signal(
        symbol=SYMBOL,
        venue="binance",
        direction="SHORT",
        strength=-0.5,
        generated_at=dt.datetime(2026, 5, 22, tzinfo=UTC),
        source="weird",
        metadata={"kind": "mystery"},
    )
    assert list(strat.on_signal(s, _account())) == []


# ---------------------------------------------------------------------------
# TP ladder emission after entry fill
# ---------------------------------------------------------------------------


def test_short_heavy_fill_emits_descending_tp_ladder() -> None:
    strat = McapAnchoredLadderStrategy(_short_config())
    heavy_intents = list(strat.on_signal(_signal(kind="heavy"), _account()))
    fill = _fill_from_intent(heavy_intents[0])
    strat.on_fill(fill)

    tps = strat.drain_pending_orders()
    cancels = strat.drain_pending_cancels()
    assert cancels == []  # no open TPs before initial laddering

    assert len(tps) == 4
    # short closes by BUYING reduce-only.
    assert all(o.side == "BUY" for o in tps)
    assert all(o.reduce_only for o in tps)
    assert all(o.metadata["intent_kind"] == "tp" for o in tps)
    # Strictly descending limit_price (since mcap_usd descends).
    prices = [o.limit_price for o in tps]
    assert prices == sorted(prices, reverse=True)
    # Each rung qty = quantize_down(0.666 * 0.25, 0.001) = 0.166.
    for o in tps:
        assert o.quantity == Decimal("0.166")
    # Total signed position is -0.666 (short).
    assert strat.position_qty == Decimal("-0.666")


def test_long_heavy_fill_emits_ascending_tp_ladder() -> None:
    strat = McapAnchoredLadderStrategy(_long_config())
    heavy_intents = list(
        strat.on_signal(_signal(kind="heavy", direction="LONG"), _account())
    )
    fill = _fill_from_intent(heavy_intents[0])
    strat.on_fill(fill)

    tps = strat.drain_pending_orders()
    assert len(tps) == 4
    # long closes by SELLING reduce-only.
    assert all(o.side == "SELL" for o in tps)
    assert all(o.reduce_only for o in tps)
    prices = [o.limit_price for o in tps]
    assert prices == sorted(prices)
    assert strat.position_qty == Decimal("1.000")


def test_tp_fill_rebalances_remaining_rungs() -> None:
    strat = McapAnchoredLadderStrategy(_short_config())
    heavy_intents = list(strat.on_signal(_signal(kind="heavy"), _account()))
    strat.on_fill(_fill_from_intent(heavy_intents[0]))

    tp_intents = strat.drain_pending_orders()
    strat.drain_pending_cancels()
    assert strat.open_tp_count == 4

    # Top rung (level 0, highest mcap → highest price for short) fills.
    top = tp_intents[0]
    assert top.metadata["tp_level"] == 0
    strat.on_fill(_fill_from_intent(top))

    cancels = strat.drain_pending_cancels()
    new_tps = strat.drain_pending_orders()

    # The 3 remaining open TPs were cancelled; new 3 emitted in their place.
    assert len(cancels) == 3
    assert len(new_tps) == 3
    # Level 0 is permanently retired.
    assert all(o.metadata["tp_level"] in (1, 2, 3) for o in new_tps)
    # Position is now -0.666 + 0.166 = -0.500.
    assert strat.position_qty == Decimal("-0.500")
    # Quantities are quantize_down(0.500 * 0.25 / 0.75 , 0.001) ≈ 0.166.
    for o in new_tps:
        assert o.quantity == Decimal("0.166")
    # New fingerprints carry .r1 (second rebalance generation).
    assert all(".r1" in o.source_signal_id for o in new_tps)


def test_dca_fill_rebalances_against_grown_position() -> None:
    strat = McapAnchoredLadderStrategy(_short_config())
    # Heavy first, then DCA.
    heavy = list(strat.on_signal(_signal(kind="heavy"), _account()))[0]
    strat.on_fill(_fill_from_intent(heavy))
    initial_tps = strat.drain_pending_orders()
    strat.drain_pending_cancels()
    assert {o.quantity for o in initial_tps} == {Decimal("0.166")}

    dca = list(strat.on_signal(_signal(kind="dca"), _account()))[0]
    strat.on_fill(_fill_from_intent(dca))

    cancels = strat.drain_pending_cancels()
    new_tps = strat.drain_pending_orders()
    # All 4 initial TPs cancelled, 4 new ones at the larger position.
    assert len(cancels) == 4
    assert len(new_tps) == 4
    # Position: -(0.666 + 0.047) = -0.713 → 0.713/4 = 0.17825 → quantize → 0.178.
    assert strat.position_qty == Decimal("-0.713")
    for o in new_tps:
        assert o.quantity == Decimal("0.178")


# ---------------------------------------------------------------------------
# Soft kill
# ---------------------------------------------------------------------------


def test_soft_kill_cancels_open_tps_and_blocks_future_entries() -> None:
    strat = McapAnchoredLadderStrategy(_short_config())
    # Build a position + TP ladder first.
    heavy = list(strat.on_signal(_signal(kind="heavy"), _account()))[0]
    strat.on_fill(_fill_from_intent(heavy))
    strat.drain_pending_orders()  # consume initial TP emit
    strat.drain_pending_cancels()
    assert strat.open_tp_count == 4

    # soft_kill signal arrives.
    sk_signal = Signal(
        symbol=SYMBOL,
        venue="binance",
        direction="SHORT",
        strength=-0.5,
        generated_at=dt.datetime(2026, 5, 22, 12, 0, 0, tzinfo=UTC),
        source="mcap_softkill:test",
        metadata={"kind": "soft_kill"},
    )
    intents = list(strat.on_signal(sk_signal, _account()))
    assert intents == []  # never emits new exposure
    assert strat.soft_killed

    cancels = strat.drain_pending_cancels()
    # All 4 TPs queued for cancellation.
    assert len(cancels) == 4
    assert strat.open_tp_count == 0

    # Further heavy / dca signals are no-ops.
    further = list(strat.on_signal(_signal(kind="dca"), _account()))
    assert further == []
    further = list(strat.on_signal(_signal(kind="heavy"), _account()))
    assert further == []


def test_soft_kill_idempotent_when_no_open_tps() -> None:
    strat = McapAnchoredLadderStrategy(_short_config())
    sk_signal = Signal(
        symbol=SYMBOL,
        venue="binance",
        direction="SHORT",
        strength=-0.5,
        generated_at=dt.datetime(2026, 5, 22, tzinfo=UTC),
        source="mcap_softkill:test",
        metadata={"kind": "soft_kill"},
    )
    assert list(strat.on_signal(sk_signal, _account())) == []
    assert strat.soft_killed
    assert strat.drain_pending_cancels() == []
