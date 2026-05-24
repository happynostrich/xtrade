"""OpenclawBridge — outbound HTTP dispatch for pending approvals.

The bridge POSTs a `BridgePayload` to openclaw's Webhooks plugin
(`/plugins/webhooks/xtrade`) authenticated with a Bearer token, then
relies on openclaw's TaskFlow to drive yuanbao. See
`docs/phase4_brief.md` §5 Task 2 + §10 for the wire contract.

Retry policy
------------
Network errors (timeouts, DNS, ECONNREFUSED) and HTTP 5xx → exponential
backoff (1s, 2s, 4s, 8s), max 4 attempts. HTTP 4xx → no retry (caller
mis-formed the payload). On exhaustion the bridge writes a
`dispatch_failed` annotation to the corresponding `ApprovalRecord`'s
shard via `ApprovalQueue.annotate_dispatch_failure(...)`, leaving
`status="pending"` so a human can still `xtrade approve confirm <id>`
to satisfy the request locally.

httpx is the transport: it's already a project dependency (used by
venue health probes) so no new package enters the import graph.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import logging
import time
from typing import Any, Callable, Mapping

import httpx

from xtrade.approval.queue import ApprovalQueue, ApprovalRecord
from xtrade.bridge.schema import (
    BridgePayload,
    SecretLeakError,
    build_payload,
    scrub_payload_for_secrets,
)


log = logging.getLogger("xtrade.bridge.out")


class BridgeConfigError(ValueError):
    """Raised when OpenclawBridge is constructed with incomplete config."""


@dataclasses.dataclass(frozen=True, slots=True)
class DispatchResult:
    """Outcome of one `dispatch(record)` call."""

    approval_id: str
    ok: bool
    status_code: int | None
    attempts: int
    elapsed_s: float
    error: str | None
    response_excerpt: str | None
    dispatched_at: dt.datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            "approval_id": self.approval_id,
            "ok": self.ok,
            "status_code": self.status_code,
            "attempts": self.attempts,
            "elapsed_s": round(self.elapsed_s, 3),
            "error": self.error,
            "response_excerpt": self.response_excerpt,
            "dispatched_at": self.dispatched_at.isoformat(),
        }


class OpenclawBridge:
    """HTTP client for xtrade → openclaw approval requests."""

    DEFAULT_BACKOFFS: tuple[float, ...] = (1.0, 2.0, 4.0, 8.0)
    DEFAULT_CONNECT_TIMEOUT_S: float = 5.0
    DEFAULT_READ_TIMEOUT_S: float = 10.0

    def __init__(
        self,
        *,
        gateway_url: str,
        shared_secret: str,
        callback_base_url: str,
        approvals_queue: ApprovalQueue | None = None,
        client: httpx.Client | None = None,
        backoffs: tuple[float, ...] = DEFAULT_BACKOFFS,
        connect_timeout_s: float = DEFAULT_CONNECT_TIMEOUT_S,
        read_timeout_s: float = DEFAULT_READ_TIMEOUT_S,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], dt.datetime] | None = None,
    ) -> None:
        if not gateway_url.startswith(("http://", "https://")):
            raise BridgeConfigError(
                f"gateway_url must be http(s):// URL, got {gateway_url!r}"
            )
        if not shared_secret:
            raise BridgeConfigError("shared_secret is required (Bearer token)")
        if not callback_base_url.startswith(("http://", "https://")):
            raise BridgeConfigError(
                f"callback_base_url must be http(s):// URL, "
                f"got {callback_base_url!r}"
            )
        if not backoffs:
            raise BridgeConfigError("backoffs tuple must be non-empty")

        self._gateway = gateway_url.rstrip("/")
        self._secret = shared_secret
        self._callback_base = callback_base_url
        self._approvals = approvals_queue
        self._backoffs = backoffs
        self._sleep = sleep
        self._now = now or (lambda: dt.datetime.now(tz=dt.timezone.utc))

        if client is not None:
            self._client = client
            self._owns_client = False
        else:
            self._client = httpx.Client(
                timeout=httpx.Timeout(
                    connect=connect_timeout_s,
                    read=read_timeout_s,
                    write=read_timeout_s,
                    pool=read_timeout_s,
                )
            )
            self._owns_client = True

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str],
        *,
        approvals_queue: ApprovalQueue | None = None,
        **kwargs: Any,
    ) -> "OpenclawBridge":
        """Build a bridge from `os.environ`-style mapping (Task 5 entry point)."""
        try:
            gateway = env["OPENCLAW_GATEWAY"]
            secret = env["OPENCLAW_SHARED_SECRET"]
        except KeyError as exc:
            raise BridgeConfigError(
                f"missing required env var: {exc.args[0]}"
            ) from exc
        callback = env.get("OPENCLAW_CALLBACK_BASE_URL", "http://127.0.0.1:18080")
        return cls(
            gateway_url=gateway,
            shared_secret=secret,
            callback_base_url=callback,
            approvals_queue=approvals_queue,
            **kwargs,
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "OpenclawBridge":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ----- main entry point ------------------------------------------------

    def dispatch(
        self,
        record: ApprovalRecord,
        *,
        risk_summary: Mapping[str, Any] | None = None,
        goal_override: str | None = None,
        ttl_s: int = 900,
    ) -> DispatchResult:
        """POST the record to openclaw with retry; never raises on transport.

        On terminal failure the record's daily shard is annotated with a
        `dispatch_failed` payload and `status` is left as-is. The caller
        decides whether to re-dispatch later.
        """
        try:
            payload = build_payload(
                record,
                callback_base_url=self._callback_base,
                ttl_s=ttl_s,
                risk_summary=risk_summary,
                goal_override=goal_override,
            )
            scrub_payload_for_secrets(payload)
        except SecretLeakError as exc:
            result = self._record_failure(
                record,
                ok=False,
                status_code=None,
                attempts=0,
                elapsed_s=0.0,
                error=f"secret-scrub: {exc}",
                response_excerpt=None,
            )
            log.error("bridge.dispatch refused: %s id=%s", exc, record.id)
            return result

        target = f"{self._gateway}/plugins/webhooks/xtrade"
        headers = {
            "Authorization": f"Bearer {self._secret}",
            "Content-Type": "application/json",
            "User-Agent": "xtrade-bridge/1.0",
        }
        body = payload.to_dict()

        start = time.monotonic()
        last_status: int | None = None
        last_error: str | None = None
        last_excerpt: str | None = None
        attempts = 0

        for idx, backoff in enumerate(self._backoffs):
            attempts = idx + 1
            try:
                resp = self._client.post(target, json=body, headers=headers)
                last_status = resp.status_code
                last_excerpt = resp.text[:200] if resp.text else None
                if 200 <= resp.status_code < 300:
                    elapsed = time.monotonic() - start
                    log.info(
                        "bridge.dispatch ok id=%s status=%d attempts=%d elapsed=%.3fs",
                        record.id, resp.status_code, attempts, elapsed,
                    )
                    return self._record_success(
                        record,
                        status_code=resp.status_code,
                        attempts=attempts,
                        elapsed_s=elapsed,
                        response_excerpt=last_excerpt,
                    )
                if 400 <= resp.status_code < 500:
                    # caller error — payload mis-formed, no retry
                    last_error = f"http-{resp.status_code}: client error (no retry)"
                    break
                last_error = f"http-{resp.status_code}: server error"
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_status = None
                last_error = f"{type(exc).__name__}: {exc}"
                last_excerpt = None

            if idx < len(self._backoffs) - 1:
                log.warning(
                    "bridge.dispatch retry id=%s attempt=%d error=%s sleep=%.1fs",
                    record.id, attempts, last_error, backoff,
                )
                self._sleep(backoff)

        elapsed = time.monotonic() - start
        log.error(
            "bridge.dispatch failed id=%s attempts=%d last=%s elapsed=%.3fs",
            record.id, attempts, last_error, elapsed,
        )
        return self._record_failure(
            record,
            ok=False,
            status_code=last_status,
            attempts=attempts,
            elapsed_s=elapsed,
            error=last_error or "unknown",
            response_excerpt=last_excerpt,
        )

    # ----- result + annotation helpers ------------------------------------

    def _record_success(
        self,
        record: ApprovalRecord,
        *,
        status_code: int,
        attempts: int,
        elapsed_s: float,
        response_excerpt: str | None,
    ) -> DispatchResult:
        result = DispatchResult(
            approval_id=record.id,
            ok=True,
            status_code=status_code,
            attempts=attempts,
            elapsed_s=elapsed_s,
            error=None,
            response_excerpt=response_excerpt,
            dispatched_at=self._now(),
        )
        if self._approvals is not None:
            self._approvals.annotate_dispatch_success(
                record.id, result=result.to_dict()
            )
        return result

    def _record_failure(
        self,
        record: ApprovalRecord,
        *,
        ok: bool,
        status_code: int | None,
        attempts: int,
        elapsed_s: float,
        error: str,
        response_excerpt: str | None,
    ) -> DispatchResult:
        result = DispatchResult(
            approval_id=record.id,
            ok=ok,
            status_code=status_code,
            attempts=attempts,
            elapsed_s=elapsed_s,
            error=error,
            response_excerpt=response_excerpt,
            dispatched_at=self._now(),
        )
        if self._approvals is not None:
            self._approvals.annotate_dispatch_failure(
                record.id, result=result.to_dict()
            )
        return result
