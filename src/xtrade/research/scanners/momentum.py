"""Momentum scanner — MA-crossover form.

Long when the fast MA crosses *above* the slow MA; flat when it crosses
back below. We deliberately mirror the Phase 1 `DemoEmaCross` strategy
shape so the S7 vectorbt ↔ Nautilus parity test has a like-for-like
reference.

Params:
  - `fast` (int) — short MA window (default grid: 5, 10).
  - `slow` (int) — long MA window  (default grid: 20, 50).

Implementation note: vectorbt's `MA.run` accepts both single integers and
parameter combos. We call it once with scalars and let `gridsearch.run_grid`
fan out combinations — this keeps each `compute_signals` call self-
contained (no leaky vbt parameter dimensions in the entries/exits panel).
"""

from __future__ import annotations

from typing import Any

import pandas as pd
import vectorbt as vbt

from xtrade.research.scanners.base import Scanner, register_scanner


@register_scanner
class MomentumScanner(Scanner):
    name = "momentum"

    @classmethod
    def default_param_grid(cls) -> dict[str, list[Any]]:
        return {"fast": [5, 10], "slow": [20, 50]}

    def compute_signals(
        self, panel: pd.DataFrame, params: dict[str, Any]
    ) -> tuple[pd.DataFrame, pd.DataFrame]:
        fast = int(params.get("fast", 5))
        slow = int(params.get("slow", 20))
        if fast <= 0 or slow <= 0:
            raise ValueError(f"fast/slow must be positive, got fast={fast} slow={slow}")
        if fast >= slow:
            raise ValueError(f"fast ({fast}) must be < slow ({slow})")

        fast_ma = vbt.MA.run(panel, fast, short_name="fast")
        slow_ma = vbt.MA.run(panel, slow, short_name="slow")
        entries = fast_ma.ma_crossed_above(slow_ma)
        exits = fast_ma.ma_crossed_below(slow_ma)
        # vectorbt returns DataFrames whose columns are MultiIndexed with
        # the indicator level. Drop the level so columns line up with `panel`.
        entries = _drop_indicator_level(entries, panel.columns)
        exits = _drop_indicator_level(exits, panel.columns)
        return entries.fillna(False), exits.fillna(False)


def _drop_indicator_level(df: pd.DataFrame, target_cols: pd.Index) -> pd.DataFrame:
    """Strip vectorbt's leading param-level from a result DataFrame's columns.

    `MA.run(panel, window).ma_crossed_above(MA.run(panel, other))` yields
    columns like `(fast_window=5, slow_window=20, symbol)`. We collapse to
    a plain `(symbol)` axis so downstream code can treat entries/exits as
    "same shape as panel".
    """
    if isinstance(df.columns, pd.MultiIndex):
        # The symbol level is the last one (vbt convention).
        df = df.copy()
        df.columns = df.columns.get_level_values(-1)
    # Reorder columns to match the input panel exactly.
    return df.reindex(columns=target_cols)
