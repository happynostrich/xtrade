#!/usr/bin/env python3
"""02_state_gc.py — archive signal jsonl shards older than N days.

Per docs/phase4_brief.md §2 T3:
  > scripts/phase4/02_state_gc.py 提供 90 天以前的 signals jsonl 归档到
  > /var/lib/xtrade/archive/ 的可选清理任务。

What this does
--------------
Walks ``$VAR_XTRADE/signals/*.jsonl`` (shard names are ``YYYY-MM-DD.jsonl``),
parses the date from the filename, and for any file older than
``--days`` (default 90) **moves** it under ``$VAR_XTRADE/archive/signals/``.

The move is atomic (``os.replace``) so a crash mid-run never leaves a
partially-archived shard. The script never touches:

- ``approvals/`` — those are audit-grade for the life of the venue
  account (no time-bound retention).
- ``logs/`` — handled by logrotate (``deploy/logrotate/xtrade``).
- ``catalog/`` — parquet files are immutable and managed by the data
  ingest layer.
- The signals cursor file (``.cursor``) — required for restart
  determinism.

Safe to run while xtrade-supervisor is live: only **read** files are
the per-day jsonl shards; the supervisor only writes to **today's**
shard, which by definition is not older than N days.

Usage
-----
  python3 scripts/phase4/02_state_gc.py
  python3 scripts/phase4/02_state_gc.py --days 90 --var /var/lib/xtrade
  python3 scripts/phase4/02_state_gc.py --dry-run

Exit codes
----------
  0  archived 0 or more files, no errors
  1  one or more files failed to move (still readable, no data loss)
  2  bad arguments / target directory missing
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import sys
from pathlib import Path


SHARD_NAME_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})\.jsonl$")


def parse_shard_date(name: str) -> dt.date | None:
    """Return the date encoded in ``YYYY-MM-DD.jsonl`` or None on mismatch."""
    match = SHARD_NAME_RE.match(name)
    if match is None:
        return None
    try:
        return dt.date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


def find_archivable(
    signals_root: Path,
    *,
    today: dt.date,
    days: int,
) -> list[Path]:
    """Return shards strictly older than `today - days`."""
    if not signals_root.is_dir():
        return []
    cutoff = today - dt.timedelta(days=days)
    out: list[Path] = []
    for entry in sorted(signals_root.iterdir()):
        if not entry.is_file():
            continue
        shard_date = parse_shard_date(entry.name)
        if shard_date is None:
            continue
        if shard_date < cutoff:
            out.append(entry)
    return out


def archive_files(
    files: list[Path],
    *,
    archive_root: Path,
    dry_run: bool = False,
) -> tuple[list[Path], list[tuple[Path, str]]]:
    """Move each file into ``archive_root``. Atomic via ``os.replace``.

    Returns ``(moved, errors)`` where ``errors`` is a list of
    ``(path, error_message)`` for the files that could not be archived.
    On dry-run, ``moved`` lists the destinations that would have been
    written (the source files are not touched).
    """
    moved: list[Path] = []
    errors: list[tuple[Path, str]] = []
    if not files:
        return moved, errors

    if not dry_run:
        archive_root.mkdir(parents=True, exist_ok=True)

    for source in files:
        target = archive_root / source.name
        if target.exists():
            errors.append((source, f"refusing to overwrite existing archive: {target}"))
            continue
        if dry_run:
            moved.append(target)
            continue
        try:
            os.replace(source, target)
        except OSError as exc:
            errors.append((source, str(exc)))
            continue
        moved.append(target)
    return moved, errors


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument(
        "--var",
        default=os.environ.get("VAR_XTRADE", "/var/lib/xtrade"),
        type=Path,
        help="State root (default: $VAR_XTRADE or /var/lib/xtrade).",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=90,
        help="Move shards strictly older than N days (default: 90).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would move without touching anything.",
    )
    parser.add_argument(
        "--today",
        default=None,
        help="Override today's UTC date as YYYY-MM-DD (for tests).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    if args.days <= 0:
        print(f"error: --days must be positive, got {args.days}", file=sys.stderr)
        return 2

    signals_root = args.var / "signals"
    archive_root = args.var / "archive" / "signals"

    if not args.var.is_dir():
        print(f"error: state root not found: {args.var}", file=sys.stderr)
        return 2

    if args.today is not None:
        try:
            today = dt.date.fromisoformat(args.today)
        except ValueError:
            print(f"error: --today not ISO YYYY-MM-DD: {args.today}", file=sys.stderr)
            return 2
    else:
        today = dt.datetime.now(tz=dt.timezone.utc).date()

    files = find_archivable(signals_root, today=today, days=args.days)
    if not files:
        print(f"state-gc: nothing to archive (cutoff: > {args.days} days, today: {today})")
        return 0

    moved, errors = archive_files(files, archive_root=archive_root, dry_run=args.dry_run)
    verb = "would move" if args.dry_run else "moved"
    for path in moved:
        print(f"state-gc: {verb} -> {path}")
    for path, message in errors:
        print(f"state-gc: ERROR archiving {path}: {message}", file=sys.stderr)

    if errors:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
