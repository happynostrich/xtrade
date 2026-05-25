"""Phase 4 ops package — runtime status + pause/resume primitives.

The ops surface is **deliberately decoupled from the supervisor's in-
memory state**. Every field on `OpsStatus` is derived from durable
files on disk (sentinel flag, signal cursor, approvals jsonl, log-
directory summary json) plus an optional systemd probe. This is
non-negotiable: `xtrade ops status` is the first thing an operator
runs **when the supervisor is suspected dead**, so it must not need
to talk to it.

Submodules
----------
- :mod:`xtrade.ops.status` — `OpsPaths`, `OpsStatus`, `collect_status`,
  and human/json renderers used by the CLI.
"""

from xtrade.ops.status import (
    BridgeStatus,
    MLGateStatus,
    OpsPaths,
    OpsStatus,
    SupervisorState,
    collect_status,
    probe_systemd_default,
    render_status_json,
    render_status_text,
)

__all__ = [
    "BridgeStatus",
    "MLGateStatus",
    "OpsPaths",
    "OpsStatus",
    "SupervisorState",
    "collect_status",
    "probe_systemd_default",
    "render_status_json",
    "render_status_text",
]
