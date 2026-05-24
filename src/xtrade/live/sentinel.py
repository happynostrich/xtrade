"""Sentinel — file-backed pause/resume flag for the live supervisor.

The supervisor reads this flag once per poll iteration. When the
sentinel file exists the supervisor still processes openclaw callbacks
and bridge dispatches (so a stuck dispatch can be retried), but it
**refuses to submit any new venue orders** — every new signal is
recorded in the journal as `paused, dropping signal` and the queue
cursor is **not** advanced, so when the operator runs `xtrade ops
resume` the queued signals replay.

The file lives at the path the operator chooses (default on VPS:
`/run/xtrade/paused.flag`, which is tmpfs and therefore wiped on
reboot — by design, so a reboot doesn't strand the supervisor in
paused state).

File format
-----------
A single UTF-8 JSON object:

    {"paused_at": "<ISO8601 UTC>", "reason": "<optional free text>"}

Idempotent. Re-pausing while paused updates `paused_at`/`reason` in
place; resuming an already-resumed sentinel is a no-op.

Atomicity
---------
Writes go through the same `tempfile.mkstemp` → `fsync` → `os.replace`
template the rest of xtrade uses, so a crash mid-write can never leave
a half-written sentinel visible.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import tempfile
from pathlib import Path
from typing import Any


class Sentinel:
    """File-backed pause/resume flag the supervisor consults each loop."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    # ----- read --------------------------------------------------------

    def paused(self) -> bool:
        """Return True iff a sentinel file exists at `self.path`.

        We deliberately do not parse the body here: a zero-byte or
        corrupt sentinel is still a pause signal. The operator can use
        :meth:`state` to inspect the body (which is best-effort).
        """
        return self.path.exists()

    def state(self) -> dict[str, Any] | None:
        """Return the parsed sentinel body, or `None` if not paused."""
        if not self.path.exists():
            return None
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"paused_at": None, "reason": "<unreadable sentinel>"}

    # ----- write -------------------------------------------------------

    def pause(
        self,
        reason: str = "",
        *,
        now: dt.datetime | None = None,
    ) -> dict[str, Any]:
        """Create/overwrite the sentinel file atomically. Returns the body."""
        when = now or dt.datetime.now(tz=dt.timezone.utc)
        body = {
            "paused_at": when.astimezone(dt.timezone.utc).isoformat(),
            "reason": reason,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_json(self.path, body)
        return body

    def resume(self) -> bool:
        """Delete the sentinel file. Returns True iff a sentinel was removed."""
        try:
            self.path.unlink()
        except FileNotFoundError:
            return False
        return True


# ---- internals --------------------------------------------------------


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    fd, tmp_name = tempfile.mkstemp(
        prefix=f"{path.stem}.",
        suffix=".json.tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
