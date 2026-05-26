"""Binance USDS-M futures mainnet bootstrap hook (Phase 6 Task T1).

What this module does
---------------------
At supervisor start time the operator needs every mainnet instrument
configured to the right leverage and margin type **before** any
order can be placed. Binance does not let you "default" these in the
venue config — they are per-symbol REST calls (`futures_change_lev-
erage` and `futures_change_margin_type`), and you cannot reliably
recover from a wrong default at fill time.

This hook is the smallest possible orchestrator over the venue
client:

  for symbol, overrides in mainnet_instrument_overrides.items():
      client.futures_change_leverage(symbol=symbol, leverage=L)
      client.futures_change_margin_type(symbol=symbol, marginType=...)

It is intentionally **client-duck-typed** so the orchestration is
testable offline (the production caller wires in the python-binance
client constructed from `BinanceFuturesConfig` credentials; tests
inject a recording fake).

Generic-by-default design (brief v2.1)
-------------------------------------
The hook does NOT hard-code SPCXUSDT. It reads its instruction set
from a yaml-supplied mapping:

  mainnet_instrument_overrides:
    SPCXUSDT:
      leverage: 1
      margin_type: ISOLATED

so that next-instance trades (BTCUSDT, ETHUSDT, ...) require a yaml
edit and zero code change.

Error model
-----------
Any failure — REST error, mismatch between the configured leverage
and the value Binance echoes back, unknown margin type — raises
`MainnetVenueBootstrapError`. The supervisor's caller surfaces this
as a startup-time crash before any side effect (cursor open,
ready-notify, etc.).
"""

from __future__ import annotations

import dataclasses
from typing import Any, Iterable, Mapping, Protocol


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class MainnetVenueBootstrapError(RuntimeError):
    """Failure during `apply_instrument_overrides`. Message includes
    the symbol + the operation that failed so an operator reading the
    supervisor crash log can immediately tell which knob is wrong."""


# ---------------------------------------------------------------------------
# Override schema
# ---------------------------------------------------------------------------


_VALID_MARGIN_TYPES: frozenset[str] = frozenset({"ISOLATED", "CROSSED"})


@dataclasses.dataclass(frozen=True)
class InstrumentOverride:
    """Per-instrument mainnet bootstrap settings."""

    symbol: str
    leverage: int
    margin_type: str

    def __post_init__(self) -> None:
        if not self.symbol:
            raise ValueError("symbol must be a non-empty string")
        if not isinstance(self.leverage, int):
            raise ValueError(
                f"leverage must be an int, got {type(self.leverage).__name__}"
            )
        if self.leverage < 1 or self.leverage > 125:
            raise ValueError(
                f"leverage must be in [1, 125], got {self.leverage}"
            )
        if self.margin_type not in _VALID_MARGIN_TYPES:
            raise ValueError(
                f"margin_type must be one of {sorted(_VALID_MARGIN_TYPES)}, "
                f"got {self.margin_type!r}"
            )


def parse_instrument_overrides(
    raw: Mapping[str, Any] | None,
) -> tuple[InstrumentOverride, ...]:
    """Parse the `mainnet_instrument_overrides:` block from
    supervisor.yaml into a tuple of validated `InstrumentOverride`
    records.

    Empty / missing block returns `()` — the supervisor then knows
    there is nothing to configure and skips the bootstrap call.
    """
    if not raw:
        return ()
    if not isinstance(raw, Mapping):
        raise MainnetVenueBootstrapError(
            f"mainnet_instrument_overrides must be a mapping, "
            f"got {type(raw).__name__}"
        )
    overrides: list[InstrumentOverride] = []
    for symbol, body in raw.items():
        if not isinstance(symbol, str) or not symbol:
            raise MainnetVenueBootstrapError(
                f"mainnet_instrument_overrides key must be a non-empty "
                f"string, got {symbol!r}"
            )
        if not isinstance(body, Mapping):
            raise MainnetVenueBootstrapError(
                f"mainnet_instrument_overrides[{symbol!r}] must be a "
                f"mapping, got {type(body).__name__}"
            )
        for required in ("leverage", "margin_type"):
            if required not in body:
                raise MainnetVenueBootstrapError(
                    f"mainnet_instrument_overrides[{symbol!r}] is missing "
                    f"required field {required!r}"
                )
        try:
            overrides.append(
                InstrumentOverride(
                    symbol=symbol,
                    leverage=int(body["leverage"]),
                    margin_type=str(body["margin_type"]).upper(),
                )
            )
        except (ValueError, TypeError) as exc:
            raise MainnetVenueBootstrapError(
                f"mainnet_instrument_overrides[{symbol!r}]: {exc}"
            ) from exc
    return tuple(overrides)


# ---------------------------------------------------------------------------
# Client protocol (duck-typed; python-binance Client matches)
# ---------------------------------------------------------------------------


class _FuturesClient(Protocol):
    def futures_change_leverage(
        self, *, symbol: str, leverage: int
    ) -> Mapping[str, Any]: ...

    def futures_change_margin_type(
        self, *, symbol: str, marginType: str
    ) -> Mapping[str, Any]: ...


# ---------------------------------------------------------------------------
# Hook
# ---------------------------------------------------------------------------


def apply_instrument_overrides(
    client: _FuturesClient,
    overrides: Iterable[InstrumentOverride],
) -> None:
    """Apply each `InstrumentOverride` to `client` exactly once.

    Order: leverage first, then margin_type — matches the python-binance
    examples and Binance's own dashboard "set leverage then set
    isolated" flow.

    Verification: after `futures_change_leverage`, the returned
    payload's `leverage` field (if present) is compared against the
    requested value; any mismatch raises `MainnetVenueBootstrapError`.

    Note: Binance returns HTTP 200 with `{"code": -4046, ...}` if the
    margin type is already set to the requested value. We treat that
    specific code as a benign no-op (idempotency); any other non-200
    or non-200-like error raises.
    """
    for ov in overrides:
        # --- leverage ---------------------------------------------------
        try:
            lev_resp = client.futures_change_leverage(
                symbol=ov.symbol, leverage=ov.leverage
            )
        except Exception as exc:  # noqa: BLE001  — surface any client error
            raise MainnetVenueBootstrapError(
                f"futures_change_leverage failed for {ov.symbol}: "
                f"{type(exc).__name__}: {exc}"
            ) from exc

        # python-binance returns `{"leverage": int, "maxNotionalValue": str,
        # "symbol": str}`. Verify echo-back if present.
        if isinstance(lev_resp, Mapping) and "leverage" in lev_resp:
            try:
                echoed = int(lev_resp["leverage"])
            except (ValueError, TypeError) as exc:
                raise MainnetVenueBootstrapError(
                    f"futures_change_leverage returned non-int leverage "
                    f"for {ov.symbol}: {lev_resp!r}"
                ) from exc
            if echoed != ov.leverage:
                raise MainnetVenueBootstrapError(
                    f"leverage echo-back mismatch for {ov.symbol}: "
                    f"requested {ov.leverage}, got {echoed}"
                )

        # --- margin type ------------------------------------------------
        try:
            mt_resp = client.futures_change_margin_type(
                symbol=ov.symbol, marginType=ov.margin_type
            )
        except Exception as exc:  # noqa: BLE001
            # python-binance raises `BinanceAPIException` for `-4046`
            # ("no need to change margin type"). We sniff for that
            # error code on the exception to keep the hook idempotent
            # without importing binance.exceptions here.
            code = getattr(exc, "code", None)
            if code == -4046:
                continue
            code_part = f" code={code}" if code is not None else ""
            raise MainnetVenueBootstrapError(
                f"futures_change_margin_type failed for {ov.symbol}: "
                f"{type(exc).__name__}{code_part}: {exc}"
            ) from exc

        if isinstance(mt_resp, Mapping):
            code = mt_resp.get("code")
            if code is not None and code != 200 and code != -4046:
                msg = mt_resp.get("msg", "")
                raise MainnetVenueBootstrapError(
                    f"futures_change_margin_type non-OK code for "
                    f"{ov.symbol}: code={code} msg={msg!r}"
                )
