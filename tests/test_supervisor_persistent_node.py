"""Tests for supervisor ↔ PersistentLiveExecutor integration (Phase 5 / A1).

What this proves
----------------
* When `config.venues_cfg` is set AND `live_executor` is not injected,
  `run_supervisor()` constructs a `PersistentLiveExecutor`, calls
  `start()` once at the top of the loop, and calls `stop()` once when
  the loop exits (clean or via stop_event).
* `supervisor.node.start` is emitted exactly once, and
  `supervisor.node.stop` is emitted exactly once, per supervisor run.
* `config.persistent_node = False` (kill switch) falls back to the
  legacy `run_live`-per-intent code path; no persistent executor is
  built.
* When tests inject `live_executor=...` directly (the existing Phase 4
  test seam), the supervisor does NOT auto-build a persistent
  executor even if `venues_cfg` is set.
* Multi-intent reuse: across 3 sequential signals, the supervisor
  calls the persistent executor 3 times against the SAME executor
  instance (we verify by reading `executor.submit_count`).
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

# Side-effect: registers `momentum_follow` so `load_strategy` works.
import xtrade.strategy  # noqa: F401
from xtrade.live.supervisor import SupervisorConfig, run_supervisor
from xtrade.research.signals import Signal, SignalQueue


UTC = dt.timezone.utc
SYMBOL = "BTCUSDT-PERP.BINANCE"


# --- common fixtures ---------------------------------------------------


def _make_config(
    tmp_path: Path,
    *,
    mode: str = "auto",
    venues_cfg: Any = None,
    persistent_node: bool = True,
) -> SupervisorConfig:
    return SupervisorConfig(
        instrument_id=SYMBOL,
        strategy_name="momentum_follow",
        signals_root=tmp_path / "signals",
        approvals_root=tmp_path / "approvals",
        cursor_path=tmp_path / "cursor.json",
        sentinel_path=tmp_path / "paused.flag",
        logs_root=tmp_path / "logs",
        approval_mode=mode,  # type: ignore[arg-type]
        strategy_config={"notional_usd": Decimal("100")},
        poll_interval_s=0.0,
        venue_timeout_s=5.0,
        safety_multiplier=Decimal("0.7"),
        risk_rules=(),
        venues_cfg=venues_cfg,
        bridge=None,
        persistent_node=persistent_node,
    )


def _seed_signal(
    signals_root: Path,
    *,
    direction: str = "LONG",
    last_price: str = "50000",
    when: dt.datetime | None = None,
    source: str = "momentum:persistent-test",
) -> Signal:
    sig = Signal(
        symbol=SYMBOL,
        venue="binance",
        direction=direction,  # type: ignore[arg-type]
        strength=0.6 if direction == "LONG" else -0.6,
        generated_at=when or dt.datetime(2026, 5, 24, 12, 0, 0, tzinfo=UTC),
        source=source,
        metadata={"last_price": last_price},
    )
    SignalQueue(signals_root).append([sig])
    return sig


class _FakePersistentExecutor:
    """A stand-in for `PersistentLiveExecutor` that records lifecycle calls.

    We monkey-patch `xtrade.live.persistent_executor.PersistentLiveExecutor`
    to this class so the supervisor uses it. This avoids spinning up an
    asyncio loop / Nautilus stub just to verify the start/stop wiring.
    """

    def __init__(self, venues_cfg, *, logs_root=None, **_kw):  # noqa: ANN001
        self.venues_cfg = venues_cfg
        self.logs_root = logs_root
        self.calls: list[dict[str, Any]] = []
        self.start_count = 0
        self.stop_count = 0
        self.submit_count = 0
        self.state = "new"
        self._trader_id = "XTRADE-FAKE-001"

    def start(self) -> None:
        self.start_count += 1
        self.state = "ready"

    def stop(self) -> None:
        self.stop_count += 1
        self.state = "stopped"

    def __call__(self, venues_cfg, **kwargs):  # noqa: ANN001
        self.calls.append({"venues_cfg": venues_cfg, **kwargs})
        self.submit_count += 1
        return {
            "passed": True,
            "summary": {"run_id": kwargs.get("run_id"), "submit_index": self.submit_count},
        }


@pytest.fixture
def patch_persistent_executor(monkeypatch):
    """Swap the real `PersistentLiveExecutor` for `_FakePersistentExecutor`.

    Also no-ops the mainnet/testnet safety asserts so tests can pass an
    opaque `object()` as `venues_cfg` without tripping Lock 3 (the real
    locks are exercised by `test_supervisor_safety_locks.py`).

    Returns the list of executors instantiated by `run_supervisor` so
    tests can assert `len(executors) == 1` (single-instance invariant).
    """
    instances: list[_FakePersistentExecutor] = []

    def _factory(*args, **kwargs):
        inst = _FakePersistentExecutor(*args, **kwargs)
        instances.append(inst)
        return inst

    import xtrade.live.mainnet_unlock as unlock_mod  # noqa: PLC0415
    import xtrade.live.persistent_executor as pe_mod  # noqa: PLC0415
    import xtrade.node.factory as factory_mod  # noqa: PLC0415

    monkeypatch.setattr(pe_mod, "PersistentLiveExecutor", _factory)
    monkeypatch.setattr(factory_mod, "_assert_testnet_only", lambda _v: None)
    monkeypatch.setattr(unlock_mod, "assert_mainnet_unlock", lambda _v: None)
    # Phase 6 T2: the supervisor now also calls `is_mainnet_venue` to
    # decide whether to enforce `assert_mainnet_risk_ceiling`. The
    # opaque `object()` sentinel below has no `.binance`/`.hyperliquid`
    # attrs, so report it as testnet (no ceiling check).
    monkeypatch.setattr(unlock_mod, "is_mainnet_venue", lambda _v: False)
    return instances


# --- tests -------------------------------------------------------------


def test_supervisor_starts_and_stops_persistent_node_once(
    patch_persistent_executor, tmp_path: Path, caplog
) -> None:
    """One supervisor run → one PersistentLiveExecutor built → one start, one stop."""
    _seed_signal(tmp_path / "signals", source="momentum:once-1")
    config = _make_config(tmp_path, mode="auto", venues_cfg=object())

    with caplog.at_level(logging.INFO, logger="xtrade.supervisor"):
        run_supervisor(config, max_iterations=1)

    # Exactly one executor instantiated and used.
    assert len(patch_persistent_executor) == 1
    executor = patch_persistent_executor[0]
    assert executor.start_count == 1
    assert executor.stop_count == 1
    assert executor.submit_count == 1  # the one signal we seeded

    # Lifecycle events appear exactly once each.
    starts = [r for r in caplog.records if '"supervisor.node.start"' in r.getMessage()]
    stops = [r for r in caplog.records if '"supervisor.node.stop"' in r.getMessage()]
    assert len(starts) == 1, [r.getMessage() for r in caplog.records]
    assert len(stops) == 1, [r.getMessage() for r in caplog.records]


def test_supervisor_reuses_executor_across_signals(
    patch_persistent_executor, tmp_path: Path
) -> None:
    """3 signals → 3 submits on the SAME executor."""
    # All three signals fit in a single iteration (consumer.iter_new yields them all).
    base = dt.datetime(2026, 5, 24, 12, 0, 0, tzinfo=UTC)
    for i in range(3):
        _seed_signal(
            tmp_path / "signals",
            source=f"momentum:reuse-{i:02d}",
            when=base + dt.timedelta(minutes=i),
        )
    config = _make_config(tmp_path, mode="auto", venues_cfg=object())

    run_supervisor(config, max_iterations=1)

    assert len(patch_persistent_executor) == 1
    executor = patch_persistent_executor[0]
    assert executor.start_count == 1
    assert executor.stop_count == 1
    assert executor.submit_count == 3
    # Every call carried the same venues_cfg (the supervisor's config one).
    venues_seen = {id(c["venues_cfg"]) for c in executor.calls}
    assert len(venues_seen) == 1


def test_kill_switch_persistent_node_false_skips_executor(
    patch_persistent_executor, tmp_path: Path, monkeypatch
) -> None:
    """`persistent_node=False` → supervisor must NOT build a persistent executor."""
    # Stub the legacy run_live import path so the supervisor doesn't try
    # to pull in Nautilus when it falls back to the per-intent runner.
    legacy_calls: list[dict[str, Any]] = []

    def _legacy_run_live(venues_cfg, **kwargs):  # noqa: ANN001
        legacy_calls.append({"venues_cfg": venues_cfg, **kwargs})
        return {"passed": True, "summary": {"run_id": kwargs.get("run_id")}}

    import xtrade.live.runner as runner_mod  # noqa: PLC0415

    monkeypatch.setattr(runner_mod, "run_live", _legacy_run_live)

    _seed_signal(tmp_path / "signals", source="momentum:kill-switch")
    config = _make_config(
        tmp_path,
        mode="auto",
        venues_cfg=object(),
        persistent_node=False,
    )

    run_supervisor(config, max_iterations=1)

    # No persistent executor was instantiated.
    assert patch_persistent_executor == []
    # Legacy run_live was called once.
    assert len(legacy_calls) == 1


def test_injected_executor_skips_persistent_construction(
    patch_persistent_executor, tmp_path: Path
) -> None:
    """When tests inject `live_executor=`, supervisor must NOT build the persistent path."""
    calls: list[dict[str, Any]] = []

    def _injected(venues_cfg, **kwargs):  # noqa: ANN001
        calls.append({"venues_cfg": venues_cfg, **kwargs})
        return {"passed": True, "summary": {"run_id": kwargs.get("run_id")}}

    _seed_signal(tmp_path / "signals", source="momentum:injected")
    # venues_cfg set but executor injected — injected wins.
    config = _make_config(tmp_path, mode="auto", venues_cfg=object())

    run_supervisor(config, live_executor=_injected, max_iterations=1)

    assert patch_persistent_executor == []  # not used
    assert len(calls) == 1


def test_persistent_node_stops_even_when_iteration_crashes(
    patch_persistent_executor, tmp_path: Path, monkeypatch
) -> None:
    """If `_supervisor_iteration` blows up, executor.stop() must still run."""
    # Force iteration to crash by patching out the iteration helper.
    import xtrade.live.supervisor as sup_mod  # noqa: PLC0415

    def _boom(**_kw):
        raise RuntimeError("synthetic iteration crash")

    monkeypatch.setattr(sup_mod, "_supervisor_iteration", _boom)

    config = _make_config(tmp_path, mode="auto", venues_cfg=object())
    run_supervisor(config, max_iterations=1)

    assert len(patch_persistent_executor) == 1
    executor = patch_persistent_executor[0]
    assert executor.start_count == 1
    assert executor.stop_count == 1


def test_no_persistent_executor_when_venues_cfg_is_none(
    patch_persistent_executor, tmp_path: Path, monkeypatch
) -> None:
    """`venues_cfg=None` (offline test default) keeps the legacy path."""
    legacy_calls: list[dict[str, Any]] = []

    def _legacy_run_live(venues_cfg, **kwargs):  # noqa: ANN001
        legacy_calls.append({"venues_cfg": venues_cfg, **kwargs})
        return {"passed": True, "summary": {"run_id": kwargs.get("run_id")}}

    import xtrade.live.runner as runner_mod  # noqa: PLC0415

    monkeypatch.setattr(runner_mod, "run_live", _legacy_run_live)

    _seed_signal(tmp_path / "signals", source="momentum:no-venues")
    config = _make_config(tmp_path, mode="auto", venues_cfg=None)

    run_supervisor(config, max_iterations=1)

    assert patch_persistent_executor == []
    assert len(legacy_calls) == 1


def test_load_supervisor_config_reads_persistent_node_yaml(tmp_path: Path) -> None:
    """Yaml plumbing: `persistent_node: false` propagates into the config."""
    from xtrade.live.supervisor import load_supervisor_config

    cfg_path = tmp_path / "supervisor.yaml"
    cfg_path.write_text(
        "\n".join(
            [
                "instrument_id: BTCUSDT-PERP.BINANCE",
                "strategy_name: momentum_follow",
                f"signals_root: {tmp_path / 'signals'}",
                f"approvals_root: {tmp_path / 'approvals'}",
                f"cursor_path: {tmp_path / 'cursor.json'}",
                f"sentinel_path: {tmp_path / 'paused.flag'}",
                f"logs_root: {tmp_path / 'logs'}",
                "approval_mode: manual",
                "poll_interval_s: 2.0",
                "venue_timeout_s: 60.0",
                "persistent_node: false",
            ]
        )
    )
    cfg = load_supervisor_config(cfg_path)
    assert cfg.persistent_node is False


def test_load_supervisor_config_persistent_node_defaults_true(tmp_path: Path) -> None:
    from xtrade.live.supervisor import load_supervisor_config

    cfg_path = tmp_path / "supervisor.yaml"
    cfg_path.write_text(
        "\n".join(
            [
                "instrument_id: BTCUSDT-PERP.BINANCE",
                "strategy_name: momentum_follow",
                f"signals_root: {tmp_path / 'signals'}",
                f"approvals_root: {tmp_path / 'approvals'}",
                f"cursor_path: {tmp_path / 'cursor.json'}",
                f"sentinel_path: {tmp_path / 'paused.flag'}",
                f"logs_root: {tmp_path / 'logs'}",
                "approval_mode: manual",
            ]
        )
    )
    cfg = load_supervisor_config(cfg_path)
    assert cfg.persistent_node is True
