"""Offline tests for `xtrade.research.gridsearch.run_grid` (Phase 2 / S4).

Synthetic sine-wave panel makes Sharpe / win-rate finite and signed in a
way we can assert without coupling to vectorbt's exact arithmetic.
"""

from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

from xtrade.research import MeanReversionScanner, MomentumScanner, run_grid


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _sine_panel(periods: int = 300, freq: str = "1min") -> pd.DataFrame:
    idx = pd.date_range("2024-01-01", periods=periods, freq=freq, tz="UTC")
    t = np.linspace(0, 6 * np.pi, periods)
    return pd.DataFrame(
        {
            "BTC": 100 + 10 * np.sin(t),
            "ETH": 50 + 5 * np.sin(t + 0.3),
        },
        index=idx,
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_run_grid_returns_expected_columns() -> None:
    panel = _sine_panel()
    df = run_grid(MomentumScanner(), panel, {"fast": [5], "slow": [20]})
    assert list(df.columns) == [
        "scanner", "params", "sharpe", "total_return", "win_rate", "n_trades"
    ]
    assert len(df) == 1
    assert df.loc[0, "scanner"] == "momentum"
    # params is a JSON string, key-sorted.
    assert json.loads(df.loc[0, "params"]) == {"fast": 5, "slow": 20}


def test_run_grid_default_param_grid_used_when_none() -> None:
    panel = _sine_panel()
    s = MomentumScanner()
    df_default = run_grid(s, panel)
    df_explicit = run_grid(s, panel, s.default_param_grid())
    # Same params combos → same shape.
    assert len(df_default) == len(df_explicit)
    assert set(df_default["params"]) == set(df_explicit["params"])


def test_run_grid_expands_cartesian_product() -> None:
    panel = _sine_panel()
    grid = {"fast": [5, 10], "slow": [20, 50]}  # 4 combos, all valid
    df = run_grid(MomentumScanner(), panel, grid)
    assert len(df) == 4


def test_run_grid_skips_invalid_combos() -> None:
    panel = _sine_panel()
    # fast=20, slow=10 is invalid (fast >= slow) → silently skipped.
    grid = {"fast": [5, 20], "slow": [10, 20]}  # 4 combos; only (5,10),(5,20) valid
    df = run_grid(MomentumScanner(), panel, grid)
    assert len(df) == 2
    valid_combos = {tuple(sorted(json.loads(p).items())) for p in df["params"]}
    assert valid_combos == {
        (("fast", 5), ("slow", 10)),
        (("fast", 5), ("slow", 20)),
    }


def test_run_grid_top_k_truncates() -> None:
    panel = _sine_panel()
    grid = {"fast": [5, 10], "slow": [20, 50]}
    df = run_grid(MomentumScanner(), panel, grid, top_k=2)
    assert len(df) == 2


# ---------------------------------------------------------------------------
# Sorting / scoring
# ---------------------------------------------------------------------------


def test_run_grid_default_sorted_by_sharpe_descending() -> None:
    panel = _sine_panel()
    df = run_grid(MomentumScanner(), panel, {"fast": [5, 10], "slow": [20, 50]})
    sharpes = df["sharpe"].tolist()
    assert sharpes == sorted(sharpes, reverse=True)


def test_run_grid_scoring_total_return() -> None:
    panel = _sine_panel()
    df = run_grid(
        MomentumScanner(),
        panel,
        {"fast": [5, 10], "slow": [20, 50]},
        scoring="total_return",
    )
    rets = df["total_return"].tolist()
    assert rets == sorted(rets, reverse=True)


def test_run_grid_robust_scoring_penalises_few_trades() -> None:
    """Robust = win_rate × sqrt(n_trades); a row with 0 trades sorts to bottom."""
    panel = _sine_panel()
    df = run_grid(
        MeanReversionScanner(),
        panel,
        {"lookback": [20, 50], "threshold": [1.0, 2.0]},
        scoring="robust",
    )
    # Just assert order is consistent with the robust score formula.
    score = df["win_rate"].fillna(-np.inf) * np.sqrt(df["n_trades"].clip(lower=0))
    assert score.tolist() == sorted(score.tolist(), reverse=True)


def test_run_grid_unknown_scoring_rejected() -> None:
    panel = _sine_panel()
    with pytest.raises(ValueError, match="unknown scoring"):
        run_grid(MomentumScanner(), panel, {"fast": [5], "slow": [20]}, scoring="alpha")


def test_run_grid_zero_top_k_rejected() -> None:
    panel = _sine_panel()
    with pytest.raises(ValueError, match="top_k must be > 0"):
        run_grid(MomentumScanner(), panel, {"fast": [5], "slow": [20]}, top_k=0)


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_run_grid_empty_panel_returns_empty() -> None:
    panel = pd.DataFrame(
        columns=["BTC"], index=pd.DatetimeIndex([], tz="UTC", name="ts_event")
    )
    df = run_grid(MomentumScanner(), panel, {"fast": [5], "slow": [20]})
    assert df.empty
    assert list(df.columns) == [
        "scanner", "params", "sharpe", "total_return", "win_rate", "n_trades"
    ]


def test_run_grid_all_invalid_combos_returns_empty() -> None:
    panel = _sine_panel()
    # fast=20, slow=10 / fast=30, slow=15 / fast=50, slow=20 — all invalid.
    grid = {"fast": [20, 30, 50], "slow": [10, 15, 20]}
    df = run_grid(MomentumScanner(), panel, grid)
    # Some combos may still be valid (e.g. fast=20, slow=20 not; fast=20, slow=15 not).
    # All combos here have fast >= slow → empty result.
    assert df.empty


def test_run_grid_rejects_empty_param_value_list() -> None:
    panel = _sine_panel()
    with pytest.raises(ValueError, match="non-empty list"):
        run_grid(MomentumScanner(), panel, {"fast": [], "slow": [20]})


def test_run_grid_rejects_empty_grid_with_no_default() -> None:
    """Explicit empty grid must be rejected even if scanner has a default."""
    panel = _sine_panel()
    with pytest.raises(ValueError, match="param_grid is empty"):
        run_grid(MomentumScanner(), panel, {})


def test_run_grid_params_json_is_stable() -> None:
    """`params` column is sorted-key JSON for round-trippability."""
    panel = _sine_panel()
    df = run_grid(MomentumScanner(), panel, {"slow": [20], "fast": [5]})
    assert df.loc[0, "params"] == '{"fast": 5, "slow": 20}'


def test_run_grid_metric_values_finite() -> None:
    """Sanity: returned Sharpe/return must be a real number, not NaN."""
    panel = _sine_panel()
    df = run_grid(MomentumScanner(), panel, {"fast": [5], "slow": [20]})
    assert np.isfinite(df.loc[0, "sharpe"])
    assert np.isfinite(df.loc[0, "total_return"])
    assert df.loc[0, "n_trades"] >= 0
