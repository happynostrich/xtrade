"""Tests for the strategy-side ML gate (Phase 5 / B4).

The gate sits inside `MomentumFollow._apply_ml_gate` and consults a
trained classifier to suppress low-confidence openings. These tests
fake the model loader so we don't need a pickled estimator on disk.

Coverage:
  * Gate disabled (default) → behaviour identical to Phase 3.
  * Gate enabled, low bullish score → BUY suppressed + `strategy.ml_gate.suppressed`
    event emitted.
  * Reduce-only closes pass through the gate unchanged.
  * Wrong-direction model output (high p_long but SELL intent) → suppressed
    when `direction_check=True`.
  * `MLGateConfig` rejects out-of-range thresholds.
"""

from __future__ import annotations

import datetime as dt
import logging
from decimal import Decimal
from pathlib import Path

import pytest

from xtrade.research import ml_gate as ml_gate_module
from xtrade.research.ml_gate import GateDecision, MLGateConfig
from xtrade.research.signals import Signal
from xtrade.strategy.base import AccountSnapshot
from xtrade.strategy.plugins.momentum_follow import MomentumFollow


UTC = dt.timezone.utc
SYMBOL = "BTCUSDT-PERP.BINANCE"
FEATURE_NAMES = [
    "ret_5m",
    "ret_15m",
    "ret_60m",
    "vol_15m",
    "vol_60m",
    "sentiment_score",
    "sentiment_score_lag_1h",
]


class _FakeModel:
    """Stand-in classifier returning a fixed bullish probability."""

    def __init__(self, p_long: float) -> None:
        self.p_long = float(p_long)

    def predict_proba(self, X):  # noqa: N802 - sklearn API name
        import numpy as np

        n = X.shape[0]
        return np.tile([1.0 - self.p_long, self.p_long], (n, 1))


@pytest.fixture
def patch_loader(monkeypatch):
    """Returns a factory that installs a fake model loader on `ml_gate`."""

    def _install(p_long: float) -> None:
        def _fake(_path: Path):
            return _FakeModel(p_long), {"feature_names": list(FEATURE_NAMES)}

        monkeypatch.setattr(ml_gate_module, "_load_model_and_meta", _fake)

    return _install


def _signal(direction: str = "LONG", h: int = 10) -> Signal:
    return Signal(
        symbol=SYMBOL,
        venue="binance",
        direction=direction,  # type: ignore[arg-type]
        strength=0.5 if direction == "LONG" else (-0.5 if direction == "SHORT" else 0.0),
        generated_at=dt.datetime(2026, 5, 22, h, 0, 0, tzinfo=UTC),
        source="momentum:abc12345",
    )


def _account(*, position: str = "0", mark: str | None = "50000") -> AccountSnapshot:
    return AccountSnapshot(
        cash_usd=Decimal("100000"),
        positions={SYMBOL: Decimal(position)},
        mark_prices={SYMBOL: Decimal(mark)} if mark else {},
        nav_usd=Decimal("100000"),
        peak_nav_usd=Decimal("100000"),
    )


# ---- config ---------------------------------------------------------------


def test_ml_gate_config_threshold_range() -> None:
    with pytest.raises(ValueError):
        MLGateConfig(score_threshold=0.0)
    with pytest.raises(ValueError):
        MLGateConfig(score_threshold=1.0)
    with pytest.raises(ValueError):
        MLGateConfig(score_threshold=-0.1)


def test_ml_gate_config_enabled_requires_path() -> None:
    with pytest.raises(ValueError):
        MLGateConfig(enabled=True, model_path=None)


def test_ml_gate_config_from_mapping_defaults() -> None:
    c = MLGateConfig.from_mapping(None)
    assert c.enabled is False
    assert c.score_threshold == 0.55
    assert c.direction_check is True


def test_ml_gate_config_from_mapping_full(tmp_path: Path) -> None:
    raw = {
        "enabled": True,
        "model_path": str(tmp_path / "model.pkl"),
        "score_threshold": 0.7,
        "direction_check": False,
    }
    c = MLGateConfig.from_mapping(raw)
    assert c.enabled is True
    assert c.model_path == tmp_path / "model.pkl"
    assert c.score_threshold == 0.7
    assert c.direction_check is False


# ---- default-off behaviour ------------------------------------------------


def test_gate_disabled_by_default_behaves_like_phase3() -> None:
    # No ml_gate config at all.
    strat = MomentumFollow({"notional_usd": "500", "qty_step": "0.001"})
    intents = list(strat.on_signal(_signal("LONG"), _account(position="0")))
    assert len(intents) == 1
    assert intents[0].side == "BUY"


def test_gate_config_enabled_false_does_not_load_model() -> None:
    # enabled=False with a model_path string → MomentumFollow must NOT touch loader.
    strat = MomentumFollow(
        {
            "notional_usd": "500",
            "ml_gate": {"enabled": False, "model_path": "/nonexistent/model.pkl"},
        }
    )
    # If the loader had been invoked it would have crashed on missing file.
    intents = list(strat.on_signal(_signal("LONG"), _account(position="0")))
    assert len(intents) == 1


# ---- gate-on behaviour ----------------------------------------------------


def test_gate_low_score_suppresses_buy(patch_loader, caplog, tmp_path: Path) -> None:
    patch_loader(p_long=0.10)  # well below default 0.55
    strat = MomentumFollow(
        {
            "notional_usd": "500",
            "ml_gate": {
                "enabled": True,
                "model_path": str(tmp_path / "model.pkl"),
                "score_threshold": 0.55,
                "direction_check": True,
            },
        }
    )
    with caplog.at_level(logging.INFO, logger="xtrade.strategy.momentum_follow"):
        intents = list(strat.on_signal(_signal("LONG"), _account(position="0")))
    assert intents == []
    suppressed = [r for r in caplog.records if "ml_gate.suppressed" in r.getMessage()]
    assert suppressed, "expected one strategy.ml_gate.suppressed event"


def test_gate_high_score_allows_buy(patch_loader, tmp_path: Path) -> None:
    patch_loader(p_long=0.95)
    strat = MomentumFollow(
        {
            "notional_usd": "500",
            "ml_gate": {
                "enabled": True,
                "model_path": str(tmp_path / "model.pkl"),
                "score_threshold": 0.55,
                "direction_check": True,
            },
        }
    )
    intents = list(strat.on_signal(_signal("LONG"), _account(position="0")))
    assert len(intents) == 1
    assert intents[0].side == "BUY"
    assert not intents[0].reduce_only


def test_gate_direction_check_blocks_wrong_side(patch_loader, tmp_path: Path) -> None:
    # Model strongly bullish (p_long=0.9) but signal is SHORT — direction_check
    # should suppress the SELL.
    patch_loader(p_long=0.90)
    strat = MomentumFollow(
        {
            "notional_usd": "500",
            "ml_gate": {
                "enabled": True,
                "model_path": str(tmp_path / "model.pkl"),
                "score_threshold": 0.55,
                "direction_check": True,
            },
        }
    )
    intents = list(strat.on_signal(_signal("SHORT"), _account(position="0")))
    assert intents == []


def test_gate_reduce_only_close_passes_through(patch_loader, tmp_path: Path) -> None:
    # Even with the gate convinced this is a bullish setup (p_long=0.05 → BUY
    # gated out), closing an existing short must still emit the reduce-only BUY.
    patch_loader(p_long=0.05)
    strat = MomentumFollow(
        {
            "notional_usd": "500",
            "ml_gate": {
                "enabled": True,
                "model_path": str(tmp_path / "model.pkl"),
                "score_threshold": 0.55,
                "direction_check": True,
            },
        }
    )
    # Position is short → LONG signal triggers (a) BUY reduce_only close and
    # (b) BUY open. The open must be gated; the close must pass through.
    intents = list(strat.on_signal(_signal("LONG"), _account(position="-1")))
    assert len(intents) == 1
    assert intents[0].side == "BUY"
    assert intents[0].reduce_only is True


def test_gate_flat_signal_close_passes_through(patch_loader, tmp_path: Path) -> None:
    patch_loader(p_long=0.05)
    strat = MomentumFollow(
        {
            "notional_usd": "500",
            "ml_gate": {
                "enabled": True,
                "model_path": str(tmp_path / "model.pkl"),
                "score_threshold": 0.55,
                "direction_check": True,
            },
        }
    )
    intents = list(strat.on_signal(_signal("FLAT"), _account(position="1")))
    # FLAT on a long position → SELL reduce_only. Must NOT be gated.
    assert len(intents) == 1
    assert intents[0].side == "SELL"
    assert intents[0].reduce_only is True


# ---- decide() unit semantics ----------------------------------------------


def test_decide_unsupported_side_passes_through(patch_loader, tmp_path: Path) -> None:
    patch_loader(p_long=0.5)
    gate = ml_gate_module.MLGate(
        MLGateConfig(
            enabled=True,
            model_path=tmp_path / "model.pkl",
            score_threshold=0.55,
        )
    )
    decision = gate.decide(side="WAT", features={n: 0.0 for n in FEATURE_NAMES})
    assert isinstance(decision, GateDecision)
    assert decision.allow is True


def test_decide_no_direction_check_uses_p_long_for_sell(patch_loader, tmp_path: Path) -> None:
    patch_loader(p_long=0.99)
    gate = ml_gate_module.MLGate(
        MLGateConfig(
            enabled=True,
            model_path=tmp_path / "model.pkl",
            score_threshold=0.55,
            direction_check=False,
        )
    )
    # direction_check=False treats `p_long` as conviction regardless of side,
    # so a 0.99 p_long ALLOWS even a SELL intent.
    dec = gate.decide(side="SELL", features={n: 0.0 for n in FEATURE_NAMES})
    assert dec.allow is True
    assert dec.score == pytest.approx(0.99)


def test_decide_missing_features_warn_once(patch_loader, tmp_path: Path, caplog) -> None:
    patch_loader(p_long=0.6)
    gate = ml_gate_module.MLGate(
        MLGateConfig(
            enabled=True,
            model_path=tmp_path / "model.pkl",
            score_threshold=0.55,
        )
    )
    with caplog.at_level(logging.WARNING, logger="xtrade.research.ml_gate"):
        gate.decide(side="BUY", features={})  # everything missing
        gate.decide(side="BUY", features={})  # second call: no new warnings
    # One warning per feature, but only on the first call.
    msgs = [r.getMessage() for r in caplog.records if "missing" in r.getMessage()]
    assert len(msgs) == len(FEATURE_NAMES)
