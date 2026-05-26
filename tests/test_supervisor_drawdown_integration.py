"""Integration tests for supervisor wire-up of `DrawdownWatcher`.

Phase 6 Task T7 (see `docs/phase6_brief.md` §5 T7). Verifies that the
supervisor:

1. Calls `drawdown.update(now, equity_usd)` once per non-paused
   iteration when both `drawdown` and `equity_provider` are configured.
2. On the iteration whose equity dips ≥ `halt_pct` below HWM, the
   sentinel becomes paused (reason starts with `drawdown.halt:`),
   one severity=crit alert is dispatched, and the next iteration is
   the standard "paused → skip new signals" branch.
3. A paused iteration does NOT call `drawdown.update` (no overwrite of
   sentinel reason while already paused).
4. Equity provider exceptions are swallowed.
5. When `drawdown` is None (default), the supervisor is unaffected.

Uses the same `FakeAlerter` shape and executor stub as the alerter
integration suite.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path
from typing import Any

import xtrade.strategy  # noqa: F401  side-effect: registers momentum_follow

from xtrade.bridge.alerter import AlertDispatchResult
from xtrade.live.drawdown import DrawdownWatcher
from xtrade.live.sentinel import Sentinel
from xtrade.live.supervisor import SupervisorConfig, run_supervisor


UTC = dt.timezone.utc
SYMBOL = "BTCUSDT-PERP.BINANCE"


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------


class FakeAlerter:
    """In-memory stand-in for AlertBridge."""

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


class _ScriptedEquity:
    """Returns the next equity from a queue; raises if asked too many times."""

    def __init__(self, values: list[Decimal]) -> None:
        self.values = list(values)
        self.calls = 0

    def __call__(self) -> Decimal:
        self.calls += 1
        if not self.values:
            # Hold the last value indefinitely if the test undercounts.
            return Decimal("0")
        return self.values.pop(0)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _make_config(
    tmp_path: Path,
    *,
    alerter: FakeAlerter | None,
    drawdown: DrawdownWatcher | None,
    equity_provider: Any = None,
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
        heartbeat=None,
        drawdown=drawdown,
        equity_provider=equity_provider,
    )


def _executor_stub() -> tuple[Any, list[dict[str, Any]]]:
    calls: list[dict[str, Any]] = []

    def _exec(venues_cfg, **kwargs):  # noqa: ANN001
        calls.append({"venues_cfg": venues_cfg, **kwargs})
        return {"passed": True, "summary": {}}

    return _exec, calls


def _build_watcher(
    tmp_path: Path,
    *,
    alerter: FakeAlerter | None,
    halt_pct: Decimal = Decimal("0.05"),
) -> tuple[DrawdownWatcher, Sentinel]:
    sentinel = Sentinel(tmp_path / "paused.flag")
    watcher = DrawdownWatcher(
        hwm_path=tmp_path / "state" / "drawdown.json",
        sentinel=sentinel,
        halt_pct=halt_pct,
        alerter=alerter,  # type: ignore[arg-type]
        instrument=SYMBOL,
    )
    return watcher, sentinel


# ---------------------------------------------------------------------------
# basic wire-up
# ---------------------------------------------------------------------------


def test_drawdown_none_is_noop(tmp_path: Path) -> None:
    """When `drawdown` is None the supervisor must not consult equity_provider."""
    config = _make_config(
        tmp_path,
        alerter=None,
        drawdown=None,
        equity_provider=lambda: 1 / 0,  # would raise if ever called
    )
    executor, _ = _executor_stub()
    results = run_supervisor(config, live_executor=executor, max_iterations=2)
    assert len(results) == 2


def test_drawdown_no_provider_is_noop(tmp_path: Path) -> None:
    """`drawdown` set but `equity_provider` None must also be a no-op."""
    watcher, sentinel = _build_watcher(tmp_path, alerter=None)
    config = _make_config(
        tmp_path,
        alerter=None,
        drawdown=watcher,
        equity_provider=None,
    )
    executor, _ = _executor_stub()
    run_supervisor(config, live_executor=executor, max_iterations=2)
    # No state file was written because update() was never called.
    assert watcher.state() is None
    assert not sentinel.paused()


def test_drawdown_first_iteration_seeds_hwm(tmp_path: Path) -> None:
    alerter = FakeAlerter()
    watcher, sentinel = _build_watcher(tmp_path, alerter=alerter)
    equity = _ScriptedEquity([Decimal("200")])
    config = _make_config(
        tmp_path,
        alerter=alerter,
        drawdown=watcher,
        equity_provider=equity,
    )
    executor, _ = _executor_stub()

    run_supervisor(config, live_executor=executor, max_iterations=1)

    state = watcher.state()
    assert state is not None
    assert state.hwm_usd == Decimal("200")
    assert state.last_equity_usd == Decimal("200")
    assert state.halted is False
    assert not sentinel.paused()
    # Only the supervisor.start info alert — no halt alert yet.
    halt_calls = [c for c in alerter.calls if c["event"] == "supervisor.drawdown.halt"]
    assert halt_calls == []


def test_drawdown_tracks_new_highs(tmp_path: Path) -> None:
    alerter = FakeAlerter()
    watcher, sentinel = _build_watcher(tmp_path, alerter=alerter)
    equity = _ScriptedEquity([Decimal("200"), Decimal("220"), Decimal("250")])
    config = _make_config(
        tmp_path,
        alerter=alerter,
        drawdown=watcher,
        equity_provider=equity,
    )
    executor, _ = _executor_stub()
    run_supervisor(config, live_executor=executor, max_iterations=3)

    state = watcher.state()
    assert state is not None
    assert state.hwm_usd == Decimal("250")
    assert state.last_equity_usd == Decimal("250")
    assert state.halted is False
    assert equity.calls == 3
    assert not sentinel.paused()


# ---------------------------------------------------------------------------
# halt behaviour
# ---------------------------------------------------------------------------


def test_drawdown_breach_pauses_sentinel_and_alerts(tmp_path: Path) -> None:
    alerter = FakeAlerter()
    watcher, sentinel = _build_watcher(tmp_path, alerter=alerter)
    # Iter 1: 200 (seed). Iter 2: 190 = 5% drop, halts.
    equity = _ScriptedEquity([Decimal("200"), Decimal("190")])
    config = _make_config(
        tmp_path,
        alerter=alerter,
        drawdown=watcher,
        equity_provider=equity,
    )
    executor, _ = _executor_stub()
    run_supervisor(config, live_executor=executor, max_iterations=2)

    state = watcher.state()
    assert state is not None
    assert state.halted is True
    assert state.drawdown_pct == Decimal("0.05")
    assert sentinel.paused()
    body = sentinel.state()
    assert body is not None
    assert body["reason"].startswith("drawdown.halt:")

    halt_calls = [c for c in alerter.calls if c["event"] == "supervisor.drawdown.halt"]
    assert len(halt_calls) == 1
    assert halt_calls[0]["severity"] == "crit"
    assert halt_calls[0]["fields"]["hwm_usd"] == "200"
    assert halt_calls[0]["fields"]["equity_usd"] == "190"


def test_drawdown_halt_makes_next_iteration_paused(tmp_path: Path) -> None:
    """After a halt, the supervisor's next iteration takes the paused branch."""
    alerter = FakeAlerter()
    watcher, sentinel = _build_watcher(tmp_path, alerter=alerter)
    equity = _ScriptedEquity(
        [Decimal("200"), Decimal("180"), Decimal("170")]
    )
    config = _make_config(
        tmp_path,
        alerter=alerter,
        drawdown=watcher,
        equity_provider=equity,
    )
    executor, _ = _executor_stub()
    results = run_supervisor(config, live_executor=executor, max_iterations=3)

    # iter 1: seed (not paused). iter 2: halt (still reports paused=False
    # for that iteration because the sentinel was raised *after* the body).
    # iter 3: paused branch.
    assert results[0].paused is False
    assert results[1].paused is False
    assert results[2].paused is True

    # The paused iteration must NOT have asked the equity provider for
    # a third value (we skip drawdown.update when paused).
    assert equity.calls == 2

    halt_calls = [c for c in alerter.calls if c["event"] == "supervisor.drawdown.halt"]
    assert len(halt_calls) == 1


def test_drawdown_paused_iteration_does_not_call_equity_provider(
    tmp_path: Path,
) -> None:
    """If the sentinel is already paused (e.g. operator pause), the
    drawdown probe is skipped entirely.
    """
    alerter = FakeAlerter()
    watcher, sentinel = _build_watcher(tmp_path, alerter=alerter)
    # Pre-pause the sentinel before starting the supervisor.
    sentinel.pause(reason="operator-manual")
    equity = _ScriptedEquity([Decimal("999")])  # would force a new high if called
    config = _make_config(
        tmp_path,
        alerter=alerter,
        drawdown=watcher,
        equity_provider=equity,
    )
    executor, _ = _executor_stub()
    run_supervisor(config, live_executor=executor, max_iterations=1)

    assert equity.calls == 0
    # No state file written because update() never ran.
    assert watcher.state() is None
    # Sentinel reason still the operator's, not overwritten.
    body = sentinel.state()
    assert body is not None
    assert body["reason"] == "operator-manual"


def test_drawdown_single_alert_per_breach(tmp_path: Path) -> None:
    """Persistent underwater equity must NOT spam the alert channel."""
    alerter = FakeAlerter()
    watcher, sentinel = _build_watcher(tmp_path, alerter=alerter)
    # Iter 1: seed 200. Iter 2: 180 (halts).
    # Iter 3 would be paused already so no equity probe; that's fine.
    equity = _ScriptedEquity([Decimal("200"), Decimal("180")])
    config = _make_config(
        tmp_path,
        alerter=alerter,
        drawdown=watcher,
        equity_provider=equity,
    )
    executor, _ = _executor_stub()
    run_supervisor(config, live_executor=executor, max_iterations=5)

    halt_calls = [c for c in alerter.calls if c["event"] == "supervisor.drawdown.halt"]
    assert len(halt_calls) == 1
    assert sentinel.paused()


# ---------------------------------------------------------------------------
# robustness
# ---------------------------------------------------------------------------


def test_drawdown_equity_provider_exception_swallowed(tmp_path: Path) -> None:
    """A throwing equity provider must not crash the supervisor."""
    alerter = FakeAlerter()
    watcher, sentinel = _build_watcher(tmp_path, alerter=alerter)

    def _broken() -> Decimal:
        raise RuntimeError("ccxt rate-limited")

    config = _make_config(
        tmp_path,
        alerter=alerter,
        drawdown=watcher,
        equity_provider=_broken,
    )
    executor, _ = _executor_stub()
    results = run_supervisor(config, live_executor=executor, max_iterations=2)
    # Supervisor kept running, both iterations completed.
    assert len(results) == 2
    # No state was written (every update attempt errored).
    assert watcher.state() is None
    assert not sentinel.paused()


def test_drawdown_state_persists_across_supervisor_restarts(tmp_path: Path) -> None:
    """Two consecutive supervisor runs see the same on-disk HWM file."""
    alerter1 = FakeAlerter()
    watcher1, _sentinel1 = _build_watcher(tmp_path, alerter=alerter1)
    equity1 = _ScriptedEquity([Decimal("300")])
    config1 = _make_config(
        tmp_path,
        alerter=alerter1,
        drawdown=watcher1,
        equity_provider=equity1,
    )
    executor, _ = _executor_stub()
    run_supervisor(config1, live_executor=executor, max_iterations=1)
    assert watcher1.state().hwm_usd == Decimal("300")

    # Fresh watcher pointed at same state file (simulated restart).
    alerter2 = FakeAlerter()
    watcher2, sentinel2 = _build_watcher(tmp_path, alerter=alerter2)
    # 300 → 280 = 6.67% > 5% → should halt.
    equity2 = _ScriptedEquity([Decimal("280")])
    config2 = _make_config(
        tmp_path,
        alerter=alerter2,
        drawdown=watcher2,
        equity_provider=equity2,
    )
    run_supervisor(config2, live_executor=executor, max_iterations=1)
    state = watcher2.state()
    assert state is not None
    assert state.hwm_usd == Decimal("300")
    assert state.halted is True
    assert sentinel2.paused()
    halt_calls = [
        c for c in alerter2.calls if c["event"] == "supervisor.drawdown.halt"
    ]
    assert len(halt_calls) == 1
