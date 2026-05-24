"""Tests for `xtrade.bridge.inbound` (Phase 4 Task 3 / T4 inbound).

The inbound server is a hand-rolled `http.server.ThreadingHTTPServer`
listening on loopback only. It accepts openclaw callbacks of the
shape:

    POST /approvals/<id>/{confirm,reject}
    Authorization: Bearer <OPENCLAW_INBOUND_SECRET>
    body: {"actor": "yuanbao:<user>", "reason": "..."}

These tests spin a real server on an ephemeral loopback port and drive
it via `urllib.request` so the wire contract (status codes, JSON
shape, header validation) is exercised end-to-end.
"""

from __future__ import annotations

import datetime as dt
import json
import threading
import urllib.error
import urllib.request
from decimal import Decimal
from pathlib import Path

import pytest

from xtrade.approval.queue import ApprovalQueue
from xtrade.bridge.inbound import (
    InboundConfig,
    _find_actionable,
    _ttl_expired,
    build_server,
)
from xtrade.strategy.intent import OrderIntent


UTC = dt.timezone.utc
SECRET = "test-shared-secret-not-real"


# ---- fixtures -------------------------------------------------------------


def _intent(*, fp_seed: str = "default") -> OrderIntent:
    return OrderIntent(
        venue="binance",
        symbol="BTCUSDT-PERP.BINANCE",
        side="BUY",
        order_type="MARKET",
        quantity=Decimal("0.002"),
        limit_price=None,
        reduce_only=False,
        time_in_force="IOC",
        source_signal_id=f"manual:{fp_seed}",
        created_at=dt.datetime(2026, 5, 24, 12, 0, 0, tzinfo=UTC),
    )


def _seed_pending(
    approvals_root: Path,
    *,
    created_at: dt.datetime | None = None,
    fp_seed: str = "default",
) -> str:
    queue = ApprovalQueue(approvals_root)
    when = created_at or dt.datetime.now(tz=UTC)
    record = queue.submit(
        _intent(fp_seed=fp_seed),
        mode="manual",
        status="pending",
        now=when,
    )
    return record.id


@pytest.fixture
def server_context(tmp_path: Path):
    """Spin up a real server on a loopback ephemeral port; tear down after."""
    config = InboundConfig(
        approvals_root=tmp_path / "approvals",
        shared_secret=SECRET,
        bind="127.0.0.1",
        port=0,  # ephemeral
        ttl_s=900,
    )
    server = build_server(config)
    host, port = server.server_address[:2]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield {
            "config": config,
            "approvals_root": tmp_path / "approvals",
            "base_url": f"http://{host}:{port}",
            "server": server,
        }
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _post(
    url: str,
    *,
    body: dict | None = None,
    bearer: str | None = SECRET,
    raw_body: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict]:
    """POST and return `(status, parsed_json)`.

    Uses `urllib.error.HTTPError` to surface 4xx/5xx with their body
    intact (urllib raises on non-2xx by default).
    """
    data = raw_body if raw_body is not None else json.dumps(body or {}).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    if bearer is not None:
        req.add_header("Authorization", f"Bearer {bearer}")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        resp = urllib.request.urlopen(req, timeout=2)
        body_bytes = resp.read()
        return resp.status, json.loads(body_bytes.decode("utf-8")) if body_bytes else {}
    except urllib.error.HTTPError as exc:
        body_bytes = exc.read()
        try:
            return exc.code, json.loads(body_bytes.decode("utf-8")) if body_bytes else {}
        except json.JSONDecodeError:
            return exc.code, {"raw": body_bytes.decode("utf-8", errors="replace")}


# ---- happy path ----------------------------------------------------------


def test_confirm_pending_returns_200_and_patches_queue(server_context) -> None:
    approval_id = _seed_pending(server_context["approvals_root"])

    status, body = _post(
        f"{server_context['base_url']}/approvals/{approval_id}/confirm",
        body={"actor": "yuanbao:alice", "reason": "ack"},
    )

    assert status == 200
    assert body["status"] == "confirmed"
    assert body["approval_id"] == approval_id
    assert body["decided_at"] is not None

    # Verify on-disk patch
    queue = ApprovalQueue(server_context["approvals_root"])
    row = queue.get(approval_id)
    assert row is not None
    assert row.status == "confirmed"
    assert row.reason == "ack"


def test_reject_pending_returns_200_and_patches_queue(server_context) -> None:
    approval_id = _seed_pending(server_context["approvals_root"])

    status, body = _post(
        f"{server_context['base_url']}/approvals/{approval_id}/reject",
        body={"actor": "yuanbao:bob", "reason": "size too big"},
    )

    assert status == 200
    assert body["status"] == "rejected"

    queue = ApprovalQueue(server_context["approvals_root"])
    assert queue.get(approval_id).status == "rejected"


def test_empty_body_allowed(server_context) -> None:
    """actor/reason are optional; an empty body must still succeed."""
    approval_id = _seed_pending(server_context["approvals_root"])
    status, body = _post(
        f"{server_context['base_url']}/approvals/{approval_id}/confirm",
        raw_body=b"",
    )
    assert status == 200
    assert body["status"] == "confirmed"


# ---- authorization -------------------------------------------------------


def test_missing_authorization_returns_401(server_context) -> None:
    approval_id = _seed_pending(server_context["approvals_root"])
    status, body = _post(
        f"{server_context['base_url']}/approvals/{approval_id}/confirm",
        bearer=None,
        body={},
    )
    assert status == 401
    assert body["code"] == "unauthorized"
    # Row must remain pending — bad auth never mutates.
    queue = ApprovalQueue(server_context["approvals_root"])
    assert queue.get(approval_id).status == "pending"


def test_wrong_bearer_returns_401(server_context) -> None:
    approval_id = _seed_pending(server_context["approvals_root"])
    status, _ = _post(
        f"{server_context['base_url']}/approvals/{approval_id}/confirm",
        bearer="not-the-real-secret",
        body={},
    )
    assert status == 401


def test_malformed_authorization_header_returns_401(server_context) -> None:
    approval_id = _seed_pending(server_context["approvals_root"])
    status, _ = _post(
        f"{server_context['base_url']}/approvals/{approval_id}/confirm",
        bearer=None,
        body={},
        headers={"Authorization": "Token oops"},
    )
    assert status == 401


# ---- idempotency / already-decided ---------------------------------------


def test_double_confirm_returns_409_with_existing_status(server_context) -> None:
    approval_id = _seed_pending(server_context["approvals_root"])

    s1, _ = _post(
        f"{server_context['base_url']}/approvals/{approval_id}/confirm",
        body={"actor": "yuanbao:alice"},
    )
    assert s1 == 200

    s2, body2 = _post(
        f"{server_context['base_url']}/approvals/{approval_id}/confirm",
        body={"actor": "yuanbao:alice"},
    )
    assert s2 == 409
    assert body2["code"] == "already_decided"
    assert body2["status"] == "confirmed"


def test_confirm_after_reject_returns_409(server_context) -> None:
    """Cross-decision retries also 409 — operator may have switched paths
    inside openclaw but xtrade never down-grades a decided row."""
    approval_id = _seed_pending(server_context["approvals_root"])

    _post(
        f"{server_context['base_url']}/approvals/{approval_id}/reject",
        body={"actor": "yuanbao:bob"},
    )
    status, body = _post(
        f"{server_context['base_url']}/approvals/{approval_id}/confirm",
        body={"actor": "yuanbao:bob"},
    )
    assert status == 409
    assert body["status"] == "rejected"


# ---- ttl + not-found -----------------------------------------------------


def test_unknown_id_returns_404(server_context) -> None:
    status, body = _post(
        f"{server_context['base_url']}/approvals/0000000000000000/confirm",
        body={},
    )
    assert status == 404
    assert body["code"] == "not_found"


def test_expired_ttl_returns_404(tmp_path: Path) -> None:
    """A row older than `ttl_s` must be refused with `code=expired`."""
    approvals_root = tmp_path / "approvals"
    # Seed a row created 10 minutes ago; configure ttl=60s.
    old = dt.datetime.now(tz=UTC) - dt.timedelta(minutes=10)
    approval_id = _seed_pending(approvals_root, created_at=old)

    config = InboundConfig(
        approvals_root=approvals_root,
        shared_secret=SECRET,
        bind="127.0.0.1",
        port=0,
        ttl_s=60,
    )
    server = build_server(config)
    host, port = server.server_address[:2]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, body = _post(
            f"http://{host}:{port}/approvals/{approval_id}/confirm",
            body={"actor": "yuanbao:alice"},
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert status == 404
    assert body["code"] == "expired"
    assert body["ttl_s"] == 60
    # Row must remain pending — expired callbacks never patch.
    assert ApprovalQueue(approvals_root).get(approval_id).status == "pending"


# ---- routing / method -----------------------------------------------------


def test_route_not_matching_pattern_returns_404(server_context) -> None:
    status, body = _post(
        f"{server_context['base_url']}/approvals/abcd/decide",
        body={},
    )
    assert status == 404
    assert body["code"] == "route_not_found"


def test_wrong_method_returns_405(server_context) -> None:
    approval_id = _seed_pending(server_context["approvals_root"])
    url = f"{server_context['base_url']}/approvals/{approval_id}/confirm"
    req = urllib.request.Request(url, method="GET")
    req.add_header("Authorization", f"Bearer {SECRET}")
    try:
        urllib.request.urlopen(req, timeout=2)
    except urllib.error.HTTPError as exc:
        assert exc.code == 405
        body = json.loads(exc.read().decode("utf-8"))
        assert body["code"] == "method_not_allowed"
    else:
        pytest.fail("expected HTTPError 405")


# ---- bad request ---------------------------------------------------------


def test_malformed_json_returns_400(server_context) -> None:
    approval_id = _seed_pending(server_context["approvals_root"])
    status, body = _post(
        f"{server_context['base_url']}/approvals/{approval_id}/confirm",
        raw_body=b"{not json",
    )
    assert status == 400
    assert body["code"] == "bad_json"


def test_non_object_body_returns_400(server_context) -> None:
    approval_id = _seed_pending(server_context["approvals_root"])
    status, body = _post(
        f"{server_context['base_url']}/approvals/{approval_id}/confirm",
        raw_body=b'"not an object"',
    )
    assert status == 400
    assert body["code"] == "body_must_be_object"


def test_oversized_body_returns_413(tmp_path: Path) -> None:
    approvals_root = tmp_path / "approvals"
    approval_id = _seed_pending(approvals_root)

    config = InboundConfig(
        approvals_root=approvals_root,
        shared_secret=SECRET,
        bind="127.0.0.1",
        port=0,
        max_body_bytes=64,
    )
    server = build_server(config)
    host, port = server.server_address[:2]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        large_body = b'{"reason":"' + b"x" * 200 + b'"}'
        status, body = _post(
            f"http://{host}:{port}/approvals/{approval_id}/confirm",
            raw_body=large_body,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert status == 413
    assert body["code"] == "body_too_large"


# ---- config validation ---------------------------------------------------


def test_inbound_config_rejects_non_loopback_bind(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="loopback"):
        InboundConfig(
            approvals_root=tmp_path,
            shared_secret=SECRET,
            bind="0.0.0.0",
        )


def test_inbound_config_rejects_empty_secret(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="shared_secret"):
        InboundConfig(approvals_root=tmp_path, shared_secret="")


def test_inbound_config_rejects_zero_ttl(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="ttl_s"):
        InboundConfig(approvals_root=tmp_path, shared_secret=SECRET, ttl_s=0)


# ---- pure helpers --------------------------------------------------------


def test_find_actionable_prefers_pending_over_audit(tmp_path: Path) -> None:
    """When a dry_run audit row coexists with a manual pending row for
    the same fingerprint, `_find_actionable` must return the pending one."""
    approvals_root = tmp_path / "approvals"
    queue = ApprovalQueue(approvals_root)
    intent = _intent()
    # dry_run audit row first (status=confirmed at write time).
    queue.submit(intent, mode="dry_run", status="confirmed",
                 decided_at=dt.datetime.now(tz=UTC))
    # then the manual pending row.
    pending = queue.submit(intent, mode="manual", status="pending")

    row, status = _find_actionable(queue, pending.id)
    assert row is not None
    assert status == "pending"
    assert row.mode == "manual"


def test_find_actionable_returns_none_for_unknown_id(tmp_path: Path) -> None:
    queue = ApprovalQueue(tmp_path / "approvals")
    row, status = _find_actionable(queue, "0" * 16)
    assert row is None
    assert status is None


def test_ttl_expired_boundary(tmp_path: Path) -> None:
    queue = ApprovalQueue(tmp_path / "approvals")
    created = dt.datetime(2026, 5, 24, 12, 0, 0, tzinfo=UTC)
    record = queue.submit(_intent(), mode="manual", status="pending", now=created)

    # within ttl
    assert not _ttl_expired(
        record, now=created + dt.timedelta(seconds=900), ttl_s=900
    )
    # just past
    assert _ttl_expired(
        record, now=created + dt.timedelta(seconds=901), ttl_s=900
    )
