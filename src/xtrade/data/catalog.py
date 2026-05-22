"""ParquetDataCatalog wrapper for xtrade.

Phase 1 Task 4 (P3) — thin helpers around
`nautilus_trader.persistence.catalog.ParquetDataCatalog`:

  - `default_catalog_path()`  -> repo-root-relative default
  - `open_catalog(path)`       -> construct + ensure directory
  - `parse_bar_spec(text)`     -> "1m" / "5m" / "1h" / "1d" -> BarSpecification
  - `bar_type_for(instrument, spec)` -> BarType (EXTERNAL aggregation)
  - `write_bars(catalog, instrument, bars)` -> idempotent write
  - `read_bars(catalog, bar_type)`
  - `missing_intervals(catalog, bar_type, start_ns, end_ns)`
  - `intervals_for(catalog, bar_type)`

Idempotency relies on Nautilus's per-file naming convention
`<start_ns>_<end_ns>.parquet` — re-writing identical ranges is a no-op
(catalog logs "already exists, skipping write").
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from nautilus_trader.model.data import Bar, BarSpecification, BarType
from nautilus_trader.model.enums import AggregationSource, BarAggregation, PriceType
from nautilus_trader.model.instruments import Instrument
from nautilus_trader.persistence.catalog import ParquetDataCatalog


def default_catalog_path() -> Path:
    """Return `<repo>/data/catalog`."""
    return Path(__file__).resolve().parents[3] / "data" / "catalog"


def open_catalog(path: Path | str | None = None) -> ParquetDataCatalog:
    """Return a `ParquetDataCatalog` rooted at `path` (or the default).

    Creates the directory if missing.
    """
    root = Path(path) if path is not None else default_catalog_path()
    root.mkdir(parents=True, exist_ok=True)
    return ParquetDataCatalog(root)


# Bar spec parsing ----------------------------------------------------------

_AGGREGATION_BY_UNIT = {
    "s": BarAggregation.SECOND,
    "m": BarAggregation.MINUTE,
    "h": BarAggregation.HOUR,
    "d": BarAggregation.DAY,
}


@dataclass(frozen=True)
class ParsedBarSpec:
    step: int
    unit: str  # one of "s","m","h","d"

    @property
    def aggregation(self) -> BarAggregation:
        return _AGGREGATION_BY_UNIT[self.unit]

    def to_spec(self, price_type: PriceType = PriceType.LAST) -> BarSpecification:
        return BarSpecification(step=self.step, aggregation=self.aggregation, price_type=price_type)

    def binance_interval(self) -> str:
        """Return the Binance kline interval string (e.g. `1m`, `5m`, `1h`)."""
        return f"{self.step}{self.unit}"

    def hyperliquid_interval(self) -> str:
        """Return the Hyperliquid candle interval string (matches Binance form)."""
        return f"{self.step}{self.unit}"

    def to_milliseconds(self) -> int:
        """Return the bar period in milliseconds (handy for paging math)."""
        factor = {"s": 1_000, "m": 60_000, "h": 3_600_000, "d": 86_400_000}[self.unit]
        return self.step * factor


def parse_bar_spec(text: str) -> ParsedBarSpec:
    """Parse `"1m"`, `"5m"`, `"1h"`, `"1d"` (Binance-style) into a `ParsedBarSpec`.

    Raises `ValueError` on malformed input. Phase 1 supports the four units
    above; tick / volume / range aggregations are deferred.
    """
    s = text.strip().lower()
    if len(s) < 2:
        raise ValueError(f"bar spec must be like '1m', '5m', '1h', got {text!r}")
    unit = s[-1]
    if unit not in _AGGREGATION_BY_UNIT:
        raise ValueError(
            f"bar spec unit must be one of s/m/h/d, got {unit!r} (in {text!r})"
        )
    try:
        step = int(s[:-1])
    except ValueError as exc:
        raise ValueError(f"bar spec step must be an int, got {s[:-1]!r}") from exc
    if step <= 0:
        raise ValueError(f"bar spec step must be positive, got {step}")
    return ParsedBarSpec(step=step, unit=unit)


def bar_type_for(instrument: Instrument, spec: ParsedBarSpec) -> BarType:
    """Build the EXTERNAL-aggregation `BarType` used by ingest + backtest."""
    return BarType(
        instrument_id=instrument.id,
        bar_spec=spec.to_spec(),
        aggregation_source=AggregationSource.EXTERNAL,
    )


# Catalog I/O ---------------------------------------------------------------


def write_bars(
    catalog: ParquetDataCatalog,
    instrument: Instrument,
    bars: list[Bar],
) -> int:
    """Write `bars` to `catalog`, ensuring the instrument definition is also
    persisted. Returns the number of bars actually written (after filtering
    out any that fall inside already-covered intervals).

    Idempotency: Nautilus partitions parquet files by `ts_init`. Re-running
    ingest can produce sub-bar `missing_intervals` (because catalog ranges
    start at the first bar's `ts_init`, not its event time), so we filter
    out bars whose `ts_init` is already covered before delegating to
    `ParquetDataCatalog.write_data`, which would otherwise raise on
    non-disjoint writes.

    All bars must share one `bar_type` (Nautilus enforces this in
    write_data).
    """
    if not bars:
        return 0
    bar_type = bars[0].bar_type
    existing = catalog.get_intervals(Bar, str(bar_type))
    if existing:
        def _covered(ts_init: int) -> bool:
            return any(lo <= ts_init <= hi for lo, hi in existing)

        bars = [b for b in bars if not _covered(b.ts_init)]
    if not bars:
        return 0
    catalog.write_data([instrument])
    catalog.write_data(bars)
    return len(bars)


def read_bars(
    catalog: ParquetDataCatalog,
    bar_type: BarType,
    *,
    start_ns: int | None = None,
    end_ns: int | None = None,
) -> list[Bar]:
    """Read bars matching `bar_type` from `catalog`. `start_ns` / `end_ns`
    are passed through to Nautilus's range filter (inclusive)."""
    return catalog.bars(
        bar_types=[str(bar_type)],
        start=start_ns,
        end=end_ns,
    )


def intervals_for(catalog: ParquetDataCatalog, bar_type: BarType) -> list[tuple[int, int]]:
    """Return the list of `(start_ns, end_ns)` covered for this bar_type."""
    return catalog.get_intervals(Bar, str(bar_type))


def missing_intervals(
    catalog: ParquetDataCatalog,
    bar_type: BarType,
    start_ns: int,
    end_ns: int,
) -> list[tuple[int, int]]:
    """Return ranges in `[start_ns, end_ns]` not yet covered for this bar_type.

    Returned as a list of (start_ns, end_ns) tuples; empty list means the
    requested range is fully covered (so ingest should be a no-op).
    """
    return catalog.get_missing_intervals_for_request(
        start_ns,
        end_ns,
        Bar,
        str(bar_type),
    )
