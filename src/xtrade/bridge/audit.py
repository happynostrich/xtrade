"""Bridge outbound audit jsonl writer (Phase 5 / Track A2).

Every call to `OpenclawBridge.dispatch(...)` results in 1..N rows here,
one per HTTP attempt plus a terminal row when the loop exhausts. The
file shards daily under UTC (`bridge_out.<YYYY-MM-DD>.jsonl`) so
logrotate / cold-storage workflows can sweep older shards.

Why O_APPEND + a single `write()`
---------------------------------
POSIX guarantees that writes opened with `O_APPEND` are atomic with
respect to each other up to `PIPE_BUF` (4 KiB on Linux). Our envelope
stays well under that, so multiple supervisor restarts (or, in the
distant future, multiple writer processes) can append concurrently
without tearing lines. The writer is also process-fork safe — each
`.write()` call opens / writes / closes a fresh fd.

What gets written
-----------------
One JSON object per line, with these stable fields::

    {
        "approval_id": "AP-2026...",
        "attempt": 2,
        "kind": "retry",            # "ok" | "retry" | "fail" | "refused"
        "status_code": 503,         # nullable
        "error": "http-503: ...",   # nullable
        "dispatched_at": "2026-05-24T12:00:00+00:00",
        "elapsed_s": 0.412,
        "response_excerpt": "..."    # nullable, truncated
    }

Secrets policy
--------------
The caller is responsible for scrubbing the `error` and
`response_excerpt` fields. The writer trusts its inputs but never
embeds the raw payload or `Authorization` header in the envelope.

This module is intentionally dependency-free; importing it does not
pull `httpx`, `nautilus`, or other heavy deps.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import os
from pathlib import Path
from typing import Literal


UTC = dt.timezone.utc

AuditKind = Literal["ok", "retry", "fail", "refused"]

_ENVELOPE_FILE_MODE = 0o640
_ENVELOPE_DIR_MODE = 0o750


@dataclasses.dataclass(frozen=True)
class BridgeAuditRow:
    """Shape of one audit jsonl row (debug / test helper)."""

    approval_id: str
    attempt: int
    kind: AuditKind
    status_code: int | None
    error: str | None
    dispatched_at: dt.datetime
    elapsed_s: float
    response_excerpt: str | None

    def to_envelope(self) -> dict[str, object]:
        return {
            "approval_id": self.approval_id,
            "attempt": self.attempt,
            "kind": self.kind,
            "status_code": self.status_code,
            "error": self.error,
            "dispatched_at": self.dispatched_at.astimezone(UTC).isoformat(),
            "elapsed_s": round(float(self.elapsed_s), 3),
            "response_excerpt": self.response_excerpt,
        }


class BridgeAuditWriter:
    """Append-only jsonl writer for bridge dispatch attempts.

    Construct once per supervisor and pass into `OpenclawBridge`; the
    writer is stateless beyond the audit_root path so multiple bridges
    can safely share one instance.
    """

    def __init__(self, audit_root: Path) -> None:
        self._audit_root = Path(audit_root)

    @property
    def audit_root(self) -> Path:
        return self._audit_root

    def write(
        self,
        *,
        approval_id: str,
        attempt: int,
        kind: AuditKind,
        status_code: int | None,
        error: str | None,
        dispatched_at: dt.datetime,
        elapsed_s: float,
        response_excerpt: str | None = None,
    ) -> Path:
        """Append one audit row to today's UTC shard. Returns the path."""
        if kind not in ("ok", "retry", "fail", "refused"):
            raise ValueError(f"invalid kind: {kind!r}")
        if dispatched_at.tzinfo is None:
            raise ValueError("dispatched_at must be timezone-aware (UTC)")

        row = BridgeAuditRow(
            approval_id=approval_id,
            attempt=int(attempt),
            kind=kind,
            status_code=status_code,
            error=error,
            dispatched_at=dispatched_at,
            elapsed_s=float(elapsed_s),
            response_excerpt=response_excerpt,
        )

        shard = self._shard_path(dispatched_at)
        # `mkdir` is idempotent; `parents=True` covers the first call.
        self._audit_root.mkdir(parents=True, exist_ok=True, mode=_ENVELOPE_DIR_MODE)

        line = json.dumps(row.to_envelope(), sort_keys=True, separators=(",", ":")) + "\n"
        data = line.encode("utf-8")

        # Single open/write/close: O_APPEND guarantees atomic positioning,
        # one write() call keeps the payload under PIPE_BUF.
        fd = os.open(
            str(shard),
            os.O_WRONLY | os.O_APPEND | os.O_CREAT,
            _ENVELOPE_FILE_MODE,
        )
        try:
            os.write(fd, data)
        finally:
            os.close(fd)
        return shard

    def _shard_path(self, when: dt.datetime) -> Path:
        day = when.astimezone(UTC).strftime("%Y-%m-%d")
        return self._audit_root / f"bridge_out.{day}.jsonl"
