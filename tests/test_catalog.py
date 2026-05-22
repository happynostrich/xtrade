"""Tests for `xtrade.data.catalog` (Phase 1 Task 4 / P8).

Fully offline: no network, no Phase 0 dependencies. Uses
`TestInstrumentProvider.btcusdt_perp_binance()` as the canonical
instrument and a deterministic synthetic bar stream.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from nautilus_trader.model.data import Bar
from nautilus_trader.model.enums import BarAggregation, PriceType
from nautilus_trader.model.objects import Price, Quantity
from nautilus_trader.test_kit.providers import TestInstrumentProvider

from xtrade.data.catalog import (
    ParsedBarSpec,
    bar_type_for,
    default_catalog_path,
    intervals_for,
    missing_intervals,
    open_catalog,
    parse_bar_spec,
    read_bars,
    write_bars,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_MIN_NS = 60 * 1_000_000_000  # 1 minute in ns


def _make_bars(bar_type, instrument, n: int, start_ns: int = 1_700_000_000_000_000_000) -> list[Bar]:
    """Synth n 1-minute bars with monotonic timestamps and tame OHLCV."""
    pp = instrument.price_precision
    sp = instrument.size_precision
    bars: list[Bar] = []
    for i in range(n):
        ts = start_ns + i * _MIN_NS
        base = 30_000.0 + i * 0.5
        bars.append(
            Bar(
                bar_type=bar_type,
                open=Price.from_str(f"{base:.{pp}f}"),
                high=Price.from_str(f"{base + 1.0:.{pp}f}"),
                low=Price.from_str(f"{base - 1.0:.{pp}f}"),
                close=Price.from_str(f"{base + 0.25:.{pp}f}"),
                volume=Quantity.from_str(f"{1.0 + 0.1 * i:.{sp}f}"),
                ts_event=ts,
                ts_init=ts,
            )
        )
    return bars


# ---------------------------------------------------------------------------
# parse_bar_spec
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,step,unit,agg",
    [
        ("1m", 1, "m", BarAggregation.MINUTE),
        ("5m", 5, "m", BarAggregation.MINUTE),
        ("1h", 1, "h", BarAggregation.HOUR),
        ("4h", 4, "h", BarAggregation.HOUR),
        ("1d", 1, "d", BarAggregation.DAY),
        ("30s", 30, "s", BarAggregation.SECOND),
    ],
)
def test_parse_bar_spec_valid(text, step, unit, agg) -> None:
    parsed = parse_bar_spec(text)
    assert parsed == ParsedBarSpec(step=step, unit=unit)
    assert parsed.aggregation == agg
    spec = parsed.to_spec()
    assert spec.step == step
    assert spec.aggregation == agg
    assert spec.price_type == PriceType.LAST


@pytest.mark.parametrize("bad", ["", "m", "5", "5x", "0m", "-1h", "abc"])
def test_parse_bar_spec_rejects_garbage(bad) -> None:
    with pytest.raises(ValueError):
        parse_bar_spec(bad)


def test_parsed_bar_spec_intervals() -> None:
    p = parse_bar_spec("5m")
    assert p.to_milliseconds() == 5 * 60_000
    assert p.binance_interval() == "5m"
    assert p.hyperliquid_interval() == "5m"


# ---------------------------------------------------------------------------
# default_catalog_path
# ---------------------------------------------------------------------------


def test_default_catalog_path_under_repo_root() -> None:
    p = default_catalog_path()
    assert p.is_absolute()
    assert p.name == "catalog"
    assert p.parent.name == "data"
    # Walk up to find the repo root sibling files.
    assert (p.parent.parent / "pyproject.toml").exists()


# ---------------------------------------------------------------------------
# write/read round-trip
# ---------------------------------------------------------------------------


def test_round_trip_preserves_count_and_endpoints(tmp_path: Path) -> None:
    instrument = TestInstrumentProvider.btcusdt_perp_binance()
    spec = parse_bar_spec("1m")
    bar_type = bar_type_for(instrument, spec)

    catalog = open_catalog(tmp_path)
    bars = _make_bars(bar_type, instrument, n=10)
    n = write_bars(catalog, instrument, bars)
    assert n == 10

    out = read_bars(catalog, bar_type)
    assert len(out) == 10
    assert out[0].ts_event == bars[0].ts_event
    assert out[-1].ts_event == bars[-1].ts_event
    assert str(out[0].open) == str(bars[0].open)
    assert str(out[-1].close) == str(bars[-1].close)


def test_round_trip_persists_instrument(tmp_path: Path) -> None:
    instrument = TestInstrumentProvider.btcusdt_perp_binance()
    spec = parse_bar_spec("1m")
    bar_type = bar_type_for(instrument, spec)
    catalog = open_catalog(tmp_path)
    write_bars(catalog, instrument, _make_bars(bar_type, instrument, n=3))

    fetched_instruments = catalog.instruments()
    assert len(fetched_instruments) == 1
    assert fetched_instruments[0].id == instrument.id


def test_range_filter_reads_only_window(tmp_path: Path) -> None:
    instrument = TestInstrumentProvider.btcusdt_perp_binance()
    spec = parse_bar_spec("1m")
    bar_type = bar_type_for(instrument, spec)
    catalog = open_catalog(tmp_path)
    bars = _make_bars(bar_type, instrument, n=20)
    write_bars(catalog, instrument, bars)

    # Read middle window: bars[5..14] inclusive.
    start_ns = bars[5].ts_event
    end_ns = bars[14].ts_event
    window = read_bars(catalog, bar_type, start_ns=start_ns, end_ns=end_ns)
    assert len(window) == 10
    assert window[0].ts_event == start_ns
    assert window[-1].ts_event == end_ns


def test_empty_write_is_noop(tmp_path: Path) -> None:
    instrument = TestInstrumentProvider.btcusdt_perp_binance()
    catalog = open_catalog(tmp_path)
    n = write_bars(catalog, instrument, [])
    assert n == 0
    # And nothing was indexed.
    spec = parse_bar_spec("1m")
    bar_type = bar_type_for(instrument, spec)
    assert read_bars(catalog, bar_type) == []


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_double_write_does_not_duplicate(tmp_path: Path) -> None:
    """Writing the same range twice must not duplicate bars on disk.

    Nautilus's per-file `<start_ns>_<end_ns>.parquet` naming naturally
    dedups, but we still validate the user-visible invariant.
    """
    instrument = TestInstrumentProvider.btcusdt_perp_binance()
    spec = parse_bar_spec("1m")
    bar_type = bar_type_for(instrument, spec)
    catalog = open_catalog(tmp_path)
    bars = _make_bars(bar_type, instrument, n=5)

    write_bars(catalog, instrument, bars)
    write_bars(catalog, instrument, bars)

    out = read_bars(catalog, bar_type)
    assert len(out) == 5
    intervals = intervals_for(catalog, bar_type)
    assert intervals == [(bars[0].ts_event, bars[-1].ts_event)]


def test_missing_intervals_after_partial_write(tmp_path: Path) -> None:
    """`missing_intervals` should report the uncovered tail of a range."""
    instrument = TestInstrumentProvider.btcusdt_perp_binance()
    spec = parse_bar_spec("1m")
    bar_type = bar_type_for(instrument, spec)
    catalog = open_catalog(tmp_path)

    # Write first 5 minutes [t0 .. t0+4min]
    base = 1_700_000_000_000_000_000
    first = _make_bars(bar_type, instrument, n=5, start_ns=base)
    write_bars(catalog, instrument, first)

    # Ask for first 10 minutes [t0 .. t0+9min]: gap = [t0+5min .. t0+9min]
    full_start = base
    full_end = base + 9 * _MIN_NS
    missing = missing_intervals(catalog, bar_type, full_start, full_end)
    assert missing, "expected at least one missing interval after partial write"
    # First missing range must start strictly after the covered region.
    assert missing[0][0] > first[-1].ts_event
    assert missing[-1][1] == full_end


def test_no_missing_intervals_after_complete_write(tmp_path: Path) -> None:
    instrument = TestInstrumentProvider.btcusdt_perp_binance()
    spec = parse_bar_spec("1m")
    bar_type = bar_type_for(instrument, spec)
    catalog = open_catalog(tmp_path)
    bars = _make_bars(bar_type, instrument, n=10)
    write_bars(catalog, instrument, bars)

    missing = missing_intervals(
        catalog, bar_type, bars[0].ts_event, bars[-1].ts_event
    )
    assert missing == []
