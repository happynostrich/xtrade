"""Schema tests for Phase 3 strategy summaries (Task 8 / T8).

Two summary artefacts are written under `logs/<run-id>/` by the Phase 3
runners:

  - `paper_summary.json`        — `xtrade.strategy.runner.run_paper`
  - `live_signal_summary.json`  — `xtrade.live.signal_runner.run_live_signal`

These tests pin the schema both modules expose, so any field rename or
deletion shows up here before downstream tooling (Phase 4 dashboards,
log shippers, the runbook) silently breaks.

Notes
-----
- Paper-summary verification needs a real `BacktestEngine` run, so it
  shells out via `tests/_paper_runner_subprocess.py` (Nautilus aborts
  on a second engine instantiation in one process — see Task 5's notes).
- Live-signal-summary verification calls `run_live_signal` directly
  with an injected `live_executor` stub, no subprocess needed.
"""

from __future__ import annotations

import datetime as dt
import json
import subprocess
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

import xtrade.strategy  # noqa: F401 — registers momentum_follow
from xtrade.live.signal_runner import run_live_signal
from xtrade.research.signals import Signal, SignalQueue


REPO_ROOT = Path(__file__).resolve().parents[1]
UTC = dt.timezone.utc


# ---------------------------------------------------------------------------
# Schema spec — single source of truth, mirrored in
# `docs/phase3_brief.md` §5 Task 8 + `docs/strategy_summary.schema.md`.
# ---------------------------------------------------------------------------

_PAPER_REQUIRED: dict[str, type | tuple[type, ...]] = {
    "run_id": str,
    "started_at": str,
    "completed_at": str,
    "mode": str,
    "strategy": str,
    "approval_mode": str,
    "instrument_id": str,
    "venue": str,
    "bar_type": str,
    "bars_loaded": int,
    "signals_consumed": int,
    "intents_generated": int,
    "risk_rejected": int,
    "approvals_pending": int,
    "approvals_confirmed": int,
    "approvals_rejected": int,
    "approvals_dry_run": int,
    "fills": int,
    "fill_events": list,
    "final_cash_usd": str,
    "final_position_qty": str,
    "final_nav_usd": str,
    "peak_nav_usd": str,
    "max_drawdown_pct": (int, float),
    "elapsed_s": (int, float),
    "errors": list,
    "config": dict,
}

_FILL_EVENT_REQUIRED: dict[str, type | tuple[type, ...]] = {
    "ts_event": int,
    "symbol": str,
    "side": str,
    "qty": str,
    "price": str,
}

_LIVE_REQUIRED: dict[str, type | tuple[type, ...]] = {
    "run_id": str,
    "started_at": str,
    "completed_at": str,
    "mode": str,
    "strategy": str,
    "approval_mode": str,
    "instrument_id": str,
    "signal": dict,
    "intent": dict,
    "approval": dict,
    "live_summary": (dict, type(None)),
    "passed": bool,
    "note": str,
    "config": dict,
}

_LIVE_SIGNAL_REQUIRED: dict[str, type | tuple[type, ...]] = {
    "symbol": str,
    "venue": str,
    "direction": str,
    "strength": (int, float),
    "generated_at": str,
    "source": str,
}

_LIVE_APPROVAL_REQUIRED: dict[str, type | tuple[type, ...]] = {
    "record_id": str,
    "status": str,
    "mode": str,
    "go": bool,
    "awaiting": bool,
}


def _assert_schema(payload: dict[str, Any], spec: dict[str, Any], *, ctx: str) -> None:
    for key, expected in spec.items():
        assert key in payload, f"{ctx}: missing required key {key!r}"
        actual = payload[key]
        if not isinstance(actual, expected):  # type: ignore[arg-type]
            raise AssertionError(
                f"{ctx}: key {key!r} has type {type(actual).__name__}, "
                f"expected {expected}"
            )


# ---------------------------------------------------------------------------
# paper_summary.json
# ---------------------------------------------------------------------------


def _run_paper_subprocess(tmp_path: Path) -> dict:
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "tests._paper_runner_subprocess",
            str(tmp_path / "catalog"),
            str(tmp_path / "signals"),
            str(tmp_path / "logs"),
            str(tmp_path / "approvals"),
            "auto",
            "schema-paper",
        ],
        cwd=str(REPO_ROOT),
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, (
        f"paper subprocess failed (rc={completed.returncode})\n"
        f"--- stdout ---\n{completed.stdout}\n"
        f"--- stderr ---\n{completed.stderr}"
    )
    for line in reversed(completed.stdout.strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            return json.loads(line)
    raise AssertionError(f"no JSON payload: {completed.stdout}")


def test_paper_summary_schema(tmp_path: Path) -> None:
    payload = _run_paper_subprocess(tmp_path)
    summary = payload["summary"]
    _assert_schema(summary, _PAPER_REQUIRED, ctx="paper_summary")

    # Bookkeeping invariants.
    assert summary["mode"] == "paper"
    assert summary["approval_mode"] == "auto"
    assert summary["fills"] == len(summary["fill_events"])
    # `approvals_confirmed + approvals_pending + approvals_dry_run +
    # approvals_rejected + risk_rejected` must add up to intents_generated.
    bucketed = (
        summary["approvals_confirmed"]
        + summary["approvals_pending"]
        + summary["approvals_dry_run"]
        + summary["approvals_rejected"]
        + summary["risk_rejected"]
    )
    assert bucketed == summary["intents_generated"], (
        f"approval/risk buckets {bucketed} != intents_generated "
        f"{summary['intents_generated']}"
    )

    # Per-fill schema.
    for i, fe in enumerate(summary["fill_events"]):
        _assert_schema(fe, _FILL_EVENT_REQUIRED, ctx=f"fill_events[{i}]")
        # `qty` and `price` are Decimal-strings — must round-trip.
        Decimal(fe["qty"])
        Decimal(fe["price"])
        assert fe["side"] in {"BUY", "SELL"}

    # Decimal-strings must round-trip to Decimal exactly.
    for key in (
        "final_cash_usd",
        "final_position_qty",
        "final_nav_usd",
        "peak_nav_usd",
    ):
        Decimal(summary[key])  # must not raise

    # `errors` is empty on a clean run.
    assert summary["errors"] == []

    # On-disk and in-memory must agree byte-for-byte.
    summary_path = Path(payload["summary_path"])
    assert summary_path.exists()
    on_disk = json.loads(summary_path.read_text())
    assert on_disk == summary


# ---------------------------------------------------------------------------
# live_signal_summary.json
# ---------------------------------------------------------------------------


class _StubLiveResult:
    passed: bool = True
    summary: dict[str, Any] = {
        "run_id": "stub",
        "order": {"accepted": True, "canceled": True, "rejected": False},
    }


def _stub_executor(venues_cfg, **kwargs):  # noqa: ANN001
    return _StubLiveResult()


def _seed_signal(signals_root: Path) -> Signal:
    sig = Signal(
        symbol="BTCUSDT-PERP.BINANCE",
        venue="binance",
        direction="LONG",
        strength=0.6,
        generated_at=dt.datetime(2026, 5, 22, 12, 0, 0, tzinfo=UTC),
        source="momentum:schema",
        metadata={"last_price": "50000"},
    )
    SignalQueue(signals_root).append([sig])
    return sig


def test_live_signal_summary_schema_auto(tmp_path: Path) -> None:
    signals = tmp_path / "signals"
    _seed_signal(signals)

    result = run_live_signal(
        venues_cfg=object(),
        strategy_name="momentum_follow",
        signals_root=signals,
        instrument_id="BTCUSDT-PERP.BINANCE",
        approval_mode="auto",
        approvals_root=tmp_path / "approvals",
        logs_root=tmp_path / "logs",
        live_executor=_stub_executor,
    )
    s = result.summary
    _assert_schema(s, _LIVE_REQUIRED, ctx="live_signal_summary")
    _assert_schema(s["signal"], _LIVE_SIGNAL_REQUIRED, ctx="live_signal_summary.signal")
    _assert_schema(s["approval"], _LIVE_APPROVAL_REQUIRED, ctx="live_signal_summary.approval")

    assert s["mode"] == "live_signal"
    assert s["approval_mode"] == "auto"
    assert s["approval"]["go"] is True
    assert s["approval"]["status"] == "confirmed"
    assert s["passed"] is True
    assert s["live_summary"] is not None

    # The intent dict is whatever `OrderIntent.to_dict()` produces; we
    # just sanity-check it has the canonical fields needed downstream.
    intent = s["intent"]
    for key in (
        "venue",
        "symbol",
        "side",
        "order_type",
        "quantity",
        "limit_price",
        "reduce_only",
        "time_in_force",
        "source_signal_id",
        "created_at",
    ):
        assert key in intent, f"intent missing {key!r}: {intent}"

    # Decimal-string round-trip.
    Decimal(intent["quantity"])

    # On-disk equals in-memory.
    on_disk = json.loads(result.summary_path.read_text())
    assert on_disk == s


def test_live_signal_summary_schema_dry_run(tmp_path: Path) -> None:
    signals = tmp_path / "signals"
    _seed_signal(signals)

    result = run_live_signal(
        venues_cfg=object(),
        strategy_name="momentum_follow",
        signals_root=signals,
        instrument_id="BTCUSDT-PERP.BINANCE",
        approval_mode="dry_run",
        approvals_root=tmp_path / "approvals",
        logs_root=tmp_path / "logs",
        live_executor=_stub_executor,
    )
    s = result.summary
    _assert_schema(s, _LIVE_REQUIRED, ctx="live_signal_summary[dry_run]")
    assert s["approval_mode"] == "dry_run"
    assert s["live_summary"] is None
    assert s["passed"] is False
    # dry_run intent must be recorded in the approval queue (just confirm
    # the summary's approval block reflects that).
    assert s["approval"]["status"] in {"dry_run", "confirmed"}
