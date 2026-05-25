"""Tests for `xtrade.research.train` (Phase 5 / B3).

Covers:
  * `run_training` writes the locked artefact set: model.pkl, metrics.json,
    feature_importance.csv, dataset_meta.json.
  * `metrics.json` has the expected key set (schema lock).
  * Deterministic `run_id` (same inputs + seed → same id).
  * `metrics.json` is byte-stable on rerun.
  * lightgbm path skipped cleanly when extra not installed.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("sklearn")

from xtrade.research.dataset import build_dataset
from xtrade.research.train import run_training


UTC = dt.timezone.utc


def _make_bundle(n_minutes: int = 600):
    start = dt.datetime(2026, 5, 1, tzinfo=UTC)
    rng = np.random.default_rng(11)
    ts = pd.date_range(start=start, periods=n_minutes, freq="1min", tz="UTC")
    # Inject a tiny exploitable autocorrelation so the classifier can score above 0.5.
    drift = rng.normal(0.0, 0.0003, size=n_minutes)
    drift[1:] += 0.5 * drift[:-1]
    close = 50000.0 * np.exp(np.cumsum(drift))
    ohlcv = pd.DataFrame({"ts": ts.view("int64"), "close": close})
    tw = (start + dt.timedelta(minutes=60), start + dt.timedelta(minutes=300))
    vw = (start + dt.timedelta(minutes=300), start + dt.timedelta(minutes=450))
    sw = (start + dt.timedelta(minutes=450), start + dt.timedelta(minutes=n_minutes))
    return build_dataset(
        "BTCUSDT-PERP.BINANCE",
        ohlcv=ohlcv,
        train_window=tw,
        val_window=vw,
        test_window=sw,
        horizon_min=15,
    )


METRICS_KEYS = {
    "run_id",
    "model_name",
    "seed",
    "params",
    "feature_names",
    "target_horizon_min",
    "label_mode",
    "n_train",
    "n_val",
    "n_test",
    "auc",
    "accuracy",
    "ic",
    "auc_test",
    "accuracy_test",
    "ic_test",
    "train_window",
    "val_window",
    "test_window",
    "created_at",
}


def test_run_training_logistic_writes_locked_artefacts(tmp_path: Path) -> None:
    bundle = _make_bundle()
    result = run_training(bundle, model_name="logistic", seed=7, out_root=tmp_path)
    run_dir = tmp_path / result.run_id
    assert (run_dir / "model.pkl").is_file()
    assert (run_dir / "metrics.json").is_file()
    assert (run_dir / "feature_importance.csv").is_file()
    assert (run_dir / "dataset_meta.json").is_file()

    # metrics.json key set lock.
    disk_metrics = json.loads((run_dir / "metrics.json").read_text())
    assert set(disk_metrics.keys()) == METRICS_KEYS
    assert disk_metrics["model_name"] == "logistic"
    assert disk_metrics["seed"] == 7
    assert disk_metrics["label_mode"] == "classification"
    assert disk_metrics["feature_names"] == list(bundle.feature_names)
    assert 0.0 <= disk_metrics["accuracy"] <= 1.0
    assert 0.0 <= disk_metrics["auc"] <= 1.0
    # Disk view has the placeholder timestamp so rerun is byte-stable.
    assert disk_metrics["created_at"] is None

    # In-memory metrics keep the real timestamp.
    assert isinstance(result.metrics["created_at"], str)
    assert result.metrics["created_at"] != "__placeholder__"

    # feature_importance.csv header lock.
    fi_text = (run_dir / "feature_importance.csv").read_text().splitlines()
    assert fi_text[0] == "feature,importance"
    assert len(fi_text) == 1 + len(bundle.feature_names)

    # dataset_meta.json matches bundle.to_meta() round-trip.
    meta_disk = json.loads((run_dir / "dataset_meta.json").read_text())
    assert meta_disk["instrument"] == bundle.instrument
    assert meta_disk["feature_names"] == list(bundle.feature_names)


def test_run_training_is_reproducible(tmp_path: Path) -> None:
    bundle = _make_bundle()
    a = run_training(bundle, model_name="logistic", seed=11, out_root=tmp_path / "a")
    b = run_training(bundle, model_name="logistic", seed=11, out_root=tmp_path / "b")
    assert a.run_id == b.run_id
    # Disk-persisted metrics.json byte-equal (placeholder for created_at).
    metrics_a = (tmp_path / "a" / a.run_id / "metrics.json").read_bytes()
    metrics_b = (tmp_path / "b" / b.run_id / "metrics.json").read_bytes()
    assert metrics_a == metrics_b


def test_run_training_run_id_changes_with_seed(tmp_path: Path) -> None:
    bundle = _make_bundle()
    a = run_training(bundle, model_name="logistic", seed=1, out_root=tmp_path)
    b = run_training(bundle, model_name="logistic", seed=2, out_root=tmp_path)
    assert a.run_id != b.run_id


def test_run_training_rejects_regression_bundle(tmp_path: Path) -> None:
    start = dt.datetime(2026, 5, 1, tzinfo=UTC)
    n = 300
    ts = pd.date_range(start=start, periods=n, freq="1min", tz="UTC")
    close = np.full(n, 100.0)
    ohlcv = pd.DataFrame({"ts": ts.view("int64"), "close": close})
    tw = (start + dt.timedelta(minutes=60), start + dt.timedelta(minutes=150))
    vw = (start + dt.timedelta(minutes=150), start + dt.timedelta(minutes=220))
    sw = (start + dt.timedelta(minutes=220), start + dt.timedelta(minutes=n))
    bundle = build_dataset(
        "BTCUSDT-PERP.BINANCE",
        ohlcv=ohlcv,
        train_window=tw,
        val_window=vw,
        test_window=sw,
        label_mode="regression",
    )
    with pytest.raises(ValueError, match="classification"):
        run_training(bundle, out_root=tmp_path)


def test_run_training_lightgbm_optional(tmp_path: Path) -> None:
    pytest.importorskip("lightgbm")
    bundle = _make_bundle()
    res = run_training(bundle, model_name="lightgbm", seed=3, out_root=tmp_path)
    metrics = json.loads((tmp_path / res.run_id / "metrics.json").read_text())
    assert metrics["model_name"] == "lightgbm"
    assert set(metrics.keys()) == METRICS_KEYS
