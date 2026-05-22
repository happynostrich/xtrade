"""Binance USDT-M futures klines fetcher.

Phase 1 Task 4 (P3) — lifted from `scripts/phase0/07_fetch_binance_history.py`
into a reusable module. Pages through the public `/fapi/v1/klines` REST
endpoint (no credentials required) and converts the resulting frame
into Nautilus `Bar` objects ready for catalog write.

The fetcher is intentionally REST-only and venue-public; testnet/demo
auth is irrelevant for historical klines (USDT-M Futures klines are
the same on mainnet REST whether you have a Demo account or not).
"""

from __future__ import annotations

import time
from typing import Iterable

import httpx
import pandas as pd
from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.instruments import Instrument
from nautilus_trader.model.objects import Price, Quantity


_REST_URL = "https://fapi.binance.com/fapi/v1/klines"
_BATCH_LIMIT = 1000  # Binance Futures max rows per request
_KLINE_COLS = (
    "open_time",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "close_time",
    "quote_asset_volume",
    "trades",
    "taker_buy_base_volume",
    "taker_buy_quote_volume",
    "ignore",
)


def fetch_klines_df(
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int,
    *,
    timeout_s: float = 20.0,
    pacing_s: float = 0.1,
) -> pd.DataFrame:
    """Page through `/fapi/v1/klines` and return a tidy DataFrame.

    Returns columns: open_time (UTC), open, high, low, close, volume,
    close_time (UTC), quote_asset_volume, trades, taker_buy_base_volume,
    taker_buy_quote_volume, ignore.

    `start_ms` / `end_ms` are Unix epoch milliseconds. Returns an empty
    DataFrame (with the right columns) if the venue has nothing in range.
    """
    rows: list[list] = []
    cursor = start_ms
    with httpx.Client(timeout=timeout_s) as client:
        while cursor < end_ms:
            params = {
                "symbol": symbol.upper(),
                "interval": interval,
                "startTime": cursor,
                "endTime": end_ms,
                "limit": _BATCH_LIMIT,
            }
            r = client.get(_REST_URL, params=params)
            r.raise_for_status()
            batch = r.json()
            if not batch:
                break
            rows.extend(batch)
            last_open = batch[-1][0]
            next_cursor = last_open + 1
            if next_cursor <= cursor:
                break
            cursor = next_cursor
            if len(batch) < _BATCH_LIMIT:
                break
            time.sleep(pacing_s)

    df = pd.DataFrame(rows, columns=list(_KLINE_COLS))
    if df.empty:
        return df
    for c in (
        "open",
        "high",
        "low",
        "close",
        "volume",
        "quote_asset_volume",
        "taker_buy_base_volume",
        "taker_buy_quote_volume",
    ):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    df["close_time"] = pd.to_datetime(df["close_time"], unit="ms", utc=True)
    return df


def klines_df_to_bars(
    df: pd.DataFrame,
    instrument: Instrument,
    bar_type: BarType,
) -> list[Bar]:
    """Convert a DataFrame from `fetch_klines_df` into Nautilus `Bar` objects.

    Prices/sizes are formatted to the instrument's declared precision so
    Nautilus's `Price` / `Quantity` constructors accept them without
    string-parsing surprises.
    """
    if df.empty:
        return []
    pp = instrument.price_precision
    sp = instrument.size_precision
    bars: list[Bar] = []
    for row in df.itertuples(index=False):
        ts_open = int(row.open_time.value)  # pandas Timestamp -> ns
        ts_close = int(row.close_time.value)
        bars.append(
            Bar(
                bar_type=bar_type,
                open=Price.from_str(f"{row.open:.{pp}f}"),
                high=Price.from_str(f"{row.high:.{pp}f}"),
                low=Price.from_str(f"{row.low:.{pp}f}"),
                close=Price.from_str(f"{row.close:.{pp}f}"),
                volume=Quantity.from_str(f"{row.volume:.{sp}f}"),
                ts_event=ts_open,
                ts_init=ts_close,
            )
        )
    return bars


def fetch_bars(
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int,
    instrument: Instrument,
    bar_type: BarType,
) -> list[Bar]:
    """One-shot: fetch klines and convert to `Bar` objects in one call."""
    df = fetch_klines_df(symbol, interval, start_ms, end_ms)
    return klines_df_to_bars(df, instrument, bar_type)


def fetch_bars_chunks(
    symbol: str,
    interval: str,
    chunks_ns: Iterable[tuple[int, int]],
    instrument: Instrument,
    bar_type: BarType,
) -> list[Bar]:
    """Fetch multiple `(start_ns, end_ns)` chunks and merge the results.

    Convenience wrapper so the CLI can feed `missing_intervals(...)`
    output straight through; chunks are converted from ns to ms for
    Binance's REST contract internally.
    """
    out: list[Bar] = []
    for start_ns, end_ns in chunks_ns:
        start_ms = start_ns // 1_000_000
        end_ms = end_ns // 1_000_000
        out.extend(fetch_bars(symbol, interval, start_ms, end_ms, instrument, bar_type))
    return out
