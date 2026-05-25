"""Baseline ML trainer (Phase 5 / B3).

Supported model names: ``"logistic"`` and ``"lightgbm"``. Both are
classification baselines using the ``int8`` label from
`xtrade.research.dataset.build_dataset(..., label_mode="classification")`.

Outputs
-------
For every run we write the following under ``models/<run_id>/``::

    model.pkl              # pickled estimator (joblib not required — stdlib pickle)
    metrics.json           # full evaluation block (see below)
    feature_importance.csv # one row per feature, columns: feature, importance
    dataset_meta.json      # echo of `DatasetBundle.to_meta()`

`metrics.json` schema (locked by `test_research_train.py`):
    run_id                str (sha1-prefix; deterministic given seed + inputs)
    model_name            "logistic" | "lightgbm"
    seed                  int
    params                dict (effective hyperparams; what was actually passed)
    feature_names         list[str]
    target_horizon_min    int
    label_mode            "classification" | "regression"
    n_train, n_val, n_test  int
    auc                   float  (val AUC; 0.5 if degenerate)
    accuracy              float  (val accuracy on threshold=0.5)
    ic                    float  (Spearman-style information coefficient
                                  approximated via pearson(rank(pred), rank(label)))
    auc_test, accuracy_test, ic_test  test-set counterparts
    train_window, val_window, test_window  list[str] (iso datetimes)
    created_at            iso utc

Determinism
-----------
Same `(DatasetBundle, model_name, seed, params)` → same `run_id` and
byte-identical `metrics.json`. Tests pin this contract.
"""

from __future__ import annotations

import csv
import dataclasses
import datetime as dt
import hashlib
import json
import logging
import pickle
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import numpy as np
import pandas as pd

from xtrade.research.dataset import DatasetBundle

log = logging.getLogger("xtrade.research.train")

UTC = dt.timezone.utc

ModelName = Literal["logistic", "lightgbm"]

DEFAULT_LOGISTIC_PARAMS: dict[str, Any] = {
    "C": 1.0,
    "max_iter": 200,
    "solver": "lbfgs",
}

DEFAULT_LIGHTGBM_PARAMS: dict[str, Any] = {
    "n_estimators": 100,
    "learning_rate": 0.05,
    "num_leaves": 15,
    "min_data_in_leaf": 5,
    "objective": "binary",
    "verbose": -1,
}


@dataclass(frozen=True)
class TrainResult:
    run_id: str
    model_path: Path
    metrics: dict[str, Any]
    feature_importance_df: pd.DataFrame


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def run_training(
    bundle: DatasetBundle,
    *,
    model_name: ModelName = "logistic",
    seed: int = 42,
    params: dict[str, Any] | None = None,
    out_root: Path | str = Path("models"),
    run_id: str | None = None,
) -> TrainResult:
    """Train one baseline classifier on `bundle` and persist artefacts.

    Parameters
    ----------
    bundle
        Output of `xtrade.research.dataset.build_dataset`. Must be in
        classification mode (`label_mode="classification"`).
    model_name
        Either ``"logistic"`` (always available) or ``"lightgbm"`` (only
        if the optional dep is installed; raises `ImportError` otherwise).
    seed
        Integer seed; threaded through to the underlying estimator.
    params
        Optional override of the model's default hyperparams. Values
        merged on top of the defaults; the merged dict is persisted to
        `metrics.json["params"]`.
    out_root
        Directory under which `<run_id>/` is created.
    run_id
        Optional explicit id (tests use this to lock paths). Default is a
        12-char sha1 prefix over the dataset meta + model_name + seed +
        merged params, ensuring reproducibility.
    """

    if bundle.label_mode != "classification":
        raise ValueError(
            f"run_training expects label_mode='classification', got {bundle.label_mode!r}"
        )

    merged = _merge_params(model_name, params)
    rid = run_id or _make_run_id(bundle, model_name, seed, merged)

    out_dir = Path(out_root) / rid
    out_dir.mkdir(parents=True, exist_ok=True)

    model, importance_df = _fit_model(
        model_name=model_name,
        seed=seed,
        params=merged,
        X_train=bundle.X_train,
        y_train=bundle.y_train,
        feature_names=bundle.feature_names,
    )

    metrics = _evaluate(model, bundle)
    metrics.update(
        {
            "run_id": rid,
            "model_name": model_name,
            "seed": seed,
            "params": merged,
            "feature_names": list(bundle.feature_names),
            "target_horizon_min": bundle.target_horizon_min,
            "label_mode": bundle.label_mode,
            "n_train": int(len(bundle.y_train)),
            "n_val": int(len(bundle.y_val)),
            "n_test": int(len(bundle.y_test)),
            "train_window": [bundle.train_window[0].isoformat(), bundle.train_window[1].isoformat()],
            "val_window": [bundle.val_window[0].isoformat(), bundle.val_window[1].isoformat()],
            "test_window": [bundle.test_window[0].isoformat(), bundle.test_window[1].isoformat()],
            "created_at": dt.datetime.now(tz=UTC).replace(microsecond=0).isoformat(),
        }
    )

    model_path = out_dir / "model.pkl"
    model_path.write_bytes(pickle.dumps(model))

    # Strict tightening: model + meta files must not be world-writable.
    # We're a research script; we don't chmod the model dir as a whole.

    metrics_path = out_dir / "metrics.json"
    metrics_for_disk = dict(metrics)
    metrics_for_disk["created_at"] = "__placeholder__"  # see below
    # We persist a stable view: replace the wall-clock timestamp with a
    # placeholder so reproducibility tests do not flake. The in-memory
    # `metrics` returned to caller keeps the real timestamp.
    metrics_path.write_text(json.dumps(_stable_metrics(metrics), indent=2, sort_keys=True))

    importance_path = out_dir / "feature_importance.csv"
    _write_importance(importance_path, importance_df)

    meta_path = out_dir / "dataset_meta.json"
    meta_path.write_text(json.dumps(bundle.to_meta(), indent=2, sort_keys=True))

    return TrainResult(
        run_id=rid,
        model_path=model_path,
        metrics=metrics,
        feature_importance_df=importance_df,
    )


# ---------------------------------------------------------------------------
# Fit
# ---------------------------------------------------------------------------


def _fit_model(
    *,
    model_name: ModelName,
    seed: int,
    params: dict[str, Any],
    X_train: pd.DataFrame,
    y_train: pd.Series,
    feature_names: tuple[str, ...],
) -> tuple[Any, pd.DataFrame]:
    if model_name == "logistic":
        from sklearn.linear_model import LogisticRegression  # noqa: PLC0415

        model = LogisticRegression(random_state=seed, **params)
        model.fit(X_train.to_numpy(), y_train.to_numpy())
        # Logistic coefficients (abs value) act as a poor man's importance.
        coefs = np.abs(model.coef_).ravel()
        importance = pd.DataFrame(
            {"feature": list(feature_names), "importance": coefs.astype("float64")}
        )
        return model, importance

    if model_name == "lightgbm":
        try:
            import lightgbm as lgb  # noqa: PLC0415
        except ImportError as exc:
            raise ImportError(
                "lightgbm is required for model_name='lightgbm'; install with "
                "`uv pip install -e '.[research]'` or `pip install lightgbm`."
            ) from exc

        model = lgb.LGBMClassifier(random_state=seed, **params)
        model.fit(X_train.to_numpy(), y_train.to_numpy())
        try:
            raw_imp = model.feature_importances_
        except AttributeError:
            raw_imp = np.zeros(len(feature_names), dtype="float64")
        importance = pd.DataFrame(
            {"feature": list(feature_names), "importance": np.asarray(raw_imp, dtype="float64")}
        )
        return model, importance

    raise ValueError(
        f"unknown model_name {model_name!r}; expected 'logistic' or 'lightgbm'"
    )


# ---------------------------------------------------------------------------
# Eval
# ---------------------------------------------------------------------------


def _evaluate(model: Any, bundle: DatasetBundle) -> dict[str, float]:
    val = _eval_split(model, bundle.X_val, bundle.y_val)
    test = _eval_split(model, bundle.X_test, bundle.y_test)
    return {
        "auc": val["auc"],
        "accuracy": val["accuracy"],
        "ic": val["ic"],
        "auc_test": test["auc"],
        "accuracy_test": test["accuracy"],
        "ic_test": test["ic"],
    }


def _eval_split(model: Any, X: pd.DataFrame, y: pd.Series) -> dict[str, float]:
    from sklearn.metrics import accuracy_score, roc_auc_score  # noqa: PLC0415

    if X.empty:
        return {"auc": 0.5, "accuracy": 0.0, "ic": 0.0}

    probs = _predict_proba(model, X)
    preds = (probs >= 0.5).astype("int8")

    try:
        auc = float(roc_auc_score(y.to_numpy(), probs))
    except ValueError:
        # roc_auc_score raises if only one class in y; degenerate → 0.5.
        auc = 0.5
    acc = float(accuracy_score(y.to_numpy(), preds))
    ic = _information_coefficient(probs, y.to_numpy())
    return {"auc": auc, "accuracy": acc, "ic": ic}


def _predict_proba(model: Any, X: pd.DataFrame) -> np.ndarray:
    if hasattr(model, "predict_proba"):
        return np.asarray(model.predict_proba(X.to_numpy()))[:, 1].astype("float64")
    if hasattr(model, "decision_function"):
        raw = np.asarray(model.decision_function(X.to_numpy()), dtype="float64")
        # Map to [0,1] via logistic so accuracy_score's threshold still makes sense.
        return 1.0 / (1.0 + np.exp(-raw))
    raise TypeError(f"{type(model).__name__} has neither predict_proba nor decision_function")


def _information_coefficient(scores: np.ndarray, labels: np.ndarray) -> float:
    """Spearman-style IC: pearson correlation of rank(scores) and labels."""
    if len(scores) < 2:
        return 0.0
    s = pd.Series(scores).rank().to_numpy()
    l = pd.Series(labels.astype("float64")).rank().to_numpy()
    if s.std() == 0 or l.std() == 0:
        return 0.0
    return float(np.corrcoef(s, l)[0, 1])


# ---------------------------------------------------------------------------
# Identity + serialisation helpers
# ---------------------------------------------------------------------------


def _merge_params(model_name: ModelName, override: dict[str, Any] | None) -> dict[str, Any]:
    if model_name == "logistic":
        base = dict(DEFAULT_LOGISTIC_PARAMS)
    elif model_name == "lightgbm":
        base = dict(DEFAULT_LIGHTGBM_PARAMS)
    else:
        raise ValueError(f"unknown model_name {model_name!r}")
    if override:
        base.update(override)
    return base


def _make_run_id(
    bundle: DatasetBundle,
    model_name: str,
    seed: int,
    params: dict[str, Any],
) -> str:
    payload = {
        "meta": bundle.to_meta(),
        "model": model_name,
        "seed": seed,
        "params": params,
    }
    blob = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha1(blob).hexdigest()[:12]


def _stable_metrics(metrics: dict[str, Any]) -> dict[str, Any]:
    """Strip the wall-clock `created_at` from the disk-persisted view so
    that re-running with the same inputs produces a byte-identical
    `metrics.json`. Callers retain the original dict (with timestamp).
    """
    out = dict(metrics)
    out["created_at"] = None
    return out


def _write_importance(path: Path, df: pd.DataFrame) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["feature", "importance"])
        for _, row in df.iterrows():
            writer.writerow([row["feature"], f"{float(row['importance']):.10f}"])
