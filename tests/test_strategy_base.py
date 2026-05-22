"""Tests for `xtrade.strategy.base` (Phase 3 Task 1 / T1)."""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable
from decimal import Decimal

import pytest

from xtrade.research.signals import Signal
from xtrade.strategy.base import (
    AccountSnapshot,
    SignalDrivenStrategy,
    StrategyRegistrationError,
    _STRATEGY_REGISTRY,
    available_strategies,
    load_strategy,
    register_strategy,
)
from xtrade.strategy.intent import OrderIntent


UTC = dt.timezone.utc


def _signal(symbol: str = "BTCUSDT-PERP.BINANCE", direction: str = "LONG") -> Signal:
    return Signal(
        symbol=symbol,
        venue="binance",
        direction=direction,  # type: ignore[arg-type]
        strength=0.5 if direction == "LONG" else (-0.5 if direction == "SHORT" else 0.0),
        generated_at=dt.datetime(2026, 5, 22, 10, 0, 0, tzinfo=UTC),
        source="test:abc12345",
    )


def _account() -> AccountSnapshot:
    return AccountSnapshot(
        cash_usd=Decimal("10000"),
        positions={},
        mark_prices={"BTCUSDT-PERP.BINANCE": Decimal("50000")},
        nav_usd=Decimal("10000"),
        peak_nav_usd=Decimal("10000"),
    )


# ---- fixtures: scrub the registry between cases --------------------------


@pytest.fixture(autouse=True)
def _clean_registry():
    snapshot = dict(_STRATEGY_REGISTRY)
    _STRATEGY_REGISTRY.clear()
    yield
    _STRATEGY_REGISTRY.clear()
    _STRATEGY_REGISTRY.update(snapshot)


# ---- registry semantics --------------------------------------------------


def test_register_and_load_roundtrip() -> None:
    @register_strategy("noop")
    class _Noop(SignalDrivenStrategy):
        def on_signal(self, signal, account):  # noqa: ANN001
            return []

    assert "noop" in available_strategies()
    inst = load_strategy("noop")
    assert isinstance(inst, _Noop)


def test_register_rejects_non_subclass() -> None:
    with pytest.raises(StrategyRegistrationError):
        @register_strategy("bad")
        class _NotStrategy:  # type: ignore[misc]
            pass


def test_register_rejects_empty_name() -> None:
    with pytest.raises(StrategyRegistrationError):
        @register_strategy("")  # type: ignore[arg-type]
        class _A(SignalDrivenStrategy):
            def on_signal(self, signal, account):  # noqa: ANN001
                return []


def test_register_rejects_duplicate_name() -> None:
    @register_strategy("alpha")
    class _A(SignalDrivenStrategy):
        def on_signal(self, signal, account):  # noqa: ANN001
            return []

    with pytest.raises(StrategyRegistrationError):
        @register_strategy("alpha")
        class _B(SignalDrivenStrategy):
            def on_signal(self, signal, account):  # noqa: ANN001
                return []


def test_re_registering_same_class_is_idempotent() -> None:
    @register_strategy("idem")
    class _C(SignalDrivenStrategy):
        def on_signal(self, signal, account):  # noqa: ANN001
            return []

    # Re-applying the same decorator to the same class object should
    # not raise.
    register_strategy("idem")(_C)
    assert available_strategies() == ["idem"]


def test_register_rejects_mismatched_class_name_attribute() -> None:
    with pytest.raises(StrategyRegistrationError):
        @register_strategy("good")
        class _MismatchedClass(SignalDrivenStrategy):
            name = "bad"

            def on_signal(self, signal, account):  # noqa: ANN001
                return []


def test_register_stamps_class_name_when_unset() -> None:
    @register_strategy("alpha2")
    class _AlphaTwo(SignalDrivenStrategy):
        def on_signal(self, signal, account):  # noqa: ANN001
            return []

    assert _AlphaTwo.name == "alpha2"


def test_load_strategy_unknown_name_raises() -> None:
    with pytest.raises(StrategyRegistrationError):
        load_strategy("does-not-exist")


def test_available_strategies_is_sorted() -> None:
    @register_strategy("zeta")
    class _Z(SignalDrivenStrategy):
        def on_signal(self, signal, account):  # noqa: ANN001
            return []

    @register_strategy("alpha")
    class _A(SignalDrivenStrategy):
        def on_signal(self, signal, account):  # noqa: ANN001
            return []

    assert available_strategies() == ["alpha", "zeta"]


# ---- abstract methods ---------------------------------------------------


def test_cannot_instantiate_abstract_base() -> None:
    with pytest.raises(TypeError):
        SignalDrivenStrategy()  # type: ignore[abstract]


def test_subclass_must_override_on_signal() -> None:
    class _Incomplete(SignalDrivenStrategy):  # no override
        pass

    with pytest.raises(TypeError):
        _Incomplete()


# ---- on_signal output contract -------------------------------------------


def test_strategy_can_emit_intents() -> None:
    @register_strategy("emit")
    class _Emit(SignalDrivenStrategy):
        def on_signal(self, signal, account) -> Iterable[OrderIntent]:  # noqa: ANN001
            return [
                OrderIntent(
                    venue=signal.venue,
                    symbol=signal.symbol,
                    side="BUY",
                    order_type="MARKET",
                    quantity=Decimal("0.001"),
                    limit_price=None,
                    reduce_only=False,
                    time_in_force="IOC",
                    source_signal_id=signal.source,
                    created_at=signal.generated_at,
                )
            ]

    strat = load_strategy("emit")
    intents = list(strat.on_signal(_signal(), _account()))
    assert len(intents) == 1
    assert intents[0].symbol == "BTCUSDT-PERP.BINANCE"


def test_default_on_fill_and_on_reject_are_noops() -> None:
    @register_strategy("hooks")
    class _Hooks(SignalDrivenStrategy):
        def on_signal(self, signal, account):  # noqa: ANN001
            return []

    strat = load_strategy("hooks")
    # Should not raise.
    from xtrade.strategy.intent import Fill

    dummy_fill = Fill(
        venue="binance",
        symbol="BTCUSDT-PERP.BINANCE",
        side="BUY",
        quantity=Decimal("0.001"),
        price=Decimal("50000"),
        ts_event=dt.datetime(2026, 5, 22, 10, 0, 0, tzinfo=UTC),
        intent_fingerprint="abc12345abcd1234",
    )
    assert strat.on_fill(dummy_fill) is None
    dummy_intent = OrderIntent(
        venue="binance",
        symbol="BTCUSDT-PERP.BINANCE",
        side="BUY",
        order_type="MARKET",
        quantity=Decimal("0.001"),
        limit_price=None,
        reduce_only=False,
        time_in_force="IOC",
        source_signal_id="",
        created_at=dt.datetime(2026, 5, 22, 10, 0, 0, tzinfo=UTC),
    )
    assert strat.on_reject(dummy_intent, "test reason") is None


def test_describe_returns_basic_metadata() -> None:
    @register_strategy("describable")
    class _Desc(SignalDrivenStrategy):
        """One-liner doc for describe()."""

        def on_signal(self, signal, account):  # noqa: ANN001
            return []

    strat = load_strategy("describable", config={"k": 1})
    info = strat.describe()
    assert info["name"] == "describable"
    assert info["config"] == {"k": 1}
    assert "One-liner" in info["doc"]


# ---- AccountSnapshot ----------------------------------------------------


def test_account_snapshot_helpers() -> None:
    acc = AccountSnapshot(
        cash_usd=Decimal("100"),
        positions={"BTCUSDT-PERP.BINANCE": Decimal("0.5")},
        mark_prices={"BTCUSDT-PERP.BINANCE": Decimal("50000")},
        nav_usd=Decimal("25100"),
        peak_nav_usd=Decimal("30000"),
    )
    assert acc.position_of("BTCUSDT-PERP.BINANCE") == Decimal("0.5")
    assert acc.position_of("ETHUSDT-PERP.BINANCE") == Decimal(0)
    assert acc.mark_of("BTCUSDT-PERP.BINANCE") == Decimal("50000")
    assert acc.mark_of("missing") is None


def test_account_snapshot_is_frozen() -> None:
    acc = _account()
    with pytest.raises(dataclasses_FrozenInstanceError := __import__("dataclasses").FrozenInstanceError):
        acc.cash_usd = Decimal("0")  # type: ignore[misc]
