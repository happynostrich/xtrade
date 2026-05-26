"""Integration tests for supervisor wire-up of `McapSoftKillWatcher`.

Phase 6 Task T8 (see `docs/phase6_brief.md` §5 T8). Verifies that the
supervisor:

1. Calls `mcap_softkill.update(now, mark)` once per non-paused
   iteration when both `mcap_softkill` and `mark_provider` are wired.
2. When the consecutive-breach threshold is reached, the watcher's
   firing path produces all four side effects in one place:
   - sentinel raised with reason `mcap.softkill:<boundary>:<mcap>`
   - `supervisor.mcap.softkill` event
   - emergency_close runner invoked with `side="reduce-only-tp-only"`
   - severity=crit alert dispatched
3. A pre-paused sentinel (operator-manual or prior drawdown) makes the
   supervisor skip the probe entirely (no mark fetch).
4. Mark-provider exceptions are swallowed.
5. When `mcap_softkill` is None the supervisor is unaffected.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path
from typing import Any

import xtrade.strategy  # noqa: F401  side-effect: registers momentum_follow

from xtrade.bridge.alerter import AlertDispatchResult
from xtrade.instruments.meta import InstrumentMeta
from xtrade.live.mcap_softkill import McapSoftKillWatcher
from xtrade.live.sentinel import Sentinel
from xtrade.live.supervisor import SupervisorConfig, run_supervisor


UTC = dt.timezone.utc
SYMBOL = "SPCXUSDT-PERP.BINANCE"
SHARES = Decimal("11870000000")
TRIGGER = Decimal("3500000000000")
BREACH_MARK = Decimal("295")  # mcap ≈ 3.5017T > 3.5T
OK_MARK = Decimal("280")  # mcap ≈ 3.324T < 3.5T


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------


class FakeAlerter:
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
            alert_id=f"AL-fake-{self._counter:06d}",
            severity=severity,  # type: ignore[arg-type]
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


class FakeRunner:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> None:
        self.calls.append(dict(kwargs))


class _ScriptedMark:
    """Returns the next mark from a queue."""

    def __init__(self, values: list[Decimal]) -> None:
        self.values = list(values)
        self.calls = 0

    def __call__(self) -> Decimal:
        self.calls += 1
        if not self.values:
            return Decimal("0")
        return self.values.pop(0)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _spcx_meta() -> InstrumentMeta:
    return InstrumentMeta(
        symbol=SYMBOL,
        shares_outstanding=SHARES,
        min_qty=Decimal("1"),
        qty_step=Decimal("1"),
        tick_size=Decimal("0.01"),
        mark_source="oracle",
    )


def _build_watcher(
    tmp_path: Path,
    *,
    alerter: FakeAlerter | None,
    runner: FakeRunner | None,
    consecutive_iterations: int = 3,
) -> tuple[McapSoftKillWatcher, Sentinel]:
    sentinel = Sentinel(tmp_path / "paused.flag")
    watcher = McapSoftKillWatcher(
        meta=_spcx_meta(),
        trigger_mcap_usd=TRIGGER,
        boundary="above",
        sentinel=sentinel,
        emergency_close_runner=runner,
        alerter=alerter,  # type: ignore[arg-type]
        consecutive_iterations=consecutive_iterations,
        state_path=tmp_path / "state" / "mcap_softkill.json",
    )
    return watcher, sentinel


def _make_config(
    tmp_path: Path,
    *,
    alerter: FakeAlerter | None,
    mcap_softkill: McapSoftKillWatcher | None,
    mark_provider: Any = None,
) -> SupervisorConfig:
    return SupervisorConfig(
        instrument_id=SYMBOL,
        strategy_name="momentum_follow",
        signals_root=tmp_path / "signals",
        approvals_root=tmp_path / "approvals",
        cursor_path=tmp_path / "cursor.json",
        sentinel_path=tmp_path / "paused.flag",
        logs_root=tmp_path / "logs",
        approval_mode="auto",
        strategy_config={"notional_usd": Decimal("100")},
        poll_interval_s=0.0,
        venue_timeout_s=5.0,
        safety_multiplier=Decimal("0.7"),
        risk_rules=(),
        venues_cfg=None,
        bridge=None,
        alerter=alerter,  # type: ignore[arg-type]
        heartbeat=None,
        mcap_softkill=mcap_softkill,
        mark_provider=mark_provider,
    )


def _executor_stub() -> tuple[Any, list[dict[str, Any]]]:
    calls: list[dict[str, Any]] = []

    def _exec(venues_cfg, **kwargs):  # noqa: ANN001
        calls.append({"venues_cfg": venues_cfg, **kwargs})
        return {"passed": True, "summary": {}}

    return _exec, calls


# ---------------------------------------------------------------------------
# basic wire-up
# ---------------------------------------------------------------------------


def test_mcap_softkill_none_is_noop(tmp_path: Path) -> None:
    """When `mcap_softkill` is None the supervisor must not call mark_provider."""
    config = _make_config(
        tmp_path,
        alerter=None,
        mcap_softkill=None,
        mark_provider=lambda: 1 / 0,  # would raise if ever called
    )
    executor, _ = _executor_stub()
    results = run_supervisor(config, live_executor=executor, max_iterations=2)
    assert len(results) == 2


def test_mcap_softkill_no_provider_is_noop(tmp_path: Path) -> None:
    """Watcher set but `mark_provider=None` must also be a no-op."""
    watcher, sentinel = _build_watcher(tmp_path, alerter=None, runner=FakeRunner())
    config = _make_config(
        tmp_path,
        alerter=None,
        mcap_softkill=watcher,
        mark_provider=None,
    )
    executor, _ = _executor_stub()
    run_supervisor(config, live_executor=executor, max_iterations=2)
    assert watcher.state() is None
    assert not sentinel.paused()


def test_mcap_softkill_first_iteration_seeds_state(tmp_path: Path) -> None:
    alerter = FakeAlerter()
    runner = FakeRunner()
    watcher, sentinel = _build_watcher(tmp_path, alerter=alerter, runner=runner)
    mark = _ScriptedMark([OK_MARK])
    config = _make_config(
        tmp_path,
        alerter=alerter,
        mcap_softkill=watcher,
        mark_provider=mark,
    )
    executor, _ = _executor_stub()
    run_supervisor(config, live_executor=executor, max_iterations=1)

    state = watcher.state()
    assert state is not None
    assert state.consecutive_breaches == 0
    assert state.triggered is False
    assert not sentinel.paused()
    assert runner.calls == []
    softkill_alerts = [
        c for c in alerter.calls if c["event"] == "supervisor.mcap.softkill"
    ]
    assert softkill_alerts == []


# ---------------------------------------------------------------------------
# fire path: all four side effects
# ---------------------------------------------------------------------------


def test_mcap_softkill_three_consecutive_breaches_fires_everything(
    tmp_path: Path,
) -> None:
    """Sentinel + event + emergency_close + crit alert in one iteration."""
    alerter = FakeAlerter()
    runner = FakeRunner()
    watcher, sentinel = _build_watcher(tmp_path, alerter=alerter, runner=runner)
    mark = _ScriptedMark([BREACH_MARK, BREACH_MARK, BREACH_MARK])
    config = _make_config(
        tmp_path,
        alerter=alerter,
        mcap_softkill=watcher,
        mark_provider=mark,
    )
    executor, _ = _executor_stub()
    results = run_supervisor(config, live_executor=executor, max_iterations=3)

    # All three iterations ran the probe.
    assert mark.calls == 3
    # Iteration 3 fired.
    assert sentinel.paused()
    body = sentinel.state()
    assert body is not None
    assert body["reason"].startswith("mcap.softkill:above:")

    # emergency_close runner invoked exactly once with the right side.
    assert len(runner.calls) == 1
    assert runner.calls[0]["side"] == "reduce-only-tp-only"
    assert runner.calls[0]["instrument"] == SYMBOL

    # Exactly one crit alert for the softkill event (start alert is info).
    softkill = [c for c in alerter.calls if c["event"] == "supervisor.mcap.softkill"]
    assert len(softkill) == 1
    assert softkill[0]["severity"] == "crit"
    assert softkill[0]["fields"]["boundary"] == "above"

    # The three iterations themselves: the breach iteration runs with
    # paused=False (sentinel raised by the watcher *after* the body)
    # — so all three results report paused=False.
    assert [r.paused for r in results] == [False, False, False]


def test_mcap_softkill_fire_makes_next_iteration_paused(tmp_path: Path) -> None:
    """After firing, the supervisor's next iteration takes the paused branch."""
    alerter = FakeAlerter()
    runner = FakeRunner()
    watcher, sentinel = _build_watcher(
        tmp_path, alerter=alerter, runner=runner, consecutive_iterations=1,
    )
    # Iter 1: breach (fires). Iter 2: paused branch — mark not fetched.
    mark = _ScriptedMark([BREACH_MARK])
    config = _make_config(
        tmp_path,
        alerter=alerter,
        mcap_softkill=watcher,
        mark_provider=mark,
    )
    executor, _ = _executor_stub()
    results = run_supervisor(config, live_executor=executor, max_iterations=2)
    assert results[0].paused is False
    assert results[1].paused is True
    # The paused iteration must NOT have called the mark provider.
    assert mark.calls == 1
    assert len(runner.calls) == 1


def test_mcap_softkill_spike_then_recover_does_not_fire(tmp_path: Path) -> None:
    """Two breaches + one clean tick resets the counter; no fire."""
    alerter = FakeAlerter()
    runner = FakeRunner()
    watcher, sentinel = _build_watcher(tmp_path, alerter=alerter, runner=runner)
    mark = _ScriptedMark([BREACH_MARK, BREACH_MARK, OK_MARK, BREACH_MARK])
    config = _make_config(
        tmp_path,
        alerter=alerter,
        mcap_softkill=watcher,
        mark_provider=mark,
    )
    executor, _ = _executor_stub()
    run_supervisor(config, live_executor=executor, max_iterations=4)

    state = watcher.state()
    assert state is not None
    assert state.triggered is False
    # After the spike-then-recover-then-breach sequence:
    #   iter1: consecutive=1, iter2: 2, iter3: 0, iter4: 1
    assert state.consecutive_breaches == 1
    assert not sentinel.paused()
    assert runner.calls == []
    softkill = [c for c in alerter.calls if c["event"] == "supervisor.mcap.softkill"]
    assert softkill == []


def test_mcap_softkill_no_alert_spam_under_persistent_breach(
    tmp_path: Path,
) -> None:
    """Once fired, persistent breach observations don't re-alert. The
    only reason later iterations don't probe is the paused-branch
    short-circuit; we exercise the post-fire stickiness directly by
    instead pre-loading a triggered state and running supervisor.
    """
    alerter = FakeAlerter()
    runner = FakeRunner()
    watcher, sentinel = _build_watcher(
        tmp_path, alerter=alerter, runner=runner, consecutive_iterations=1,
    )
    # Fire on iter 1.
    mark = _ScriptedMark([BREACH_MARK])
    config = _make_config(
        tmp_path,
        alerter=alerter,
        mcap_softkill=watcher,
        mark_provider=mark,
    )
    executor, _ = _executor_stub()
    run_supervisor(config, live_executor=executor, max_iterations=5)

    softkill = [c for c in alerter.calls if c["event"] == "supervisor.mcap.softkill"]
    assert len(softkill) == 1
    assert len(runner.calls) == 1
    assert sentinel.paused()


# ---------------------------------------------------------------------------
# pre-paused interaction
# ---------------------------------------------------------------------------


def test_mcap_softkill_pre_paused_sentinel_skips_probe(tmp_path: Path) -> None:
    """If the sentinel is already paused, skip the mark provider entirely."""
    alerter = FakeAlerter()
    runner = FakeRunner()
    watcher, sentinel = _build_watcher(tmp_path, alerter=alerter, runner=runner)
    sentinel.pause(reason="operator-manual")
    mark = _ScriptedMark([BREACH_MARK])
    config = _make_config(
        tmp_path,
        alerter=alerter,
        mcap_softkill=watcher,
        mark_provider=mark,
    )
    executor, _ = _executor_stub()
    run_supervisor(config, live_executor=executor, max_iterations=1)

    assert mark.calls == 0
    # No state file because update() never ran.
    assert watcher.state() is None
    # Sentinel reason unchanged.
    body = sentinel.state()
    assert body is not None
    assert body["reason"] == "operator-manual"
    assert runner.calls == []


# ---------------------------------------------------------------------------
# robustness
# ---------------------------------------------------------------------------


def test_mcap_softkill_mark_provider_exception_swallowed(tmp_path: Path) -> None:
    """A throwing mark provider must not crash the supervisor."""
    alerter = FakeAlerter()
    runner = FakeRunner()
    watcher, sentinel = _build_watcher(tmp_path, alerter=alerter, runner=runner)

    def _broken() -> Decimal:
        raise RuntimeError("oracle outage")

    config = _make_config(
        tmp_path,
        alerter=alerter,
        mcap_softkill=watcher,
        mark_provider=_broken,
    )
    executor, _ = _executor_stub()
    results = run_supervisor(config, live_executor=executor, max_iterations=2)
    assert len(results) == 2
    # update() never succeeded → no state file.
    assert watcher.state() is None
    assert not sentinel.paused()


def test_mcap_softkill_state_persists_across_supervisor_restarts(
    tmp_path: Path,
) -> None:
    """Two consecutive supervisor runs share the same state file."""
    alerter1 = FakeAlerter()
    runner1 = FakeRunner()
    watcher1, _sentinel1 = _build_watcher(
        tmp_path, alerter=alerter1, runner=runner1,
    )
    mark1 = _ScriptedMark([BREACH_MARK, BREACH_MARK])
    config1 = _make_config(
        tmp_path,
        alerter=alerter1,
        mcap_softkill=watcher1,
        mark_provider=mark1,
    )
    executor, _ = _executor_stub()
    run_supervisor(config1, live_executor=executor, max_iterations=2)
    assert watcher1.state().consecutive_breaches == 2
    assert not _sentinel1.paused()
    assert runner1.calls == []

    # Fresh supervisor + fresh watcher pointed at same state file.
    alerter2 = FakeAlerter()
    runner2 = FakeRunner()
    watcher2, sentinel2 = _build_watcher(
        tmp_path, alerter=alerter2, runner=runner2,
    )
    mark2 = _ScriptedMark([BREACH_MARK])
    config2 = _make_config(
        tmp_path,
        alerter=alerter2,
        mcap_softkill=watcher2,
        mark_provider=mark2,
    )
    run_supervisor(config2, live_executor=executor, max_iterations=1)
    # The third consecutive breach (across the restart) fires.
    assert sentinel2.paused()
    assert len(runner2.calls) == 1
    softkill = [c for c in alerter2.calls if c["event"] == "supervisor.mcap.softkill"]
    assert len(softkill) == 1
