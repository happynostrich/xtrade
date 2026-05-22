"""Subprocess entry point for the Phase 3 replay-parity test (T7).

`tests/test_signal_replay_parity.py` shells out to this module twice —
once per variant — because `BacktestEngine.__init__` aborts the
interpreter on a second instantiation in the same Python process.

Variants
--------
- ``paper``  : straight `run_paper(approval_mode="auto", ...)`.
              `ApprovalGate` is fully exercised (records `confirmed`
              rows in `data/approvals/`), then the runner submits.

- ``direct`` : same `run_paper(...)` call, but `xtrade.approval.ApprovalGate`
              is monkey-patched **before** the call to a pass-through
              stub that never touches the queue. RiskGate still runs.

Both variants seed the *same* synthetic catalog + signals and run the
*same* strategy on the *same* bar window. Fill sequences MUST be
identical (ts_event / symbol / side / qty / price, Decimal-strict)
under the contract that `ApprovalGate.auto` is observation-only.

Invocation
----------
    python -m tests._paper_replay_runner \\
        <catalog_path> <signals_root> <logs_root> <approvals_root> \\
        <variant> <run_id>

The script writes one JSON line to stdout: the run summary plus the
captured `fill_events` list extracted from it.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import sys
from pathlib import Path

from nautilus_trader.model.data import Bar
from nautilus_trader.model.objects import Price, Quantity
from nautilus_trader.test_kit.providers import TestInstrumentProvider

from xtrade.data.catalog import (
    bar_type_for,
    open_catalog,
    parse_bar_spec,
    write_bars,
)
from xtrade.research.signals import Signal, SignalQueue


_MIN_NS = 60 * 1_000_000_000
_START_NS = 1_700_000_000_000_000_000
_UTC = dt.timezone.utc


# ---------------------------------------------------------------------------
# Deterministic seeding (must be byte-identical across variants)
# ---------------------------------------------------------------------------


def _seed_catalog(catalog_path: Path, n: int) -> tuple:
    """Same synthetic 1m BTCUSDT-PERP bars used by the Task-5 subprocess."""
    instrument = TestInstrumentProvider.btcusdt_perp_binance()
    spec = parse_bar_spec("1m")
    bar_type = bar_type_for(instrument, spec)
    pp = instrument.price_precision
    sp = instrument.size_precision

    bars: list[Bar] = []
    for i in range(n):
        ts = _START_NS + i * _MIN_NS
        mid = 30_000.0 + 250.0 * math.sin(i / 4.0)
        open_p = mid
        close_p = mid + 5.0 * math.sin((i + 1) / 4.0)
        hi = max(open_p, close_p) + 2.0
        lo = min(open_p, close_p) - 2.0
        bars.append(
            Bar(
                bar_type=bar_type,
                open=Price.from_str(f"{open_p:.{pp}f}"),
                high=Price.from_str(f"{hi:.{pp}f}"),
                low=Price.from_str(f"{lo:.{pp}f}"),
                close=Price.from_str(f"{close_p:.{pp}f}"),
                volume=Quantity.from_str(f"{1.0 + 0.01 * i:.{sp}f}"),
                ts_event=ts,
                ts_init=ts,
            )
        )

    catalog = open_catalog(catalog_path)
    write_bars(catalog, instrument, bars)
    return instrument, bars


def _seed_signals(signals_root: Path, symbol: str) -> int:
    base = dt.datetime(2023, 11, 14, 22, 13, 20, tzinfo=_UTC)
    sigs = [
        Signal(
            symbol=symbol,
            venue="binance",
            direction="LONG",
            strength=0.5,
            generated_at=base + dt.timedelta(minutes=20),
            source="momentum:replay-a",
        ),
        Signal(
            symbol=symbol,
            venue="binance",
            direction="SHORT",
            strength=-0.5,
            generated_at=base + dt.timedelta(minutes=60),
            source="momentum:replay-b",
        ),
        Signal(
            symbol=symbol,
            venue="binance",
            direction="FLAT",
            strength=0.0,
            generated_at=base + dt.timedelta(minutes=120),
            source="momentum:replay-c",
        ),
    ]
    return SignalQueue(signals_root).append(sigs)


# ---------------------------------------------------------------------------
# Variant: monkey-patch ApprovalGate to a pure pass-through stub
# ---------------------------------------------------------------------------


def _install_passthrough_approval_gate() -> None:
    """Replace `xtrade.approval.ApprovalGate` with a no-op stub.

    Must be called BEFORE importing `xtrade.strategy.runner` (which does
    a deferred `from xtrade.approval import ApprovalGate` inside its
    `run_paper`). We patch the source module so the deferred import
    resolves to our stub.
    """
    import xtrade.approval as approval_mod
    from xtrade.approval.gate import ApprovalDecision

    class _PassthroughApprovalGate:
        """Always returns `go=True` immediately; touches no disk."""

        def __init__(self, mode, queue_root):  # noqa: ANN001 — match signature
            self.mode = "auto"  # surface as auto in any summary that asks
            self.queue_root = queue_root

        def decide(self, intent, *, now=None):  # noqa: ANN001
            # Deterministic synthetic record id from the intent fingerprint
            # so two `direct` runs are byte-stable.
            try:
                rec_id = intent.fingerprint()[:16]
            except Exception:  # pragma: no cover
                rec_id = "deadbeefdeadbeef"
            return ApprovalDecision(
                go=True,
                awaiting=False,
                record_id=rec_id,
                status="confirmed",
                mode="auto",
            )

    approval_mod.ApprovalGate = _PassthroughApprovalGate  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: list[str]) -> int:
    if len(argv) != 7:
        print(
            "usage: python -m tests._paper_replay_runner "
            "<catalog> <signals_root> <logs_root> <approvals_root> "
            "<variant> <run_id>",
            file=sys.stderr,
        )
        return 2

    catalog_path = Path(argv[1])
    signals_root = Path(argv[2])
    logs_root = Path(argv[3])
    approvals_root = Path(argv[4])
    variant = argv[5]
    run_id = argv[6]

    if variant not in {"paper", "direct"}:
        print(f"unknown variant {variant!r}", file=sys.stderr)
        return 2

    _seed_catalog(catalog_path, n=200)
    symbol = "BTCUSDT-PERP.BINANCE"
    _seed_signals(signals_root, symbol)

    if variant == "direct":
        # Replace ApprovalGate BEFORE run_paper triggers its deferred
        # import.
        _install_passthrough_approval_gate()

    # Import after the patch (run_paper does a lazy import inside, but
    # the lookup happens at call time so this ordering is correct
    # either way).
    from xtrade.strategy.runner import run_paper

    result = run_paper(
        strategy_name="momentum_follow",
        catalog_path=catalog_path,
        instrument_id=symbol,
        bar="1m",
        signals_root=signals_root,
        approval_mode="auto",
        risk_rules=[],
        strategy_config={"notional_usd": "500", "qty_step": "0.001"},
        starting_balance=1_000_000,
        approvals_root=approvals_root,
        run_id=run_id,
        logs_root=logs_root,
    )

    payload = {
        "variant": variant,
        "summary": result.summary,
        "fill_events": result.summary.get("fill_events", []),
    }
    sys.stdout.write(json.dumps(payload, default=str) + "\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
