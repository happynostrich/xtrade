"""Instrument metadata package (Phase 6 Task T3).

Holds **off-venue** static facts about a tradable instrument that the
exchange's `exchangeInfo` does not carry (e.g. `shares_outstanding`
for pre-IPO perpetuals, IPO/anchor mode), plus thin helpers to
convert between price and implied market cap.

The runtime contract is "yaml → frozen dataclass": every field is
loaded from `config/instrument_meta.yaml`, coerced to `Decimal` where
appropriate, and exposed read-only through `MetaRegistry`.
"""

from xtrade.instruments.meta import (
    InstrumentMeta,
    InstrumentMetaError,
    InstrumentMetaStaleError,
    InstrumentNotFoundError,
    MetaRegistry,
    load_instrument_meta,
    mcap_from_price,
    price_from_mcap,
    quantize_qty,
)

__all__ = [
    "InstrumentMeta",
    "InstrumentMetaError",
    "InstrumentMetaStaleError",
    "InstrumentNotFoundError",
    "MetaRegistry",
    "load_instrument_meta",
    "mcap_from_price",
    "price_from_mcap",
    "quantize_qty",
]
