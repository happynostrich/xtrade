"""Live testnet runner (Phase 1 Task 6 / P5).

`run_live(...)` is the synchronous public entry point. It

  1. asserts every configured venue is testnet (`build_testnet_node`
     raises `MainnetRefusedError` if not — Phase 1 brief §6),
  2. builds an un-built `TradingNode`,
  3. instantiates the chosen strategy in live mode and adds it,
  4. drives the node async until the strategy signals `done` or
     `timeout_s + 30` elapses,
  5. snapshots the post-run account (Phase 0 quirk: `node.cache`,
     not `node.trader.cache`),
  6. writes `logs/<run_id>/summary.json`,
  7. returns a `LiveResult` whose `passed` property mirrors the
     strategy's verdict.

Currently registers a single strategy: `"live_order_probe"` →
`LiveOrderProbe` (the C2-spot-style safety probe). `demo_ema` is
intentionally *not* registered here — Phase 1 keeps the live path on
the connectivity probe; running EMA live is a Phase 2 concern.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import time
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from nautilus_trader.model.identifiers import InstrumentId

from xtrade.config import VenuesConfig
from xtrade.node.factory import build_testnet_node
from xtrade.strategies.base import XtradeStrategy
from xtrade.strategies.live_order_probe import LiveOrderProbe, LiveOrderProbeConfig


# ---------------------------------------------------------------------------
# Strategy registry
# ---------------------------------------------------------------------------


_LIVE_STRATEGY_REGISTRY: dict[str, type[XtradeStrategy]] = {
    "live_order_probe": LiveOrderProbe,
}


def available_live_strategies() -> list[str]:
    return sorted(_LIVE_STRATEGY_REGISTRY)


def _live_strategy_class(name: str) -> type[XtradeStrategy]:
    try:
        return _LIVE_STRATEGY_REGISTRY[name]
    except KeyError as exc:
        raise ValueError(
            f"unknown live strategy {name!r}; "
            f"available: {available_live_strategies()}"
        ) from exc


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LiveResult:
    run_id: str
    log_dir: Path
    summary_path: Path
    summary: dict[str, Any] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return bool(self.summary.get("passed", False))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_run_id(supplied: str | None) -> str:
    if supplied:
        return supplied
    stamp = dt.datetime.now(tz=dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"live-{stamp}"


def _utc_iso(ns: int | None) -> str | None:
    if ns is None:
        return None
    return dt.datetime.fromtimestamp(ns / 1e9, tz=dt.timezone.utc).isoformat()


async def _run_node_until(node, done_event: asyncio.Event, timeout_s: float) -> None:
    """Build the node and drive it until `done_event` is set or
    `timeout_s` elapses, then stop the node cleanly. Mirrors Phase 0's
    `_common.run_node_until`.

    `node.build()` is invoked here (inside the live event loop) so the
    Nautilus engines can schedule their async tasks against a running
    loop. Calling `build()` outside the loop produces "Started when
    loop is not running" warnings and a data client that silently
    never connects.
    """
    node.build()
    run_task = asyncio.create_task(node.run_async())
    try:
        await asyncio.wait_for(done_event.wait(), timeout=timeout_s)
    except asyncio.TimeoutError:
        pass
    finally:
        try:
            await node.stop_async()
        except Exception:  # noqa: BLE001
            pass
        if not run_task.done():
            try:
                await asyncio.wait_for(run_task, timeout=10)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):  # noqa: BLE001
                run_task.cancel()
                try:
                    await run_task
                except BaseException:  # noqa: BLE001
                    pass


def _account_snapshot(node, venue) -> list[dict[str, Any]]:
    """Return a JSON-friendly snapshot of `node.cache.account_for_venue(venue)`.

    Phase 0 quirk: `cache = node.cache`, NOT `node.trader.cache`.
    """
    try:
        account = node.cache.account_for_venue(venue)
    except Exception:  # noqa: BLE001
        return []
    if account is None:
        return []
    out: list[dict[str, Any]] = []
    try:
        for ccy, balance in account.balances().items():
            out.append(
                {
                    "currency": str(ccy),
                    "total": str(balance.total),
                    "locked": str(balance.locked),
                    "free": str(balance.free),
                }
            )
    except Exception:  # noqa: BLE001
        pass
    return out


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def run_live(
    venues_cfg: VenuesConfig,
    *,
    instrument_id: str | InstrumentId,
    strategy: str = "live_order_probe",
    quantity: Decimal = Decimal("0.001"),
    side: str = "BUY",
    safety_multiplier: Decimal = Decimal("0.7"),
    timeout_s: float = 60.0,
    run_id: str | None = None,
    logs_root: Path | str | None = None,
    trader_id: str = "XTRADE-LIVE-001",
    log_level: str = "INFO",
) -> LiveResult:
    """Run a one-shot live testnet probe and return a structured result.

    Parameters
    ----------
    venues_cfg : VenuesConfig
        Loaded venues config; forwarded to `build_testnet_node`. Any
        non-testnet routing causes `MainnetRefusedError` *before* the
        node is constructed.
    instrument_id : str | InstrumentId
        The single instrument to probe (e.g. `"BTCUSDT.BINANCE"`).
    strategy : str
        Registry key. Currently only `"live_order_probe"`.
    quantity, side, safety_multiplier : Decimal / str / Decimal
        Forwarded to `LiveOrderProbeConfig`. See that class's docstring.
    timeout_s : float
        Per-probe timeout. The run as a whole returns within
        `timeout_s + 30` seconds.
    run_id : str | None
        Override the auto-generated `live-YYYYMMDDTHHMMSSZ` id.
    logs_root : Path | str | None
        Override the `logs/` root (mainly for tests).
    trader_id, log_level : str
        Forwarded to `build_testnet_node`.
    """
    cls = _live_strategy_class(strategy)
    if cls is not LiveOrderProbe:  # pragma: no cover - reserved
        raise NotImplementedError(
            f"live strategy {strategy!r} has no config factory yet"
        )

    iid = (
        instrument_id
        if isinstance(instrument_id, InstrumentId)
        else InstrumentId.from_str(instrument_id)
    )

    repo_root = Path(__file__).resolve().parents[3]
    logs_root_p = Path(logs_root) if logs_root is not None else (repo_root / "logs")
    run_id = _resolve_run_id(run_id)
    log_dir = logs_root_p / run_id
    log_dir.mkdir(parents=True, exist_ok=True)

    # Eagerly refuse mainnet so callers see the error before any log
    # directory is created or TradingNode is constructed.
    from xtrade.node.factory import _assert_testnet_only
    from xtrade.live.mainnet_unlock import assert_mainnet_unlock

    _assert_testnet_only(venues_cfg)
    # Phase 5 Task A5 — third lock (defense in depth). Lock 1 above will
    # hard-reject any mainnet routing today, so this call is a no-op in
    # the current Phase 5 codebase; it becomes load-bearing once Phase 6
    # relaxes Lock 1 conditional on the unlock ritual.
    assert_mainnet_unlock(venues_cfg)

    # TradingNode.__init__ captures `asyncio.get_event_loop()` and the
    # engines latch onto that specific loop forever. Constructing the
    # node outside asyncio.run leaves the engines bound to a loop that
    # never runs, so data/exec client `_connect` coroutines are never
    # awaited (silent failure). We therefore build the node, register
    # the strategy, and run all inside one orchestrating coroutine.
    probe_holder: dict[str, LiveOrderProbe] = {}
    node_holder: dict[str, Any] = {}

    async def _orchestrate() -> None:
        node = build_testnet_node(
            venues_cfg,
            trader_id=trader_id,
            log_level=log_level,
            log_directory=log_dir,
        )
        node_holder["node"] = node
        probe = LiveOrderProbe(
            config=LiveOrderProbeConfig(
                mode="live",
                instrument_id=iid,
                quantity=quantity,
                side=side,
                safety_multiplier=safety_multiplier,
                timeout_s=timeout_s,
            ),
        )
        probe_holder["probe"] = probe
        node.trader.add_strategy(probe)
        await _run_node_until(node, probe.done, timeout_s=timeout_s + 30.0)

    t0 = time.monotonic()
    try:
        asyncio.run(_orchestrate())
    finally:
        node = node_holder.get("node")
        if node is not None:
            try:
                node.dispose()
            except Exception:  # noqa: BLE001
                pass
    elapsed_s = round(time.monotonic() - t0, 3)
    probe = probe_holder["probe"]
    node = node_holder["node"]

    venue = iid.venue
    account_snapshot = _account_snapshot(node, venue)
    summary: dict[str, Any] = {
        "run_id": run_id,
        "mode": "live",
        "trader_id": trader_id,
        "strategy": strategy,
        "instrument_id": str(iid),
        "venue": str(venue),
        "timeout_s": timeout_s,
        "events": probe.events,
        "first_quote_iso": _utc_iso(probe.first_quote_ns),
        "first_trade_iso": _utc_iso(probe.first_trade_ns),
        "order": {
            "client_order_id": (
                str(probe.order.client_order_id) if probe.order is not None else None
            ),
            "accepted": probe.order_accepted,
            "canceled": probe.order_canceled,
            "rejected": probe.order_rejected,
            "rejection_reason": probe.rejection_reason,
        },
        "timed_out": probe.timed_out,
        "passed": probe.passed,
        "account_snapshot": account_snapshot,
        "elapsed_s": elapsed_s,
        "config": {
            "strategy": strategy,
            "quantity": str(quantity),
            "side": side,
            "safety_multiplier": str(safety_multiplier),
        },
    }

    summary_path = log_dir / "summary.json"
    summary_path.write_text(json.dumps(summary, indent=2, default=str))

    return LiveResult(
        run_id=run_id,
        log_dir=log_dir,
        summary_path=summary_path,
        summary=summary,
    )
