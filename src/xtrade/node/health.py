"""TradingNode health probe (Phase 1 Task 3 / P2).

`probe(venues_cfg, instruments, timeout_s)` builds a testnet
`TradingNode`, subscribes to the given instruments (one per venue),
awaits the first quote/trade on each subscription within `timeout_s`,
and returns a structured result. A JSON summary is also written to
`logs/<run_id>/health.json`.

Per the Phase 0 quirk noted in `node/health.py`'s pre-Task-3 stub: the
cache lives on `node.cache`, not `node.trader.cache` — we honor that
when reading the post-run account snapshot.

This module never falls back to mainnet — it relies on `factory.build_testnet_node`
which raises on any non-testnet config.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from nautilus_trader.config import StrategyConfig
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.trading.strategy import Strategy

from xtrade.config import VenuesConfig
from xtrade.node.factory import build_testnet_node


# ---------------------------------------------------------------------------
# Probe strategy
# ---------------------------------------------------------------------------


class _HealthProbeConfig(StrategyConfig, frozen=True):
    instrument_ids: tuple[InstrumentId, ...]
    timeout_s: float


class _HealthProbeStrategy(Strategy):
    """Subscribes to quotes + trades on each given instrument and signals
    `done` once every instrument has seen at least one quote (or the
    overall timer fires)."""

    def __init__(self, config: _HealthProbeConfig) -> None:
        super().__init__(config)
        self._start_ns: int = 0
        self.first_quote_ns: dict[InstrumentId, int] = {}
        self.first_trade_ns: dict[InstrumentId, int] = {}
        self.events: list[str] = []
        self.done = asyncio.Event()

    def on_start(self) -> None:  # noqa: D401
        cfg: _HealthProbeConfig = self.config  # type: ignore[assignment]
        self._start_ns = self.clock.timestamp_ns()
        for iid in cfg.instrument_ids:
            self.subscribe_quote_ticks(iid)
            self.subscribe_trade_ticks(iid)
            self.events.append(f"subscribed quotes/trades: {iid}")
        self.clock.set_time_alert_ns(
            "health-timeout",
            self._start_ns + int(cfg.timeout_s * 1e9),
            self._timeout,
        )

    def _timeout(self, _event) -> None:
        if not self.done.is_set():
            self.events.append("timeout reached before all channels observed quotes")
            self.done.set()

    def on_quote_tick(self, tick) -> None:
        iid = tick.instrument_id
        if iid not in self.first_quote_ns:
            self.first_quote_ns[iid] = self.clock.timestamp_ns()
            self.events.append(
                f"first quote: {iid} bid={tick.bid_price} ask={tick.ask_price}"
            )
        self._maybe_done()

    def on_trade_tick(self, tick) -> None:
        iid = tick.instrument_id
        if iid not in self.first_trade_ns:
            self.first_trade_ns[iid] = self.clock.timestamp_ns()
            self.events.append(f"first trade: {iid} px={tick.price} qty={tick.size}")

    def _maybe_done(self) -> None:
        cfg: _HealthProbeConfig = self.config  # type: ignore[assignment]
        if all(iid in self.first_quote_ns for iid in cfg.instrument_ids):
            if not self.done.is_set():
                self.events.append("all channels observed at least one quote")
                self.done.set()


# ---------------------------------------------------------------------------
# Async runner
# ---------------------------------------------------------------------------


async def _run_node_until(node, done_event: asyncio.Event, timeout_s: float) -> None:
    """Build the node and drive it until `done_event` is set or
    `timeout_s` elapses, then stop the node cleanly. Mirrors Phase 0's
    `_common.run_node_until`.

    `node.build()` is invoked here (inside the live event loop) rather
    than by the caller. Nautilus's engines schedule async tasks during
    `build()` and emit "Started when loop is not running" / "Async task
    '_connect' created but event loop is not running" if the loop
    isn't already up — the data client then never actually connects.
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


# ---------------------------------------------------------------------------
# Public probe API
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HealthResult:
    run_id: str
    log_dir: Path
    summary_path: Path
    summary: dict[str, Any] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        per_venue = self.summary.get("per_instrument", {})
        return bool(per_venue) and all(
            entry.get("first_quote_iso") is not None for entry in per_venue.values()
        )


def _utc_iso(ns: int | None) -> str | None:
    if ns is None:
        return None
    return dt.datetime.fromtimestamp(ns / 1e9, tz=dt.timezone.utc).isoformat()


def _resolve_run_id(supplied: str | None) -> str:
    if supplied:
        return supplied
    stamp = dt.datetime.now(tz=dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"health-{stamp}"


def probe(
    venues_cfg: VenuesConfig,
    *,
    instruments: list[InstrumentId],
    timeout_s: float = 60.0,
    run_id: str | None = None,
    logs_root: Path | str | None = None,
    trader_id: str = "XTRADE-HEALTH-001",
    log_level: str = "WARN",
) -> HealthResult:
    """Run a one-shot health probe and return a structured result.

    Parameters
    ----------
    venues_cfg : VenuesConfig
        Loaded venues config; passed through to `build_testnet_node`.
    instruments : list[InstrumentId]
        One or more instruments to subscribe to. The probe waits until
        each instrument has produced at least one quote.
    timeout_s : float
        Overall timeout. The probe returns within `timeout_s + 30`
        regardless of subscription progress.
    run_id : str | None
        Override the auto-generated `health-YYYYMMDDTHHMMSSZ` id.
    logs_root : Path | str | None
        Override the `logs/` root (mainly for tests).
    trader_id, log_level : str
        Forwarded to `build_testnet_node`.
    """
    if not instruments:
        raise ValueError("probe(): `instruments` must contain at least one InstrumentId.")

    # Eagerly refuse mainnet so the caller sees the error before any
    # log dir is created or TradingNode is constructed.
    from xtrade.node.factory import _assert_testnet_only
    from xtrade.live.mainnet_unlock import assert_mainnet_unlock

    _assert_testnet_only(venues_cfg)
    # Phase 5 Task A5 — third lock (see runner.run_live for rationale).
    assert_mainnet_unlock(venues_cfg)

    repo_root = Path(__file__).resolve().parents[3]
    logs_root_p = Path(logs_root) if logs_root is not None else (repo_root / "logs")
    run_id = _resolve_run_id(run_id)
    log_dir = logs_root_p / run_id
    log_dir.mkdir(parents=True, exist_ok=True)

    # TradingNode.__init__ captures `asyncio.get_event_loop()` at
    # construction time; if we build the node *outside* asyncio.run the
    # engines latch onto a loop that never runs and the data client's
    # _connect coroutine is never awaited (silent connection failure).
    # We therefore construct the node, add the strategy, build, and run
    # all inside one coroutine driven by asyncio.run.
    strategy_holder: dict[str, _HealthProbeStrategy] = {}
    node_holder: dict[str, Any] = {}

    async def _orchestrate() -> None:
        node = build_testnet_node(
            venues_cfg,
            trader_id=trader_id,
            log_level=log_level,
            log_directory=log_dir,
        )
        node_holder["node"] = node
        strategy = _HealthProbeStrategy(
            config=_HealthProbeConfig(
                instrument_ids=tuple(instruments),
                timeout_s=timeout_s,
            ),
        )
        strategy_holder["strategy"] = strategy
        node.trader.add_strategy(strategy)
        await _run_node_until(node, strategy.done, timeout_s=timeout_s + 30.0)

    try:
        asyncio.run(_orchestrate())
    finally:
        node = node_holder.get("node")
        if node is not None:
            try:
                node.dispose()
            except Exception:  # noqa: BLE001
                pass

    strategy = strategy_holder["strategy"]

    # Build the summary.
    per_instrument: dict[str, dict[str, Any]] = {}
    start_ns = strategy._start_ns or 0
    for iid in instruments:
        q_ns = strategy.first_quote_ns.get(iid)
        t_ns = strategy.first_trade_ns.get(iid)
        per_instrument[str(iid)] = {
            "venue": str(iid.venue),
            "first_quote_iso": _utc_iso(q_ns),
            "first_quote_latency_ms": (
                round((q_ns - start_ns) / 1e6, 3) if q_ns and start_ns else None
            ),
            "first_trade_iso": _utc_iso(t_ns),
            "first_trade_latency_ms": (
                round((t_ns - start_ns) / 1e6, 3) if t_ns and start_ns else None
            ),
        }

    summary: dict[str, Any] = {
        "run_id": run_id,
        "trader_id": trader_id,
        "timeout_s": timeout_s,
        "instruments": [str(iid) for iid in instruments],
        "events": strategy.events,
        "per_instrument": per_instrument,
        "passed": all(
            entry["first_quote_iso"] is not None for entry in per_instrument.values()
        ),
    }

    summary_path = log_dir / "health.json"
    summary_path.write_text(json.dumps(summary, indent=2, default=str))

    return HealthResult(
        run_id=run_id,
        log_dir=log_dir,
        summary_path=summary_path,
        summary=summary,
    )
