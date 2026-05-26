"""Phase 5 Track A1 — persistent TradingNode executor.

Background
----------
Phase 4 used a "one intent → one TradingNode" pattern: each call to
`xtrade.live.runner.run_live(...)` spins up a fresh
`nautilus_trader.live.node.TradingNode`, places one far-from-market
limit order through `LiveOrderProbe`, cancels it, and disposes the
node. That is wasteful in production (10+s startup per intent, full
venue handshake every time) but kept Phase 4 on Phase 1's most-tested
code path while the supervisor framework stabilised.

A1 promotes the supervisor to a **single persistent node** that:

  1. Calls `build_testnet_node(venues_cfg)` exactly **once** at
     supervisor startup.
  2. Calls `node.build()` and `node.run_async()` exactly **once** —
     inside a dedicated background thread that owns the asyncio loop
     the engines are bound to.
  3. Per intent: registers an ephemeral `LiveOrderProbe`, awaits its
     `done` event on the executor's loop (driven from the supervisor's
     synchronous thread via `asyncio.run_coroutine_threadsafe`), then
     deregisters / disposes the probe.
  4. Calls `node.stop_async()` + `node.dispose()` exactly **once** on
     SIGTERM (supervisor shutdown).

Why a background thread
-----------------------
TradingNode's `__init__` captures `asyncio.get_event_loop()` and the
data / exec engines latch onto that loop forever. The supervisor's
main loop is synchronous (driven by `threading.Event.wait(interval)`),
so we can't host the node directly. The cleanest separation is:

  * **Main thread**: synchronous supervisor loop, file IO, bridge
    HTTP, polling cursor — all blocking calls are fine here.
  * **Node thread**: owns a dedicated asyncio loop, runs the node's
    `run_async()` task forever, accepts work via
    `run_coroutine_threadsafe`.

This mirrors what `LiveOrderProbe` already does inside `run_live` — we
just hoist the loop ownership up to the supervisor scope.

API surface
-----------
`PersistentLiveExecutor` exposes a callable interface compatible with
`xtrade.live.runner.run_live` so the supervisor can swap it in via the
existing `live_executor` injection seam without further changes:

    executor = PersistentLiveExecutor(venues_cfg, logs_root=...)
    executor.start()                # once
    try:
        result = executor(           # n times
            venues_cfg,
            instrument_id="BTCUSDT-PERP.BINANCE",
            quantity=Decimal("0.001"),
            side="BUY",
            ...,
        )
    finally:
        executor.stop()              # once

`executor.start()` blocks until the background thread has built the
node and the run task is alive (or it raises). `executor(...)` blocks
the calling (supervisor) thread until the per-intent probe completes
or the configured timeout elapses. `executor.stop()` cleanly tears the
node down and joins the thread.

Notes for tests
---------------
The executor never imports `nautilus_trader` at module load — it only
hits it inside the background-thread code path. Tests monkey-patch
`build_testnet_node` to return a stub node that mimics the small
contract this module needs (`build` / `run_async` / `stop_async` /
`dispose` / `trader.add_strategy` / `trader.remove_strategy`).
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import json
import logging
import signal
import threading
import time
from concurrent.futures import Future
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any


log = logging.getLogger("xtrade.live.persistent_executor")


# Imported lazily inside methods so the module loads under offline test
# environments that have no `nautilus_trader` wheel. Tests that do not
# inject a stub `build_testnet_node` will not exercise this module's
# Nautilus path at all.


@contextlib.contextmanager
def _suppress_main_thread_signal_install() -> Any:
    """Temporarily neutralise ``signal.signal`` during Nautilus node build.

    Nautilus 1.227.0's ``NautilusKernel._setup_loop`` calls
    ``signal.signal(SIGINT, SIG_DFL)`` unconditionally during
    ``node.build()`` (system/kernel.py:562) and then
    ``loop.add_signal_handler`` for SIGTERM/SIGINT/SIGABRT
    (system/kernel.py:566). Both APIs require the *main thread of the
    main interpreter*; we run the node in a dedicated background
    thread (see module docstring on `Why a background thread`), so the
    unmodified call raises ``ValueError: signal only works in main
    thread of the main interpreter`` and the supervisor enters a
    crash-restart loop.

    The supervisor itself installs SIGTERM/SIGINT handlers in the main
    thread before this executor is started (see
    `xtrade.live.supervisor`), so it is safe to swallow Nautilus's
    duplicate install attempt here. The companion patch on
    ``loop.add_signal_handler`` is applied directly on the loop
    instance in `_thread_main`.
    """
    original = signal.signal
    signal.signal = lambda *_a, **_kw: None  # type: ignore[assignment]
    try:
        yield
    finally:
        signal.signal = original  # type: ignore[assignment]


class PersistentLiveExecutorError(RuntimeError):
    """Raised when the persistent node is unusable (crashed / not started)."""


@dataclass(frozen=True)
class _SubmitParams:
    instrument_id: Any  # str | InstrumentId
    quantity: Decimal
    side: str
    safety_multiplier: Decimal
    timeout_s: float
    run_id: str
    log_dir: Path
    strategy: str  # currently must be "live_order_probe"


class PersistentLiveExecutor:
    """Long-lived TradingNode executor — one node for the whole supervisor run.

    Lifecycle states (transitions are one-way):

        NEW → STARTING → READY → STOPPING → STOPPED
                         ↑           ↑
                         └─ CRASHED ─┘   (terminal; submit raises)
    """

    _STATE_NEW = "new"
    _STATE_STARTING = "starting"
    _STATE_READY = "ready"
    _STATE_STOPPING = "stopping"
    _STATE_STOPPED = "stopped"
    _STATE_CRASHED = "crashed"

    def __init__(
        self,
        venues_cfg: Any,
        *,
        logs_root: Path | str | None = None,
        trader_id: str = "XTRADE-SUPERVISOR-001",
        log_level: str = "INFO",
        startup_timeout_s: float = 60.0,
        shutdown_timeout_s: float = 60.0,
    ) -> None:
        self._venues_cfg = venues_cfg
        self._logs_root = Path(logs_root) if logs_root is not None else None
        self._trader_id = trader_id
        self._log_level = log_level
        self._startup_timeout_s = float(startup_timeout_s)
        self._shutdown_timeout_s = float(shutdown_timeout_s)

        self._state = self._STATE_NEW
        self._state_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._node: Any = None
        self._run_task: asyncio.Task[Any] | None = None
        self._ready_event = threading.Event()
        self._start_error: BaseException | None = None
        # Filled when the run task exits (clean stop or crash mid-run).
        self._run_exit: BaseException | None = None
        # Stable across calls: how many submits have been issued.
        self._submit_count = 0

    # ----- public lifecycle ------------------------------------------------

    @property
    def state(self) -> str:
        return self._state

    @property
    def submit_count(self) -> int:
        return self._submit_count

    def start(self) -> None:
        """Spawn the background thread, build the node, start node.run_async().

        Blocks until the thread reports ready (node built, run task
        scheduled) or `startup_timeout_s` elapses. Raises
        `PersistentLiveExecutorError` on failure.
        """
        with self._state_lock:
            if self._state != self._STATE_NEW:
                raise PersistentLiveExecutorError(
                    f"start() called in state={self._state}; "
                    "PersistentLiveExecutor is single-use"
                )
            self._state = self._STATE_STARTING

        self._thread = threading.Thread(
            target=self._thread_main,
            name="xtrade-live-node",
            daemon=True,
        )
        self._thread.start()
        ok = self._ready_event.wait(timeout=self._startup_timeout_s)
        if not ok:
            # Thread never signalled ready — leave it daemon and
            # surface a hard error. The thread may still be doing IO,
            # but the supervisor cannot proceed.
            self._state = self._STATE_CRASHED
            raise PersistentLiveExecutorError(
                f"persistent node did not start within {self._startup_timeout_s}s"
            )
        if self._start_error is not None:
            self._state = self._STATE_CRASHED
            raise PersistentLiveExecutorError(
                f"persistent node startup failed: "
                f"{type(self._start_error).__name__}: {self._start_error}"
            ) from self._start_error
        self._state = self._STATE_READY

    def stop(self) -> None:
        """Graceful shutdown — idempotent; safe to call from any state."""
        with self._state_lock:
            if self._state in (self._STATE_STOPPED, self._STATE_NEW):
                self._state = self._STATE_STOPPED
                return
            if self._state == self._STATE_STOPPING:
                # Another caller already in flight — just wait for the
                # thread to finish below.
                pass
            else:
                self._state = self._STATE_STOPPING

        loop = self._loop
        if loop is not None and loop.is_running():
            try:
                fut = asyncio.run_coroutine_threadsafe(self._async_stop(), loop)
                fut.result(timeout=self._shutdown_timeout_s)
            except Exception as exc:  # noqa: BLE001
                log.warning("persistent_executor.stop: async stop raised: %s", exc)
            try:
                loop.call_soon_threadsafe(loop.stop)
            except RuntimeError:
                pass

        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=self._shutdown_timeout_s)

        self._state = self._STATE_STOPPED

    # ----- callable surface (drop-in for run_live) ------------------------

    def __call__(
        self,
        venues_cfg: Any,
        *,
        instrument_id: Any,
        strategy: str = "live_order_probe",
        quantity: Decimal = Decimal("0.001"),
        side: str = "BUY",
        safety_multiplier: Decimal = Decimal("0.7"),
        timeout_s: float = 60.0,
        run_id: str | None = None,
        logs_root: Path | str | None = None,
        trader_id: str | None = None,  # accepted for parity; ignored
        log_level: str | None = None,  # accepted for parity; ignored
    ) -> "LiveResult":
        """Submit one intent through the persistent node and return a LiveResult.

        Blocks the calling (supervisor) thread until the probe
        completes or `timeout_s + 30` elapses. Raises
        `PersistentLiveExecutorError` if the node is not in READY state.
        """
        # Late import to dodge cycles + Nautilus eager-import.
        from xtrade.live.runner import LiveResult, _resolve_run_id  # noqa: PLC0415

        if self._state != self._STATE_READY:
            raise PersistentLiveExecutorError(
                f"cannot submit: persistent node state={self._state}"
            )
        if self._run_task is not None and self._run_task.done():
            self._state = self._STATE_CRASHED
            exc = self._run_exit
            raise PersistentLiveExecutorError(
                f"persistent node run_task already finished: {exc!r}"
            )
        if strategy != "live_order_probe":
            raise NotImplementedError(
                f"persistent executor only supports 'live_order_probe', got {strategy!r}"
            )

        rid = _resolve_run_id(run_id)
        repo_root = Path(__file__).resolve().parents[3]
        logs_root_p = (
            Path(logs_root)
            if logs_root is not None
            else (self._logs_root if self._logs_root is not None else repo_root / "logs")
        )
        log_dir = logs_root_p / rid
        log_dir.mkdir(parents=True, exist_ok=True)

        params = _SubmitParams(
            instrument_id=instrument_id,
            quantity=quantity,
            side=side,
            safety_multiplier=safety_multiplier,
            timeout_s=timeout_s,
            run_id=rid,
            log_dir=log_dir,
            strategy=strategy,
        )

        self._submit_count += 1
        assert self._loop is not None  # READY implies loop is set
        fut = asyncio.run_coroutine_threadsafe(
            self._async_submit(params), self._loop
        )
        # Generous outer timeout: the inner await already has timeout_s.
        result = fut.result(timeout=timeout_s + 60.0)
        # `result` is the summary dict; build LiveResult to match run_live.
        summary_path = log_dir / "summary.json"
        try:
            summary_path.write_text(json.dumps(result, indent=2, default=str))
        except Exception:  # noqa: BLE001
            log.exception("persistent_executor.summary_write_failed run_id=%s", rid)
        return LiveResult(
            run_id=rid,
            log_dir=log_dir,
            summary_path=summary_path,
            summary=result,
        )

    # ----- background thread ---------------------------------------------

    def _thread_main(self) -> None:
        """Entry point for the node-owning background thread."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        # Nautilus's NautilusKernel._setup_loop installs SIGINT/SIGTERM
        # handlers on the event loop during node.build(). Those calls
        # require the main thread of the main interpreter; this thread
        # is not the main thread. See _suppress_main_thread_signal_install
        # for the matching signal.signal patch.
        self._loop.add_signal_handler = (  # type: ignore[method-assign]
            lambda *_a, **_kw: None
        )
        self._loop.remove_signal_handler = (  # type: ignore[method-assign]
            lambda *_a, **_kw: True
        )
        try:
            with _suppress_main_thread_signal_install():
                self._loop.run_until_complete(self._async_init())
        except BaseException as exc:  # noqa: BLE001
            self._start_error = exc
            log.exception("persistent_executor.init.crash")
            self._ready_event.set()
            try:
                self._loop.close()
            finally:
                self._loop = None
            return
        self._ready_event.set()
        try:
            self._loop.run_forever()
        finally:
            try:
                self._loop.close()
            except Exception:  # noqa: BLE001
                pass

    async def _async_init(self) -> None:
        """Inside the node thread's loop: build node + start run_async task."""
        from xtrade.node.factory import build_testnet_node  # noqa: PLC0415

        log_dir = self._logs_root
        node = build_testnet_node(
            self._venues_cfg,
            trader_id=self._trader_id,
            log_level=self._log_level,
            log_directory=log_dir,
        )
        self._node = node
        # `build()` finalises engine wiring; must be inside the loop the
        # engines bound to.
        node.build()
        self._run_task = asyncio.create_task(
            self._wrap_run_async(node), name="xtrade-live-node.run"
        )
        # Give the loop one tick to actually schedule the task.
        await asyncio.sleep(0)

    async def _wrap_run_async(self, node: Any) -> None:
        """Run node forever; capture any terminal exception for diagnostics."""
        try:
            await node.run_async()
        except BaseException as exc:  # noqa: BLE001
            self._run_exit = exc
            log.exception("persistent_executor.run_async.crash")
            raise

    async def _async_submit(self, params: _SubmitParams) -> dict[str, Any]:
        """Per-intent body, executed on the node thread's loop.

        Constructs an ephemeral `LiveOrderProbe`, registers it with the
        already-running trader, awaits its `done` event, then attempts
        to deregister + cleanup. Returns the same summary dict that
        `xtrade.live.runner.run_live` writes to `summary.json`.
        """
        # Lazy imports — avoid pulling Nautilus into modules that lazy-import
        # this executor in tests.
        from nautilus_trader.model.identifiers import InstrumentId  # noqa: PLC0415

        from xtrade.live.runner import _account_snapshot, _utc_iso  # noqa: PLC0415
        from xtrade.strategies.live_order_probe import (  # noqa: PLC0415
            LiveOrderProbe,
            LiveOrderProbeConfig,
        )

        iid = (
            params.instrument_id
            if isinstance(params.instrument_id, InstrumentId)
            else InstrumentId.from_str(str(params.instrument_id))
        )

        probe = LiveOrderProbe(
            config=LiveOrderProbeConfig(
                mode="live",
                instrument_id=iid,
                quantity=params.quantity,
                side=params.side,
                safety_multiplier=params.safety_multiplier,
                timeout_s=params.timeout_s,
            ),
        )

        t0 = time.monotonic()
        node = self._node
        node.trader.add_strategy(probe)
        try:
            try:
                await asyncio.wait_for(probe.done.wait(), timeout=params.timeout_s)
            except asyncio.TimeoutError:
                # Probe is left to clean itself up on remove; runner
                # surfaces this via `timed_out`.
                pass
        finally:
            # `remove_strategy` is best-effort: some Nautilus versions may
            # not expose it. If it isn't available, the probe stays in the
            # trader until node disposal — that's acceptable because each
            # probe is short-lived and has its own `done` latch.
            remove = getattr(node.trader, "remove_strategy", None)
            if callable(remove):
                try:
                    remove(probe)
                except Exception:  # noqa: BLE001
                    log.exception("persistent_executor.remove_strategy.crash")

        elapsed_s = round(time.monotonic() - t0, 3)
        venue = iid.venue
        account_snapshot = _account_snapshot(node, venue)
        return {
            "run_id": params.run_id,
            "mode": "live",
            "trader_id": self._trader_id,
            "strategy": params.strategy,
            "instrument_id": str(iid),
            "venue": str(venue),
            "timeout_s": params.timeout_s,
            "events": probe.events,
            "first_quote_iso": _utc_iso(probe.first_quote_ns),
            "first_trade_iso": _utc_iso(probe.first_trade_ns),
            "order": {
                "client_order_id": (
                    str(probe.order.client_order_id)
                    if probe.order is not None
                    else None
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
            "persistent_node": True,
            "submit_index": self._submit_count,
            "config": {
                "strategy": params.strategy,
                "quantity": str(params.quantity),
                "side": params.side,
                "safety_multiplier": str(params.safety_multiplier),
            },
        }

    async def _async_stop(self) -> None:
        """Inside the node thread's loop: stop_async + dispose."""
        node = self._node
        if node is None:
            return
        try:
            await node.stop_async()
        except Exception:  # noqa: BLE001
            log.exception("persistent_executor.stop_async.crash")
        if self._run_task is not None and not self._run_task.done():
            try:
                await asyncio.wait_for(self._run_task, timeout=10.0)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):  # noqa: BLE001
                self._run_task.cancel()
                try:
                    await self._run_task
                except BaseException:  # noqa: BLE001
                    pass
        try:
            node.dispose()
        except Exception:  # noqa: BLE001
            log.exception("persistent_executor.dispose.crash")


__all__ = [
    "PersistentLiveExecutor",
    "PersistentLiveExecutorError",
]
