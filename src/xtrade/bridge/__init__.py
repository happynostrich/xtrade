"""Phase 4 openclaw bridge package.

The bridge ferries pending approval requests between xtrade's local file
queue and the operator-facing yuanbao channel. xtrade never speaks to
yuanbao directly: every payload goes through openclaw's Webhooks plugin
which then drives openclaw's TaskFlow → 大管家 → yuanbao push pipeline.

Submodules
----------
- :mod:`xtrade.bridge.schema` — request/response dataclasses and a narrow
  credential leak guard.
- :mod:`xtrade.bridge.openclaw_webhook` — outbound HTTP client (Task 2).
- :mod:`xtrade.bridge.inbound` — localhost HTTP server (Task 3, pending).
"""

from xtrade.bridge.openclaw_webhook import (
    BridgeConfigError,
    DispatchResult,
    OpenclawBridge,
)
from xtrade.bridge.schema import (
    BridgePayload,
    CallbackUrls,
    SecretLeakError,
    build_payload,
    scrub_payload_for_secrets,
)

__all__ = [
    "BridgeConfigError",
    "BridgePayload",
    "CallbackUrls",
    "DispatchResult",
    "OpenclawBridge",
    "SecretLeakError",
    "build_payload",
    "scrub_payload_for_secrets",
]
