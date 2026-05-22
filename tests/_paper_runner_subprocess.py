"""Subprocess entry point for Phase 3 paper-runner tests (Task 5 / T5).

`tests/test_paper_runner.py` shells out to this module because
`nautilus_trader.backtest.engine.BacktestEngine.__init__` aborts the
interpreter on the second call within a single Python process. Same
pattern as `tests/_parity_nautilus_runner.py`.

Invocation:

    python -m tests._paper_runner_subprocess \
        <catalog_path> <signals_root> <logs_root> <approvals_root> \
        <approval_mode> <run_id>

On success the script prints exactly one JSON line on stdout: the
`paper_summary.json` payload produced by `run_paper(...)`.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import sys
from decimal import Decimal
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
from xtrade.strategy.runner import run_paper


_MIN_NS = 60 * 1_000_000_000
_START_NS = 1_700_000_000_000_000_000
_UTC = dt.timezone.utc


def _seed_catalog(catalog_path: Path, n: int) -> tuple:
    """Write `n` synthetic 1m BTCUSDT-PERP bars; return (instrument, bars)."""
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
    """Append a fixed set of LONG/SHORT/FLAT signals; return count."""
    base = dt.datetime(2023, 11, 14, 22, 13, 20, tzinfo=_UTC)
    # _START_NS = 2023-11-14T22:13:20Z (ns); use offsets so signals fall
    # inside the synthetic-bar window.
    sigs = [
        Signal(
            symbol=symbol,
            venue="binance",
            direction="LONG",
            strength=0.5,
            generated_at=base + dt.timedelta(minutes=20),
            source="momentum:aaaaaaaa",
        ),
        Signal(
            symbol=symbol,
            venue="binance",
            direction="SHORT",
            strength=-0.5,
            generated_at=base + dt.timedelta(minutes=60),
            source="momentum:bbbbbbbb",
        ),
        Signal(
            symbol=symbol,
            venue="binance",
            direction="FLAT",
            strength=0.0,
            generated_at=base + dt.timedelta(minutes=120),
            source="momentum:cccccccc",
        ),
    ]
    queue = SignalQueue(signals_root)
    return queue.append(sigs)


def main(argv: list[str]) -> int:
    if len(argv) != 7:
        print(
            "usage: python -m tests._paper_runner_subprocess "
            "<catalog> <signals_root> <logs_root> <approvals_root> "
            "<mode> <run_id>",
            file=sys.stderr,
        )
        return 2

    catalog_path = Path(argv[1])
    signals_root = Path(argv[2])
    logs_root = Path(argv[3])
    approvals_root = Path(argv[4])
    mode = argv[5]
    run_id = argv[6]

    _seed_catalog(catalog_path, n=200)
    symbol = "BTCUSDT-PERP.BINANCE"
    _seed_signals(signals_root, symbol)

    result = run_paper(
        strategy_name="momentum_follow",
        catalog_path=catalog_path,
        instrument_id=symbol,
        bar="1m",
        signals_root=signals_root,
        approval_mode=mode,
        risk_rules=[],
        strategy_config={"notional_usd": "500", "qty_step": "0.001"},
        starting_balance=1_000_000,
        approvals_root=approvals_root,
        run_id=run_id,
        logs_root=logs_root,
    )

    # Emit summary JSON on a single line for the parent test to parse.
    payload = {
        "summary": result.summary,
        "summary_path": str(result.summary_path),
        "log_dir": str(result.log_dir),
        "run_id": result.run_id,
    }
    sys.stdout.write(json.dumps(payload, default=str) + "\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
