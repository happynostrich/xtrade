"""Offline tests for `xtrade.research.scanners` (Phase 2 Task 3 / S3).

Each scanner is exercised on a deterministic synthetic panel where we
know what the entries/exits should look like to within edge-trigger
timing. Tests stay close to the contract (shapes, dtypes, registry
behaviour) rather than to specific signal indices — that protects
against accidental indicator-tweak regressions while letting us prove
the wiring is sane.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from xtrade.research.scanners import (
    BreakoutScanner,
    MeanReversionScanner,
    MomentumScanner,
    Scanner,
    SpreadScanner,
    available_scanners,
    get_scanner,
    register_scanner,
)
from xtrade.research.scanners.base import params_hash


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sine_panel(periods: int = 300, freq: str = "1min") -> pd.DataFrame:
    """Two correlated sine waves — BTC large amplitude, ETH smaller / phase-shifted."""
    idx = pd.date_range("2024-01-01", periods=periods, freq=freq, tz="UTC")
    t = np.linspace(0, 6 * np.pi, periods)
    btc = pd.Series(100 + 10 * np.sin(t), index=idx, name="BTC")
    eth = pd.Series(50 + 5 * np.sin(t + 0.3), index=idx, name="ETH")
    return pd.concat([btc, eth], axis=1)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_registry_lists_all_four_scanners() -> None:
    assert set(available_scanners()) >= {"momentum", "mean_reversion", "breakout", "spread"}


def test_get_scanner_returns_class() -> None:
    cls = get_scanner("momentum")
    assert cls is MomentumScanner


def test_get_scanner_unknown_name() -> None:
    with pytest.raises(KeyError, match="unknown scanner"):
        get_scanner("does_not_exist")


def test_register_rejects_duplicate_name() -> None:
    """Re-registering the same name with a *different* class must fail."""

    class _Dummy(Scanner):
        name = "momentum"  # collides with the real MomentumScanner

        @classmethod
        def default_param_grid(cls):
            return {}

        def compute_signals(self, panel, params):
            raise NotImplementedError

    with pytest.raises(ValueError, match="already registered"):
        register_scanner(_Dummy)


def test_register_rejects_missing_name() -> None:
    class _NoName(Scanner):
        # name left as ""
        @classmethod
        def default_param_grid(cls):
            return {}

        def compute_signals(self, panel, params):
            raise NotImplementedError

    with pytest.raises(TypeError, match="non-empty string `name`"):
        register_scanner(_NoName)


# ---------------------------------------------------------------------------
# Per-scanner contract: shape, dtype, run() records
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "scanner_cls,params",
    [
        (MomentumScanner, {"fast": 5, "slow": 20}),
        (MeanReversionScanner, {"lookback": 20, "threshold": 1.0}),
        (BreakoutScanner, {"lookback": 20}),
        (SpreadScanner, {"lookback": 30, "threshold": 1.5}),
    ],
)
def test_compute_signals_matches_panel_shape(scanner_cls, params) -> None:
    panel = _sine_panel()
    s = scanner_cls()
    entries, exits = s.compute_signals(panel, params)
    assert entries.shape == panel.shape
    assert exits.shape == panel.shape
    assert (entries.dtypes == bool).all()
    assert (exits.dtypes == bool).all()
    assert entries.index.equals(panel.index)
    assert exits.index.equals(panel.index)
    assert list(entries.columns) == list(panel.columns)


@pytest.mark.parametrize(
    "scanner_cls,params",
    [
        (MomentumScanner, {"fast": 5, "slow": 20}),
        (MeanReversionScanner, {"lookback": 20, "threshold": 1.0}),
        (BreakoutScanner, {"lookback": 20}),
        (SpreadScanner, {"lookback": 30, "threshold": 1.5}),
    ],
)
def test_run_returns_long_format_records(scanner_cls, params) -> None:
    panel = _sine_panel()
    s = scanner_cls()
    records = s.run(panel, params)
    assert list(records.columns) == [
        "ts_event", "symbol", "direction", "strength", "source", "params"
    ]
    if not records.empty:
        assert set(records["direction"].unique()) <= {"LONG", "FLAT"}
        assert all(records["source"].str.startswith(f"{scanner_cls.name}:"))
        # ts_event monotonic non-decreasing.
        ts = records["ts_event"].tolist()
        assert ts == sorted(ts)


# ---------------------------------------------------------------------------
# Momentum-specific
# ---------------------------------------------------------------------------


def test_momentum_rejects_fast_ge_slow() -> None:
    panel = _sine_panel(periods=50)
    s = MomentumScanner()
    with pytest.raises(ValueError, match="must be <"):
        s.compute_signals(panel, {"fast": 20, "slow": 10})


def test_momentum_produces_some_signals_on_oscillator() -> None:
    """Sine-wave price guarantees MA cross-overs; sanity-check we see ≥1."""
    panel = _sine_panel(periods=300)
    s = MomentumScanner()
    entries, exits = s.compute_signals(panel, {"fast": 5, "slow": 20})
    assert entries.values.sum() > 0
    assert exits.values.sum() > 0


# ---------------------------------------------------------------------------
# Mean reversion specific
# ---------------------------------------------------------------------------


def test_mean_reversion_zero_variance_window_yields_no_signal() -> None:
    """A constant-price series has 0 std and must not crash; must emit no
    entries (z-score is undefined)."""
    idx = pd.date_range("2024-01-01", periods=200, freq="1min", tz="UTC")
    panel = pd.DataFrame({"BTC": 100.0}, index=idx)
    s = MeanReversionScanner()
    entries, exits = s.compute_signals(panel, {"lookback": 20, "threshold": 1.0})
    assert entries.values.sum() == 0
    assert exits.values.sum() == 0


def test_mean_reversion_rejects_bad_threshold() -> None:
    panel = _sine_panel(periods=100)
    s = MeanReversionScanner()
    with pytest.raises(ValueError):
        s.compute_signals(panel, {"lookback": 20, "threshold": 0.0})


# ---------------------------------------------------------------------------
# Breakout specific
# ---------------------------------------------------------------------------


def test_breakout_on_step_function_fires_after_lookback() -> None:
    """A monotonically rising series should break out continuously, but
    edge-trigger reduces it to a single fire then no more — useful sanity."""
    idx = pd.date_range("2024-01-01", periods=200, freq="1min", tz="UTC")
    rising = pd.Series(np.arange(200, dtype=float), index=idx, name="STEP")
    panel = rising.to_frame()
    s = BreakoutScanner()
    entries, exits = s.compute_signals(panel, {"lookback": 10})
    # First few bars (lookback warmup) have NaN bounds → False.
    assert entries.iloc[:10].values.sum() == 0
    # After warmup the rising series breaks the prior 10-bar high every bar
    # *before* edge-triggering; edge trigger keeps only the first fire.
    assert entries.values.sum() == 1


# ---------------------------------------------------------------------------
# Spread specific
# ---------------------------------------------------------------------------


def test_spread_one_symbol_panel_returns_empty() -> None:
    """A single-symbol panel can't form a pair — no signals, no crash."""
    idx = pd.date_range("2024-01-01", periods=200, freq="1min", tz="UTC")
    panel = pd.DataFrame({"BTC": np.sin(np.linspace(0, 6 * np.pi, 200))}, index=idx)
    s = SpreadScanner()
    entries, exits = s.compute_signals(panel, {"lookback": 30, "threshold": 1.5})
    assert entries.shape == panel.shape
    assert entries.values.sum() == 0
    assert exits.values.sum() == 0


def test_spread_attributes_signals_to_first_symbol() -> None:
    panel = _sine_panel(periods=300)
    s = SpreadScanner()
    entries, exits = s.compute_signals(panel, {"lookback": 30, "threshold": 1.5})
    # Spread scanner attributes to first column only.
    second_col = panel.columns[1]
    assert entries[second_col].values.sum() == 0
    assert exits[second_col].values.sum() == 0


# ---------------------------------------------------------------------------
# params_hash + source stamping
# ---------------------------------------------------------------------------


def test_params_hash_is_stable_and_short() -> None:
    a = params_hash({"fast": 5, "slow": 20})
    b = params_hash({"slow": 20, "fast": 5})  # different key order
    assert a == b
    assert len(a) == 8


def test_default_param_grid_returns_lists() -> None:
    for cls in (MomentumScanner, MeanReversionScanner, BreakoutScanner, SpreadScanner):
        grid = cls.default_param_grid()
        assert isinstance(grid, dict) and grid, f"{cls.__name__} default grid empty"
        for k, v in grid.items():
            assert isinstance(v, list) and v, f"{cls.__name__}.{k} grid not a non-empty list"
