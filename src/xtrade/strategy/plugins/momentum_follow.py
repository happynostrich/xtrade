"""MomentumFollow — reference `SignalDrivenStrategy` plugin (Phase 3 Task 4 / T5).

Behaviour
---------
Translates Phase 2 momentum signals into order intents:

  LONG  : if currently short, close it (BUY reduce_only). Then if no
          existing long, open a fresh long (BUY).
  SHORT : if currently long, close it (SELL reduce_only). Then if no
          existing short, open a fresh short (SELL).
  FLAT  : close any open position on `signal.symbol`.

Sizing
------
The plugin reads `notional_usd` from its `config`. Per-intent quantity
is `notional_usd / mark_price`, rounded down to `qty_step` (also from
config; default `0.001`). If the account has no mark for the symbol
the plugin skips the signal — the RiskGate would block the resulting
intent anyway (no mark → no notional check).

Safety boundary
---------------
This plugin emits `OrderIntent` only. It does NOT touch
`SignalQueue`, Nautilus `Order`, or the venue API. The runner alone
funnels intents through risk + approval → execution.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable
from decimal import ROUND_DOWN, Decimal

from xtrade.research.signals import Signal
from xtrade.strategy.base import (
    AccountSnapshot,
    SignalDrivenStrategy,
    register_strategy,
)
from xtrade.strategy.intent import OrderIntent


_DEFAULT_NOTIONAL_USD = Decimal("100")
_DEFAULT_QTY_STEP = Decimal("0.001")


@register_strategy("momentum_follow")
class MomentumFollow(SignalDrivenStrategy):
    """Reference momentum-follow plugin (LONG/SHORT/FLAT → BUY/SELL/CLOSE)."""

    name = "momentum_follow"

    def __init__(self, config: dict | None = None) -> None:
        super().__init__(config)
        notional = self.config.get("notional_usd", _DEFAULT_NOTIONAL_USD)
        step = self.config.get("qty_step", _DEFAULT_QTY_STEP)
        self.notional_usd: Decimal = Decimal(str(notional))
        self.qty_step: Decimal = Decimal(str(step))
        if self.notional_usd <= 0:
            raise ValueError(
                f"notional_usd must be > 0, got {self.notional_usd}"
            )
        if self.qty_step <= 0:
            raise ValueError(
                f"qty_step must be > 0, got {self.qty_step}"
            )

    # ---- override -------------------------------------------------------

    def on_signal(
        self,
        signal: Signal,
        account: AccountSnapshot,
    ) -> Iterable[OrderIntent]:
        key = signal.symbol
        mark = account.mark_of(key)
        if mark is None or mark <= 0:
            return []
        position = account.position_of(key)
        ts = signal.generated_at

        if signal.direction == "FLAT":
            close = self._close_intent(signal, position, ts)
            return [close] if close is not None else []

        if signal.direction == "LONG":
            out: list[OrderIntent] = []
            if position < 0:
                # Close existing short with reduce_only BUY.
                qty = self._round_qty(position.copy_abs())
                if qty > 0:
                    out.append(self._make_intent(
                        signal, side="BUY", qty=qty, reduce_only=True, ts=ts,
                    ))
            if position <= 0:
                # Open fresh long sized to notional.
                qty = self._size_qty(mark)
                if qty > 0:
                    out.append(self._make_intent(
                        signal, side="BUY", qty=qty, reduce_only=False, ts=ts,
                    ))
            return out

        if signal.direction == "SHORT":
            out = []
            if position > 0:
                qty = self._round_qty(position.copy_abs())
                if qty > 0:
                    out.append(self._make_intent(
                        signal, side="SELL", qty=qty, reduce_only=True, ts=ts,
                    ))
            if position >= 0:
                qty = self._size_qty(mark)
                if qty > 0:
                    out.append(self._make_intent(
                        signal, side="SELL", qty=qty, reduce_only=False, ts=ts,
                    ))
            return out

        return []

    # ---- helpers --------------------------------------------------------

    def _close_intent(
        self,
        signal: Signal,
        position: Decimal,
        ts: dt.datetime,
    ) -> OrderIntent | None:
        if position == 0:
            return None
        qty = self._round_qty(position.copy_abs())
        if qty <= 0:
            return None
        side = "SELL" if position > 0 else "BUY"
        return self._make_intent(
            signal, side=side, qty=qty, reduce_only=True, ts=ts,
        )

    def _size_qty(self, mark: Decimal) -> Decimal:
        raw = self.notional_usd / mark
        return self._round_qty(raw)

    def _round_qty(self, qty: Decimal) -> Decimal:
        if qty <= 0:
            return Decimal(0)
        # Floor to qty_step grid.
        steps = (qty / self.qty_step).to_integral_value(rounding=ROUND_DOWN)
        return steps * self.qty_step

    def _make_intent(
        self,
        signal: Signal,
        *,
        side: str,
        qty: Decimal,
        reduce_only: bool,
        ts: dt.datetime,
    ) -> OrderIntent:
        return OrderIntent(
            venue=signal.venue,
            symbol=signal.symbol,
            side=side,  # type: ignore[arg-type]
            order_type="MARKET",
            quantity=qty,
            limit_price=None,
            reduce_only=reduce_only,
            time_in_force="IOC",
            source_signal_id="|".join([
                signal.generated_at.isoformat(),
                signal.symbol,
                signal.source,
            ]),
            created_at=ts,
            metadata={
                "strategy": self.name,
                "direction": signal.direction,
                "strength": signal.strength,
            },
        )
