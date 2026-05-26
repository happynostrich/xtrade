"""Tests for `xtrade.live.heartbeat.HeartbeatWatcher` (Phase 6 Task T9)."""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest

from xtrade.bridge.alerter import AlertBridge, AlertDispatchResult
from xtrade.live.heartbeat import HeartbeatConfigError, HeartbeatWatcher


UTC = dt.timezone.utc


# ---- fakes ----------------------------------------------------------------


class FakeAlerter:
    """Captures dispatch_alert calls without touching the network."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
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


def _watcher(
    *,
    idle_warn_s: float = 60.0,
    idle_crit_s: float = 180.0,
    start_ts: dt.datetime | None = None,
    instrument: str | None = "SPCXUSDT-PERP.BINANCE",
) -> tuple[HeartbeatWatcher, FakeAlerter]:
    alerter = FakeAlerter()
    base = start_ts or dt.datetime(2026, 5, 24, 12, 0, 0, tzinfo=UTC)
    w = HeartbeatWatcher(
        alerter=alerter,  # type: ignore[arg-type]
        idle_warn_s=idle_warn_s,
        idle_crit_s=idle_crit_s,
        instrument=instrument,
        start_ts=base,
        clock=lambda: base,
    )
    return w, alerter


# ---- ctor validation ------------------------------------------------------


def test_ctor_rejects_non_monotonic_thresholds() -> None:
    alerter = FakeAlerter()
    with pytest.raises(HeartbeatConfigError, match="idle_crit_s"):
        HeartbeatWatcher(alerter=alerter, idle_warn_s=600, idle_crit_s=600)  # type: ignore[arg-type]


def test_ctor_rejects_zero_warn() -> None:
    alerter = FakeAlerter()
    with pytest.raises(HeartbeatConfigError, match="idle_warn_s"):
        HeartbeatWatcher(alerter=alerter, idle_warn_s=0, idle_crit_s=10)  # type: ignore[arg-type]


def test_ctor_rejects_negative_warn() -> None:
    alerter = FakeAlerter()
    with pytest.raises(HeartbeatConfigError):
        HeartbeatWatcher(alerter=alerter, idle_warn_s=-5, idle_crit_s=10)  # type: ignore[arg-type]


def test_ctor_rejects_none_alerter() -> None:
    with pytest.raises(HeartbeatConfigError, match="alerter"):
        HeartbeatWatcher(alerter=None, idle_warn_s=1, idle_crit_s=2)  # type: ignore[arg-type]


def test_ctor_rejects_naive_start_ts() -> None:
    alerter = FakeAlerter()
    with pytest.raises(HeartbeatConfigError, match="timezone"):
        HeartbeatWatcher(
            alerter=alerter,  # type: ignore[arg-type]
            idle_warn_s=1,
            idle_crit_s=2,
            start_ts=dt.datetime(2026, 5, 24, 12, 0),
        )


def test_ctor_starts_at_info_level() -> None:
    w, _ = _watcher()
    assert w.current_level == "info"


# ---- tick: no transition --------------------------------------------------


def test_tick_within_warn_threshold_no_alert() -> None:
    base = dt.datetime(2026, 5, 24, 12, 0, 0, tzinfo=UTC)
    w, alerter = _watcher(start_ts=base)
    result = w.tick(now=base + dt.timedelta(seconds=30))
    assert result.transitioned is False
    assert result.current_level == "info"
    assert alerter.calls == []


# ---- info → warn transition -----------------------------------------------


def test_tick_info_to_warn_pushes_one_warn() -> None:
    base = dt.datetime(2026, 5, 24, 12, 0, 0, tzinfo=UTC)
    w, alerter = _watcher(start_ts=base)
    result = w.tick(now=base + dt.timedelta(seconds=60))
    assert result.transitioned is True
    assert result.current_level == "warn"
    assert result.previous_level == "info"
    assert len(alerter.calls) == 1
    call = alerter.calls[0]
    assert call["severity"] == "warn"
    assert call["event"] == "supervisor.heartbeat.idle"
    assert call["instrument"] == "SPCXUSDT-PERP.BINANCE"
    assert call["fields"]["previous_level"] == "info"
    assert call["fields"]["next_level"] == "warn"


def test_tick_warn_does_not_repush_while_still_in_warn() -> None:
    """'Same level no repeat' — only one warn alert per ladder transition."""
    base = dt.datetime(2026, 5, 24, 12, 0, 0, tzinfo=UTC)
    w, alerter = _watcher(start_ts=base)
    w.tick(now=base + dt.timedelta(seconds=60))     # info → warn
    w.tick(now=base + dt.timedelta(seconds=90))     # still warn
    w.tick(now=base + dt.timedelta(seconds=120))    # still warn
    # exactly one alert, from the info→warn transition
    assert len(alerter.calls) == 1


# ---- warn → crit transition -----------------------------------------------


def test_tick_warn_to_crit_pushes_one_crit() -> None:
    base = dt.datetime(2026, 5, 24, 12, 0, 0, tzinfo=UTC)
    w, alerter = _watcher(start_ts=base)
    w.tick(now=base + dt.timedelta(seconds=60))     # info → warn
    w.tick(now=base + dt.timedelta(seconds=200))    # warn → crit
    assert len(alerter.calls) == 2
    crit = alerter.calls[1]
    assert crit["severity"] == "crit"
    assert crit["event"] == "supervisor.heartbeat.idle"
    assert crit["fields"]["previous_level"] == "warn"
    assert crit["fields"]["next_level"] == "crit"


def test_tick_info_to_crit_directly_when_huge_gap() -> None:
    """If the supervisor was wedged across multiple ticks (e.g. systemd
    just woke us), one tick can leap straight from info to crit."""
    base = dt.datetime(2026, 5, 24, 12, 0, 0, tzinfo=UTC)
    w, alerter = _watcher(start_ts=base)
    w.tick(now=base + dt.timedelta(seconds=400))
    assert len(alerter.calls) == 1
    call = alerter.calls[0]
    assert call["severity"] == "crit"
    assert call["fields"]["previous_level"] == "info"


def test_tick_crit_no_repush_until_recovery() -> None:
    base = dt.datetime(2026, 5, 24, 12, 0, 0, tzinfo=UTC)
    w, alerter = _watcher(start_ts=base)
    w.tick(now=base + dt.timedelta(seconds=200))    # info → crit
    w.tick(now=base + dt.timedelta(seconds=300))    # still crit
    w.tick(now=base + dt.timedelta(seconds=600))    # still crit
    assert len(alerter.calls) == 1


# ---- recovery: warn → info ------------------------------------------------


def test_recovery_from_warn_pushes_info_recovered() -> None:
    base = dt.datetime(2026, 5, 24, 12, 0, 0, tzinfo=UTC)
    w, alerter = _watcher(start_ts=base)
    w.tick(now=base + dt.timedelta(seconds=60))     # info → warn
    w.record_activity(ts=base + dt.timedelta(seconds=70))
    w.tick(now=base + dt.timedelta(seconds=80))     # warn → info (recovered)
    assert len(alerter.calls) == 2
    rec = alerter.calls[1]
    assert rec["severity"] == "info"
    assert rec["event"] == "supervisor.heartbeat.recovered"
    assert rec["fields"]["previous_level"] == "warn"
    assert rec["fields"]["next_level"] == "info"


def test_recovery_from_crit_pushes_info_recovered() -> None:
    base = dt.datetime(2026, 5, 24, 12, 0, 0, tzinfo=UTC)
    w, alerter = _watcher(start_ts=base)
    w.tick(now=base + dt.timedelta(seconds=200))    # info → crit
    w.record_activity(ts=base + dt.timedelta(seconds=210))
    w.tick(now=base + dt.timedelta(seconds=220))    # crit → info (recovered)
    assert len(alerter.calls) == 2
    rec = alerter.calls[1]
    assert rec["severity"] == "info"
    assert rec["event"] == "supervisor.heartbeat.recovered"
    assert rec["fields"]["previous_level"] == "crit"


# ---- record_activity guards ----------------------------------------------


def test_record_activity_rejects_naive_ts() -> None:
    w, _ = _watcher()
    with pytest.raises(ValueError, match="timezone-aware"):
        w.record_activity(ts=dt.datetime(2026, 5, 24, 12, 0))


def test_record_activity_updates_last_activity() -> None:
    base = dt.datetime(2026, 5, 24, 12, 0, 0, tzinfo=UTC)
    w, _ = _watcher(start_ts=base)
    activity = base + dt.timedelta(seconds=50)
    w.record_activity(ts=activity)
    assert w.last_activity_ts == activity


# ---- tick guards ----------------------------------------------------------


def test_tick_rejects_naive_now() -> None:
    w, _ = _watcher()
    with pytest.raises(ValueError, match="timezone-aware"):
        w.tick(now=dt.datetime(2026, 5, 24, 12, 0))


def test_tick_clamps_negative_skew_to_zero() -> None:
    """Backward-moving clock must not escalate (defensive guard)."""
    base = dt.datetime(2026, 5, 24, 12, 0, 0, tzinfo=UTC)
    w, alerter = _watcher(start_ts=base)
    result = w.tick(now=base - dt.timedelta(seconds=30))
    assert result.elapsed_s == 0.0
    assert result.current_level == "info"
    assert alerter.calls == []


# ---- full ladder lifecycle -----------------------------------------------


def test_full_ladder_info_warn_crit_recover_info() -> None:
    """End-to-end: info → warn → crit → recover → info, four pushes total."""
    base = dt.datetime(2026, 5, 24, 12, 0, 0, tzinfo=UTC)
    w, alerter = _watcher(start_ts=base)

    w.tick(now=base + dt.timedelta(seconds=60))     # info → warn
    w.tick(now=base + dt.timedelta(seconds=200))    # warn → crit
    # supervisor resumes work
    w.record_activity(ts=base + dt.timedelta(seconds=210))
    w.tick(now=base + dt.timedelta(seconds=220))    # crit → info (recovered)
    # subsequent quiet stretch escalates again
    w.tick(now=base + dt.timedelta(seconds=290))    # info → warn (again)

    severities = [c["severity"] for c in alerter.calls]
    events = [c["event"] for c in alerter.calls]
    assert severities == ["warn", "crit", "info", "warn"]
    assert events == [
        "supervisor.heartbeat.idle",
        "supervisor.heartbeat.idle",
        "supervisor.heartbeat.recovered",
        "supervisor.heartbeat.idle",
    ]


# ---- alert envelope fields ------------------------------------------------


def test_idle_alert_carries_threshold_metadata() -> None:
    base = dt.datetime(2026, 5, 24, 12, 0, 0, tzinfo=UTC)
    w, alerter = _watcher(start_ts=base, idle_warn_s=60, idle_crit_s=180)
    w.tick(now=base + dt.timedelta(seconds=90))
    call = alerter.calls[0]
    assert call["fields"]["idle_warn_s"] == 60.0
    assert call["fields"]["idle_crit_s"] == 180.0
    assert call["fields"]["elapsed_s"] == 90.0
    assert call["fields"]["last_activity"].startswith("2026-05-24T12:00:00")


def test_alert_omits_instrument_when_none() -> None:
    base = dt.datetime(2026, 5, 24, 12, 0, 0, tzinfo=UTC)
    alerter = FakeAlerter()
    w = HeartbeatWatcher(
        alerter=alerter,  # type: ignore[arg-type]
        idle_warn_s=60,
        idle_crit_s=180,
        instrument=None,
        start_ts=base,
        clock=lambda: base,
    )
    w.tick(now=base + dt.timedelta(seconds=90))
    assert alerter.calls[0]["instrument"] is None
