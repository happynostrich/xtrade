"""Phase 6 Task T5 — `mcap_anchored_ladder` strategy plugin.

Direction-parameterised strategy that implements the "mcap-anchored
ladder" pattern: heavy first-entry on a price threshold, daily DCA
nibbles below it, and a market-cap-anchored take-profit ladder. The
**same code** services SPCXUSDT short (direction=short) today and any
future long-direction instance tomorrow — instance config lives in
yaml, not Python.

Per the Phase 6 brief §T5:

    heavy fire     ────────►  one large limit order at heavy trigger
    daily dca      ────────►  small limit order at dca trigger (1× / UTC day)
    entry fill     ────────►  emit N TP limits, each at mcap_i / shares
    tp fill        ────────►  cancel-and-replace remaining TPs
                              (rebalance fractions to sum to 1.0)
    soft_kill      ────────►  cancel ALL open TP orders; do NOT add or
                              reduce position; supervisor side handles
                              the sentinel + alert.

Boundary protocol with the runner
---------------------------------
The Phase 3 `SignalDrivenStrategy` ABC declares `on_fill(fill) -> None`.
This plugin extends that contract by queueing post-fill TP orders +
cancel requests into per-instance buffers that the supervisor drains
through new public methods:

    .drain_pending_orders() -> list[OrderIntent]
    .drain_pending_cancels() -> list[str]

The base class's return-typing is unchanged, so existing tests /
plugins are unaffected; the supervisor-side wiring lands in T9/T10.

Signal kind protocol
--------------------
`Signal.metadata["kind"]` is the dispatch key (set by the matching
scanner / watcher):

    "heavy"      first-entry signal (one-shot per process lifetime)
    "dca"        daily nibble signal (one-shot per UTC date)
    "soft_kill"  emergency: cancel open TPs, do not flip exposure
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from collections.abc import Iterable
from decimal import Decimal
from typing import Any, Literal

from xtrade.instruments.meta import InstrumentMeta, MetaRegistry, quantize_qty
from xtrade.live.sizing import McapAnchoredSizer
from xtrade.research.signals import Signal
from xtrade.strategy.base import AccountSnapshot, SignalDrivenStrategy, register_strategy
from xtrade.strategy.intent import Fill, OrderIntent


UTC = dt.timezone.utc

Direction = Literal["short", "long"]

# Map plugin direction → expected Signal.direction (the research layer
# uses uppercase LONG/SHORT/FLAT; we use lowercase for the plugin
# config to match the brief's yaml dialect).
_DIRECTION_TO_SIGNAL: dict[Direction, str] = {"short": "SHORT", "long": "LONG"}

# Map plugin direction → entry order side. Short entry sells short;
# long entry buys long. Reduce-only TP orders are the opposite side.
_DIRECTION_TO_ENTRY_SIDE: dict[Direction, str] = {"short": "SELL", "long": "BUY"}
_DIRECTION_TO_TP_SIDE: dict[Direction, str] = {"short": "BUY", "long": "SELL"}


# ---------------------------------------------------------------------------
# Frozen helpers
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class TpLevel:
    """One rung of the take-profit ladder.

    `mcap_usd` anchors the TP at the same market-cap anchor the operator
    reasoned about; the plugin converts to price via the instrument's
    `shares_outstanding` (Phase 6 T3 `InstrumentMeta`). `fraction` is
    the share of the **post-entry** position this rung covers; the
    rebalance logic preserves the original ratios across remaining
    rungs after a TP fills.
    """

    mcap_usd: Decimal
    fraction: Decimal

    def __post_init__(self) -> None:
        if not isinstance(self.mcap_usd, Decimal):
            object.__setattr__(self, "mcap_usd", Decimal(str(self.mcap_usd)))
        if not isinstance(self.fraction, Decimal):
            object.__setattr__(self, "fraction", Decimal(str(self.fraction)))
        if self.mcap_usd <= 0:
            raise ValueError(f"mcap_usd must be > 0, got {self.mcap_usd}")
        if not (Decimal(0) < self.fraction <= Decimal(1)):
            raise ValueError(
                f"fraction must be in (0, 1], got {self.fraction}"
            )


# ---------------------------------------------------------------------------
# Plugin
# ---------------------------------------------------------------------------


@register_strategy("mcap_anchored_ladder")
class McapAnchoredLadderStrategy(SignalDrivenStrategy):
    """Mcap-anchored ladder strategy (Phase 6 T5)."""

    name = "mcap_anchored_ladder"

    # -- construction ------------------------------------------------------

    def __init__(self, config: dict | None = None) -> None:
        super().__init__(config)
        cfg = self.config

        # --- direction ----------------------------------------------------
        direction = cfg.get("direction")
        if direction not in ("short", "long"):
            raise ValueError(
                f"direction must be 'short' or 'long', got {direction!r}"
            )
        self._direction: Direction = direction  # type: ignore[assignment]
        self._expected_signal_direction = _DIRECTION_TO_SIGNAL[self._direction]
        self._entry_side = _DIRECTION_TO_ENTRY_SIDE[self._direction]
        self._tp_side = _DIRECTION_TO_TP_SIDE[self._direction]

        # --- instrument + meta -------------------------------------------
        instrument = cfg.get("instrument")
        if not isinstance(instrument, str) or not instrument:
            raise ValueError(
                f"instrument must be a non-empty string, got {instrument!r}"
            )
        self._instrument: str = instrument
        venue_raw = cfg.get("venue")
        if not isinstance(venue_raw, str) or not venue_raw:
            raise ValueError(
                f"venue must be a non-empty string, got {venue_raw!r}"
            )
        self._venue: str = venue_raw

        meta = cfg.get("meta")
        if isinstance(meta, InstrumentMeta):
            self._meta = meta
        elif isinstance(meta, MetaRegistry):
            self._meta = meta.get(self._instrument)
        else:
            raise ValueError(
                "config must include either 'meta': InstrumentMeta or "
                "'meta': MetaRegistry for the instrument lookup; got "
                f"{type(meta).__name__}"
            )

        # --- sizer ceiling check (boot-time fail) ------------------------
        target_mcap_liq_usd = _to_decimal("target_mcap_liq_usd", cfg)
        mmr = _to_decimal("mmr", cfg)
        leverage = _to_decimal("leverage", cfg)

        heavy_entry = cfg.get("heavy_entry")
        if not isinstance(heavy_entry, dict):
            raise ValueError(
                "heavy_entry must be a mapping with at least "
                "'trigger_mark_usd' and 'qty'"
            )
        heavy_trigger = _to_decimal("trigger_mark_usd", heavy_entry)
        heavy_qty = _to_decimal("qty", heavy_entry)

        self._sizer = McapAnchoredSizer(
            target_mcap_liq_usd=target_mcap_liq_usd,
            mmr=mmr,
            shares_outstanding=self._meta.shares_outstanding,
            direction=self._direction,
        )
        # Boot-time guard: refuse to construct a strategy whose
        # requested leverage already breaks the mcap ceiling at the
        # heavy trigger. Errors are loud (operator must fix yaml).
        self._sizer.validate_strategy_yaml(
            requested_leverage=leverage,
            reference_entry=heavy_trigger,
        )

        # --- dca block ---------------------------------------------------
        dca_entry = cfg.get("dca_entry")
        if not isinstance(dca_entry, dict):
            raise ValueError(
                "dca_entry must be a mapping with 'trigger_mark_usd' and 'qty'"
            )
        dca_trigger = _to_decimal("trigger_mark_usd", dca_entry)
        dca_qty = _to_decimal("qty", dca_entry)

        # --- tp ladder ---------------------------------------------------
        tp_ladder_raw = cfg.get("tp_ladder")
        if not isinstance(tp_ladder_raw, list) or not tp_ladder_raw:
            raise ValueError(
                "tp_ladder must be a non-empty list of {mcap_usd, fraction} "
                "entries"
            )
        ladder = tuple(
            TpLevel(
                mcap_usd=Decimal(str(entry["mcap_usd"])),
                fraction=Decimal(str(entry["fraction"])),
            )
            for entry in tp_ladder_raw
        )
        # Per direction, validate the ladder is monotonic in the
        # profit-taking sense:
        #   short ⇒ entry profits as mcap falls ⇒ rungs descend.
        #   long  ⇒ entry profits as mcap rises ⇒ rungs ascend.
        if self._direction == "short":
            for prev, curr in zip(ladder, ladder[1:]):
                if curr.mcap_usd >= prev.mcap_usd:
                    raise ValueError(
                        f"short tp_ladder mcap_usd must strictly decrease; "
                        f"got {prev.mcap_usd} → {curr.mcap_usd}"
                    )
        else:  # long
            for prev, curr in zip(ladder, ladder[1:]):
                if curr.mcap_usd <= prev.mcap_usd:
                    raise ValueError(
                        f"long tp_ladder mcap_usd must strictly increase; "
                        f"got {prev.mcap_usd} → {curr.mcap_usd}"
                    )
        total_frac = sum((lvl.fraction for lvl in ladder), start=Decimal(0))
        if total_frac != Decimal(1):
            raise ValueError(
                f"tp_ladder fractions must sum to exactly 1.0, got {total_frac}"
            )
        self._tp_ladder: tuple[TpLevel, ...] = ladder

        # --- soft kill ---------------------------------------------------
        # Optional — when omitted, soft_kill signals are still routed
        # (kind dispatch) but the strategy has no preconfigured trigger
        # for its own audit metadata.
        soft_kill_cfg = cfg.get("soft_kill") or {}
        self._soft_kill_boundary: str | None = soft_kill_cfg.get("boundary")
        self._soft_kill_trigger_mcap_usd: Decimal | None = (
            Decimal(str(soft_kill_cfg["trigger_mcap_usd"]))
            if "trigger_mcap_usd" in soft_kill_cfg
            else None
        )

        # --- stash entry params ------------------------------------------
        self._heavy_trigger_mark_usd = heavy_trigger
        self._heavy_qty = heavy_qty
        self._dca_trigger_mark_usd = dca_trigger
        self._dca_qty = dca_qty
        self._fingerprint_prefix: str = str(
            cfg.get("fingerprint_prefix", f"mcap_ladder.{self._direction}")
        )

        # --- mutable state -----------------------------------------------
        self._heavy_fired: bool = False
        self._dca_fired_dates: set[dt.date] = set()
        # Signed position (long > 0, short < 0).
        self._position_qty: Decimal = Decimal(0)
        # Active TP orders keyed by level index. When a TP fills the
        # entry is popped and the remaining ladder is re-emitted with
        # rebalanced fractions.
        self._open_tp_fingerprints: dict[int, str] = {}
        # TP rungs already realized (closed by venue). Excluded from
        # all future rebalances.
        self._filled_tp_levels: set[int] = set()
        # Monotonic counter that disambiguates rebalance generations in
        # the TP fingerprint (so the cancel/replace stream is
        # idempotent across the journal).
        self._rebalance_counter: int = 0
        # Set when a soft-kill signal arrives. Persists for the
        # process lifetime — operator must restart to clear.
        self._soft_killed: bool = False
        # Output buffers — drained by the supervisor after each call.
        self._pending_orders: list[OrderIntent] = []
        self._pending_cancels: list[str] = []

    # -- public test/runtime accessors ------------------------------------

    @property
    def direction(self) -> Direction:
        return self._direction

    @property
    def soft_killed(self) -> bool:
        return self._soft_killed

    @property
    def heavy_fired(self) -> bool:
        return self._heavy_fired

    @property
    def open_tp_count(self) -> int:
        return len(self._open_tp_fingerprints)

    @property
    def position_qty(self) -> Decimal:
        return self._position_qty

    def drain_pending_orders(self) -> list[OrderIntent]:
        """Return + clear the post-fill order buffer. Called by the
        supervisor after each `on_fill`."""
        out = list(self._pending_orders)
        self._pending_orders.clear()
        return out

    def drain_pending_cancels(self) -> list[str]:
        """Return + clear the cancel buffer (fingerprints of TP
        orders that must be cancelled at the venue)."""
        out = list(self._pending_cancels)
        self._pending_cancels.clear()
        return out

    # -- ABC overrides -----------------------------------------------------

    def on_signal(
        self,
        signal: Signal,
        account: AccountSnapshot,
    ) -> Iterable[OrderIntent]:
        kind = signal.metadata.get("kind") if signal.metadata else None

        # Soft kill — emit no orders, but record cancel intent for all
        # currently open TP rungs. Entry limits (if any) are
        # intentionally left in place per brief §T5.
        if kind == "soft_kill":
            return self._handle_soft_kill()

        # Once soft-killed, every future entry signal is a no-op.
        if self._soft_killed:
            return []

        # Direction guard — a scanner that crosses wires should never
        # cause the wrong-direction strategy to act on its signal.
        if signal.direction != self._expected_signal_direction:
            return []

        if kind == "heavy":
            return self._handle_heavy(signal)
        if kind == "dca":
            return self._handle_dca(signal)
        # Unknown / absent kind → ignore. Scanner authors are
        # responsible for setting kind explicitly; silent dropping
        # avoids accidental routing across instance configs.
        return []

    def on_fill(self, fill: Fill) -> None:
        # Track signed position based on intent side.
        signed_qty = (
            fill.quantity if fill.side == "BUY" else -fill.quantity
        )
        self._position_qty += signed_qty

        intent_kind = fill.metadata.get("intent_kind") if fill.metadata else None

        if intent_kind in ("heavy", "dca"):
            # Entry filled (or DCA topped up). (Re)build the TP ladder
            # against the new |position|.
            self._rebuild_tp_ladder(now=fill.ts_event)
            return

        if intent_kind == "tp":
            level_idx = fill.metadata.get("tp_level")
            if isinstance(level_idx, int):
                # Mark this rung permanently realized and drop from the
                # open set; the remaining ladder rebalances against the
                # shrunken position.
                self._open_tp_fingerprints.pop(level_idx, None)
                self._filled_tp_levels.add(level_idx)
            self._rebuild_tp_ladder(now=fill.ts_event)
            return

        # Foreign fill kind: ignore. Tests for kind dispatch live in
        # `test_strategy_mcap_anchored_ladder.py`.

    # -- handlers ---------------------------------------------------------

    def _handle_heavy(self, signal: Signal) -> list[OrderIntent]:
        if self._heavy_fired:
            return []
        self._heavy_fired = True
        intent = self._build_entry_intent(
            signal=signal,
            kind="heavy",
            limit_price=self._heavy_trigger_mark_usd,
            quantity=self._heavy_qty,
        )
        return [intent]

    def _handle_dca(self, signal: Signal) -> list[OrderIntent]:
        today = signal.generated_at.astimezone(UTC).date()
        if today in self._dca_fired_dates:
            return []
        self._dca_fired_dates.add(today)
        intent = self._build_entry_intent(
            signal=signal,
            kind="dca",
            limit_price=self._dca_trigger_mark_usd,
            quantity=self._dca_qty,
            fingerprint_suffix=today.strftime("%Y%m%d"),
        )
        return [intent]

    def _handle_soft_kill(self) -> list[OrderIntent]:
        self._soft_killed = True
        # Move every open TP fingerprint into the cancel buffer; the
        # ladder itself is wiped so future fills don't try to rebalance
        # against ghost rungs.
        for fp in self._open_tp_fingerprints.values():
            self._pending_cancels.append(fp)
        self._open_tp_fingerprints.clear()
        return []

    # -- internals --------------------------------------------------------

    def _build_entry_intent(
        self,
        *,
        signal: Signal,
        kind: str,
        limit_price: Decimal,
        quantity: Decimal,
        fingerprint_suffix: str | None = None,
    ) -> OrderIntent:
        qty = quantize_qty(quantity, self._meta)
        if qty < self._meta.min_qty:
            raise ValueError(
                f"{kind} qty {qty} below instrument min_qty {self._meta.min_qty}"
            )
        suffix = f".{fingerprint_suffix}" if fingerprint_suffix else ""
        return OrderIntent(
            venue=self._venue,
            symbol=self._instrument,
            side=self._entry_side,  # type: ignore[arg-type]
            order_type="LIMIT",
            quantity=qty,
            limit_price=limit_price,
            reduce_only=False,
            time_in_force="GTC",
            source_signal_id=f"{self._fingerprint_prefix}.{kind}{suffix}",
            created_at=signal.generated_at,
            metadata={
                "strategy": self.name,
                "intent_kind": kind,
                "direction": self._direction,
            },
        )

    def _rebuild_tp_ladder(self, *, now: dt.datetime) -> None:
        """Cancel every currently-open TP and re-emit the remaining
        rungs sized against the current |position|.

        Called after every entry fill (initial laddering) and every TP
        fill (rebalance). The buffer is drained by the supervisor.
        """
        # Cancel everything currently outstanding first — the venue
        # only has one TP per rung at a time.
        for fp in self._open_tp_fingerprints.values():
            self._pending_cancels.append(fp)
        self._open_tp_fingerprints.clear()

        position_abs = abs(self._position_qty)
        if position_abs <= 0:
            return

        # Surviving rungs are those whose mcap is still "ahead" of the
        # current position in the profit direction. For simplicity we
        # rebalance over **every** rung whose level index hasn't been
        # filled — the open set is the index space `range(len(ladder))`
        # minus the indices already realized via TP fills.
        remaining_indices = [
            i
            for i, _ in enumerate(self._tp_ladder)
            if i not in self._filled_tp_levels
        ]
        if not remaining_indices:
            return

        total_frac = sum(
            (self._tp_ladder[i].fraction for i in remaining_indices),
            start=Decimal(0),
        )
        if total_frac <= 0:
            return

        for level_idx in remaining_indices:
            level = self._tp_ladder[level_idx]
            normalised = level.fraction / total_frac
            qty = quantize_qty(position_abs * normalised, self._meta)
            if qty < self._meta.min_qty:
                # Cannot place a sub-minimum rung; skip but keep the
                # index in the open set so subsequent rebalances retry.
                continue
            # mark price implied by mcap rung.
            price = level.mcap_usd / self._meta.shares_outstanding
            fingerprint = (
                f"{self._fingerprint_prefix}.tp.level{level_idx}.r{self._rebalance_counter}"
            )
            intent = OrderIntent(
                venue=self._venue,
                symbol=self._instrument,
                side=self._tp_side,  # type: ignore[arg-type]
                order_type="LIMIT",
                quantity=qty,
                limit_price=price,
                reduce_only=True,
                time_in_force="GTC",
                source_signal_id=fingerprint,
                created_at=now,
                metadata={
                    "strategy": self.name,
                    "intent_kind": "tp",
                    "tp_level": level_idx,
                    "direction": self._direction,
                },
            )
            self._pending_orders.append(intent)
            self._open_tp_fingerprints[level_idx] = fingerprint

        self._rebalance_counter += 1


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _to_decimal(field: str, body: dict[str, Any]) -> Decimal:
    if field not in body:
        raise ValueError(f"missing required field {field!r}")
    value = body[field]
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except Exception as exc:
        raise ValueError(
            f"{field!r} must be coercible to Decimal, got {value!r}"
        ) from exc
