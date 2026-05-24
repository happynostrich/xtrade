"""Tests for `run_supervisor` cursor + sentinel semantics (Phase 4 Task 5 / T5).

These tests inject a stub `live_executor` so the supervisor's
orchestration is exercised end-to-end without spinning up Nautilus.
Coverage:

- Cursor commit: signals processed in iteration 1 are NOT replayed
  on a fresh supervisor invocation against the same `cursor_path`.
- Mid-batch crash safety: if the strategy raises on one signal, the
  supervisor still commits the cursor for the rest of the batch (the
  brief explicitly states ApprovalGate idempotency makes a full-batch
  replay safe; we model the "happy" case here, plus a strategy crash
  that doesn't tear down the loop).
- Sentinel pause: when the sentinel exists, the supervisor sees the
  new signal but does NOT process it AND does NOT advance the cursor;
  after `Sentinel.resume()` the next iteration picks the signal up.
- `auto` mode: signal → intent → live_executor invoked once.
- `dry_run` mode: signal → intent → NO live_executor invocation.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

# Side-effect: registers `momentum_follow` so `load_strategy` works.
import xtrade.strategy  # noqa: F401
from xtrade.live.sentinel import Sentinel
from xtrade.live.supervisor import SupervisorConfig, run_supervisor
from xtrade.research.signals import Signal, SignalQueue


UTC = dt.timezone.utc
SYMBOL = "BTCUSDT-PERP.BINANCE"


# ---- helpers -------------------------------------------------------------


def _make_config(
    tmp_path: Path,
    *,
    mode: str = "auto",
    sentinel_name: str = "paused.flag",
) -> SupervisorConfig:
    return SupervisorConfig(
        instrument_id=SYMBOL,
        strategy_name="momentum_follow",
        signals_root=tmp_path / "signals",
        approvals_root=tmp_path / "approvals",
        cursor_path=tmp_path / "cursor.json",
        sentinel_path=tmp_path / sentinel_name,
        logs_root=tmp_path / "logs",
        approval_mode=mode,  # type: ignore[arg-type]
        strategy_config={"notional_usd": Decimal("100")},
        poll_interval_s=0.0,
        venue_timeout_s=5.0,
        safety_multiplier=Decimal("0.7"),
        risk_rules=(),
        venues_cfg=None,
        bridge=None,
    )


def _seed_signal(
    signals_root: Path,
    *,
    direction: str = "LONG",
    last_price: str = "50000",
    when: dt.datetime | None = None,
    source: str = "momentum:cursor-test",
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


def _make_executor():
    """Return (executor_callable, calls_log)."""
    calls: list[dict[str, Any]] = []

    def _exec(venues_cfg, **kwargs):  # noqa: ANN001
        calls.append({"venues_cfg": venues_cfg, **kwargs})
        return {"passed": True, "summary": {"run_id": kwargs.get("run_id")}}

    return _exec, calls


# ---- cursor persistence --------------------------------------------------


def test_auto_mode_submits_signal_and_advances_cursor(tmp_path: Path) -> None:
    config = _make_config(tmp_path, mode="auto")
    _seed_signal(config.signals_root)
    executor, calls = _make_executor()

    results = run_supervisor(
        config, live_executor=executor, max_iterations=1,
    )

    assert len(results) == 1
    r0 = results[0]
    assert r0.paused is False
    assert r0.signals_seen == 1
    assert r0.signals_processed == 1
    assert r0.intents_submitted == 1
    assert r0.intents_parked_manual == 0
    assert len(calls) == 1
    # Submission carried the right quantity (100 / 50000 = 0.002).
    assert Decimal(str(calls[0]["quantity"])) == Decimal("0.002")
    # Cursor file was written.
    assert config.cursor_path.exists()


def test_cursor_prevents_replay_across_restart(tmp_path: Path) -> None:
    """Second `run_supervisor` invocation must NOT re-submit prior signals."""
    config = _make_config(tmp_path, mode="auto")
    _seed_signal(config.signals_root)
    executor, calls = _make_executor()

    run_supervisor(config, live_executor=executor, max_iterations=1)
    assert len(calls) == 1

    # Simulated restart — fresh supervisor, same cursor_path.
    executor2, calls2 = _make_executor()
    results2 = run_supervisor(
        config, live_executor=executor2, max_iterations=1,
    )

    assert results2[0].signals_seen == 0
    assert results2[0].intents_submitted == 0
    assert calls2 == []


def test_new_signal_after_restart_is_picked_up(tmp_path: Path) -> None:
    config = _make_config(tmp_path, mode="auto")
    _seed_signal(config.signals_root, source="momentum:first")
    executor, calls = _make_executor()
    run_supervisor(config, live_executor=executor, max_iterations=1)
    assert len(calls) == 1

    # Add a second, distinct signal then restart.
    _seed_signal(
        config.signals_root,
        source="momentum:second",
        when=dt.datetime(2026, 5, 24, 13, 0, 0, tzinfo=UTC),
    )
    executor2, calls2 = _make_executor()
    results = run_supervisor(
        config, live_executor=executor2, max_iterations=1,
    )
    assert results[0].signals_seen == 1
    assert len(calls2) == 1


# ---- dry-run mode --------------------------------------------------------


def test_dry_run_mode_records_but_does_not_submit(tmp_path: Path) -> None:
    config = _make_config(tmp_path, mode="dry_run")
    _seed_signal(config.signals_root)
    executor, calls = _make_executor()

    results = run_supervisor(
        config, live_executor=executor, max_iterations=1,
    )

    assert results[0].signals_seen == 1
    assert results[0].intents_submitted == 0
    assert results[0].intents_parked_manual == 0
    # No actual submission.
    assert calls == []
    # But the approval queue holds the audit row.
    queue_files = list((tmp_path / "approvals").glob("*.jsonl"))
    assert len(queue_files) == 1
    assert "dry_run" in queue_files[0].read_text(encoding="utf-8")


# ---- sentinel pause ------------------------------------------------------


def test_paused_supervisor_does_not_process_or_commit(tmp_path: Path) -> None:
    config = _make_config(tmp_path, mode="auto")
    _seed_signal(config.signals_root)
    Sentinel(config.sentinel_path).pause(reason="under test")
    executor, calls = _make_executor()

    results = run_supervisor(
        config, live_executor=executor, max_iterations=1,
    )

    r0 = results[0]
    assert r0.paused is True
    assert r0.signals_seen == 0
    assert r0.intents_submitted == 0
    assert calls == []
    # Cursor must NOT advance while paused (so resume replays).
    # We verify this indirectly: resume + re-run picks up the signal.
    Sentinel(config.sentinel_path).resume()
    executor2, calls2 = _make_executor()
    results2 = run_supervisor(
        config, live_executor=executor2, max_iterations=1,
    )
    assert results2[0].signals_seen == 1
    assert len(calls2) == 1


# ---- crash safety inside a single iteration ------------------------------


def test_executor_crash_does_not_tear_down_loop(tmp_path: Path) -> None:
    """A crashing live_executor must not crash the supervisor; the loop
    surfaces the error in the iteration result and keeps running."""
    config = _make_config(tmp_path, mode="auto")
    _seed_signal(config.signals_root)

    def _boom(venues_cfg, **kwargs):  # noqa: ANN001
        raise RuntimeError("simulated venue blow-up")

    results = run_supervisor(
        config, live_executor=_boom, max_iterations=2,
    )

    # The supervisor caught the per-intent exception (logged via the
    # `_route_intent` try/except) and continued, so we have 2 results.
    assert len(results) == 2
    # No submissions credited (executor raised) but processing happened.
    assert results[0].signals_seen == 1
    assert results[0].intents_submitted == 0


# ---- stop_event short-circuit --------------------------------------------


def test_stop_event_breaks_loop_immediately(tmp_path: Path) -> None:
    import threading

    config = _make_config(tmp_path, mode="auto")
    stop_event = threading.Event()
    stop_event.set()  # already set before the loop starts

    executor, calls = _make_executor()
    results = run_supervisor(
        config,
        stop_event=stop_event,
        live_executor=executor,
        max_iterations=10,
    )
    assert results == []
    assert calls == []
