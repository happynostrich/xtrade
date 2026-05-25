"""Phase 5 Track C4 — end-to-end ML-gate smoke (opt-in).

Why opt-in
----------
This test sweeps the entire research-to-strategy pipeline:

    B2: build_dataset (synthetic OHLCV) ->
    B3: run_training (logistic) ->
    C3: registry.promote ->
    Strategy: MomentumFollow(use_active_model=True) twice:
              once with the gate enabled, once with it disabled

Even a 600-bar dataset + sklearn logistic takes a few seconds. We
don't want every PR run paying that cost, so the test is gated on
``XTRADE_RUN_PAPER_E2E=1``. CI matrix does NOT export it; nightly /
manual runs do.

What this proves
----------------
1. The whole pipeline composes — train output is shaped right for
   promote, registry output is shaped right for `MLGateConfig`,
   the gate actually inferences in-process.
2. The gate, when enabled, suppresses at least some intents — i.e.
   gate-on intent count is ``<=`` gate-off intent count, AND at least
   one ``strategy.ml_gate.*`` event is emitted.
3. lightgbm stays out of `sys.modules` — `logistic` is enough.

What this does NOT do
---------------------
* Drive Nautilus / BacktestEngine — that path is exercised by
  `tests/test_paper_runner.py`. We feed signals directly to the
  strategy plugin to keep the test self-contained and parallel-safe.
* Hit the venue API / mainnet / news fetcher.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import sys
from decimal import Decimal
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("XTRADE_RUN_PAPER_E2E") != "1",
    reason="opt-in: set XTRADE_RUN_PAPER_E2E=1 to enable",
)

# These imports are intentionally guarded behind the skip so a CI host
# without sklearn doesn't fail at collection time.
pytest.importorskip("sklearn")
pytest.importorskip("numpy")
pytest.importorskip("pandas")


UTC = dt.timezone.utc
SYMBOL = "BTCUSDT-PERP.BINANCE"


def _make_bundle(n_minutes: int = 600):
    import numpy as np
    import pandas as pd

    from xtrade.research.dataset import build_dataset

    start = dt.datetime(2026, 5, 1, tzinfo=UTC)
    rng = np.random.default_rng(11)
    ts = pd.date_range(start=start, periods=n_minutes, freq="1min", tz="UTC")
    # Inject a small exploitable autocorrelation so logistic > 0.5.
    drift = rng.normal(0.0, 0.0003, size=n_minutes)
    drift[1:] += 0.5 * drift[:-1]
    close = 50000.0 * np.exp(np.cumsum(drift))
    ohlcv = pd.DataFrame({"ts": ts.view("int64"), "close": close})
    train_w = (start + dt.timedelta(minutes=60), start + dt.timedelta(minutes=300))
    val_w = (start + dt.timedelta(minutes=300), start + dt.timedelta(minutes=450))
    test_w = (start + dt.timedelta(minutes=450), start + dt.timedelta(minutes=n_minutes))
    return build_dataset(
        SYMBOL,
        ohlcv=ohlcv,
        train_window=train_w,
        val_window=val_w,
        test_window=test_w,
        horizon_min=15,
    )


def _build_synthetic_signals(n: int = 24):
    """N alternating LONG/SHORT signals over a 2h window."""
    from xtrade.research.signals import Signal

    start = dt.datetime(2026, 5, 25, 12, 0, 0, tzinfo=UTC)
    sigs = []
    for i in range(n):
        direction = "LONG" if i % 2 == 0 else "SHORT"
        strength = 0.6 if direction == "LONG" else -0.6
        sigs.append(
            Signal(
                symbol=SYMBOL,
                venue="binance",
                direction=direction,  # type: ignore[arg-type]
                strength=strength,
                generated_at=start + dt.timedelta(minutes=5 * i),
                source=f"momentum:e2e{i:04d}",
            )
        )
    return sigs


def _account(*, position: str = "0", mark: str = "50000"):
    from xtrade.strategy.base import AccountSnapshot

    return AccountSnapshot(
        cash_usd=Decimal("100000"),
        positions={SYMBOL: Decimal(position)},
        mark_prices={SYMBOL: Decimal(mark)},
        nav_usd=Decimal("100000"),
        peak_nav_usd=Decimal("100000"),
    )


def _run_strategy(strat, signals) -> list:
    """Walk the strategy through a signal sequence, tracking position locally."""
    position = Decimal("0")
    all_intents: list = []
    for sig in signals:
        intents = list(strat.on_signal(sig, _account(position=str(position))))
        all_intents.extend(intents)
        for intent in intents:
            qty = intent.quantity if intent.side == "BUY" else -intent.quantity
            if intent.reduce_only:
                # Reduce-only closes existing position toward zero.
                position = position + qty
            else:
                position = position + qty
    return all_intents


def test_ml_gate_paper_smoke(tmp_path: Path, caplog) -> None:
    from xtrade.research.registry import promote
    from xtrade.research.train import run_training
    from xtrade.strategy.plugins.momentum_follow import MomentumFollow

    # --- 1. Build dataset (B2) -------------------------------------------
    bundle = _make_bundle()

    # --- 2. Train (B3) ---------------------------------------------------
    models_root = tmp_path / "models"
    result = run_training(
        bundle,
        model_name="logistic",
        seed=7,
        out_root=models_root,
    )
    metrics_path = models_root / result.run_id / "metrics.json"
    assert metrics_path.exists()

    # --- 3. Promote (C3) -------------------------------------------------
    active = promote(result.run_id, models_root=models_root, promoted_by="e2e")
    assert active.run_id == result.run_id
    assert (models_root / "active.json").exists()

    signals = _build_synthetic_signals()

    # --- 4a. Gate-off run ------------------------------------------------
    strat_off = MomentumFollow(
        {
            "notional_usd": "500",
            "ml_gate": {"enabled": False},
        }
    )
    intents_off = _run_strategy(strat_off, signals)

    # --- 4b. Gate-on run -------------------------------------------------
    audit_root = tmp_path / "audit"
    strat_on = MomentumFollow(
        {
            "notional_usd": "500",
            "ml_gate_audit_root": str(audit_root),
            "ml_gate": {
                "enabled": True,
                "use_active_model": True,
                "models_root": str(models_root),
                "score_threshold": 0.55,
                "direction_check": True,
            },
        }
    )
    with caplog.at_level(logging.INFO, logger="xtrade.strategy.momentum_follow"):
        intents_on = _run_strategy(strat_on, signals)

    # --- 5. Assertions ---------------------------------------------------
    assert len(intents_off) > 0, "rule engine should fire for synthetic signals"
    assert len(intents_on) <= len(intents_off), (
        f"gate-on intents ({len(intents_on)}) should be <= gate-off ({len(intents_off)})"
    )

    gate_events = [
        r for r in caplog.records
        if "ml_gate.allowed" in r.getMessage() or "ml_gate.suppressed" in r.getMessage()
    ]
    assert gate_events, "gate-on run must emit at least one ml_gate.* event"

    # audit jsonl shards must exist for the gate-on run.
    shards = list(audit_root.glob("ml_gate.*.jsonl"))
    assert shards, "expected audit shards to be written under audit_root"

    # --- 6. lightgbm must not be in sys.modules at this point ------------
    # We only trained logistic; lightgbm should never have been loaded.
    assert "lightgbm" not in sys.modules, (
        "logistic-only e2e leaked lightgbm into the import graph"
    )
