"""Localhost-only HTTP server that receives openclaw approval callbacks.

The receiver runs as `xtrade-bridge.service` (see
`deploy/systemd/xtrade-bridge.service.in`). openclaw's TaskFlow POSTs a
human's confirm/reject decision (sourced from yuanbao) to one of:

    POST /approvals/<approval_id>/confirm
    POST /approvals/<approval_id>/reject

The handler patches the matching `ApprovalQueue` row in-place. The
supervisor (running in a separate process) picks the new status up on
its next poll iteration via `_drain_pending_decisions` and either
submits the parked intent or drops it (see `xtrade.live.supervisor`).

Why hand-rolled HTTP (vs FastAPI / uvicorn)?
--------------------------------------------
- xtrade has a hard "no new heavyweight deps" rule for Phase 4 (see
  `docs/phase4_brief.md` §4.2 — only `uv` and `httpx` are admitted).
- The contract surface is two POST routes with fixed schemas; there
  is no path/query/body negotiation worth a framework.
- systemd cgroup unit caps the bridge at 200 MB RAM; the small
  stdlib `http.server` is happy there. uvicorn alone is heavier.

Security envelope
-----------------
- Binds **127.0.0.1 only**; the systemd unit additionally sets
  `IPAddressAllow=127.0.0.0/8 + IPAddressDeny=any`. There is no TLS
  because all traffic is loopback (openclaw lives on the same VPS).
- `Authorization: Bearer <OPENCLAW_INBOUND_SECRET>` compared with
  `hmac.compare_digest` (constant-time) — a missing or wrong header
  returns 401.
- Body size capped at `max_body_bytes` (default 4 KiB) so a runaway
  caller cannot OOM the bridge.
- TTL enforced server-side: rows older than `ttl_s` since
  `created_at` are refused with 404 (forces openclaw to re-issue
  rather than satisfying a stale approval).
- Logs are single-line JSON on `xtrade.bridge.in` (per brief §8) so
  journalctl + grep is the operator surface.

Status codes (operator contract — see brief §10):

    200 OK     — row patched, body `{"status":"confirmed", ...}`
    400        — malformed JSON body
    401        — missing/wrong bearer
    404        — id not found OR ttl expired
    409        — id exists but already decided (idempotent re-post)
    405        — wrong HTTP verb
    413        — body exceeds `max_body_bytes`
    500        — defensive only; handler bugs surface here

Idempotency
-----------
Re-posting a confirm after the row was already confirmed (perhaps
openclaw retried after a transient 502) returns 409 with the row's
existing status in the body so openclaw can short-circuit. The
ApprovalQueue is never re-patched.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import hmac
import json
import logging
import re
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from socketserver import ThreadingMixIn
from typing import Any, Callable

from xtrade.approval.queue import ApprovalQueue, ApprovalQueueError, ApprovalRecord


log = logging.getLogger("xtrade.bridge.in")


# ---------------------------------------------------------------------------
# Config + result types
# ---------------------------------------------------------------------------


_PATH_RE = re.compile(r"^/approvals/(?P<id>[A-Za-z0-9_-]{1,128})/(?P<action>confirm|reject)/?$")
_VALID_ACTIONS: frozenset[str] = frozenset({"confirm", "reject"})


@dataclasses.dataclass(frozen=True)
class InboundConfig:
    """Static configuration for the inbound HTTP server.

    `clock` is a test seam: production passes None, tests inject a
    controllable `datetime` factory so ttl checks are deterministic.
    """

    approvals_root: Path
    shared_secret: str
    bind: str = "127.0.0.1"
    port: int = 18080
    ttl_s: int = 900
    max_body_bytes: int = 4096
    clock: Callable[[], dt.datetime] | None = None

    def __post_init__(self) -> None:
        if not self.shared_secret:
            raise ValueError("shared_secret must be a non-empty string")
        if self.ttl_s <= 0:
            raise ValueError("ttl_s must be > 0")
        if self.max_body_bytes <= 0:
            raise ValueError("max_body_bytes must be > 0")
        if self.bind not in {"127.0.0.1", "::1", "localhost"}:
            # Refuse non-loopback binds defensively. The systemd unit
            # also sets IPAddressDeny=any but defence-in-depth: the
            # process itself must refuse to listen on a public interface.
            raise ValueError(
                f"bind must be loopback (127.0.0.1 / ::1 / localhost); "
                f"got {self.bind!r}"
            )


# ---------------------------------------------------------------------------
# Server factory + run loop
# ---------------------------------------------------------------------------


class _InboundServer(ThreadingHTTPServer):
    """ThreadingHTTPServer carrying the `InboundConfig` for the handler."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, config: InboundConfig) -> None:
        super().__init__(
            (config.bind, config.port), _InboundHandler
        )
        self.config = config
        self._lock = threading.Lock()

    @property
    def lock(self) -> threading.Lock:
        return self._lock


def build_server(config: InboundConfig) -> _InboundServer:
    """Bind + return the server. Caller drives `serve_forever()`.

    Tests bind on `port=0` to pick an ephemeral port, then read the
    actual port off `server.server_address[1]`.
    """
    return _InboundServer(config)


def run_inbound_server(config: InboundConfig) -> None:
    """Production entry: bind + serve forever (blocking).

    Logs a single `bridge.in.start` event on startup and a
    `bridge.in.stop` event on graceful shutdown.
    """
    server = build_server(config)
    bind_host, bind_port = server.server_address[:2]
    log.info(
        json.dumps(
            {
                "event": "bridge.in.start",
                "bind": bind_host,
                "port": bind_port,
                "approvals_root": str(config.approvals_root),
                "ttl_s": config.ttl_s,
            },
            sort_keys=True,
        )
    )
    try:
        server.serve_forever()
    finally:
        server.server_close()
        log.info(
            json.dumps(
                {"event": "bridge.in.stop", "bind": bind_host, "port": bind_port},
                sort_keys=True,
            )
        )


# ---------------------------------------------------------------------------
# Request handler
# ---------------------------------------------------------------------------


class _InboundHandler(BaseHTTPRequestHandler):
    """Tiny POST-only handler for `/approvals/<id>/{confirm,reject}`.

    Everything is processed synchronously; the `ThreadingHTTPServer`
    parent handles request-level fan-out so a hung client cannot stall
    the next request.
    """

    server_version = "xtrade-bridge/1.0"
    # Silence Python's default stderr access log — we already emit a
    # structured json log line per request.
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return

    # ---- public entry points -----------------------------------------

    def do_POST(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler API)
        self._handle("POST")

    def do_GET(self) -> None:  # noqa: N802
        # GET is not part of the contract; refuse cleanly so a stray
        # liveness probe doesn't leak our routes via 404 differences.
        self._send_json(HTTPStatus.METHOD_NOT_ALLOWED, {"code": "method_not_allowed"})

    def do_PUT(self) -> None:  # noqa: N802
        self._send_json(HTTPStatus.METHOD_NOT_ALLOWED, {"code": "method_not_allowed"})

    def do_DELETE(self) -> None:  # noqa: N802
        self._send_json(HTTPStatus.METHOD_NOT_ALLOWED, {"code": "method_not_allowed"})

    # ---- core dispatch -----------------------------------------------

    def _handle(self, method: str) -> None:
        config: InboundConfig = self.server.config  # type: ignore[attr-defined]
        match = _PATH_RE.match(self.path)
        if match is None:
            self._send_json(HTTPStatus.NOT_FOUND, {"code": "route_not_found"})
            self._audit(method=method, status=404, approval_id=None, code="route_not_found")
            return

        approval_id = match.group("id")
        action = match.group("action")
        if action not in _VALID_ACTIONS:
            # Defensive — regex already restricts to confirm/reject.
            self._send_json(HTTPStatus.NOT_FOUND, {"code": "route_not_found"})
            self._audit(method=method, status=404, approval_id=approval_id, code="route_not_found")
            return

        if not self._authorized(config):
            self._send_json(HTTPStatus.UNAUTHORIZED, {"code": "unauthorized"})
            self._audit(method=method, status=401, approval_id=approval_id, code="unauthorized")
            return

        body, body_err = self._read_body(config)
        if body_err is not None:
            status, code = body_err
            self._send_json(status, {"code": code})
            self._audit(method=method, status=int(status), approval_id=approval_id, code=code)
            return

        actor = str(body.get("actor", "") or "")
        reason = str(body.get("reason", "") or "")

        # Cheap server-side ttl + status check so callers see deterministic
        # 404 vs 409 (rather than queue.patch raising on already-decided).
        queue = ApprovalQueue(config.approvals_root)
        with getattr(self.server, "lock"):  # serialise queue mutations
            target, existing_status = _find_actionable(queue, approval_id)
            if target is None:
                self._send_json(HTTPStatus.NOT_FOUND, {"code": "not_found"})
                self._audit(method=method, status=404, approval_id=approval_id, code="not_found")
                return

            if existing_status != "pending":
                self._send_json(
                    HTTPStatus.CONFLICT,
                    {"code": "already_decided", "status": existing_status},
                )
                self._audit(
                    method=method,
                    status=409,
                    approval_id=approval_id,
                    code="already_decided",
                    existing_status=existing_status,
                )
                return

            now_fn = config.clock or (lambda: dt.datetime.now(tz=dt.timezone.utc))
            now = now_fn()
            if _ttl_expired(target, now=now, ttl_s=config.ttl_s):
                self._send_json(
                    HTTPStatus.NOT_FOUND,
                    {"code": "expired", "ttl_s": config.ttl_s},
                )
                self._audit(
                    method=method,
                    status=404,
                    approval_id=approval_id,
                    code="expired",
                )
                return

            new_status = "confirmed" if action == "confirm" else "rejected"
            try:
                updated = queue.patch(
                    approval_id,
                    status=new_status,
                    reason=reason,
                    now=now,
                )
            except ApprovalQueueError as exc:
                # Race: row was decided after _find_actionable but before
                # patch() acquired its read. Surface as 409 (idempotent).
                latest = queue.get(approval_id)
                latest_status = latest.status if latest is not None else "unknown"
                self._send_json(
                    HTTPStatus.CONFLICT,
                    {"code": "already_decided", "status": latest_status, "detail": str(exc)},
                )
                self._audit(
                    method=method,
                    status=409,
                    approval_id=approval_id,
                    code="already_decided_race",
                )
                return

        self._send_json(
            HTTPStatus.OK,
            {
                "status": updated.status,
                "approval_id": updated.id,
                "decided_at": (
                    updated.decided_at.isoformat()
                    if updated.decided_at is not None
                    else None
                ),
            },
        )
        self._audit(
            method=method,
            status=200,
            approval_id=approval_id,
            code="ok",
            action=action,
            actor=actor or None,
        )

    # ---- helpers -----------------------------------------------------

    def _authorized(self, config: InboundConfig) -> bool:
        header = self.headers.get("Authorization", "")
        prefix = "Bearer "
        if not header.startswith(prefix):
            return False
        provided = header[len(prefix):].strip()
        if not provided:
            return False
        return hmac.compare_digest(provided, config.shared_secret)

    def _read_body(
        self, config: InboundConfig
    ) -> tuple[dict[str, Any], tuple[HTTPStatus, str] | None]:
        length_header = self.headers.get("Content-Length", "0")
        try:
            length = int(length_header)
        except ValueError:
            return {}, (HTTPStatus.BAD_REQUEST, "bad_content_length")
        if length < 0:
            return {}, (HTTPStatus.BAD_REQUEST, "bad_content_length")
        if length > config.max_body_bytes:
            return {}, (HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "body_too_large")
        if length == 0:
            # Empty body is allowed (actor / reason optional).
            return {}, None
        raw = self.rfile.read(length)
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {}, (HTTPStatus.BAD_REQUEST, "bad_json")
        if not isinstance(parsed, dict):
            return {}, (HTTPStatus.BAD_REQUEST, "body_must_be_object")
        return parsed, None

    def _send_json(self, status: HTTPStatus, body: dict[str, Any]) -> None:
        payload = json.dumps(body, sort_keys=True).encode("utf-8")
        self.send_response(int(status))
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        # Defence in depth: explicit no-cache; the bridge body always
        # reflects mutable queue state and openclaw must not reuse it.
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(payload)
        except BrokenPipeError:
            # Client hung up before we finished writing the response.
            # Nothing to do — the queue mutation (if any) is already
            # committed atomically.
            pass

    def _audit(self, **fields: Any) -> None:
        """Emit one structured json log line per request."""
        fields = {"event": "bridge.in.request", **{k: v for k, v in fields.items() if v is not None}}
        log.info(json.dumps(fields, sort_keys=True))


# ---------------------------------------------------------------------------
# Pure helpers (unit-testable without spinning the server)
# ---------------------------------------------------------------------------


def _find_actionable(
    queue: ApprovalQueue, approval_id: str,
) -> tuple[ApprovalRecord | None, str | None]:
    """Return `(row, existing_status)` for `approval_id`.

    Preference order: a `pending` row wins over coexisting
    `confirmed`/`rejected` audit rows (e.g. a dry_run audit row may
    share the same fingerprint with a manual pending row — see the
    Phase 3.5 idempotency tightening in `ApprovalQueue` docstring).

    Returns `(None, None)` if no row with that id exists at all.
    """
    rows = [r for r in queue if r.id == approval_id]
    if not rows:
        return None, None
    for row in rows:
        if row.status == "pending":
            return row, "pending"
    # All rows are already decided — return the first one's status.
    return rows[0], rows[0].status


def _ttl_expired(
    record: ApprovalRecord, *, now: dt.datetime, ttl_s: int,
) -> bool:
    age = (now - record.created_at).total_seconds()
    return age > ttl_s
