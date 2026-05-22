"""LiveOrderProbe — Phase 1 Task 6 / P5 connectivity-style strategy.

Drives the full live order path on testnet end-to-end:

  1. Subscribe to quotes + trades on the configured instrument.
  2. On first quote: submit a single GTC limit order **far from market**
     (`safety_multiplier × bid` for BUY, `1 / safety_multiplier × ask`
     for SELL — defaults pin BUY to 0.7×bid, matching Phase 0 C2-spot's
     safety pattern).
  3. On accept: cancel the order immediately.
  4. On cancel confirmation (or reject, or timeout): signal `done`.

Because the order is placed deeply OTM, it cannot realistically fill
within the probe window — making this a no-risk live demonstration of
data → submit → accept → cancel.

Mode: this strategy only makes sense in live mode. It still inherits
from `XtradeStrategy` so it picks up the shared `on_start` dispatch and
the same mode plumbing the rest of Phase 1 uses; the `mode` field is
expected to be `"live"`.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal

from nautilus_trader.model.enums import OrderSide, TimeInForce
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.orders import LimitOrder

from xtrade.strategies.base import XtradeStrategy, XtradeStrategyConfig


class LiveOrderProbeConfig(XtradeStrategyConfig, frozen=True, kw_only=True):
    """Configuration for `LiveOrderProbe`.

    Parameters
    ----------
    instrument_id : InstrumentId
        Instrument to subscribe to and place the probe order against.
    quantity : Decimal
        Order size in instrument units (e.g. ``0.001`` BTC).
    side : str
        ``"BUY"`` (default) or ``"SELL"``.
    safety_multiplier : Decimal
        BUY orders price at ``safety_multiplier × bid``; SELL orders at
        ``ask / safety_multiplier``. Default ``0.7`` matches Phase 0
        C2-spot — clear of Binance Spot's `PERCENT_PRICE_BY_SIDE` 0.5x
        floor but far enough from market that no fill is realistic in
        a 60-second window.
    timeout_s : float
        Wall-clock timeout for the whole probe (first quote + order
        accepted + cancel confirmed).
    """

    instrument_id: InstrumentId
    quantity: Decimal = Decimal("0.001")
    side: str = "BUY"
    safety_multiplier: Decimal = Decimal("0.7")
    timeout_s: float = 60.0


class LiveOrderProbe(XtradeStrategy):
    """Live-order connectivity probe.

    Internal state machine tracked via plain attributes + an asyncio
    `Event` (`done`) that the runner awaits.
    """

    def __init__(self, config: LiveOrderProbeConfig) -> None:
        super().__init__(config)
        # Public observation surface for the runner / summary.json.
        self.events: list[str] = []
        self.first_quote_ns: int | None = None
        self.first_trade_ns: int | None = None
        self.order: LimitOrder | None = None
        self.order_accepted: bool = False
        self.order_canceled: bool = False
        self.order_rejected: bool = False
        self.rejection_reason: str | None = None
        self.timed_out: bool = False
        # Set when the runner can shut down the node.
        self.done = asyncio.Event()
        self._start_ns: int = 0

    # ----- lifecycle ------------------------------------------------------

    def on_start_live(self) -> None:
        cfg: LiveOrderProbeConfig = self.config  # type: ignore[assignment]
        side = cfg.side.upper()
        if side not in ("BUY", "SELL"):
            self.log.error(f"LiveOrderProbe: invalid side {cfg.side!r}; aborting")
            self._mark_done("invalid-side")
            return

        self._start_ns = self.clock.timestamp_ns()
        self.subscribe_quote_ticks(cfg.instrument_id)
        self.subscribe_trade_ticks(cfg.instrument_id)
        self._record(f"subscribed quotes/trades: {cfg.instrument_id}")
        self.clock.set_time_alert_ns(
            "live-probe-timeout",
            self._start_ns + int(cfg.timeout_s * 1e9),
            self._on_timeout,
        )

    def on_start_backtest(self) -> None:  # pragma: no cover - defensive
        self.log.error("LiveOrderProbe should only be run in live mode")
        self._mark_done("backtest-mode-rejected")

    # ----- timer ----------------------------------------------------------

    def _on_timeout(self, _event) -> None:
        if self.done.is_set():
            return
        self.timed_out = True
        self._record("timeout reached before order lifecycle completed")
        self._mark_done("timeout")

    # ----- market data ----------------------------------------------------

    def on_quote_tick(self, tick) -> None:
        cfg: LiveOrderProbeConfig = self.config  # type: ignore[assignment]
        if self.first_quote_ns is None:
            self.first_quote_ns = self.clock.timestamp_ns()
            self._record(
                f"first quote: bid={tick.bid_price} ask={tick.ask_price}"
            )
        if self.order is None:
            ref = float(tick.bid_price) if cfg.side.upper() == "BUY" else float(tick.ask_price)
            self._place_order(ref)

    def on_trade_tick(self, tick) -> None:
        if self.first_trade_ns is None:
            self.first_trade_ns = self.clock.timestamp_ns()
            self._record(f"first trade: px={tick.price} qty={tick.size}")

    # ----- order placement ------------------------------------------------

    def _place_order(self, ref_price: float) -> None:
        cfg: LiveOrderProbeConfig = self.config  # type: ignore[assignment]
        instrument = self.cache.instrument(cfg.instrument_id)
        if instrument is None:
            self.log.warning(
                f"instrument {cfg.instrument_id} not yet in cache; "
                f"will retry on next tick"
            )
            return

        side = cfg.side.upper()
        if side == "BUY":
            target = Decimal(str(ref_price)) * cfg.safety_multiplier
            order_side = OrderSide.BUY
        else:
            # Far above ask: ref / safety_multiplier. 0.7 default → ~1.43x ask.
            target = Decimal(str(ref_price)) / cfg.safety_multiplier
            order_side = OrderSide.SELL

        price = instrument.make_price(target)
        qty = instrument.make_qty(cfg.quantity)
        order: LimitOrder = self.order_factory.limit(
            instrument_id=cfg.instrument_id,
            order_side=order_side,
            quantity=qty,
            price=price,
            time_in_force=TimeInForce.GTC,
            post_only=False,
        )
        self.order = order
        self.submit_order(order)
        self._record(f"submitted limit {side} {qty} @ {price}")

    # ----- order lifecycle events -----------------------------------------

    def on_order_accepted(self, event) -> None:
        self.order_accepted = True
        self._record(f"order accepted: {event.client_order_id}")
        if self.order is not None and not self.order_canceled:
            self.cancel_order(self.order)

    def on_order_canceled(self, event) -> None:
        self.order_canceled = True
        self._record(f"order canceled: {event.client_order_id}")
        self._mark_done("order-canceled")

    def on_order_rejected(self, event) -> None:
        self.order_rejected = True
        # Nautilus's reject event exposes `.reason` on most adapters.
        reason = getattr(event, "reason", "")
        self.rejection_reason = str(reason) if reason else None
        self._record(f"order REJECTED: {self.rejection_reason or '<no reason>'}")
        self._mark_done("order-rejected")

    # ----- helpers --------------------------------------------------------

    def _record(self, msg: str) -> None:
        self.events.append(msg)
        self.log.info(msg)

    def _mark_done(self, reason: str) -> None:
        self._record(f"done: {reason}")
        self.done.set()

    # ----- summary --------------------------------------------------------

    @property
    def passed(self) -> bool:
        """A run "passes" iff the limit order was both accepted and
        canceled (cleanly demonstrating the full live order path)."""
        return self.order_accepted and self.order_canceled and not self.order_rejected
