#!/usr/bin/env python3
"""Phase 1 Task 6 verification script — live testnet order probe.

A thin wrapper around `xtrade live run` that loads
`config/venues.testnet.yaml`, places one far-from-market limit order
on the requested instrument, waits for accept + cancel, and prints a
one-line PASS / FAIL.

Exit codes mirror the CLI contract (P7):
  0  PASS — order accepted and canceled within the timeout
  1  FAIL — order rejected, or lifecycle incomplete by timeout
  2  config / precondition error (missing yaml, missing env var,
     mainnet routing detected, etc.)
"""

from __future__ import annotations

import argparse
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_YAML = REPO_ROOT / "config" / "venues.testnet.yaml"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--instrument",
        required=True,
        help="Instrument id, e.g. BTCUSDT.BINANCE or BTC-USD-PERP.HYPERLIQUID.",
    )
    parser.add_argument("--side", default="BUY", help="BUY | SELL.")
    parser.add_argument("--quantity", default="0.001", help="Order size.")
    parser.add_argument(
        "--safety-multiplier",
        default="0.7",
        help="BUY=mult×bid; SELL=ask/mult. Default 0.7 (matches C2-spot).",
    )
    parser.add_argument(
        "--timeout", type=int, default=60, help="Probe timeout (seconds)."
    )
    parser.add_argument(
        "--venues-yaml",
        type=Path,
        default=DEFAULT_YAML,
        help=f"Path to venues yaml (default: {DEFAULT_YAML}).",
    )
    parser.add_argument("--run-id", default=None, help="Override the auto run id.")
    args = parser.parse_args()

    from xtrade.config import ConfigError, MissingCredentialError, load_venues
    from xtrade.live.runner import run_live
    from xtrade.node.factory import MainnetRefusedError

    if args.side.upper() not in ("BUY", "SELL"):
        print(f"error: --side must be BUY or SELL, got {args.side!r}", file=sys.stderr)
        return 2
    try:
        qty = Decimal(args.quantity)
        mult = Decimal(args.safety_multiplier)
    except (InvalidOperation, ValueError) as exc:
        print(f"error: --quantity/--safety-multiplier must be decimals: {exc}", file=sys.stderr)
        return 2

    try:
        venues_cfg = load_venues(args.venues_yaml)
    except (ConfigError, MissingCredentialError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    try:
        result = run_live(
            venues_cfg,
            instrument_id=args.instrument,
            quantity=qty,
            side=args.side.upper(),
            safety_multiplier=mult,
            timeout_s=float(args.timeout),
            run_id=args.run_id,
        )
    except MainnetRefusedError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    s = result.summary
    print(f"run_id:  {s['run_id']}")
    print(f"summary: {result.summary_path}")
    print(f"instrument: {s['instrument_id']}")
    print(f"first_quote: {s['first_quote_iso']}")
    order = s["order"]
    print(
        f"order: accepted={order['accepted']} "
        f"canceled={order['canceled']} rejected={order['rejected']}"
    )

    if result.passed:
        print("PASS")
        return 0
    print("FAIL", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
