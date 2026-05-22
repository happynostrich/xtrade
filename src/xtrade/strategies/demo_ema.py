"""DemoEmaCross — the canonical xtrade demo strategy (Phase 1 Task 5).

A minimal EMA-cross strategy lifted from Phase 0's C6b
(`scripts/phase0/08_sample_backtest.py`) and reshaped to use the
`XtradeStrategy` base so the same class runs in both backtest and live
testnet modes (P6, "one strategy, two modes").

Behavior:
  - Subscribes to bars of `config.bar_type`.
  - Maintains two ExponentialMovingAverage indicators (fast + slow).
  - When fast >= slow and we are flat or net-short: market BUY
    `trade_size` units. When fast < slow and we are flat or net-long:
    market SELL `trade_size` units. Position-flipping always closes
    first.
  - In live mode, requests one day of historical bars on start to warm
    up the indicators. Skipped in backtest (the engine pre-loads bars).

This is purely a connectivity / plumbing demo — it is intentionally not
a profitable signal.
"""

from __future__ import annotations

from decimal import Decimal

import pandas as pd
from nautilus_trader.core.correctness import PyCondition
from nautilus_trader.indicators.averages import ExponentialMovingAverage
from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.enums import OrderSide, TimeInForce
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.instruments import Instrument
from nautilus_trader.model.orders import MarketOrder

from xtrade.strategies.base import XtradeStrategy, XtradeStrategyConfig


class DemoEmaCrossConfig(XtradeStrategyConfig, frozen=True, kw_only=True):
    """Configuration for `DemoEmaCross`.

    Parameters
    ----------
    instrument_id : InstrumentId
        Instrument to trade.
    bar_type : BarType
        Bar type to subscribe to (must be EXTERNAL aggregation for the
        catalog backtest path).
    trade_size : Decimal
        Quantity per trade, in instrument size units (e.g. 0.010 BTC).
    fast_ema_period : int, default 10
    slow_ema_period : int, default 20
    """

    instrument_id: InstrumentId
    bar_type: BarType
    trade_size: Decimal
    fast_ema_period: int = 10
    slow_ema_period: int = 20


class DemoEmaCross(XtradeStrategy):
    """EMA-cross demo using the xtrade base class."""

    def __init__(self, config: DemoEmaCrossConfig) -> None:
        PyCondition.is_true(
            config.fast_ema_period < config.slow_ema_period,
            "fast_ema_period must be < slow_ema_period",
        )
        super().__init__(config)
        self.instrument: Instrument | None = None
        self.fast_ema = ExponentialMovingAverage(config.fast_ema_period)
        self.slow_ema = ExponentialMovingAverage(config.slow_ema_period)

    # ----- lifecycle ------------------------------------------------------

    def on_start_common(self) -> None:
        cfg: DemoEmaCrossConfig = self.config  # type: ignore[assignment]
        self.instrument = self.cache.instrument(cfg.instrument_id)
        if self.instrument is None:
            self.log.error(f"Could not find instrument for {cfg.instrument_id}")
            self.stop()
            return

        self.register_indicator_for_bars(cfg.bar_type, self.fast_ema)
        self.register_indicator_for_bars(cfg.bar_type, self.slow_ema)
        self.subscribe_bars(cfg.bar_type)

    def on_start_live(self) -> None:
        # Warm up the indicators from venue history.
        cfg: DemoEmaCrossConfig = self.config  # type: ignore[assignment]
        self.request_bars(
            cfg.bar_type,
            start=self._clock.utc_now() - pd.Timedelta(days=1),
        )

    # ----- core logic -----------------------------------------------------

    def on_bar(self, bar: Bar) -> None:
        cfg: DemoEmaCrossConfig = self.config  # type: ignore[assignment]

        if not self.indicators_initialized():
            return

        if bar.is_single_price():
            return

        if self.fast_ema.value >= self.slow_ema.value:
            if self.portfolio.is_flat(cfg.instrument_id):
                self._buy()
            elif self.portfolio.is_net_short(cfg.instrument_id):
                self.close_all_positions(cfg.instrument_id)
                self._buy()
        else:
            if self.portfolio.is_flat(cfg.instrument_id):
                self._sell()
            elif self.portfolio.is_net_long(cfg.instrument_id):
                self.close_all_positions(cfg.instrument_id)
                self._sell()

    # ----- helpers --------------------------------------------------------

    def _quantity(self):
        assert self.instrument is not None
        return self.instrument.make_qty(self.config.trade_size)  # type: ignore[attr-defined]

    def _buy(self) -> None:
        cfg: DemoEmaCrossConfig = self.config  # type: ignore[assignment]
        order: MarketOrder = self.order_factory.market(
            instrument_id=cfg.instrument_id,
            order_side=OrderSide.BUY,
            quantity=self._quantity(),
            time_in_force=TimeInForce.GTC,
        )
        self.submit_order(order)

    def _sell(self) -> None:
        cfg: DemoEmaCrossConfig = self.config  # type: ignore[assignment]
        order: MarketOrder = self.order_factory.market(
            instrument_id=cfg.instrument_id,
            order_side=OrderSide.SELL,
            quantity=self._quantity(),
            time_in_force=TimeInForce.GTC,
        )
        self.submit_order(order)
