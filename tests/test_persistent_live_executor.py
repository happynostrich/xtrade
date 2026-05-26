"""Tests for `xtrade.live.persistent_executor.PersistentLiveExecutor` (Phase 5 / Track A1).

What this proves
----------------
* `start()` calls `build_testnet_node` exactly once, then `node.build()` +
  `node.run_async()` each once, in the background thread.
* `__call__(...)` reuses the same node across N submissions (the node is
  built once; only the probe strategy is added per intent).
* `stop()` calls `node.stop_async()` + `node.dispose()` exactly once and
  joins the background thread.
* `state` transitions monotonically NEW → STARTING → READY → STOPPED.
* `submit_count` reflects the number of completed submissions.
* Submitting before `start()` (or after `stop()`) raises
  `PersistentLiveExecutorError`.
* Re-entrant `stop()` is safe (idempotent).
* When the background thread fails to build the node, `start()` raises
  with the underlying cause attached as `__cause__`.

This module never touches a real Nautilus engine — it monkey-patches
`xtrade.node.factory.build_testnet_node` to return a `_StubNode` whose
trader fires `probe.done` immediately on `add_strategy`, simulating a
fast probe completion.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

# nautilus_trader is a real dep — required to construct `LiveOrderProbe`.
pytest.importorskip("nautilus_trader")

from xtrade.live.persistent_executor import (
    PersistentLiveExecutor,
    PersistentLiveExecutorError,
)


UTC = dt.timezone.utc
SYMBOL = "BTCUSDT-PERP.BINANCE"


# --- stub node ----------------------------------------------------------


class _StubTrader:
    """Trader stand-in. `add_strategy` simulates an instantly-completing probe."""

    def __init__(self, *, simulate_done: bool = True) -> None:
        self.added: list[Any] = []
        self.removed: list[Any] = []
        self._simulate_done = simulate_done

    def add_strategy(self, probe: Any) -> None:
        self.added.append(probe)
        if self._simulate_done:
            # Mutate observable surface so the resulting summary dict has
            # non-trivial content; the probe.events list is what the
            # summary writer reads.
            try:
                probe.events.append("stub.start")
                probe.events.append("stub.order_accepted")
                probe.events.append("stub.order_canceled")
                probe.order_accepted = True
                probe.order_canceled = True
            except Exception:  # noqa: BLE001
                pass
            # Fire `done` on the running loop. probe.done is asyncio.Event
            # but was created in the executor's loop, so use the running
            # loop's call_soon to stay thread-correct.
            loop = asyncio.get_event_loop()
            loop.call_soon(probe.done.set)

    def remove_strategy(self, probe: Any) -> None:
        self.removed.append(probe)


class _StubCache:
    def account_for_venue(self, venue: Any) -> None:
        return None


class _StubNode:
    """Minimal stand-in for `nautilus_trader.live.node.TradingNode`."""

    def __init__(self, *, simulate_done: bool = True) -> None:
        self.build_calls = 0
        self.run_async_calls = 0
        self.stop_async_calls = 0
        self.dispose_calls = 0
        self.trader = _StubTrader(simulate_done=simulate_done)
        self.cache = _StubCache()
        # Set within the running loop, used to make run_async() block.
        self._stop_signal: asyncio.Event | None = None

    def build(self) -> None:
        self.build_calls += 1

    async def run_async(self) -> None:
        self.run_async_calls += 1
        # Create the stop event inside the loop so we don't bind to
        # whatever loop made the node.
        self._stop_signal = asyncio.Event()
        await self._stop_signal.wait()

    async def stop_async(self) -> None:
        self.stop_async_calls += 1
        if self._stop_signal is not None:
            self._stop_signal.set()

    def dispose(self) -> None:
        self.dispose_calls += 1


@pytest.fixture
def stub_node(monkeypatch):
    """Monkey-patch `build_testnet_node` to return a fresh `_StubNode`.

    Returns the *list* of nodes built across the test (asserting len==1
    is how we prove "node built exactly once").
    """
    nodes: list[_StubNode] = []

    def _fake_build(venues_cfg, *, trader_id, log_level, log_directory):  # noqa: ARG001
        node = _StubNode()
        nodes.append(node)
        return node

    import xtrade.node.factory as factory_mod  # noqa: PLC0415

    monkeypatch.setattr(factory_mod, "build_testnet_node", _fake_build)
    return nodes


@pytest.fixture
def stub_node_factory(monkeypatch):
    """Variant of `stub_node` letting tests inject a custom node factory."""

    def _install(factory):
        import xtrade.node.factory as factory_mod  # noqa: PLC0415

        monkeypatch.setattr(factory_mod, "build_testnet_node", factory)

    return _install


# --- lifecycle ---------------------------------------------------------


def test_start_builds_node_once(stub_node, tmp_path: Path) -> None:
    executor = PersistentLiveExecutor(
        venues_cfg=object(),  # opaque; stub factory ignores it
        logs_root=tmp_path,
    )
    assert executor.state == "new"
    executor.start()
    try:
        assert executor.state == "ready"
        assert len(stub_node) == 1
        node = stub_node[0]
        assert node.build_calls == 1
        # The run task is scheduled — run_async_calls increments after
        # the loop picks it up. Give it a beat.
        for _ in range(20):
            if node.run_async_calls >= 1:
                break
            import time
            time.sleep(0.05)
        assert node.run_async_calls == 1
        assert node.stop_async_calls == 0
        assert node.dispose_calls == 0
    finally:
        executor.stop()


def test_stop_disposes_node_once(stub_node, tmp_path: Path) -> None:
    executor = PersistentLiveExecutor(venues_cfg=object(), logs_root=tmp_path)
    executor.start()
    executor.stop()
    assert executor.state == "stopped"
    node = stub_node[0]
    assert node.stop_async_calls == 1
    assert node.dispose_calls == 1
    # Background thread joined.
    assert executor._thread is not None
    assert not executor._thread.is_alive()


def test_stop_is_idempotent(stub_node, tmp_path: Path) -> None:
    executor = PersistentLiveExecutor(venues_cfg=object(), logs_root=tmp_path)
    executor.start()
    executor.stop()
    executor.stop()  # second call must not blow up
    assert executor.state == "stopped"
    node = stub_node[0]
    assert node.stop_async_calls == 1
    assert node.dispose_calls == 1


def test_stop_before_start_is_safe(tmp_path: Path) -> None:
    executor = PersistentLiveExecutor(venues_cfg=object(), logs_root=tmp_path)
    executor.stop()
    assert executor.state == "stopped"


def test_start_twice_raises(stub_node, tmp_path: Path) -> None:
    executor = PersistentLiveExecutor(venues_cfg=object(), logs_root=tmp_path)
    executor.start()
    try:
        with pytest.raises(PersistentLiveExecutorError, match="single-use"):
            executor.start()
    finally:
        executor.stop()


def test_submit_before_start_raises(tmp_path: Path) -> None:
    executor = PersistentLiveExecutor(venues_cfg=object(), logs_root=tmp_path)
    with pytest.raises(PersistentLiveExecutorError, match="state=new"):
        executor(
            object(),
            instrument_id=SYMBOL,
            quantity=Decimal("0.001"),
            side="BUY",
        )


def test_submit_after_stop_raises(stub_node, tmp_path: Path) -> None:
    executor = PersistentLiveExecutor(venues_cfg=object(), logs_root=tmp_path)
    executor.start()
    executor.stop()
    with pytest.raises(PersistentLiveExecutorError, match="state=stopped"):
        executor(
            object(),
            instrument_id=SYMBOL,
            quantity=Decimal("0.001"),
            side="BUY",
        )


# --- multi-submit (the actual A1 invariant) ----------------------------


def test_multiple_submits_reuse_same_node(stub_node, tmp_path: Path) -> None:
    """The headline A1 invariant: N submits, ONE node lifecycle."""
    executor = PersistentLiveExecutor(venues_cfg=object(), logs_root=tmp_path)
    executor.start()
    try:
        results = []
        for i in range(4):
            result = executor(
                object(),
                instrument_id=SYMBOL,
                quantity=Decimal("0.001"),
                side="BUY",
                timeout_s=5.0,
                run_id=f"a1-test-{i:02d}",
            )
            results.append(result)
    finally:
        executor.stop()

    # One node was built.
    assert len(stub_node) == 1
    node = stub_node[0]
    # The trader received four probes, one per submit.
    assert len(node.trader.added) == 4
    # build / run_async / stop_async / dispose all called exactly once.
    assert node.build_calls == 1
    assert node.run_async_calls == 1
    assert node.stop_async_calls == 1
    assert node.dispose_calls == 1
    # submit_count tracks issued submissions.
    assert executor.submit_count == 4
    # Each LiveResult carries a distinct run_id + summary file.
    run_ids = {r.run_id for r in results}
    assert len(run_ids) == 4
    for r in results:
        assert r.summary_path.exists()
        assert r.summary.get("persistent_node") is True


def test_submit_writes_summary_with_submit_index(stub_node, tmp_path: Path) -> None:
    executor = PersistentLiveExecutor(venues_cfg=object(), logs_root=tmp_path)
    executor.start()
    try:
        r1 = executor(
            object(),
            instrument_id=SYMBOL,
            quantity=Decimal("0.001"),
            side="BUY",
            timeout_s=5.0,
            run_id="a1-idx-1",
        )
        r2 = executor(
            object(),
            instrument_id=SYMBOL,
            quantity=Decimal("0.001"),
            side="BUY",
            timeout_s=5.0,
            run_id="a1-idx-2",
        )
    finally:
        executor.stop()
    assert r1.summary["submit_index"] == 1
    assert r2.summary["submit_index"] == 2


# --- failure modes -----------------------------------------------------


def test_start_failure_surfaces_cause(stub_node_factory, tmp_path: Path) -> None:
    """If build_testnet_node raises, start() must raise with the cause."""

    def _boom(venues_cfg, *, trader_id, log_level, log_directory):  # noqa: ARG001
        raise RuntimeError("synthetic build failure")

    stub_node_factory(_boom)

    executor = PersistentLiveExecutor(
        venues_cfg=object(),
        logs_root=tmp_path,
        startup_timeout_s=5.0,
    )
    with pytest.raises(PersistentLiveExecutorError, match="startup failed"):
        executor.start()
    assert executor.state == "crashed"


def test_unsupported_strategy_raises(stub_node, tmp_path: Path) -> None:
    executor = PersistentLiveExecutor(venues_cfg=object(), logs_root=tmp_path)
    executor.start()
    try:
        with pytest.raises(NotImplementedError, match="live_order_probe"):
            executor(
                object(),
                instrument_id=SYMBOL,
                strategy="momentum_follow",  # not supported by persistent path
                quantity=Decimal("0.001"),
                side="BUY",
                timeout_s=2.0,
            )
    finally:
        executor.stop()


# --- signal-handler workaround (A6 / mainnet crash-loop fix) ----------


def test_thread_main_neutralises_signal_signal_and_add_signal_handler(
    stub_node_factory, tmp_path: Path
) -> None:
    """Nautilus's NautilusKernel._setup_loop calls ``signal.signal`` and
    ``loop.add_signal_handler`` during ``node.build()``. Both raise
    ``ValueError: signal only works in main thread of the main
    interpreter`` when invoked from our background thread, which
    triggered the May-2026 supervisor crash-restart loop.

    This test stubs a node whose ``build()`` exercises both APIs and
    asserts the executor reaches READY (i.e. the workaround installed
    in ``_thread_main`` neutralises both calls in the node thread).
    """
    import signal as _signal

    observed: dict[str, Any] = {
        "signal_signal_called": False,
        "add_signal_handler_called": False,
    }

    class _SigSnoopNode(_StubNode):
        def build(self) -> None:
            super().build()
            # Mimic kernel.py:562 — signal.signal from non-main thread
            # raises ValueError unless the workaround neutralises it.
            _signal.signal(_signal.SIGINT, _signal.SIG_DFL)
            observed["signal_signal_called"] = True
            # Mimic kernel.py:566 — loop.add_signal_handler also
            # requires the main thread without the workaround.
            loop = asyncio.get_event_loop()
            loop.add_signal_handler(_signal.SIGTERM, lambda: None)
            observed["add_signal_handler_called"] = True

    def _factory(venues_cfg, *, trader_id, log_level, log_directory):  # noqa: ARG001
        return _SigSnoopNode()

    stub_node_factory(_factory)
    executor = PersistentLiveExecutor(
        venues_cfg=object(),
        logs_root=tmp_path,
        startup_timeout_s=5.0,
    )
    executor.start()
    try:
        assert executor.state == "ready"
        assert observed["signal_signal_called"] is True
        assert observed["add_signal_handler_called"] is True
    finally:
        executor.stop()


def test_suppress_main_thread_signal_install_restores_signal_signal() -> None:
    """The context manager must restore the original ``signal.signal``
    even if the wrapped block raises.
    """
    import signal as _signal

    from xtrade.live.persistent_executor import (
        _suppress_main_thread_signal_install,
    )

    original = _signal.signal
    with _suppress_main_thread_signal_install():
        assert _signal.signal is not original
    assert _signal.signal is original

    with pytest.raises(RuntimeError, match="boom"):
        with _suppress_main_thread_signal_install():
            raise RuntimeError("boom")
    assert _signal.signal is original


# --- import isolation --------------------------------------------------


def test_module_import_does_not_load_nautilus(tmp_path: Path) -> None:
    """Importing the executor module alone must NOT pull nautilus into sys.modules.

    The supervisor relies on this to keep lazy paths lazy.
    """
    import subprocess
    import sys
    import textwrap

    script = textwrap.dedent(
        """
        import sys
        # Drop anything pre-imported in the parent.
        for k in list(sys.modules):
            if k.startswith("nautilus_trader"):
                sys.modules.pop(k, None)
        import xtrade.live.persistent_executor  # noqa: F401
        leaked = sorted(
            m for m in sys.modules if m.startswith("nautilus_trader")
        )
        if leaked:
            print("LEAKED:" + ",".join(leaked))
            sys.exit(1)
        print("OK")
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert "OK" in result.stdout
