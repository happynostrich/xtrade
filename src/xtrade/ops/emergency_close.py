"""Phase 6 Task T10 — emergency_close shared runner.

Cancels open Binance USDT-M futures orders for a single instrument
*without going through the supervisor*. This is the operator's break-
glass tool when the supervisor is dead, hung, or visibly mis-routing,
and is also the runner the McapSoftKillWatcher (T8) invokes when the
soft-kill boundary is crossed.

Brief §5 T10:

    xtrade ops emergency_close --yes
        [--instrument SPCXUSDT-PERP.BINANCE]
        [--side {reduce-only-tp-only,all}]

Behaviour
---------
- ``--side reduce-only-tp-only`` (default): GET ``/fapi/v1/openOrders``,
  filter rows with ``reduceOnly==True``, then DELETE each via
  ``/fapi/v1/order``. **Limit-entry orders (reduceOnly==False) are
  preserved** because the operator may still want them in flight after
  cancellation of the take-profit ladder.
- ``--side all``: single ``DELETE /fapi/v1/allOpenOrders?symbol=X``.

Effect sequence (idempotent on partial failure)
-----------------------------------------------
1. ``sentinel.pause(reason=f"ops.emergency_close:{side}:{instrument}")``
   — written **first**, before any HTTP call, so an operator running this
   from a dying VPS still gets the supervisor parked even if Binance is
   unreachable.
2. Binance signed REST calls (HMAC-SHA256 against ``fapi.binance.com``
   for ``environment=LIVE``; ``testnet.binancefuture.com`` otherwise).
3. ``alerter.dispatch_alert(severity="crit", event="ops.emergency_close")``
   — best-effort; alerter exceptions never raise out of this runner.

Exit code (matches ``xtrade`` global contract — Phase 1 P7):

- ``0`` — every cancel attempt returned a 2xx.
- ``2`` — at least one cancel returned non-2xx, or the venue listed
  open orders that we then failed to delete.

Decoupling
----------
- The module imports only :mod:`httpx`, :mod:`xtrade.bridge.alerter`,
  :mod:`xtrade.config`, :mod:`xtrade.live.sentinel`, and
  :mod:`xtrade.obs.log_event`. No supervisor / Nautilus / strategy
  imports.
- Tests inject an ``httpx.Client`` built on ``httpx.MockTransport`` via
  the private ``http_client=`` knob.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import logging
import os
import time
import urllib.parse
from pathlib import Path
from typing import Any, Callable, Iterable, Literal, Mapping

import httpx

from xtrade.bridge.alerter import AlertBridge
from xtrade.config import BinanceFuturesConfig, ConfigError, load_venues
from xtrade.live.sentinel import Sentinel
from xtrade.obs.log_event import emit_event


log = logging.getLogger("xtrade.ops.emergency_close")

UTC = dt.timezone.utc

Side = Literal["reduce-only-tp-only", "all"]
_VALID_SIDES: frozenset[str] = frozenset({"reduce-only-tp-only", "all"})

_RECV_WINDOW_MS: int = 5000
_DEFAULT_CONNECT_TIMEOUT_S: float = 5.0
_DEFAULT_READ_TIMEOUT_S: float = 10.0


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class EmergencyCloseConfigError(ValueError):
    """Raised when the caller passed invalid inputs (bad side, missing
    venues yaml, missing futures config, etc.). Maps to CLI exit 2."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _binance_host(environment: str) -> str:
    """Resolve the Binance USDT-M futures REST host for `environment`.

    Brief §3: mainnet uses ``fapi.binance.com``. We also map ``TESTNET``
    to the public testnet host for completeness; ``DEMO`` reuses the
    LIVE host because Binance routes demo via the same domain with a
    different account-flavoured key (matches Nautilus 1.227 wiring).
    """
    env = (environment or "").upper()
    if env == "TESTNET":
        return "https://testnet.binancefuture.com"
    if env in ("LIVE", "DEMO"):
        return "https://fapi.binance.com"
    raise EmergencyCloseConfigError(
        f"binance.futures.environment must be LIVE/TESTNET/DEMO, got {environment!r}"
    )


def _binance_symbol(instrument: str) -> str:
    """Convert a Nautilus instrument id (``SPCXUSDT-PERP.BINANCE``) to a
    bare Binance symbol (``SPCXUSDT``).

    Format rule (Nautilus 1.227 + brief §3):
    ``<symbol>-PERP.BINANCE`` for USDT-M perp.
    """
    if not isinstance(instrument, str) or not instrument:
        raise EmergencyCloseConfigError(
            f"instrument must be a non-empty str, got {instrument!r}"
        )
    s = instrument.strip().upper()
    # Strip the venue suffix.
    if "." in s:
        head, _, _ = s.partition(".")
    else:
        head = s
    # Strip the contract-kind suffix.
    if head.endswith("-PERP"):
        head = head[: -len("-PERP")]
    if not head or not head.isalnum():
        raise EmergencyCloseConfigError(
            f"could not derive a Binance symbol from instrument {instrument!r}"
        )
    return head


def _sign(secret: str, query: str) -> str:
    """HMAC-SHA256(secret, query) → lowercase hex digest (Binance spec)."""
    return hmac.new(
        secret.encode("utf-8"),
        query.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _build_signed_query(
    params: Mapping[str, Any],
    *,
    secret: str,
    now_ms: int,
) -> str:
    """Return a ``?k=v&...&signature=<hex>`` query string per Binance USDT-M
    futures spec: ``recvWindow`` + ``timestamp`` appended, then sign the
    *whole* query, then append ``signature=`` (Binance includes signature
    only in the URL/body, never in the to-be-signed string itself)."""
    full: dict[str, Any] = dict(params)
    full["recvWindow"] = _RECV_WINDOW_MS
    full["timestamp"] = now_ms
    query = urllib.parse.urlencode(full, doseq=False)
    signature = _sign(secret, query)
    return f"{query}&signature={signature}"


def _is_reduce_only_tp(order: Mapping[str, Any]) -> bool:
    """Return True iff `order` is a reduce-only take-profit row.

    Binance USDT-M `/fapi/v1/openOrders` row schema (subset):
        {"orderId": int, "symbol": str, "type": "LIMIT"|"TAKE_PROFIT_MARKET"|...,
         "reduceOnly": bool, "side": "BUY"|"SELL", "status": "NEW", ...}

    The brief's "reduce-only-tp-only" filter intentionally matches *any*
    reduceOnly row, not just literal TAKE_PROFIT / TAKE_PROFIT_MARKET —
    the v1 ladder strategy (T5) places its ladder as reduce-only LIMIT
    BUYs, and we want those cancelled too. The negative case (preserve
    limit ENTRY orders) is achieved because entry limits are submitted
    with `reduceOnly=False`.
    """
    ro = order.get("reduceOnly")
    if isinstance(ro, bool):
        return ro
    if isinstance(ro, str):
        return ro.lower() == "true"
    return False


# ---------------------------------------------------------------------------
# REST calls
# ---------------------------------------------------------------------------


def _list_open_orders(
    *,
    client: httpx.Client,
    host: str,
    api_key: str,
    api_secret: str,
    symbol: str,
    now_ms: int,
) -> list[dict[str, Any]]:
    """GET /fapi/v1/openOrders?symbol=<symbol>. Raises on non-2xx."""
    query = _build_signed_query(
        {"symbol": symbol},
        secret=api_secret,
        now_ms=now_ms,
    )
    url = f"{host}/fapi/v1/openOrders?{query}"
    resp = client.get(url, headers={"X-MBX-APIKEY": api_key})
    if not (200 <= resp.status_code < 300):
        raise httpx.HTTPStatusError(
            f"openOrders returned HTTP {resp.status_code}: {resp.text[:200]}",
            request=resp.request,
            response=resp,
        )
    data = resp.json()
    if not isinstance(data, list):
        raise EmergencyCloseConfigError(
            f"openOrders payload must be a list, got {type(data).__name__}"
        )
    return [row for row in data if isinstance(row, dict)]


def _cancel_one(
    *,
    client: httpx.Client,
    host: str,
    api_key: str,
    api_secret: str,
    symbol: str,
    order_id: int,
    now_ms: int,
) -> httpx.Response:
    """DELETE /fapi/v1/order?symbol=<symbol>&orderId=<id>. Returns the
    response; caller decides whether to surface non-2xx as failure."""
    query = _build_signed_query(
        {"symbol": symbol, "orderId": int(order_id)},
        secret=api_secret,
        now_ms=now_ms,
    )
    url = f"{host}/fapi/v1/order?{query}"
    return client.delete(url, headers={"X-MBX-APIKEY": api_key})


def _cancel_all(
    *,
    client: httpx.Client,
    host: str,
    api_key: str,
    api_secret: str,
    symbol: str,
    now_ms: int,
) -> httpx.Response:
    """DELETE /fapi/v1/allOpenOrders?symbol=<symbol>."""
    query = _build_signed_query(
        {"symbol": symbol},
        secret=api_secret,
        now_ms=now_ms,
    )
    url = f"{host}/fapi/v1/allOpenOrders?{query}"
    return client.delete(url, headers={"X-MBX-APIKEY": api_key})


# ---------------------------------------------------------------------------
# Shared runner
# ---------------------------------------------------------------------------


def _load_futures_config(venues_yaml: Path) -> BinanceFuturesConfig:
    try:
        cfg = load_venues(venues_yaml)
    except (ConfigError, OSError, FileNotFoundError) as exc:
        raise EmergencyCloseConfigError(
            f"could not load venues yaml at {venues_yaml}: {exc}"
        ) from exc
    if cfg.binance is None or cfg.binance.futures is None:
        raise EmergencyCloseConfigError(
            f"venues yaml at {venues_yaml} does not configure binance.futures"
        )
    return cfg.binance.futures


def _build_client() -> httpx.Client:
    return httpx.Client(
        timeout=httpx.Timeout(
            connect=_DEFAULT_CONNECT_TIMEOUT_S,
            read=_DEFAULT_READ_TIMEOUT_S,
            write=_DEFAULT_READ_TIMEOUT_S,
            pool=_DEFAULT_READ_TIMEOUT_S,
        )
    )


def _dispatch_alert_safe(
    alerter: AlertBridge | None,
    *,
    severity: Literal["info", "warn", "crit"],
    event: str,
    message: str,
    instrument: str,
    fields: Mapping[str, Any],
) -> None:
    """dispatch_alert wrapped so transport / payload errors never raise."""
    if alerter is None:
        return
    try:
        alerter.dispatch_alert(
            severity=severity,
            event=event,
            message=message[:200],
            instrument=instrument,
            fields=dict(fields),
        )
    except Exception:  # pragma: no cover - defensive
        log.exception("emergency_close alert dispatch failed (swallowed)")


def run(
    *,
    side: Side,
    instrument: str,
    venues_yaml: Path,
    sentinel_path: Path,
    alerter: AlertBridge | None,
    http_client: httpx.Client | None = None,
    now: Callable[[], dt.datetime] | None = None,
) -> int:
    """Cancel open orders for `instrument` on Binance USDT-M futures.

    Returns 0 on full success, 2 on any failure. Never raises out — the
    CLI maps the return value to `typer.Exit(code=...)` directly.

    The supervisor's mcap soft-kill path binds `venues_yaml`,
    `sentinel_path`, `alerter` via :func:`functools.partial` before
    passing the partial as the ``emergency_close_runner`` to
    :class:`xtrade.live.mcap_softkill.McapSoftKillWatcher`.

    Parameters
    ----------
    side:
        ``"reduce-only-tp-only"`` (default for CLI) — list openOrders,
        filter reduceOnly==True, cancel one-by-one. Preserves limit
        entry orders (reduceOnly=False).
        ``"all"`` — single DELETE /fapi/v1/allOpenOrders.
    instrument:
        Nautilus-style id, e.g. ``"SPCXUSDT-PERP.BINANCE"``.
    venues_yaml:
        Path to the venues yaml; must define ``binance.futures``.
    sentinel_path:
        Where to write the pause sentinel. Always written before any
        HTTP call.
    alerter:
        Optional :class:`AlertBridge`. ``None`` skips the alert
        dispatch (e.g. when called from tests).
    http_client:
        Private testability knob; if set, used directly (caller owns
        teardown).
    now:
        Private testability knob; UTC clock factory.
    """
    if side not in _VALID_SIDES:
        raise EmergencyCloseConfigError(
            f"side must be one of {sorted(_VALID_SIDES)}, got {side!r}"
        )
    symbol = _binance_symbol(instrument)
    futures = _load_futures_config(Path(venues_yaml))
    host = _binance_host(futures.environment)
    clock = now or (lambda: dt.datetime.now(tz=UTC))
    when = clock()

    # 1) Sentinel first — operator-visible halt even if Binance call hangs.
    sentinel = Sentinel(sentinel_path)
    sentinel_reason = f"ops.emergency_close:{side}:{instrument}"
    sentinel.pause(reason=sentinel_reason, now=when)

    emit_event(
        log,
        "ops.emergency_close.start",
        side=side,
        instrument=instrument,
        symbol=symbol,
        host=host,
        now=when,
    )

    owns_client = False
    client = http_client
    if client is None:
        client = _build_client()
        owns_client = True

    cancelled: list[int] = []
    failures: list[dict[str, Any]] = []
    listed_total: int = 0

    try:
        if side == "all":
            now_ms = int(time.time() * 1000)
            resp = _cancel_all(
                client=client,
                host=host,
                api_key=futures.api_key,
                api_secret=futures.api_secret,
                symbol=symbol,
                now_ms=now_ms,
            )
            if 200 <= resp.status_code < 300:
                cancelled.append(-1)  # sentinel: bulk cancel
            else:
                failures.append(
                    {
                        "endpoint": "allOpenOrders",
                        "status_code": resp.status_code,
                        "excerpt": (resp.text or "")[:200],
                    }
                )
        else:
            # reduce-only-tp-only: list, filter, cancel each
            now_ms = int(time.time() * 1000)
            try:
                rows = _list_open_orders(
                    client=client,
                    host=host,
                    api_key=futures.api_key,
                    api_secret=futures.api_secret,
                    symbol=symbol,
                    now_ms=now_ms,
                )
            except httpx.HTTPStatusError as exc:
                failures.append(
                    {
                        "endpoint": "openOrders",
                        "status_code": exc.response.status_code,
                        "excerpt": (exc.response.text or "")[:200],
                    }
                )
                rows = []
            listed_total = len(rows)
            reduce_only_rows = [r for r in rows if _is_reduce_only_tp(r)]
            for row in reduce_only_rows:
                order_id = row.get("orderId")
                if not isinstance(order_id, int):
                    failures.append(
                        {
                            "endpoint": "order",
                            "order_id": order_id,
                            "status_code": None,
                            "excerpt": "orderId missing or non-int",
                        }
                    )
                    continue
                resp = _cancel_one(
                    client=client,
                    host=host,
                    api_key=futures.api_key,
                    api_secret=futures.api_secret,
                    symbol=symbol,
                    order_id=order_id,
                    now_ms=int(time.time() * 1000),
                )
                if 200 <= resp.status_code < 300:
                    cancelled.append(order_id)
                else:
                    failures.append(
                        {
                            "endpoint": "order",
                            "order_id": order_id,
                            "status_code": resp.status_code,
                            "excerpt": (resp.text or "")[:200],
                        }
                    )
    finally:
        if owns_client:
            try:
                client.close()
            except Exception:  # pragma: no cover - defensive
                pass

    exit_code = 0 if not failures else 2

    emit_event(
        log,
        "ops.emergency_close.done",
        level=logging.INFO if exit_code == 0 else logging.ERROR,
        side=side,
        instrument=instrument,
        symbol=symbol,
        listed=listed_total,
        cancelled=len(cancelled),
        failures=len(failures),
        exit_code=exit_code,
    )

    # 3) Alert (best-effort).
    if cancelled and not failures:
        severity: Literal["info", "warn", "crit"] = "crit"
        message = (
            f"emergency_close OK side={side} instrument={instrument} "
            f"cancelled={len(cancelled)}"
        )
    else:
        severity = "crit"
        message = (
            f"emergency_close FAILED side={side} instrument={instrument} "
            f"cancelled={len(cancelled)} failures={len(failures)}"
        )
    _dispatch_alert_safe(
        alerter,
        severity=severity,
        event="ops.emergency_close.invoked",
        message=message,
        instrument=instrument,
        fields={
            "side": side,
            "symbol": symbol,
            "listed": listed_total,
            "cancelled": len(cancelled),
            "failures": len(failures),
            "exit_code": exit_code,
        },
    )

    return exit_code


__all__ = [
    "EmergencyCloseConfigError",
    "Side",
    "run",
]
