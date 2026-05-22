#!/usr/bin/env python3
"""Phase 1 Task 3 verification script — TradingNode health probe.

A thin wrapper around `xtrade.node.health.probe` that loads a per-venue
testnet yaml (Phase 3.5+ layout — see `config/venues.*.testnet.yaml`),
picks the canonical testnet instrument for that venue, and prints a
one-line PASS / FAIL. One invocation drives one venue's TradingNode;
to sweep all three, prefer `xtrade live health` (which chains the
probes sequentially in-process).

Exit codes mirror the CLI contract (P7):
  0  PASS — every probed channel observed a quote within the timeout
  1  FAIL — at least one channel saw no quote
  2  config / precondition error (missing yaml, missing env var, etc.)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

_DEFAULT_INSTRUMENTS = {
    "binance_spot": "BTCUSDT.BINANCE",
    "binance_futures": "BTCUSDT-PERP.BINANCE",
    "hyperliquid": "BTC-USD-PERP.HYPERLIQUID",
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--venues",
        default="binance_spot,binance_futures,hyperliquid",
        help=(
            "Comma-separated venue keys to probe. Only keys actually "
            "populated in the supplied --venues-yaml are exercised."
        ),
    )
    parser.add_argument(
        "--timeout", type=int, default=60, help="Per-channel quote timeout (seconds)."
    )
    parser.add_argument(
        "--venues-yaml",
        type=Path,
        required=True,
        help=(
            "Path to a per-venue testnet yaml (e.g. "
            "config/venues.binance_futures.testnet.yaml). The aggregated "
            "config/venues.testnet.yaml pointer is intentionally empty as "
            "of Phase 3.5 and cannot be loaded directly."
        ),
    )
    parser.add_argument("--run-id", default=None, help="Override the auto run id.")
    args = parser.parse_args()

    from nautilus_trader.model.identifiers import InstrumentId

    from xtrade.config import ConfigError, MissingCredentialError, load_venues
    from xtrade.node.factory import MainnetRefusedError
    from xtrade.node.health import probe

    venue_keys = [v.strip() for v in args.venues.split(",") if v.strip()]
    unknown = [v for v in venue_keys if v not in _DEFAULT_INSTRUMENTS]
    if unknown:
        print(
            f"error: unknown venue keys {unknown}; "
            f"valid: {sorted(_DEFAULT_INSTRUMENTS)}",
            file=sys.stderr,
        )
        return 2

    iids = [InstrumentId.from_str(_DEFAULT_INSTRUMENTS[v]) for v in venue_keys]

    try:
        venues_cfg = load_venues(args.venues_yaml)
    except (ConfigError, MissingCredentialError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    try:
        result = probe(
            venues_cfg,
            instruments=iids,
            timeout_s=float(args.timeout),
            run_id=args.run_id,
        )
    except MainnetRefusedError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(f"run_id:  {result.run_id}")
    print(f"summary: {result.summary_path}")
    for iid_str, entry in result.summary["per_instrument"].items():
        if entry["first_quote_iso"] is None:
            print(f"  {iid_str}: NO QUOTE within {args.timeout}s")
        else:
            print(
                f"  {iid_str}: first_quote={entry['first_quote_iso']} "
                f"(+{entry['first_quote_latency_ms']} ms)"
            )

    if result.passed:
        print("PASS")
        return 0
    print("FAIL", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
