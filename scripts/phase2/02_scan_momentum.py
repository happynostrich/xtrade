#!/usr/bin/env python3
"""Phase 2 verification script — run a single momentum scan.

Thin wrapper around `xtrade scan run --scanner momentum`. Useful for
operators who want a quick "did the scan produce signals on today's
catalog?" check without typing the full CLI invocation.

Exit codes mirror the CLI contract (P7 / S6):
  0  PASS — scan completed, signals written (or no signals + non-strict)
  1  FAIL — scan ran but produced zero signals in --strict mode
  2  config error
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_UNIVERSE = REPO_ROOT / "config" / "universe.example.yaml"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--universe", type=Path, default=DEFAULT_UNIVERSE,
        help=f"Universe yaml (default: {DEFAULT_UNIVERSE}).",
    )
    parser.add_argument("--bar", default="1m", help="Bar spec (default 1m).")
    parser.add_argument("--since", default=None, help="ISO date/time lower bound.")
    parser.add_argument("--until", default=None, help="ISO date/time upper bound.")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--run-id", default=None)
    args = parser.parse_args()

    from xtrade.cli import app
    from typer.testing import CliRunner

    runner = CliRunner(mix_stderr=False)
    cli_args = [
        "scan", "run",
        "--universe", str(args.universe),
        "--scanner", "momentum",
        "--bar", args.bar,
        "--top-k", str(args.top_k),
    ]
    if args.since:
        cli_args += ["--since", args.since]
    if args.until:
        cli_args += ["--until", args.until]
    if args.strict:
        cli_args.append("--strict")
    if args.run_id:
        cli_args += ["--run-id", args.run_id]

    result = runner.invoke(app, cli_args)
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    return result.exit_code


if __name__ == "__main__":
    sys.exit(main())
