"""HeartbeatWatcher — supervisor liveness watchdog with severity ladder.

Phase 6 Task T9 (see `docs/phase6_brief.md` §5 T9). Detects when the
supervisor loop has gone quiet (no real signal / no real intent /
no real alert generated) for too long, which usually means
TradingNode froze, the websocket dropped silently, or systemd is
keeping the process up while the work loop has wedged.

State machine
-------------
The watcher tracks a single `_current_level` per
`Literal["info", "warn", "crit"]` and one `_last_activity_ts`. Each
supervisor iteration calls:

    watcher.record_activity(ts=now)   # only when real work happened
    watcher.tick(now=now)             # always

`tick(now)` compares `now - last_activity_ts` against the thresholds
and either escalates (info → warn → crit) or recovers (warn|crit →
info) via exactly one alert per transition. "Same level no repeat"
(brief §5 T9 wording) means we never push warn / crit twice in a row
without an intervening recovery.

Thresholds are constructor-injected so tests can compress the ladder
(e.g. 1 s warn / 2 s crit) without monkey-patching. Production
supervisor.yaml binds the brief's defaults:

    HeartbeatWatcher(idle_warn_s=600, idle_crit_s=1800)

Both thresholds are **strict** at construction:

    0 < idle_warn_s < idle_crit_s

Alert events (brief §10):

    severity=warn  → event="supervisor.heartbeat.idle"
    severity=crit  → event="supervisor.heartbeat.idle"
    severity=info  → event="supervisor.heartbeat.recovered"  (recovery only)

The watcher never inspects venue state directly — it only observes
the supervisor loop's own self-reported activity, so the same
machinery covers "Nautilus dropped the websocket" as well as
"supervisor.py raised an exception every iteration and we were
swallowing it".
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import logging
from typing import Callable, Literal

from xtrade.bridge.alerter import AlertBridge, AlertDispatchResult
from xtrade.obs import emit_event


log = logging.getLogger("xtrade.live.heartbeat")


Level = Literal["info", "warn", "crit"]

UTC = dt.timezone.utc


class HeartbeatConfigError(ValueError):
    """Raised when HeartbeatWatcher is built with non-monotonic thresholds."""


@dataclasses.dataclass(frozen=True)
class HeartbeatTickResult:
    """Outcome of one `tick(now)` call (test helper)."""

    current_level: Level
    previous_level: Level
    elapsed_s: float
    transitioned: bool
    dispatch: AlertDispatchResult | None


class HeartbeatWatcher:
    """Idle-time watchdog with info→warn→crit→info severity ladder."""

    DEFAULT_IDLE_WARN_S: float = 600.0
    DEFAULT_IDLE_CRIT_S: float = 1800.0

    def __init__(
        self,
        *,
        alerter: AlertBridge,
        idle_warn_s: float = DEFAULT_IDLE_WARN_S,
        idle_crit_s: float = DEFAULT_IDLE_CRIT_S,
        instrument: str | None = None,
        clock: Callable[[], dt.datetime] | None = None,
        start_ts: dt.datetime | None = None,
    ) -> None:
        if not isinstance(idle_warn_s, (int, float)):
            raise HeartbeatConfigError(
                f"idle_warn_s must be numeric, got {type(idle_warn_s).__name__}"
            )
        if not isinstance(idle_crit_s, (int, float)):
            raise HeartbeatConfigError(
                f"idle_crit_s must be numeric, got {type(idle_crit_s).__name__}"
            )
        if idle_warn_s <= 0:
            raise HeartbeatConfigError(
                f"idle_warn_s must be > 0, got {idle_warn_s}"
            )
        if idle_crit_s <= idle_warn_s:
            raise HeartbeatConfigError(
                f"idle_crit_s must be > idle_warn_s "
                f"(got idle_warn_s={idle_warn_s}, idle_crit_s={idle_crit_s})"
            )
        if alerter is None:
            raise HeartbeatConfigError("alerter is required")

        self._alerter = alerter
        self._idle_warn_s = float(idle_warn_s)
        self._idle_crit_s = float(idle_crit_s)
        self._instrument = instrument
        self._clock = clock or (lambda: dt.datetime.now(tz=UTC))

        seed = start_ts if start_ts is not None else self._clock()
        if seed.tzinfo is None:
            raise HeartbeatConfigError("start_ts must be timezone-aware (UTC)")
        self._last_activity_ts: dt.datetime = seed.astimezone(UTC)
        self._current_level: Level = "info"

    # ------------------------------------------------------------------
    # introspection (test + ops/status hooks)
    # ------------------------------------------------------------------

    @property
    def current_level(self) -> Level:
        return self._current_level

    @property
    def last_activity_ts(self) -> dt.datetime:
        return self._last_activity_ts

    @property
    def idle_warn_s(self) -> float:
        return self._idle_warn_s

    @property
    def idle_crit_s(self) -> float:
        return self._idle_crit_s

    # ------------------------------------------------------------------
    # supervisor hooks
    # ------------------------------------------------------------------

    def record_activity(self, ts: dt.datetime | None = None) -> None:
        """Bump `_last_activity_ts`. Called when the supervisor loop did real work."""
        when = ts if ts is not None else self._clock()
        if when.tzinfo is None:
            raise ValueError("activity ts must be timezone-aware (UTC)")
        self._last_activity_ts = when.astimezone(UTC)

    def tick(self, now: dt.datetime | None = None) -> HeartbeatTickResult:
        """Evaluate the elapsed-idle ladder; emit one alert on transition.

        Returns a `HeartbeatTickResult` describing the post-state so
        the supervisor can fold it into its iteration metrics.
        """
        when = now if now is not None else self._clock()
        if when.tzinfo is None:
            raise ValueError("tick now ts must be timezone-aware (UTC)")
        when = when.astimezone(UTC)

        elapsed_s = (when - self._last_activity_ts).total_seconds()
        # Clamp negative skew (clock injection edge case) to 0 so we
        # never accidentally escalate on a backwards-moving clock.
        if elapsed_s < 0:
            elapsed_s = 0.0

        previous_level = self._current_level
        next_level: Level
        if elapsed_s >= self._idle_crit_s:
            next_level = "crit"
        elif elapsed_s >= self._idle_warn_s:
            next_level = "warn"
        else:
            next_level = "info"

        if next_level == previous_level:
            return HeartbeatTickResult(
                current_level=previous_level,
                previous_level=previous_level,
                elapsed_s=elapsed_s,
                transitioned=False,
                dispatch=None,
            )

        # State transition — push exactly one alert.
        self._current_level = next_level
        dispatch = self._emit_transition_alert(
            previous_level=previous_level,
            next_level=next_level,
            elapsed_s=elapsed_s,
            when=when,
        )
        return HeartbeatTickResult(
            current_level=next_level,
            previous_level=previous_level,
            elapsed_s=elapsed_s,
            transitioned=True,
            dispatch=dispatch,
        )

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _emit_transition_alert(
        self,
        *,
        previous_level: Level,
        next_level: Level,
        elapsed_s: float,
        when: dt.datetime,
    ) -> AlertDispatchResult:
        recovered = next_level == "info" and previous_level != "info"
        if recovered:
            event = "supervisor.heartbeat.recovered"
            message = (
                f"supervisor heartbeat recovered "
                f"(idle was {elapsed_s:.0f}s, prev_level={previous_level})"
            )
            severity = "info"
        else:
            # info→warn / info→crit / warn→crit all use the same event
            # name (idle) — `severity` carries the distinction.
            event = "supervisor.heartbeat.idle"
            message = (
                f"supervisor heartbeat idle for {elapsed_s:.0f}s "
                f"({previous_level} → {next_level})"
            )
            severity = next_level

        emit_event(
            log,
            "supervisor.heartbeat.transition",
            level=logging.WARNING if severity != "info" else logging.INFO,
            previous_level=previous_level,
            next_level=next_level,
            elapsed_s=round(elapsed_s, 3),
            last_activity=self._last_activity_ts.isoformat(),
            now_iso=when.isoformat(),
        )

        return self._alerter.dispatch_alert(
            severity=severity,
            event=event,
            message=message,
            instrument=self._instrument,
            fields={
                "previous_level": previous_level,
                "next_level": next_level,
                "elapsed_s": round(float(elapsed_s), 3),
                "idle_warn_s": float(self._idle_warn_s),
                "idle_crit_s": float(self._idle_crit_s),
                "last_activity": self._last_activity_ts.isoformat(),
            },
        )
