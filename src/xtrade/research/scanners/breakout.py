"""Donchian-channel breakout scanner.

Long when close breaks above the prior-N-bar high; flat when it breaks
back below the prior-N-bar low. The `.shift(1)` on the channel bounds is
intentional — we only trade on information available *before* the
current bar's close, which keeps the result honest under vectorbt's
"signal at bar close, fill at next bar" execution model.

Params:
  - `lookback` (int) — channel window (default grid: 20, 50, 100).
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from xtrade.research.scanners.base import Scanner, register_scanner


@register_scanner
class BreakoutScanner(Scanner):
    name = "breakout"

    @classmethod
    def default_param_grid(cls) -> dict[str, list[Any]]:
        return {"lookback": [20, 50, 100]}

    def compute_signals(
        self, panel: pd.DataFrame, params: dict[str, Any]
    ) -> tuple[pd.DataFrame, pd.DataFrame]:
        lookback = int(params.get("lookback", 20))
        if lookback <= 1:
            raise ValueError(f"lookback must be > 1, got {lookback}")

        upper = panel.rolling(lookback, min_periods=lookback).max().shift(1)
        lower = panel.rolling(lookback, min_periods=lookback).min().shift(1)

        broke_up = panel > upper
        broke_down = panel < lower

        entries = broke_up & ~broke_up.shift(1, fill_value=False)
        exits = broke_down & ~broke_down.shift(1, fill_value=False)
        return entries.fillna(False), exits.fillna(False)
