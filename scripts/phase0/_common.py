"""Shared helpers for Phase 0 verification scripts.

These helpers centralize:
  * locating the repo root and `docs/phase0_results.md`
  * appending a structured PASS/FAIL section per check
  * a minimal logger that prints the same content shown in the report
  * a guard that forbids order placement on Binance / Hyperliquid mainnet
"""

from __future__ import annotations

import datetime as _dt
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable

REPO_ROOT = Path(__file__).resolve().parents[2]
RESULTS_PATH = REPO_ROOT / "docs" / "phase0_results.md"


def utc_now_iso() -> str:
    return _dt.datetime.now(tz=_dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


def section(title: str) -> None:
    bar = "=" * len(title)
    print(f"\n{title}\n{bar}")


def banner(check_id: str, name: str) -> None:
    section(f"[{check_id}] {name}")


def append_result(
    check_id: str,
    name: str,
    status: str,
    notes: Iterable[str] | None = None,
) -> None:
    """Append a per-check result block to `docs/phase0_results.md`."""
    assert status in {"PASS", "FAIL", "SKIP", "INFO"}, status
    RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not RESULTS_PATH.exists():
        RESULTS_PATH.write_text(_RESULTS_HEADER, encoding="utf-8")

    lines = [
        "",
        f"### {check_id} — {name}",
        "",
        f"- **Status**: `{status}`",
        f"- **Recorded**: {utc_now_iso()}",
    ]
    if notes:
        lines.append("- **Notes**:")
        for n in notes:
            for sub in str(n).splitlines():
                lines.append(f"    - {sub}" if sub.strip() else "")
    lines.append("")
    with RESULTS_PATH.open("a", encoding="utf-8") as f:
        f.write("\n".join(lines))


_RESULTS_HEADER = """# Phase 0 Results

> This file is generated/appended by `scripts/phase0/*.py`.
> Each check (C1..C6) appears as an `###` section below with a
> `PASS` / `FAIL` / `SKIP` status, timestamp, and notes.

"""


@contextmanager
def stepwise(check_id: str, name: str):
    """Context manager that prints a banner and records a FAIL if an
    unhandled exception propagates."""
    banner(check_id, name)
    try:
        yield
    except KeyboardInterrupt:
        print(f"[{check_id}] interrupted by user")
        append_result(check_id, name, "SKIP", notes=["interrupted by user"])
        raise
    except Exception as exc:  # noqa: BLE001
        print(f"[{check_id}] FAIL: {type(exc).__name__}: {exc}", file=sys.stderr)
        append_result(
            check_id,
            name,
            "FAIL",
            notes=[f"{type(exc).__name__}: {exc}"],
        )
        raise


def assert_not_mainnet_order(*, is_mainnet: bool, will_place_order: bool) -> None:
    """Hard guard. Phase 0 forbids order placement on mainnet."""
    if is_mainnet and will_place_order:
        raise RuntimeError(
            "Phase 0 safety guard: a script attempted to place an order on "
            "MAINNET. This is forbidden in Phase 0. Aborting."
        )


async def run_node_until(node, done_event, timeout_s: float) -> None:
    """Drive a TradingNode for at most `timeout_s` seconds, returning as
    soon as `done_event` is set.

    This carefully sequences `run_async` / `stop_async` to avoid the
    common asyncio.run teardown race where the kernel's task-cancellation
    callback fires after the loop has begun closing.
    """
    import asyncio

    run_task = asyncio.create_task(node.run_async())
    try:
        await asyncio.wait_for(done_event.wait(), timeout=timeout_s)
    finally:
        try:
            await node.stop_async()
        except Exception:  # noqa: BLE001
            pass
        # Wait for the run_task to finish cleanly (it should after stop).
        if not run_task.done():
            try:
                await asyncio.wait_for(run_task, timeout=10)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):  # noqa: BLE001
                run_task.cancel()
                try:
                    await run_task
                except BaseException:  # noqa: BLE001
                    pass
