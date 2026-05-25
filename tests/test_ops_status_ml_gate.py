"""Tests for `xtrade.ops.status._read_ml_gate_audit` (Phase 5 / Track C2).

The ML-gate audit reader is the pure-filesystem half of C2: it consumes
``<audit_root>/ml_gate.<YYYY-MM-DD>.jsonl`` shards produced by
`xtrade.strategy.ml_gate_audit.MLGateAuditWriter` and aggregates them
into the 24h `MLGateStatus` exposed via `xtrade ops status`.

Coverage
--------
* Missing / empty audit_root → all zeros, rate None, age None.
* All-allowed → suppression_rate_24h == 0.0 (when sample ≥ 10).
* All-suppressed → suppression_rate_24h == 100.0 (when sample ≥ 10).
* Mixed sample ≥ 10 → rate populated, last_event_age_s monotone.
* Sample size < `_MLGATE_RATE_MIN_SAMPLE` → rate None.
* Rows older than 24h cutoff are excluded.
* Corrupt / missing-key rows degrade silently.
* Spanning two day-shards (yesterday + today) both counted.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from xtrade.ops.status import (
    MLGateStatus,
    _MLGATE_RATE_MIN_SAMPLE,
    _read_ml_gate_audit,
)


UTC = dt.timezone.utc
NOW = dt.datetime(2026, 5, 23, 12, 0, 0, tzinfo=UTC)


def _row(
    *,
    ts: dt.datetime,
    kind: str = "allowed",
    symbol: str = "BTCUSDT-PERP",
    side: str = "BUY",
    score: float = 0.7,
    threshold: float = 0.55,
) -> str:
    return json.dumps(
        {
            "ts": ts.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "kind": kind,
            "symbol": symbol,
            "side": side,
            "score": score,
            "threshold": threshold,
            "reason": "r",
            "source_signal_id": "s",
        },
        sort_keys=True,
    )


def _write_shard(audit_root: Path, day: dt.date, rows: list[str]) -> Path:
    audit_root.mkdir(parents=True, exist_ok=True)
    shard = audit_root / f"ml_gate.{day.isoformat()}.jsonl"
    shard.write_text("\n".join(rows) + ("\n" if rows else ""), encoding="utf-8")
    return shard


# ---- empty / missing -----------------------------------------------------


def test_missing_audit_root_returns_empty(tmp_path: Path) -> None:
    out = _read_ml_gate_audit(tmp_path / "does-not-exist", now=NOW)
    assert out == MLGateStatus()


def test_audit_root_is_file_returns_empty(tmp_path: Path) -> None:
    bad = tmp_path / "audit"
    bad.write_text("not a directory")
    out = _read_ml_gate_audit(bad, now=NOW)
    assert out == MLGateStatus()


def test_empty_shard_dir_returns_empty(tmp_path: Path) -> None:
    audit_root = tmp_path / "audit"
    audit_root.mkdir()
    out = _read_ml_gate_audit(audit_root, now=NOW)
    assert out == MLGateStatus()


# ---- happy paths ---------------------------------------------------------


def test_all_allowed_rate_zero_when_sample_sufficient(tmp_path: Path) -> None:
    audit_root = tmp_path / "audit"
    rows = [
        _row(ts=NOW - dt.timedelta(minutes=10 + i), kind="allowed")
        for i in range(_MLGATE_RATE_MIN_SAMPLE)
    ]
    _write_shard(audit_root, NOW.date(), rows)
    out = _read_ml_gate_audit(audit_root, now=NOW)
    assert out.allowed_24h == _MLGATE_RATE_MIN_SAMPLE
    assert out.suppressed_24h == 0
    assert out.suppression_rate_24h == 0.0


def test_all_suppressed_rate_hundred(tmp_path: Path) -> None:
    audit_root = tmp_path / "audit"
    rows = [
        _row(ts=NOW - dt.timedelta(minutes=i), kind="suppressed")
        for i in range(_MLGATE_RATE_MIN_SAMPLE)
    ]
    _write_shard(audit_root, NOW.date(), rows)
    out = _read_ml_gate_audit(audit_root, now=NOW)
    assert out.allowed_24h == 0
    assert out.suppressed_24h == _MLGATE_RATE_MIN_SAMPLE
    assert out.suppression_rate_24h == 100.0


def test_mixed_sample_rate_populated_and_age_monotone(tmp_path: Path) -> None:
    audit_root = tmp_path / "audit"
    # 6 allowed + 6 suppressed = 12 total ≥ 10 → rate populated.
    newest_ts = NOW - dt.timedelta(minutes=2)
    rows = []
    for i in range(6):
        rows.append(_row(ts=NOW - dt.timedelta(minutes=10 + i), kind="allowed"))
    for i in range(6):
        rows.append(_row(ts=NOW - dt.timedelta(minutes=20 + i), kind="suppressed"))
    rows.append(_row(ts=newest_ts, kind="allowed"))  # newest, defines age
    _write_shard(audit_root, NOW.date(), rows)

    out = _read_ml_gate_audit(audit_root, now=NOW)
    assert out.allowed_24h == 7
    assert out.suppressed_24h == 6
    assert out.suppression_rate_24h == round(100.0 * 6 / 13, 2)
    assert out.last_event_age_s == pytest.approx(120.0, abs=1.0)


def test_below_min_sample_rate_is_none(tmp_path: Path) -> None:
    audit_root = tmp_path / "audit"
    rows = [
        _row(ts=NOW - dt.timedelta(minutes=i), kind="suppressed")
        for i in range(_MLGATE_RATE_MIN_SAMPLE - 1)
    ]
    _write_shard(audit_root, NOW.date(), rows)
    out = _read_ml_gate_audit(audit_root, now=NOW)
    assert out.suppressed_24h == _MLGATE_RATE_MIN_SAMPLE - 1
    assert out.allowed_24h == 0
    assert out.suppression_rate_24h is None
    # Age still defined from newest row.
    assert out.last_event_age_s is not None


# ---- windowing -----------------------------------------------------------


def test_rows_outside_24h_window_excluded(tmp_path: Path) -> None:
    audit_root = tmp_path / "audit"
    yesterday = (NOW - dt.timedelta(days=1)).date()
    # 25h-old row should be EXCLUDED.
    old = _row(ts=NOW - dt.timedelta(hours=25), kind="suppressed")
    # 1h-old row should be INCLUDED.
    fresh = _row(ts=NOW - dt.timedelta(hours=1), kind="allowed")
    _write_shard(audit_root, yesterday, [old])
    _write_shard(audit_root, NOW.date(), [fresh])
    out = _read_ml_gate_audit(audit_root, now=NOW)
    assert out.allowed_24h == 1
    assert out.suppressed_24h == 0


def test_two_day_shards_aggregated(tmp_path: Path) -> None:
    audit_root = tmp_path / "audit"
    yesterday = (NOW - dt.timedelta(days=1)).date()
    _write_shard(
        audit_root,
        yesterday,
        [_row(ts=NOW - dt.timedelta(hours=23), kind="allowed")],
    )
    _write_shard(
        audit_root,
        NOW.date(),
        [_row(ts=NOW - dt.timedelta(hours=1), kind="suppressed")],
    )
    out = _read_ml_gate_audit(audit_root, now=NOW)
    assert out.allowed_24h == 1
    assert out.suppressed_24h == 1


# ---- soft-failure --------------------------------------------------------


def test_corrupt_rows_degrade_silently(tmp_path: Path) -> None:
    audit_root = tmp_path / "audit"
    rows = [
        "{this is not json",                              # bad json
        json.dumps({"kind": "allowed"}),                  # missing ts
        json.dumps({"ts": "not-iso", "kind": "allowed"}), # bad ts
        json.dumps({"ts": NOW.strftime("%Y-%m-%dT%H:%M:%SZ"), "kind": "weird"}),  # bad kind
        _row(ts=NOW - dt.timedelta(minutes=5), kind="allowed"),
    ]
    _write_shard(audit_root, NOW.date(), rows)
    out = _read_ml_gate_audit(audit_root, now=NOW)
    assert out.allowed_24h == 1
    assert out.suppressed_24h == 0


def test_naive_ts_treated_as_utc(tmp_path: Path) -> None:
    audit_root = tmp_path / "audit"
    naive_ts = (NOW - dt.timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%S")
    row = json.dumps(
        {
            "ts": naive_ts,
            "kind": "allowed",
            "symbol": "X",
            "side": "BUY",
            "score": 0.7,
            "threshold": 0.55,
            "reason": "r",
            "source_signal_id": None,
        }
    )
    _write_shard(audit_root, NOW.date(), [row])
    out = _read_ml_gate_audit(audit_root, now=NOW)
    assert out.allowed_24h == 1
