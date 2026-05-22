"""Phase 3 strategy framework — pure-Python signal-driven layer.

Sits between the Phase 2 scanners (which write `Signal` objects to a
`SignalQueue`) and the Phase 1 execution kernel (`XtradeStrategy` /
Nautilus `TradingNode`). Unlike `xtrade.strategies` (plural), which is
Nautilus-coupled, this package is pure Python so it can:

  - be unit-tested without spinning up a `BacktestEngine`;
  - be exercised from a paper-run loop that owns its own clock;
  - enforce the Phase 3 risk + approval gates at a single chokepoint.

Submodules
----------
- `intent`    : `OrderIntent` / `Fill` dataclasses (Decimal money path).
- `base`      : `SignalDrivenStrategy` ABC + `@register_strategy` registry.
- `consumer`  : `SignalConsumer` — thin wrapper around `SignalQueue`.
- `runner`    : `run_paper` orchestrator (Task 5).
- `plugins/`  : built-in strategy plugins.
"""

from xtrade.strategy.base import (
    AccountSnapshot,
    SignalDrivenStrategy,
    StrategyRegistrationError,
    available_strategies,
    load_strategy,
    register_strategy,
)
from xtrade.strategy.consumer import SignalConsumer
from xtrade.strategy.intent import (
    Fill,
    OrderIntent,
    OrderIntentError,
)
from xtrade.strategy.runner import PaperRunResult, run_paper

# Importing the plugins package registers all shipped strategies with
# the global registry as a side effect.
from xtrade.strategy import plugins as _plugins  # noqa: F401

__all__ = [
    "AccountSnapshot",
    "Fill",
    "OrderIntent",
    "OrderIntentError",
    "PaperRunResult",
    "run_paper",
    "SignalConsumer",
    "SignalDrivenStrategy",
    "StrategyRegistrationError",
    "available_strategies",
    "load_strategy",
    "register_strategy",
]
