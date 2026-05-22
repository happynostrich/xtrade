"""End-to-end paper-runner test (Phase 3 Task 5 / T5).

Driven via subprocess (`tests/_paper_runner_subprocess.py`) because
`nautilus_trader.backtest.engine.BacktestEngine.__init__` aborts the
interpreter on second instantiation in the same process — the same
constraint that motivates `tests/_parity_nautilus_runner.py`.

The subprocess seeds a synthetic catalog + SignalQueue, calls
`run_paper(...)`, and prints the resulting summary JSON to stdout; the
parent here asserts shape + invariants.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def _run_paper_subprocess(
    *,
    catalog_path: Path,
    signals_root: Path,
    logs_root: Path,
    approvals_root: Path,
    mode: str,
    run_id: str,
) -> dict:
    """Shell out to the paper-runner subprocess and parse its stdout."""
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "tests._paper_runner_subprocess",
            str(catalog_path),
            str(signals_root),
            str(logs_root),
            str(approvals_root),
            mode,
            run_id,
        ],
        cwd=str(REPO_ROOT),
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise AssertionError(
            f"paper-runner subprocess failed (rc={completed.returncode})\n"
            f"--- stdout ---\n{completed.stdout}\n"
            f"--- stderr ---\n{completed.stderr}\n"
        )
    # The subprocess emits exactly one JSON line + some Nautilus logging
    # noise; grab the last non-empty stdout line that parses as JSON.
    payload: dict | None = None
    for line in reversed(completed.stdout.strip().splitlines()):
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        break
    assert payload is not None, f"no JSON payload in subprocess stdout: {completed.stdout}"
    return payload


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_paper_runner_auto_mode_produces_summary(tmp_path: Path) -> None:
    catalog = tmp_path / "catalog"
    signals = tmp_path / "signals"
    logs = tmp_path / "logs"
    approvals = tmp_path / "approvals"

    payload = _run_paper_subprocess(
        catalog_path=catalog,
        signals_root=signals,
        logs_root=logs,
        approvals_root=approvals,
        mode="auto",
        run_id="paper-auto",
    )

    summary = payload["summary"]

    # --- 1. Identity + plumbing -----------------------------------------
    assert summary["run_id"] == "paper-auto"
    assert summary["mode"] == "paper"
    assert summary["strategy"] == "momentum_follow"
    assert summary["approval_mode"] == "auto"
    assert summary["instrument_id"] == "BTCUSDT-PERP.BINANCE"
    assert summary["venue"] == "BINANCE"
    assert summary["bars_loaded"] == 200

    # --- 2. Signal flow --------------------------------------------------
    # Three signals (LONG/SHORT/FLAT) were seeded.
    assert summary["signals_consumed"] == 3
    # At least one intent must come out of MomentumFollow on the
    # synthetic bars; exact count depends on Nautilus fill timing so
    # we don't pin it.
    assert summary["intents_generated"] >= 1

    # --- 3. Approval-bucket counts (auto path) --------------------------
    assert summary["risk_rejected"] == 0
    assert summary["approvals_pending"] == 0
    assert summary["approvals_rejected"] == 0
    assert summary["approvals_dry_run"] == 0
    assert summary["approvals_confirmed"] == summary["intents_generated"]

    # --- 4. Fills + NAV --------------------------------------------------
    assert summary["fills"] > 0
    # NAV is reported as stringified Decimal.
    nav = summary["final_nav_usd"]
    assert isinstance(nav, str) and nav != ""
    # Auto mode means every confirmed intent SHOULD reach the engine;
    # fills <= confirmed (the engine may reject some due to balance / lot
    # size).
    assert summary["fills"] <= summary["approvals_confirmed"]

    # --- 5. Summary file on disk matches in-memory --------------------
    summary_path = Path(payload["summary_path"])
    assert summary_path.exists()
    on_disk = json.loads(summary_path.read_text())
    assert on_disk == summary


def test_paper_runner_dry_run_mode_records_but_does_not_submit(tmp_path: Path) -> None:
    catalog = tmp_path / "catalog"
    signals = tmp_path / "signals"
    logs = tmp_path / "logs"
    approvals = tmp_path / "approvals"

    payload = _run_paper_subprocess(
        catalog_path=catalog,
        signals_root=signals,
        logs_root=logs,
        approvals_root=approvals,
        mode="dry_run",
        run_id="paper-dry",
    )

    summary = payload["summary"]
    assert summary["approval_mode"] == "dry_run"
    # Every intent should land in the dry-run bucket; nothing submitted.
    assert summary["intents_generated"] > 0
    assert summary["approvals_dry_run"] == summary["intents_generated"]
    assert summary["approvals_confirmed"] == 0
    assert summary["fills"] == 0


def test_paper_runner_manual_mode_holds_intents_pending(tmp_path: Path) -> None:
    catalog = tmp_path / "catalog"
    signals = tmp_path / "signals"
    logs = tmp_path / "logs"
    approvals = tmp_path / "approvals"

    payload = _run_paper_subprocess(
        catalog_path=catalog,
        signals_root=signals,
        logs_root=logs,
        approvals_root=approvals,
        mode="manual",
        run_id="paper-manual",
    )

    summary = payload["summary"]
    assert summary["approval_mode"] == "manual"
    # Every intent waits for human confirmation; nothing fills.
    assert summary["intents_generated"] > 0
    assert summary["approvals_pending"] == summary["intents_generated"]
    assert summary["approvals_confirmed"] == 0
    assert summary["fills"] == 0

    # The approvals root should now contain at least one shard with
    # `pending` rows.
    shards = list(Path(approvals).glob("*.jsonl"))
    assert shards, f"expected pending-approval shards under {approvals}"
    pending_seen = False
    for shard in shards:
        for line in shard.read_text().splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("status") == "pending":
                pending_seen = True
                break
        if pending_seen:
            break
    assert pending_seen, "expected at least one pending row in approval queue"
