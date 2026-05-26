"""AlertBridge — outbound HTTP dispatch for severity-tagged alerts.

Phase 6 Task T9 / yuanbao alert outbound (see `docs/phase6_brief.md`
§5 T9 + §10). Posts an alert envelope to openclaw's alerts webhook
authenticated with a Bearer token; openclaw's TaskFlow chooses the
yuanbao push channel based on `severity`.

The wire contract (brief §10):

    POST <OPENCLAW_GATEWAY>/webhooks/xtrade/alerts
    Headers: Authorization: Bearer <OPENCLAW_SHARED_SECRET>
             Content-Type: application/json
    Body schema:
      {
        "action": "create_alert",
        "severity": "info" | "warn" | "crit",
        "event": "supervisor.heartbeat.idle" | "supervisor.mcap.softkill"
               | "supervisor.drawdown.halt" | "supervisor.start"
               | "scanner.threshold_ladder.heavy_emit" | ...,
        "message": "≤ 200 char human-readable one-liner",
        "instrument": "SPCXUSDT-PERP.BINANCE",   # optional
        "fields": { ... arbitrary scalar key → value },
        "dispatched_at": "ISO 8601 Z"
      }

Retry policy
------------
Mirrors `OpenclawBridge`: network errors and HTTP 5xx → exponential
backoff (1/2/4/8 s, max 4 attempts). HTTP 4xx → terminal (caller
mis-formed the payload). Alerts are best-effort: a terminal failure
never raises out of `dispatch_alert(...)` — it returns a result with
`ok=False` so the caller can fold it into structured logs without
disrupting the supervisor loop.

Audit
-----
When `audit_writer` is set, every attempt appends a row to
`alerts.<YYYY-MM-DD>.jsonl` under the writer's audit_root. The row
schema is bridge_out-shaped (kind ∈ {ok, retry, fail, refused},
status_code, error, elapsed_s, response_excerpt) plus `severity`
and `event` so the operator can grep by alert class.

Secret scrub
------------
`message` + every value in `fields` is regex-scanned against the same
narrow set of credential patterns the approval bridge uses. A hit
short-circuits dispatch with kind="refused" — alerts must never carry
secrets, even accidentally (an exception traceback embedded in a
`fields["error"]` could otherwise leak an API key).

Decoupling
----------
The class does not import `xtrade.research`, `xtrade.strategy`, or
any Nautilus symbol; it only depends on `httpx` and the local
observability helpers. Heartbeat / drawdown / soft-kill watchers
(other T9 / T7 / T8 modules) hold a reference and call
`dispatch_alert(...)` directly.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import logging
import os
import re
import secrets
import time
from pathlib import Path
from typing import Any, Callable, Literal, Mapping

import httpx

from xtrade.obs import emit_event


log = logging.getLogger("xtrade.bridge.alerter")

Severity = Literal["info", "warn", "crit"]
AuditKind = Literal["ok", "retry", "fail", "refused"]

UTC = dt.timezone.utc

_ALERT_FILE_MODE = 0o640
_ALERT_DIR_MODE = 0o750

# Narrow regex set duplicated from `xtrade.bridge.schema` so the alerter
# stays decoupled from the approval bridge's private symbols (brief §6).
_FORBIDDEN_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bsk-[A-Za-z0-9]{16,}\b"),     # OpenAI / Anthropic prefix
    re.compile(r"\b0x[0-9a-fA-F]{64}\b"),       # EVM private key
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),        # AWS access key id
)

_MAX_MESSAGE_CHARS = 200
_VALID_SEVERITIES: frozenset[str] = frozenset({"info", "warn", "crit"})


class AlertBridgeConfigError(ValueError):
    """Raised when AlertBridge is constructed with incomplete config."""


class AlertSecretLeakError(ValueError):
    """Raised when an alert payload contains a credential-shaped string."""


class AlertPayloadError(ValueError):
    """Raised on caller-side payload validation (bad severity, oversize message, ...)."""


# ---------------------------------------------------------------------------
# Result + audit row
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True, slots=True)
class AlertDispatchResult:
    """Outcome of one `dispatch_alert(...)` call."""

    alert_id: str
    severity: Severity
    event: str
    ok: bool
    status_code: int | None
    attempts: int
    elapsed_s: float
    error: str | None
    response_excerpt: str | None
    dispatched_at: dt.datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            "alert_id": self.alert_id,
            "severity": self.severity,
            "event": self.event,
            "ok": self.ok,
            "status_code": self.status_code,
            "attempts": self.attempts,
            "elapsed_s": round(self.elapsed_s, 3),
            "error": self.error,
            "response_excerpt": self.response_excerpt,
            "dispatched_at": self.dispatched_at.isoformat(),
        }


@dataclasses.dataclass(frozen=True)
class AlertAuditRow:
    """One audit jsonl row (debug / test helper)."""

    alert_id: str
    severity: Severity
    event: str
    attempt: int
    kind: AuditKind
    status_code: int | None
    error: str | None
    dispatched_at: dt.datetime
    elapsed_s: float
    response_excerpt: str | None

    def to_envelope(self) -> dict[str, object]:
        return {
            "alert_id": self.alert_id,
            "severity": self.severity,
            "event": self.event,
            "attempt": self.attempt,
            "kind": self.kind,
            "status_code": self.status_code,
            "error": self.error,
            "dispatched_at": self.dispatched_at.astimezone(UTC).isoformat(),
            "elapsed_s": round(float(self.elapsed_s), 3),
            "response_excerpt": self.response_excerpt,
        }


class AlertAuditWriter:
    """Append-only jsonl writer for alert dispatch attempts.

    File layout: `<audit_root>/alerts.<YYYY-MM-DD>.jsonl`. Same atomic
    O_APPEND single-write template as `BridgeAuditWriter` (see
    `src/xtrade/bridge/audit.py` for the rationale).
    """

    def __init__(self, audit_root: Path) -> None:
        self._audit_root = Path(audit_root)

    @property
    def audit_root(self) -> Path:
        return self._audit_root

    def write(
        self,
        *,
        alert_id: str,
        severity: Severity,
        event: str,
        attempt: int,
        kind: AuditKind,
        status_code: int | None,
        error: str | None,
        dispatched_at: dt.datetime,
        elapsed_s: float,
        response_excerpt: str | None = None,
    ) -> Path:
        if severity not in _VALID_SEVERITIES:
            raise ValueError(f"invalid severity: {severity!r}")
        if kind not in ("ok", "retry", "fail", "refused"):
            raise ValueError(f"invalid kind: {kind!r}")
        if dispatched_at.tzinfo is None:
            raise ValueError("dispatched_at must be timezone-aware (UTC)")

        row = AlertAuditRow(
            alert_id=alert_id,
            severity=severity,
            event=event,
            attempt=int(attempt),
            kind=kind,
            status_code=status_code,
            error=error,
            dispatched_at=dispatched_at,
            elapsed_s=float(elapsed_s),
            response_excerpt=response_excerpt,
        )

        shard = self._shard_path(dispatched_at)
        self._audit_root.mkdir(parents=True, exist_ok=True, mode=_ALERT_DIR_MODE)

        line = json.dumps(row.to_envelope(), sort_keys=True, separators=(",", ":")) + "\n"
        data = line.encode("utf-8")

        fd = os.open(
            str(shard),
            os.O_WRONLY | os.O_APPEND | os.O_CREAT,
            _ALERT_FILE_MODE,
        )
        try:
            os.write(fd, data)
        finally:
            os.close(fd)
        return shard

    def _shard_path(self, when: dt.datetime) -> Path:
        day = when.astimezone(UTC).strftime("%Y-%m-%d")
        return self._audit_root / f"alerts.{day}.jsonl"


# ---------------------------------------------------------------------------
# Payload scrub
# ---------------------------------------------------------------------------


def _scrub_strings_for_secrets(values: list[str]) -> None:
    """Raise `AlertSecretLeakError` if any value matches a leak pattern."""
    for v in values:
        for pat in _FORBIDDEN_PATTERNS:
            if pat.search(v):
                raise AlertSecretLeakError(
                    "alert payload contains credential-shaped string; "
                    "refusing to dispatch (see xtrade.bridge.alerter)"
                )


def _coerce_fields_for_json(fields: Mapping[str, Any] | None) -> dict[str, Any]:
    """Validate that every `fields` value is a scalar; reject nested containers.

    Alert payloads are operator-facing; nested structures bloat the
    yuanbao push and increase scrub surface. Allowed value types:
    str, int, float, bool, None.
    """
    if fields is None:
        return {}
    out: dict[str, Any] = {}
    for k, v in fields.items():
        if not isinstance(k, str):
            raise AlertPayloadError(
                f"fields keys must be str, got {type(k).__name__}"
            )
        if v is None or isinstance(v, (str, int, float, bool)):
            out[k] = v
        else:
            raise AlertPayloadError(
                f"fields[{k!r}] must be scalar (str/int/float/bool/None), "
                f"got {type(v).__name__}"
            )
    return out


# ---------------------------------------------------------------------------
# AlertBridge
# ---------------------------------------------------------------------------


class AlertBridge:
    """HTTP client for xtrade → openclaw alert outbound."""

    DEFAULT_BACKOFFS: tuple[float, ...] = (1.0, 2.0, 4.0, 8.0)
    DEFAULT_CONNECT_TIMEOUT_S: float = 5.0
    DEFAULT_READ_TIMEOUT_S: float = 10.0
    DEFAULT_ROUTE: str = "/webhooks/xtrade/alerts"

    def __init__(
        self,
        *,
        gateway_url: str,
        shared_secret: str,
        audit_writer: AlertAuditWriter | None = None,
        client: httpx.Client | None = None,
        backoffs: tuple[float, ...] = DEFAULT_BACKOFFS,
        connect_timeout_s: float = DEFAULT_CONNECT_TIMEOUT_S,
        read_timeout_s: float = DEFAULT_READ_TIMEOUT_S,
        route: str = DEFAULT_ROUTE,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], dt.datetime] | None = None,
        alert_id_factory: Callable[[], str] | None = None,
    ) -> None:
        if not isinstance(gateway_url, str) or not gateway_url.startswith(("http://", "https://")):
            raise AlertBridgeConfigError(
                f"gateway_url must be http(s):// URL, got {gateway_url!r}"
            )
        if not shared_secret:
            raise AlertBridgeConfigError("shared_secret is required (Bearer token)")
        if not backoffs:
            raise AlertBridgeConfigError("backoffs tuple must be non-empty")
        if not isinstance(route, str) or not route.startswith("/"):
            raise AlertBridgeConfigError(
                f"route must start with '/', got {route!r}"
            )

        self._gateway = gateway_url.rstrip("/")
        self._secret = shared_secret
        self._route = route
        self._backoffs = backoffs
        self._sleep = sleep
        self._now = now or (lambda: dt.datetime.now(tz=UTC))
        self._audit_writer = audit_writer
        self._id_factory = alert_id_factory or _default_alert_id

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
        audit_writer: AlertAuditWriter | None = None,
        **kwargs: Any,
    ) -> "AlertBridge":
        """Build a bridge from `os.environ`-style mapping."""
        try:
            gateway = env["OPENCLAW_GATEWAY"]
            secret = env["OPENCLAW_SHARED_SECRET"]
        except KeyError as exc:
            raise AlertBridgeConfigError(
                f"missing required env var: {exc.args[0]}"
            ) from exc
        return cls(
            gateway_url=gateway,
            shared_secret=secret,
            audit_writer=audit_writer,
            **kwargs,
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "AlertBridge":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ------------------------------------------------------------------
    # main entry point
    # ------------------------------------------------------------------

    def dispatch_alert(
        self,
        *,
        severity: Severity,
        event: str,
        message: str,
        instrument: str | None = None,
        fields: Mapping[str, Any] | None = None,
    ) -> AlertDispatchResult:
        """POST one alert envelope to openclaw with retry; never raises on transport.

        Validates locally first (severity / event / message length /
        scalar-only fields / secret scrub) — any local failure short-
        circuits to kind="refused" without an HTTP attempt.
        """
        alert_id = self._id_factory()

        # ---- local validation (raises caller-facing errors before any I/O) --
        if severity not in _VALID_SEVERITIES:
            raise AlertPayloadError(
                f"severity must be one of {sorted(_VALID_SEVERITIES)}, "
                f"got {severity!r}"
            )
        if not isinstance(event, str) or not event:
            raise AlertPayloadError("event must be a non-empty str")
        if not isinstance(message, str):
            raise AlertPayloadError("message must be a str")
        if len(message) > _MAX_MESSAGE_CHARS:
            raise AlertPayloadError(
                f"message exceeds {_MAX_MESSAGE_CHARS} chars "
                f"(got {len(message)}); shorten before dispatch"
            )
        if instrument is not None and not isinstance(instrument, str):
            raise AlertPayloadError("instrument must be a str or None")

        coerced_fields = _coerce_fields_for_json(fields)

        # ---- secret scrub (after coercion; we now know all values are scalar) ----
        scrub_targets: list[str] = [message, event]
        if instrument is not None:
            scrub_targets.append(instrument)
        for fv in coerced_fields.values():
            if isinstance(fv, str):
                scrub_targets.append(fv)
        try:
            _scrub_strings_for_secrets(scrub_targets)
        except AlertSecretLeakError as exc:
            return self._record_refused(
                alert_id=alert_id,
                severity=severity,
                event=event,
                error=f"secret-scrub: {type(exc).__name__}",
            )

        body: dict[str, Any] = {
            "action": "create_alert",
            "severity": severity,
            "event": event,
            "message": message,
            "fields": coerced_fields,
            "dispatched_at": self._now().astimezone(UTC).isoformat(),
        }
        if instrument is not None:
            body["instrument"] = instrument

        target = f"{self._gateway}{self._route}"
        headers = {
            "Authorization": f"Bearer {self._secret}",
            "Content-Type": "application/json",
            "User-Agent": "xtrade-alerter/1.0",
        }

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
                    emit_event(
                        log,
                        "alerter.dispatch_ok",
                        id=alert_id,
                        severity=severity,
                        alert_event=event,
                        status=resp.status_code,
                        attempts=attempts,
                        elapsed_s=round(elapsed, 3),
                    )
                    self._audit(
                        alert_id=alert_id,
                        severity=severity,
                        event=event,
                        attempt=attempts,
                        kind="ok",
                        status_code=resp.status_code,
                        error=None,
                        elapsed_s=elapsed,
                        response_excerpt=last_excerpt,
                    )
                    return AlertDispatchResult(
                        alert_id=alert_id,
                        severity=severity,
                        event=event,
                        ok=True,
                        status_code=resp.status_code,
                        attempts=attempts,
                        elapsed_s=elapsed,
                        error=None,
                        response_excerpt=last_excerpt,
                        dispatched_at=self._now(),
                    )
                if 400 <= resp.status_code < 500:
                    last_error = f"http-{resp.status_code}: client error (no retry)"
                    break
                last_error = f"http-{resp.status_code}: server error"
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_status = None
                last_error = f"{type(exc).__name__}: {exc}"
                last_excerpt = None

            if idx < len(self._backoffs) - 1:
                emit_event(
                    log,
                    "alerter.dispatch_retry",
                    level=logging.WARNING,
                    id=alert_id,
                    severity=severity,
                    alert_event=event,
                    attempt=attempts,
                    error=last_error,
                    sleep_s=round(backoff, 3),
                )
                self._audit(
                    alert_id=alert_id,
                    severity=severity,
                    event=event,
                    attempt=attempts,
                    kind="retry",
                    status_code=last_status,
                    error=last_error,
                    elapsed_s=time.monotonic() - start,
                    response_excerpt=last_excerpt,
                )
                self._sleep(backoff)

        elapsed = time.monotonic() - start
        emit_event(
            log,
            "alerter.dispatch_failed",
            level=logging.ERROR,
            id=alert_id,
            severity=severity,
            alert_event=event,
            attempts=attempts,
            last_error=last_error,
            elapsed_s=round(elapsed, 3),
        )
        self._audit(
            alert_id=alert_id,
            severity=severity,
            event=event,
            attempt=attempts,
            kind="fail",
            status_code=last_status,
            error=last_error or "unknown",
            elapsed_s=elapsed,
            response_excerpt=last_excerpt,
        )
        return AlertDispatchResult(
            alert_id=alert_id,
            severity=severity,
            event=event,
            ok=False,
            status_code=last_status,
            attempts=attempts,
            elapsed_s=elapsed,
            error=last_error or "unknown",
            response_excerpt=last_excerpt,
            dispatched_at=self._now(),
        )

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _record_refused(
        self,
        *,
        alert_id: str,
        severity: Severity,
        event: str,
        error: str,
    ) -> AlertDispatchResult:
        now = self._now()
        emit_event(
            log,
            "alerter.dispatch_refused",
            level=logging.ERROR,
            id=alert_id,
            severity=severity,
            alert_event=event,
            reason=error,
        )
        self._audit(
            alert_id=alert_id,
            severity=severity,
            event=event,
            attempt=0,
            kind="refused",
            status_code=None,
            error=error,
            elapsed_s=0.0,
            response_excerpt=None,
        )
        return AlertDispatchResult(
            alert_id=alert_id,
            severity=severity,
            event=event,
            ok=False,
            status_code=None,
            attempts=0,
            elapsed_s=0.0,
            error=error,
            response_excerpt=None,
            dispatched_at=now,
        )

    def _audit(
        self,
        *,
        alert_id: str,
        severity: Severity,
        event: str,
        attempt: int,
        kind: AuditKind,
        status_code: int | None,
        error: str | None,
        elapsed_s: float,
        response_excerpt: str | None,
    ) -> None:
        """Best-effort audit append. Never raises out of dispatch."""
        if self._audit_writer is None:
            return
        try:
            self._audit_writer.write(
                alert_id=alert_id,
                severity=severity,
                event=event,
                attempt=attempt,
                kind=kind,
                status_code=status_code,
                error=error,
                dispatched_at=self._now(),
                elapsed_s=elapsed_s,
                response_excerpt=response_excerpt,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "alerter audit write failed: %s: %s",
                type(exc).__name__,
                exc,
            )


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _default_alert_id() -> str:
    """`AL-<YYYYmmddTHHMMSSZ>-<6 hex>` — alert ids are operator-facing."""
    ts = dt.datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"AL-{ts}-{secrets.token_hex(3)}"
