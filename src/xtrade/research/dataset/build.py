"""Feature & label dataset assembly (Phase 5 / B2).

Pipeline
--------
1. Load OHLCV for `instrument` from a Parquet directory (or accept a
   DataFrame directly — the unit tests use the DataFrame mode).
2. Load sentiment Parquet for the same instrument from the directory
   layout written by `xtrade.research.news.build_sentiment_features`.
   Missing sentiment is NOT fatal — the relevant features fall back to
   ``0.0`` with one logged warning.
3. Compute features::

       ret_5m, ret_15m, ret_60m            # log returns over rolling windows
       vol_15m, vol_60m                    # rolling std of 1-min returns
       sentiment_score                     # latest sentiment_score at bar ts (carry-forward)
       sentiment_score_lag_1h              # sentiment value 1h before bar ts

4. Compute the label::

       fwd_ret_<horizon>m                  # close[t+H] / close[t] - 1
       fwd_dir_<horizon>m                  # sign(fwd_ret) (binary: 1 if > 0 else 0)

5. Time-window split: ``train_window``, ``val_window``, ``test_window``
   are three non-overlapping ``(start, end)`` UTC datetime tuples. They
   MUST satisfy ``train.end <= val.start <= val.end <= test.start <=
   test.end`` (strict monotonicity check; equality at the boundary is
   allowed since slicing is half-open ``[start, end)``).

The bundle is **deterministic**: given the same inputs and seed-less
construction order (no shuffling), `build_dataset` is bit-stable.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import numpy as np
import pandas as pd

log = logging.getLogger("xtrade.research.dataset")

UTC = dt.timezone.utc

# Stable feature ordering (the trained model serializes this list into
# its metadata; the live ML gate cross-checks).
FEATURE_NAMES: tuple[str, ...] = (
    "ret_5m",
    "ret_15m",
    "ret_60m",
    "vol_15m",
    "vol_60m",
    "sentiment_score",
    "sentiment_score_lag_1h",
)


TimeWindow = tuple[dt.datetime, dt.datetime]
LabelMode = Literal["classification", "regression"]


@dataclass(frozen=True)
class DatasetBundle:
    """Train/val/test triple with stable column ordering & index tracking.

    Notes
    -----
    * `X_*` rows are aligned with `index_*` (a `pd.DatetimeIndex` of UTC
      timestamps), one row per bar.
    * `feature_names` is preserved in the same order as the columns of
      every `X_*`.
    * `instrument` is propagated so downstream `train.py` can write it
      into the model's `dataset_meta.json`.
    """

    instrument: str
    feature_names: tuple[str, ...]
    target_horizon_min: int
    label_mode: LabelMode
    X_train: pd.DataFrame
    y_train: pd.Series
    X_val: pd.DataFrame
    y_val: pd.Series
    X_test: pd.DataFrame
    y_test: pd.Series
    index_train: pd.DatetimeIndex
    index_val: pd.DatetimeIndex
    index_test: pd.DatetimeIndex
    train_window: TimeWindow
    val_window: TimeWindow
    test_window: TimeWindow

    def n_samples(self) -> dict[str, int]:
        return {
            "train": len(self.y_train),
            "val": len(self.y_val),
            "test": len(self.y_test),
        }

    def to_meta(self) -> dict:
        """Serialisable meta block embedded into `dataset_meta.json`."""
        return {
            "instrument": self.instrument,
            "feature_names": list(self.feature_names),
            "target_horizon_min": self.target_horizon_min,
            "label_mode": self.label_mode,
            "n_samples": self.n_samples(),
            "train_window": [self.train_window[0].isoformat(), self.train_window[1].isoformat()],
            "val_window": [self.val_window[0].isoformat(), self.val_window[1].isoformat()],
            "test_window": [self.test_window[0].isoformat(), self.test_window[1].isoformat()],
        }


# ---------------------------------------------------------------------------
# Public loaders
# ---------------------------------------------------------------------------


def load_ohlcv_dataframe(ohlcv_root: Path | str, instrument: str) -> pd.DataFrame:
    """Load minute-bar OHLCV Parquet for `instrument` from a per-instrument
    directory layout (``<root>/<instrument>/*.parquet``).

    Returned frame is sorted ascending by ts, with a tz-aware UTC
    `DatetimeIndex` named ``ts`` and at minimum a ``close`` column.
    """

    root = Path(ohlcv_root)
    inst_dir = root / instrument
    if not inst_dir.is_dir():
        raise FileNotFoundError(
            f"OHLCV directory missing for {instrument!r}: {inst_dir}"
        )
    shards = sorted(inst_dir.glob("*.parquet"))
    if not shards:
        raise FileNotFoundError(f"no parquet shards under {inst_dir}")
    frames = [pd.read_parquet(p) for p in shards]
    df = pd.concat(frames, ignore_index=True)
    return _normalise_ohlcv(df)


def load_sentiment_dataframe(sentiment_root: Path | str, instrument: str) -> pd.DataFrame:
    """Load all sentiment Parquet shards for `instrument` from the layout
    produced by `xtrade.research.news.build_sentiment_features`.

    Returns an empty DataFrame (with the canonical schema) if the
    directory is missing or empty. This is a soft fallback so a
    fresh project (no news pulled yet) can still build a dataset
    with sentiment features = 0.
    """

    root = Path(sentiment_root)
    inst_dir = root / instrument
    cols = ["ts", "instrument", "source", "scorer", "raw_score", "normalized_score", "n_articles"]
    if not inst_dir.is_dir():
        return pd.DataFrame({c: pd.Series(dtype=_sentiment_dtype(c)) for c in cols})
    shards = sorted(inst_dir.glob("*.parquet"))
    if not shards:
        return pd.DataFrame({c: pd.Series(dtype=_sentiment_dtype(c)) for c in cols})
    frames = [pd.read_parquet(p) for p in shards]
    df = pd.concat(frames, ignore_index=True)
    return df.sort_values("ts", kind="mergesort").reset_index(drop=True)


def _sentiment_dtype(col: str) -> str:
    return {
        "ts": "int64",
        "instrument": "string",
        "source": "string",
        "scorer": "string",
        "raw_score": "float64",
        "normalized_score": "float64",
        "n_articles": "int32",
    }[col]


# ---------------------------------------------------------------------------
# Core builder
# ---------------------------------------------------------------------------


def build_dataset(
    instrument: str,
    *,
    ohlcv: pd.DataFrame | None = None,
    sentiment: pd.DataFrame | None = None,
    ohlcv_root: Path | str | None = None,
    sentiment_root: Path | str | None = None,
    horizon_min: int = 15,
    train_window: TimeWindow,
    val_window: TimeWindow,
    test_window: TimeWindow,
    label_mode: LabelMode = "classification",
) -> DatasetBundle:
    """Assemble a `(features, label)` bundle with deterministic ordering
    and a strict no-leakage time split.

    Either pass `ohlcv` / `sentiment` DataFrames directly (tests), or
    supply `ohlcv_root` / `sentiment_root` to load from disk.
    """

    # 1. Load + validate inputs.
    if ohlcv is None:
        if ohlcv_root is None:
            raise ValueError("must provide either `ohlcv` or `ohlcv_root`")
        ohlcv = load_ohlcv_dataframe(ohlcv_root, instrument)
    else:
        ohlcv = _normalise_ohlcv(ohlcv)

    if sentiment is None:
        if sentiment_root is None:
            sentiment_df = pd.DataFrame(
                {c: pd.Series(dtype=_sentiment_dtype(c)) for c in
                 ["ts", "instrument", "source", "scorer", "raw_score",
                  "normalized_score", "n_articles"]}
            )
        else:
            sentiment_df = load_sentiment_dataframe(sentiment_root, instrument)
    else:
        sentiment_df = sentiment.copy()

    _validate_split(train_window, val_window, test_window)

    # 2. Build features.
    feat = _compute_features(ohlcv, sentiment_df)

    # 3. Build label.
    label_series = _compute_label(ohlcv, horizon_min=horizon_min, mode=label_mode)

    # 4. Align features and label by index, drop NaNs.
    aligned = feat.join(label_series.rename("__label__"), how="inner").dropna()
    if aligned.empty:
        raise ValueError(
            f"dataset for {instrument!r} is empty after feature + label NaN drop; "
            f"input OHLCV had {len(ohlcv)} rows"
        )

    X = aligned[list(FEATURE_NAMES)].astype("float64")
    y = aligned["__label__"]
    if label_mode == "classification":
        y = y.astype("int8")
    else:
        y = y.astype("float64")

    # 5. Time-split. Each window is half-open [start, end).
    X_train, y_train, idx_train = _slice(X, y, *train_window)
    X_val, y_val, idx_val = _slice(X, y, *val_window)
    X_test, y_test, idx_test = _slice(X, y, *test_window)

    for tag, frame in (("train", X_train), ("val", X_val), ("test", X_test)):
        if frame.empty:
            raise ValueError(
                f"{tag} window is empty after slicing; "
                f"check that the OHLCV range covers {tag}_window"
            )

    return DatasetBundle(
        instrument=instrument,
        feature_names=FEATURE_NAMES,
        target_horizon_min=horizon_min,
        label_mode=label_mode,
        X_train=X_train,
        y_train=y_train,
        X_val=X_val,
        y_val=y_val,
        X_test=X_test,
        y_test=y_test,
        index_train=idx_train,
        index_val=idx_val,
        index_test=idx_test,
        train_window=train_window,
        val_window=val_window,
        test_window=test_window,
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _normalise_ohlcv(df: pd.DataFrame) -> pd.DataFrame:
    """Normalise an OHLCV frame to ``DatetimeIndex name='ts' (UTC)`` with
    at least a ``close`` column. Accepts either a `ts` int64-ns column,
    a `ts` datetime column, or an existing DatetimeIndex.
    """

    if df.empty:
        raise ValueError("OHLCV input is empty")
    out = df.copy()
    if isinstance(out.index, pd.DatetimeIndex):
        if out.index.tz is None:
            out.index = out.index.tz_localize(UTC)
        out.index = out.index.tz_convert(UTC)
        out.index.name = "ts"
    elif "ts" in out.columns:
        col = out["ts"]
        if pd.api.types.is_integer_dtype(col):
            idx = pd.to_datetime(col, unit="ns", utc=True)
        else:
            idx = pd.to_datetime(col, utc=True)
        out = out.drop(columns=["ts"]).set_index(idx)
        out.index.name = "ts"
    else:
        raise ValueError(
            "OHLCV input must have a `ts` column or a DatetimeIndex; "
            f"got columns={list(df.columns)}"
        )
    if "close" not in out.columns:
        raise ValueError(f"OHLCV input must contain a `close` column; got {list(out.columns)}")
    return out.sort_index()


def _validate_split(
    train: TimeWindow, val: TimeWindow, test: TimeWindow
) -> None:
    for tag, w in (("train", train), ("val", val), ("test", test)):
        if w[1] <= w[0]:
            raise ValueError(f"{tag}_window: end must be > start, got {w}")
        if w[0].tzinfo is None or w[1].tzinfo is None:
            raise ValueError(f"{tag}_window endpoints must be tz-aware (UTC)")
    if not (train[1] <= val[0]):
        raise ValueError(
            f"split leakage: train_window.end ({train[1]}) > val_window.start ({val[0]})"
        )
    if not (val[1] <= test[0]):
        raise ValueError(
            f"split leakage: val_window.end ({val[1]}) > test_window.start ({test[0]})"
        )


def _compute_features(ohlcv: pd.DataFrame, sentiment: pd.DataFrame) -> pd.DataFrame:
    """Build the feature frame. Index = `ohlcv.index` (UTC DatetimeIndex)."""

    close = ohlcv["close"].astype("float64")
    log_close = np.log(close)
    one_min_ret = log_close.diff()

    features = pd.DataFrame(index=ohlcv.index)
    features["ret_5m"] = log_close - log_close.shift(5)
    features["ret_15m"] = log_close - log_close.shift(15)
    features["ret_60m"] = log_close - log_close.shift(60)
    features["vol_15m"] = one_min_ret.rolling(15).std()
    features["vol_60m"] = one_min_ret.rolling(60).std()

    # Sentiment alignment.
    sent_series = _carry_forward_sentiment(sentiment, ohlcv.index)
    sent_lag = sent_series.shift(60)  # ~1h lag on minute bars
    if sentiment.empty:
        log.warning(
            "no sentiment rows available; sentiment_score features set to 0 "
            "(this is a soft fallback — train metrics will reflect a content-less feature)"
        )
    features["sentiment_score"] = sent_series.fillna(0.0).astype("float64")
    features["sentiment_score_lag_1h"] = sent_lag.fillna(0.0).astype("float64")

    return features[list(FEATURE_NAMES)]


def _carry_forward_sentiment(sentiment: pd.DataFrame, bar_index: pd.DatetimeIndex) -> pd.Series:
    """Reindex sentiment scores onto `bar_index` with forward-fill.

    The sentiment DataFrame's `ts` is int64 nanoseconds (per the B1
    Parquet schema). We collapse same-ts duplicates by taking the mean
    `normalized_score`.
    """

    if sentiment.empty:
        return pd.Series(np.nan, index=bar_index, name="sentiment_score")
    sent = sentiment.copy()
    sent["ts_dt"] = pd.to_datetime(sent["ts"], unit="ns", utc=True)
    agg = sent.groupby("ts_dt", sort=True)["normalized_score"].mean()
    aligned = agg.reindex(bar_index.union(agg.index)).sort_index().ffill()
    return aligned.reindex(bar_index)


def _compute_label(ohlcv: pd.DataFrame, *, horizon_min: int, mode: LabelMode) -> pd.Series:
    close = ohlcv["close"].astype("float64")
    fwd = close.shift(-horizon_min) / close - 1.0
    if mode == "classification":
        return (fwd > 0).astype("int8").where(fwd.notna())
    return fwd


def _slice(
    X: pd.DataFrame, y: pd.Series, start: dt.datetime, end: dt.datetime
) -> tuple[pd.DataFrame, pd.Series, pd.DatetimeIndex]:
    mask = (X.index >= start) & (X.index < end)
    Xw = X.loc[mask].copy()
    yw = y.loc[mask].copy()
    return Xw, yw, Xw.index
