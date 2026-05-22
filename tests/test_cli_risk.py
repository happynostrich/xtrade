"""Tests for `xtrade risk dry-run` (Phase 3.5 hardening).

These tests pin the contract of the CLI wrapper around
`xtrade.risk.dry_run.dry_run`:

  - happy path (synthetic signal, no rules) → exit 0, intent approved;
  - `--json` emits a single JSON document, valid against the
    `DryRunReport.to_dict()` shape;
  - `--risk-config` plugs `load_rules_from_yaml` in;
  - operator errors (unknown strategy, mutually-exclusive flags, bad
    decimals) exit with the Phase-1 contract code 2;
  - `--signals-from` integrates with `SignalQueue` and `SignalConsumer`.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from typer.testing import CliRunner

import xtrade.strategy  # noqa: F401 — registers momentum_follow
from xtrade.cli import app
from xtrade.research.signals import Signal, SignalQueue


runner = CliRunner()

UTC = dt.timezone.utc


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


def test_risk_dry_run_default_happy_path() -> None:
    """No risk-config + default account → 1 intent, 0 rules, approved."""
    result = runner.invoke(
        app,
        ["risk", "dry-run", "--strategy", "momentum_follow", "--instrument", "BTCUSDT-PERP.BINANCE"],
    )
    assert result.exit_code == 0, result.output
    assert "intents generated:  1" in result.output
    assert "intents approved:   1" in result.output
    assert "APPROVED: BUY" in result.output


def test_risk_dry_run_json_output_is_valid() -> None:
    """`--json` emits the full report dict in one JSON document."""
    result = runner.invoke(
        app,
        [
            "risk", "dry-run",
            "--strategy", "momentum_follow",
            "--instrument", "BTCUSDT-PERP.BINANCE",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["strategy"] == "momentum_follow"
    assert payload["intents_generated"] == 1
    assert payload["intents_approved"] == 1
    # account block matches AccountSnapshot shape.
    assert "cash_usd" in payload["account"]
    assert "mark_prices" in payload["account"]


def test_risk_dry_run_loads_risk_yaml(tmp_path: Path) -> None:
    """`--risk-config` plugs four rules in; output mentions them."""
    risk_yaml = tmp_path / "risk.yaml"
    risk_yaml.write_text(
        "\n".join(
            [
                "max_notional_per_order_usd: 1000",
                "max_position_per_symbol_usd: 5000",
                "max_total_notional_usd: 20000",
                "max_drawdown_pct: 0.10",
            ]
        ),
        encoding="utf-8",
    )
    result = runner.invoke(
        app,
        [
            "risk", "dry-run",
            "--strategy", "momentum_follow",
            "--instrument", "BTCUSDT-PERP.BINANCE",
            "--risk-config", str(risk_yaml),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "rules:              4" in result.output
    # All four rule names appear in the verdict matrix.
    assert "max_notional_per_order" in result.output
    assert "max_drawdown_pct" in result.output


def test_risk_dry_run_tight_cap_flips_to_rejected(tmp_path: Path) -> None:
    """A 1-USD per-order cap is impossible to meet → intent rejected."""
    risk_yaml = tmp_path / "risk.yaml"
    risk_yaml.write_text("max_notional_per_order_usd: 1\n", encoding="utf-8")
    result = runner.invoke(
        app,
        [
            "risk", "dry-run",
            "--strategy", "momentum_follow",
            "--instrument", "BTCUSDT-PERP.BINANCE",
            "--risk-config", str(risk_yaml),
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["intents_approved"] == 0
    assert payload["intents_rejected"] == 1
    rr = payload["intents"][0]["rule_results"]
    assert any(r["name"] == "max_notional_per_order" and r["ok"] is False for r in rr)


def test_risk_dry_run_reads_signal_from_queue(tmp_path: Path) -> None:
    """`--signals-from` replays the newest matching signal from a SignalQueue."""
    queue_root = tmp_path / "signals"
    queue = SignalQueue(queue_root)
    queue.append([
        Signal(
            symbol="BTCUSDT-PERP.BINANCE",
            venue="binance",
            direction="LONG",
            strength=0.42,
            generated_at=dt.datetime(2026, 5, 22, 11, 0, 0, tzinfo=UTC),
            source="momentum:test",
            metadata={"last_price": "50000"},
        ),
    ])
    result = runner.invoke(
        app,
        [
            "risk", "dry-run",
            "--strategy", "momentum_follow",
            "--instrument", "BTCUSDT-PERP.BINANCE",
            "--signals-from", str(queue_root),
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["signal"]["source"] == "momentum:test"
    assert payload["intents_generated"] == 1


# ---------------------------------------------------------------------------
# Operator errors (exit code 2)
# ---------------------------------------------------------------------------


def test_risk_dry_run_unknown_strategy_exits_2() -> None:
    result = runner.invoke(
        app,
        [
            "risk", "dry-run",
            "--strategy", "no_such_strategy",
            "--instrument", "BTCUSDT-PERP.BINANCE",
        ],
    )
    assert result.exit_code == 2
    assert "unknown strategy" in result.output


def test_risk_dry_run_mutually_exclusive_sources_exit_2(tmp_path: Path) -> None:
    """Cannot combine --signals-from with --synthetic-direction."""
    result = runner.invoke(
        app,
        [
            "risk", "dry-run",
            "--strategy", "momentum_follow",
            "--instrument", "BTCUSDT-PERP.BINANCE",
            "--signals-from", str(tmp_path),
            "--synthetic-direction", "LONG",
        ],
    )
    assert result.exit_code == 2
    assert "mutually exclusive" in result.output


def test_risk_dry_run_bad_cash_decimal_exits_2() -> None:
    result = runner.invoke(
        app,
        [
            "risk", "dry-run",
            "--strategy", "momentum_follow",
            "--instrument", "BTCUSDT-PERP.BINANCE",
            "--cash", "not-a-number",
        ],
    )
    assert result.exit_code == 2


def test_risk_dry_run_bad_positions_format_exits_2() -> None:
    """`--positions` entries must be KEY=VAL."""
    result = runner.invoke(
        app,
        [
            "risk", "dry-run",
            "--strategy", "momentum_follow",
            "--instrument", "BTCUSDT-PERP.BINANCE",
            "--positions", "missing_equals",
        ],
    )
    assert result.exit_code != 0


def test_risk_dry_run_missing_signal_in_queue_exits_2(tmp_path: Path) -> None:
    """Empty signal queue → exit 2 with helpful message."""
    queue_root = tmp_path / "signals"
    SignalQueue(queue_root)  # creates the root, no rows
    result = runner.invoke(
        app,
        [
            "risk", "dry-run",
            "--strategy", "momentum_follow",
            "--instrument", "BTCUSDT-PERP.BINANCE",
            "--signals-from", str(queue_root),
        ],
    )
    assert result.exit_code == 2
    assert "no signals matched" in result.output


def test_risk_dry_run_help_text_documents_no_io() -> None:
    """The help blurb should mention the no-I/O contract."""
    result = runner.invoke(app, ["risk", "dry-run", "--help"])
    assert result.exit_code == 0
    assert "no I/O" in result.output or "no-I/O" in result.output or "no orders" in result.output
