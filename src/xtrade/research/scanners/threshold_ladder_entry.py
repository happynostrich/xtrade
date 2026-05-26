"""Phase 6 Task T6 — `threshold_ladder_entry` streaming scanner.

Why this is **not** a `Scanner` ABC subclass
-------------------------------------------
The Phase 2 `Scanner` ABC (`scanners/base.py`) is batch-oriented: it
takes a full close panel (`pd.DataFrame`) and returns `(entries, exits)`
boolean DataFrames sized to the panel. That contract is right for
gridsearch over historical bars, but the brief §T6 design is
**stateful streaming**:

    on_mark(ts, mark) -> list[Signal]
        first time mark crosses heavy_trigger    → emit one heavy Signal
        subsequent crosses (after fall-back)     → no re-emit
        once per UTC day mark satisfies          → emit one dca Signal
        dca trigger                                (deduped by date)

Folding that into the batch ABC would require a panel-wide cumulative
state machine that's awkward to express in pandas and harder to test
than the explicit `on_mark` loop. So this watcher is a standalone
class, instantiated by the supervisor's mark-tick loop (T9/T10 wiring),
matching the brief's pseudocode 1:1.

Direction parameterisation
--------------------------
The same class serves the SPCX short instance today and any future
long-direction instance with a yaml-only flip:

    direction=above  short-style: emit when mark ≥ trigger.
                     Generated Signal carries direction=SHORT.
    direction=below  long-style:  emit when mark ≤ trigger.
                     Generated Signal carries direction=LONG.

Output contract (matches T5 `mcap_anchored_ladder` consumer)
------------------------------------------------------------
Each emitted `Signal` has:

    direction   = SHORT (above) / LONG (below)
    source      = "<fingerprint_prefix>.heavy.v1"
                  or "<fingerprint_prefix>.dca.<yyyymmdd_utc>"
    metadata    = {
        "kind"             : "heavy" | "dca",
        "mark_usd"         : Decimal-str of the mark that triggered,
        "intent_side"      : "SELL" (above) | "BUY" (below),
        "qty"              : audit-only qty (margin / mark, quantized
                              to qty_step) — strategy plugin ignores
                              this and uses its own yaml-configured
                              qty, but the brief §T6 acceptance test
                              checks `qty_step` quantization here.
        "limit_price_bps_offset": optional bps to skew the limit price
                                    against trigger (informational).
        "margin_usd"       : margin budget for this entry (audit).
    }

The natural-key dedup in `SignalQueue.append()` `(generated_at, symbol,
source)` aligns with our fingerprint, so re-emitting a signal across
process restarts is silently absorbed at the queue layer.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from decimal import Decimal
from typing import Any, Literal

from xtrade.instruments.meta import InstrumentMeta, quantize_qty
from xtrade.research.signals import Signal


UTC = dt.timezone.utc

Direction = Literal["above", "below"]

# direction → corresponding Signal.direction value.
_DIRECTION_TO_SIGNAL_DIRECTION: dict[Direction, str] = {
    "above": "SHORT",   # mark rose above trigger → short opportunity
    "below": "LONG",    # mark fell below trigger → long opportunity
}

# direction → intent_side hint stamped into signal metadata.
_DIRECTION_TO_INTENT_SIDE: dict[Direction, str] = {
    "above": "SELL",
    "below": "BUY",
}


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ScannerConfigError(ValueError):
    """Raised by `ThresholdLadderEntryScanner.__init__` on bad config."""


# ---------------------------------------------------------------------------
# Scanner
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class _ScannerConfig:
    """Internal validated config snapshot."""

    instrument: str
    venue: str
    direction: Direction
    heavy_trigger_mark_usd: Decimal
    dca_trigger_mark_usd: Decimal
    heavy_margin_usd: Decimal
    dca_margin_usd: Decimal
    dca_window: Literal["daily"]
    fingerprint_prefix: str
    limit_price_bps_offset: Decimal


class ThresholdLadderEntryScanner:
    """Streaming threshold-crossing scanner (Phase 6 T6).

    Boot-time config
    ----------------
    Required keys (no defaults — the brief intentionally forces every
    instance yaml to be explicit):

        instrument             : `<symbol>.<venue>` matching InstrumentMeta key
        venue                  : Signal.venue value
        direction              : "above" | "below"
        heavy_trigger_mark_usd : Decimal-coercible
        dca_trigger_mark_usd   : Decimal-coercible
        heavy_margin_usd       : Decimal-coercible
        dca_margin_usd         : Decimal-coercible
        fingerprint_prefix     : non-empty string (audit + dedup namespace)

    Optional keys (with documented defaults):

        dca_window               : "daily" (only value accepted in v1)
        limit_price_bps_offset   : "0"
    """

    kind: str = "threshold_ladder_entry"

    def __init__(self, config: dict, meta: InstrumentMeta) -> None:
        if not isinstance(meta, InstrumentMeta):
            raise ScannerConfigError(
                f"meta must be an InstrumentMeta, got {type(meta).__name__}"
            )
        self._meta = meta
        self._config = self._validate_config(config, meta=meta)

        # Runtime state — heavy fires once per instance lifetime, dca
        # dedups by UTC date.
        self._heavy_fired: bool = False
        self._dca_fired_dates: set[dt.date] = set()

    # -- introspection ----------------------------------------------------

    @property
    def direction(self) -> Direction:
        return self._config.direction

    @property
    def fingerprint_prefix(self) -> str:
        return self._config.fingerprint_prefix

    @property
    def heavy_fired(self) -> bool:
        return self._heavy_fired

    def dca_fired_dates(self) -> frozenset[dt.date]:
        return frozenset(self._dca_fired_dates)

    # -- lifecycle hook ---------------------------------------------------

    def on_mark(self, ts: dt.datetime, mark: Decimal) -> list[Signal]:
        """Process one mark tick. Returns 0–2 Signals (heavy and/or dca).

        Parameters
        ----------
        ts
            Tick timestamp, must be tz-aware (UTC normalised internally).
        mark
            Current mark price as `Decimal`. Plain ints/floats are
            coerced via `Decimal(str(x))`; callers should pre-coerce
            in production paths to keep the audit chain Decimal-only.
        """
        if not isinstance(ts, dt.datetime):
            raise TypeError(
                f"ts must be datetime, got {type(ts).__name__}"
            )
        if ts.tzinfo is None:
            raise ValueError("ts must be tz-aware (UTC)")
        ts_utc = ts.astimezone(UTC)
        if not isinstance(mark, Decimal):
            mark = Decimal(str(mark))
        if mark <= 0:
            raise ValueError(f"mark must be > 0, got {mark}")

        out: list[Signal] = []

        if not self._heavy_fired and self._crossed(
            mark, self._config.heavy_trigger_mark_usd
        ):
            out.append(self._emit_heavy(ts_utc, mark))
            self._heavy_fired = True

        if self._crossed(mark, self._config.dca_trigger_mark_usd):
            today = ts_utc.date()
            if today not in self._dca_fired_dates:
                out.append(self._emit_dca(ts_utc, mark, today))
                self._dca_fired_dates.add(today)

        return out

    # -- internals --------------------------------------------------------

    def _crossed(self, mark: Decimal, trigger: Decimal) -> bool:
        """Direction-aware crossing predicate.

        above (short-style): mark ≥ trigger → crossed.
        below (long-style):  mark ≤ trigger → crossed.
        """
        if self._config.direction == "above":
            return mark >= trigger
        return mark <= trigger

    def _emit_heavy(self, ts_utc: dt.datetime, mark: Decimal) -> Signal:
        source = f"{self._config.fingerprint_prefix}.heavy.v1"
        return self._build_signal(
            ts_utc=ts_utc,
            mark=mark,
            kind="heavy",
            source=source,
            margin_usd=self._config.heavy_margin_usd,
        )

    def _emit_dca(
        self,
        ts_utc: dt.datetime,
        mark: Decimal,
        today: dt.date,
    ) -> Signal:
        suffix = today.strftime("%Y%m%d")
        source = f"{self._config.fingerprint_prefix}.dca.{suffix}"
        return self._build_signal(
            ts_utc=ts_utc,
            mark=mark,
            kind="dca",
            source=source,
            margin_usd=self._config.dca_margin_usd,
        )

    def _build_signal(
        self,
        *,
        ts_utc: dt.datetime,
        mark: Decimal,
        kind: str,
        source: str,
        margin_usd: Decimal,
    ) -> Signal:
        direction = _DIRECTION_TO_SIGNAL_DIRECTION[self._config.direction]
        intent_side = _DIRECTION_TO_INTENT_SIDE[self._config.direction]
        # Audit-only qty derived from margin / mark (1× leverage proxy).
        # The downstream strategy ignores this and uses its own
        # yaml-configured qty; we emit it here so the brief §T6 test
        # can lock the qty_step quantization contract.
        raw_qty = margin_usd / mark
        qty = quantize_qty(raw_qty, self._meta)
        # `strength` should match the sign convention of `direction`
        # (LONG positive, SHORT negative). Use ±1 since a crossing is a
        # full-strength conviction signal.
        strength = -1.0 if direction == "SHORT" else 1.0
        return Signal(
            symbol=self._config.instrument,
            venue=self._config.venue,
            direction=direction,  # type: ignore[arg-type]
            strength=strength,
            generated_at=ts_utc,
            source=source,
            metadata={
                "kind": kind,
                "mark_usd": str(mark),
                "intent_side": intent_side,
                "qty": str(qty),
                "limit_price_bps_offset": str(
                    self._config.limit_price_bps_offset
                ),
                "margin_usd": str(margin_usd),
            },
        )

    # -- config validation ------------------------------------------------

    @staticmethod
    def _validate_config(
        config: dict,
        *,
        meta: InstrumentMeta,
    ) -> _ScannerConfig:
        if not isinstance(config, dict):
            raise ScannerConfigError(
                f"config must be a mapping, got {type(config).__name__}"
            )

        instrument = config.get("instrument")
        if not isinstance(instrument, str) or not instrument:
            raise ScannerConfigError(
                f"instrument must be a non-empty string, got {instrument!r}"
            )
        if instrument != meta.symbol:
            raise ScannerConfigError(
                f"instrument {instrument!r} does not match meta.symbol "
                f"{meta.symbol!r}"
            )

        venue = config.get("venue")
        if not isinstance(venue, str) or not venue:
            raise ScannerConfigError(
                f"venue must be a non-empty string, got {venue!r}"
            )

        direction = config.get("direction")
        if direction not in ("above", "below"):
            raise ScannerConfigError(
                f"direction must be 'above' or 'below', got {direction!r}"
            )

        heavy_trigger = _coerce_decimal("heavy_trigger_mark_usd", config)
        dca_trigger = _coerce_decimal("dca_trigger_mark_usd", config)
        heavy_margin = _coerce_decimal("heavy_margin_usd", config)
        dca_margin = _coerce_decimal("dca_margin_usd", config)

        for label, value in (
            ("heavy_trigger_mark_usd", heavy_trigger),
            ("dca_trigger_mark_usd", dca_trigger),
            ("heavy_margin_usd", heavy_margin),
            ("dca_margin_usd", dca_margin),
        ):
            if value <= 0:
                raise ScannerConfigError(f"{label} must be > 0, got {value}")

        # Direction-aware sanity: for 'above', dca trigger must sit below
        # the heavy trigger (later/laddered entry on the way up). For
        # 'below', it sits above the heavy trigger (later entry on the
        # way down). Equal values are nonsensical for a ladder.
        if direction == "above" and dca_trigger >= heavy_trigger:
            raise ScannerConfigError(
                f"direction=above requires dca_trigger ({dca_trigger}) < "
                f"heavy_trigger ({heavy_trigger})"
            )
        if direction == "below" and dca_trigger <= heavy_trigger:
            raise ScannerConfigError(
                f"direction=below requires dca_trigger ({dca_trigger}) > "
                f"heavy_trigger ({heavy_trigger})"
            )

        dca_window = config.get("dca_window", "daily")
        if dca_window != "daily":
            raise ScannerConfigError(
                f"dca_window must be 'daily' (only value accepted in v1), "
                f"got {dca_window!r}"
            )

        fingerprint_prefix = config.get("fingerprint_prefix")
        if not isinstance(fingerprint_prefix, str) or not fingerprint_prefix:
            raise ScannerConfigError(
                "fingerprint_prefix must be a non-empty string, got "
                f"{fingerprint_prefix!r}"
            )

        if "limit_price_bps_offset" in config:
            bps_offset = _coerce_decimal("limit_price_bps_offset", config)
        else:
            bps_offset = Decimal(0)
        if bps_offset < 0:
            raise ScannerConfigError(
                f"limit_price_bps_offset must be >= 0, got {bps_offset}"
            )

        return _ScannerConfig(
            instrument=instrument,
            venue=venue,
            direction=direction,  # type: ignore[arg-type]
            heavy_trigger_mark_usd=heavy_trigger,
            dca_trigger_mark_usd=dca_trigger,
            heavy_margin_usd=heavy_margin,
            dca_margin_usd=dca_margin,
            dca_window="daily",
            fingerprint_prefix=fingerprint_prefix,
            limit_price_bps_offset=bps_offset,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _coerce_decimal(field: str, body: dict[str, Any]) -> Decimal:
    if field not in body:
        raise ScannerConfigError(f"missing required field {field!r}")
    value = body[field]
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except Exception as exc:
        raise ScannerConfigError(
            f"{field!r} must be coercible to Decimal, got {value!r}"
        ) from exc
