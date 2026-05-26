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
    MAINNET_MAX_DRAWDOWN_PCT_CEILING,
    MAINNET_MAX_NOTIONAL_CEILING_USD,
    MainnetRiskTooLooseError,
    MaxDrawdownPct,
    MaxNotionalPerOrder,
    MaxPositionPerSymbol,
    MaxTotalNotional,
    RiskRule,
    RuleResult,
    assert_mainnet_risk_ceiling,
    load_rules_from_yaml,
)

__all__ = [
    "DryRunReport",
    "IntentEvaluation",
    "MAINNET_MAX_DRAWDOWN_PCT_CEILING",
    "MAINNET_MAX_NOTIONAL_CEILING_USD",
    "MainnetRiskTooLooseError",
    "MaxDrawdownPct",
    "MaxNotionalPerOrder",
    "MaxPositionPerSymbol",
    "MaxTotalNotional",
    "RiskDecision",
    "RiskGate",
    "RiskRule",
    "RuleResult",
    "assert_mainnet_risk_ceiling",
    "dry_run",
    "load_rules_from_yaml",
]
