"""ML-gate jsonl audit writer (Phase 5 / Track C2).

Why a dedicated jsonl writer
----------------------------
`xtrade.obs.emit_event` lands events on journalctl (`logger.log(level,
"%s", line)`). The ops surface — `xtrade ops status` — is intentionally
**pure-filesystem** so it keeps working when the supervisor is dead.
journalctl is not a filesystem path; ops cannot scan it.

To expose ML-gate volume (`allowed_24h` / `suppressed_24h`) on the ops
surface we therefore mirror each allow / suppress decision to a small,
append-only jsonl file using the same pattern as `BridgeAuditWriter`
(Phase 5 Track A2 — see `src/xtrade/bridge/audit.py` once it lands):

    /var/lib/xtrade/audit/ml_gate.<YYYY-MM-DD>.jsonl

Each line is one decision:

    {"ts": "<iso-utc-Z>", "kind": "allowed"|"suppressed",
     "symbol": "...", "side": "BUY"|"SELL",
     "score": 0.781234, "threshold": 0.55,
     "reason": "...", "source_signal_id": "...|...|..."}

Atomic append
-------------
We use ``os.open(..., O_WRONLY|O_APPEND|O_CREAT, 0o640)`` plus a single
``os.write(line + "\\n")``. POSIX guarantees that ``O_APPEND`` writes
shorter than ``PIPE_BUF`` (≥ 512 B, typically 4 KiB) are atomic — our
~250 B envelopes fit comfortably. We never modify historical rows, so
the worst observable corruption is a half-written final line if the
process dies mid-write.

No-op fallback
--------------
``MLGateAuditWriter.if_enabled(None)`` returns ``None``. Strategies hold
``audit_writer: MLGateAuditWriter | None`` and skip the call when None.
This keeps paper / unit-test paths from needing an audit_root.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
from typing import Literal


_LineKind = Literal["allowed", "suppressed"]


class MLGateAuditWriter:
    """Atomic-append jsonl writer for ML-gate decisions."""

    def __init__(self, audit_root: Path | str) -> None:
        self.audit_root = Path(audit_root)
        # mkdir is best-effort; permission errors are propagated so the
        # operator notices before silent loss of audit data.
        self.audit_root.mkdir(parents=True, exist_ok=True)

    @classmethod
    def if_enabled(cls, audit_root: Path | str | None) -> "MLGateAuditWriter | None":
        """Construct only if ``audit_root`` is non-None.

        Lets strategies do ``self._audit = MLGateAuditWriter.if_enabled(cfg.get("audit_root"))``
        and then ``if self._audit: self._audit.write(...)``.
        """
        return cls(audit_root) if audit_root is not None else None

    # ------------------------------------------------------------------

    def write(
        self,
        *,
        kind: _LineKind,
        symbol: str,
        side: str,
        score: float,
        threshold: float,
        reason: str,
        source_signal_id: str | None,
        ts: dt.datetime | None = None,
    ) -> None:
        if kind not in ("allowed", "suppressed"):
            raise ValueError(
                f"kind must be 'allowed' or 'suppressed', got {kind!r}"
            )
        ts_utc = (ts or dt.datetime.now(dt.timezone.utc)).astimezone(dt.timezone.utc)
        path = self.audit_root / f"ml_gate.{ts_utc.date().isoformat()}.jsonl"
        line = json.dumps(
            {
                "ts": ts_utc.replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "kind": kind,
                "symbol": symbol,
                "side": side,
                "score": round(float(score), 6),
                "threshold": round(float(threshold), 6),
                "reason": reason,
                "source_signal_id": source_signal_id,
            },
            sort_keys=True,
            ensure_ascii=False,
        )
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o640)
        try:
            os.write(fd, (line + "\n").encode("utf-8"))
        finally:
            os.close(fd)
