"""Task G (part 1) — C6: Fetch Binance historical klines.

Pulls a slice of BTCUSDT 1m klines from Binance public REST and persists
both a raw CSV (`data/binance_BTCUSDT_1m.csv`) and a NautilusTrader
`Bar` parquet (`data/binance_BTCUSDT_1m_bars.parquet`) ready for the
backtest in `08_sample_backtest.py`.

No credentials needed (public endpoint). Read-only.
"""

from __future__ import annotations

import datetime as dt
import sys
import time
from pathlib import Path

import httpx
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import append_result, stepwise  # noqa: E402


CHECK_ID = "C6a"
CHECK_NAME = "Fetch Binance historical klines"

SYMBOL = "BTCUSDT"
INTERVAL = "1m"
DAYS_BACK = 3
BATCH_LIMIT = 1000  # Binance max per request

DATA_DIR = Path(__file__).resolve().parents[2] / "data"
CSV_PATH = DATA_DIR / f"binance_{SYMBOL}_{INTERVAL}.csv"

REST_URL = "https://fapi.binance.com/fapi/v1/klines"  # USDT-M futures


def _fetch_klines(symbol: str, interval: str, start_ms: int, end_ms: int) -> pd.DataFrame:
    rows: list[list] = []
    cursor = start_ms
    with httpx.Client(timeout=20.0) as client:
        while cursor < end_ms:
            params = {
                "symbol": symbol,
                "interval": interval,
                "startTime": cursor,
                "endTime": end_ms,
                "limit": BATCH_LIMIT,
            }
            r = client.get(REST_URL, params=params)
            r.raise_for_status()
            batch = r.json()
            if not batch:
                break
            rows.extend(batch)
            # Next cursor = last open_time + 1ms
            last_open = batch[-1][0]
            next_cursor = last_open + 1
            if next_cursor <= cursor:
                break
            cursor = next_cursor
            if len(batch) < BATCH_LIMIT:
                break
            time.sleep(0.1)  # gentle pacing

    cols = [
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "quote_asset_volume", "trades",
        "taker_buy_base_volume", "taker_buy_quote_volume", "ignore",
    ]
    df = pd.DataFrame(rows, columns=cols)
    for c in ("open", "high", "low", "close", "volume",
              "quote_asset_volume", "taker_buy_base_volume", "taker_buy_quote_volume"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    df["close_time"] = pd.to_datetime(df["close_time"], unit="ms", utc=True)
    return df


def main() -> int:
    with stepwise(CHECK_ID, CHECK_NAME):
        notes: list[str] = []
        DATA_DIR.mkdir(parents=True, exist_ok=True)

        end = dt.datetime.now(tz=dt.timezone.utc).replace(second=0, microsecond=0)
        start = end - dt.timedelta(days=DAYS_BACK)
        start_ms = int(start.timestamp() * 1000)
        end_ms = int(end.timestamp() * 1000)
        print(f"Fetching {SYMBOL} {INTERVAL} from {start.isoformat()} to {end.isoformat()}")

        df = _fetch_klines(SYMBOL, INTERVAL, start_ms, end_ms)
        if df.empty:
            raise RuntimeError("No klines returned by Binance")
        df.to_csv(CSV_PATH, index=False)
        print(f"Wrote {len(df)} rows -> {CSV_PATH}")
        notes.append(f"{len(df)} klines saved to {CSV_PATH}")
        notes.append(
            f"range: {df['open_time'].min().isoformat()} .. {df['open_time'].max().isoformat()}"
        )

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
