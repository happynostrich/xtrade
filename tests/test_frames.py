"""Offline tests for `xtrade.research.frames` (Phase 2 Task 2 / S2).

Covers the catalog → pandas DataFrame bridge:

  - `bars_to_dataframe`: single-symbol OHLCV round-trip, empty-catalog
    short-circuit, range filter, monotonic UTC index.
  - `bars_to_panel`: multi-symbol close panel with outer-join alignment,
    field selection, empty bar_types, duplicate-instrument guard.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest
from nautilus_trader.model.data import Bar
from nautilus_trader.model.objects import Price, Quantity
from nautilus_trader.test_kit.providers import TestInstrumentProvider

from xtrade.data.catalog import bar_type_for, open_catalog, parse_bar_spec, write_bars
from xtrade.research.frames import bars_to_dataframe, bars_to_panel


_MIN_NS = 60 * 1_000_000_000


def _make_bars(bar_type, instrument, n: int, *, start_ns: int, base: float = 30_000.0) -> list[Bar]:
    pp = instrument.price_precision
    sp = instrument.size_precision
    bars: list[Bar] = []
    for i in range(n):
        ts = start_ns + i * _MIN_NS
        b = base + i
        bars.append(
            Bar(
                bar_type=bar_type,
                open=Price.from_str(f"{b:.{pp}f}"),
                high=Price.from_str(f"{b + 1.0:.{pp}f}"),
                low=Price.from_str(f"{b - 1.0:.{pp}f}"),
                close=Price.from_str(f"{b + 0.25:.{pp}f}"),
                volume=Quantity.from_str(f"{1.0 + 0.1 * i:.{sp}f}"),
                ts_event=ts,
                ts_init=ts,
            )
        )
    return bars


# ---------------------------------------------------------------------------
# bars_to_dataframe
# ---------------------------------------------------------------------------


def test_bars_to_dataframe_round_trip(tmp_path: Path) -> None:
    instrument = TestInstrumentProvider.btcusdt_perp_binance()
    bar_type = bar_type_for(instrument, parse_bar_spec("1m"))
    catalog = open_catalog(tmp_path)
    bars = _make_bars(bar_type, instrument, n=10, start_ns=1_700_000_000_000_000_000)
    write_bars(catalog, instrument, bars)

    df = bars_to_dataframe(catalog, bar_type)
    assert isinstance(df, pd.DataFrame)
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
    assert len(df) == 10
    # ts_event index is UTC and monotonic.
    assert df.index.tz is not None and str(df.index.tz) == "UTC"
    assert df.index.is_monotonic_increasing
    # First open value equals what we wrote.
    assert df["open"].iloc[0] == pytest.approx(30_000.0)
    # close was written as `base + 0.25` rounded to instrument precision
    # (BTCUSDT-PERP has price_precision=1, so 30009.25 → 30009.2/3).
    assert df["close"].iloc[-1] == pytest.approx(30_009.25, abs=0.1)


def test_bars_to_dataframe_empty_catalog(tmp_path: Path) -> None:
    instrument = TestInstrumentProvider.btcusdt_perp_binance()
    bar_type = bar_type_for(instrument, parse_bar_spec("1m"))
    catalog = open_catalog(tmp_path)

    df = bars_to_dataframe(catalog, bar_type)
    assert df.empty
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
    assert str(df.index.tz) == "UTC"


def test_bars_to_dataframe_range_filter(tmp_path: Path) -> None:
    instrument = TestInstrumentProvider.btcusdt_perp_binance()
    bar_type = bar_type_for(instrument, parse_bar_spec("1m"))
    catalog = open_catalog(tmp_path)
    bars = _make_bars(bar_type, instrument, n=20, start_ns=1_700_000_000_000_000_000)
    write_bars(catalog, instrument, bars)

    start_ns = bars[5].ts_event
    end_ns = bars[14].ts_event
    df = bars_to_dataframe(catalog, bar_type, since_ns=start_ns, until_ns=end_ns)
    assert len(df) == 10  # inclusive on both ends
    assert int(df.index[0].value) == start_ns
    assert int(df.index[-1].value) == end_ns


def test_bars_to_dataframe_accepts_catalog_path(tmp_path: Path) -> None:
    """The bridge should accept either an open ParquetDataCatalog or a Path."""
    instrument = TestInstrumentProvider.btcusdt_perp_binance()
    bar_type = bar_type_for(instrument, parse_bar_spec("1m"))
    catalog = open_catalog(tmp_path)
    write_bars(
        catalog, instrument,
        _make_bars(bar_type, instrument, n=3, start_ns=1_700_000_000_000_000_000),
    )

    df = bars_to_dataframe(tmp_path, bar_type)
    assert len(df) == 3


# ---------------------------------------------------------------------------
# bars_to_panel
# ---------------------------------------------------------------------------


def test_bars_to_panel_aligns_two_symbols(tmp_path: Path) -> None:
    btc = TestInstrumentProvider.btcusdt_perp_binance()
    eth = TestInstrumentProvider.ethusdt_perp_binance()
    btc_bt = bar_type_for(btc, parse_bar_spec("1m"))
    eth_bt = bar_type_for(eth, parse_bar_spec("1m"))
    catalog = open_catalog(tmp_path)

    base = 1_700_000_000_000_000_000
    btc_bars = _make_bars(btc_bt, btc, n=10, start_ns=base, base=30_000.0)
    eth_bars = _make_bars(eth_bt, eth, n=8, start_ns=base + 2 * _MIN_NS, base=2_000.0)
    write_bars(catalog, btc, btc_bars)
    write_bars(catalog, eth, eth_bars)

    panel = bars_to_panel(catalog, [btc_bt, eth_bt])
    assert set(panel.columns) == {str(btc_bt.instrument_id), str(eth_bt.instrument_id)}
    # Outer-join: BTC has 10 bars from t0, ETH has 8 bars from t0+2m.
    # Union should be 10 timestamps (BTC's are a superset here).
    assert len(panel) == 10
    # ETH is NaN for the first 2 minutes.
    eth_col = str(eth_bt.instrument_id)
    assert panel[eth_col].iloc[0:2].isna().all()
    assert not panel[eth_col].iloc[2:].isna().any()
    # UTC, monotonic.
    assert str(panel.index.tz) == "UTC"
    assert panel.index.is_monotonic_increasing


def test_bars_to_panel_preserves_caller_column_order(tmp_path: Path) -> None:
    """Column order tracks bar_types, not alphabetical sort."""
    btc = TestInstrumentProvider.btcusdt_perp_binance()
    eth = TestInstrumentProvider.ethusdt_perp_binance()
    btc_bt = bar_type_for(btc, parse_bar_spec("1m"))
    eth_bt = bar_type_for(eth, parse_bar_spec("1m"))
    catalog = open_catalog(tmp_path)
    write_bars(
        catalog, btc, _make_bars(btc_bt, btc, n=3, start_ns=1_700_000_000_000_000_000),
    )
    write_bars(
        catalog, eth, _make_bars(eth_bt, eth, n=3, start_ns=1_700_000_000_000_000_000, base=2000.0),
    )

    panel_a = bars_to_panel(catalog, [btc_bt, eth_bt])
    panel_b = bars_to_panel(catalog, [eth_bt, btc_bt])
    assert list(panel_a.columns) == [str(btc_bt.instrument_id), str(eth_bt.instrument_id)]
    assert list(panel_b.columns) == [str(eth_bt.instrument_id), str(btc_bt.instrument_id)]


def test_bars_to_panel_field_selection(tmp_path: Path) -> None:
    btc = TestInstrumentProvider.btcusdt_perp_binance()
    btc_bt = bar_type_for(btc, parse_bar_spec("1m"))
    catalog = open_catalog(tmp_path)
    bars = _make_bars(btc_bt, btc, n=5, start_ns=1_700_000_000_000_000_000)
    write_bars(catalog, btc, bars)

    close_panel = bars_to_panel(catalog, [btc_bt], field="close")
    high_panel = bars_to_panel(catalog, [btc_bt], field="high")
    col = str(btc_bt.instrument_id)
    # Close = base + 0.25; high = base + 1.0; they should differ row-wise.
    assert (high_panel[col] > close_panel[col]).all()


def test_bars_to_panel_rejects_unknown_field(tmp_path: Path) -> None:
    btc = TestInstrumentProvider.btcusdt_perp_binance()
    btc_bt = bar_type_for(btc, parse_bar_spec("1m"))
    catalog = open_catalog(tmp_path)
    with pytest.raises(ValueError, match="field must be one of"):
        bars_to_panel(catalog, [btc_bt], field="vwap")  # type: ignore[arg-type]


def test_bars_to_panel_empty_bar_types(tmp_path: Path) -> None:
    catalog = open_catalog(tmp_path)
    panel = bars_to_panel(catalog, [])
    assert panel.empty
    assert str(panel.index.tz) == "UTC"


def test_bars_to_panel_rejects_duplicate_bar_types(tmp_path: Path) -> None:
    btc = TestInstrumentProvider.btcusdt_perp_binance()
    btc_bt = bar_type_for(btc, parse_bar_spec("1m"))
    catalog = open_catalog(tmp_path)
    with pytest.raises(ValueError, match="duplicate instrument"):
        bars_to_panel(catalog, [btc_bt, btc_bt])


def test_bars_to_panel_empty_catalog(tmp_path: Path) -> None:
    btc = TestInstrumentProvider.btcusdt_perp_binance()
    btc_bt = bar_type_for(btc, parse_bar_spec("1m"))
    catalog = open_catalog(tmp_path)
    panel = bars_to_panel(catalog, [btc_bt])
    assert panel.empty
    assert list(panel.columns) == [str(btc_bt.instrument_id)]
    assert str(panel.index.tz) == "UTC"
