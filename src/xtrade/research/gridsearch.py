"""Grid search over scanner parameter spaces (Phase 2 Task 4 / S4).

`run_grid(scanner, panel, param_grid, scoring, top_k)` walks the Cartesian
product of `param_grid`, evaluates each combo with vectorbt's
`Portfolio.from_signals`, and returns a tidy DataFrame ranked by the
chosen scoring rule.

Metric aggregation across symbols
---------------------------------
vectorbt returns per-column metrics (one number per symbol). For a
single row-per-combo summary we collapse those vectors:

  - `sharpe`       mean across symbols (skipna)
  - `total_return` mean across symbols (skipna)
  - `win_rate`     mean across symbols (skipna)
  - `n_trades`     sum across symbols

Scoring rules
-------------
  - `"sharpe"`            — default, mean per-symbol Sharpe
  - `"total_return"`      — mean per-symbol total return
  - `"robust"`            — win_rate × sqrt(n_trades); penalises low-N rows
                            so a single lucky trade can't top the table

Failure modes
-------------
If a scanner rejects a particular param combo (e.g. MomentumScanner with
`fast >= slow`), we *skip* that row rather than aborting the whole grid.
That keeps grid definitions cheap: callers can dump a broad range of
candidates and let validation prune invalid corners.
"""

from __future__ import annotations

import itertools
import json
import math
from typing import Any

import numpy as np
import pandas as pd
import vectorbt as vbt

from xtrade.research.scanners.base import Scanner


_RESULT_COLUMNS: tuple[str, ...] = (
    "scanner",
    "params",
    "sharpe",
    "total_return",
    "win_rate",
    "n_trades",
)

_SCORING_RULES: frozenset[str] = frozenset({"sharpe", "total_return", "robust"})


def run_grid(
    scanner: Scanner,
    panel: pd.DataFrame,
    param_grid: dict[str, list[Any]] | None = None,
    *,
    scoring: str = "sharpe",
    top_k: int = 20,
    freq: str | None = None,
) -> pd.DataFrame:
    """Evaluate `scanner` over `param_grid` and return ranked results.

    Parameters
    ----------
    scanner
        Scanner instance (already constructed; no params on the instance).
    panel
        Multi-symbol close panel from `bars_to_panel`. UTC-indexed.
    param_grid
        Mapping of param-name → list of candidate values. If None, falls
        back to `scanner.default_param_grid()`.
    scoring
        Ranking rule. One of {"sharpe", "total_return", "robust"}.
    top_k
        Cap on number of returned rows.
    freq
        Bar frequency for vectorbt annualisation (e.g. "1min", "1h"). If
        None, inferred from `panel.index.inferred_freq` and falls back to
        "1min" when undecidable (mirrors vbt's lenient default).

    Returns
    -------
    pd.DataFrame
        Columns: `scanner, params, sharpe, total_return, win_rate,
        n_trades`. Sorted descending by `scoring`, head `top_k`.
        Empty if no combo produced any trades.
    """
    if scoring not in _SCORING_RULES:
        raise ValueError(
            f"unknown scoring rule {scoring!r}; choose from {sorted(_SCORING_RULES)}"
        )
    if top_k <= 0:
        raise ValueError(f"top_k must be > 0, got {top_k}")
    if panel.empty:
        return _empty_result()

    grid = param_grid if param_grid is not None else scanner.default_param_grid()
    if not grid:
        raise ValueError("param_grid is empty and scanner has no default grid")
    for k, v in grid.items():
        if not isinstance(v, list) or not v:
            raise ValueError(f"param_grid[{k!r}] must be a non-empty list, got {v!r}")

    inferred_freq = freq or panel.index.inferred_freq or "1min"

    keys = list(grid.keys())
    combos = list(itertools.product(*[grid[k] for k in keys]))

    rows: list[dict[str, Any]] = []
    for combo in combos:
        params = dict(zip(keys, combo))
        try:
            entries, exits = scanner.compute_signals(panel, params)
        except ValueError:
            # Invalid combo (e.g. fast >= slow). Skip silently — the grid
            # is allowed to over-specify; we just want the viable corner.
            continue

        pf = vbt.Portfolio.from_signals(
            close=panel,
            entries=entries,
            exits=exits,
            freq=inferred_freq,
        )

        sharpe = _to_scalar(pf.sharpe_ratio(), agg="mean")
        total_return = _to_scalar(pf.total_return(), agg="mean")
        win_rate = _safe_win_rate(pf)
        n_trades = _safe_n_trades(pf)

        rows.append(
            {
                "scanner": scanner.name,
                "params": json.dumps(params, sort_keys=True, default=str),
                "sharpe": float(sharpe) if sharpe is not None else float("nan"),
                "total_return": (
                    float(total_return) if total_return is not None else float("nan")
                ),
                "win_rate": float(win_rate) if win_rate is not None else float("nan"),
                "n_trades": int(n_trades),
            }
        )

    if not rows:
        return _empty_result()

    df = pd.DataFrame(rows, columns=list(_RESULT_COLUMNS))
    df = _sort_by_scoring(df, scoring)
    return df.head(top_k).reset_index(drop=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _empty_result() -> pd.DataFrame:
    return pd.DataFrame(columns=list(_RESULT_COLUMNS))


def _to_scalar(value: Any, *, agg: str = "mean") -> float | None:
    """Collapse a vectorbt per-symbol Series to a scalar.

    Handles the case where vbt returns a Series (panel input) or a
    scalar (single-column input) transparently.
    """
    if isinstance(value, pd.Series):
        if value.empty:
            return None
        if agg == "mean":
            return value.mean(skipna=True)
        if agg == "sum":
            return value.sum(skipna=True)
        raise ValueError(f"unknown agg {agg!r}")
    if value is None:
        return None
    # Bare scalar.
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_win_rate(pf: Any) -> float | None:
    """Win rate aggregated across symbols, mean of per-symbol rates.

    vectorbt raises if no trades occurred; we map that to NaN.
    """
    try:
        wr = pf.trades.win_rate()
    except Exception:
        return None
    return _to_scalar(wr, agg="mean")


def _safe_n_trades(pf: Any) -> int:
    """Total trade count summed across symbols. 0 if vbt can't compute."""
    try:
        count = pf.trades.count()
    except Exception:
        return 0
    if isinstance(count, pd.Series):
        total = count.sum(skipna=True)
    else:
        try:
            total = float(count)
        except (TypeError, ValueError):
            return 0
    if math.isnan(total):
        return 0
    return int(total)


def _sort_by_scoring(df: pd.DataFrame, scoring: str) -> pd.DataFrame:
    """Stable sort `df` descending by `scoring`. NaN → bottom."""
    if scoring == "robust":
        # win_rate × sqrt(n_trades). NaN win_rate → -inf so it sorts last.
        score = df["win_rate"].fillna(-np.inf) * np.sqrt(df["n_trades"].clip(lower=0))
    else:
        score = df[scoring]
    sort_key = score.fillna(-np.inf)
    order = sort_key.sort_values(ascending=False, kind="mergesort").index
    return df.loc[order]
