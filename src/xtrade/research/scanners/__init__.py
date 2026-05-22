"""Phase 2 scanners (Task 3 / S3).

Each scanner consumes a `pd.DataFrame` "close panel" (rows = UTC ts_event,
columns = symbol id strings) plus a `params` mapping, and returns:

  - `(entries, exits)` boolean panels of the same shape (used by vectorbt
    inside the grid search via `Portfolio.from_signals`); and
  - via `Scanner.run(...)`, a long-format DataFrame of Signal *records*
    suitable for serialising into the signal queue.

Adding a new scanner: subclass `Scanner`, set `name`, implement
`compute_signals` + `default_param_grid`, and register it via the
`@register_scanner` decorator.
"""

from xtrade.research.scanners.base import (
    Scanner,
    available_scanners,
    get_scanner,
    register_scanner,
)
from xtrade.research.scanners.breakout import BreakoutScanner
from xtrade.research.scanners.mean_reversion import MeanReversionScanner
from xtrade.research.scanners.momentum import MomentumScanner
from xtrade.research.scanners.spread import SpreadScanner

__all__ = [
    "BreakoutScanner",
    "MeanReversionScanner",
    "MomentumScanner",
    "Scanner",
    "SpreadScanner",
    "available_scanners",
    "get_scanner",
    "register_scanner",
]
