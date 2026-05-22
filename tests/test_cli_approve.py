"""Tests for `xtrade approve` CLI subcommands (Phase 3 Task 5 / T5)."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path

from typer.testing import CliRunner

from xtrade.approval import ApprovalQueue
from xtrade.cli import app
from xtrade.strategy.intent import OrderIntent


runner = CliRunner()


UTC = dt.timezone.utc


def _intent(symbol: str = "BTCUSDT-PERP.BINANCE") -> OrderIntent:
    return OrderIntent(
        venue="binance",
        symbol=symbol,
        side="BUY",
        order_type="MARKET",
        quantity=Decimal("0.01"),
        limit_price=None,
        reduce_only=False,
        time_in_force="IOC",
        source_signal_id="2026-05-22T10:00:00+00:00|BTCUSDT-PERP|momentum:aaaaaaaa",
        created_at=dt.datetime(2026, 5, 22, 10, 0, 0, tzinfo=UTC),
    )


def _seed_pending(root: Path) -> str:
    q = ApprovalQueue(root)
    rec = q.submit(
        _intent(),
        mode="manual",
        status="pending",
        now=dt.datetime(2026, 5, 22, 10, 0, 0, tzinfo=UTC),
    )
    return rec.id


def test_approve_list_empty(tmp_path: Path) -> None:
    result = runner.invoke(app, ["approve", "list", "--root", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "no approvals" in result.output


def test_approve_list_shows_pending_row(tmp_path: Path) -> None:
    approval_id = _seed_pending(tmp_path)
    result = runner.invoke(app, ["approve", "list", "--root", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert approval_id in result.output
    assert "pending" in result.output


def test_approve_confirm_flips_status(tmp_path: Path) -> None:
    approval_id = _seed_pending(tmp_path)
    result = runner.invoke(
        app, ["approve", "confirm", approval_id, "--root", str(tmp_path)]
    )
    assert result.exit_code == 0, result.output
    assert "confirmed" in result.output
    assert approval_id in result.output

    # Sanity-check disk: row is now `confirmed`.
    q = ApprovalQueue(tmp_path)
    rows = q.list()
    assert any(r.id == approval_id and r.status == "confirmed" for r in rows)


def test_approve_reject_flips_status_with_reason(tmp_path: Path) -> None:
    approval_id = _seed_pending(tmp_path)
    result = runner.invoke(
        app,
        [
            "approve",
            "reject",
            approval_id,
            "--reason",
            "manual veto",
            "--root",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "rejected" in result.output

    q = ApprovalQueue(tmp_path)
    rows = q.list()
    matching = [r for r in rows if r.id == approval_id]
    assert matching, "expected the approval row to still exist after reject"
    assert matching[0].status == "rejected"
    assert matching[0].reason == "manual veto"


def test_approve_list_bad_status_returns_config_error(tmp_path: Path) -> None:
    result = runner.invoke(
        app, ["approve", "list", "--status", "bogus", "--root", str(tmp_path)]
    )
    assert result.exit_code == 2


def test_approve_confirm_unknown_id_returns_config_error(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        ["approve", "confirm", "deadbeefdeadbeef", "--root", str(tmp_path)],
    )
    assert result.exit_code == 2
