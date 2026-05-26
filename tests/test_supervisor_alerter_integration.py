"""Integration tests for supervisor wire-up of AlertBridge + HeartbeatWatcher.

Phase 6 Task T9 (see `docs/phase6_brief.md` §5 T9). Verifies that the
supervisor:

1. Pushes a `supervisor.start` info alert at startup when an alerter
   is configured.
2. Ticks the heartbeat watcher every iteration; bumps activity only
   when the iteration actually did work.
3. Emits a heartbeat warn alert after the supervisor idles past
   `idle_warn_s` and the clock advances past the threshold.
4. Closes the alerter at shutdown.

The tests use a `FakeAlerter` (no httpx) and a controllable clock
injected via `SupervisorConfig.heartbeat`'s own `clock=...`.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path
from typing import Any

import xtrade.strategy  # noqa: F401  side-effect: registers momentum_follow

from xtrade.bridge.alerter import AlertDispatchResult
from xtrade.live.heartbeat import HeartbeatWatcher
from xtrade.live.supervisor import SupervisorConfig, run_supervisor
from xtrade.research.signals import Signal, SignalQueue


UTC = dt.timezone.utc
SYMBOL = "BTCUSDT-PERP.BINANCE"


# ---- fakes ----------------------------------------------------------------


class FakeAlerter:
    """In-memory stand-in for AlertBridge. Records every dispatch + close."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.closed: int = 0
        self._counter = 0

    def dispatch_alert(
        self,
        *,
        severity: str,
        event: str,
        message: str,
        instrument: str | None = None,
        fields: dict[str, Any] | None = None,
    ) -> AlertDispatchResult:
        self._counter += 1
        self.calls.append(
            {
                "severity": severity,
                "event": event,
                "message": message,
                "instrument": instrument,
                "fields": dict(fields or {}),
            }
        )
        return AlertDispatchResult(
            alert_id=f"AL-fake-{self._counter}",
            severity=severity,
            event=event,
            ok=True,
            status_code=200,
            attempts=1,
            elapsed_s=0.001,
            error=None,
            response_excerpt=None,
            dispatched_at=dt.datetime(2026, 5, 24, 12, 0, tzinfo=UTC),
        )

    def close(self) -> None:
        self.closed += 1


class _MutableClock:
    """Mutable UTC clock for both the supervisor and the heartbeat watcher."""

    def __init__(self, start: dt.datetime) -> None:
        self.now: dt.datetime = start

    def __call__(self) -> dt.datetime:
        return self.now


# ---- helpers --------------------------------------------------------------


def _make_config(
    tmp_path: Path,
    *,
    alerter: FakeAlerter | None,
    heartbeat: HeartbeatWatcher | None,
    mode: str = "auto",
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
        venues_cfg=None,
        bridge=None,
        alerter=alerter,  # type: ignore[arg-type]
        heartbeat=heartbeat,
    )


def _seed_signal(signals_root: Path) -> Signal:
    sig = Signal(
        symbol=SYMBOL,
        venue="binance",
        direction="LONG",
        strength=0.6,
        generated_at=dt.datetime(2026, 5, 24, 12, 0, 0, tzinfo=UTC),
        source="momentum:integration",
        metadata={"last_price": "50000"},
    )
    SignalQueue(signals_root).append([sig])
    return sig


def _executor_stub() -> tuple[Any, list[dict[str, Any]]]:
    calls: list[dict[str, Any]] = []

    def _exec(venues_cfg, **kwargs):  # noqa: ANN001
        calls.append({"venues_cfg": venues_cfg, **kwargs})
        return {"passed": True, "summary": {}}

    return _exec, calls


# ---- supervisor.start alert ----------------------------------------------


def test_start_pushes_info_alert(tmp_path: Path) -> None:
    alerter = FakeAlerter()
    config = _make_config(tmp_path, alerter=alerter, heartbeat=None)
    executor, _ = _executor_stub()

    run_supervisor(config, live_executor=executor, max_iterations=1)

    assert len(alerter.calls) >= 1
    start_call = alerter.calls[0]
    assert start_call["severity"] == "info"
    assert start_call["event"] == "supervisor.start"
    assert start_call["instrument"] == SYMBOL
    assert start_call["fields"]["strategy"] == "momentum_follow"
    assert start_call["fields"]["mode"] == "auto"


def test_start_alert_skipped_when_no_alerter(tmp_path: Path) -> None:
    config = _make_config(tmp_path, alerter=None, heartbeat=None)
    executor, _ = _executor_stub()
    # Must not raise even though alerter is None.
    results = run_supervisor(config, live_executor=executor, max_iterations=1)
    assert len(results) == 1


# ---- heartbeat: real work bumps activity ---------------------------------


def test_heartbeat_records_activity_on_processed_signal(tmp_path: Path) -> None:
    alerter = FakeAlerter()
    base = dt.datetime(2026, 5, 24, 12, 0, 0, tzinfo=UTC)
    clock = _MutableClock(base)

    hb = HeartbeatWatcher(
        alerter=alerter,  # type: ignore[arg-type]
        idle_warn_s=60.0,
        idle_crit_s=180.0,
        instrument=SYMBOL,
        clock=clock,
        start_ts=base,
    )
    config = _make_config(tmp_path, alerter=alerter, heartbeat=hb)
    _seed_signal(config.signals_root)
    executor, _ = _executor_stub()

    run_supervisor(
        config, live_executor=executor, max_iterations=1, clock=clock,
    )

    # The iteration processed a signal → record_activity bumped to base.
    # No threshold was crossed → no idle alert fired.
    severities = [c["severity"] for c in alerter.calls]
    events = [c["event"] for c in alerter.calls]
    # Exactly one alert: the supervisor.start info.
    assert severities == ["info"]
    assert events == ["supervisor.start"]
    assert hb.last_activity_ts == base


# ---- heartbeat: silent loop escalates ------------------------------------


def test_heartbeat_warn_alert_after_idle_warn_s(tmp_path: Path) -> None:
    alerter = FakeAlerter()
    base = dt.datetime(2026, 5, 24, 12, 0, 0, tzinfo=UTC)
    clock = _MutableClock(base)

    hb = HeartbeatWatcher(
        alerter=alerter,  # type: ignore[arg-type]
        idle_warn_s=60.0,
        idle_crit_s=180.0,
        instrument=SYMBOL,
        clock=clock,
        start_ts=base,
    )
    config = _make_config(tmp_path, alerter=alerter, heartbeat=hb)
    executor, _ = _executor_stub()

    # Drive 3 idle iterations across a 90-second jump per iteration.
    iter_calls = 0

    def _advancing_clock() -> dt.datetime:
        nonlocal iter_calls
        # Hold the clock per iteration; bump between iterations.
        return clock.now

    # Run iteration 1 at t=0 (no work, no signals).
    run_supervisor(config, live_executor=executor, max_iterations=1, clock=clock)

    # Advance clock past warn threshold; run iteration 2.
    clock.now = base + dt.timedelta(seconds=90)
    run_supervisor(config, live_executor=executor, max_iterations=1, clock=clock)

    # Expect: start info (twice, once per run_supervisor invocation) +
    # heartbeat warn (once, on the second run after the clock advanced).
    severities = [c["severity"] for c in alerter.calls]
    events = [c["event"] for c in alerter.calls]
    # At least one warn idle alert must appear.
    warn_idx = [
        i for i, e in enumerate(events)
        if events[i] == "supervisor.heartbeat.idle" and severities[i] == "warn"
    ]
    assert warn_idx, f"no warn heartbeat alert in {alerter.calls!r}"
    assert hb.current_level == "warn"


def test_heartbeat_crit_alert_after_idle_crit_s(tmp_path: Path) -> None:
    alerter = FakeAlerter()
    base = dt.datetime(2026, 5, 24, 12, 0, 0, tzinfo=UTC)
    clock = _MutableClock(base)

    hb = HeartbeatWatcher(
        alerter=alerter,  # type: ignore[arg-type]
        idle_warn_s=60.0,
        idle_crit_s=180.0,
        instrument=SYMBOL,
        clock=clock,
        start_ts=base,
    )
    config = _make_config(tmp_path, alerter=alerter, heartbeat=hb)
    executor, _ = _executor_stub()

    # Jump past crit threshold in one go.
    clock.now = base + dt.timedelta(seconds=300)
    run_supervisor(config, live_executor=executor, max_iterations=1, clock=clock)

    # The heartbeat tick must have escalated straight to crit.
    crit_events = [
        c for c in alerter.calls
        if c["severity"] == "crit" and c["event"] == "supervisor.heartbeat.idle"
    ]
    assert len(crit_events) == 1
    assert hb.current_level == "crit"


# ---- heartbeat: recovery info push ---------------------------------------


def test_heartbeat_recovery_pushes_info(tmp_path: Path) -> None:
    alerter = FakeAlerter()
    base = dt.datetime(2026, 5, 24, 12, 0, 0, tzinfo=UTC)
    clock = _MutableClock(base)

    hb = HeartbeatWatcher(
        alerter=alerter,  # type: ignore[arg-type]
        idle_warn_s=60.0,
        idle_crit_s=180.0,
        instrument=SYMBOL,
        clock=clock,
        start_ts=base,
    )
    config = _make_config(tmp_path, alerter=alerter, heartbeat=hb)
    executor, _ = _executor_stub()

    # 1) Idle past warn threshold.
    clock.now = base + dt.timedelta(seconds=90)
    run_supervisor(config, live_executor=executor, max_iterations=1, clock=clock)
    assert hb.current_level == "warn"

    # 2) Now feed a signal so the supervisor records activity.
    _seed_signal(config.signals_root)
    clock.now = base + dt.timedelta(seconds=100)
    run_supervisor(config, live_executor=executor, max_iterations=1, clock=clock)
    # Watcher should now be back at info.
    assert hb.current_level == "info"

    recovery_events = [
        c for c in alerter.calls
        if c["event"] == "supervisor.heartbeat.recovered"
    ]
    assert len(recovery_events) == 1
    assert recovery_events[0]["severity"] == "info"


# ---- shutdown closes alerter ---------------------------------------------


def test_alerter_closed_on_supervisor_exit(tmp_path: Path) -> None:
    alerter = FakeAlerter()
    config = _make_config(tmp_path, alerter=alerter, heartbeat=None)
    executor, _ = _executor_stub()

    run_supervisor(config, live_executor=executor, max_iterations=1)

    assert alerter.closed == 1


# ---- pause does not bump activity ----------------------------------------


def test_paused_iteration_does_not_bump_activity(tmp_path: Path) -> None:
    alerter = FakeAlerter()
    base = dt.datetime(2026, 5, 24, 12, 0, 0, tzinfo=UTC)
    clock = _MutableClock(base)

    hb = HeartbeatWatcher(
        alerter=alerter,  # type: ignore[arg-type]
        idle_warn_s=60.0,
        idle_crit_s=180.0,
        instrument=SYMBOL,
        clock=clock,
        start_ts=base,
    )
    config = _make_config(tmp_path, alerter=alerter, heartbeat=hb)
    # Pre-pause the sentinel so the iteration goes through the paused branch.
    from xtrade.live.sentinel import Sentinel
    Sentinel(config.sentinel_path).pause(reason="test")

    executor, _ = _executor_stub()
    # Advance clock past warn threshold; if pause counted as activity
    # the heartbeat would not fire.
    clock.now = base + dt.timedelta(seconds=90)
    run_supervisor(config, live_executor=executor, max_iterations=1, clock=clock)

    warn_calls = [
        c for c in alerter.calls
        if c["severity"] == "warn" and c["event"] == "supervisor.heartbeat.idle"
    ]
    assert warn_calls, "paused iteration must not be counted as heartbeat activity"
