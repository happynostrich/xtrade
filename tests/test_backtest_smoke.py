"""End-to-end offline smoke test for the backtest path (P8 / Task 5).

Builds a synthetic BTCUSDT-PERP catalog in tmp_path, runs the
`demo_ema` strategy through `run_backtest`, and asserts that the
pipeline produces a `summary.json` with `orders_filled > 0`.

The synthetic price series is a long up-trend → down-trend cycle so
the fast/slow EMA cross multiple times within the window, guaranteeing
that the strategy actually trades.
"""

from __future__ import annotations

import json
import math
from decimal import Decimal
from pathlib import Path

from nautilus_trader.model.data import Bar
from nautilus_trader.model.objects import Price, Quantity
from nautilus_trader.test_kit.providers import TestInstrumentProvider

from xtrade.backtest.runner import run_backtest
from xtrade.data.catalog import (
    bar_type_for,
    open_catalog,
    parse_bar_spec,
    write_bars,
)


_MIN_NS = 60 * 1_000_000_000


def _build_catalog_with_trending_bars(
    catalog_path: Path, n: int = 200, start_ns: int = 1_700_000_000_000_000_000
) -> int:
    """Seed `catalog_path` with `n` synthetic 1m BTCUSDT bars whose price
    follows a sin-wave so EMA10/EMA20 cross repeatedly. Returns n."""
    instrument = TestInstrumentProvider.btcusdt_perp_binance()
    spec = parse_bar_spec("1m")
    bar_type = bar_type_for(instrument, spec)

    pp = instrument.price_precision
    sp = instrument.size_precision

    bars: list[Bar] = []
    for i in range(n):
        ts = start_ns + i * _MIN_NS
        # ~25-bar period sin wave around 30000 with $250 amplitude.
        mid = 30_000.0 + 250.0 * math.sin(i / 4.0)
        open_p = mid
        close_p = mid + 5.0 * math.sin((i + 1) / 4.0)
        hi = max(open_p, close_p) + 2.0
        lo = min(open_p, close_p) - 2.0
        bars.append(
            Bar(
                bar_type=bar_type,
                open=Price.from_str(f"{open_p:.{pp}f}"),
                high=Price.from_str(f"{hi:.{pp}f}"),
                low=Price.from_str(f"{lo:.{pp}f}"),
                close=Price.from_str(f"{close_p:.{pp}f}"),
                volume=Quantity.from_str(f"{1.0 + 0.01 * i:.{sp}f}"),
                ts_event=ts,
                ts_init=ts,
            )
        )

    catalog = open_catalog(catalog_path)
    written = write_bars(catalog, instrument, bars)
    assert written == n
    return n


def test_backtest_smoke_writes_summary_and_fills_orders(tmp_path: Path) -> None:
    catalog_path = tmp_path / "catalog"
    logs_root = tmp_path / "logs"
    n_bars = _build_catalog_with_trending_bars(catalog_path, n=200)

    result = run_backtest(
        catalog_path=catalog_path,
        instrument_id="BTCUSDT-PERP.BINANCE",
        bar="1m",
        strategy="demo_ema",
        trade_size=Decimal("0.010"),
        fast_ema_period=5,
        slow_ema_period=15,
        logs_root=logs_root,
        run_id="smoke",
    )

    # Logs directory + summary file exist.
    assert result.log_dir == logs_root / "smoke"
    assert result.summary_path.exists()
    on_disk = json.loads(result.summary_path.read_text())
    assert on_disk == result.summary

    s = result.summary
    assert s["mode"] == "backtest"
    assert s["instrument_id"] == "BTCUSDT-PERP.BINANCE"
    assert s["bars_loaded"] == n_bars
    assert s["strategy"] == "demo_ema"
    assert s["venue"] == "BINANCE"

    # The sin-wave bars must trigger at least one EMA cross + fill.
    assert s["orders_filled"] > 0, "expected demo_ema to fill orders on trending bars"
    assert s["positions_opened"] > 0
    assert s["account_final"], "account report should not be empty"


def test_backtest_missing_instrument_raises(tmp_path: Path) -> None:
    """Backtesting against an empty catalog must surface a clear error."""
    import pytest

    catalog_path = tmp_path / "catalog"
    open_catalog(catalog_path)  # creates the dir

    with pytest.raises(FileNotFoundError) as excinfo:
        run_backtest(
            catalog_path=catalog_path,
            instrument_id="BTCUSDT-PERP.BINANCE",
            bar="1m",
            logs_root=tmp_path / "logs",
            run_id="missing",
        )
    assert "BTCUSDT-PERP.BINANCE" in str(excinfo.value)


def test_unknown_strategy_rejected(tmp_path: Path) -> None:
    import pytest

    _build_catalog_with_trending_bars(tmp_path / "catalog", n=30)

    with pytest.raises(ValueError):
        run_backtest(
            catalog_path=tmp_path / "catalog",
            instrument_id="BTCUSDT-PERP.BINANCE",
            bar="1m",
            strategy="does_not_exist",
            logs_root=tmp_path / "logs",
            run_id="bad-strategy",
        )
