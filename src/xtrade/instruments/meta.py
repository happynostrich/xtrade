"""`InstrumentMeta` + `MetaRegistry` — Phase 6 Task T3.

Why this module exists
----------------------
Binance's `exchangeInfo` is enough to size a *normal* perpetual: it
gives `min_qty`, `qty_step`, `tick_size`. It is **not** enough for the
SPCXUSDT class of instruments where the strategy reasons about
**implied market cap** (`price * shares_outstanding`) — `shares_out-
standing` is a fact about the underlying, not about the contract, and
Binance does not publish it.

T3 is the minimum-viable home for those off-venue static facts:

- Keyed by full `<symbol>.<venue>` (e.g. `SPCXUSDT-PERP.BINANCE`) so
  the same registry can serve mainnet / testnet / future venues.
- Every numeric field stored as `Decimal` (never `float`) to keep
  price ↔ mcap conversions exact.
- Loaded once from `config/instrument_meta.yaml`, then read-only.

Brief §5 T3 fixes the public surface:
  * `InstrumentMeta` frozen dataclass (symbol, shares_outstanding,
    min_qty, qty_step, tick_size, mark_source)
  * `MetaRegistry.load(path)` / `MetaRegistry.get(symbol)`
  * `mcap_from_price(price, meta)` / `price_from_mcap(mcap, meta)`

We additionally expose `quantize_qty(qty, meta)` (round-down to
qty_step multiples — brief §5 T3 lists the test) and the staleness
exception `InstrumentMetaStaleError` which scanner-side sanity checks
raise when the recorded `shares_outstanding` drifts > 5% from the
public-source ground truth.
"""

from __future__ import annotations

import dataclasses
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class InstrumentMetaError(RuntimeError):
    """Base error for `xtrade.instruments.meta`."""


class InstrumentNotFoundError(InstrumentMetaError):
    """`MetaRegistry.get(symbol)` lookup miss."""


class InstrumentMetaStaleError(InstrumentMetaError):
    """Raised by scanner-side sanity checks when `shares_outstanding`
    in `instrument_meta.yaml` differs from the public source by more
    than the configured tolerance (brief §1 row 9 / §5 T3)."""


# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------


_REQUIRED_FIELDS: tuple[str, ...] = (
    "shares_outstanding",
    "min_qty",
    "qty_step",
    "tick_size",
    "mark_source",
)

_DECIMAL_FIELDS: tuple[str, ...] = (
    "shares_outstanding",
    "min_qty",
    "qty_step",
    "tick_size",
)


@dataclasses.dataclass(frozen=True)
class InstrumentMeta:
    """Off-venue static facts about one tradable instrument.

    `symbol` is the full `<contract>.<venue>` key as stored in
    `instrument_meta.yaml` (e.g. `SPCXUSDT-PERP.BINANCE`). All numeric
    fields are `Decimal`; string fields like `mark_source` are open
    enums (`"oracle"` / `"spot_anchor"` / venue-specific).
    """

    symbol: str
    shares_outstanding: Decimal
    min_qty: Decimal
    qty_step: Decimal
    tick_size: Decimal
    mark_source: str

    def __post_init__(self) -> None:
        for field in _DECIMAL_FIELDS:
            value = getattr(self, field)
            if not isinstance(value, Decimal):
                object.__setattr__(self, field, Decimal(str(value)))
        if self.shares_outstanding <= 0:
            raise ValueError(
                f"shares_outstanding must be > 0, got {self.shares_outstanding}"
            )
        if self.min_qty <= 0:
            raise ValueError(f"min_qty must be > 0, got {self.min_qty}")
        if self.qty_step <= 0:
            raise ValueError(f"qty_step must be > 0, got {self.qty_step}")
        if self.tick_size <= 0:
            raise ValueError(f"tick_size must be > 0, got {self.tick_size}")
        if not self.symbol:
            raise ValueError("symbol must be a non-empty string")
        if not self.mark_source:
            raise ValueError("mark_source must be a non-empty string")


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class MetaRegistry:
    """In-memory lookup from `<symbol>.<venue>` → `InstrumentMeta`.

    Build via `MetaRegistry.load(path)`; query via `get(symbol)`. The
    registry is intentionally immutable after construction — callers
    that need to mutate must build a new registry from a new yaml.
    """

    def __init__(self, entries: dict[str, InstrumentMeta]) -> None:
        self._entries: dict[str, InstrumentMeta] = dict(entries)

    @classmethod
    def load(cls, path: Path | str) -> "MetaRegistry":
        return load_instrument_meta(path)

    def get(self, symbol: str) -> InstrumentMeta:
        try:
            return self._entries[symbol]
        except KeyError:
            known = ", ".join(sorted(self._entries)) or "<empty>"
            raise InstrumentNotFoundError(
                f"instrument meta not found for {symbol!r}; known: {known}"
            ) from None

    def __contains__(self, symbol: object) -> bool:
        return symbol in self._entries

    def __len__(self) -> int:
        return len(self._entries)

    def symbols(self) -> tuple[str, ...]:
        return tuple(sorted(self._entries))


# ---------------------------------------------------------------------------
# YAML loader
# ---------------------------------------------------------------------------


def load_instrument_meta(path: Path | str) -> MetaRegistry:
    """Parse `instrument_meta.yaml` → `MetaRegistry`.

    Raises:
        FileNotFoundError: if the yaml does not exist.
        InstrumentMetaError: on malformed top-level structure, missing
            required fields, or unparsable numeric values.
    """
    import yaml

    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"instrument meta yaml not found: {path}")

    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if raw is None:
        return MetaRegistry({})
    if not isinstance(raw, dict):
        raise InstrumentMetaError(
            f"instrument meta yaml root must be a mapping, got {type(raw).__name__}"
        )

    entries: dict[str, InstrumentMeta] = {}
    for symbol, body in raw.items():
        if not isinstance(symbol, str) or not symbol:
            raise InstrumentMetaError(
                f"instrument meta key must be a non-empty string, got {symbol!r}"
            )
        if not isinstance(body, dict):
            raise InstrumentMetaError(
                f"instrument meta entry {symbol!r} must be a mapping, "
                f"got {type(body).__name__}"
            )
        missing = [f for f in _REQUIRED_FIELDS if f not in body]
        if missing:
            raise InstrumentMetaError(
                f"instrument meta entry {symbol!r} is missing required "
                f"field(s): {missing}"
            )
        kwargs: dict[str, Any] = {"symbol": symbol}
        for field in _DECIMAL_FIELDS:
            try:
                kwargs[field] = Decimal(str(body[field]))
            except (ValueError, ArithmeticError) as exc:
                raise InstrumentMetaError(
                    f"instrument meta entry {symbol!r} field {field!r} "
                    f"is not a valid decimal: {body[field]!r}"
                ) from exc
        kwargs["mark_source"] = str(body["mark_source"])
        entries[symbol] = InstrumentMeta(**kwargs)

    return MetaRegistry(entries)


# ---------------------------------------------------------------------------
# Helpers — price ↔ mcap + qty quantization
# ---------------------------------------------------------------------------


def mcap_from_price(price: Decimal, meta: InstrumentMeta) -> Decimal:
    """Implied market cap = price × shares_outstanding (exact Decimal)."""
    if not isinstance(price, Decimal):
        price = Decimal(str(price))
    return price * meta.shares_outstanding


def price_from_mcap(mcap: Decimal, meta: InstrumentMeta) -> Decimal:
    """Inverse of `mcap_from_price`. Caller is responsible for any
    rounding to `tick_size` (we keep full Decimal precision here)."""
    if not isinstance(mcap, Decimal):
        mcap = Decimal(str(mcap))
    return mcap / meta.shares_outstanding


def quantize_qty(qty: Decimal, meta: InstrumentMeta) -> Decimal:
    """Round `qty` *down* to the nearest multiple of `meta.qty_step`.

    Used at order build time so we never submit a fractional step the
    venue would reject. `Decimal` arithmetic + `ROUND_DOWN` keeps this
    deterministic.
    """
    if not isinstance(qty, Decimal):
        qty = Decimal(str(qty))
    step = meta.qty_step
    if qty <= 0:
        return Decimal(0)
    # floor(qty / step) * step  — done in Decimal to avoid float drift.
    steps = (qty / step).quantize(Decimal(1), rounding=ROUND_DOWN)
    return steps * step
