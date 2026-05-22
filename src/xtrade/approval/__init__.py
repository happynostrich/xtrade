"""Phase 3 approval module — three-mode gate before any venue submission.

Public surface
--------------
- `ApprovalQueue`: per-day jsonl queue at `<root>/<YYYY-MM-DD>.jsonl`
  with `id, intent, status, created_at, decided_at, reason` records;
  atomic writes + idempotent on `intent.fingerprint()`.
- `ApprovalGate(mode, queue_root)`:
  - `auto` → go=True, no queue write.
  - `dry_run` → go=False, record-only (`status=confirmed` so audits see
    it as "decided", with `mode: dry_run` annotation).
  - `manual` → go=False, write `pending`; CLI `xtrade approve {list,
    confirm, reject}` patches the row.
- `ApprovalDecision`: `(go: bool, id: str | None, awaiting: bool)`.

See `docs/phase3_brief.md` §5 Task 3.
"""

from xtrade.approval.gate import (
    ApprovalDecision,
    ApprovalGate,
    ApprovalMode,
)
from xtrade.approval.queue import (
    ApprovalQueue,
    ApprovalQueueError,
    ApprovalRecord,
)

__all__ = [
    "ApprovalDecision",
    "ApprovalGate",
    "ApprovalMode",
    "ApprovalQueue",
    "ApprovalQueueError",
    "ApprovalRecord",
]
