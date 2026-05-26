"""Offline unit tests for `xtrade.live.mcap_softkill.McapSoftKillWatcher`.

Phase 6 Task T8 (see `docs/phase6_brief.md` §5 T8). Exercises the
debounced mcap soft-kill state machine in BOTH boundary directions:

  - `boundary="above"`: SPCXUSDT short-bias (kill when mcap rises past
    trigger). Used by the production yaml.
  - `boundary="below"`: long-bias mirror to prove the watcher is
    direction-symmetric.

All on-disk paths use `tmp_path` so tests stay hermetic.
"""

from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from xtrade.bridge.alerter import AlertDispatchResult
from xtrade.instruments.meta import InstrumentMeta
from xtrade.live.mcap_softkill import (
    McapSoftKillConfigError,
    McapSoftKillState,
    McapSoftKillUpdateResult,
    McapSoftKillWatcher,
)
from xtrade.live.sentinel import Sentinel


UTC = dt.timezone.utc

# SPCXUSDT-like meta: 11.87B shares → $3.5T trigger ≈ $294.86 mark.
SPCX_SHARES = Decimal("11870000000")
SPCX_TRIGGER_MCAP = Decimal("3500000000000")
SPCX_BREACH_MARK = Decimal("295")  # mcap ≈ 3.5017T > 3.5T → above-breach
SPCX_OK_MARK = Decimal("280")  # mcap ≈ 3.324T < 3.5T → above-clean

# Long-bias mirror: same shares, but boundary=below at $1T → $84.25 mark.
LONG_TRIGGER_MCAP = Decimal("1000000000000")
LONG_BREACH_MARK = Decimal("80")  # mcap ≈ 0.9496T < 1T → below-breach
LONG_OK_MARK = Decimal("100")  # mcap ≈ 1.187T > 1T → below-clean


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------


class FakeAlerter:
    """In-memory stand-in for AlertBridge."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.raise_on_dispatch: Exception | None = None
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
        if self.raise_on_dispatch is not None:
            raise self.raise_on_dispatch
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
            elapsed_s=0.0,
            error=None,
            response_excerpt=None,
            dispatched_at=dt.datetime.now(tz=UTC),
        )


class FakeRunner:
    """Records every emergency_close invocation."""

    def __init__(self, *, raise_exc: Exception | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.raise_exc = raise_exc

    def __call__(self, **kwargs: Any) -> None:
        self.calls.append(dict(kwargs))
        if self.raise_exc is not None:
            raise self.raise_exc


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _ts(year: int = 2026, month: int = 5, day: int = 24, hour: int = 12) -> dt.datetime:
    return dt.datetime(year, month, day, hour, 0, 0, tzinfo=UTC)


def _spcx_meta() -> InstrumentMeta:
    return InstrumentMeta(
        symbol="SPCXUSDT-PERP.BINANCE",
        shares_outstanding=SPCX_SHARES,
        min_qty=Decimal("1"),
        qty_step=Decimal("1"),
        tick_size=Decimal("0.01"),
        mark_source="oracle",
    )


def _build_above(
    tmp_path: Path,
    *,
    consecutive_iterations: int = 3,
    alerter: FakeAlerter | None = None,
    runner: FakeRunner | None = None,
) -> tuple[McapSoftKillWatcher, Sentinel, FakeAlerter, FakeRunner]:
    sentinel = Sentinel(tmp_path / "paused.flag")
    fake_alert = alerter if alerter is not None else FakeAlerter()
    fake_runner = runner if runner is not None else FakeRunner()
    watcher = McapSoftKillWatcher(
        meta=_spcx_meta(),
        trigger_mcap_usd=SPCX_TRIGGER_MCAP,
        boundary="above",
        sentinel=sentinel,
        emergency_close_runner=fake_runner,
        alerter=fake_alert,  # type: ignore[arg-type]
        consecutive_iterations=consecutive_iterations,
        state_path=tmp_path / "state" / "mcap_softkill.json",
    )
    return watcher, sentinel, fake_alert, fake_runner


def _build_below(
    tmp_path: Path,
    *,
    consecutive_iterations: int = 3,
    alerter: FakeAlerter | None = None,
    runner: FakeRunner | None = None,
) -> tuple[McapSoftKillWatcher, Sentinel, FakeAlerter, FakeRunner]:
    sentinel = Sentinel(tmp_path / "paused.flag")
    fake_alert = alerter if alerter is not None else FakeAlerter()
    fake_runner = runner if runner is not None else FakeRunner()
    watcher = McapSoftKillWatcher(
        meta=_spcx_meta(),
        trigger_mcap_usd=LONG_TRIGGER_MCAP,
        boundary="below",
        sentinel=sentinel,
        emergency_close_runner=fake_runner,
        alerter=fake_alert,  # type: ignore[arg-type]
        consecutive_iterations=consecutive_iterations,
        state_path=tmp_path / "state" / "mcap_softkill.json",
    )
    return watcher, sentinel, fake_alert, fake_runner


# ---------------------------------------------------------------------------
# constructor validation
# ---------------------------------------------------------------------------


def test_ctor_requires_meta(tmp_path: Path) -> None:
    with pytest.raises(McapSoftKillConfigError, match="meta is required"):
        McapSoftKillWatcher(
            meta=None,  # type: ignore[arg-type]
            trigger_mcap_usd=SPCX_TRIGGER_MCAP,
            boundary="above",
            sentinel=Sentinel(tmp_path / "paused.flag"),
        )


def test_ctor_requires_sentinel(tmp_path: Path) -> None:
    with pytest.raises(McapSoftKillConfigError, match="sentinel is required"):
        McapSoftKillWatcher(
            meta=_spcx_meta(),
            trigger_mcap_usd=SPCX_TRIGGER_MCAP,
            boundary="above",
            sentinel=None,  # type: ignore[arg-type]
        )


def test_ctor_rejects_invalid_boundary(tmp_path: Path) -> None:
    with pytest.raises(McapSoftKillConfigError, match="boundary must be"):
        McapSoftKillWatcher(
            meta=_spcx_meta(),
            trigger_mcap_usd=SPCX_TRIGGER_MCAP,
            boundary="sideways",  # type: ignore[arg-type]
            sentinel=Sentinel(tmp_path / "paused.flag"),
        )


def test_ctor_rejects_zero_trigger(tmp_path: Path) -> None:
    with pytest.raises(McapSoftKillConfigError, match=r"trigger_mcap_usd must be > 0"):
        McapSoftKillWatcher(
            meta=_spcx_meta(),
            trigger_mcap_usd=Decimal("0"),
            boundary="above",
            sentinel=Sentinel(tmp_path / "paused.flag"),
        )


def test_ctor_rejects_negative_trigger(tmp_path: Path) -> None:
    with pytest.raises(McapSoftKillConfigError, match=r"trigger_mcap_usd must be > 0"):
        McapSoftKillWatcher(
            meta=_spcx_meta(),
            trigger_mcap_usd=Decimal("-1"),
            boundary="above",
            sentinel=Sentinel(tmp_path / "paused.flag"),
        )


def test_ctor_rejects_non_numeric_trigger(tmp_path: Path) -> None:
    with pytest.raises(McapSoftKillConfigError, match="Decimal-coercible"):
        McapSoftKillWatcher(
            meta=_spcx_meta(),
            trigger_mcap_usd="not-a-number",
            boundary="above",
            sentinel=Sentinel(tmp_path / "paused.flag"),
        )


def test_ctor_rejects_zero_consecutive_iterations(tmp_path: Path) -> None:
    with pytest.raises(McapSoftKillConfigError, match=">= 1"):
        McapSoftKillWatcher(
            meta=_spcx_meta(),
            trigger_mcap_usd=SPCX_TRIGGER_MCAP,
            boundary="above",
            sentinel=Sentinel(tmp_path / "paused.flag"),
            consecutive_iterations=0,
        )


def test_ctor_rejects_non_int_consecutive_iterations(tmp_path: Path) -> None:
    with pytest.raises(McapSoftKillConfigError, match="must be int"):
        McapSoftKillWatcher(
            meta=_spcx_meta(),
            trigger_mcap_usd=SPCX_TRIGGER_MCAP,
            boundary="above",
            sentinel=Sentinel(tmp_path / "paused.flag"),
            consecutive_iterations=3.0,  # type: ignore[arg-type]
        )


def test_ctor_introspection(tmp_path: Path) -> None:
    watcher, _sentinel, _, _ = _build_above(tmp_path)
    assert watcher.boundary == "above"
    assert watcher.trigger_mcap_usd == SPCX_TRIGGER_MCAP
    assert watcher.consecutive_iterations == 3


# ---------------------------------------------------------------------------
# state file round-trip
# ---------------------------------------------------------------------------


def test_state_returns_none_when_file_absent(tmp_path: Path) -> None:
    watcher, *_ = _build_above(tmp_path)
    assert watcher.state() is None


def test_state_round_trip_after_update(tmp_path: Path) -> None:
    watcher, *_ = _build_above(tmp_path)
    res = watcher.update(now=_ts(), mark=SPCX_OK_MARK)
    assert isinstance(res, McapSoftKillUpdateResult)
    state = watcher.state()
    assert state is not None
    assert state == res.state
    assert state.boundary == "above"
    assert state.consecutive_iterations == 3
    assert state.triggered is False
    assert state.consecutive_breaches == 0


def test_state_corrupt_file_raises(tmp_path: Path) -> None:
    watcher, *_ = _build_above(tmp_path)
    watcher._state_path.parent.mkdir(parents=True, exist_ok=True)
    watcher._state_path.write_text("not json", encoding="utf-8")
    with pytest.raises(McapSoftKillConfigError, match="unreadable"):
        watcher.state()


def test_state_bad_boundary_field_raises(tmp_path: Path) -> None:
    watcher, *_ = _build_above(tmp_path)
    watcher._state_path.parent.mkdir(parents=True, exist_ok=True)
    body = {
        "consecutive_breaches": 0,
        "last_mcap_usd": "0",
        "triggered": False,
        "boundary": "sideways",
        "trigger_mcap_usd": "1",
        "consecutive_iterations": 3,
        "last_update_ts": _ts().isoformat(),
    }
    watcher._state_path.write_text(json.dumps(body), encoding="utf-8")
    with pytest.raises(McapSoftKillConfigError, match="invalid boundary"):
        watcher.state()


def test_dataclass_to_from_dict_round_trip() -> None:
    state = McapSoftKillState(
        consecutive_breaches=2,
        last_mcap_usd=Decimal("3490000000000"),
        triggered=False,
        boundary="above",
        trigger_mcap_usd=SPCX_TRIGGER_MCAP,
        consecutive_iterations=3,
        last_update_ts=_ts(),
    )
    assert McapSoftKillState.from_dict(state.to_dict()) == state


# ---------------------------------------------------------------------------
# above-boundary: debounce + fire
# ---------------------------------------------------------------------------


def test_above_clean_tick_keeps_zero(tmp_path: Path) -> None:
    watcher, sentinel, fake_alert, fake_runner = _build_above(tmp_path)
    res = watcher.update(now=_ts(hour=12), mark=SPCX_OK_MARK)
    assert res.fired_this_call is False
    assert res.state.consecutive_breaches == 0
    assert res.state.triggered is False
    assert not sentinel.paused()
    assert fake_alert.calls == []
    assert fake_runner.calls == []


def test_above_single_breach_no_fire(tmp_path: Path) -> None:
    watcher, sentinel, fake_alert, fake_runner = _build_above(tmp_path)
    res = watcher.update(now=_ts(hour=12), mark=SPCX_BREACH_MARK)
    assert res.state.consecutive_breaches == 1
    assert res.fired_this_call is False
    assert not sentinel.paused()
    assert fake_runner.calls == []
    assert fake_alert.calls == []


def test_above_two_breaches_no_fire(tmp_path: Path) -> None:
    watcher, sentinel, _, fake_runner = _build_above(tmp_path)
    watcher.update(now=_ts(hour=12), mark=SPCX_BREACH_MARK)
    res = watcher.update(now=_ts(hour=13), mark=SPCX_BREACH_MARK)
    assert res.state.consecutive_breaches == 2
    assert res.fired_this_call is False
    assert not sentinel.paused()
    assert fake_runner.calls == []


def test_above_three_consecutive_breaches_fires(tmp_path: Path) -> None:
    watcher, sentinel, fake_alert, fake_runner = _build_above(tmp_path)
    watcher.update(now=_ts(hour=12), mark=SPCX_BREACH_MARK)
    watcher.update(now=_ts(hour=13), mark=SPCX_BREACH_MARK)
    res = watcher.update(now=_ts(hour=14), mark=SPCX_BREACH_MARK)
    assert res.fired_this_call is True
    assert res.state.consecutive_breaches == 3
    assert res.state.triggered is True
    assert sentinel.paused()
    body = sentinel.state()
    assert body is not None
    assert body["reason"].startswith("mcap.softkill:above:")
    # Exactly one emergency_close + one crit alert.
    assert len(fake_runner.calls) == 1
    assert fake_runner.calls[0]["side"] == "reduce-only-tp-only"
    assert fake_runner.calls[0]["instrument"] == "SPCXUSDT-PERP.BINANCE"
    assert len(fake_alert.calls) == 1
    call = fake_alert.calls[0]
    assert call["severity"] == "crit"
    assert call["event"] == "supervisor.mcap.softkill"
    assert call["fields"]["boundary"] == "above"
    assert call["instrument"] == "SPCXUSDT-PERP.BINANCE"


def test_above_spike_then_recovery_resets(tmp_path: Path) -> None:
    """Two breaches then a clean tick must reset the consecutive counter."""
    watcher, sentinel, _, fake_runner = _build_above(tmp_path)
    watcher.update(now=_ts(hour=12), mark=SPCX_BREACH_MARK)
    watcher.update(now=_ts(hour=13), mark=SPCX_BREACH_MARK)
    res = watcher.update(now=_ts(hour=14), mark=SPCX_OK_MARK)
    assert res.state.consecutive_breaches == 0
    assert res.fired_this_call is False
    assert not sentinel.paused()
    assert fake_runner.calls == []

    # A subsequent single breach does not yet fire.
    res2 = watcher.update(now=_ts(hour=15), mark=SPCX_BREACH_MARK)
    assert res2.state.consecutive_breaches == 1
    assert res2.fired_this_call is False


def test_above_fires_at_exact_trigger(tmp_path: Path) -> None:
    """mcap == trigger is `>=` (breached) under boundary='above'."""
    watcher, sentinel, _, runner = _build_above(tmp_path, consecutive_iterations=1)
    # mark such that mcap == trigger exactly.
    exact_mark = SPCX_TRIGGER_MCAP / SPCX_SHARES
    res = watcher.update(now=_ts(), mark=exact_mark)
    assert res.fired_this_call is True
    assert sentinel.paused()
    assert len(runner.calls) == 1


def test_above_just_below_trigger_does_not_breach(tmp_path: Path) -> None:
    """mcap one cent below trigger must not count as a breach."""
    watcher, sentinel, _, _ = _build_above(tmp_path, consecutive_iterations=1)
    # one tick_size (0.01) below the exact mark → mcap < trigger.
    exact_mark = SPCX_TRIGGER_MCAP / SPCX_SHARES
    safe_mark = exact_mark - Decimal("0.01")
    res = watcher.update(now=_ts(), mark=safe_mark)
    assert res.fired_this_call is False
    assert res.state.consecutive_breaches == 0
    assert not sentinel.paused()


def test_above_sticky_triggered_does_not_refire(tmp_path: Path) -> None:
    """After firing, further breach observations don't refire."""
    watcher, sentinel, fake_alert, fake_runner = _build_above(
        tmp_path, consecutive_iterations=1,
    )
    watcher.update(now=_ts(hour=12), mark=SPCX_BREACH_MARK)
    assert len(fake_runner.calls) == 1
    assert len(fake_alert.calls) == 1

    # More breaches must not refire.
    for h in (13, 14, 15):
        r = watcher.update(now=_ts(hour=h), mark=SPCX_BREACH_MARK)
        assert r.fired_this_call is False
        assert r.state.triggered is True
    assert len(fake_runner.calls) == 1
    assert len(fake_alert.calls) == 1


def test_above_sticky_triggered_even_when_mcap_recovers(tmp_path: Path) -> None:
    """No auto-recovery: triggered stays True even when mark drops back."""
    watcher, sentinel, _, _ = _build_above(tmp_path, consecutive_iterations=1)
    watcher.update(now=_ts(hour=12), mark=SPCX_BREACH_MARK)

    res = watcher.update(now=_ts(hour=13), mark=SPCX_OK_MARK)
    assert res.fired_this_call is False
    assert res.state.triggered is True  # sticky
    assert sentinel.paused()  # sentinel also sticky (brief §5 T8)


# ---------------------------------------------------------------------------
# below-boundary mirror
# ---------------------------------------------------------------------------


def test_below_clean_tick_keeps_zero(tmp_path: Path) -> None:
    watcher, sentinel, _, _ = _build_below(tmp_path)
    res = watcher.update(now=_ts(), mark=LONG_OK_MARK)
    assert res.fired_this_call is False
    assert res.state.consecutive_breaches == 0
    assert not sentinel.paused()


def test_below_three_consecutive_breaches_fires(tmp_path: Path) -> None:
    watcher, sentinel, fake_alert, fake_runner = _build_below(tmp_path)
    watcher.update(now=_ts(hour=12), mark=LONG_BREACH_MARK)
    watcher.update(now=_ts(hour=13), mark=LONG_BREACH_MARK)
    res = watcher.update(now=_ts(hour=14), mark=LONG_BREACH_MARK)
    assert res.fired_this_call is True
    assert res.state.consecutive_breaches == 3
    assert sentinel.paused()
    body = sentinel.state()
    assert body is not None
    assert body["reason"].startswith("mcap.softkill:below:")
    assert len(fake_runner.calls) == 1
    assert fake_runner.calls[0]["side"] == "reduce-only-tp-only"
    assert fake_alert.calls[0]["fields"]["boundary"] == "below"


def test_below_spike_then_recovery_resets(tmp_path: Path) -> None:
    watcher, sentinel, _, fake_runner = _build_below(tmp_path)
    watcher.update(now=_ts(hour=12), mark=LONG_BREACH_MARK)
    watcher.update(now=_ts(hour=13), mark=LONG_BREACH_MARK)
    res = watcher.update(now=_ts(hour=14), mark=LONG_OK_MARK)
    assert res.state.consecutive_breaches == 0
    assert res.fired_this_call is False
    assert not sentinel.paused()
    assert fake_runner.calls == []


def test_below_fires_at_exact_trigger(tmp_path: Path) -> None:
    """mcap == trigger is `<=` (breached) under boundary='below'."""
    watcher, sentinel, _, runner = _build_below(tmp_path, consecutive_iterations=1)
    exact_mark = LONG_TRIGGER_MCAP / SPCX_SHARES
    res = watcher.update(now=_ts(), mark=exact_mark)
    assert res.fired_this_call is True
    assert sentinel.paused()
    assert len(runner.calls) == 1


def test_below_just_above_trigger_does_not_breach(tmp_path: Path) -> None:
    watcher, sentinel, _, _ = _build_below(tmp_path, consecutive_iterations=1)
    exact_mark = LONG_TRIGGER_MCAP / SPCX_SHARES
    safe_mark = exact_mark + Decimal("0.01")
    res = watcher.update(now=_ts(), mark=safe_mark)
    assert res.fired_this_call is False
    assert res.state.consecutive_breaches == 0
    assert not sentinel.paused()


# ---------------------------------------------------------------------------
# persistence across watcher instances
# ---------------------------------------------------------------------------


def test_persistence_carries_counter_across_instances(tmp_path: Path) -> None:
    """A fresh watcher pointed at the same state file resumes the
    consecutive counter (so a supervisor restart can't lose the
    debounce progress).
    """
    sentinel = Sentinel(tmp_path / "paused.flag")
    runner = FakeRunner()
    state_path = tmp_path / "state" / "mcap_softkill.json"

    w1 = McapSoftKillWatcher(
        meta=_spcx_meta(),
        trigger_mcap_usd=SPCX_TRIGGER_MCAP,
        boundary="above",
        sentinel=sentinel,
        emergency_close_runner=runner,
        alerter=None,
        consecutive_iterations=3,
        state_path=state_path,
    )
    w1.update(now=_ts(hour=12), mark=SPCX_BREACH_MARK)
    w1.update(now=_ts(hour=13), mark=SPCX_BREACH_MARK)

    # Simulated supervisor restart: new watcher, same state file.
    w2 = McapSoftKillWatcher(
        meta=_spcx_meta(),
        trigger_mcap_usd=SPCX_TRIGGER_MCAP,
        boundary="above",
        sentinel=sentinel,
        emergency_close_runner=runner,
        alerter=None,
        consecutive_iterations=3,
        state_path=state_path,
    )
    res = w2.update(now=_ts(hour=14), mark=SPCX_BREACH_MARK)
    assert res.fired_this_call is True
    assert res.state.consecutive_breaches == 3
    assert sentinel.paused()
    assert len(runner.calls) == 1


def test_persistence_triggered_state_survives_restart(tmp_path: Path) -> None:
    """`triggered=True` on disk must remain sticky across instances."""
    sentinel = Sentinel(tmp_path / "paused.flag")
    state_path = tmp_path / "state" / "mcap_softkill.json"

    runner1 = FakeRunner()
    w1 = McapSoftKillWatcher(
        meta=_spcx_meta(),
        trigger_mcap_usd=SPCX_TRIGGER_MCAP,
        boundary="above",
        sentinel=sentinel,
        emergency_close_runner=runner1,
        consecutive_iterations=1,
        state_path=state_path,
    )
    w1.update(now=_ts(hour=12), mark=SPCX_BREACH_MARK)
    assert len(runner1.calls) == 1

    # Restart. Even though the new watcher's runner is fresh, no refire.
    runner2 = FakeRunner()
    alerter2 = FakeAlerter()
    w2 = McapSoftKillWatcher(
        meta=_spcx_meta(),
        trigger_mcap_usd=SPCX_TRIGGER_MCAP,
        boundary="above",
        sentinel=sentinel,
        emergency_close_runner=runner2,
        alerter=alerter2,  # type: ignore[arg-type]
        consecutive_iterations=1,
        state_path=state_path,
    )
    res = w2.update(now=_ts(hour=13), mark=SPCX_BREACH_MARK)
    assert res.fired_this_call is False
    assert res.state.triggered is True
    assert runner2.calls == []
    assert alerter2.calls == []


# ---------------------------------------------------------------------------
# robustness: optional deps + crashes
# ---------------------------------------------------------------------------


def test_runner_optional_still_fires_sentinel_and_alert(tmp_path: Path) -> None:
    """If no emergency_close runner is wired, watcher still pauses + alerts."""
    sentinel = Sentinel(tmp_path / "paused.flag")
    fake_alert = FakeAlerter()
    watcher = McapSoftKillWatcher(
        meta=_spcx_meta(),
        trigger_mcap_usd=SPCX_TRIGGER_MCAP,
        boundary="above",
        sentinel=sentinel,
        emergency_close_runner=None,
        alerter=fake_alert,  # type: ignore[arg-type]
        consecutive_iterations=1,
        state_path=tmp_path / "state" / "mcap_softkill.json",
    )
    res = watcher.update(now=_ts(), mark=SPCX_BREACH_MARK)
    assert res.fired_this_call is True
    assert sentinel.paused()
    assert len(fake_alert.calls) == 1


def test_alerter_optional_still_fires_sentinel_and_runner(tmp_path: Path) -> None:
    sentinel = Sentinel(tmp_path / "paused.flag")
    runner = FakeRunner()
    watcher = McapSoftKillWatcher(
        meta=_spcx_meta(),
        trigger_mcap_usd=SPCX_TRIGGER_MCAP,
        boundary="above",
        sentinel=sentinel,
        emergency_close_runner=runner,
        alerter=None,
        consecutive_iterations=1,
        state_path=tmp_path / "state" / "mcap_softkill.json",
    )
    res = watcher.update(now=_ts(), mark=SPCX_BREACH_MARK)
    assert res.fired_this_call is True
    assert sentinel.paused()
    assert len(runner.calls) == 1


def test_runner_crash_does_not_block_alert(tmp_path: Path) -> None:
    """A throwing emergency_close runner must still let the alert fire."""
    sentinel = Sentinel(tmp_path / "paused.flag")
    fake_alert = FakeAlerter()
    runner = FakeRunner(raise_exc=RuntimeError("binance 401"))
    watcher = McapSoftKillWatcher(
        meta=_spcx_meta(),
        trigger_mcap_usd=SPCX_TRIGGER_MCAP,
        boundary="above",
        sentinel=sentinel,
        emergency_close_runner=runner,
        alerter=fake_alert,  # type: ignore[arg-type]
        consecutive_iterations=1,
        state_path=tmp_path / "state" / "mcap_softkill.json",
    )
    res = watcher.update(now=_ts(), mark=SPCX_BREACH_MARK)
    assert res.fired_this_call is True
    assert sentinel.paused()
    assert len(runner.calls) == 1  # runner was called, then raised
    assert len(fake_alert.calls) == 1  # alert still went out


def test_alerter_crash_swallowed(tmp_path: Path) -> None:
    """A throwing alerter must not prevent the state file from being written."""
    fake_alert = FakeAlerter()
    fake_alert.raise_on_dispatch = RuntimeError("yuanbao down")
    runner = FakeRunner()
    watcher, sentinel, _, _ = _build_above(
        tmp_path, consecutive_iterations=1, alerter=fake_alert, runner=runner,
    )
    res = watcher.update(now=_ts(), mark=SPCX_BREACH_MARK)
    assert res.fired_this_call is True
    assert sentinel.paused()
    assert len(runner.calls) == 1
    assert watcher.state() is not None


# ---------------------------------------------------------------------------
# input validation
# ---------------------------------------------------------------------------


def test_update_rejects_naive_datetime(tmp_path: Path) -> None:
    watcher, *_ = _build_above(tmp_path)
    naive = dt.datetime(2026, 5, 24, 12, 0, 0)
    with pytest.raises(ValueError, match="timezone-aware"):
        watcher.update(now=naive, mark=SPCX_OK_MARK)


def test_update_rejects_non_positive_mark(tmp_path: Path) -> None:
    watcher, *_ = _build_above(tmp_path)
    with pytest.raises(ValueError, match=r"mark must be > 0"):
        watcher.update(now=_ts(), mark=Decimal("0"))


def test_update_rejects_non_numeric_mark(tmp_path: Path) -> None:
    watcher, *_ = _build_above(tmp_path)
    with pytest.raises(ValueError, match="Decimal-coercible"):
        watcher.update(now=_ts(), mark="bad")


def test_update_accepts_float_and_str_mark(tmp_path: Path) -> None:
    watcher, *_ = _build_above(tmp_path)
    r1 = watcher.update(now=_ts(hour=12), mark=280.0)
    assert r1.state.last_mcap_usd > 0
    r2 = watcher.update(now=_ts(hour=13), mark="281")
    assert r2.state.last_mcap_usd > 0


# ---------------------------------------------------------------------------
# atomic write
# ---------------------------------------------------------------------------


def test_update_writes_atomic_no_tmp_debris(tmp_path: Path) -> None:
    watcher, *_ = _build_above(tmp_path)
    watcher.update(now=_ts(), mark=SPCX_OK_MARK)
    parent = watcher._state_path.parent
    files = list(parent.iterdir())
    assert len(files) == 1
    assert files[0].name == "mcap_softkill.json"


def test_update_creates_parent_directory(tmp_path: Path) -> None:
    nested = tmp_path / "a" / "b" / "c"
    sentinel = Sentinel(tmp_path / "paused.flag")
    watcher = McapSoftKillWatcher(
        meta=_spcx_meta(),
        trigger_mcap_usd=SPCX_TRIGGER_MCAP,
        boundary="above",
        sentinel=sentinel,
        emergency_close_runner=None,
        state_path=nested / "mcap_softkill.json",
    )
    watcher.update(now=_ts(), mark=SPCX_OK_MARK)
    assert (nested / "mcap_softkill.json").exists()
