"""Hyperliquid HIP-3 candle snapshot fetcher.

Phase 1 Task 4 (P3) — wraps Hyperliquid's `POST /info` endpoint with
`{"type": "candleSnapshot", "req": {...}}`. trade.xyz symbols use the
HIP-3 `dex:TICKER` syntax (e.g. `xyz:TSLA`); the venue currently lives
on Hyperliquid mainnet only (Phase 0 C4b/C5 verified).

HL returns at most 5000 candles per call, so this module pages the
request by `(end - start) / interval_ms` chunks.
"""

from __future__ import annotations

import time
from typing import Iterable

import httpx
import pandas as pd
from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.instruments import Instrument
from nautilus_trader.model.objects import Price, Quantity


_INFO_URL_MAINNET = "https://api.hyperliquid.xyz/info"
_INFO_URL_TESTNET = "https://api.hyperliquid-testnet.xyz/info"
# HL caps a single candleSnapshot at 5000 candles.
_BATCH_LIMIT = 5000
# Interval -> milliseconds (mirrors Binance kline conventions; HL supports
# the same `1m,5m,15m,30m,1h,4h,1d` set).
_INTERVAL_MS = {
    "1m": 60_000,
    "3m": 180_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
    "2h": 7_200_000,
    "4h": 14_400_000,
    "8h": 28_800_000,
    "12h": 43_200_000,
    "1d": 86_400_000,
}


def _interval_ms(interval: str) -> int:
    try:
        return _INTERVAL_MS[interval]
    except KeyError as exc:
        raise ValueError(
            f"Unsupported Hyperliquid candle interval {interval!r}. "
            f"Allowed: {sorted(_INTERVAL_MS)}"
        ) from exc


def fetch_candles_df(
    dex: str,
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int,
    *,
    mainnet: bool = True,
    timeout_s: float = 20.0,
    pacing_s: float = 0.1,
) -> pd.DataFrame:
    """Page through HL's `candleSnapshot` and return a tidy DataFrame.

    `symbol` accepts either bare `"TSLA"` or already-prefixed `"xyz:TSLA"`.
    HIP-3 perps require the `dex:` prefix in the `coin` request field.

    Returns columns: open_time (UTC), close_time (UTC), open, high, low,
    close, volume, trades. Empty DataFrame if the venue has nothing in range.
    """
    url = _INFO_URL_MAINNET if mainnet else _INFO_URL_TESTNET
    if ":" in symbol:
        coin = symbol
    else:
        coin = f"{dex}:{symbol}"
    step_ms = _interval_ms(interval)
    batch_ms = step_ms * _BATCH_LIMIT

    rows: list[dict] = []
    cursor = start_ms
    with httpx.Client(timeout=timeout_s) as client:
        while cursor < end_ms:
            window_end = min(cursor + batch_ms, end_ms)
            body = {
                "type": "candleSnapshot",
                "req": {
                    "coin": coin,
                    "interval": interval,
                    "startTime": cursor,
                    "endTime": window_end,
                },
            }
            r = client.post(url, json=body)
            r.raise_for_status()
            batch = r.json() or []
            if not batch:
                # Advance cursor anyway so we don't loop forever on a sparse range.
                cursor = window_end
                continue
            rows.extend(batch)
            last_open = int(batch[-1]["t"])
            next_cursor = last_open + step_ms
            if next_cursor <= cursor:
                break
            cursor = next_cursor
            if len(batch) < _BATCH_LIMIT:
                # Partial fill -> we caught up to end_ms or nothing more available.
                if cursor < end_ms:
                    continue
                break
            time.sleep(pacing_s)

    if not rows:
        return pd.DataFrame(
            columns=["open_time", "close_time", "open", "high", "low", "close", "volume", "trades"]
        )

    df = pd.DataFrame(rows)
    df = df.rename(
        columns={
            "t": "open_time",
            "T": "close_time",
            "o": "open",
            "h": "high",
            "l": "low",
            "c": "close",
            "v": "volume",
            "n": "trades",
        }
    )
    for c in ("open", "high", "low", "close", "volume"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    df["close_time"] = pd.to_datetime(df["close_time"], unit="ms", utc=True)
    return df[["open_time", "close_time", "open", "high", "low", "close", "volume", "trades"]]


def candles_df_to_bars(
    df: pd.DataFrame,
    instrument: Instrument,
    bar_type: BarType,
) -> list[Bar]:
    """Convert a `fetch_candles_df` frame into Nautilus `Bar` objects."""
    if df.empty:
        return []
    pp = instrument.price_precision
    sp = instrument.size_precision
    bars: list[Bar] = []
    for row in df.itertuples(index=False):
        ts_open = int(row.open_time.value)
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
    dex: str,
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int,
    instrument: Instrument,
    bar_type: BarType,
    *,
    mainnet: bool = True,
) -> list[Bar]:
    df = fetch_candles_df(dex, symbol, interval, start_ms, end_ms, mainnet=mainnet)
    return candles_df_to_bars(df, instrument, bar_type)


def fetch_bars_chunks(
    dex: str,
    symbol: str,
    interval: str,
    chunks_ns: Iterable[tuple[int, int]],
    instrument: Instrument,
    bar_type: BarType,
    *,
    mainnet: bool = True,
) -> list[Bar]:
    out: list[Bar] = []
    for start_ns, end_ns in chunks_ns:
        start_ms = start_ns // 1_000_000
        end_ms = end_ns // 1_000_000
        out.extend(
            fetch_bars(
                dex, symbol, interval, start_ms, end_ms, instrument, bar_type, mainnet=mainnet
            )
        )
    return out
