"""Bridge payload schema + credential leak guard.

The outbound openclaw payload structure is the cut-and-paste contract
the openclaw operator implements as a TaskFlow controller (see
`docs/phase4_brief.md` §10). We keep it as a tiny dataclass tree rather
than a free dict so any field rename ripples through tests.

The credential regex set is a narrow copy of the one in
`xtrade.research.signals._FORBIDDEN_PATTERNS`. We deliberately do not
import that private symbol — the bridge stays decoupled from the
research package, and the guard remains narrow on both sides
(see docs/phase3_brief.md §6 + docs/phase4_brief.md §6).
"""

from __future__ import annotations

import dataclasses
import re
from typing import Any, Iterable, Mapping

from xtrade.approval.queue import ApprovalRecord


class SecretLeakError(ValueError):
    """Raised when a payload contains a credential-shaped string."""


_FORBIDDEN_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bsk-[A-Za-z0-9]{16,}\b"),     # OpenAI / Anthropic prefix
    re.compile(r"\b0x[0-9a-fA-F]{64}\b"),       # EVM private key
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),        # AWS access key id
)


@dataclasses.dataclass(frozen=True, slots=True)
class CallbackUrls:
    """Localhost callback URLs the bridge embeds in each dispatch.

    openclaw drives these from its TaskFlow once the operator decides
    on yuanbao. The bridge inbound server (Task 3) is what receives
    them.
    """

    confirm_url: str
    reject_url: str
    ttl_s: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "confirm_url": self.confirm_url,
            "reject_url": self.reject_url,
            "ttl_s": self.ttl_s,
        }


@dataclasses.dataclass(frozen=True, slots=True)
class BridgePayload:
    """Outbound POST body to openclaw's `/plugins/webhooks/xtrade`.

    The top-level shape mirrors openclaw's expected webhook contract
    (`action`, `goal`, `metadata`). All xtrade-specific fields live
    under `metadata` so openclaw's controller can pass them straight
    through to TaskFlow.
    """

    action: str
    goal: str
    approval_id: str
    intent: dict[str, Any]
    risk_summary: dict[str, Any]
    callback: CallbackUrls

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "goal": self.goal,
            "metadata": {
                "approval_id": self.approval_id,
                "intent": self.intent,
                "risk_summary": self.risk_summary,
                "callback": self.callback.to_dict(),
            },
        }


def build_payload(
    record: ApprovalRecord,
    *,
    callback_base_url: str,
    ttl_s: int = 900,
    risk_summary: Mapping[str, Any] | None = None,
    goal_override: str | None = None,
) -> BridgePayload:
    """Construct a `BridgePayload` for a pending approval record."""
    base = callback_base_url.rstrip("/")
    callback = CallbackUrls(
        confirm_url=f"{base}/approvals/{record.id}/confirm",
        reject_url=f"{base}/approvals/{record.id}/reject",
        ttl_s=ttl_s,
    )
    intent = record.intent
    direction = intent.side
    qty = str(intent.quantity)
    default_goal = (
        f"xtrade approval: {direction} {qty} {intent.symbol} on "
        f"{intent.venue} (id={record.id})"
    )
    return BridgePayload(
        action="create_flow",
        goal=goal_override or default_goal,
        approval_id=record.id,
        intent=intent.to_dict(),
        risk_summary=dict(risk_summary or {}),
        callback=callback,
    )


def scrub_payload_for_secrets(payload: BridgePayload | Mapping[str, Any]) -> None:
    """Raise `SecretLeakError` if any nested string matches a leak pattern."""
    if isinstance(payload, BridgePayload):
        root: Any = payload.to_dict()
    else:
        root = payload
    stack: list[Any] = [root]
    while stack:
        node = stack.pop()
        if isinstance(node, str):
            for pat in _FORBIDDEN_PATTERNS:
                if pat.search(node):
                    raise SecretLeakError(
                        "bridge payload contains credential-shaped string; "
                        "refusing to dispatch (see xtrade.bridge.schema)"
                    )
        elif isinstance(node, Mapping):
            stack.extend(node.values())
        elif isinstance(node, Iterable) and not isinstance(node, (str, bytes)):
            stack.extend(node)
