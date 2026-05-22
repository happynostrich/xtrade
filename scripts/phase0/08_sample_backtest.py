"""Task G (part 2) — C6: Run a minimal NautilusTrader backtest.

Consumes `data/binance_BTCUSDT_1m.csv` produced by `07_fetch_binance_history.py`,
wraps it into `Bar` data, and runs a tiny EMA-cross strategy in a
`BacktestEngine`. Prints the resulting positions/PnL summary and
records pass/fail.

This intentionally avoids any external data catalog setup; the bars
are fed directly into the engine.
"""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import append_result, stepwise  # noqa: E402


CHECK_ID = "C6b"
CHECK_NAME = "NautilusTrader minimal EMA-cross backtest"

CSV_PATH = Path(__file__).resolve().parents[2] / "data" / "binance_BTCUSDT_1m.csv"


def main() -> int:
    with stepwise(CHECK_ID, CHECK_NAME):
        notes: list[str] = []

        if not CSV_PATH.exists():
            raise RuntimeError(
                f"{CSV_PATH} not found. Run 07_fetch_binance_history.py first."
            )

        df = pd.read_csv(CSV_PATH, parse_dates=["open_time", "close_time"])
        if df.empty:
            raise RuntimeError("History CSV is empty")
        notes.append(f"loaded {len(df)} klines from {CSV_PATH.name}")

        from nautilus_trader.backtest.engine import BacktestEngine, BacktestEngineConfig
        from nautilus_trader.config import LoggingConfig
        from nautilus_trader.examples.strategies.ema_cross import EMACross, EMACrossConfig
        from nautilus_trader.model.currencies import USDT
        from nautilus_trader.model.data import Bar, BarSpecification, BarType
        from nautilus_trader.model.enums import (
            AccountType,
            AggregationSource,
            BarAggregation,
            OmsType,
            PriceType,
        )
        from nautilus_trader.model.identifiers import InstrumentId, Symbol, Venue
        from nautilus_trader.model.instruments import CryptoPerpetual
        from nautilus_trader.model.objects import Money, Price, Quantity
        from nautilus_trader.test_kit.providers import TestInstrumentProvider

        # Use TestInstrumentProvider's BTCUSDT perp definition (matches Binance USDT-M).
        try:
            instrument = TestInstrumentProvider.btcusdt_perp_binance()
        except AttributeError:
            instrument = TestInstrumentProvider.btcusdt_binance()
        instrument_id = instrument.id
        notes.append(f"backtest instrument: {instrument_id}")

        bar_spec = BarSpecification(
            step=1,
            aggregation=BarAggregation.MINUTE,
            price_type=PriceType.LAST,
        )
        bar_type = BarType(
            instrument_id=instrument_id,
            bar_spec=bar_spec,
            aggregation_source=AggregationSource.EXTERNAL,
        )

        # Construct Bar objects.
        bars: list[Bar] = []
        for row in df.itertuples(index=False):
            ts_ns = int(row.open_time.value)  # pandas Timestamp -> ns
            close_ns = int(row.close_time.value)
            bars.append(
                Bar(
                    bar_type=bar_type,
                    # BTCUSDT-PERP has tickSize=0.10 (price_precision=1);
                    # match the test instrument and the live venue.
                    open=Price.from_str(f"{row.open:.1f}"),
                    high=Price.from_str(f"{row.high:.1f}"),
                    low=Price.from_str(f"{row.low:.1f}"),
                    close=Price.from_str(f"{row.close:.1f}"),
                    # BTCUSDT lotSize=0.001 → size_precision=3.
                    volume=Quantity.from_str(f"{row.volume:.3f}"),
                    ts_event=ts_ns,
                    ts_init=close_ns,
                )
            )
        notes.append(f"built {len(bars)} Bar objects for backtest")

        engine = BacktestEngine(
            config=BacktestEngineConfig(
                trader_id="BACKTESTER-001",
                logging=LoggingConfig(log_level="INFO"),
            ),
        )
        venue = Venue("BINANCE")
        engine.add_venue(
            venue=venue,
            oms_type=OmsType.NETTING,
            account_type=AccountType.MARGIN,
            base_currency=None,
            starting_balances=[Money(1_000_000, USDT)],
        )
        engine.add_instrument(instrument)
        engine.add_data(bars)

        strategy = EMACross(
            config=EMACrossConfig(
                instrument_id=instrument_id,
                bar_type=bar_type,
                fast_ema_period=10,
                slow_ema_period=20,
                trade_size=Decimal("0.010"),
            ),
        )
        engine.add_strategy(strategy=strategy)

        engine.run()

        report = engine.trader.generate_account_report(venue)
        positions_report = engine.trader.generate_positions_report()
        orders_report = engine.trader.generate_order_fills_report()

        print("\n--- account report ---")
        print(report.to_string() if report is not None else "<none>")
        print("\n--- positions ---")
        print(positions_report.to_string() if positions_report is not None else "<none>")
        print("\n--- order fills (head) ---")
        print(orders_report.head().to_string() if orders_report is not None else "<none>")

        notes.append(f"orders filled: {len(orders_report) if orders_report is not None else 0}")
        notes.append(
            f"positions opened: {len(positions_report) if positions_report is not None else 0}"
        )

        engine.dispose()
        append_result(CHECK_ID, CHECK_NAME, "PASS", notes=notes)
        print(f"[{CHECK_ID}] PASS")
        return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception:
        sys.exit(1)
