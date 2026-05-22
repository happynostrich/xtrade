#!/usr/bin/env python3
"""Phase 2 verification script — inspect a universe yaml.

Thin wrapper around `xtrade.research.universe.load_universe`. Prints a
PASS/FAIL line plus the parsed entries so an operator can sanity-check
that a yaml change didn't drop any symbols.

Exit codes:
  0  PASS — yaml parsed and yields ≥1 symbol
  2  config error — file missing, malformed yaml, unknown venue, etc.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_YAML = REPO_ROOT / "config" / "universe.example.yaml"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_YAML,
        help=f"Path to universe yaml (default: {DEFAULT_YAML}).",
    )
    args = parser.parse_args()

    from xtrade.research.universe import UniverseConfigError, load_universe

    try:
        universe = load_universe(args.config)
    except (FileNotFoundError, UniverseConfigError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(f"universe: {args.config}")
    print(f"size:     {len(universe)}")
    for spec in universe.symbols:
        print(f"  {spec.venue}:{spec.symbol}")
    if len(universe) == 0:
        print("FAIL (empty universe)", file=sys.stderr)
        return 2
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
