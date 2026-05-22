#!/usr/bin/env python3
"""Phase 2 verification script — run every registered scanner in turn.

Iterates `xtrade.research.scanners.available_scanners()` and shells out
to `xtrade scan run` once per scanner. Useful as a smoke test before
deploying a new universe yaml.

Exit codes:
  0  every scanner exited 0
  1  at least one scanner returned 1 (business failure)
  2  config error before any scanner ran
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
    parser.add_argument("--bar", default="1m")
    parser.add_argument("--top-k", type=int, default=5)
    args = parser.parse_args()

    if not args.universe.exists():
        print(f"error: universe yaml {args.universe} does not exist", file=sys.stderr)
        return 2

    from typer.testing import CliRunner

    from xtrade.cli import app
    from xtrade.research.scanners.base import available_scanners

    runner = CliRunner(mix_stderr=False)
    worst = 0
    for scanner in available_scanners():
        print(f"--- {scanner} ---")
        cli_args = [
            "scan", "run",
            "--universe", str(args.universe),
            "--scanner", scanner,
            "--bar", args.bar,
            "--top-k", str(args.top_k),
        ]
        result = runner.invoke(app, cli_args)
        sys.stdout.write(result.stdout)
        sys.stderr.write(result.stderr)
        worst = max(worst, result.exit_code)
    return worst


if __name__ == "__main__":
    sys.exit(main())
