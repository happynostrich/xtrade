"""Instrument resolvers for Phase 1 ingest / backtest.

The catalog stores `Bar` objects keyed by `BarType`, which in turn is
keyed by `InstrumentId`. To write bars we therefore need a Nautilus
`Instrument` with the right `price_precision` / `size_precision` etc.

For Phase 1 we resolve instruments by `(venue, symbol)`:

  - Binance USDT-M futures BTCUSDT  -> `TestInstrumentProvider.btcusdt_perp_binance()`
    (proven path, matches Phase 0 C6b backtest PASS).
  - Hyperliquid HIP-3 (`dex:TICKER`) -> built from HL `/info` `meta`
    response (`szDecimals` drives precision).

Other Binance symbols raise `NotImplementedError` for now — they'd need
either a static registry or a network lookup against Binance's
`/exchangeInfo`, which Task 4 punts on. The resolver shape is in place
so Task 4b / Task 5 follow-ups can extend it without rewriting callers.
"""

from __future__ import annotations

import time
from decimal import Decimal
from typing import Any

import httpx
from nautilus_trader.model.currencies import USDC, USDT
from nautilus_trader.model.identifiers import InstrumentId, Symbol, Venue
from nautilus_trader.model.instruments import CryptoPerpetual, Instrument
from nautilus_trader.model.objects import Currency, Money, Price, Quantity
from nautilus_trader.test_kit.providers import TestInstrumentProvider


HL_VENUE = Venue("HYPERLIQUID")
BINANCE_VENUE = Venue("BINANCE")

_HL_INFO_URL_MAINNET = "https://api.hyperliquid.xyz/info"
_HL_INFO_URL_TESTNET = "https://api.hyperliquid-testnet.xyz/info"


class InstrumentResolutionError(RuntimeError):
    """Raised when no instrument definition is available for a (venue, symbol)."""


# ---------------------------------------------------------------------------
# Binance
# ---------------------------------------------------------------------------


def resolve_binance_perp(symbol: str) -> CryptoPerpetual:
    """Return the Nautilus instrument for a Binance USDT-M perpetual.

    Phase 1 supports only `BTCUSDT` — the symbol C6b's backtest proved.
    Other symbols raise `InstrumentResolutionError` with a clear next-step
    pointer; Task 5/Task 4b will plug in a real `/exchangeInfo` lookup if
    we widen the universe.
    """
    sym = symbol.upper()
    if sym == "BTCUSDT":
        return TestInstrumentProvider.btcusdt_perp_binance()
    raise InstrumentResolutionError(
        f"No Binance USDT-M perpetual definition wired up for {sym!r}. "
        f"Phase 1 ships with BTCUSDT only; extend "
        f"`xtrade.data.instruments.resolve_binance_perp` with a real "
        f"`/fapi/v1/exchangeInfo` lookup to support more symbols."
    )


# ---------------------------------------------------------------------------
# Hyperliquid HIP-3
# ---------------------------------------------------------------------------


def _fetch_hl_meta(dex: str, *, mainnet: bool, timeout_s: float = 20.0) -> dict[str, Any]:
    url = _HL_INFO_URL_MAINNET if mainnet else _HL_INFO_URL_TESTNET
    with httpx.Client(timeout=timeout_s) as client:
        r = client.post(url, json={"type": "meta", "dex": dex})
        r.raise_for_status()
        return r.json()


def resolve_hyperliquid_hip3(
    dex: str,
    symbol: str,
    *,
    mainnet: bool = True,
    quote: str = "USDC",
) -> CryptoPerpetual:
    """Return a Nautilus `CryptoPerpetual` for an HL HIP-3 ticker.

    `symbol` accepts either `"TSLA"` or `"xyz:TSLA"`. The returned
    instrument id is `xyz:TSLA-USD-PERP.HYPERLIQUID` to match the
    convention NautilusTrader's Hyperliquid adapter expects (Phase 0
    C4b/C5).

    `mainnet=True` is the read-only default: HIP-3 perp DEXes are
    mainnet-only in practice (trade.xyz lives on HL mainnet). Precision
    is derived from `szDecimals` in the HL `meta` response; price
    precision is `max(0, 6 - szDecimals)` per HL's tick-rule.
    """
    ticker = symbol.split(":", 1)[-1].upper()
    if not ticker:
        raise InstrumentResolutionError(f"Empty ticker in symbol={symbol!r}")

    meta = _fetch_hl_meta(dex, mainnet=mainnet)
    universe = meta.get("universe") or []
    entry = next((u for u in universe if u.get("name", "").upper() == ticker), None)
    if entry is None:
        raise InstrumentResolutionError(
            f"Ticker {ticker!r} not present in Hyperliquid dex {dex!r} universe. "
            f"Use `scripts/phase0/04_discover_hyperliquid_perp_dexs.py` to list."
        )
    sz_decimals = int(entry.get("szDecimals", 2))
    px_decimals = max(0, 6 - sz_decimals)
    px_increment = Decimal(1).scaleb(-px_decimals) if px_decimals > 0 else Decimal(1)
    sz_increment = Decimal(1).scaleb(-sz_decimals) if sz_decimals > 0 else Decimal(1)

    quote_ccy = USDC if quote.upper() == "USDC" else USDT

    # Synthetic "underlying" base currency. HL HIP-3 stock perps don't have
    # a deliverable base; we mint a Currency so Nautilus's bookkeeping has
    # something to label positions with.
    base_ccy = Currency(
        code=ticker,
        precision=sz_decimals,
        iso4217=0,
        name=ticker,
        currency_type=quote_ccy.currency_type,
    )

    raw_symbol = f"{dex}:{ticker}"
    instrument_id = InstrumentId(Symbol(f"{raw_symbol}-USD-PERP"), HL_VENUE)
    now_ns = time.time_ns()
    return CryptoPerpetual(
        instrument_id=instrument_id,
        raw_symbol=Symbol(raw_symbol),
        base_currency=base_ccy,
        quote_currency=quote_ccy,
        settlement_currency=quote_ccy,
        is_inverse=False,
        price_precision=px_decimals,
        price_increment=Price(float(px_increment), px_decimals),
        size_precision=sz_decimals,
        size_increment=Quantity(float(sz_increment), sz_decimals),
        max_quantity=None,
        min_quantity=Quantity(float(sz_increment), sz_decimals),
        max_notional=None,
        min_notional=Money(10.00, quote_ccy),
        max_price=None,
        min_price=None,
        margin_init=Decimal("0.05"),
        margin_maint=Decimal("0.025"),
        maker_fee=Decimal("0.0002"),
        taker_fee=Decimal("0.0005"),
        ts_event=now_ns,
        ts_init=now_ns,
    )


# ---------------------------------------------------------------------------
# Top-level dispatch
# ---------------------------------------------------------------------------


def resolve(venue: str, symbol: str, **kwargs: Any) -> Instrument:
    """Dispatch to the appropriate resolver based on a CLI-style venue tag.

    Recognised `venue` values:
      - `binance`           -> Binance USDT-M perpetual (BTCUSDT only for now)
      - `hyperliquid`       -> HL HIP-3, expects `symbol` of form `dex:TICKER`
                                (or pass `dex=` explicitly)
    """
    v = venue.lower()
    if v == "binance":
        return resolve_binance_perp(symbol)
    if v == "hyperliquid":
        if ":" in symbol:
            dex, ticker = symbol.split(":", 1)
        elif "dex" in kwargs:
            dex = kwargs["dex"]
            ticker = symbol
        else:
            raise InstrumentResolutionError(
                f"Hyperliquid HIP-3 symbol must be `dex:TICKER` form, got {symbol!r}."
            )
        mainnet = bool(kwargs.get("mainnet", True))
        return resolve_hyperliquid_hip3(dex=dex, symbol=ticker, mainnet=mainnet)
    raise InstrumentResolutionError(
        f"Unknown venue tag {venue!r}. Expected one of: binance, hyperliquid."
    )
