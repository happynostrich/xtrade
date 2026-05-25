"""Disk capacity guard for `/var/lib/xtrade` (Phase 5 Track A4).

Why this lives in `xtrade.ops`
------------------------------
The supervisor writes every signal, approval, log shard and audit row
under `/var/lib/xtrade`. When that filesystem fills up, the supervisor
must **stop submitting new venue orders** — otherwise we lose audit
trail integrity (atomic appends start failing mid-write, sentinel /
cursor writes can leave half-written tmp files, the bridge audit can
miss outbound dispatches).

This module is the single source of truth for "how full is the data
volume right now?" — used by:

- `xtrade ops status` to surface a one-liner to the operator;
- `xtrade.live.supervisor` to halt the iteration loop and write the
  pause sentinel when the volume crosses the halt threshold.

Failure mode
------------
`check_disk` is "soft" — when the path is missing, unreadable, or
`shutil.disk_usage` raises (e.g. on a network volume that briefly
disappeared), it returns a `DiskState` with `used_pct=0`,
`free_bytes=0`, and **both flags False**. We intentionally fail open
on the halt flag: a transient OS-level error must not trip the
supervisor's pause sentinel — that would amplify the outage. The
operator sees `disk used_pct=0 warning=false halt=false` in
`xtrade ops status`, which is recognisably "I can't tell" and not
"all clear".
"""

from __future__ import annotations

import dataclasses
import shutil
from pathlib import Path


@dataclasses.dataclass(frozen=True)
class DiskState:
    """One-shot disk-usage snapshot for a single mount point.

    Attributes
    ----------
    path:
        The path inspected (kept verbatim so renderers can show which
        volume produced the numbers, useful when an operator has more
        than one xtrade install).
    used_pct:
        Integer percent used, 0..100. `0` when the probe failed.
    free_bytes:
        Bytes free on the filesystem. `0` when the probe failed.
    warning:
        True when `used_pct >= warn_pct`. Intended for the
        `xtrade ops status` operator one-liner; does NOT pause the
        supervisor on its own.
    halt:
        True when `used_pct >= halt_pct`. The supervisor turns this
        into a `sentinel.pause(reason="disk-exhausted")` + a
        `supervisor.disk.exhausted` event in `_supervisor_iteration`.
    """

    path: Path
    used_pct: int
    free_bytes: int
    warning: bool
    halt: bool


def check_disk(
    path: Path,
    *,
    warn_pct: int = 80,
    halt_pct: int = 90,
) -> DiskState:
    """Probe a mount point and decide warn / halt status.

    Parameters
    ----------
    path:
        Directory on the filesystem to inspect (typically
        `/var/lib/xtrade`). The path itself need not be the mount
        root — `shutil.disk_usage` resolves to the containing mount.
    warn_pct:
        Threshold (inclusive) at which `warning` flips True. Operator-
        visible only; does not pause the supervisor.
    halt_pct:
        Threshold (inclusive) at which `halt` flips True. The
        supervisor reacts by writing the pause sentinel.

    Returns
    -------
    `DiskState` with safe-default zeros + both flags False when the
    probe fails. See module docstring for the rationale.
    """
    try:
        usage = shutil.disk_usage(str(path))
    except (OSError, ValueError):
        return DiskState(
            path=path,
            used_pct=0,
            free_bytes=0,
            warning=False,
            halt=False,
        )

    total = usage.total
    used = usage.used
    free = usage.free
    if total <= 0:
        # Defensive: a zero-sized total would divide-by-zero. Treat as
        # "can't tell" the same way an OSError would.
        return DiskState(
            path=path,
            used_pct=0,
            free_bytes=0,
            warning=False,
            halt=False,
        )

    # Integer percent — operators read this in `xtrade ops status` and
    # decimals add noise (`63.4%` vs `63%`). We round down so a
    # nominally "80 %" warn threshold doesn't flip on 79.6 %.
    used_pct = int((used * 100) // total)
    # Clamp to [0, 100] — a network volume can briefly report a used
    # value slightly larger than total during a remount race.
    if used_pct < 0:
        used_pct = 0
    elif used_pct > 100:
        used_pct = 100

    return DiskState(
        path=path,
        used_pct=used_pct,
        free_bytes=int(free),
        warning=used_pct >= warn_pct,
        halt=used_pct >= halt_pct,
    )


__all__ = ["DiskState", "check_disk"]
