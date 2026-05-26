"""McapSoftKillWatcher — debounced market-cap soft-kill trigger.

Phase 6 Task T8 (see `docs/phase6_brief.md` §5 T8). When the running
implied market cap (`mark × shares_outstanding`) crosses a configured
trigger value for `consecutive_iterations` successive observations in
the breach direction, the watcher:

  1. Writes the supervisor sentinel with
     `reason="mcap.softkill:<boundary>:<mcap_usd>"`.
  2. Emits a `supervisor.mcap.softkill` ERROR event.
  3. Invokes the shared `emergency_close` runner (Task T10), defaulting
     to `side="reduce-only-tp-only"` — cancels reduce-only TP ladder
     but leaves the entry limit orders intact so a future operator
     re-entry is still possible.
  4. Pushes a severity=crit alert via the optional `AlertBridge`.

Boundary parameterization
-------------------------
The brief documents this as a *generic-by-default* watcher: SPCXUSDT
runs `boundary="above"` (short bias — kill when mcap rises past the
$3.5T trigger), while a long-bias instrument would run
`boundary="below"`. The breach predicate is:

    above:   breached iff mcap_usd >= trigger
    below:   breached iff mcap_usd <= trigger

Debouncing
----------
A single noisy tick is not a soft-kill: the watcher counts
**consecutive** in-breach observations and only fires once the counter
reaches `consecutive_iterations` (default 3). Any in-direction reset
(a clean tick that is NOT breached) zeros the counter.

State file
----------
`/var/lib/xtrade/state/mcap_softkill.json` (0640 in prod; `tmp_path`
in tests). Atomic write template — same `tempfile.mkstemp` + `fsync` +
`os.replace` pattern as `xtrade.live.sentinel` and
`xtrade.live.drawdown`. Schema:

    {
      "consecutive_breaches": 0|1|2|3|...,
      "last_mcap_usd": "3490000000000.00",
      "triggered": false,
      "boundary": "above" | "below",
      "trigger_mcap_usd": "3500000000000",
      "consecutive_iterations": 3,
      "last_update_ts": "ISO 8601 UTC"
    }

`triggered=True` is sticky on disk: once the watcher fires, subsequent
calls return immediately without re-firing the alert or re-running
emergency_close. The operator clears the sticky state by running
`xtrade ops resume` (which clears the sentinel) followed by a manual
state-file deletion / `xtrade ops reset_mcap_softkill` (not provided
here — out of scope for T8; brief §5 T8 explicitly defers automatic
recovery).

Decoupling from T10
-------------------
The watcher does NOT import `xtrade.ops.emergency_close` directly.
Instead it accepts an `emergency_close_runner: Callable[..., None]`
in its constructor; the supervisor wires the real T10 runner once it
exists, and tests inject a fake recorder. This keeps T8 self-testable
before T10 lands and keeps the dependency arrow pointing the right
way (ops/cli depends on live/, never the reverse).
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
from typing import Any, Callable, Literal

from xtrade.bridge.alerter import AlertBridge
from xtrade.instruments.meta import InstrumentMeta, mcap_from_price
from xtrade.live.sentinel import Sentinel
from xtrade.obs import emit_event


log = logging.getLogger("xtrade.live.mcap_softkill")


UTC = dt.timezone.utc
_STATE_FILE_MODE = 0o640


Boundary = Literal["above", "below"]


class McapSoftKillConfigError(ValueError):
    """Raised when McapSoftKillWatcher is built with invalid config."""


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class McapSoftKillState:
    """On-disk representation of the watcher state."""

    consecutive_breaches: int
    last_mcap_usd: Decimal
    triggered: bool
    boundary: Boundary
    trigger_mcap_usd: Decimal
    consecutive_iterations: int
    last_update_ts: dt.datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            "consecutive_breaches": int(self.consecutive_breaches),
            "last_mcap_usd": str(self.last_mcap_usd),
            "triggered": bool(self.triggered),
            "boundary": self.boundary,
            "trigger_mcap_usd": str(self.trigger_mcap_usd),
            "consecutive_iterations": int(self.consecutive_iterations),
            "last_update_ts": self.last_update_ts.astimezone(UTC).isoformat(),
        }

    @classmethod
    def from_dict(cls, body: dict[str, object]) -> "McapSoftKillState":
        try:
            consecutive = int(body["consecutive_breaches"])
            last_mcap = Decimal(str(body["last_mcap_usd"]))
            triggered = bool(body["triggered"])
            boundary_raw = str(body["boundary"])
            trigger_mcap = Decimal(str(body["trigger_mcap_usd"]))
            consecutive_iterations = int(body["consecutive_iterations"])
            ts_raw = str(body["last_update_ts"])
            ts = dt.datetime.fromisoformat(ts_raw)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=UTC)
        except (KeyError, ValueError) as exc:
            raise McapSoftKillConfigError(
                f"mcap_softkill state file corrupt or incomplete: {exc}"
            ) from exc
        if boundary_raw not in ("above", "below"):
            raise McapSoftKillConfigError(
                f"mcap_softkill state file has invalid boundary={boundary_raw!r}"
            )
        return cls(
            consecutive_breaches=consecutive,
            last_mcap_usd=last_mcap,
            triggered=triggered,
            boundary=boundary_raw,  # type: ignore[arg-type]
            trigger_mcap_usd=trigger_mcap,
            consecutive_iterations=consecutive_iterations,
            last_update_ts=ts,
        )


@dataclasses.dataclass(frozen=True)
class McapSoftKillUpdateResult:
    """Outcome of one `update(...)` call."""

    state: McapSoftKillState
    fired_this_call: bool


# ---------------------------------------------------------------------------
# Runner protocol
# ---------------------------------------------------------------------------


EmergencyCloseRunner = Callable[..., Any]
"""Signature of the T10 shared emergency_close runner.

    runner(*, side: Literal["reduce-only-tp-only", "all"],
              instrument: str, when: datetime) -> Any

Concrete T10 will likely also accept `venues_yaml`, `sentinel_path`,
and `alerter`; the watcher only relies on `side` + `instrument` since
the supervisor that wires the watcher already owns the other deps.
The watcher catches any exception so a runner failure cannot wedge
the supervisor loop.
"""


# ---------------------------------------------------------------------------
# Watcher
# ---------------------------------------------------------------------------


class McapSoftKillWatcher:
    """Debounced mcap soft-kill trigger keyed on `(boundary, trigger)`."""

    DEFAULT_CONSECUTIVE_ITERATIONS: int = 3
    DEFAULT_STATE_PATH: Path = Path("/var/lib/xtrade/state/mcap_softkill.json")

    def __init__(
        self,
        *,
        meta: InstrumentMeta,
        trigger_mcap_usd: Decimal | float | str,
        boundary: Boundary,
        sentinel: Sentinel,
        emergency_close_runner: EmergencyCloseRunner | None = None,
        alerter: AlertBridge | None = None,
        consecutive_iterations: int = DEFAULT_CONSECUTIVE_ITERATIONS,
        state_path: Path | str = DEFAULT_STATE_PATH,
        instrument: str | None = None,
        clock: Callable[[], dt.datetime] | None = None,
    ) -> None:
        if meta is None:
            raise McapSoftKillConfigError("meta is required")
        if sentinel is None:
            raise McapSoftKillConfigError("sentinel is required")
        if boundary not in ("above", "below"):
            raise McapSoftKillConfigError(
                f"boundary must be 'above' or 'below', got {boundary!r}"
            )
        try:
            trigger = Decimal(str(trigger_mcap_usd))
        except Exception as exc:  # noqa: BLE001
            raise McapSoftKillConfigError(
                f"trigger_mcap_usd must be Decimal-coercible, got {trigger_mcap_usd!r}"
            ) from exc
        if trigger <= 0:
            raise McapSoftKillConfigError(
                f"trigger_mcap_usd must be > 0, got {trigger}"
            )
        if not isinstance(consecutive_iterations, int):
            raise McapSoftKillConfigError(
                "consecutive_iterations must be int, got "
                f"{type(consecutive_iterations).__name__}"
            )
        if consecutive_iterations < 1:
            raise McapSoftKillConfigError(
                f"consecutive_iterations must be >= 1, got {consecutive_iterations}"
            )

        self._meta = meta
        self._trigger = trigger
        self._boundary: Boundary = boundary
        self._sentinel = sentinel
        self._runner = emergency_close_runner
        self._alerter = alerter
        self._consecutive_iterations = int(consecutive_iterations)
        self._state_path = Path(state_path)
        # `instrument` is the alert/runner key; default to meta.symbol so the
        # operator sees a meaningful instrument tag in yuanbao.
        self._instrument = instrument or meta.symbol
        self._clock = clock or (lambda: dt.datetime.now(tz=UTC))

    # ------------------------------------------------------------------
    # introspection
    # ------------------------------------------------------------------

    @property
    def boundary(self) -> Boundary:
        return self._boundary

    @property
    def trigger_mcap_usd(self) -> Decimal:
        return self._trigger

    @property
    def consecutive_iterations(self) -> int:
        return self._consecutive_iterations

    @property
    def state_path(self) -> Path:
        return self._state_path

    def state(self) -> McapSoftKillState | None:
        """Read current state from disk; None if the file does not exist."""
        if not self._state_path.exists():
            return None
        try:
            body = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise McapSoftKillConfigError(
                f"mcap_softkill state file unreadable at {self._state_path}: {exc}"
            ) from exc
        return McapSoftKillState.from_dict(body)

    # ------------------------------------------------------------------
    # main entry
    # ------------------------------------------------------------------

    def _is_breached(self, mcap: Decimal) -> bool:
        if self._boundary == "above":
            return mcap >= self._trigger
        return mcap <= self._trigger

    def update(
        self,
        *,
        now: dt.datetime,
        mark: Decimal | float | str,
    ) -> McapSoftKillUpdateResult:
        """Apply one mark observation; fire on `consecutive_iterations` breach."""
        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware (UTC)")
        now_utc = now.astimezone(UTC)
        try:
            mark_dec = Decimal(str(mark))
        except Exception as exc:  # noqa: BLE001
            raise ValueError(
                f"mark must be Decimal-coercible, got {mark!r}"
            ) from exc
        if mark_dec <= 0:
            raise ValueError(f"mark must be > 0, got {mark_dec}")

        mcap = mcap_from_price(mark_dec, self._meta)
        prior = self.state()

        # Once `triggered=True` is sticky on disk, every subsequent call
        # just refreshes `last_mcap_usd` + `last_update_ts` and returns
        # without re-firing. Operator must clear the state file to rearm.
        if prior is not None and prior.triggered:
            new_state = McapSoftKillState(
                consecutive_breaches=prior.consecutive_breaches,
                last_mcap_usd=mcap,
                triggered=True,
                boundary=self._boundary,
                trigger_mcap_usd=self._trigger,
                consecutive_iterations=self._consecutive_iterations,
                last_update_ts=now_utc,
            )
            self._write_state(new_state)
            return McapSoftKillUpdateResult(state=new_state, fired_this_call=False)

        # Compute the new debounce counter.
        prior_consecutive = prior.consecutive_breaches if prior is not None else 0
        if self._is_breached(mcap):
            consecutive = prior_consecutive + 1
        else:
            consecutive = 0

        # Decide whether to fire (this call crosses the consecutive threshold).
        fired_this_call = (
            consecutive >= self._consecutive_iterations
            and (prior is None or not prior.triggered)
        )

        new_state = McapSoftKillState(
            consecutive_breaches=consecutive,
            last_mcap_usd=mcap,
            triggered=fired_this_call,
            boundary=self._boundary,
            trigger_mcap_usd=self._trigger,
            consecutive_iterations=self._consecutive_iterations,
            last_update_ts=now_utc,
        )
        self._write_state(new_state)

        if fired_this_call:
            self._fire_softkill(state=new_state, when=now_utc)

        return McapSoftKillUpdateResult(
            state=new_state, fired_this_call=fired_this_call,
        )

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _write_state(self, state: McapSoftKillState) -> None:
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        body = state.to_dict()
        fd, tmp_name = tempfile.mkstemp(
            prefix=f"{self._state_path.stem}.",
            suffix=".json.tmp",
            dir=str(self._state_path.parent),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(body, fh, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_name, self._state_path)
            try:
                os.chmod(self._state_path, _STATE_FILE_MODE)
            except OSError:
                pass
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    def _fire_softkill(
        self,
        *,
        state: McapSoftKillState,
        when: dt.datetime,
    ) -> None:
        """Sentinel + structured log + emergency_close + crit alert."""
        mcap_str = f"{state.last_mcap_usd}"
        reason = f"mcap.softkill:{self._boundary}:{mcap_str}"
        self._sentinel.pause(reason=reason, now=when)

        emit_event(
            log,
            "supervisor.mcap.softkill",
            level=logging.ERROR,
            instrument=self._instrument,
            boundary=self._boundary,
            mcap_usd=mcap_str,
            trigger_mcap_usd=str(self._trigger),
            consecutive_breaches=int(state.consecutive_breaches),
            consecutive_iterations=int(self._consecutive_iterations),
        )

        # Emergency close runner — wrap broadly so a runner crash does
        # not abort the rest of the fire sequence (alert still goes out).
        if self._runner is not None:
            try:
                self._runner(
                    side="reduce-only-tp-only",
                    instrument=self._instrument,
                    when=when,
                )
            except Exception:  # noqa: BLE001
                log.exception(
                    "supervisor.mcap.softkill emergency_close runner crashed"
                )

        if self._alerter is not None:
            try:
                self._alerter.dispatch_alert(
                    severity="crit",
                    event="supervisor.mcap.softkill",
                    message=(
                        f"mcap soft-kill triggered: boundary={self._boundary} "
                        f"mcap={mcap_str} (trigger={self._trigger}) — "
                        f"emergency_close reduce-only-tp-only fired"
                    ),
                    instrument=self._instrument,
                    fields={
                        "boundary": self._boundary,
                        "mcap_usd": mcap_str,
                        "trigger_mcap_usd": str(self._trigger),
                        "consecutive_breaches": int(state.consecutive_breaches),
                        "consecutive_iterations": int(
                            self._consecutive_iterations
                        ),
                    },
                )
            except Exception:  # noqa: BLE001
                log.exception(
                    "supervisor.mcap.softkill alert dispatch failed"
                )
