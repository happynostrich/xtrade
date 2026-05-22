"""ApprovalGate — three-mode wrapper around `ApprovalQueue` (Phase 3 Task 3).

Modes
-----
- `auto`     : pass-through. Returns `ApprovalDecision(go=True, ...)`
               immediately and DOES write a record (status=confirmed,
               mode=auto) so the audit trail still contains the intent.
- `dry_run`  : record-only. Returns `go=False`; record status=confirmed,
               mode=dry_run. Runner records the intent but does not
               submit. Useful for capacity / cost dry-rolls.
- `manual`   : record `pending`; runner must wait for an external
               `xtrade approve confirm <id>` before flipping to
               `confirmed` and re-submitting.

`decide(intent)` is idempotent on `intent.fingerprint()`: re-submitting
the same intent returns the existing record's decision.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from pathlib import Path
from typing import Literal

from xtrade.approval.queue import ApprovalQueue, ApprovalRecord
from xtrade.strategy.intent import OrderIntent


ApprovalMode = Literal["auto", "manual", "dry_run"]
_VALID_MODES: frozenset[str] = frozenset({"auto", "manual", "dry_run"})


@dataclasses.dataclass(frozen=True)
class ApprovalDecision:
    """Result of `ApprovalGate.decide()`.

    `go=True` means the runner is cleared to submit. `awaiting=True`
    means the gate stored a `pending` record and the runner should
    leave the intent unsent until human approval flips it. `record_id`
    is the queue id (used by the CLI / summary) regardless of mode.
    """

    go: bool
    awaiting: bool
    record_id: str
    status: str  # mirrors the queue row's status at decision time
    mode: ApprovalMode


class ApprovalGate:
    """Three-mode approval gate built on `ApprovalQueue`."""

    def __init__(
        self,
        mode: ApprovalMode,
        queue_root: Path | str,
    ) -> None:
        if mode not in _VALID_MODES:
            raise ValueError(
                f"mode must be one of {sorted(_VALID_MODES)}, got {mode!r}"
            )
        self.mode: ApprovalMode = mode
        self.queue = ApprovalQueue(queue_root)

    def decide(
        self,
        intent: OrderIntent,
        *,
        now: dt.datetime | None = None,
    ) -> ApprovalDecision:
        when = now or dt.datetime.now(tz=dt.timezone.utc)
        if self.mode == "auto":
            record = self.queue.submit(
                intent,
                mode="auto",
                status="confirmed",
                decided_at=when,
                now=when,
            )
            return ApprovalDecision(
                go=True,
                awaiting=False,
                record_id=record.id,
                status=record.status,
                mode="auto",
            )
        if self.mode == "dry_run":
            record = self.queue.submit(
                intent,
                mode="dry_run",
                status="confirmed",
                decided_at=when,
                now=when,
            )
            return ApprovalDecision(
                go=False,
                awaiting=False,
                record_id=record.id,
                status=record.status,
                mode="dry_run",
            )
        # manual
        record = self.queue.submit(
            intent,
            mode="manual",
            status="pending",
            now=when,
        )
        return ApprovalDecision(
            go=record.status == "confirmed",  # re-emit after confirm
            awaiting=record.status == "pending",
            record_id=record.id,
            status=record.status,
            mode="manual",
        )

    def pending(self) -> list[ApprovalRecord]:
        return self.queue.list(status="pending")
