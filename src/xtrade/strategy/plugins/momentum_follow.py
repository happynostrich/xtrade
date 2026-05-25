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

Optional ML gate (Phase 5 / B4)
-------------------------------
The plugin accepts an `ml_gate` config block. When `enabled=True`,
the trained baseline at `model_path` is loaded once at construction
and consulted after each rule-side intent. Intents whose ML score is
below `score_threshold` (or whose ML direction disagrees, when
`direction_check=True`) are SUPPRESSED — the strategy still emits a
structured `strategy.ml_gate.suppressed` event so the paper / live
audit trail can show why an intent didn't fire.

Default is `enabled=False` (Phase 3 behaviour, no model loaded).
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Iterable
from decimal import ROUND_DOWN, Decimal
from typing import TYPE_CHECKING

from xtrade.obs import emit_event
from xtrade.research.signals import Signal
from xtrade.strategy.base import (
    AccountSnapshot,
    SignalDrivenStrategy,
    register_strategy,
)
from xtrade.strategy.intent import OrderIntent

if TYPE_CHECKING:  # pragma: no cover - typing-only import
    from xtrade.research.ml_gate import MLGate, MLGateConfig


_DEFAULT_NOTIONAL_USD = Decimal("100")
_DEFAULT_QTY_STEP = Decimal("0.001")

_log = logging.getLogger("xtrade.strategy.momentum_follow")


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

        # ML gate (Phase 5 / B4). Lazy-import so disabled gate keeps the
        # supervisor import-graph free of sklearn / lightgbm.
        self._ml_gate_config: "MLGateConfig | None" = None
        self._ml_gate: "MLGate | None" = None
        raw_gate = self.config.get("ml_gate")
        if raw_gate:
            from xtrade.research.ml_gate import MLGate, MLGateConfig  # noqa: PLC0415

            self._ml_gate_config = MLGateConfig.from_mapping(raw_gate)
            if self._ml_gate_config.enabled:
                self._ml_gate = MLGate(self._ml_gate_config)

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
            return self._apply_ml_gate([close] if close is not None else [], signal)

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
            return self._apply_ml_gate(out, signal)

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
            return self._apply_ml_gate(out, signal)

        return []

    # ---- ML gate (Phase 5 / B4) ----------------------------------------

    def _apply_ml_gate(
        self,
        intents: list[OrderIntent],
        signal: Signal,
    ) -> list[OrderIntent]:
        """Filter `intents` through the ML gate if configured.

        Reduce-only closes (any direction) are PASSED THROUGH unchanged
        even when the gate is enabled — closing an existing position is
        a risk-reduction action that should not be blocked by a model
        prediction. Only opening intents go through the gate.

        Emits one `strategy.ml_gate.suppressed` event per dropped intent.
        """
        if not intents or self._ml_gate is None:
            return intents
        gate = self._ml_gate
        passed: list[OrderIntent] = []
        features = self._build_gate_features(signal)
        for intent in intents:
            if intent.reduce_only:
                passed.append(intent)
                continue
            decision = gate.decide(side=intent.side, features=features)
            if decision.allow:
                passed.append(intent)
                continue
            emit_event(
                _log,
                "strategy.ml_gate.suppressed",
                signal_symbol=signal.symbol,
                signal_direction=signal.direction,
                intent_side=intent.side,
                model_score=round(float(decision.score), 6),
                threshold=float(gate.config.score_threshold),
                direction_check=bool(gate.config.direction_check),
                reason=decision.reason,
                source_signal_id=intent.source_signal_id,
            )
        return passed

    def _build_gate_features(self, signal: Signal) -> dict[str, float]:
        """Best-effort feature mapping built from `signal` only.

        The strategy does not (yet) ingest OHLCV rolling stats; missing
        features fall back to 0.0 inside `MLGate.score` with a one-time
        warning. Sentiment values can be threaded via `signal.metadata`
        keys ``sentiment_score`` / ``sentiment_score_lag_1h`` when the
        scanner has them; otherwise 0.0.
        """
        from xtrade.research.ml_gate import build_features_from_signal  # noqa: PLC0415

        meta = signal.metadata if isinstance(signal.metadata, dict) else {}
        sentiment_score = float(meta.get("sentiment_score", 0.0))
        sentiment_lag = float(meta.get("sentiment_score_lag_1h", 0.0))
        return build_features_from_signal(
            signal_strength=float(signal.strength),
            direction=signal.direction,
            sentiment_score=sentiment_score,
            sentiment_score_lag_1h=sentiment_lag,
        )

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
