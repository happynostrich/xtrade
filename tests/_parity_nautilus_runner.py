"""Subprocess entry point for the Nautilus side of the parity test.

`tests/test_parity_vectorbt_nautilus.py` shells out to this module
(`python -m tests._parity_nautilus_runner <catalog_path> <log_dir>
<fast> <slow>`) because `nautilus_trader.backtest.engine.BacktestEngine`
cannot be cleanly instantiated more than once in a single Python
process (the second `__init__` aborts the interpreter). Running it in
a fresh subprocess sidesteps the issue without dragging in
pytest-forked or pytest-xdist.

The script prints a single JSON line to stdout containing the list of
`ts_event` (nanoseconds) values at which the parity strategy recorded
a long entry — the test parses that line back into a list of ints.

This is a test-only helper. It is intentionally *not* part of
`xtrade.strategies` or `xtrade.backtest`.
"""

from __future__ import annotations

import json
import sys
from decimal import Decimal
from pathlib import Path

from nautilus_trader.backtest.engine import BacktestEngine, BacktestEngineConfig
from nautilus_trader.config import LoggingConfig
from nautilus_trader.core.correctness import PyCondition
from nautilus_trader.indicators.averages import SimpleMovingAverage
from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.enums import AccountType, OmsType, OrderSide, TimeInForce
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.instruments import Instrument
from nautilus_trader.model.objects import Money
from nautilus_trader.model.orders import MarketOrder

from xtrade.data.catalog import bar_type_for, open_catalog, parse_bar_spec, read_bars
from xtrade.strategies.base import XtradeStrategy, XtradeStrategyConfig


# ---------------------------------------------------------------------------
# Parity-only Nautilus strategy
# ---------------------------------------------------------------------------


class MomentumDemoSMAConfig(XtradeStrategyConfig, frozen=True, kw_only=True):
    instrument_id: InstrumentId
    bar_type: BarType
    trade_size: Decimal
    fast_period: int
    slow_period: int


class MomentumDemoSMA(XtradeStrategy):
    """SMA-cross sibling of `DemoEmaCross` used only for parity tests.

    Mirrors `MomentumScanner` arithmetic: fast SMA, slow SMA, edge-
    triggered on bar close. When fast >= slow and previously fast < slow,
    record a `long_entry_ts` and BUY. When fast < slow and previously
    fast >= slow, record `exit_ts` and close.
    """

    def __init__(self, config: MomentumDemoSMAConfig) -> None:
        PyCondition.is_true(
            config.fast_period < config.slow_period,
            "fast_period must be < slow_period",
        )
        super().__init__(config)
        self.instrument: Instrument | None = None
        self.fast_sma = SimpleMovingAverage(config.fast_period)
        self.slow_sma = SimpleMovingAverage(config.slow_period)
        self.long_entry_ts: list[int] = []
        self.exit_ts: list[int] = []
        self._prev_above: bool | None = None

    def on_start_common(self) -> None:
        cfg: MomentumDemoSMAConfig = self.config  # type: ignore[assignment]
        self.instrument = self.cache.instrument(cfg.instrument_id)
        if self.instrument is None:
            self.log.error(f"Could not find instrument for {cfg.instrument_id}")
            self.stop()
            return
        self.register_indicator_for_bars(cfg.bar_type, self.fast_sma)
        self.register_indicator_for_bars(cfg.bar_type, self.slow_sma)
        self.subscribe_bars(cfg.bar_type)

    def on_bar(self, bar: Bar) -> None:
        cfg: MomentumDemoSMAConfig = self.config  # type: ignore[assignment]
        if not self.indicators_initialized():
            return
        above = self.fast_sma.value >= self.slow_sma.value

        if self._prev_above is None:
            # First "settled" bar — establish a baseline but do not emit
            # an event (parity with vectorbt: the first comparison row
            # is NaN and so produces no edge).
            self._prev_above = above
            return

        if above and not self._prev_above:
            self.long_entry_ts.append(bar.ts_event)
            if self.portfolio.is_flat(cfg.instrument_id):
                self._buy()
            elif self.portfolio.is_net_short(cfg.instrument_id):
                self.close_all_positions(cfg.instrument_id)
                self._buy()
        elif (not above) and self._prev_above:
            self.exit_ts.append(bar.ts_event)
            if self.portfolio.is_net_long(cfg.instrument_id):
                self.close_all_positions(cfg.instrument_id)

        self._prev_above = above

    # ----- helpers --------------------------------------------------------

    def _quantity(self):
        assert self.instrument is not None
        return self.instrument.make_qty(self.config.trade_size)  # type: ignore[attr-defined]

    def _buy(self) -> None:
        cfg: MomentumDemoSMAConfig = self.config  # type: ignore[assignment]
        order: MarketOrder = self.order_factory.market(
            instrument_id=cfg.instrument_id,
            order_side=OrderSide.BUY,
            quantity=self._quantity(),
            time_in_force=TimeInForce.GTC,
        )
        self.submit_order(order)


# ---------------------------------------------------------------------------
# Subprocess entry point
# ---------------------------------------------------------------------------


def run(catalog_path: Path, log_dir: Path, fast: int, slow: int) -> list[int]:
    catalog = open_catalog(catalog_path)
    # The catalog has exactly one instrument seeded by the test.
    instruments = list(catalog.instruments())
    assert instruments, f"no instruments in catalog {catalog_path}"
    instrument = instruments[0]
    bar_type = bar_type_for(instrument, parse_bar_spec("1m"))
    bars = read_bars(catalog, bar_type)
    log_dir.mkdir(parents=True, exist_ok=True)

    engine = BacktestEngine(
        config=BacktestEngineConfig(
            trader_id="PARITY-TESTER",
            logging=LoggingConfig(
                log_level="ERROR",
                log_level_file="ERROR",
                log_directory=str(log_dir),
                log_file_name="run",
            ),
        ),
    )
    engine.add_venue(
        venue=instrument.venue,
        oms_type=OmsType.NETTING,
        account_type=AccountType.MARGIN,
        base_currency=None,
        starting_balances=[Money(1_000_000, instrument.settlement_currency)],
    )
    engine.add_instrument(instrument)
    engine.add_data(bars)
    strategy = MomentumDemoSMA(
        config=MomentumDemoSMAConfig(
            mode="backtest",
            instrument_id=instrument.id,
            bar_type=bar_type,
            trade_size=Decimal("0.010"),
            fast_period=fast,
            slow_period=slow,
        )
    )
    engine.add_strategy(strategy=strategy)
    engine.run()
    captured = list(strategy.long_entry_ts)
    engine.dispose()
    return captured


def main(argv: list[str]) -> int:
    if len(argv) != 5:
        print(
            "usage: python -m tests._parity_nautilus_runner "
            "<catalog_path> <log_dir> <fast> <slow>",
            file=sys.stderr,
        )
        return 2
    catalog_path = Path(argv[1])
    log_dir = Path(argv[2])
    fast = int(argv[3])
    slow = int(argv[4])
    ts_list = run(catalog_path, log_dir, fast, slow)
    # Emit one JSON line on stdout for the parent to parse.
    sys.stdout.write(json.dumps({"long_entry_ts": ts_list}) + "\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
