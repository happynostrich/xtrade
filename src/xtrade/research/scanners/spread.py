"""Pair-spread (statistical-arbitrage) scanner.

Builds a residual spread between the first two columns of the panel
(`A = panel.iloc[:, 0]`, `B = panel.iloc[:, 1]`) using a rolling
beta-neutral hedge ratio, z-scores the residual, and emits long-spread
entries when z drops below `-threshold` and flat exits when z rises
above 0.

Convention: the signal is *attributed to the first symbol* (the
"long-leg" of the spread). This keeps the Signal queue's one-row-per-
symbol contract intact; downstream consumers that want the matched
short leg can recover it from `metadata.params["short_leg"]` (set in
`metadata` during grid search if desired).

Params:
  - `lookback`  (int)   — rolling window for both beta and z-score
                          (default grid: 30, 60).
  - `threshold` (float) — z-score entry trigger (default grid: 1.5, 2.5).

A panel with fewer than 2 columns yields empty entries/exits panels
matched to the input shape, rather than raising — Phase 2 doesn't want
the scanner registry to be brittle when a universe shrinks to one symbol.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from xtrade.research.scanners.base import Scanner, register_scanner


@register_scanner
class SpreadScanner(Scanner):
    name = "spread"

    @classmethod
    def default_param_grid(cls) -> dict[str, list[Any]]:
        return {"lookback": [30, 60], "threshold": [1.5, 2.5]}

    def compute_signals(
        self, panel: pd.DataFrame, params: dict[str, Any]
    ) -> tuple[pd.DataFrame, pd.DataFrame]:
        lookback = int(params.get("lookback", 30))
        threshold = float(params.get("threshold", 1.5))
        if lookback <= 1:
            raise ValueError(f"lookback must be > 1, got {lookback}")
        if threshold <= 0:
            raise ValueError(f"threshold must be > 0, got {threshold}")

        entries = pd.DataFrame(
            False, index=panel.index, columns=panel.columns, dtype=bool
        )
        exits = pd.DataFrame(
            False, index=panel.index, columns=panel.columns, dtype=bool
        )
        if panel.shape[1] < 2:
            return entries, exits

        a = panel.iloc[:, 0]
        b = panel.iloc[:, 1]
        # Rolling beta = cov(a, b) / var(b). var(b) == 0 → NaN beta, NaN spread.
        cov = a.rolling(lookback, min_periods=lookback).cov(b)
        var = b.rolling(lookback, min_periods=lookback).var()
        beta = cov / var.replace(0.0, np.nan)
        spread = a - beta * b
        mean = spread.rolling(lookback, min_periods=lookback).mean()
        std = spread.rolling(lookback, min_periods=lookback).std().replace(0.0, np.nan)
        z = (spread - mean) / std

        below = z < -threshold
        above_zero = z > 0
        first_col = panel.columns[0]
        # Edge-trigger to a single bar.
        entries[first_col] = (below & ~below.shift(1, fill_value=False)).fillna(False)
        exits[first_col] = (above_zero & ~above_zero.shift(1, fill_value=False)).fillna(False)
        return entries, exits
