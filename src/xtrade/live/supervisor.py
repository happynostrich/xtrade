"""Phase 4 always-on supervisor (Task 5 / T5).

`run_supervisor(config)` is the long-running entry point invoked by
the systemd `xtrade-supervisor.service` unit (`ExecStart=... xtrade
live supervise --config /etc/xtrade/supervisor.yaml`). It polls the
local `SignalQueue` and drives each new signal through the Phase 3
chain:

    SignalQueue → strategy.on_signal → RiskGate → ApprovalGate
        ├── auto      → submit via live_executor (Phase 1 run_live)
        ├── dry_run   → record-only; continue
        └── manual    → bridge.dispatch(record) + park; on next iter
                        check ApprovalQueue for a flip to confirmed →
                        then submit via live_executor.

Differences from Phase 3 `run_live_signal`
------------------------------------------
- Long-running, polling loop instead of one-shot. Cursor lives on disk
  so SIGKILL + systemd restart resumes mid-stream without replaying
  already-handled signals.
- Manual approvals are dispatched to openclaw via `OpenclawBridge` but
  the loop does **not** block waiting for the human; pending intents
  are parked in an in-memory map and re-checked each iteration. A
  fresh in-process map suffices because both the ApprovalQueue and
  signal cursor are persistent — on restart pending rows are
  re-discovered from the queue.
- Sentinel-based pause: when `/run/xtrade/paused.flag` exists the loop
  skips new signal processing **without** committing the cursor, so
  on resume the queued signals replay. Pending approvals already
  dispatched continue to drain (a paused supervisor must still notice
  if openclaw confirms a previously-sent intent).
- Same single-process invariant as Phase 3: this module is
  synchronous; the testnet hop's asyncio lives entirely inside each
  `live_executor` (= `run_live`) call.

Trading node lifecycle
----------------------
Phase 4 Task 5 deliberately keeps the Phase 3 "one TradingNode per
intent" pattern (each `live_executor` call spins a fresh node, places
a far-from-market limit, cancels, disposes). It is wasteful but
**re-uses Phase 1's most-tested code path**. Promoting to a single
persistent always-on node is a Phase 5 follow-up (see `docs/
phase4_brief.md` §5 Task 4 note on `node.start()`); the current design
runs intents serially which is fine while scanner cadence is 5min and
intent execution is ~30s.

Mainnet hard refusal
--------------------
`live_executor` (= `xtrade.live.runner.run_live`) calls
`_assert_testnet_only(venues_cfg)` before any node construction; the
supervisor inherits that guard unmodified.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import logging
import threading
import time
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from xtrade.approval.gate import ApprovalDecision, ApprovalGate, ApprovalMode
from xtrade.approval.queue import ApprovalRecord
from xtrade.bridge.openclaw_webhook import OpenclawBridge
from xtrade.live.sentinel import Sentinel
from xtrade.research.signals import Signal, SignalQueue
from xtrade.risk import RiskGate, RiskRule
from xtrade.strategy.base import AccountSnapshot, load_strategy
from xtrade.strategy.consumer import SignalConsumer
from xtrade.strategy.intent import OrderIntent


log = logging.getLogger("xtrade.supervisor")


# ---------------------------------------------------------------------------
# Config + result types
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class SupervisorConfig:
    """Static configuration loaded from `/etc/xtrade/supervisor.yaml`.

    All path fields accept absolute paths (production layout) or
    relative paths (tests). Optional fields default to in-process
    behaviour suitable for offline testing — production deployments
    set every field explicitly via yaml.
    """

    instrument_id: str
    strategy_name: str
    signals_root: Path
    approvals_root: Path
    cursor_path: Path
    sentinel_path: Path
    logs_root: Path
    approval_mode: ApprovalMode = "manual"
    strategy_config: dict[str, Any] | None = None
    poll_interval_s: float = 2.0
    venue_timeout_s: float = 60.0
    safety_multiplier: Decimal = Decimal("0.7")
    # `risk_rules` and `venues_cfg` are not yaml-friendly types so the
    # CLI loader resolves them and hands the supervisor concrete
    # objects.
    risk_rules: tuple[RiskRule, ...] = ()
    venues_cfg: Any = None  # `VenuesConfig | None`; None ⇒ live_executor must be injected
    # `bridge` may be None when the supervisor runs in dry_run mode
    # (the brief §9 explicitly supports an early no-bridge soak).
    bridge: OpenclawBridge | None = None


@dataclasses.dataclass(frozen=True)
class SupervisorIterationResult:
    """What happened in one poll loop iteration (returned to tests)."""

    iteration: int
    paused: bool
    signals_seen: int
    signals_processed: int
    intents_submitted: int
    intents_parked_manual: int
    pending_promoted: int
    pending_rejected: int
    errors: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def run_supervisor(
    config: SupervisorConfig,
    *,
    stop_event: threading.Event | None = None,
    live_executor: Callable[..., Any] | None = None,
    max_iterations: int | None = None,
    clock: Callable[[], dt.datetime] | None = None,
) -> list[SupervisorIterationResult]:
    """Run the supervisor loop until `stop_event` is set.

    Parameters
    ----------
    config
        Fully-resolved `SupervisorConfig` (yaml + env merged by caller).
    stop_event
        Set by the CLI signal handler (SIGINT/SIGTERM) to drain
        gracefully. If None a fresh Event is created (loop must be
        bounded by `max_iterations`).
    live_executor
        Callable that mimics `xtrade.live.runner.run_live`. Defaults
        to the real `run_live`; tests inject a stub so they don't
        spin up Nautilus. Signature must accept `venues_cfg` plus the
        kwargs we pass below.
    max_iterations
        Test hook to bound the loop. None ⇒ infinite (until
        `stop_event`).
    clock
        Override `datetime.now(tz=UTC)`. Tests inject a controllable
        clock; production passes None.

    Returns
    -------
    A list of per-iteration result objects (helpful for tests; in
    production the list grows unbounded so callers should not retain
    the return value of a long-lived process — use `journalctl`
    instead).
    """
    if stop_event is None:
        stop_event = threading.Event()
    now = clock or (lambda: dt.datetime.now(tz=dt.timezone.utc))

    if live_executor is None:
        # Lazy import keeps offline tests free of the Nautilus dep.
        from xtrade.live.runner import run_live as live_executor  # noqa: PLC0415

    queue = SignalQueue(config.signals_root)
    consumer = SignalConsumer(
        queue,
        symbol=config.instrument_id,
        cursor_path=config.cursor_path,
    )
    approval_gate = ApprovalGate(config.approval_mode, config.approvals_root)
    risk_gate = RiskGate(rules=tuple(config.risk_rules or ()))
    sentinel = Sentinel(config.sentinel_path)
    strategy = load_strategy(
        config.strategy_name, config=config.strategy_config
    )

    # Pending manual approvals: {record_id: (intent, dispatched)}. Loaded
    # eagerly from the approval queue so a fresh restart re-discovers
    # rows the previous supervisor parked.
    pending: dict[str, _PendingIntent] = {
        rec.id: _PendingIntent(
            record_id=rec.id,
            intent=rec.intent,
            dispatched=rec.dispatch is not None,
        )
        for rec in approval_gate.pending()
        if rec.mode == "manual"
    }

    log.info(
        "supervisor.start instrument=%s mode=%s strategy=%s "
        "signals_root=%s pending=%d",
        config.instrument_id,
        config.approval_mode,
        config.strategy_name,
        config.signals_root,
        len(pending),
    )

    results: list[SupervisorIterationResult] = []
    iteration = 0
    while not stop_event.is_set():
        if max_iterations is not None and iteration >= max_iterations:
            break
        iteration += 1
        try:
            iter_result = _supervisor_iteration(
                iteration=iteration,
                config=config,
                consumer=consumer,
                approval_gate=approval_gate,
                risk_gate=risk_gate,
                strategy=strategy,
                sentinel=sentinel,
                bridge=config.bridge,
                live_executor=live_executor,
                pending=pending,
                now=now,
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("supervisor.iteration.crash iteration=%d", iteration)
            iter_result = SupervisorIterationResult(
                iteration=iteration,
                paused=False,
                signals_seen=0,
                signals_processed=0,
                intents_submitted=0,
                intents_parked_manual=0,
                pending_promoted=0,
                pending_rejected=0,
                errors=(f"{type(exc).__name__}: {exc}",),
            )
        results.append(iter_result)

        # Wait one poll interval, but wake early if stop_event fires.
        stop_event.wait(config.poll_interval_s)

    log.info(
        "supervisor.stop iterations=%d submitted=%d parked=%d",
        len(results),
        sum(r.intents_submitted for r in results),
        sum(r.intents_parked_manual for r in results),
    )
    if config.bridge is not None:
        config.bridge.close()
    return results


# ---------------------------------------------------------------------------
# Iteration body
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=False, slots=True)
class _PendingIntent:
    """A manual-mode intent waiting on a human decision."""

    record_id: str
    intent: OrderIntent
    dispatched: bool  # True once `bridge.dispatch` returned (ok or terminal)


def _supervisor_iteration(
    *,
    iteration: int,
    config: SupervisorConfig,
    consumer: SignalConsumer,
    approval_gate: ApprovalGate,
    risk_gate: RiskGate,
    strategy,
    sentinel: Sentinel,
    bridge: OpenclawBridge | None,
    live_executor: Callable[..., Any],
    pending: dict[str, _PendingIntent],
    now: Callable[[], dt.datetime],
) -> SupervisorIterationResult:
    paused = sentinel.paused()

    # Phase 1 of an iteration: promote any pending-manual rows that the
    # operator (or openclaw → bridge inbound) has flipped to confirmed
    # or rejected since last poll. Runs even when paused — a paused
    # supervisor must still drain decisions already in flight.
    promoted, rejected = _drain_pending_decisions(
        pending=pending,
        approval_gate=approval_gate,
        live_executor=live_executor,
        config=config,
    )

    if paused:
        log.warning(
            "supervisor.iteration.paused iteration=%d "
            "(promoted=%d rejected=%d)",
            iteration, promoted, rejected,
        )
        return SupervisorIterationResult(
            iteration=iteration,
            paused=True,
            signals_seen=0,
            signals_processed=0,
            intents_submitted=promoted,
            intents_parked_manual=0,
            pending_promoted=promoted,
            pending_rejected=rejected,
        )

    # Phase 2: drain new signals; produce + route intents. NB: we
    # iterate the consumer eagerly into a list so we can both count
    # signals_seen AND commit the cursor only after every signal in
    # this batch is handled.
    new_signals = list(consumer.iter_new())
    signals_seen = len(new_signals)
    submitted = promoted
    parked = 0
    processed = 0

    for sig in new_signals:
        try:
            intents = _strategy_intents_for(strategy, sig, config.instrument_id)
        except Exception as exc:  # noqa: BLE001
            log.exception(
                "supervisor.strategy.crash signal=%s err=%s",
                sig.dedup_key(), exc,
            )
            continue
        processed += 1
        for intent in intents:
            try:
                route_result = _route_intent(
                    intent=intent,
                    risk_gate=risk_gate,
                    approval_gate=approval_gate,
                    bridge=bridge,
                    live_executor=live_executor,
                    config=config,
                    now=now,
                    pending=pending,
                )
            except Exception as exc:  # noqa: BLE001
                log.exception(
                    "supervisor.intent.crash intent=%s err=%s",
                    intent.fingerprint(), exc,
                )
                continue
            if route_result == "submitted":
                submitted += 1
            elif route_result == "parked":
                parked += 1

    # Commit cursor only after every signal in this batch was processed.
    # If the supervisor crashes mid-batch (e.g. RiskGate threw on one
    # signal) we replay the whole batch on restart, which is safe
    # because ApprovalGate is idempotent on `(fingerprint, mode)`.
    consumer.commit()

    return SupervisorIterationResult(
        iteration=iteration,
        paused=False,
        signals_seen=signals_seen,
        signals_processed=processed,
        intents_submitted=submitted,
        intents_parked_manual=parked,
        pending_promoted=promoted,
        pending_rejected=rejected,
    )


# ---------------------------------------------------------------------------
# Routing helpers
# ---------------------------------------------------------------------------


def _strategy_intents_for(
    strategy, sig: Signal, instrument_id: str,
) -> list[OrderIntent]:
    """Build the synthetic account snapshot Phase 3 uses, then call
    `strategy.on_signal`. Mirrors `signal_runner._snapshot_for(...)`.
    """
    mark_str = (sig.metadata or {}).get("last_price")
    marks: dict[str, Decimal] = {}
    if mark_str is not None:
        try:
            marks[instrument_id] = Decimal(str(mark_str))
        except Exception:  # noqa: BLE001
            pass
    account = AccountSnapshot(
        cash_usd=Decimal(0),
        positions={instrument_id: Decimal(0)},
        mark_prices=marks,
        nav_usd=Decimal(0),
        peak_nav_usd=Decimal(0),
    )
    return list(strategy.on_signal(sig, account))


def _route_intent(
    *,
    intent: OrderIntent,
    risk_gate: RiskGate,
    approval_gate: ApprovalGate,
    bridge: OpenclawBridge | None,
    live_executor: Callable[..., Any],
    config: SupervisorConfig,
    now: Callable[[], dt.datetime],
    pending: dict[str, _PendingIntent],
) -> str:
    """Return one of: 'submitted', 'parked', 'rejected', 'dry_run'."""
    risk_decision = risk_gate.check(intent, _empty_account(config.instrument_id))
    if not risk_decision.approve:
        log.info(
            "supervisor.intent.risk_rejected intent=%s reasons=%s",
            intent.fingerprint(), risk_decision.reasons,
        )
        return "rejected"

    decision: ApprovalDecision = approval_gate.decide(intent, now=now())

    if decision.mode == "dry_run":
        log.info(
            "supervisor.intent.dry_run intent=%s record=%s",
            intent.fingerprint(), decision.record_id,
        )
        return "dry_run"

    if decision.go:
        _submit_intent(intent, live_executor=live_executor, config=config)
        return "submitted"

    # manual mode, awaiting human
    if decision.awaiting:
        record = approval_gate.queue.get(decision.record_id)
        if record is None:
            log.error(
                "supervisor.intent.parked but queue.get() missed record=%s",
                decision.record_id,
            )
            return "rejected"
        if decision.record_id in pending:
            # Re-emit: dispatch already done in a prior iteration.
            return "parked"
        slot = _PendingIntent(
            record_id=decision.record_id,
            intent=intent,
            dispatched=False,
        )
        pending[decision.record_id] = slot
        if bridge is not None:
            _dispatch_via_bridge(bridge=bridge, record=record, slot=slot)
        else:
            log.info(
                "supervisor.intent.parked record=%s (no bridge configured)",
                decision.record_id,
            )
        return "parked"
    return "rejected"


def _drain_pending_decisions(
    *,
    pending: dict[str, _PendingIntent],
    approval_gate: ApprovalGate,
    live_executor: Callable[..., Any],
    config: SupervisorConfig,
) -> tuple[int, int]:
    """Promote any pending rows the operator has flipped since last poll.

    Returns `(promoted_count, rejected_count)`. Mutates `pending` in
    place (rows that no longer need supervisor attention are dropped).
    """
    if not pending:
        return 0, 0
    promoted = 0
    rejected = 0
    for record_id in list(pending.keys()):
        # We only care about the **manual** row for this id — a coexisting
        # dry_run audit row would also share `id` but is never going to
        # decide. ApprovalQueue iteration yields one row at a time so we
        # walk the manual row explicitly.
        row = _find_manual_row(approval_gate, record_id)
        if row is None:
            # Vanished from queue (shouldn't happen; defensive)
            pending.pop(record_id, None)
            continue
        if row.status == "confirmed":
            slot = pending.pop(record_id)
            try:
                _submit_intent(
                    slot.intent, live_executor=live_executor, config=config
                )
                promoted += 1
                log.info(
                    "supervisor.pending.promoted record=%s", record_id,
                )
            except Exception as exc:  # noqa: BLE001
                log.exception(
                    "supervisor.pending.submit_failed record=%s err=%s",
                    record_id, exc,
                )
                # Keep it out of pending; operator will need to look at logs.
        elif row.status == "rejected":
            pending.pop(record_id, None)
            rejected += 1
            log.info(
                "supervisor.pending.rejected record=%s reason=%s",
                record_id, row.reason,
            )
    return promoted, rejected


def _find_manual_row(approval_gate: ApprovalGate, record_id: str):
    for row in approval_gate.queue:
        if row.id == record_id and row.mode == "manual":
            return row
    return None


def _dispatch_via_bridge(
    *,
    bridge: OpenclawBridge,
    record: ApprovalRecord,
    slot: _PendingIntent,
) -> None:
    """One-shot dispatch; never raises. Sets `slot.dispatched=True`."""
    try:
        result = bridge.dispatch(record)
        slot.dispatched = True
        log.info(
            "supervisor.bridge.dispatch record=%s ok=%s status=%s attempts=%d",
            record.id, result.ok, result.status_code, result.attempts,
        )
    except Exception as exc:  # noqa: BLE001
        # Bridge dispatch is supposed to be terminal-safe (it annotates
        # the queue on failure). Anything escaping here is a bug — log
        # but do not crash the supervisor.
        log.exception(
            "supervisor.bridge.dispatch.unhandled record=%s err=%s",
            record.id, exc,
        )


def _submit_intent(
    intent: OrderIntent,
    *,
    live_executor: Callable[..., Any],
    config: SupervisorConfig,
) -> Any:
    """Drive one intent through `live_executor` (= Phase 1 `run_live`)."""
    stamp = dt.datetime.now(tz=dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_id = f"supervisor-{stamp}-{intent.fingerprint()[:8]}"
    return live_executor(
        config.venues_cfg,
        instrument_id=config.instrument_id,
        strategy="live_order_probe",
        quantity=intent.quantity,
        side=intent.side,
        safety_multiplier=config.safety_multiplier,
        timeout_s=config.venue_timeout_s,
        run_id=run_id,
        logs_root=config.logs_root,
    )


def _empty_account(instrument_id: str) -> AccountSnapshot:
    return AccountSnapshot(
        cash_usd=Decimal(0),
        positions={instrument_id: Decimal(0)},
        mark_prices={},
        nav_usd=Decimal(0),
        peak_nav_usd=Decimal(0),
    )


# ---------------------------------------------------------------------------
# YAML config loader
# ---------------------------------------------------------------------------


def load_supervisor_config(
    yaml_path: Path | str,
    *,
    env: Mapping[str, str] | None = None,
    bridge: OpenclawBridge | None = None,
    extra: Mapping[str, Any] | None = None,
) -> SupervisorConfig:
    """Load `/etc/xtrade/supervisor.yaml` → `SupervisorConfig`.

    Yaml shape (every path is absolute on the VPS, relative ok in tests):

        instrument_id: BTCUSDT-PERP.BINANCE
        strategy_name: momentum_follow
        strategy_config: {...}
        approval_mode: manual
        signals_root: /var/lib/xtrade/signals
        approvals_root: /var/lib/xtrade/approvals
        cursor_path: /var/lib/xtrade/signals/.cursor
        sentinel_path: /run/xtrade/paused.flag
        logs_root: /var/lib/xtrade/logs
        venues_yaml: /etc/xtrade/venues.binance_spot.testnet.yaml
        risk_yaml: /etc/xtrade/risk.yaml          # optional
        poll_interval_s: 2.0
        venue_timeout_s: 60.0
        safety_multiplier: "0.7"

    `bridge` is built externally (typically via
    `OpenclawBridge.from_env(os.environ)`) and passed in so the
    loader can stay yaml-only.
    """
    import yaml  # local import to keep test-only paths off the import graph

    raw = yaml.safe_load(Path(yaml_path).read_text(encoding="utf-8")) or {}
    if extra:
        raw = {**raw, **extra}

    venues_cfg = None
    venues_yaml = raw.get("venues_yaml")
    if venues_yaml:
        from xtrade.config import load_venues  # noqa: PLC0415

        venues_cfg = load_venues(venues_yaml)

    risk_rules: tuple[RiskRule, ...] = ()
    risk_yaml = raw.get("risk_yaml")
    if risk_yaml:
        from xtrade.risk import load_rules_from_yaml  # noqa: PLC0415

        risk_rules = tuple(load_rules_from_yaml(risk_yaml))

    return SupervisorConfig(
        instrument_id=str(raw["instrument_id"]),
        strategy_name=str(raw["strategy_name"]),
        signals_root=Path(raw["signals_root"]),
        approvals_root=Path(raw["approvals_root"]),
        cursor_path=Path(raw["cursor_path"]),
        sentinel_path=Path(raw["sentinel_path"]),
        logs_root=Path(raw["logs_root"]),
        approval_mode=raw.get("approval_mode", "manual"),
        strategy_config=raw.get("strategy_config"),
        poll_interval_s=float(raw.get("poll_interval_s", 2.0)),
        venue_timeout_s=float(raw.get("venue_timeout_s", 60.0)),
        safety_multiplier=Decimal(str(raw.get("safety_multiplier", "0.7"))),
        risk_rules=risk_rules,
        venues_cfg=venues_cfg,
        bridge=bridge,
    )
