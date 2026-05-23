"""ApprovalQueue — persistent file-backed approval queue (Phase 3 Task 3 / T4).

Format
------
One file per UTC day: `<root>/<YYYY-MM-DD>.jsonl`. Each line is one JSON
record:

    {
      "id": "<16-hex>",                # OrderIntent.fingerprint()
      "intent": {...},                 # OrderIntent.to_dict()
      "status": "pending|confirmed|rejected",
      "created_at": "<ISO8601 UTC>",
      "decided_at": "<ISO8601 UTC>" or null,
      "reason": "",                    # populated on reject
      "mode": "manual|dry_run|auto"    # which gate mode wrote it
    }

Atomic mutation
---------------
Every state change (append a new pending row, flip pending→confirmed,
flip pending→rejected) rewrites the daily file via the same
`tempfile.mkstemp` → `fsync` → `os.replace` dance used by
`xtrade.research.signals.SignalQueue` so a crash mid-write can never
leave a half-written file visible.

Idempotency
-----------
Appending an intent is idempotent **per `(fingerprint, mode)` pair** —
re-submitting the same intent in the same gate mode returns the existing
record, but the same intent submitted in a *different* mode produces a
separate row. This is deliberate: a `dry_run` audit row must not satisfy
a later `manual` approval, and vice versa. Operators using
`xtrade approve confirm <id>` will see the queue contain the same `id`
twice if a dry_run audit + manual decision coexist for one intent; the
confirm/reject path always targets the unique `pending` row.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import os
import tempfile
import warnings
from pathlib import Path
from typing import Any, Iterator, Literal

from xtrade.strategy.intent import OrderIntent


Status = Literal["pending", "confirmed", "rejected"]
_VALID_STATUSES: frozenset[str] = frozenset({"pending", "confirmed", "rejected"})


class ApprovalQueueError(RuntimeError):
    """Raised by `ApprovalQueue` for structural problems."""


@dataclasses.dataclass(frozen=True)
class ApprovalRecord:
    """One row in the approval queue."""

    id: str
    intent: OrderIntent
    status: Status
    created_at: dt.datetime
    decided_at: dt.datetime | None
    reason: str
    mode: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "intent": self.intent.to_dict(),
            "status": self.status,
            "created_at": self.created_at.astimezone(dt.timezone.utc).isoformat(),
            "decided_at": (
                self.decided_at.astimezone(dt.timezone.utc).isoformat()
                if self.decided_at is not None
                else None
            ),
            "reason": self.reason,
            "mode": self.mode,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ApprovalRecord":
        decided_raw = data.get("decided_at")
        decided_at: dt.datetime | None = None
        if decided_raw is not None:
            decided_at = dt.datetime.fromisoformat(decided_raw)
            if decided_at.tzinfo is None:
                raise ApprovalQueueError("decided_at must be tz-aware")
            decided_at = decided_at.astimezone(dt.timezone.utc)
        created = dt.datetime.fromisoformat(data["created_at"])
        if created.tzinfo is None:
            raise ApprovalQueueError("created_at must be tz-aware")
        status = data["status"]
        if status not in _VALID_STATUSES:
            raise ApprovalQueueError(f"unknown status {status!r}")
        return cls(
            id=data["id"],
            intent=OrderIntent.from_dict(data["intent"]),
            status=status,  # type: ignore[arg-type]
            created_at=created.astimezone(dt.timezone.utc),
            decided_at=decided_at,
            reason=data.get("reason", ""),
            mode=data.get("mode", "manual"),
        )


class ApprovalQueue:
    """File-backed approval queue. Single-writer-safe (per-day)."""

    def __init__(self, root_dir: Path | str) -> None:
        self.root_dir = Path(root_dir)
        self.root_dir.mkdir(parents=True, exist_ok=True)

    # ----- write side ------------------------------------------------------

    def submit(
        self,
        intent: OrderIntent,
        *,
        mode: str,
        status: Status = "pending",
        decided_at: dt.datetime | None = None,
        reason: str = "",
        now: dt.datetime | None = None,
    ) -> ApprovalRecord:
        """Append `intent` (or return existing record if `(fingerprint, mode)` clashes).

        Idempotency is scoped to the `(fingerprint, mode)` pair so a
        `dry_run` audit row never satisfies a later `manual` approval
        (see the module docstring for why). Returns the resulting
        `ApprovalRecord`.
        """
        when = now or dt.datetime.now(tz=dt.timezone.utc)
        day = when.astimezone(dt.timezone.utc).date()
        shard = self._shard_path(day)
        fp = intent.fingerprint()

        rows = self._read_shard(shard)
        for row in rows:
            if row.id == fp and row.mode == mode:
                # Already enqueued in the same mode — caller may re-emit safely.
                return row

        record = ApprovalRecord(
            id=fp,
            intent=intent,
            status=status,
            created_at=when,
            decided_at=decided_at,
            reason=reason,
            mode=mode,
        )
        rows.append(record)
        self._write_shard_atomic(shard, rows)
        return record

    def patch(
        self,
        approval_id: str,
        *,
        status: Status,
        reason: str = "",
        now: dt.datetime | None = None,
    ) -> ApprovalRecord:
        """Flip a `pending` row to `confirmed` / `rejected`."""
        if status not in _VALID_STATUSES:
            raise ApprovalQueueError(f"unknown status {status!r}")
        if status == "pending":
            raise ApprovalQueueError("patch() cannot set status back to pending")
        # Same `approval_id` (= intent fingerprint) may legitimately appear
        # twice when an operator has both a `dry_run` audit row and a
        # `manual` pending row for the same intent. We always target the
        # unique `pending` row — auto/dry_run rows are written as
        # `confirmed` at create time and never transition.
        seen_non_pending: list[str] = []
        for shard in sorted(self.root_dir.glob("*.jsonl")):
            rows = self._read_shard(shard)
            for idx, row in enumerate(rows):
                if row.id != approval_id:
                    continue
                if row.status != "pending":
                    seen_non_pending.append(row.status)
                    continue
                when = now or dt.datetime.now(tz=dt.timezone.utc)
                updated = dataclasses.replace(
                    row,
                    status=status,
                    decided_at=when,
                    reason=reason,
                )
                rows[idx] = updated
                self._write_shard_atomic(shard, rows)
                return updated
        if seen_non_pending:
            raise ApprovalQueueError(
                f"approval {approval_id} has no pending row to "
                f"transition; existing rows are {seen_non_pending}"
            )
        raise ApprovalQueueError(f"approval id {approval_id!r} not found")

    # ----- read side -------------------------------------------------------

    def get(self, approval_id: str) -> ApprovalRecord | None:
        for row in self:
            if row.id == approval_id:
                return row
        return None

    def list(
        self,
        *,
        status: Status | None = None,
        since: dt.datetime | None = None,
    ) -> list[ApprovalRecord]:
        if since is not None and since.tzinfo is None:
            raise ApprovalQueueError("`since` must be tz-aware (UTC)")
        out: list[ApprovalRecord] = []
        for row in self:
            if status is not None and row.status != status:
                continue
            if since is not None and row.created_at < since:
                continue
            out.append(row)
        return out

    def __iter__(self) -> Iterator[ApprovalRecord]:
        for shard in sorted(self.root_dir.glob("*.jsonl")):
            yield from self._read_shard(shard)

    # ----- internals -------------------------------------------------------

    def _shard_path(self, day: dt.date) -> Path:
        return self.root_dir / f"{day.isoformat()}.jsonl"

    def _read_shard(self, path: Path) -> list[ApprovalRecord]:
        if not path.exists():
            return []
        out: list[ApprovalRecord] = []
        with path.open("r", encoding="utf-8") as fh:
            for lineno, line in enumerate(fh, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(ApprovalRecord.from_dict(json.loads(line)))
                except (json.JSONDecodeError, KeyError, ValueError, TypeError, ApprovalQueueError) as exc:
                    warnings.warn(
                        f"skipping corrupt approval at {path.name}:{lineno}: {exc}",
                        RuntimeWarning,
                        stacklevel=3,
                    )
        return out

    def _write_shard_atomic(
        self, path: Path, rows: list[ApprovalRecord]
    ) -> None:
        fd, tmp_name = tempfile.mkstemp(
            prefix=f"{path.stem}.",
            suffix=".jsonl.tmp",
            dir=str(path.parent),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                for row in rows:
                    fh.write(json.dumps(row.to_dict(), sort_keys=True))
                    fh.write("\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_name, path)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
