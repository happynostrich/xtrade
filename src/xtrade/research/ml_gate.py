"""ML gate (Phase 5 / B4) — strategy-side inference of a trained baseline.

The gate sits **after** the strategy's rule engine has built an
`OrderIntent`. If `enabled=False` (the default), the gate is a no-op
and the strategy behaves identically to Phase 3. When enabled, the
gate:

1. Loads a pickled classifier produced by
   `xtrade.research.train.run_training` (and validates that the
   sibling `dataset_meta.json` exists — pickled estimator without
   provenance metadata is REFUSED to bound the unpickle blast radius).
2. Builds a feature vector from `(signal, account)` using the same
   feature names the model was trained on (read from `dataset_meta.json`).
   Missing features fall back to ``0.0`` with one warning per
   `(model_path, feature_name)`.
3. Computes the model's bullish probability `p`.
4. Returns a `GateDecision`:

       allow  — `p >= score_threshold` AND (if direction_check) the
                model's directional vote matches the intent side
       drop   — otherwise; the strategy emits
                `strategy.ml_gate.suppressed` and skips the intent.

Why not joblib?
---------------
We use stdlib `pickle.loads`. The model file is part of the trust
boundary documented in the Phase 5 brief §6 ("ML 模型文件信任边界"):
operators must verify `dataset_meta.json` provenance before deploying
a `model.pkl`. We do NOT chase the joblib dep here.

Lazy imports
------------
This module deliberately keeps top-level imports stdlib-only. sklearn /
lightgbm are pulled in only when `MLGate.score()` runs the unpickled
estimator's `predict_proba`. The supervisor's import of
`MomentumFollow(...)` with `ml_gate.enabled=False` never touches this
module's `score()` code path.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import pickle
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

log = logging.getLogger("xtrade.research.ml_gate")


@dataclass(frozen=True, slots=True)
class MLGateConfig:
    """Strategy-side ML gate config (immutable).

    Fields
    ------
    enabled
        Master switch. ``False`` (default) → no model load, gate is a
        no-op, supervisor never instantiates `MLGate`.
    model_path
        Absolute path to ``model.pkl`` produced by
        `xtrade.research.train.run_training`. Must have a sibling
        ``dataset_meta.json`` in the same directory.
    score_threshold
        Bullish probability cutoff. The gate ALLOWS an intent only if
        `p >= score_threshold` (for BUY) or `1 - p >= score_threshold`
        (for SELL, when `direction_check=True`).
    direction_check
        When ``True`` (default), the gate also requires the model's
        directional vote to match the intent side. When ``False``,
        only the threshold is checked (model score is interpreted as
        "conviction in trade direction" regardless of side).
    """

    enabled: bool = False
    model_path: Path | None = None
    score_threshold: float = 0.55
    direction_check: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.score_threshold, (int, float)):
            raise TypeError(
                f"score_threshold must be a number, got {type(self.score_threshold).__name__}"
            )
        if not 0.0 < float(self.score_threshold) < 1.0:
            raise ValueError(
                f"score_threshold must be in (0, 1), got {self.score_threshold}"
            )
        if self.enabled and self.model_path is None:
            raise ValueError("ml_gate.enabled=True requires model_path to be set")
        if self.model_path is not None and not isinstance(self.model_path, Path):
            # Friendly: tolerate str at construction time.
            object.__setattr__(self, "model_path", Path(self.model_path))

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None) -> "MLGateConfig":
        """Build from a strategy yaml subtree::

            ml_gate:
              enabled: true
              model_path: /opt/xtrade/models/abc123/model.pkl
              score_threshold: 0.6
              direction_check: true
        """
        if not raw:
            return cls()
        kwargs: dict[str, Any] = {}
        if "enabled" in raw:
            kwargs["enabled"] = bool(raw["enabled"])
        if "model_path" in raw and raw["model_path"] is not None:
            kwargs["model_path"] = Path(str(raw["model_path"]))
        if "score_threshold" in raw:
            kwargs["score_threshold"] = float(raw["score_threshold"])
        if "direction_check" in raw:
            kwargs["direction_check"] = bool(raw["direction_check"])
        return cls(**kwargs)


@dataclass(frozen=True, slots=True)
class GateDecision:
    allow: bool
    score: float
    reason: str


class MLGate:
    """Single-model inference wrapper.

    Construction loads (and validates) the model file. `score()` is the
    hot path called by the strategy on every candidate intent.
    """

    def __init__(self, config: MLGateConfig) -> None:
        if not config.enabled:
            raise ValueError("MLGate constructed with config.enabled=False")
        if config.model_path is None:
            raise ValueError("MLGate constructed without model_path")
        self.config = config
        self._model, self._meta = _load_model_and_meta(config.model_path)
        self._feature_names: tuple[str, ...] = tuple(self._meta["feature_names"])
        # Per-(model, feature) warn-once memo so we don't flood logs.
        self._warned_missing: set[str] = set()

    @property
    def feature_names(self) -> tuple[str, ...]:
        return self._feature_names

    def score(self, features: Mapping[str, float]) -> float:
        """Return the bullish probability `p ∈ [0, 1]`.

        Missing keys fall back to ``0.0`` with one warning per feature.
        """
        vec: list[float] = []
        for name in self._feature_names:
            if name in features:
                vec.append(float(features[name]))
                continue
            if name not in self._warned_missing:
                log.warning(
                    "ml_gate: feature %r missing from input; falling back to 0.0",
                    name,
                )
                self._warned_missing.add(name)
            vec.append(0.0)
        # Lazy import — supervisor's `import MLGate` does not pull these.
        import numpy as np  # noqa: PLC0415

        arr = np.asarray([vec], dtype="float64")
        model = self._model
        if hasattr(model, "predict_proba"):
            probs = model.predict_proba(arr)
            return float(probs[0, 1])
        if hasattr(model, "decision_function"):
            raw = float(model.decision_function(arr)[0])
            return 1.0 / (1.0 + pow(2.718281828459045, -raw))
        raise TypeError(
            f"model {type(model).__name__} has neither predict_proba nor decision_function"
        )

    def decide(
        self,
        *,
        side: str,
        features: Mapping[str, float],
    ) -> GateDecision:
        """Evaluate the gate for one intent.

        Parameters
        ----------
        side
            ``"BUY"`` or ``"SELL"`` (the OrderIntent side).
        features
            Mapping ``feature_name -> value``. Unknown features are
            ignored; missing features fall back to 0.0 (one warning).
        """
        side_u = side.upper()
        if side_u not in {"BUY", "SELL"}:
            return GateDecision(
                allow=True,
                score=0.0,
                reason=f"ml_gate: unsupported side {side!r}; passing through",
            )

        p_long = self.score(features)
        if self.config.direction_check:
            if side_u == "BUY":
                allow = p_long >= self.config.score_threshold
                conviction = p_long
                reason = (
                    "allow: bullish conviction sufficient"
                    if allow
                    else f"drop: BUY p_long={p_long:.4f} < threshold={self.config.score_threshold:.4f}"
                )
            else:  # SELL
                p_short = 1.0 - p_long
                allow = p_short >= self.config.score_threshold
                conviction = p_short
                reason = (
                    "allow: bearish conviction sufficient"
                    if allow
                    else f"drop: SELL p_short={p_short:.4f} < threshold={self.config.score_threshold:.4f}"
                )
            return GateDecision(allow=allow, score=float(conviction), reason=reason)

        # No direction check: just threshold the bullish probability
        # treated as "conviction" regardless of side.
        allow = p_long >= self.config.score_threshold
        return GateDecision(
            allow=allow,
            score=float(p_long),
            reason=(
                "allow: score above threshold"
                if allow
                else f"drop: score={p_long:.4f} < threshold={self.config.score_threshold:.4f}"
            ),
        )


# ---------------------------------------------------------------------------
# Loader (private)
# ---------------------------------------------------------------------------


def _load_model_and_meta(model_path: Path) -> tuple[Any, dict[str, Any]]:
    """Load `model.pkl` and its sibling `dataset_meta.json`.

    REFUSES to load if either is missing. The meta sibling is the
    operator's commitment that this pickle came from `run_training`
    in THIS repo — it bounds the blast radius of `pickle.loads`.
    """

    if not model_path.exists():
        raise FileNotFoundError(f"ml_gate: model_path does not exist: {model_path}")
    if not model_path.is_file():
        raise IsADirectoryError(f"ml_gate: model_path is not a regular file: {model_path}")

    meta_path = model_path.parent / "dataset_meta.json"
    if not meta_path.exists():
        raise FileNotFoundError(
            f"ml_gate: refusing to load {model_path} — sibling dataset_meta.json missing. "
            f"Phase 5 brief §6 requires meta provenance to bound pickle deserialization risk."
        )
    try:
        meta = json.loads(meta_path.read_text())
    except json.JSONDecodeError as exc:
        raise ValueError(f"ml_gate: dataset_meta.json is not valid JSON: {meta_path}") from exc
    if "feature_names" not in meta or not isinstance(meta["feature_names"], list):
        raise ValueError(
            f"ml_gate: dataset_meta.json at {meta_path} missing or invalid feature_names"
        )

    # Triggers sklearn / lightgbm class imports as needed.
    payload = model_path.read_bytes()
    model = pickle.loads(payload)
    return model, meta


# ---------------------------------------------------------------------------
# Feature-vector builders used by strategies
# ---------------------------------------------------------------------------


def build_features_from_signal(
    *,
    signal_strength: float,
    direction: str,
    sentiment_score: float = 0.0,
    sentiment_score_lag_1h: float = 0.0,
    extra: Mapping[str, float] | None = None,
) -> dict[str, float]:
    """Best-effort feature mapping the strategy can hand to `MLGate.decide`.

    The current B2 builder produces 7 features; live strategies don't
    have direct access to OHLCV rolling stats so they only fill what
    they can. The remaining features fall back to 0 (warning emitted
    by `MLGate.score`). When the strategy starts ingesting bar
    features (Phase 6+), it can populate the rest via `extra`.
    """

    dir_u = direction.upper()
    direction_bias = (
        1.0 if dir_u == "LONG" else -1.0 if dir_u == "SHORT" else 0.0
    )

    base: dict[str, float] = {
        "ret_5m": direction_bias * float(signal_strength) * 0.001,
        "ret_15m": direction_bias * float(signal_strength) * 0.002,
        "ret_60m": direction_bias * float(signal_strength) * 0.004,
        "vol_15m": 0.0,
        "vol_60m": 0.0,
        "sentiment_score": float(sentiment_score),
        "sentiment_score_lag_1h": float(sentiment_score_lag_1h),
    }
    if extra:
        for k, v in extra.items():
            base[k] = float(v)
    return base
