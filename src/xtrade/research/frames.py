"""ParquetDataCatalog → pandas DataFrame bridge for Phase 2 scanners.

Scanners are vectorbt-driven and want plain pandas DataFrames with a UTC
`DatetimeIndex`, not Nautilus `Bar` objects. This module adapts the
catalog read path into that shape.

Two entry points:

  - `bars_to_dataframe(catalog, bar_type, ...)`
      Single symbol → DataFrame with columns
      `[open, high, low, close, volume]`. Used by indicator-heavy
      scanners that need the full OHLCV.

  - `bars_to_panel(catalog, bar_types, ..., field="close")`
      Multi-symbol → DataFrame whose columns are `str(InstrumentId)`
      and whose values are the chosen OHLCV field. Indices are the
      *outer join* of all per-symbol timestamps; missing cells stay
      NaN (the brief says: do not forward-fill — let scanner decide).

Time-zone contract: every returned `DatetimeIndex` is `UTC`. Nautilus
stores `ts_event` as nanoseconds-since-epoch in UTC, and we surface that
verbatim.

Phase 2 brief §6: this module is read-only against the catalog — it
never imports from `xtrade.live.*` and never calls `write_bars`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import pandas as pd
from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.persistence.catalog import ParquetDataCatalog

from xtrade.data.catalog import open_catalog, read_bars


_OHLCV_COLUMNS: tuple[str, ...] = ("open", "high", "low", "close", "volume")
Field = Literal["open", "high", "low", "close", "volume"]


def _ensure_catalog(
    catalog: ParquetDataCatalog | Path | str | None,
) -> ParquetDataCatalog:
    """Accept either an open catalog or a path; return an open catalog.

    This dual-typed adapter keeps callers from having to import
    `open_catalog` themselves when they already have a `Path`."""
    if isinstance(catalog, ParquetDataCatalog):
        return catalog
    return open_catalog(catalog)


def _bars_to_records(bars: list[Bar]) -> list[dict[str, float | int]]:
    """Convert Nautilus `Bar` objects to plain Python dicts ready for
    `pd.DataFrame.from_records`. Prices and Quantity are coerced via
    `as_double()` — fine for vectorbt math, lossy for venue tick rules
    (which scanners don't care about)."""
    return [
        {
            "ts_event": b.ts_event,
            "open": b.open.as_double(),
            "high": b.high.as_double(),
            "low": b.low.as_double(),
            "close": b.close.as_double(),
            "volume": b.volume.as_double(),
        }
        for b in bars
    ]


def bars_to_dataframe(
    catalog: ParquetDataCatalog | Path | str | None,
    bar_type: BarType,
    *,
    since_ns: int | None = None,
    until_ns: int | None = None,
) -> pd.DataFrame:
    """Read bars for one `BarType` and return a UTC-indexed OHLCV DataFrame.

    Empty catalog or empty range → empty DataFrame with the canonical
    `[open, high, low, close, volume]` column set and a UTC index
    (so callers can rely on `.columns` and `.index.tz` regardless of
    fill state).

    Index is unique and monotonic — duplicate bars from a misbehaving
    catalog are de-duped by keeping the *first* occurrence per ts_event.
    """
    cat = _ensure_catalog(catalog)
    bars = read_bars(cat, bar_type, start_ns=since_ns, end_ns=until_ns)
    if not bars:
        idx = pd.DatetimeIndex([], tz="UTC", name="ts_event")
        return pd.DataFrame({c: [] for c in _OHLCV_COLUMNS}, index=idx)

    df = pd.DataFrame.from_records(_bars_to_records(bars))
    df["ts_event"] = pd.to_datetime(df["ts_event"], unit="ns", utc=True)
    df = df.drop_duplicates(subset="ts_event", keep="first")
    df = df.set_index("ts_event").sort_index()
    df.index.name = "ts_event"
    # Guarantee column order.
    return df[list(_OHLCV_COLUMNS)]


def bars_to_panel(
    catalog: ParquetDataCatalog | Path | str | None,
    bar_types: list[BarType],
    *,
    since_ns: int | None = None,
    until_ns: int | None = None,
    field: Field = "close",
) -> pd.DataFrame:
    """Multi-symbol panel: one column per `BarType.instrument_id`.

    Columns are sorted to match the order of `bar_types` (caller-defined),
    not alphabetical. The index is the outer join of every constituent's
    `ts_event` set, sorted ascending, UTC. Missing cells stay NaN —
    scanners decide whether to drop, forward-fill, or treat as flat.

    `field` must be one of the OHLCV columns. The default `"close"` is
    what momentum / mean-reversion / breakout scanners want; cointegration
    scanners can ask for `"close"` of two symbols and compute the spread
    themselves.
    """
    if field not in _OHLCV_COLUMNS:
        raise ValueError(
            f"field must be one of {_OHLCV_COLUMNS}, got {field!r}"
        )
    if not bar_types:
        idx = pd.DatetimeIndex([], tz="UTC", name="ts_event")
        return pd.DataFrame(index=idx)

    cat = _ensure_catalog(catalog)
    series_by_symbol: dict[str, pd.Series] = {}
    for bt in bar_types:
        col = str(bt.instrument_id)
        if col in series_by_symbol:
            raise ValueError(
                f"duplicate instrument {col!r} in bar_types; the panel would "
                f"have duplicate columns"
            )
        df = bars_to_dataframe(cat, bt, since_ns=since_ns, until_ns=until_ns)
        series_by_symbol[col] = df[field].rename(col)

    panel = pd.concat(series_by_symbol.values(), axis=1, join="outer")
    panel = panel.sort_index()
    # `pd.concat` will already preserve UTC tz when all series carry it,
    # but if every per-symbol frame was empty the resulting index can lose
    # tz info — re-impose it for a stable contract.
    if panel.index.tz is None:
        panel.index = panel.index.tz_localize("UTC")
    panel.index.name = "ts_event"
    return panel
