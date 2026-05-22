"""Tests for `xtrade.strategy.plugins.momentum_follow` (Phase 3 Task 4 / T5)."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from xtrade.research.signals import Signal
from xtrade.strategy import load_strategy
from xtrade.strategy.base import AccountSnapshot
from xtrade.strategy.plugins.momentum_follow import MomentumFollow


UTC = dt.timezone.utc
SYMBOL = "BTCUSDT-PERP.BINANCE"


def _signal(direction: str = "LONG", h: int = 10) -> Signal:
    return Signal(
        symbol=SYMBOL,
        venue="binance",
        direction=direction,  # type: ignore[arg-type]
        strength=0.5 if direction == "LONG" else (-0.5 if direction == "SHORT" else 0.0),
        generated_at=dt.datetime(2026, 5, 22, h, 0, 0, tzinfo=UTC),
        source="momentum:abc12345",
    )


def _account(
    *,
    position: str = "0",
    mark: str | None = "50000",
) -> AccountSnapshot:
    return AccountSnapshot(
        cash_usd=Decimal("100000"),
        positions={SYMBOL: Decimal(position)},
        mark_prices={SYMBOL: Decimal(mark)} if mark else {},
        nav_usd=Decimal("100000"),
        peak_nav_usd=Decimal("100000"),
    )


# ---- registry -----------------------------------------------------------


def test_momentum_follow_is_registered() -> None:
    strat = load_strategy("momentum_follow")
    assert isinstance(strat, MomentumFollow)


def test_default_config() -> None:
    strat = MomentumFollow()
    assert strat.notional_usd == Decimal("100")
    assert strat.qty_step == Decimal("0.001")


def test_init_rejects_non_positive_notional() -> None:
    with pytest.raises(ValueError):
        MomentumFollow({"notional_usd": "0"})
    with pytest.raises(ValueError):
        MomentumFollow({"notional_usd": "-1"})


def test_init_rejects_non_positive_qty_step() -> None:
    with pytest.raises(ValueError):
        MomentumFollow({"qty_step": "0"})


# ---- LONG signal --------------------------------------------------------


def test_long_from_flat_opens_buy(tmp_path) -> None:
    strat = MomentumFollow({"notional_usd": "500", "qty_step": "0.001"})
    intents = list(strat.on_signal(_signal("LONG"), _account(position="0")))
    assert len(intents) == 1
    o = intents[0]
    assert o.side == "BUY"
    assert not o.reduce_only
    # 500 / 50000 = 0.01
    assert o.quantity == Decimal("0.01")
    assert o.symbol == SYMBOL
    assert o.order_type == "MARKET"


def test_long_from_short_closes_then_opens() -> None:
    strat = MomentumFollow({"notional_usd": "500"})
    intents = list(strat.on_signal(_signal("LONG"), _account(position="-0.02")))
    assert len(intents) == 2
    close, opener = intents
    assert close.side == "BUY"
    assert close.reduce_only
    assert close.quantity == Decimal("0.02")
    assert opener.side == "BUY"
    assert not opener.reduce_only
    assert opener.quantity == Decimal("0.01")


def test_long_when_already_long_emits_nothing() -> None:
    strat = MomentumFollow({"notional_usd": "500"})
    intents = list(strat.on_signal(_signal("LONG"), _account(position="0.05")))
    assert intents == []


# ---- SHORT signal -------------------------------------------------------


def test_short_from_flat_opens_sell() -> None:
    strat = MomentumFollow({"notional_usd": "500"})
    intents = list(strat.on_signal(_signal("SHORT"), _account(position="0")))
    assert len(intents) == 1
    o = intents[0]
    assert o.side == "SELL"
    assert not o.reduce_only


def test_short_from_long_closes_then_opens() -> None:
    strat = MomentumFollow({"notional_usd": "500"})
    intents = list(strat.on_signal(_signal("SHORT"), _account(position="0.03")))
    assert len(intents) == 2
    close, opener = intents
    assert close.side == "SELL"
    assert close.reduce_only
    assert close.quantity == Decimal("0.03")
    assert opener.side == "SELL"
    assert not opener.reduce_only


def test_short_when_already_short_emits_nothing() -> None:
    strat = MomentumFollow({"notional_usd": "500"})
    intents = list(strat.on_signal(_signal("SHORT"), _account(position="-0.05")))
    assert intents == []


# ---- FLAT signal --------------------------------------------------------


def test_flat_closes_long() -> None:
    strat = MomentumFollow()
    intents = list(strat.on_signal(_signal("FLAT"), _account(position="0.04")))
    assert len(intents) == 1
    o = intents[0]
    assert o.side == "SELL"
    assert o.reduce_only
    assert o.quantity == Decimal("0.04")


def test_flat_closes_short() -> None:
    strat = MomentumFollow()
    intents = list(strat.on_signal(_signal("FLAT"), _account(position="-0.04")))
    assert len(intents) == 1
    o = intents[0]
    assert o.side == "BUY"
    assert o.reduce_only


def test_flat_when_zero_emits_nothing() -> None:
    strat = MomentumFollow()
    intents = list(strat.on_signal(_signal("FLAT"), _account(position="0")))
    assert intents == []


# ---- missing mark / safety guards ---------------------------------------


def test_no_mark_skips_signal() -> None:
    strat = MomentumFollow()
    intents = list(strat.on_signal(_signal("LONG"), _account(position="0", mark=None)))
    assert intents == []


def test_sub_step_notional_emits_nothing() -> None:
    # 5 USD / 50000 = 0.0001; rounded to 0.001 step → 0.
    strat = MomentumFollow({"notional_usd": "5", "qty_step": "0.001"})
    intents = list(strat.on_signal(_signal("LONG"), _account()))
    assert intents == []


def test_intent_metadata_carries_strategy_name() -> None:
    strat = MomentumFollow({"notional_usd": "500"})
    intents = list(strat.on_signal(_signal("LONG"), _account()))
    assert intents[0].metadata["strategy"] == "momentum_follow"
    assert intents[0].metadata["direction"] == "LONG"


def test_intent_source_signal_id_matches_signal_key() -> None:
    sig = _signal("LONG")
    strat = MomentumFollow({"notional_usd": "500"})
    intents = list(strat.on_signal(sig, _account()))
    assert intents[0].source_signal_id == "|".join(
        [sig.generated_at.isoformat(), sig.symbol, sig.source]
    )


def test_qty_step_floor_rounding() -> None:
    # 500 / 33333 ~= 0.015000150..., step 0.001 → 0.015
    strat = MomentumFollow({"notional_usd": "500", "qty_step": "0.001"})
    acc = AccountSnapshot(
        cash_usd=Decimal("100000"),
        positions={SYMBOL: Decimal("0")},
        mark_prices={SYMBOL: Decimal("33333")},
        nav_usd=Decimal("100000"),
        peak_nav_usd=Decimal("100000"),
    )
    intents = list(strat.on_signal(_signal("LONG"), acc))
    assert intents[0].quantity == Decimal("0.015")
