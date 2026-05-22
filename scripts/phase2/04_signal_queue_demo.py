#!/usr/bin/env python3
"""Phase 2 verification script — write + read a synthetic signal queue.

Constructs a few `Signal` objects in memory and exercises the on-disk
`SignalQueue` API end-to-end (append + tail + filter). No catalog or
universe required — useful for verifying the persistence layer in
isolation.

Exit codes:
  0  PASS
  2  unexpected validation / IO error
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
import tempfile
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--queue-root",
        type=Path,
        default=None,
        help="Queue root (default: a fresh tmp dir).",
    )
    args = parser.parse_args()

    from xtrade.research.signals import Signal, SignalQueue

    root = args.queue_root if args.queue_root is not None else Path(
        tempfile.mkdtemp(prefix="xtrade-signals-demo-")
    )
    print(f"queue root: {root}")

    queue = SignalQueue(root)
    now = dt.datetime.now(tz=dt.timezone.utc)
    sigs = [
        Signal(
            symbol="BTCUSDT-PERP.BINANCE", venue="binance", direction="LONG",
            strength=0.7, generated_at=now, source="momentum:demo-a",
        ),
        Signal(
            symbol="ETHUSDT-PERP.BINANCE", venue="binance", direction="LONG",
            strength=0.5, generated_at=now, source="momentum:demo-a",
        ),
        Signal(
            symbol="BTCUSDT-PERP.BINANCE", venue="binance", direction="FLAT",
            strength=0.0, generated_at=now + dt.timedelta(minutes=1),
            source="meanrev:demo-b",
        ),
    ]
    written = queue.append(sigs)
    print(f"appended: {written}")

    # Idempotency check.
    written2 = queue.append(sigs)
    print(f"second append (should be 0): {written2}")

    print("tail(2):")
    for s in queue.tail(2):
        print(f"  {s.generated_at.isoformat()} {s.symbol} {s.direction} {s.source}")

    print("filter(source=meanrev:demo-b):")
    for s in queue.filter(source="meanrev:demo-b"):
        print(f"  {s.symbol} {s.direction}")

    if written != len(sigs) or written2 != 0:
        print("FAIL: queue did not dedup correctly", file=sys.stderr)
        return 2
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
