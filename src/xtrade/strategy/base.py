"""SignalDrivenStrategy ABC + registry (Phase 3 Task 1 / T1).

A `SignalDrivenStrategy` is a pure-Python strategy plugin: it receives
`Signal` objects (from the Phase 2 `SignalQueue` via `SignalConsumer`)
plus an `AccountState` snapshot, and returns an iterable of
`OrderIntent`s. The Phase 3 runner is responsible for then funnelling
those intents through the risk and approval gates before they reach
either the paper-mode simulator or the live testnet client.

Why a registry (vs. direct imports)?
------------------------------------
The CLI accepts a strategy name (`--strategy momentum_follow`), and
operator-side scripts in `scripts/phase3/` should be able to list /
inspect available plugins without importing the runner. Mirroring the
Phase 2 scanner registry keeps the mental model consistent across the
two layers.
"""

from __future__ import annotations

import abc
from collections.abc import Iterable
from typing import Callable, TypeVar

from xtrade.research.signals import Signal
from xtrade.strategy.intent import Fill, OrderIntent


class StrategyRegistrationError(KeyError):
    """Raised on duplicate or unknown strategy registry keys."""


class SignalDrivenStrategy(abc.ABC):
    """Base class for pure-Python signal-driven strategies (Phase 3).

    Subclasses MUST override `on_signal`. The hooks `on_fill` and
    `on_reject` default to no-ops so subclasses opt in only when they
    actually need to react to execution feedback.

    The `name` class attribute MUST be set on every concrete subclass;
    `@register_strategy(name)` will assert this.
    """

    #: Human-readable registry key (e.g. ``"momentum_follow"``).
    name: str = ""

    def __init__(self, config: dict | None = None) -> None:
        # Concrete plugins may pull tunables out of `config`. The base
        # class itself is config-less; we keep the attribute for
        # introspection by the CLI's `xtrade strategy describe`.
        self.config: dict = dict(config or {})

    # ---- override points -------------------------------------------------

    @abc.abstractmethod
    def on_signal(
        self,
        signal: Signal,
        account: "AccountSnapshot",
    ) -> Iterable[OrderIntent]:
        """Translate one signal into zero or more order intents.

        The strategy SHOULD NOT call `submit_order` / construct
        Nautilus `Order` objects. The runner is the only path to the
        venue; the RiskGate import-graph lint enforces this (Task 2).
        """

    def on_fill(self, fill: Fill) -> None:
        """Hook called after a fill is confirmed. Default: no-op."""

    def on_reject(self, intent: OrderIntent, reason: str) -> None:
        """Hook called when the risk gate or venue rejects an intent.

        Default: no-op. Plugins that want to back off / kill-switch
        themselves can override.
        """

    def describe(self) -> dict:
        """Used by `xtrade strategy describe`. Plugins may override."""
        return {
            "name": self.name,
            "class": f"{type(self).__module__}.{type(self).__qualname__}",
            "config": dict(self.config),
            "doc": (type(self).__doc__ or "").strip().splitlines()[0] if (type(self).__doc__ or "").strip() else "",
        }


# ---------------------------------------------------------------------------
# Minimal account snapshot shared between paper / live paths.
# ---------------------------------------------------------------------------


import dataclasses
from decimal import Decimal


@dataclasses.dataclass(frozen=True)
class AccountSnapshot:
    """Read-only view of the account that `on_signal` may consult.

    Positions are keyed by Nautilus-style ``symbol.venue`` strings
    (e.g. ``"BTCUSDT-PERP.BINANCE"``) and signed (long > 0, short < 0)
    in instrument units. ``cash_usd`` is the free balance available
    for new exposure. ``peak_nav_usd`` is needed by the drawdown rule.

    The snapshot is rebuilt by the runner once per signal tick; the
    strategy MUST NOT mutate it (frozen dataclass + read-only mapping
    discipline at the call site).
    """

    cash_usd: Decimal
    positions: dict[str, Decimal]
    mark_prices: dict[str, Decimal]
    nav_usd: Decimal
    peak_nav_usd: Decimal

    def position_of(self, key: str) -> Decimal:
        return self.positions.get(key, Decimal(0))

    def mark_of(self, key: str) -> Decimal | None:
        return self.mark_prices.get(key)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


_STRATEGY_REGISTRY: dict[str, type[SignalDrivenStrategy]] = {}

_T = TypeVar("_T", bound=SignalDrivenStrategy)


def register_strategy(name: str) -> Callable[[type[_T]], type[_T]]:
    """Decorator registering a `SignalDrivenStrategy` subclass.

    Usage::

        @register_strategy("momentum_follow")
        class MomentumFollow(SignalDrivenStrategy):
            name = "momentum_follow"
            ...
    """

    def _decorate(cls: type[_T]) -> type[_T]:
        if not isinstance(name, str) or not name:
            raise StrategyRegistrationError(
                f"strategy name must be a non-empty string, got {name!r}"
            )
        if not issubclass(cls, SignalDrivenStrategy):
            raise StrategyRegistrationError(
                f"{cls.__name__} is not a SignalDrivenStrategy subclass"
            )
        if name in _STRATEGY_REGISTRY:
            existing = _STRATEGY_REGISTRY[name]
            if existing is cls:
                # Idempotent re-registration (e.g. module reimported in tests).
                return cls
            raise StrategyRegistrationError(
                f"strategy name {name!r} already registered to {existing.__name__}"
            )
        # Stamp the class attribute if subclass forgot to.
        if not cls.name:
            cls.name = name
        elif cls.name != name:
            raise StrategyRegistrationError(
                f"class.name={cls.name!r} mismatches registry name={name!r}"
            )
        _STRATEGY_REGISTRY[name] = cls
        return cls

    return _decorate


def available_strategies() -> list[str]:
    """Return registered strategy names in sorted order."""
    return sorted(_STRATEGY_REGISTRY)


def load_strategy(name: str, config: dict | None = None) -> SignalDrivenStrategy:
    """Instantiate a registered strategy by name."""
    cls = _STRATEGY_REGISTRY.get(name)
    if cls is None:
        raise StrategyRegistrationError(
            f"unknown strategy {name!r}; available: {available_strategies()}"
        )
    return cls(config=config)


def _reset_registry_for_tests() -> None:  # pragma: no cover - test helper
    """Clear the registry. Tests use this between cases to avoid leakage."""
    _STRATEGY_REGISTRY.clear()
