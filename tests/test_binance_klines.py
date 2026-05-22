"""Offline tests for `xtrade.data.binance_klines` pure transformations
(Phase 1 Task 8 / P8 + P3).

We don't hit Binance's REST endpoint here — the network-bound paginator
is exercised by `scripts/phase1/02_ingest_binance.py` end-to-end. What we
do test:

  - `klines_df_to_bars` round-trips Binance's raw kline list into Nautilus
    `Bar` objects with the right precision, OHLCV, and timestamps.
  - Empty / missing dataframes yield `[]` without raising.
"""

from __future__ import annotations

import pandas as pd
from nautilus_trader.model.data import Bar
from nautilus_trader.model.objects import Price
from nautilus_trader.test_kit.providers import TestInstrumentProvider

from xtrade.data.binance_klines import _KLINE_COLS, klines_df_to_bars
from xtrade.data.catalog import bar_type_for, parse_bar_spec


def _make_raw_kline_row(open_ms: int, close_ms: int, base: float, vol: float) -> list:
    """Mimic the 12-tuple Binance Futures `/fapi/v1/klines` returns."""
    return [
        open_ms,                # open_time
        f"{base:.2f}",          # open
        f"{base + 1.0:.2f}",    # high
        f"{base - 1.0:.2f}",    # low
        f"{base + 0.25:.2f}",   # close
        f"{vol:.4f}",           # volume
        close_ms,               # close_time
        "0",                    # quote_asset_volume
        0,                      # trades
        "0",                    # taker_buy_base_volume
        "0",                    # taker_buy_quote_volume
        "0",                    # ignore
    ]


def _build_df(n: int) -> pd.DataFrame:
    rows = []
    base_ms = 1_700_000_000_000  # arbitrary fixed epoch ms
    for i in range(n):
        open_ms = base_ms + i * 60_000
        close_ms = open_ms + 59_999
        rows.append(_make_raw_kline_row(open_ms, close_ms, 30_000.0 + i, 1.0 + 0.1 * i))
    df = pd.DataFrame(rows, columns=list(_KLINE_COLS))
    # Numeric columns get coerced by `fetch_klines_df`; replicate that here.
    for c in ("open", "high", "low", "close", "volume"):
        df[c] = pd.to_numeric(df[c])
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    df["close_time"] = pd.to_datetime(df["close_time"], unit="ms", utc=True)
    return df


def test_empty_df_returns_no_bars() -> None:
    instrument = TestInstrumentProvider.btcusdt_perp_binance()
    bar_type = bar_type_for(instrument, parse_bar_spec("1m"))
    empty = pd.DataFrame(columns=list(_KLINE_COLS))
    assert klines_df_to_bars(empty, instrument, bar_type) == []


def test_df_to_bars_round_trip_count_and_endpoints() -> None:
    instrument = TestInstrumentProvider.btcusdt_perp_binance()
    bar_type = bar_type_for(instrument, parse_bar_spec("1m"))
    df = _build_df(n=5)

    bars = klines_df_to_bars(df, instrument, bar_type)

    assert len(bars) == 5
    assert all(isinstance(b, Bar) for b in bars)
    # First bar's ts_event should equal df.open_time[0] in nanoseconds.
    assert bars[0].ts_event == int(df["open_time"].iloc[0].value)
    assert bars[-1].ts_event == int(df["open_time"].iloc[-1].value)
    # ts_init carries the close_time.
    assert bars[0].ts_init == int(df["close_time"].iloc[0].value)


def test_df_to_bars_uses_instrument_precision() -> None:
    instrument = TestInstrumentProvider.btcusdt_perp_binance()
    bar_type = bar_type_for(instrument, parse_bar_spec("1m"))
    df = _build_df(n=1)

    bars = klines_df_to_bars(df, instrument, bar_type)

    # BTCUSDT-PERP.BINANCE has price_precision=2 in the test kit.
    expected_open = Price.from_str(f"{30_000.0:.{instrument.price_precision}f}")
    assert str(bars[0].open) == str(expected_open)
    # Sanity: prices and volume are positive and OHLC ordering holds.
    assert bars[0].high >= bars[0].open >= bars[0].low
    assert bars[0].high >= bars[0].close >= bars[0].low
    assert float(bars[0].volume.as_double()) > 0.0


def test_df_to_bars_monotonic_timestamps() -> None:
    instrument = TestInstrumentProvider.btcusdt_perp_binance()
    bar_type = bar_type_for(instrument, parse_bar_spec("1m"))
    df = _build_df(n=10)

    bars = klines_df_to_bars(df, instrument, bar_type)

    assert all(b2.ts_event > b1.ts_event for b1, b2 in zip(bars, bars[1:]))
