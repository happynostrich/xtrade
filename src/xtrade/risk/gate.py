"""RiskGate — mandatory single point for risk checks (Phase 3 Task 2 / T3).

`xtrade.strategy.runner` MUST call `gate.check(intent, account)` before
any intent reaches the venue (paper, testnet, mainnet). The import-graph
lint in `tests/test_risk_gate.py` enforces that the strategy package
itself never imports Nautilus execution APIs / calls `submit_order` —
the only path to a venue is via the runner, which is the only caller
of `RiskGate.check`.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from xtrade.strategy.base import AccountSnapshot
    from xtrade.strategy.intent import OrderIntent

from xtrade.risk.rules import RiskRule, RuleResult


@dataclasses.dataclass(frozen=True)
class RiskDecision:
    """Aggregate verdict from running every rule in the gate.

    `approve == True` means EVERY rule passed; `reasons` is empty.
    `approve == False` means at least one rule rejected; `reasons`
    lists every failing rule's reason (the gate may run all rules or
    short-circuit on the first failure — see `RiskGate(short_circuit=)`).
    """

    approve: bool
    reasons: tuple[str, ...] = ()

    @classmethod
    def passed(cls) -> "RiskDecision":
        return cls(approve=True, reasons=())

    @classmethod
    def rejected(cls, reasons: Iterable[str]) -> "RiskDecision":
        return cls(approve=False, reasons=tuple(reasons))


class RiskGate:
    """Single point through which all order intents must pass.

    Parameters
    ----------
    rules : iterable of `RiskRule`
        Evaluated in given order.
    short_circuit : bool
        If True (default), stop at the first failing rule. If False,
        run every rule and accumulate reasons; useful in tests and in
        audit dashboards.
    """

    def __init__(
        self,
        rules: Iterable[RiskRule],
        *,
        short_circuit: bool = True,
    ) -> None:
        self.rules: list[RiskRule] = list(rules)
        self.short_circuit = short_circuit

    def check(
        self,
        intent: "OrderIntent",
        account: "AccountSnapshot",
    ) -> RiskDecision:
        """Run every rule in order; return the aggregate decision."""
        reasons: list[str] = []
        for rule in self.rules:
            result: RuleResult = rule.check(intent, account)
            if not result.ok:
                reasons.append(result.reason)
                if self.short_circuit:
                    break
        if reasons:
            return RiskDecision.rejected(reasons)
        return RiskDecision.passed()

    def describe(self) -> list[dict]:
        return [rule.describe() for rule in self.rules]
