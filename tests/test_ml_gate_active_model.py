"""Tests for `MLGateConfig.use_active_model` + `resolve_model_path` (C3).

Coverage
--------
* `use_active_model=True` resolves model_path via `models/active.json`.
* `use_active_model=True` but no `active.json` → ValueError at resolve time.
* `use_active_model=False` (default) keeps Track B behaviour: literal
  `model_path` is honoured; absent `model_path` with `enabled=True` raises
  at config construction.
* `MomentumFollow` with `use_active_model=True` constructs successfully
  and loads the registered model when active.json points at it.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from xtrade.research import ml_gate as ml_gate_module
from xtrade.research.ml_gate import MLGateConfig
from xtrade.research.registry import promote
from xtrade.research.signals import Signal
from xtrade.strategy.base import AccountSnapshot
from xtrade.strategy.plugins.momentum_follow import MomentumFollow

from decimal import Decimal


UTC = dt.timezone.utc
NOW = dt.datetime(2026, 5, 25, 14, 30, 0, tzinfo=UTC)
SYMBOL = "BTCUSDT-PERP.BINANCE"
FEATURE_NAMES = [
    "ret_5m", "ret_15m", "ret_60m", "vol_15m", "vol_60m",
    "sentiment_score", "sentiment_score_lag_1h",
]


class _FakeModel:
    def __init__(self, p_long: float = 0.9) -> None:
        self.p_long = float(p_long)

    def predict_proba(self, X):  # noqa: N802
        import numpy as np

        n = X.shape[0]
        return np.tile([1.0 - self.p_long, self.p_long], (n, 1))


@pytest.fixture
def patch_loader(monkeypatch):
    def _install(p_long: float = 0.9) -> None:
        def _fake(path: Path):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"unused")
            return _FakeModel(p_long), {"feature_names": list(FEATURE_NAMES)}

        monkeypatch.setattr(ml_gate_module, "_load_model_and_meta", _fake)

    return _install


def _seed_promoted_model(models_root: Path, run_id: str = "abc12345") -> None:
    run_dir = models_root / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "model.pkl").write_bytes(b"unused-by-fake-loader")
    (run_dir / "metrics.json").write_text(json.dumps({"auc": 0.71}))
    (run_dir / "dataset_meta.json").write_text(
        json.dumps({"feature_names": list(FEATURE_NAMES)})
    )
    promote(run_id, models_root=models_root, promoted_by="t", now=NOW)


# ---- MLGateConfig --------------------------------------------------------


def test_config_use_active_model_skips_model_path_requirement() -> None:
    cfg = MLGateConfig(enabled=True, use_active_model=True)
    assert cfg.use_active_model is True
    assert cfg.model_path is None


def test_config_enabled_without_model_path_or_active_raises() -> None:
    with pytest.raises(ValueError, match="model_path"):
        MLGateConfig(enabled=True)


def test_config_from_mapping_parses_use_active_model(tmp_path: Path) -> None:
    cfg = MLGateConfig.from_mapping(
        {
            "enabled": True,
            "use_active_model": True,
            "models_root": str(tmp_path),
            "score_threshold": 0.6,
        }
    )
    assert cfg.use_active_model is True
    assert cfg.models_root == tmp_path
    assert cfg.score_threshold == 0.6


def test_resolve_model_path_legacy_returns_literal(tmp_path: Path) -> None:
    cfg = MLGateConfig(enabled=True, model_path=tmp_path / "m.pkl")
    assert cfg.resolve_model_path() == tmp_path / "m.pkl"


def test_resolve_model_path_raises_when_active_missing(tmp_path: Path) -> None:
    cfg = MLGateConfig(enabled=True, use_active_model=True, models_root=tmp_path)
    with pytest.raises(ValueError, match="active.json"):
        cfg.resolve_model_path()


def test_resolve_model_path_via_registry(tmp_path: Path) -> None:
    _seed_promoted_model(tmp_path)
    cfg = MLGateConfig(enabled=True, use_active_model=True, models_root=tmp_path)
    resolved = cfg.resolve_model_path()
    assert resolved == tmp_path / "abc12345" / "model.pkl"


# ---- MomentumFollow integration -----------------------------------------


def _signal_long() -> Signal:
    return Signal(
        symbol=SYMBOL,
        venue="binance",
        direction="LONG",
        strength=0.5,
        generated_at=NOW,
        source="momentum:abc12345",
    )


def _account_flat() -> AccountSnapshot:
    return AccountSnapshot(
        cash_usd=Decimal("100000"),
        positions={SYMBOL: Decimal("0")},
        mark_prices={SYMBOL: Decimal("50000")},
        nav_usd=Decimal("100000"),
        peak_nav_usd=Decimal("100000"),
    )


def test_momentum_follow_uses_active_model(patch_loader, tmp_path: Path) -> None:
    patch_loader(p_long=0.95)
    _seed_promoted_model(tmp_path)
    strat = MomentumFollow(
        {
            "notional_usd": "500",
            "ml_gate": {
                "enabled": True,
                "use_active_model": True,
                "models_root": str(tmp_path),
                "score_threshold": 0.55,
            },
        }
    )
    intents = list(strat.on_signal(_signal_long(), _account_flat()))
    assert len(intents) == 1
    assert intents[0].side == "BUY"


def test_momentum_follow_active_mode_fails_without_active(
    patch_loader, tmp_path: Path
) -> None:
    patch_loader(p_long=0.95)
    # No promote → active.json absent → construct should fail.
    with pytest.raises(ValueError, match="active.json"):
        MomentumFollow(
            {
                "notional_usd": "500",
                "ml_gate": {
                    "enabled": True,
                    "use_active_model": True,
                    "models_root": str(tmp_path),
                    "score_threshold": 0.55,
                },
            }
        )


def test_momentum_follow_legacy_model_path_still_works(
    patch_loader, tmp_path: Path
) -> None:
    """Track B yaml without use_active_model continues to function."""
    patch_loader(p_long=0.95)
    strat = MomentumFollow(
        {
            "notional_usd": "500",
            "ml_gate": {
                "enabled": True,
                "model_path": str(tmp_path / "legacy.pkl"),
                "score_threshold": 0.55,
            },
        }
    )
    intents = list(strat.on_signal(_signal_long(), _account_flat()))
    assert len(intents) == 1
