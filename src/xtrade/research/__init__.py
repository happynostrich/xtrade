"""xtrade research / opportunity-discovery layer (Phase 2).

This package is the *read side* of the system: it consumes the historical
bars Phase 1 wrote into `ParquetDataCatalog`, runs technical scanners
(via vectorbt), and emits Signals to a jsonl queue. It must never call
`xtrade.live.*` or construct a `TradingNode` (Phase 2 brief §6 — research
layer is execution-free).
"""

from xtrade.research.frames import bars_to_dataframe, bars_to_panel
from xtrade.research.gridsearch import run_grid
from xtrade.research.scanners import (
    BreakoutScanner,
    MeanReversionScanner,
    MomentumScanner,
    Scanner,
    SpreadScanner,
    available_scanners,
    get_scanner,
)
from xtrade.research.universe import (
    SymbolSpec,
    UniverseConfig,
    UniverseConfigError,
    load_universe,
)

__all__ = [
    "BreakoutScanner",
    "MeanReversionScanner",
    "MomentumScanner",
    "Scanner",
    "SpreadScanner",
    "SymbolSpec",
    "UniverseConfig",
    "UniverseConfigError",
    "available_scanners",
    "bars_to_dataframe",
    "bars_to_panel",
    "get_scanner",
    "load_universe",
    "run_grid",
]
