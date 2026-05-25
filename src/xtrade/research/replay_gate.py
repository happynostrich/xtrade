"""Offline ML-gate replay (Phase 5 / Track C1).

Replays a `MLGate` decision over a previously-persisted `SignalQueue`
jsonl shard and persists a summary report. This is a **diagnostic tool**
— it never constructs a venue connection, never imports `xtrade.live.*`,
and never modifies the signals on disk.

Typical use::

    xtrade research replay-gate \\
        --run-id abc12345 \\
        --since 2026-05-22T00:00:00Z \\
        --until 2026-05-23T00:00:00Z

Output landing
--------------
``<models_root>/<run_id>/replay_<since>_<until>.json``

The output is byte-stable: re-running with the same inputs produces an
identical file (timestamps are formatted without microseconds; counts
dict is JSON-dumped with ``sort_keys=True``).

What is NOT replayed
--------------------
- Account state / positions: the strategy's reduce-only pass-through is
  not modelled here. Replay reflects "would the gate have allowed the
  *opening* intent?". Reduce-only closes are unaffected by the gate
  (Track B), so the replay focus is by design.
- Signal `FLAT`: counted under ``n_skipped_flat`` (no gateable side).
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
from collections.abc import Iterable
from pathlib import Path

from xtrade.research.ml_gate import (
    MLGate,
    MLGateConfig,
    build_features_from_signal,
)
from xtrade.research.signals import Signal, SignalQueue


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class ReplaySummary:
    """In-memory view of a replay run (mirrors the on-disk JSON schema)."""

    run_id: str
    since: dt.datetime
    until: dt.datetime
    score_threshold: float
    direction_check: bool
    n_signals: int
    n_allowed: int
    n_suppressed: int
    n_skipped_flat: int
    by_side: dict[str, dict[str, int]]
    by_symbol: dict[str, dict[str, int]]
    p_long_quantiles: dict[str, float]


def replay_gate(
    *,
    run_id: str,
    since: dt.datetime,
    until: dt.datetime,
    signals_root: Path,
    models_root: Path = Path("models"),
    score_threshold: float = 0.55,
    direction_check: bool = True,
) -> Path:
    """Replay an ML gate over signals in ``[since, until)``.

    Returns the path to the persisted JSON summary. The summary itself
    is also available in-memory via :func:`replay_gate_summary` for tests
    that don't want to round-trip through disk.
    """

    summary = replay_gate_summary(
        run_id=run_id,
        since=since,
        until=until,
        signals_root=signals_root,
        models_root=models_root,
        score_threshold=score_threshold,
        direction_check=direction_check,
    )
    out_dir = models_root / run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / _summary_filename(since, until)
    out_path.write_text(_render_json(summary), encoding="utf-8")
    return out_path


def replay_gate_summary(
    *,
    run_id: str,
    since: dt.datetime,
    until: dt.datetime,
    signals_root: Path,
    models_root: Path = Path("models"),
    score_threshold: float = 0.55,
    direction_check: bool = True,
) -> ReplaySummary:
    """Compute the replay summary without touching disk for the output.

    Raises
    ------
    ValueError
        If ``since`` / ``until`` are not tz-aware or ``until <= since``.
    FileNotFoundError
        If the model artefact triple (model.pkl + dataset_meta.json) is
        missing under ``models_root/<run_id>/``.
    """

    if since.tzinfo is None or until.tzinfo is None:
        raise ValueError("since/until must be timezone-aware (UTC)")
    if until <= since:
        raise ValueError(f"until ({until!s}) must be strictly > since ({since!s})")

    model_path = models_root / run_id / "model.pkl"
    config = MLGateConfig(
        enabled=True,
        model_path=model_path,
        score_threshold=score_threshold,
        direction_check=direction_check,
    )
    gate = MLGate(config)

    signals = _load_signals(signals_root, since, until)
    return _tally(
        gate=gate,
        signals=signals,
        run_id=run_id,
        since=since,
        until=until,
        score_threshold=score_threshold,
        direction_check=direction_check,
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


_DIRECTION_TO_SIDE: dict[str, str] = {"LONG": "BUY", "SHORT": "SELL"}


def _load_signals(
    signals_root: Path,
    since: dt.datetime,
    until: dt.datetime,
) -> list[Signal]:
    if not signals_root.exists():
        return []
    queue = SignalQueue(signals_root)
    since_utc = since.astimezone(dt.timezone.utc)
    until_utc = until.astimezone(dt.timezone.utc)
    return [s for s in queue if since_utc <= s.generated_at < until_utc]


def _tally(
    *,
    gate: MLGate,
    signals: Iterable[Signal],
    run_id: str,
    since: dt.datetime,
    until: dt.datetime,
    score_threshold: float,
    direction_check: bool,
) -> ReplaySummary:
    by_side: dict[str, dict[str, int]] = {}
    by_symbol: dict[str, dict[str, int]] = {}
    p_longs: list[float] = []

    n_signals = 0
    n_allowed = 0
    n_suppressed = 0
    n_skipped_flat = 0

    for sig in signals:
        n_signals += 1
        side = _DIRECTION_TO_SIDE.get(sig.direction.upper())
        if side is None:
            # FLAT signal: no opening intent, nothing to gate.
            n_skipped_flat += 1
            continue
        features = build_features_from_signal(
            signal_strength=sig.strength,
            direction=sig.direction,
            sentiment_score=float(sig.metadata.get("sentiment_score", 0.0)),
            sentiment_score_lag_1h=float(
                sig.metadata.get("sentiment_score_lag_1h", 0.0)
            ),
        )
        # `MLGate.score` is the bullish probability regardless of which
        # side decide() compares it to; record it once per gated signal.
        p_long = gate.score(features)
        p_longs.append(p_long)

        decision = gate.decide(side=side, features=features)
        outcome = "allowed" if decision.allow else "suppressed"
        if decision.allow:
            n_allowed += 1
        else:
            n_suppressed += 1
        _bump(by_side, side, outcome)
        _bump(by_symbol, sig.symbol, outcome)

    return ReplaySummary(
        run_id=run_id,
        since=since.astimezone(dt.timezone.utc),
        until=until.astimezone(dt.timezone.utc),
        score_threshold=float(score_threshold),
        direction_check=bool(direction_check),
        n_signals=n_signals,
        n_allowed=n_allowed,
        n_suppressed=n_suppressed,
        n_skipped_flat=n_skipped_flat,
        by_side=by_side,
        by_symbol=by_symbol,
        p_long_quantiles=_quantiles(p_longs),
    )


def _bump(buckets: dict[str, dict[str, int]], key: str, outcome: str) -> None:
    bucket = buckets.setdefault(key, {"allowed": 0, "suppressed": 0})
    bucket[outcome] = bucket.get(outcome, 0) + 1


def _quantiles(values: list[float]) -> dict[str, float]:
    if not values:
        return {"p10": 0.0, "p50": 0.0, "p90": 0.0}
    import numpy as np  # noqa: PLC0415

    arr = np.asarray(values, dtype="float64")
    q = np.quantile(arr, [0.10, 0.50, 0.90])
    return {
        "p10": round(float(q[0]), 6),
        "p50": round(float(q[1]), 6),
        "p90": round(float(q[2]), 6),
    }


def _summary_filename(since: dt.datetime, until: dt.datetime) -> str:
    return (
        f"replay_{_compact(since)}_{_compact(until)}.json"
    )


def _compact(ts: dt.datetime) -> str:
    """``2026-05-22T00:00:00+00:00`` → ``20260522T000000Z``."""
    return ts.astimezone(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _iso_z(ts: dt.datetime) -> str:
    """ISO-8601 with trailing ``Z`` and no microseconds."""
    return ts.astimezone(dt.timezone.utc).replace(microsecond=0).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _render_json(summary: ReplaySummary) -> str:
    payload: dict[str, object] = {
        "run_id": summary.run_id,
        "since": _iso_z(summary.since),
        "until": _iso_z(summary.until),
        "score_threshold": summary.score_threshold,
        "direction_check": summary.direction_check,
        "n_signals": summary.n_signals,
        "n_allowed": summary.n_allowed,
        "n_suppressed": summary.n_suppressed,
        "n_skipped_flat": summary.n_skipped_flat,
        "by_side": summary.by_side,
        "by_symbol": summary.by_symbol,
        "p_long_quantiles": summary.p_long_quantiles,
    }
    return json.dumps(payload, sort_keys=True, indent=2) + "\n"
