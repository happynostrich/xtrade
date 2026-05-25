"""Tests for `xtrade.research.replay_gate` (Phase 5 / C1).

The replay-gate is an offline diagnostic: it consumes persisted
SignalQueue jsonl shards + a trained model and emits a deterministic
JSON summary. These tests fake the model loader (same pattern as
`tests/test_strategy_ml_gate.py`) so we don't need a pickled estimator
on disk.

Coverage
--------
* Empty time window → file written with zero counts (exit-code-friendly).
* Mixed LONG / SHORT / FLAT signals → counts populated; FLAT goes to
  ``n_skipped_flat``.
* Byte-stability: two identical runs produce byte-identical JSON.
* Missing model artefact → FileNotFoundError surfaced from MLGate loader.
* `until <= since` → ValueError (caller misuse, fail fast).
* Naive datetime → ValueError.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from xtrade.research import ml_gate as ml_gate_module
from xtrade.research.replay_gate import replay_gate, replay_gate_summary
from xtrade.research.signals import Signal, SignalQueue


UTC = dt.timezone.utc

FEATURE_NAMES = (
    "ret_5m",
    "ret_15m",
    "ret_60m",
    "vol_15m",
    "vol_60m",
    "sentiment_score",
    "sentiment_score_lag_1h",
)


class _FakeModel:
    """Returns ``p_long`` as a function of ``ret_5m`` sign so we can
    exercise the direction_check branch deterministically."""

    def __init__(self, *, bullish_p: float, bearish_p: float) -> None:
        self.bullish_p = bullish_p
        self.bearish_p = bearish_p

    def predict_proba(self, X):  # noqa: N802 - sklearn API name
        import numpy as np

        out = np.zeros((X.shape[0], 2), dtype="float64")
        for i in range(X.shape[0]):
            # ret_5m is feature index 0; positive → bullish row.
            p = self.bullish_p if X[i, 0] >= 0.0 else self.bearish_p
            out[i, 0] = 1.0 - p
            out[i, 1] = p
        return out


@pytest.fixture
def patch_loader(monkeypatch):
    """Install a fake `_load_model_and_meta` so tests don't need real pickle."""

    def _install(*, bullish_p: float = 0.9, bearish_p: float = 0.1) -> None:
        model = _FakeModel(bullish_p=bullish_p, bearish_p=bearish_p)
        meta = {"feature_names": list(FEATURE_NAMES)}

        def _fake(_path: Path):
            # Touch the path so `MLGate.__init__`'s `model_path.exists()`
            # check passes; the fake loader is what actually returns the
            # model bytes-substitute.
            _path.parent.mkdir(parents=True, exist_ok=True)
            _path.write_bytes(b"unused-by-fake-loader")
            return model, meta

        monkeypatch.setattr(ml_gate_module, "_load_model_and_meta", _fake)

    return _install


def _sig(
    *,
    ts: dt.datetime,
    symbol: str = "BTCUSDT-PERP",
    direction: str = "LONG",
    strength: float = 0.5,
    source: str = "momentum:abc12345",
) -> Signal:
    return Signal(
        symbol=symbol,
        venue="binance",
        direction=direction,  # type: ignore[arg-type]
        strength=strength if direction != "FLAT" else 0.0,
        generated_at=ts,
        source=source,
    )


def _seed_signals(root: Path, signals: list[Signal]) -> None:
    SignalQueue(root).append(signals)


# --- contract & input validation -------------------------------------------


def test_replay_gate_rejects_naive_since(patch_loader, tmp_path: Path) -> None:
    patch_loader()
    with pytest.raises(ValueError):
        replay_gate_summary(
            run_id="r",
            since=dt.datetime(2026, 5, 22),  # naive
            until=dt.datetime(2026, 5, 23, tzinfo=UTC),
            signals_root=tmp_path / "signals",
            models_root=tmp_path / "models",
        )


def test_replay_gate_rejects_inverted_window(patch_loader, tmp_path: Path) -> None:
    patch_loader()
    with pytest.raises(ValueError):
        replay_gate_summary(
            run_id="r",
            since=dt.datetime(2026, 5, 23, tzinfo=UTC),
            until=dt.datetime(2026, 5, 22, tzinfo=UTC),
            signals_root=tmp_path / "signals",
            models_root=tmp_path / "models",
        )


def test_replay_gate_missing_model_artefact_raises(tmp_path: Path) -> None:
    # No patch_loader → real `_load_model_and_meta` runs and fails.
    with pytest.raises(FileNotFoundError):
        replay_gate_summary(
            run_id="r",
            since=dt.datetime(2026, 5, 22, tzinfo=UTC),
            until=dt.datetime(2026, 5, 23, tzinfo=UTC),
            signals_root=tmp_path / "signals",
            models_root=tmp_path / "models",
        )


# --- empty / boundary cases ------------------------------------------------


def test_replay_gate_empty_window_writes_zero_summary(
    patch_loader, tmp_path: Path
) -> None:
    patch_loader()
    signals_root = tmp_path / "signals"
    signals_root.mkdir()
    models_root = tmp_path / "models"
    out = replay_gate(
        run_id="r",
        since=dt.datetime(2026, 5, 22, tzinfo=UTC),
        until=dt.datetime(2026, 5, 23, tzinfo=UTC),
        signals_root=signals_root,
        models_root=models_root,
    )
    assert out.exists()
    payload = json.loads(out.read_text())
    assert payload["n_signals"] == 0
    assert payload["n_allowed"] == 0
    assert payload["n_suppressed"] == 0
    assert payload["by_side"] == {}
    assert payload["by_symbol"] == {}
    # Quantiles defined even when empty.
    assert payload["p_long_quantiles"] == {"p10": 0.0, "p50": 0.0, "p90": 0.0}


def test_replay_gate_missing_signals_root_is_empty(patch_loader, tmp_path: Path) -> None:
    patch_loader()
    out = replay_gate(
        run_id="r",
        since=dt.datetime(2026, 5, 22, tzinfo=UTC),
        until=dt.datetime(2026, 5, 23, tzinfo=UTC),
        signals_root=tmp_path / "signals" / "does-not-exist",
        models_root=tmp_path / "models",
    )
    payload = json.loads(out.read_text())
    assert payload["n_signals"] == 0


# --- end-to-end counts -----------------------------------------------------


def test_replay_gate_mixed_signals_counts_by_side_and_symbol(
    patch_loader, tmp_path: Path
) -> None:
    # bullish_p=0.90 → LONG (BUY) at 0.90 >= 0.55 → allowed.
    # bearish_p=0.10 → SHORT row: p_short=1-0.10=0.90 >= 0.55 → allowed.
    # Both LONG and SHORT pass with this fake; switch thresholds to force splits.
    patch_loader(bullish_p=0.80, bearish_p=0.45)
    signals_root = tmp_path / "signals"
    signals = [
        _sig(ts=dt.datetime(2026, 5, 22, 10, 0, tzinfo=UTC), direction="LONG"),
        _sig(
            ts=dt.datetime(2026, 5, 22, 10, 5, tzinfo=UTC),
            symbol="ETHUSDT-PERP",
            direction="LONG",
            source="momentum:eth12345",
        ),
        _sig(
            ts=dt.datetime(2026, 5, 22, 10, 10, tzinfo=UTC),
            direction="SHORT",
            source="momentum:short1234",
        ),
        _sig(
            ts=dt.datetime(2026, 5, 22, 10, 15, tzinfo=UTC),
            direction="FLAT",
            source="momentum:flat12345",
        ),
        # Outside window — must be dropped.
        _sig(
            ts=dt.datetime(2026, 5, 21, 23, 59, tzinfo=UTC),
            direction="LONG",
            source="momentum:out12345",
        ),
    ]
    _seed_signals(signals_root, signals)

    out = replay_gate(
        run_id="r",
        since=dt.datetime(2026, 5, 22, tzinfo=UTC),
        until=dt.datetime(2026, 5, 23, tzinfo=UTC),
        signals_root=signals_root,
        models_root=tmp_path / "models",
        score_threshold=0.55,
        direction_check=True,
    )
    payload = json.loads(out.read_text())

    # 4 signals in window (out-of-window dropped), 1 FLAT skipped.
    assert payload["n_signals"] == 4
    assert payload["n_skipped_flat"] == 1
    # LONG bullish_p=0.80 ≥ 0.55 → 2 allowed BUY.
    # SHORT: p_short = 1 - bullish_p(bearish_row, ret_5m<0) = 1 - 0.45 = 0.55 ≥ 0.55 → allowed.
    assert payload["n_allowed"] == 3
    assert payload["n_suppressed"] == 0
    # by_side
    assert payload["by_side"]["BUY"]["allowed"] == 2
    assert payload["by_side"]["SELL"]["allowed"] == 1
    # by_symbol
    assert payload["by_symbol"]["BTCUSDT-PERP"]["allowed"] == 2  # 1 LONG + 1 SHORT
    assert payload["by_symbol"]["ETHUSDT-PERP"]["allowed"] == 1


def test_replay_gate_suppression_path(patch_loader, tmp_path: Path) -> None:
    # bullish_p=0.10 → BUY suppressed; bearish_p=0.95 (long-row positive p_long
    # for the SHORT input's ret_5m<0 path; p_short=1-0.95=0.05 < threshold → suppressed).
    patch_loader(bullish_p=0.10, bearish_p=0.95)
    signals_root = tmp_path / "signals"
    _seed_signals(
        signals_root,
        [
            _sig(ts=dt.datetime(2026, 5, 22, 10, 0, tzinfo=UTC), direction="LONG"),
            _sig(
                ts=dt.datetime(2026, 5, 22, 10, 1, tzinfo=UTC),
                direction="SHORT",
                source="momentum:s1",
            ),
        ],
    )
    out = replay_gate(
        run_id="r",
        since=dt.datetime(2026, 5, 22, tzinfo=UTC),
        until=dt.datetime(2026, 5, 23, tzinfo=UTC),
        signals_root=signals_root,
        models_root=tmp_path / "models",
        score_threshold=0.55,
    )
    payload = json.loads(out.read_text())
    assert payload["n_signals"] == 2
    assert payload["n_allowed"] == 0
    assert payload["n_suppressed"] == 2
    assert payload["by_side"]["BUY"]["suppressed"] == 1
    assert payload["by_side"]["SELL"]["suppressed"] == 1


# --- byte-stability --------------------------------------------------------


def test_replay_gate_output_is_byte_stable(patch_loader, tmp_path: Path) -> None:
    patch_loader(bullish_p=0.80, bearish_p=0.20)
    signals_root = tmp_path / "signals"
    _seed_signals(
        signals_root,
        [
            _sig(ts=dt.datetime(2026, 5, 22, 10, 0, tzinfo=UTC), direction="LONG"),
            _sig(
                ts=dt.datetime(2026, 5, 22, 10, 1, tzinfo=UTC),
                direction="SHORT",
                source="momentum:s1",
            ),
        ],
    )

    out_a = replay_gate(
        run_id="r",
        since=dt.datetime(2026, 5, 22, tzinfo=UTC),
        until=dt.datetime(2026, 5, 23, tzinfo=UTC),
        signals_root=signals_root,
        models_root=tmp_path / "a",
    )
    out_b = replay_gate(
        run_id="r",
        since=dt.datetime(2026, 5, 22, tzinfo=UTC),
        until=dt.datetime(2026, 5, 23, tzinfo=UTC),
        signals_root=signals_root,
        models_root=tmp_path / "b",
    )
    assert out_a.read_bytes() == out_b.read_bytes()


# --- filename encoding -----------------------------------------------------


def test_replay_gate_filename_encodes_window(patch_loader, tmp_path: Path) -> None:
    patch_loader()
    signals_root = tmp_path / "signals"
    signals_root.mkdir()
    out = replay_gate(
        run_id="abc12345",
        since=dt.datetime(2026, 5, 22, 0, 0, 0, tzinfo=UTC),
        until=dt.datetime(2026, 5, 23, 0, 0, 0, tzinfo=UTC),
        signals_root=signals_root,
        models_root=tmp_path / "models",
    )
    assert out.name == "replay_20260522T000000Z_20260523T000000Z.json"
    assert out.parent.name == "abc12345"
