"""Phase 3 testnet end-to-end runner (Task 6 / T6).

`run_live_signal(...)` drives one signal from `SignalQueue` through the
full Phase 3 chain on a testnet venue:

    SignalQueue → strategy.on_signal → RiskGate → ApprovalGate
        (auto: pass / dry_run: record-only / manual: poll until confirmed)
        → run_live(...) — Phase 1 testnet limit-and-cancel hop
        → live_signal_summary.json

The actual venue hop reuses Phase 1's `run_live`, which spins up a
`TradingNode` against a testnet config, places a far-from-market GTC
limit, awaits accept + cancels, then disposes the node. That's the
single most-tested path we have on real testnet — keeping it
load-bearing lets Phase 3 reuse the entire safety story (mainnet
refusal, account snapshot, summary schema) without re-deriving it.

Single-process invariant
------------------------
This module is intentionally synchronous: it `time.sleep`s while
polling the approval queue rather than running an event loop. The
testnet `TradingNode` spin-up happens inside `run_live`, which manages
its own `asyncio.run`. We never hold two event loops simultaneously.

Test isolation
--------------
The live-hop callable is injected (`live_executor`) so offline tests
can exercise the orchestration (signal lookup, intent rejection,
manual-polling, dry-run short-circuit) without dragging in
`TradingNode` / Nautilus engines.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import time
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

from xtrade.research.signals import Signal, SignalQueue
from xtrade.strategy.base import AccountSnapshot, load_strategy
from xtrade.strategy.consumer import SignalConsumer
from xtrade.strategy.intent import OrderIntent


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class LiveSignalResult:
    """Public return of `run_live_signal`."""

    run_id: str
    log_dir: Path
    summary_path: Path
    summary: dict[str, Any]

    @property
    def passed(self) -> bool:
        return bool(self.summary.get("passed", False))


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class LiveSignalError(RuntimeError):
    """Base class for `run_live_signal` business failures."""


class NoMatchingSignalError(LiveSignalError):
    """Raised when no signal matched the filter or `signal_id`."""


class StrategyEmittedNothingError(LiveSignalError):
    """Raised when `strategy.on_signal` returned zero intents."""


class RiskRejectedError(LiveSignalError):
    """Raised when `RiskGate.check` blocked the intent."""


class ApprovalRejectedError(LiveSignalError):
    """Raised when the approval queue row was flipped to `rejected`."""


class ApprovalTimeoutError(LiveSignalError):
    """Raised when manual approval didn't complete within the deadline."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_run_id(supplied: str | None) -> str:
    if supplied:
        return supplied
    stamp = dt.datetime.now(tz=dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"live-signal-{stamp}"


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    import os
    import tempfile

    fd, tmp_name = tempfile.mkstemp(
        prefix=f"{path.stem}.", suffix=".json.tmp", dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, default=str, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _signal_composite_id(sig: Signal) -> str:
    """Same shape we stamp into `OrderIntent.source_signal_id`."""
    return "|".join([sig.generated_at.isoformat(), sig.symbol, sig.source])


def _pick_signal(
    consumer: SignalConsumer,
    *,
    signal_id: str | None,
) -> Signal | None:
    """Return the signal to act on: by `signal_id` if given, else newest."""
    all_signals = consumer.list_all()
    if not all_signals:
        return None
    if signal_id is None:
        return all_signals[-1]
    for sig in all_signals:
        if _signal_composite_id(sig) == signal_id:
            return sig
    return None


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------


def run_live_signal(
    venues_cfg,
    *,
    strategy_name: str,
    signals_root: Path | str,
    instrument_id: str,
    approval_mode: str = "manual",
    signal_id: str | None = None,
    risk_rules: list | None = None,
    strategy_config: dict | None = None,
    safety_multiplier: Decimal = Decimal("0.7"),
    approval_timeout_s: float = 600.0,
    poll_interval_s: float = 2.0,
    venue_timeout_s: float = 60.0,
    approvals_root: Path | str | None = None,
    run_id: str | None = None,
    logs_root: Path | str | None = None,
    live_executor: Callable[..., Any] | None = None,
) -> LiveSignalResult:
    """End-to-end Phase 3 testnet hop driven by one `SignalQueue` row.

    Parameters
    ----------
    venues_cfg
        Loaded `VenuesConfig` (forwarded verbatim to `run_live`).
    strategy_name
        `SignalDrivenStrategy` registry key.
    signals_root
        `SignalQueue` jsonl root directory.
    instrument_id
        Canonical Nautilus instrument id (e.g. `"BTCUSDT.BINANCE"`).
        Both the SignalConsumer filter and `run_live` see this verbatim.
    approval_mode
        `"auto"` / `"manual"` (default) / `"dry_run"`.
    signal_id
        If set, look up exactly one signal by its composite id
        (`f"{generated_at}|{symbol}|{source}"`); else use the newest.
    risk_rules
        Optional list of `RiskRule` instances.
    strategy_config
        Forwarded to the strategy's `__init__`.
    safety_multiplier
        Far-from-market multiplier for the testnet limit order; matches
        Phase 1's `LiveOrderProbe` default of 0.7.
    approval_timeout_s
        Max wall-clock time `run_live_signal` waits for an external
        `xtrade approve confirm <id>` (manual mode only).
    poll_interval_s
        How often to re-read the approval queue while polling.
    venue_timeout_s
        Per-probe testnet timeout (forwarded to `run_live`).
    approvals_root
        Override for `data/approvals/` root.
    run_id, logs_root
        Override for the auto-generated run id / `logs/` root.
    live_executor
        Override for the testnet hop. Default is Phase 1's `run_live`;
        tests inject a dummy callable. Signature must match `run_live`.
    """
    # Phase 5 Task A5 — third lock. Refuse mainnet routing before any
    # log dir is created or the signal queue is opened. Mirrors the
    # double-call pattern in runner.run_live / health.probe: Lock 1
    # (testnet-only) followed by Lock 3 (unlock ritual).
    #
    # We only run the locks when `venues_cfg` is a real `VenuesConfig`
    # — offline tests inject a sentinel object together with a stub
    # `live_executor`, and the eventual real `run_live` (production
    # path) re-asserts both locks itself, so this fast-path check is
    # purely a "fail before side effects" optimisation.
    from xtrade.config import VenuesConfig as _VenuesConfig

    if isinstance(venues_cfg, _VenuesConfig):
        from xtrade.node.factory import _assert_testnet_only
        from xtrade.live.mainnet_unlock import assert_mainnet_unlock

        _assert_testnet_only(venues_cfg)
        assert_mainnet_unlock(venues_cfg)

    # ---- 1. Resolve filesystem layout ----------------------------------
    repo_root = Path(__file__).resolve().parents[3]
    logs_root_p = Path(logs_root) if logs_root is not None else (repo_root / "logs")
    run_id = _resolve_run_id(run_id)
    log_dir = logs_root_p / run_id
    log_dir.mkdir(parents=True, exist_ok=True)

    if approvals_root is None:
        approvals_root = repo_root / "data" / "approvals"
    approvals_root = Path(approvals_root)

    started_at = dt.datetime.now(tz=dt.timezone.utc)

    # ---- 2. Pull the signal --------------------------------------------
    queue = SignalQueue(signals_root)
    consumer = SignalConsumer(queue, symbol=instrument_id)
    signal = _pick_signal(consumer, signal_id=signal_id)
    if signal is None:
        raise NoMatchingSignalError(
            f"no signals match instrument {instrument_id!r} "
            f"under {Path(signals_root)}"
            + (f" (signal_id={signal_id!r})" if signal_id else "")
        )

    # ---- 3. Strategy + flat-account snapshot ---------------------------
    strategy = load_strategy(strategy_name, config=strategy_config)
    # We don't run a paper engine here, so the account is synthetic:
    # cash = 0, no positions, mark = signal-time approximation (None
    # forces the strategy to skip if it cares). MomentumFollow consults
    # `mark_of(key)` so we seed it from the signal's `last_price` if
    # present in metadata; else strategies that need a mark will return
    # nothing and the runner exits cleanly.
    mark_str = (signal.metadata or {}).get("last_price")
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

    intents = list(strategy.on_signal(signal, account))
    if not intents:
        raise StrategyEmittedNothingError(
            f"strategy {strategy_name!r} emitted no intents for signal "
            f"{signal.symbol} {signal.direction} @ {signal.generated_at.isoformat()}; "
            f"likely cause: no mark price (strategy.on_signal returned [])"
        )
    if len(intents) > 1:
        # Phase 3 testnet path is intentionally one-shot; refuse to fire
        # multiple legs out of an abundance of caution.
        raise LiveSignalError(
            f"testnet runner refuses multi-leg signal "
            f"(strategy emitted {len(intents)} intents); "
            f"split into separate signals or use `xtrade paper run`"
        )
    intent: OrderIntent = intents[0]

    # ---- 4. RiskGate ---------------------------------------------------
    from xtrade.risk import RiskGate  # noqa: PLC0415

    risk_gate = RiskGate(rules=tuple(risk_rules or ()))
    risk_decision = risk_gate.check(intent, account)
    if not risk_decision.approve:
        raise RiskRejectedError(
            "intent blocked by RiskGate: " + "; ".join(risk_decision.reasons)
        )

    # ---- 5. ApprovalGate ----------------------------------------------
    from xtrade.approval import ApprovalGate  # noqa: PLC0415

    approval_gate = ApprovalGate(approval_mode, approvals_root)
    approval = approval_gate.decide(intent, now=started_at)

    if approval.mode == "dry_run":
        # Record-only; never submit.
        summary = _build_summary(
            run_id=run_id,
            started_at=started_at,
            strategy_name=strategy_name,
            instrument_id=instrument_id,
            approval_mode=approval_mode,
            signal=signal,
            intent=intent,
            approval=approval,
            risk_rules=risk_rules or (),
            strategy_config=strategy_config,
            live_summary=None,
            passed=False,
            note="dry_run: intent recorded but not submitted",
        )
        summary_path = log_dir / "live_signal_summary.json"
        _atomic_write_json(summary_path, summary)
        return LiveSignalResult(
            run_id=run_id,
            log_dir=log_dir,
            summary_path=summary_path,
            summary=summary,
        )

    # Manual mode → poll until decided.
    if approval.awaiting:
        approval = _wait_for_manual_decision(
            approval_gate=approval_gate,
            record_id=approval.record_id,
            poll_interval_s=poll_interval_s,
            deadline_s=approval_timeout_s,
        )

    if not approval.go:
        raise ApprovalRejectedError(
            f"approval {approval.record_id} rejected (status={approval.status})"
        )

    # ---- 6. Testnet hop ------------------------------------------------
    if live_executor is None:
        from xtrade.live.runner import run_live as live_executor  # noqa: PLC0415

    live_result = live_executor(
        venues_cfg,
        instrument_id=instrument_id,
        strategy="live_order_probe",
        quantity=intent.quantity,
        side=intent.side,
        safety_multiplier=safety_multiplier,
        timeout_s=venue_timeout_s,
        run_id=f"{run_id}.venue",
        logs_root=log_dir,
    )

    live_summary = getattr(live_result, "summary", None)
    venue_passed = bool(getattr(live_result, "passed", False))

    summary = _build_summary(
        run_id=run_id,
        started_at=started_at,
        strategy_name=strategy_name,
        instrument_id=instrument_id,
        approval_mode=approval_mode,
        signal=signal,
        intent=intent,
        approval=approval,
        risk_rules=risk_rules or (),
        strategy_config=strategy_config,
        live_summary=live_summary,
        passed=venue_passed,
        note=(
            "ok: testnet limit accepted and canceled"
            if venue_passed
            else "live order lifecycle incomplete (see live_summary)"
        ),
    )
    summary_path = log_dir / "live_signal_summary.json"
    _atomic_write_json(summary_path, summary)
    return LiveSignalResult(
        run_id=run_id,
        log_dir=log_dir,
        summary_path=summary_path,
        summary=summary,
    )


# ---------------------------------------------------------------------------
# Internal pieces
# ---------------------------------------------------------------------------


def _wait_for_manual_decision(
    *,
    approval_gate,
    record_id: str,
    poll_interval_s: float,
    deadline_s: float,
):
    """Poll `approval_gate.queue` until the record decides or we time out."""
    from xtrade.approval import ApprovalDecision  # noqa: PLC0415

    deadline = time.monotonic() + deadline_s
    while True:
        rows = approval_gate.queue.list()
        for row in rows:
            if row.id != record_id:
                continue
            # Guard against latching onto a `dry_run` / `auto` audit row
            # for the same intent — only a row written in manual mode
            # counts as an operator decision.
            if row.mode != "manual":
                continue
            if row.status in {"confirmed", "rejected"}:
                return ApprovalDecision(
                    go=row.status == "confirmed",
                    awaiting=False,
                    record_id=row.id,
                    status=row.status,
                    mode="manual",
                )
        if time.monotonic() >= deadline:
            raise ApprovalTimeoutError(
                f"manual approval {record_id} did not complete within "
                f"{deadline_s:.0f}s; run `xtrade approve list` to see status"
            )
        time.sleep(poll_interval_s)


def _build_summary(
    *,
    run_id: str,
    started_at: dt.datetime,
    strategy_name: str,
    instrument_id: str,
    approval_mode: str,
    signal: Signal,
    intent: OrderIntent,
    approval,
    risk_rules,
    strategy_config: dict | None,
    live_summary: dict | None,
    passed: bool,
    note: str,
) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "started_at": started_at.isoformat(),
        "completed_at": dt.datetime.now(tz=dt.timezone.utc).isoformat(),
        "mode": "live_signal",
        "strategy": strategy_name,
        "approval_mode": approval_mode,
        "instrument_id": instrument_id,
        "signal": {
            "symbol": signal.symbol,
            "venue": signal.venue,
            "direction": signal.direction,
            "strength": signal.strength,
            "generated_at": signal.generated_at.isoformat(),
            "source": signal.source,
        },
        "intent": intent.to_dict(),
        "approval": {
            "record_id": approval.record_id,
            "status": approval.status,
            "mode": approval.mode,
            "go": approval.go,
            "awaiting": approval.awaiting,
        },
        "live_summary": live_summary,
        "passed": passed,
        "note": note,
        "config": {
            "strategy_config": dict(strategy_config or {}),
            "risk_rules": [type(r).__name__ for r in (risk_rules or ())],
        },
    }
