"""Tests for `xtrade.research.dataset.build` (Phase 5 / B2).

Covers:
  * `build_dataset` produces stable feature ordering matching FEATURE_NAMES.
  * Time-window splits are non-overlapping and reject leakage.
  * Sentiment carry-forward + 1h-lag alignment.
  * Empty sentiment fallback to zero with no exception.
  * Reproducibility: same inputs → same X/y bytes.
"""

from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd
import pytest

from xtrade.research.dataset import build_dataset
from xtrade.research.dataset.build import (
    FEATURE_NAMES,
    DatasetBundle,
)


UTC = dt.timezone.utc


def _synthetic_ohlcv(
    start: dt.datetime, n_minutes: int, seed: int = 7
) -> pd.DataFrame:
    """Minute-bar synthetic OHLCV with a deterministic random walk close."""
    rng = np.random.default_rng(seed)
    ts = pd.date_range(start=start, periods=n_minutes, freq="1min", tz="UTC")
    steps = rng.normal(loc=0.0, scale=0.0005, size=n_minutes)
    close = 50000.0 * np.exp(np.cumsum(steps))
    df = pd.DataFrame({"ts": ts.view("int64"), "close": close})
    return df


def _synthetic_sentiment(start: dt.datetime, n_rows: int = 8) -> pd.DataFrame:
    rows = []
    for i in range(n_rows):
        ts = start + dt.timedelta(minutes=i * 30)
        rows.append(
            {
                "ts": pd.Timestamp(ts).value,
                "instrument": "BTCUSDT-PERP.BINANCE",
                "source": "rss-fake",
                "scorer": "vader",
                "raw_score": float((-1) ** i) * 0.2,
                "normalized_score": float((-1) ** i) * 0.2,
                "n_articles": 1,
            }
        )
    return pd.DataFrame(rows)


def _windows(start: dt.datetime, n_minutes: int):
    bar_end = start + dt.timedelta(minutes=n_minutes)
    a = start + dt.timedelta(minutes=60)  # leave warmup for rolling
    b = a + dt.timedelta(minutes=(n_minutes - 60) // 3)
    c = b + dt.timedelta(minutes=(n_minutes - 60) // 3)
    return (a, b), (b, c), (c, bar_end)


def test_build_dataset_feature_order_locked() -> None:
    start = dt.datetime(2026, 5, 1, tzinfo=UTC)
    ohlcv = _synthetic_ohlcv(start, n_minutes=600)
    sent = _synthetic_sentiment(start)
    tw, vw, sw = _windows(start, n_minutes=600)
    bundle = build_dataset(
        "BTCUSDT-PERP.BINANCE",
        ohlcv=ohlcv,
        sentiment=sent,
        horizon_min=15,
        train_window=tw,
        val_window=vw,
        test_window=sw,
    )
    assert isinstance(bundle, DatasetBundle)
    assert bundle.feature_names == FEATURE_NAMES
    assert tuple(bundle.X_train.columns) == FEATURE_NAMES
    assert tuple(bundle.X_val.columns) == FEATURE_NAMES
    assert tuple(bundle.X_test.columns) == FEATURE_NAMES
    assert bundle.instrument == "BTCUSDT-PERP.BINANCE"
    assert bundle.target_horizon_min == 15
    assert bundle.label_mode == "classification"
    assert bundle.y_train.dtype == np.int8
    assert bundle.y_val.dtype == np.int8
    assert bundle.y_test.dtype == np.int8


def test_build_dataset_splits_are_non_overlapping() -> None:
    start = dt.datetime(2026, 5, 1, tzinfo=UTC)
    ohlcv = _synthetic_ohlcv(start, n_minutes=600)
    sent = _synthetic_sentiment(start)
    tw, vw, sw = _windows(start, n_minutes=600)
    b = build_dataset(
        "BTCUSDT-PERP.BINANCE",
        ohlcv=ohlcv,
        sentiment=sent,
        train_window=tw,
        val_window=vw,
        test_window=sw,
    )
    # No timestamp should appear in two splits.
    s_train = set(b.index_train)
    s_val = set(b.index_val)
    s_test = set(b.index_test)
    assert not (s_train & s_val)
    assert not (s_val & s_test)
    assert not (s_train & s_test)
    # Order: last train ts < first val ts < last val ts < first test ts.
    if b.index_train.size and b.index_val.size:
        assert b.index_train.max() < b.index_val.min()
    if b.index_val.size and b.index_test.size:
        assert b.index_val.max() < b.index_test.min()


def test_build_dataset_rejects_leaking_split() -> None:
    start = dt.datetime(2026, 5, 1, tzinfo=UTC)
    ohlcv = _synthetic_ohlcv(start, n_minutes=600)
    # train.end > val.start → leak
    train_end = start + dt.timedelta(minutes=300)
    val_start = start + dt.timedelta(minutes=200)
    val_end = start + dt.timedelta(minutes=400)
    test_start = start + dt.timedelta(minutes=400)
    test_end = start + dt.timedelta(minutes=600)
    with pytest.raises(ValueError, match="split leakage"):
        build_dataset(
            "BTCUSDT-PERP.BINANCE",
            ohlcv=ohlcv,
            train_window=(start + dt.timedelta(minutes=60), train_end),
            val_window=(val_start, val_end),
            test_window=(test_start, test_end),
        )


def test_build_dataset_naive_window_rejected() -> None:
    start = dt.datetime(2026, 5, 1, tzinfo=UTC)
    ohlcv = _synthetic_ohlcv(start, n_minutes=200)
    with pytest.raises(ValueError, match="tz-aware"):
        build_dataset(
            "BTCUSDT-PERP.BINANCE",
            ohlcv=ohlcv,
            train_window=(dt.datetime(2026, 5, 1, 1), dt.datetime(2026, 5, 1, 2)),
            val_window=(dt.datetime(2026, 5, 1, 2), dt.datetime(2026, 5, 1, 3)),
            test_window=(dt.datetime(2026, 5, 1, 3), dt.datetime(2026, 5, 1, 4)),
        )


def test_build_dataset_empty_sentiment_falls_back_to_zero() -> None:
    start = dt.datetime(2026, 5, 1, tzinfo=UTC)
    ohlcv = _synthetic_ohlcv(start, n_minutes=600)
    tw, vw, sw = _windows(start, n_minutes=600)
    b = build_dataset(
        "BTCUSDT-PERP.BINANCE",
        ohlcv=ohlcv,
        # No sentiment passed → empty fallback.
        train_window=tw,
        val_window=vw,
        test_window=sw,
    )
    assert (b.X_train["sentiment_score"] == 0.0).all()
    assert (b.X_train["sentiment_score_lag_1h"] == 0.0).all()


def test_build_dataset_reproducible() -> None:
    start = dt.datetime(2026, 5, 1, tzinfo=UTC)
    ohlcv = _synthetic_ohlcv(start, n_minutes=600)
    sent = _synthetic_sentiment(start)
    tw, vw, sw = _windows(start, n_minutes=600)
    a = build_dataset(
        "BTCUSDT-PERP.BINANCE",
        ohlcv=ohlcv,
        sentiment=sent,
        train_window=tw,
        val_window=vw,
        test_window=sw,
    )
    b = build_dataset(
        "BTCUSDT-PERP.BINANCE",
        ohlcv=ohlcv,
        sentiment=sent,
        train_window=tw,
        val_window=vw,
        test_window=sw,
    )
    pd.testing.assert_frame_equal(a.X_train, b.X_train)
    pd.testing.assert_series_equal(a.y_train, b.y_train)
    pd.testing.assert_frame_equal(a.X_val, b.X_val)
    pd.testing.assert_series_equal(a.y_val, b.y_val)
    pd.testing.assert_frame_equal(a.X_test, b.X_test)
    pd.testing.assert_series_equal(a.y_test, b.y_test)


def test_dataset_bundle_to_meta_schema() -> None:
    start = dt.datetime(2026, 5, 1, tzinfo=UTC)
    ohlcv = _synthetic_ohlcv(start, n_minutes=400)
    tw, vw, sw = _windows(start, n_minutes=400)
    b = build_dataset(
        "BTCUSDT-PERP.BINANCE",
        ohlcv=ohlcv,
        horizon_min=10,
        train_window=tw,
        val_window=vw,
        test_window=sw,
    )
    meta = b.to_meta()
    assert meta["instrument"] == "BTCUSDT-PERP.BINANCE"
    assert meta["feature_names"] == list(FEATURE_NAMES)
    assert meta["target_horizon_min"] == 10
    assert meta["label_mode"] == "classification"
    assert set(meta["n_samples"]) == {"train", "val", "test"}
    assert isinstance(meta["train_window"][0], str)
