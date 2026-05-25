"""Tests for `xtrade.strategy.ml_gate_audit` (Phase 5 / C2).

Coverage
--------
* `MLGateAuditWriter` writes one jsonl row per call, date-sharded.
* Atomic append (multiple writes accumulate in the same shard).
* Rejects unknown `kind`.
* `if_enabled(None)` returns None (no-op fallback).
* `MomentumFollow` integration:
  - audit writer not constructed when `ml_gate_audit_root` is absent
  - audit writer writes one row per allowed / suppressed decision
  - reduce-only closes do NOT generate audit rows (pass-through)
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from decimal import Decimal
from pathlib import Path

import pytest

from xtrade.research import ml_gate as ml_gate_module
from xtrade.research.signals import Signal
from xtrade.strategy.base import AccountSnapshot
from xtrade.strategy.ml_gate_audit import MLGateAuditWriter
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
    def __init__(self, p_long: float) -> None:
        self.p_long = float(p_long)

    def predict_proba(self, X):  # noqa: N802
        import numpy as np

        n = X.shape[0]
        return np.tile([1.0 - self.p_long, self.p_long], (n, 1))


@pytest.fixture
def patch_loader(monkeypatch):
    def _install(p_long: float) -> None:
        def _fake(path: Path):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"unused")
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


# ---- audit writer unit ----------------------------------------------------


def test_audit_writer_creates_dir_and_writes_one_row(tmp_path: Path) -> None:
    audit_root = tmp_path / "audit"
    w = MLGateAuditWriter(audit_root)
    w.write(
        kind="allowed",
        symbol="BTCUSDT-PERP",
        side="BUY",
        score=0.81,
        threshold=0.55,
        reason="allow: bullish conviction sufficient",
        source_signal_id="2026-05-22|BTCUSDT-PERP|momentum:x",
        ts=dt.datetime(2026, 5, 22, 10, 0, 0, tzinfo=UTC),
    )
    shard = audit_root / "ml_gate.2026-05-22.jsonl"
    assert shard.exists()
    rows = [json.loads(l) for l in shard.read_text().splitlines() if l.strip()]
    assert len(rows) == 1
    r = rows[0]
    assert r["kind"] == "allowed"
    assert r["symbol"] == "BTCUSDT-PERP"
    assert r["side"] == "BUY"
    assert r["score"] == 0.81
    assert r["threshold"] == 0.55
    assert r["ts"] == "2026-05-22T10:00:00Z"


def test_audit_writer_appends_atomically_across_calls(tmp_path: Path) -> None:
    w = MLGateAuditWriter(tmp_path / "audit")
    for i in range(20):
        w.write(
            kind="allowed" if i % 2 == 0 else "suppressed",
            symbol="ETHUSDT-PERP",
            side="BUY",
            score=0.5 + 0.01 * i,
            threshold=0.55,
            reason="r",
            source_signal_id=None,
            ts=dt.datetime(2026, 5, 22, 10, 0, i, tzinfo=UTC),
        )
    shard = (tmp_path / "audit" / "ml_gate.2026-05-22.jsonl")
    rows = [json.loads(l) for l in shard.read_text().splitlines() if l.strip()]
    assert len(rows) == 20
    assert sum(1 for r in rows if r["kind"] == "allowed") == 10
    assert sum(1 for r in rows if r["kind"] == "suppressed") == 10


def test_audit_writer_date_shards_by_utc(tmp_path: Path) -> None:
    w = MLGateAuditWriter(tmp_path / "audit")
    w.write(
        kind="allowed", symbol="A", side="BUY", score=0.6, threshold=0.55,
        reason="r", source_signal_id=None,
        ts=dt.datetime(2026, 5, 22, 23, 59, 59, tzinfo=UTC),
    )
    w.write(
        kind="suppressed", symbol="A", side="BUY", score=0.4, threshold=0.55,
        reason="r", source_signal_id=None,
        ts=dt.datetime(2026, 5, 23, 0, 0, 1, tzinfo=UTC),
    )
    assert (tmp_path / "audit" / "ml_gate.2026-05-22.jsonl").exists()
    assert (tmp_path / "audit" / "ml_gate.2026-05-23.jsonl").exists()


def test_audit_writer_rejects_bad_kind(tmp_path: Path) -> None:
    w = MLGateAuditWriter(tmp_path / "audit")
    with pytest.raises(ValueError):
        w.write(
            kind="wat",  # type: ignore[arg-type]
            symbol="A", side="BUY", score=0.5, threshold=0.55,
            reason="r", source_signal_id=None,
        )


def test_audit_writer_if_enabled_returns_none_for_none() -> None:
    assert MLGateAuditWriter.if_enabled(None) is None


# ---- strategy integration -------------------------------------------------


def test_momentum_follow_writes_audit_on_allow(
    patch_loader, tmp_path: Path
) -> None:
    patch_loader(p_long=0.95)
    audit_root = tmp_path / "audit"
    strat = MomentumFollow(
        {
            "notional_usd": "500",
            "ml_gate_audit_root": str(audit_root),
            "ml_gate": {
                "enabled": True,
                "model_path": str(tmp_path / "model.pkl"),
                "score_threshold": 0.55,
            },
        }
    )
    intents = list(strat.on_signal(_signal("LONG"), _account(position="0")))
    assert len(intents) == 1  # allowed BUY
    shard = audit_root / f"ml_gate.{intents[0].created_at.date().isoformat()}.jsonl"
    assert shard.exists()
    rows = [json.loads(l) for l in shard.read_text().splitlines() if l.strip()]
    assert len(rows) == 1
    assert rows[0]["kind"] == "allowed"
    assert rows[0]["side"] == "BUY"


def test_momentum_follow_writes_audit_on_suppress(
    patch_loader, tmp_path: Path, caplog
) -> None:
    patch_loader(p_long=0.05)
    audit_root = tmp_path / "audit"
    strat = MomentumFollow(
        {
            "notional_usd": "500",
            "ml_gate_audit_root": str(audit_root),
            "ml_gate": {
                "enabled": True,
                "model_path": str(tmp_path / "model.pkl"),
                "score_threshold": 0.55,
            },
        }
    )
    with caplog.at_level(logging.INFO, logger="xtrade.strategy.momentum_follow"):
        intents = list(strat.on_signal(_signal("LONG"), _account(position="0")))
    assert intents == []  # suppressed
    shard = next((audit_root.glob("ml_gate.*.jsonl")))
    rows = [json.loads(l) for l in shard.read_text().splitlines() if l.strip()]
    assert len(rows) == 1
    assert rows[0]["kind"] == "suppressed"


def test_momentum_follow_no_audit_when_root_absent(patch_loader, tmp_path: Path) -> None:
    patch_loader(p_long=0.95)
    strat = MomentumFollow(
        {
            "notional_usd": "500",
            # NO ml_gate_audit_root
            "ml_gate": {
                "enabled": True,
                "model_path": str(tmp_path / "model.pkl"),
                "score_threshold": 0.55,
            },
        }
    )
    intents = list(strat.on_signal(_signal("LONG"), _account(position="0")))
    assert len(intents) == 1
    # No audit_root was given → no shard anywhere.
    assert not list(tmp_path.glob("ml_gate.*.jsonl"))


def test_momentum_follow_reduce_only_close_skips_audit(
    patch_loader, tmp_path: Path
) -> None:
    # Even gate-on with low p_long, a FLAT-direction reduce-only close
    # passes through unchanged AND must NOT write an audit row. FLAT
    # generates ONLY a close (no follow-on open) so this exercises the
    # "reduce_only bypasses gate" path in isolation.
    patch_loader(p_long=0.05)
    audit_root = tmp_path / "audit"
    strat = MomentumFollow(
        {
            "notional_usd": "500",
            "ml_gate_audit_root": str(audit_root),
            "ml_gate": {
                "enabled": True,
                "model_path": str(tmp_path / "model.pkl"),
                "score_threshold": 0.55,
            },
        }
    )
    # Long position + FLAT signal → reduce-only SELL close, nothing else.
    intents = list(strat.on_signal(_signal("FLAT"), _account(position="1")))
    assert len(intents) == 1
    assert intents[0].reduce_only is True
    # No audit row was written.
    assert not list(audit_root.glob("ml_gate.*.jsonl"))


def test_momentum_follow_emits_allowed_event(patch_loader, tmp_path: Path, caplog) -> None:
    patch_loader(p_long=0.95)
    strat = MomentumFollow(
        {
            "notional_usd": "500",
            "ml_gate": {
                "enabled": True,
                "model_path": str(tmp_path / "model.pkl"),
                "score_threshold": 0.55,
            },
        }
    )
    with caplog.at_level(logging.INFO, logger="xtrade.strategy.momentum_follow"):
        intents = list(strat.on_signal(_signal("LONG"), _account(position="0")))
    assert len(intents) == 1
    allowed = [r for r in caplog.records if "ml_gate.allowed" in r.getMessage()]
    assert allowed, "expected strategy.ml_gate.allowed event"
