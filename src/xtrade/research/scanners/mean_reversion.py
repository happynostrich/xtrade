"""Mean-reversion scanner — z-score envelope.

Long when the close rolls below a `-threshold` z-score (oversold) and
flat when it rolls back above 0 (mean recovered).

Params:
  - `lookback`  (int)   — rolling window for mean/std (default grid: 20, 50).
  - `threshold` (float) — z-score trigger for entries (default grid: 1.0, 2.0).

The implementation is pure pandas — vectorbt's role for this scanner is
limited to the downstream `Portfolio.from_signals` evaluation in grid
search, so we keep the indicator math standard pandas for readability.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from xtrade.research.scanners.base import Scanner, register_scanner


@register_scanner
class MeanReversionScanner(Scanner):
    name = "mean_reversion"

    @classmethod
    def default_param_grid(cls) -> dict[str, list[Any]]:
        return {"lookback": [20, 50], "threshold": [1.0, 2.0]}

    def compute_signals(
        self, panel: pd.DataFrame, params: dict[str, Any]
    ) -> tuple[pd.DataFrame, pd.DataFrame]:
        lookback = int(params.get("lookback", 20))
        threshold = float(params.get("threshold", 1.0))
        if lookback <= 1:
            raise ValueError(f"lookback must be > 1, got {lookback}")
        if threshold <= 0:
            raise ValueError(f"threshold must be > 0, got {threshold}")

        mean = panel.rolling(lookback, min_periods=lookback).mean()
        std = panel.rolling(lookback, min_periods=lookback).std()
        # Guard against zero-variance windows (constant series in tests).
        std_safe = std.replace(0.0, np.nan)
        zscore = (panel - mean) / std_safe

        # Edge transitions: enter when crossing below -threshold; exit when
        # crossing above 0. Using `& ~prev` ensures one True per state flip,
        # which matches Nautilus's bar-close event semantics.
        below = zscore < -threshold
        entries = below & ~below.shift(1, fill_value=False)
        above_zero = zscore > 0
        exits = above_zero & ~above_zero.shift(1, fill_value=False)
        return entries.fillna(False), exits.fillna(False)
