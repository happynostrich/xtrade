"""Phase 3 risk module — mandatory single point for all order intents.

Public surface
--------------
- `RiskRule` ABC + four built-in rules:
  - `MaxNotionalPerOrder`
  - `MaxPositionPerSymbol`
  - `MaxTotalNotional`
  - `MaxDrawdownPct`
- `RiskGate.check(intent, account) -> RiskDecision`
- `RiskDecision(approve|reject, reasons)`
- `load_rules_from_yaml(path)`

See `docs/phase3_brief.md` §5 Task 2 and §6 (security boundary).
"""

from xtrade.risk.dry_run import DryRunReport, IntentEvaluation, dry_run
from xtrade.risk.gate import (
    RiskDecision,
    RiskGate,
)
from xtrade.risk.rules import (
    MaxDrawdownPct,
    MaxNotionalPerOrder,
    MaxPositionPerSymbol,
    MaxTotalNotional,
    RiskRule,
    RuleResult,
    load_rules_from_yaml,
)

__all__ = [
    "DryRunReport",
    "IntentEvaluation",
    "MaxDrawdownPct",
    "MaxNotionalPerOrder",
    "MaxPositionPerSymbol",
    "MaxTotalNotional",
    "RiskDecision",
    "RiskGate",
    "RiskRule",
    "RuleResult",
    "dry_run",
    "load_rules_from_yaml",
]
