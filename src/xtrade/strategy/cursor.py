"""Persistent cursor store for `SignalConsumer` (Phase 3.5 hardening).

Why this module exists separately
---------------------------------
`tests/test_signal_consumer.py::test_consumer_does_not_open_files_directly`
walks the AST of `xtrade.strategy.consumer` and refuses any direct file
I/O (`open`, `read_text`, `read_bytes`, `.open` attribute access). That
guard exists because Phase 2 → Phase 3 contract requires the consumer
to go through `SignalQueue` for signal-shard reads.

Persistent cursor state, however, is a different concern: it's a single
small JSON file recording which dedup keys this consumer has already
yielded, so a restarted process doesn't replay the entire signal queue.
Putting the file I/O in this module keeps the architectural guard on
`consumer.py` intact while still giving the consumer a way to persist.

File format
-----------
```json
{
  "version": 1,
  "updated_at": "2026-05-22T12:34:56+00:00",
  "seen": [
    ["2026-05-22T12:00:00+00:00", "BTCUSDT-PERP.BINANCE", "momentum:abc"],
    ...
  ]
}
```

Each row is the same `(generated_at_iso, symbol, source)` triple
`Signal.dedup_key()` returns. The list is sorted on write for stable
diffs.

Durability
----------
`save()` writes through `tempfile.mkstemp` + `os.fdopen` + `f.flush()` +
`os.fsync(...)` + `os.replace(...)` — the same atomic-write template
used by `SignalQueue.append` and `ApprovalQueue.append`. A crash mid-
write leaves either the old file or no file; never a half-written file.

`load()` is tolerant: a missing file or a corrupt / unparseable file
returns an empty set. Corrupt-file → empty-set is deliberate: it makes
the safe failure mode "replay the queue" rather than "crash on startup".
"""

from __future__ import annotations

import datetime as dt
import json
import os
import tempfile
from pathlib import Path


_VERSION = 1


def load(path: Path) -> set[tuple[str, str, str]]:
    """Load the persisted seen-set from `path`.

    Returns an empty set if the file does not exist or cannot be
    parsed. Never raises.
    """
    if not path.exists():
        return set()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    if not isinstance(payload, dict):
        return set()
    raw = payload.get("seen", [])
    if not isinstance(raw, list):
        return set()
    out: set[tuple[str, str, str]] = set()
    for row in raw:
        if isinstance(row, list) and len(row) == 3 and all(isinstance(c, str) for c in row):
            out.add((row[0], row[1], row[2]))
    return out


def save(
    path: Path,
    seen: set[tuple[str, str, str]],
    *,
    updated_at: dt.datetime | None = None,
) -> None:
    """Atomically persist `seen` to `path`.

    Creates the parent directory if needed. Sorts entries for stable
    diffs.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    when = (updated_at or dt.datetime.now(dt.timezone.utc)).isoformat()
    payload = {
        "version": _VERSION,
        "updated_at": when,
        "seen": sorted([list(t) for t in seen]),
    }
    body = json.dumps(payload, indent=2, sort_keys=True)

    tmp_fd, tmp_name = tempfile.mkstemp(
        prefix=".cursor.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
            fh.write(body)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
