"""Replay-parity test (Phase 3 Task 7 / T7).

The Phase 3 paper runner threads every intent through `ApprovalGate`
even in `auto` mode — the gate writes a `confirmed` row to the
approval queue and returns `go=True`. The hard guarantee Phase 3 owes
its downstream is that this bookkeeping is observation-only: the
sequence of *fills* produced by `run_paper(approval_mode="auto", ...)`
must equal the sequence produced by the same `run_paper(...)` call
when `ApprovalGate` is replaced by a pass-through stub that touches
no disk.

If those two fill sequences diverge, `ApprovalGate` has somehow
mutated intent or its control flow no longer follows the brief — both
would invalidate every PnL number Phase 4/5 derives from paper or
testnet runs.

Mechanics
---------
Each subprocess hop runs ONE `BacktestEngine` (Nautilus aborts on a
second instantiation in one process — same constraint that motivates
`tests/_parity_nautilus_runner.py` and `tests/_paper_runner_subprocess.py`).
We shell out once per variant and compare the captured fill events
byte-strict.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def _run_variant(
    *,
    catalog_path: Path,
    signals_root: Path,
    logs_root: Path,
    approvals_root: Path,
    variant: str,
    run_id: str,
) -> dict:
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "tests._paper_replay_runner",
            str(catalog_path),
            str(signals_root),
            str(logs_root),
            str(approvals_root),
            variant,
            run_id,
        ],
        cwd=str(REPO_ROOT),
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise AssertionError(
            f"replay-parity subprocess failed (variant={variant}, "
            f"rc={completed.returncode})\n"
            f"--- stdout ---\n{completed.stdout}\n"
            f"--- stderr ---\n{completed.stderr}\n"
        )
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
    assert payload is not None, (
        f"no JSON payload in {variant!r} stdout: {completed.stdout}"
    )
    return payload


def test_paper_and_direct_paths_produce_identical_fills(tmp_path: Path) -> None:
    """`auto` ApprovalGate must be a pure observer over fills."""
    # Two fully isolated workspaces — same inputs, different fs roots,
    # so the only thing that can differ is the ApprovalGate behaviour.
    paper_work = tmp_path / "paper"
    direct_work = tmp_path / "direct"
    paper_work.mkdir()
    direct_work.mkdir()

    paper_payload = _run_variant(
        catalog_path=paper_work / "catalog",
        signals_root=paper_work / "signals",
        logs_root=paper_work / "logs",
        approvals_root=paper_work / "approvals",
        variant="paper",
        run_id="replay-paper",
    )
    direct_payload = _run_variant(
        catalog_path=direct_work / "catalog",
        signals_root=direct_work / "signals",
        logs_root=direct_work / "logs",
        approvals_root=direct_work / "approvals",
        variant="direct",
        run_id="replay-direct",
    )

    paper_fills = paper_payload["fill_events"]
    direct_fills = direct_payload["fill_events"]

    # Both runs must actually trade — otherwise the assertion below is
    # trivially true.
    assert paper_fills, "paper variant produced no fills; test is degenerate"
    assert direct_fills, "direct variant produced no fills; test is degenerate"

    # Fill counts match — first cheap invariant.
    assert len(paper_fills) == len(direct_fills), (
        f"fill counts differ: paper={len(paper_fills)} direct={len(direct_fills)}"
    )

    # Per-fill byte-strict comparison: ts_event / symbol / side / qty / price.
    for i, (a, b) in enumerate(zip(paper_fills, direct_fills, strict=True)):
        assert a["ts_event"] == b["ts_event"], (
            f"fill[{i}] ts_event mismatch: paper={a} direct={b}"
        )
        assert a["symbol"] == b["symbol"], f"fill[{i}] symbol mismatch"
        assert a["side"] == b["side"], f"fill[{i}] side mismatch"
        # qty / price are stringified Decimals — string equality is the
        # Decimal-strict comparison the brief asks for.
        assert a["qty"] == b["qty"], f"fill[{i}] qty mismatch: {a} vs {b}"
        assert a["price"] == b["price"], f"fill[{i}] price mismatch: {a} vs {b}"

    # And: every other counter the runner exposes must match too. NAV +
    # PnL are pure functions of fills × marks so they're a secondary
    # invariant of the same property.
    pa = paper_payload["summary"]
    pb = direct_payload["summary"]
    for key in (
        "bars_loaded",
        "signals_consumed",
        "intents_generated",
        "risk_rejected",
        "fills",
        "final_cash_usd",
        "final_position_qty",
        "final_nav_usd",
        "peak_nav_usd",
    ):
        assert pa[key] == pb[key], (
            f"summary[{key!r}] differs: paper={pa[key]!r} direct={pb[key]!r}"
        )


def test_paper_summary_includes_fill_events(tmp_path: Path) -> None:
    """The paper summary surfaces per-fill events (Task 7 schema bump)."""
    work = tmp_path / "paper-only"
    work.mkdir()
    payload = _run_variant(
        catalog_path=work / "catalog",
        signals_root=work / "signals",
        logs_root=work / "logs",
        approvals_root=work / "approvals",
        variant="paper",
        run_id="replay-schema",
    )
    summary = payload["summary"]
    assert "fill_events" in summary
    assert isinstance(summary["fill_events"], list)
    # The synthetic catalog + signals reliably produce >= 1 fill.
    assert summary["fill_events"], "expected at least one fill event"
    sample = summary["fill_events"][0]
    for key in ("ts_event", "symbol", "side", "qty", "price"):
        assert key in sample, f"fill_event missing key {key!r}: {sample}"
    # Counts agree with the list length.
    assert summary["fills"] == len(summary["fill_events"])
