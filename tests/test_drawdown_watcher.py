"""Offline unit tests for `xtrade.live.drawdown.DrawdownWatcher`.

Phase 6 Task T7 (see `docs/phase6_brief.md` §5 T7). Exercises the
high-water-mark tracker, halt-on-breach semantics, atomic state file
round-trip, and operator `reset_hwm` override using a real on-disk
`Sentinel` plus a `FakeAlerter` stand-in.

Note: the watcher writes via `tempfile.mkstemp` + `os.replace` so each
test gets its own `tmp_path` directory.
"""

from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from xtrade.bridge.alerter import AlertDispatchResult
from xtrade.live.drawdown import (
    DrawdownConfigError,
    DrawdownState,
    DrawdownUpdateResult,
    DrawdownWatcher,
)
from xtrade.live.sentinel import Sentinel


UTC = dt.timezone.utc
INSTRUMENT = "SPCXUSDT-PERP.BINANCE"


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------


class FakeAlerter:
    """In-memory stand-in for AlertBridge. Records dispatches."""

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


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _ts(year: int = 2026, month: int = 5, day: int = 24, hour: int = 12) -> dt.datetime:
    return dt.datetime(year, month, day, hour, 0, 0, tzinfo=UTC)


def _build_watcher(
    tmp_path: Path,
    *,
    halt_pct: Decimal | float | str = Decimal("0.05"),
    alerter: FakeAlerter | None = None,
    instrument: str | None = INSTRUMENT,
) -> tuple[DrawdownWatcher, Sentinel, FakeAlerter]:
    sentinel = Sentinel(tmp_path / "paused.flag")
    fake = alerter if alerter is not None else FakeAlerter()
    watcher = DrawdownWatcher(
        hwm_path=tmp_path / "state" / "drawdown.json",
        sentinel=sentinel,
        halt_pct=halt_pct,
        alerter=fake,  # type: ignore[arg-type]
        instrument=instrument,
    )
    return watcher, sentinel, fake


# ---------------------------------------------------------------------------
# constructor validation
# ---------------------------------------------------------------------------


def test_ctor_requires_sentinel(tmp_path: Path) -> None:
    with pytest.raises(DrawdownConfigError, match="sentinel is required"):
        DrawdownWatcher(
            hwm_path=tmp_path / "drawdown.json",
            sentinel=None,  # type: ignore[arg-type]
        )


def test_ctor_rejects_zero_halt_pct(tmp_path: Path) -> None:
    sentinel = Sentinel(tmp_path / "paused.flag")
    with pytest.raises(DrawdownConfigError, match="halt_pct must be > 0"):
        DrawdownWatcher(
            hwm_path=tmp_path / "drawdown.json",
            sentinel=sentinel,
            halt_pct=Decimal("0"),
        )


def test_ctor_rejects_negative_halt_pct(tmp_path: Path) -> None:
    sentinel = Sentinel(tmp_path / "paused.flag")
    with pytest.raises(DrawdownConfigError, match="halt_pct must be > 0"):
        DrawdownWatcher(
            hwm_path=tmp_path / "drawdown.json",
            sentinel=sentinel,
            halt_pct=Decimal("-0.05"),
        )


def test_ctor_rejects_halt_pct_at_one(tmp_path: Path) -> None:
    sentinel = Sentinel(tmp_path / "paused.flag")
    with pytest.raises(DrawdownConfigError, match=r"halt_pct must be < 1"):
        DrawdownWatcher(
            hwm_path=tmp_path / "drawdown.json",
            sentinel=sentinel,
            halt_pct=Decimal("1.0"),
        )


def test_ctor_rejects_halt_pct_above_one(tmp_path: Path) -> None:
    sentinel = Sentinel(tmp_path / "paused.flag")
    with pytest.raises(DrawdownConfigError, match=r"halt_pct must be < 1"):
        DrawdownWatcher(
            hwm_path=tmp_path / "drawdown.json",
            sentinel=sentinel,
            halt_pct=Decimal("1.5"),
        )


def test_ctor_rejects_non_numeric_halt_pct(tmp_path: Path) -> None:
    sentinel = Sentinel(tmp_path / "paused.flag")
    with pytest.raises(DrawdownConfigError, match="Decimal-coercible"):
        DrawdownWatcher(
            hwm_path=tmp_path / "drawdown.json",
            sentinel=sentinel,
            halt_pct="not-a-number",
        )


def test_ctor_accepts_float_and_str_halt_pct(tmp_path: Path) -> None:
    sentinel = Sentinel(tmp_path / "paused.flag")
    w1 = DrawdownWatcher(
        hwm_path=tmp_path / "a.json",
        sentinel=sentinel,
        halt_pct=0.07,
    )
    w2 = DrawdownWatcher(
        hwm_path=tmp_path / "b.json",
        sentinel=sentinel,
        halt_pct="0.10",
    )
    assert w1.halt_pct == Decimal("0.07")
    assert w2.halt_pct == Decimal("0.10")


def test_ctor_alerter_optional(tmp_path: Path) -> None:
    sentinel = Sentinel(tmp_path / "paused.flag")
    w = DrawdownWatcher(
        hwm_path=tmp_path / "drawdown.json",
        sentinel=sentinel,
        alerter=None,
    )
    assert w.halt_pct == Decimal("0.05")


# ---------------------------------------------------------------------------
# state() round-trip
# ---------------------------------------------------------------------------


def test_state_returns_none_when_file_absent(tmp_path: Path) -> None:
    watcher, _sentinel, _fake = _build_watcher(tmp_path)
    assert watcher.state() is None


def test_state_returns_dataclass_after_first_update(tmp_path: Path) -> None:
    watcher, _sentinel, _fake = _build_watcher(tmp_path)
    res = watcher.update(now=_ts(), equity_usd=Decimal("200"))
    state = watcher.state()
    assert state is not None
    assert state == res.state
    assert state.hwm_usd == Decimal("200")
    assert state.last_equity_usd == Decimal("200")
    assert state.halted is False
    assert state.halt_pct == Decimal("0.05")


def test_state_corrupt_file_raises(tmp_path: Path) -> None:
    watcher, _sentinel, _fake = _build_watcher(tmp_path)
    watcher._hwm_path.parent.mkdir(parents=True, exist_ok=True)
    watcher._hwm_path.write_text("this is not json", encoding="utf-8")
    with pytest.raises(DrawdownConfigError, match="unreadable"):
        watcher.state()


def test_state_missing_field_raises(tmp_path: Path) -> None:
    watcher, _sentinel, _fake = _build_watcher(tmp_path)
    watcher._hwm_path.parent.mkdir(parents=True, exist_ok=True)
    watcher._hwm_path.write_text(
        json.dumps({"last_equity_usd": "100"}),
        encoding="utf-8",
    )
    with pytest.raises(DrawdownConfigError, match="corrupt or incomplete"):
        watcher.state()


def test_drawdown_state_to_from_dict_round_trip() -> None:
    state = DrawdownState(
        hwm_usd=Decimal("200.50"),
        last_equity_usd=Decimal("180.25"),
        last_update_ts=_ts(),
        halted=True,
        drawdown_pct=Decimal("0.101"),
        halt_pct=Decimal("0.05"),
    )
    round_trip = DrawdownState.from_dict(state.to_dict())
    assert round_trip == state


# ---------------------------------------------------------------------------
# update() — seeding, new highs, drawdown calculation
# ---------------------------------------------------------------------------


def test_update_first_call_seeds_hwm(tmp_path: Path) -> None:
    watcher, sentinel, fake = _build_watcher(tmp_path)
    res = watcher.update(now=_ts(), equity_usd=Decimal("150"))
    assert isinstance(res, DrawdownUpdateResult)
    assert res.halt_triggered is False
    assert res.state.hwm_usd == Decimal("150")
    assert res.state.last_equity_usd == Decimal("150")
    assert res.state.drawdown_pct == Decimal("0")
    assert res.state.halted is False
    assert not sentinel.paused()
    assert fake.calls == []


def test_update_new_high_advances_hwm(tmp_path: Path) -> None:
    watcher, _sentinel, _fake = _build_watcher(tmp_path)
    watcher.update(now=_ts(hour=12), equity_usd=Decimal("100"))
    res = watcher.update(now=_ts(hour=13), equity_usd=Decimal("125"))
    assert res.state.hwm_usd == Decimal("125")
    assert res.state.last_equity_usd == Decimal("125")
    assert res.state.drawdown_pct == Decimal("0")
    assert res.halt_triggered is False


def test_update_small_drawdown_does_not_halt(tmp_path: Path) -> None:
    watcher, sentinel, fake = _build_watcher(tmp_path)
    watcher.update(now=_ts(hour=12), equity_usd=Decimal("200"))
    # 200 → 195 = 2.5% drawdown, well below 5% halt
    res = watcher.update(now=_ts(hour=13), equity_usd=Decimal("195"))
    assert res.halt_triggered is False
    assert res.state.hwm_usd == Decimal("200")
    assert res.state.last_equity_usd == Decimal("195")
    assert res.state.drawdown_pct == Decimal("0.025")
    assert res.state.halted is False
    assert not sentinel.paused()
    assert fake.calls == []


def test_update_drawdown_pct_calculation(tmp_path: Path) -> None:
    watcher, _sentinel, _fake = _build_watcher(tmp_path, halt_pct=Decimal("0.50"))
    watcher.update(now=_ts(hour=12), equity_usd=Decimal("100"))
    res = watcher.update(now=_ts(hour=13), equity_usd=Decimal("85"))
    # (100 - 85) / 100 = 0.15
    assert res.state.drawdown_pct == Decimal("0.15")
    assert res.halt_triggered is False


# ---------------------------------------------------------------------------
# update() — halt behaviour
# ---------------------------------------------------------------------------


def test_update_triggers_halt_at_threshold(tmp_path: Path) -> None:
    watcher, sentinel, fake = _build_watcher(tmp_path)
    watcher.update(now=_ts(hour=12), equity_usd=Decimal("200"))
    # 200 → 190 = exactly 5% drawdown (>= halt_pct=0.05)
    res = watcher.update(now=_ts(hour=13), equity_usd=Decimal("190"))
    assert res.halt_triggered is True
    assert res.state.halted is True
    assert res.state.drawdown_pct == Decimal("0.05")
    assert sentinel.paused()
    body = sentinel.state()
    assert body is not None
    assert "drawdown.halt:0.0500" in body["reason"]
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["severity"] == "crit"
    assert call["event"] == "supervisor.drawdown.halt"
    assert call["instrument"] == INSTRUMENT
    assert call["fields"]["hwm_usd"] == "200"
    assert call["fields"]["equity_usd"] == "190"


def test_update_triggers_halt_above_threshold(tmp_path: Path) -> None:
    watcher, sentinel, fake = _build_watcher(tmp_path)
    watcher.update(now=_ts(hour=12), equity_usd=Decimal("200"))
    res = watcher.update(now=_ts(hour=13), equity_usd=Decimal("180"))
    assert res.halt_triggered is True
    assert res.state.drawdown_pct == Decimal("0.10")
    assert sentinel.paused()
    assert len(fake.calls) == 1


def test_update_second_breach_while_halted_does_not_realert(tmp_path: Path) -> None:
    """Once halted, further underwater observations must NOT push another alert."""
    watcher, sentinel, fake = _build_watcher(tmp_path)
    watcher.update(now=_ts(hour=12), equity_usd=Decimal("200"))
    r1 = watcher.update(now=_ts(hour=13), equity_usd=Decimal("180"))
    assert r1.halt_triggered is True
    assert len(fake.calls) == 1

    r2 = watcher.update(now=_ts(hour=14), equity_usd=Decimal("170"))
    assert r2.halt_triggered is False  # already halted — no re-fire
    assert r2.state.halted is True
    assert r2.state.drawdown_pct == Decimal("0.15")
    assert sentinel.paused()
    # No additional alert beyond the first breach.
    assert len(fake.calls) == 1


def test_update_new_high_rearms_halt_but_does_not_clear_sentinel(tmp_path: Path) -> None:
    """After a halt, a new HWM rearms the breach detector (in-memory),
    but the sentinel stays paused — operator must run `xtrade ops resume`.
    """
    watcher, sentinel, fake = _build_watcher(tmp_path)
    watcher.update(now=_ts(hour=12), equity_usd=Decimal("200"))
    watcher.update(now=_ts(hour=13), equity_usd=Decimal("180"))
    assert sentinel.paused()
    assert len(fake.calls) == 1

    # New high — watcher should rearm halted=False but NOT clear sentinel.
    r = watcher.update(now=_ts(hour=14), equity_usd=Decimal("210"))
    assert r.halt_triggered is False
    assert r.state.halted is False
    assert r.state.hwm_usd == Decimal("210")
    assert r.state.drawdown_pct == Decimal("0")
    assert sentinel.paused(), "sentinel must persist until operator clears it"
    assert len(fake.calls) == 1


def test_update_rearmed_watcher_can_halt_again(tmp_path: Path) -> None:
    """Full lifecycle: halt → new high → second halt fires a fresh alert."""
    watcher, sentinel, fake = _build_watcher(tmp_path)
    watcher.update(now=_ts(hour=12), equity_usd=Decimal("200"))
    watcher.update(now=_ts(hour=13), equity_usd=Decimal("180"))
    assert len(fake.calls) == 1

    # New high rearms.
    watcher.update(now=_ts(hour=14), equity_usd=Decimal("250"))
    # Operator clears sentinel.
    sentinel.resume()
    assert not sentinel.paused()

    # Second drawdown 250 → 230 = 8% > 5%.
    r = watcher.update(now=_ts(hour=15), equity_usd=Decimal("230"))
    assert r.halt_triggered is True
    assert sentinel.paused()
    assert len(fake.calls) == 2
    assert fake.calls[1]["fields"]["hwm_usd"] == "250"


def test_update_zero_hwm_does_not_divide(tmp_path: Path) -> None:
    """Edge case: HWM seeded at 0 (cold account); subsequent 0 equity
    must not blow up on division by zero, and never halts.
    """
    watcher, sentinel, fake = _build_watcher(tmp_path)
    watcher.update(now=_ts(hour=12), equity_usd=Decimal("0"))
    r = watcher.update(now=_ts(hour=13), equity_usd=Decimal("0"))
    assert r.halt_triggered is False
    assert r.state.drawdown_pct == Decimal("0")
    assert not sentinel.paused()
    assert fake.calls == []


def test_update_alerter_optional_still_halts(tmp_path: Path) -> None:
    """Watcher with `alerter=None` should still halt (sentinel + event)."""
    sentinel = Sentinel(tmp_path / "paused.flag")
    watcher = DrawdownWatcher(
        hwm_path=tmp_path / "state" / "drawdown.json",
        sentinel=sentinel,
        halt_pct=Decimal("0.05"),
        alerter=None,
    )
    watcher.update(now=_ts(hour=12), equity_usd=Decimal("200"))
    r = watcher.update(now=_ts(hour=13), equity_usd=Decimal("180"))
    assert r.halt_triggered is True
    assert sentinel.paused()


def test_update_alerter_failure_is_swallowed(tmp_path: Path) -> None:
    """A throwing alerter must not propagate out of `update(...)` —
    halt is more important than alert delivery.
    """
    fake = FakeAlerter()
    fake.raise_on_dispatch = RuntimeError("yuanbao down")
    watcher, sentinel, _ = _build_watcher(tmp_path, alerter=fake)
    watcher.update(now=_ts(hour=12), equity_usd=Decimal("200"))
    r = watcher.update(now=_ts(hour=13), equity_usd=Decimal("180"))
    assert r.halt_triggered is True
    assert sentinel.paused()


# ---------------------------------------------------------------------------
# update() — input validation
# ---------------------------------------------------------------------------


def test_update_rejects_naive_datetime(tmp_path: Path) -> None:
    watcher, _sentinel, _fake = _build_watcher(tmp_path)
    naive = dt.datetime(2026, 5, 24, 12, 0, 0)
    with pytest.raises(ValueError, match="timezone-aware"):
        watcher.update(now=naive, equity_usd=Decimal("100"))


def test_update_rejects_negative_equity(tmp_path: Path) -> None:
    watcher, _sentinel, _fake = _build_watcher(tmp_path)
    with pytest.raises(ValueError, match=">= 0"):
        watcher.update(now=_ts(), equity_usd=Decimal("-1"))


def test_update_rejects_non_numeric_equity(tmp_path: Path) -> None:
    watcher, _sentinel, _fake = _build_watcher(tmp_path)
    with pytest.raises(ValueError, match="Decimal-coercible"):
        watcher.update(now=_ts(), equity_usd="not-a-number")


def test_update_accepts_float_and_int_equity(tmp_path: Path) -> None:
    watcher, _sentinel, _fake = _build_watcher(tmp_path)
    r1 = watcher.update(now=_ts(hour=12), equity_usd=100.0)
    assert r1.state.last_equity_usd == Decimal("100.0")
    r2 = watcher.update(now=_ts(hour=13), equity_usd=120)
    assert r2.state.last_equity_usd == Decimal("120")


# ---------------------------------------------------------------------------
# atomic write
# ---------------------------------------------------------------------------


def test_update_writes_atomic_no_tmp_debris(tmp_path: Path) -> None:
    watcher, _sentinel, _fake = _build_watcher(tmp_path)
    watcher.update(now=_ts(), equity_usd=Decimal("150"))
    parent = watcher._hwm_path.parent
    files = list(parent.iterdir())
    # Only the final state file should remain.
    assert len(files) == 1
    assert files[0].name == "drawdown.json"


def test_update_creates_parent_directory(tmp_path: Path) -> None:
    nested = tmp_path / "a" / "b" / "c"
    sentinel = Sentinel(tmp_path / "paused.flag")
    watcher = DrawdownWatcher(
        hwm_path=nested / "drawdown.json",
        sentinel=sentinel,
        halt_pct=Decimal("0.05"),
        alerter=None,
    )
    watcher.update(now=_ts(), equity_usd=Decimal("100"))
    assert (nested / "drawdown.json").exists()


def test_update_persists_across_watcher_instances(tmp_path: Path) -> None:
    """A new watcher pointed at the same state file must see the prior HWM."""
    sentinel = Sentinel(tmp_path / "paused.flag")
    state_path = tmp_path / "state" / "drawdown.json"

    w1 = DrawdownWatcher(
        hwm_path=state_path,
        sentinel=sentinel,
        halt_pct=Decimal("0.05"),
        alerter=None,
    )
    w1.update(now=_ts(hour=12), equity_usd=Decimal("300"))

    # Fresh instance — must read prior HWM from disk.
    w2 = DrawdownWatcher(
        hwm_path=state_path,
        sentinel=sentinel,
        halt_pct=Decimal("0.05"),
        alerter=None,
    )
    r = w2.update(now=_ts(hour=13), equity_usd=Decimal("285"))
    # 300 → 285 = 5% exactly, should halt.
    assert r.halt_triggered is True
    assert r.state.hwm_usd == Decimal("300")


# ---------------------------------------------------------------------------
# reset_hwm operator override
# ---------------------------------------------------------------------------


def test_reset_hwm_rewrites_state_clears_halted(tmp_path: Path) -> None:
    watcher, sentinel, fake = _build_watcher(tmp_path)
    watcher.update(now=_ts(hour=12), equity_usd=Decimal("200"))
    watcher.update(now=_ts(hour=13), equity_usd=Decimal("180"))
    assert sentinel.paused()
    assert len(fake.calls) == 1

    new_state = watcher.reset_hwm(equity_usd=Decimal("180"), now=_ts(hour=14))
    assert new_state.hwm_usd == Decimal("180")
    assert new_state.last_equity_usd == Decimal("180")
    assert new_state.halted is False
    assert new_state.drawdown_pct == Decimal("0")
    # On disk too.
    persisted = watcher.state()
    assert persisted == new_state


def test_reset_hwm_does_not_touch_sentinel(tmp_path: Path) -> None:
    """Reset only manages the HWM file; the sentinel pause persists
    until the operator runs `xtrade ops resume` separately.
    """
    watcher, sentinel, _fake = _build_watcher(tmp_path)
    watcher.update(now=_ts(hour=12), equity_usd=Decimal("200"))
    watcher.update(now=_ts(hour=13), equity_usd=Decimal("180"))
    assert sentinel.paused()

    watcher.reset_hwm(equity_usd=Decimal("180"), now=_ts(hour=14))
    assert sentinel.paused(), "reset_hwm must NOT auto-resume"


def test_reset_hwm_rearms_breach_detector(tmp_path: Path) -> None:
    """After reset, a subsequent breach against the new HWM should halt again."""
    watcher, sentinel, fake = _build_watcher(tmp_path)
    watcher.update(now=_ts(hour=12), equity_usd=Decimal("200"))
    watcher.update(now=_ts(hour=13), equity_usd=Decimal("180"))
    sentinel.resume()
    watcher.reset_hwm(equity_usd=Decimal("180"), now=_ts(hour=14))
    assert len(fake.calls) == 1

    # Second breach against new HWM=180: 180 → 170 = 5.55%.
    r = watcher.update(now=_ts(hour=15), equity_usd=Decimal("170"))
    assert r.halt_triggered is True
    assert sentinel.paused()
    assert len(fake.calls) == 2


def test_reset_hwm_rejects_naive_datetime(tmp_path: Path) -> None:
    watcher, _sentinel, _fake = _build_watcher(tmp_path)
    naive = dt.datetime(2026, 5, 24, 12, 0, 0)
    with pytest.raises(ValueError, match="timezone-aware"):
        watcher.reset_hwm(equity_usd=Decimal("100"), now=naive)


def test_reset_hwm_rejects_negative_equity(tmp_path: Path) -> None:
    watcher, _sentinel, _fake = _build_watcher(tmp_path)
    with pytest.raises(ValueError, match=">= 0"):
        watcher.reset_hwm(equity_usd=Decimal("-10"), now=_ts())


def test_reset_hwm_uses_clock_when_now_omitted(tmp_path: Path) -> None:
    sentinel = Sentinel(tmp_path / "paused.flag")
    fixed = _ts(hour=9)
    watcher = DrawdownWatcher(
        hwm_path=tmp_path / "state" / "drawdown.json",
        sentinel=sentinel,
        halt_pct=Decimal("0.05"),
        alerter=None,
        clock=lambda: fixed,
    )
    state = watcher.reset_hwm(equity_usd=Decimal("100"))
    assert state.last_update_ts == fixed
