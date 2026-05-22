"""Pure-Python risk calibration helper (Phase 3.5 hardening).

Why this exists
---------------
Before deploying a strategy + ``risk.yaml`` to the cloud (Phase 4) or
even to testnet (Phase 3 §3), it's expensive to discover that:

  - the strategy never emits any intent for the typical signal shape
    (mark missing, strength below threshold, ...);
  - every intent is rejected because the caps are too tight;
  - every intent passes because the caps are too loose (defeats the
    purpose);
  - one specific rule is doing all the rejecting and the others are
    inert.

:func:`dry_run` walks exactly the same chain ``xtrade.strategy.runner``
walks — ``strategy.on_signal(signal, account)`` → ``RiskGate.check`` —
but with **no side effects whatsoever**: no venue calls, no jsonl writes,
no `BacktestEngine` instantiation. It returns a structured report the
caller renders however it wants (a CLI prints JSON; a notebook eyeballs
the dict; a future Phase 4 dashboard could feed it directly).

For each emitted intent, every rule is evaluated independently (no
short-circuit) so the report shows the *full* matrix of {intent × rule}
verdicts, not just the first failure.

This module is pure-Python: no Nautilus imports, no network, no disk.
"""

from __future__ import annotations

import dataclasses
from typing import Any

from xtrade.research.signals import Signal
from xtrade.risk.rules import RiskRule
from xtrade.strategy.base import AccountSnapshot, SignalDrivenStrategy
from xtrade.strategy.intent import OrderIntent


@dataclasses.dataclass(frozen=True)
class IntentEvaluation:
    """Per-intent dry-run verdict, with every rule's individual result."""

    intent: OrderIntent
    rule_results: tuple[dict[str, Any], ...]  # ({name, ok, reason}, ...)
    aggregate_approved: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "intent": self.intent.to_dict(),
            "rule_results": [dict(r) for r in self.rule_results],
            "aggregate_approved": self.aggregate_approved,
        }


@dataclasses.dataclass(frozen=True)
class DryRunReport:
    """Top-level dry-run output: signal + account + per-intent verdicts."""

    strategy: str
    signal: Signal
    account: AccountSnapshot
    intents: tuple[IntentEvaluation, ...]

    @property
    def intents_generated(self) -> int:
        return len(self.intents)

    @property
    def intents_approved(self) -> int:
        return sum(1 for e in self.intents if e.aggregate_approved)

    @property
    def intents_rejected(self) -> int:
        return self.intents_generated - self.intents_approved

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy,
            "signal": self.signal.to_dict(),
            "account": {
                "cash_usd": str(self.account.cash_usd),
                "positions": {k: str(v) for k, v in self.account.positions.items()},
                "mark_prices": {k: str(v) for k, v in self.account.mark_prices.items()},
                "nav_usd": str(self.account.nav_usd),
                "peak_nav_usd": str(self.account.peak_nav_usd),
            },
            "intents_generated": self.intents_generated,
            "intents_approved": self.intents_approved,
            "intents_rejected": self.intents_rejected,
            "intents": [e.to_dict() for e in self.intents],
        }


def dry_run(
    *,
    strategy: SignalDrivenStrategy,
    signal: Signal,
    account: AccountSnapshot,
    rules: list[RiskRule],
) -> DryRunReport:
    """Evaluate `strategy.on_signal(signal, account)` against every rule.

    Pure: no I/O, no venue calls, no order construction. Safe to run
    inside notebooks, CI, or pre-flight scripts against any combination
    of synthetic and live signals.

    Parameters
    ----------
    strategy
        A ``SignalDrivenStrategy`` instance (already configured).
    signal
        The signal to feed into ``strategy.on_signal``.
    account
        The account snapshot the strategy and rules see.
    rules
        Risk rules to evaluate. Each rule is checked against every
        emitted intent; no short-circuit so the report shows the full
        matrix.

    Returns
    -------
    DryRunReport
        Frozen dataclass with `.to_dict()` for JSON rendering.
    """
    intents = list(strategy.on_signal(signal, account))
    evaluations: list[IntentEvaluation] = []
    for intent in intents:
        results: list[dict[str, Any]] = []
        for rule in rules:
            r = rule.check(intent, account)
            results.append({"name": rule.name, "ok": bool(r.ok), "reason": r.reason})
        approved = all(r["ok"] for r in results)
        evaluations.append(
            IntentEvaluation(
                intent=intent,
                rule_results=tuple(results),
                aggregate_approved=approved,
            )
        )
    return DryRunReport(
        strategy=strategy.name,
        signal=signal,
        account=account,
        intents=tuple(evaluations),
    )
