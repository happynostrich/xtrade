"""DrawdownWatcher — high-water-mark tracking + halt sentinel.

Phase 6 Task T7 (see `docs/phase6_brief.md` §5 T7). Persistent NAV
high-water-mark; halts the supervisor (pause sentinel + crit alert)
when the running equity falls `halt_pct` below the HWM.

State file
----------
`/var/lib/xtrade/state/drawdown.json` (0640 xtrade:xtrade in prod;
tmp_path-based in tests). Atomic write template — same fsync +
`os.replace` pattern as `xtrade.live.sentinel`. Schema:

    {
      "hwm_usd": "200.00",
      "last_equity_usd": "188.00",
      "last_update_ts": "2026-05-24T12:00:00+00:00",
      "halted": false,
      "drawdown_pct": "0.060",
      "halt_pct": "0.05"
    }

Brief §10 schema is `{hwm_usd, last_equity_usd, last_update_ts}`; we
extend with three fields:

  - `halted`: lets `update(...)` enforce one halt push per breach
    (prevents per-iteration alert spam while drawdown stays underwater)
    without consulting the sentinel body (the sentinel is shared by
    disk / soft-kill / manual pauses, so we cannot disambiguate
    drawdown halts from there).
  - `drawdown_pct`: convenience for `xtrade ops status` so the
    operator does not have to recompute from the two raw fields.
  - `halt_pct`: records the threshold the file was produced under,
    so a mismatched `--halt_pct` change is detectable in ops.

Halt semantics
--------------
On `update(now, equity_usd)`:

  - First-ever call: HWM = equity_usd, halted = False.
  - equity_usd > hwm_usd: HWM updates to equity_usd, halted resets
    to False ONLY if equity > hwm (i.e., we made a new high). This
    is NOT an "auto-resume" — the sentinel pause persists until the
    operator explicitly clears it (brief §5 T7: "不自动 resume").
    Resetting the in-memory `halted` here just rearms the breach
    detector so a future drawdown can re-halt.
  - drawdown_pct = (hwm - equity) / hwm.
  - drawdown_pct >= halt_pct and NOT halted:
      → set halted = True
      → sentinel.pause(reason="drawdown.halt:<pct>")
      → emit "supervisor.drawdown.halt" structured log
      → push severity=crit alert (if alerter wired)
      → return DrawdownUpdateResult(halt_triggered=True, ...)

The supervisor calls `update(...)` each iteration; subsequent breaches
while still halted return `halt_triggered=False` so no duplicate alert
goes out (brief §5 T9 wording "同档不重复推" applied to drawdown).

Operator overrides
------------------
`reset_hwm(equity_usd)` — used by `xtrade ops reset_drawdown_hwm
--yes` to declare a new HWM baseline after a halt (e.g., when the
operator decides the float is the new normal). Writes a fresh state
file with `halted=False`; does NOT touch the sentinel — the operator
runs `xtrade ops resume` separately.

`state()` — read-only accessor for `xtrade ops status` JSON.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import logging
import os
import tempfile
from decimal import Decimal
from pathlib import Path
from typing import Callable

from xtrade.bridge.alerter import AlertBridge
from xtrade.live.sentinel import Sentinel
from xtrade.obs import emit_event


log = logging.getLogger("xtrade.live.drawdown")


UTC = dt.timezone.utc

_STATE_FILE_MODE = 0o640


class DrawdownConfigError(ValueError):
    """Raised when DrawdownWatcher is built with incomplete config."""


@dataclasses.dataclass(frozen=True)
class DrawdownState:
    """On-disk representation of the watcher state."""

    hwm_usd: Decimal
    last_equity_usd: Decimal
    last_update_ts: dt.datetime
    halted: bool
    drawdown_pct: Decimal
    halt_pct: Decimal

    def to_dict(self) -> dict[str, str | bool]:
        return {
            "hwm_usd": str(self.hwm_usd),
            "last_equity_usd": str(self.last_equity_usd),
            "last_update_ts": self.last_update_ts.astimezone(UTC).isoformat(),
            "halted": self.halted,
            "drawdown_pct": str(self.drawdown_pct),
            "halt_pct": str(self.halt_pct),
        }

    @classmethod
    def from_dict(cls, body: dict[str, object]) -> "DrawdownState":
        try:
            hwm = Decimal(str(body["hwm_usd"]))
            last_eq = Decimal(str(body["last_equity_usd"]))
            ts_raw = str(body["last_update_ts"])
            ts = dt.datetime.fromisoformat(ts_raw)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=UTC)
            halted = bool(body.get("halted", False))
            dd_pct = Decimal(str(body.get("drawdown_pct", "0")))
            halt_pct = Decimal(str(body.get("halt_pct", "0.05")))
        except (KeyError, ValueError) as exc:
            raise DrawdownConfigError(
                f"drawdown state file corrupt or incomplete: {exc}"
            ) from exc
        return cls(
            hwm_usd=hwm,
            last_equity_usd=last_eq,
            last_update_ts=ts,
            halted=halted,
            drawdown_pct=dd_pct,
            halt_pct=halt_pct,
        )


@dataclasses.dataclass(frozen=True)
class DrawdownUpdateResult:
    """Outcome of one `update(...)` call."""

    state: DrawdownState
    halt_triggered: bool


class DrawdownWatcher:
    """Persistent HWM tracker with pause-sentinel halt on `halt_pct` breach."""

    DEFAULT_HALT_PCT: Decimal = Decimal("0.05")

    def __init__(
        self,
        *,
        hwm_path: Path | str,
        sentinel: Sentinel,
        halt_pct: Decimal | float | str = DEFAULT_HALT_PCT,
        alerter: AlertBridge | None = None,
        instrument: str | None = None,
        clock: Callable[[], dt.datetime] | None = None,
    ) -> None:
        if sentinel is None:
            raise DrawdownConfigError("sentinel is required")
        try:
            pct = Decimal(str(halt_pct))
        except Exception as exc:  # noqa: BLE001
            raise DrawdownConfigError(
                f"halt_pct must be Decimal-coercible, got {halt_pct!r}"
            ) from exc
        if pct <= 0:
            raise DrawdownConfigError(f"halt_pct must be > 0, got {pct}")
        if pct >= 1:
            raise DrawdownConfigError(
                f"halt_pct must be < 1 (fractional), got {pct}"
            )

        self._hwm_path = Path(hwm_path)
        self._sentinel = sentinel
        self._halt_pct = pct
        self._alerter = alerter
        self._instrument = instrument
        self._clock = clock or (lambda: dt.datetime.now(tz=UTC))

    # ------------------------------------------------------------------
    # introspection
    # ------------------------------------------------------------------

    @property
    def halt_pct(self) -> Decimal:
        return self._halt_pct

    @property
    def hwm_path(self) -> Path:
        return self._hwm_path

    def state(self) -> DrawdownState | None:
        """Read current state from disk; None if the file does not exist."""
        if not self._hwm_path.exists():
            return None
        try:
            body = json.loads(self._hwm_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise DrawdownConfigError(
                f"drawdown state file unreadable at {self._hwm_path}: {exc}"
            ) from exc
        return DrawdownState.from_dict(body)

    # ------------------------------------------------------------------
    # main entry
    # ------------------------------------------------------------------

    def update(
        self,
        *,
        now: dt.datetime,
        equity_usd: Decimal | float | str,
    ) -> DrawdownUpdateResult:
        """Apply one equity observation; halt on first breach."""
        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware (UTC)")
        now_utc = now.astimezone(UTC)
        try:
            equity = Decimal(str(equity_usd))
        except Exception as exc:  # noqa: BLE001
            raise ValueError(
                f"equity_usd must be Decimal-coercible, got {equity_usd!r}"
            ) from exc
        if equity < 0:
            raise ValueError(
                f"equity_usd must be >= 0 (account equity), got {equity}"
            )

        prior = self.state()

        # First-ever call: seed HWM = equity, no halt.
        if prior is None:
            new_state = DrawdownState(
                hwm_usd=equity,
                last_equity_usd=equity,
                last_update_ts=now_utc,
                halted=False,
                drawdown_pct=Decimal("0"),
                halt_pct=self._halt_pct,
            )
            self._write_state(new_state)
            return DrawdownUpdateResult(state=new_state, halt_triggered=False)

        hwm = prior.hwm_usd
        halted = prior.halted

        # New high: HWM updates and the breach detector rearms. We do
        # NOT clear the sentinel — operator owns that.
        if equity > hwm:
            hwm = equity
            halted = False
            drawdown_pct = Decimal("0")
        else:
            if hwm > 0:
                drawdown_pct = (hwm - equity) / hwm
            else:
                drawdown_pct = Decimal("0")

        halt_triggered = False
        if drawdown_pct >= self._halt_pct and not halted:
            halt_triggered = True
            halted = True

        new_state = DrawdownState(
            hwm_usd=hwm,
            last_equity_usd=equity,
            last_update_ts=now_utc,
            halted=halted,
            drawdown_pct=drawdown_pct,
            halt_pct=self._halt_pct,
        )
        self._write_state(new_state)

        if halt_triggered:
            self._fire_halt(state=new_state, when=now_utc)

        return DrawdownUpdateResult(state=new_state, halt_triggered=halt_triggered)

    # ------------------------------------------------------------------
    # operator override
    # ------------------------------------------------------------------

    def reset_hwm(
        self,
        *,
        equity_usd: Decimal | float | str,
        now: dt.datetime | None = None,
    ) -> DrawdownState:
        """Rewrite the state file with a new HWM = `equity_usd`, halted=False.

        Backing call for `xtrade ops reset_drawdown_hwm --yes`. Does
        NOT touch the sentinel; the operator clears it via
        `xtrade ops resume` separately so the workflow is auditable
        in two distinct journal events.
        """
        try:
            equity = Decimal(str(equity_usd))
        except Exception as exc:  # noqa: BLE001
            raise ValueError(
                f"equity_usd must be Decimal-coercible, got {equity_usd!r}"
            ) from exc
        if equity < 0:
            raise ValueError(
                f"equity_usd must be >= 0, got {equity}"
            )

        when = now or self._clock()
        if when.tzinfo is None:
            raise ValueError("now must be timezone-aware (UTC)")

        new_state = DrawdownState(
            hwm_usd=equity,
            last_equity_usd=equity,
            last_update_ts=when.astimezone(UTC),
            halted=False,
            drawdown_pct=Decimal("0"),
            halt_pct=self._halt_pct,
        )
        self._write_state(new_state)
        emit_event(
            log,
            "supervisor.drawdown.hwm_reset",
            hwm_usd=str(equity),
            halt_pct=str(self._halt_pct),
            instrument=self._instrument,
        )
        return new_state

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _write_state(self, state: DrawdownState) -> None:
        self._hwm_path.parent.mkdir(parents=True, exist_ok=True)
        body = state.to_dict()
        # Atomic write template — same as `xtrade.live.sentinel`.
        fd, tmp_name = tempfile.mkstemp(
            prefix=f"{self._hwm_path.stem}.",
            suffix=".json.tmp",
            dir=str(self._hwm_path.parent),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(body, fh, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_name, self._hwm_path)
            try:
                os.chmod(self._hwm_path, _STATE_FILE_MODE)
            except OSError:
                # Best-effort on platforms without POSIX permissions.
                pass
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    def _fire_halt(self, *, state: DrawdownState, when: dt.datetime) -> None:
        """Sentinel + structured log + crit alert on halt transition."""
        # Format the drawdown_pct for the reason string with 4 decimal
        # places so the journal line is readable.
        pct_str = f"{state.drawdown_pct:.4f}"
        reason = f"drawdown.halt:{pct_str}"
        # The sentinel may already be paused for another reason (disk,
        # mcap soft-kill, manual). Overwriting the body here is the
        # documented sentinel behaviour (`Sentinel.pause` is idempotent
        # and replaces); the operator-visible state in `xtrade ops
        # status` will combine both watchers.
        self._sentinel.pause(reason=reason, now=when)

        emit_event(
            log,
            "supervisor.drawdown.halt",
            level=logging.ERROR,
            instrument=self._instrument,
            hwm_usd=str(state.hwm_usd),
            equity_usd=str(state.last_equity_usd),
            drawdown_pct=pct_str,
            halt_pct=str(self._halt_pct),
        )

        if self._alerter is not None:
            try:
                self._alerter.dispatch_alert(
                    severity="crit",
                    event="supervisor.drawdown.halt",
                    message=(
                        f"drawdown halt: equity={state.last_equity_usd} "
                        f"hwm={state.hwm_usd} drawdown_pct={pct_str} "
                        f"(>= halt_pct={self._halt_pct})"
                    ),
                    instrument=self._instrument,
                    fields={
                        "hwm_usd": str(state.hwm_usd),
                        "equity_usd": str(state.last_equity_usd),
                        "drawdown_pct": pct_str,
                        "halt_pct": str(self._halt_pct),
                    },
                )
            except Exception:  # noqa: BLE001
                log.exception("supervisor.drawdown.halt alert dispatch failed")
