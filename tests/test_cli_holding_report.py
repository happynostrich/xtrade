"""Tests for Phase 6 Task T11 — ``xtrade ops holding_report`` CLI surface.

Covers brief §5 T11 acceptance:

- Happy path: writes ``<output_dir>/holding_<DATE>.json`` with the brief
  schema; exits 0 with stdout banner; dispatches a ``severity=info``
  summary alert when ``--skip-alert`` is *not* given.
- ``--skip-alert``: alerter env presence is irrelevant; no alert is built.
- Config / input validation (exit 2):
  * malformed ``DATE``
  * unknown ``--soft-kill-boundary``
  * unknown ``--direction``
  * non-Decimal ``--current-mark``
  * missing ``--meta-yaml``
- Empty fills with explicit ``--direction=short`` is allowed (a
  "position fully closed yesterday" snapshot writes a zero-state file).
- Invalid fills jsonl raises (exit 2).

These exercise the typer surface — the pure aggregator is covered by
``tests/test_holding_report.py``.
"""

from __future__ import annotations

import json
import os
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from xtrade.cli import app


runner = CliRunner()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _meta_yaml(tmp_path: Path) -> Path:
    yml = tmp_path / "instrument_meta.yaml"
    yml.write_text(
        "SPCXUSDT-PERP.BINANCE:\n"
        "  shares_outstanding: 11_870_000_000\n"
        "  min_qty: '0.001'\n"
        "  qty_step: '0.001'\n"
        "  tick_size: '0.01'\n"
        "  mark_source: oracle\n",
        encoding="utf-8",
    )
    return yml


def _fills_jsonl(tmp_path: Path, *, name: str = "fills.jsonl") -> Path:
    """Two SELL entries (short) at 200/220 + one TP at 195."""
    p = tmp_path / name
    rows = [
        {"ts": "2026-05-23T10:00:00Z", "side": "SELL", "qty": "1",
         "price": "200", "reduce_only": False},
        {"ts": "2026-05-23T11:00:00Z", "side": "SELL", "qty": "1",
         "price": "220", "reduce_only": False},
        {"ts": "2026-05-23T14:00:00Z", "side": "BUY",  "qty": "1",
         "price": "195", "reduce_only": True},
    ]
    p.write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8"
    )
    return p


def _tp_ladder_json(tmp_path: Path) -> Path:
    p = tmp_path / "tp_ladder.json"
    p.write_text(
        json.dumps(
            [
                {"target_mcap_usd": "2000000000000",
                 "filled_qty": "1", "open_qty": "0"},
                {"target_mcap_usd": "1500000000000",
                 "filled_qty": "0", "open_qty": "1"},
            ]
        ),
        encoding="utf-8",
    )
    return p


def _drawdown_json(tmp_path: Path) -> Path:
    p = tmp_path / "drawdown.json"
    p.write_text(
        json.dumps(
            {
                "hwm_usd": "10000",
                "last_equity_usd": "9500",
                "last_update_ts": "2026-05-23T15:00:00Z",
                "halted": False,
                "drawdown_pct": "0.05",
                "halt_pct": "0.1",
            }
        ),
        encoding="utf-8",
    )
    return p


@pytest.fixture(autouse=True)
def _strip_alerter_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make alert dispatch a no-op (best-effort) for tests that don't
    explicitly patch AlertBridge."""
    for key in list(os.environ.keys()):
        if key.startswith("XTRADE_ALERT_"):
            monkeypatch.delenv(key, raising=False)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestHappyPath:
    def test_writes_report_json(self, tmp_path: Path) -> None:
        out_dir = tmp_path / "reports"
        result = runner.invoke(
            app,
            [
                "ops", "holding_report",
                "2026-05-23",
                "--instrument", "SPCXUSDT-PERP.BINANCE",
                "--current-mark", "210",
                "--soft-kill-trigger-mcap-usd", "3500000000000",
                "--soft-kill-boundary", "above",
                "--funding-paid-cumulative", "12.34",
                "--meta-yaml", str(_meta_yaml(tmp_path)),
                "--fills-jsonl", str(_fills_jsonl(tmp_path)),
                "--tp-ladder-json", str(_tp_ladder_json(tmp_path)),
                "--drawdown-state-json", str(_drawdown_json(tmp_path)),
                "--output-dir", str(out_dir),
                "--skip-alert",
            ],
        )
        assert result.exit_code == 0, result.output
        out_path = out_dir / "holding_2026-05-23.json"
        assert out_path.exists()
        body = json.loads(out_path.read_text(encoding="utf-8"))
        # Schema keys (brief §5 T11)
        for k in (
            "date", "instrument", "avg_entry_usd", "current_mark_usd",
            "current_mcap_usd", "pos_size", "unrealized_pnl_usd",
            "realized_pnl_usd", "hwm_drawdown_pct", "tp_ladder_state",
            "soft_kill_distance", "funding_paid_cumulative_usd",
            "generated_at",
        ):
            assert k in body, f"missing schema key: {k}"
        assert body["date"] == "2026-05-23"
        assert body["instrument"] == "SPCXUSDT-PERP.BINANCE"
        # Direction inferred short → pos_size negative after 1 TP fill
        assert Decimal(body["pos_size"]) == Decimal("-1")
        # avg_entry = (200 + 220) / 2 = 210 (running-avg preserved on TP)
        assert Decimal(body["avg_entry_usd"]) == Decimal("210")
        # realized = (210 - 195) * 1 = 15
        assert Decimal(body["realized_pnl_usd"]) == Decimal("15")
        # unrealized at mark=210 on remaining 1 unit short = (210 - 210)*1 = 0
        assert Decimal(body["unrealized_pnl_usd"]) == Decimal("0")
        # mcap_now = 210 * 11_870_000_000 = 2_492_700_000_000 (no sci notation)
        assert "e" not in body["current_mcap_usd"].lower()
        # soft_kill_distance: boundary=above, headroom = (3.5T - 2.49T) / 3.5T > 0
        assert Decimal(body["soft_kill_distance"]["headroom_pct"]) > 0
        # hwm_drawdown_pct lifted from drawdown.json
        assert Decimal(body["hwm_drawdown_pct"]) == Decimal("0.05")
        # tp_ladder_state: two rungs, target_mark = target_mcap / shares
        assert len(body["tp_ladder_state"]) == 2
        rung0 = body["tp_ladder_state"][0]
        assert (
            Decimal(rung0["target_mark_usd"])
            == Decimal("2000000000000") / Decimal("11870000000")
        )
        # banner stdout includes output path
        assert "holding_2026-05-23.json" in result.output

    def test_empty_fills_with_explicit_short_direction(
        self, tmp_path: Path
    ) -> None:
        """A position-closed-yesterday daily snapshot still writes a file."""
        out_dir = tmp_path / "reports"
        empty = tmp_path / "fills.jsonl"
        empty.write_text("", encoding="utf-8")
        result = runner.invoke(
            app,
            [
                "ops", "holding_report",
                "2026-05-24",
                "--current-mark", "210",
                "--direction", "short",
                "--meta-yaml", str(_meta_yaml(tmp_path)),
                "--fills-jsonl", str(empty),
                "--output-dir", str(out_dir),
                "--skip-alert",
            ],
        )
        assert result.exit_code == 0, result.output
        body = json.loads(
            (out_dir / "holding_2026-05-24.json").read_text(encoding="utf-8")
        )
        assert Decimal(body["pos_size"]) == Decimal("0")
        assert Decimal(body["avg_entry_usd"]) == Decimal("0")
        assert Decimal(body["realized_pnl_usd"]) == Decimal("0")
        assert Decimal(body["unrealized_pnl_usd"]) == Decimal("0")
        assert body["tp_ladder_state"] == []

    def test_skip_alert_no_alerter_required(self, tmp_path: Path) -> None:
        """No alerter env vars + --skip-alert → exit 0 cleanly."""
        result = runner.invoke(
            app,
            [
                "ops", "holding_report",
                "2026-05-23",
                "--current-mark", "210",
                "--meta-yaml", str(_meta_yaml(tmp_path)),
                "--fills-jsonl", str(_fills_jsonl(tmp_path)),
                "--output-dir", str(tmp_path / "reports"),
                "--skip-alert",
            ],
        )
        assert result.exit_code == 0, result.output

    def test_default_alert_swallowed_when_env_missing(
        self, tmp_path: Path
    ) -> None:
        """Without --skip-alert, the alerter env-absence is non-fatal."""
        result = runner.invoke(
            app,
            [
                "ops", "holding_report",
                "2026-05-23",
                "--current-mark", "210",
                "--meta-yaml", str(_meta_yaml(tmp_path)),
                "--fills-jsonl", str(_fills_jsonl(tmp_path)),
                "--output-dir", str(tmp_path / "reports"),
                # NOTE: no --skip-alert
            ],
        )
        # AlertBridge.from_env returns None or raises AlertBridgeConfigError
        # when env is empty — both are swallowed by the CLI.
        assert result.exit_code == 0, result.output


# ---------------------------------------------------------------------------
# Input validation — exit 2
# ---------------------------------------------------------------------------


class TestInputValidation:
    def _base_args(self, tmp_path: Path) -> list[str]:
        return [
            "ops", "holding_report",
            "2026-05-23",
            "--current-mark", "210",
            "--meta-yaml", str(_meta_yaml(tmp_path)),
            "--fills-jsonl", str(_fills_jsonl(tmp_path)),
            "--output-dir", str(tmp_path / "reports"),
            "--skip-alert",
        ]

    def test_bad_date_exit_2(self, tmp_path: Path) -> None:
        args = self._base_args(tmp_path)
        # Replace the date positional with garbage.
        args[2] = "2026/05/23"
        result = runner.invoke(app, args)
        assert result.exit_code == 2, result.output

    def test_bad_soft_kill_boundary_exit_2(self, tmp_path: Path) -> None:
        result = runner.invoke(
            app,
            [*self._base_args(tmp_path),
             "--soft-kill-boundary", "sideways"],
        )
        assert result.exit_code == 2, result.output

    def test_bad_direction_exit_2(self, tmp_path: Path) -> None:
        result = runner.invoke(
            app,
            [*self._base_args(tmp_path), "--direction", "sideways"],
        )
        assert result.exit_code == 2, result.output

    def test_bad_current_mark_exit_2(self, tmp_path: Path) -> None:
        args = self._base_args(tmp_path)
        idx = args.index("--current-mark")
        args[idx + 1] = "not-a-decimal"
        result = runner.invoke(app, args)
        assert result.exit_code == 2, result.output

    def test_bad_funding_decimal_exit_2(self, tmp_path: Path) -> None:
        result = runner.invoke(
            app,
            [*self._base_args(tmp_path),
             "--funding-paid-cumulative", "nope"],
        )
        assert result.exit_code == 2, result.output

    def test_bad_soft_kill_trigger_exit_2(self, tmp_path: Path) -> None:
        result = runner.invoke(
            app,
            [*self._base_args(tmp_path),
             "--soft-kill-trigger-mcap-usd", "nope"],
        )
        assert result.exit_code == 2, result.output

    def test_missing_meta_yaml_exit_2(self, tmp_path: Path) -> None:
        args = self._base_args(tmp_path)
        idx = args.index("--meta-yaml")
        args[idx + 1] = str(tmp_path / "nonexistent.yaml")
        result = runner.invoke(app, args)
        assert result.exit_code == 2, result.output

    def test_unknown_instrument_in_meta_exit_2(self, tmp_path: Path) -> None:
        result = runner.invoke(
            app,
            [*self._base_args(tmp_path),
             "--instrument", "GHOSTUSDT-PERP.BINANCE"],
        )
        assert result.exit_code == 2, result.output

    def test_bad_fills_jsonl_exit_2(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.jsonl"
        bad.write_text("this is not json\n", encoding="utf-8")
        args = self._base_args(tmp_path)
        idx = args.index("--fills-jsonl")
        args[idx + 1] = str(bad)
        result = runner.invoke(app, args)
        assert result.exit_code == 2, result.output

    def test_empty_fills_no_direction_exit_2(self, tmp_path: Path) -> None:
        """Empty fills + no explicit --direction must refuse."""
        args = self._base_args(tmp_path)
        # Overwrite the fills jsonl with empty *after* _base_args wrote it.
        idx = args.index("--fills-jsonl")
        empty = tmp_path / "empty_fills.jsonl"
        empty.write_text("", encoding="utf-8")
        args[idx + 1] = str(empty)
        result = runner.invoke(app, args)
        assert result.exit_code == 2, result.output

    def test_zero_current_mark_exit_2(self, tmp_path: Path) -> None:
        args = self._base_args(tmp_path)
        idx = args.index("--current-mark")
        args[idx + 1] = "0"
        result = runner.invoke(app, args)
        assert result.exit_code == 2, result.output

    def test_zero_soft_kill_trigger_exit_2(self, tmp_path: Path) -> None:
        result = runner.invoke(
            app,
            [*self._base_args(tmp_path),
             "--soft-kill-trigger-mcap-usd", "0"],
        )
        assert result.exit_code == 2, result.output


# ---------------------------------------------------------------------------
# Default file paths: absent files are tolerated by the loaders
# ---------------------------------------------------------------------------


class TestDefaultPathsTolerant:
    def test_missing_tp_ladder_and_drawdown_files_ok(
        self, tmp_path: Path
    ) -> None:
        """When TP ladder / drawdown state files are absent on a fresh
        VPS, the loaders return empty/zero and the report still writes."""
        out_dir = tmp_path / "reports"
        result = runner.invoke(
            app,
            [
                "ops", "holding_report",
                "2026-05-23",
                "--current-mark", "210",
                "--meta-yaml", str(_meta_yaml(tmp_path)),
                "--fills-jsonl", str(_fills_jsonl(tmp_path)),
                "--tp-ladder-json", str(tmp_path / "no_ladder.json"),
                "--drawdown-state-json", str(tmp_path / "no_drawdown.json"),
                "--output-dir", str(out_dir),
                "--skip-alert",
            ],
        )
        assert result.exit_code == 0, result.output
        body = json.loads(
            (out_dir / "holding_2026-05-23.json").read_text(encoding="utf-8")
        )
        assert body["tp_ladder_state"] == []
        assert Decimal(body["hwm_drawdown_pct"]) == Decimal("0")


# ---------------------------------------------------------------------------
# Alert dispatch — best-effort
# ---------------------------------------------------------------------------


class _RecordingAlerter:
    """Test double that records dispatched alerts."""

    def __init__(self) -> None:
        self.dispatched: list[dict[str, Any]] = []

    def dispatch_alert(self, **kwargs: Any) -> None:
        self.dispatched.append(kwargs)

    def close(self) -> None:
        pass


class TestAlertDispatch:
    def test_info_alert_dispatched_when_alerter_present(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When AlertBridge.from_env returns a recording alerter, the
        CLI dispatches a severity=info ops.holding_report.daily event
        with the four brief summary keys."""
        recorder = _RecordingAlerter()

        from xtrade.bridge import alerter as alerter_mod
        monkeypatch.setattr(
            alerter_mod.AlertBridge, "from_env",
            classmethod(lambda cls, env: recorder),
        )

        out_dir = tmp_path / "reports"
        result = runner.invoke(
            app,
            [
                "ops", "holding_report",
                "2026-05-23",
                "--current-mark", "210",
                "--meta-yaml", str(_meta_yaml(tmp_path)),
                "--fills-jsonl", str(_fills_jsonl(tmp_path)),
                "--output-dir", str(out_dir),
            ],
        )
        assert result.exit_code == 0, result.output
        assert len(recorder.dispatched) == 1
        msg = recorder.dispatched[0]
        assert msg["severity"] == "info"
        assert msg["event"] == "ops.holding_report.daily"
        assert msg["instrument"] == "SPCXUSDT-PERP.BINANCE"
        fields = msg["fields"]
        assert set(fields.keys()) == {
            "avg_entry", "current_mark", "unrealized_pnl",
            "soft_kill_headroom_pct",
        }
        assert fields["avg_entry"] == "210"
        assert fields["current_mark"] == "210"

    def test_alerter_crash_is_swallowed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An alerter that throws on dispatch must not break the CLI."""

        class CrashAlerter:
            def dispatch_alert(self, **kwargs: Any) -> None:
                raise RuntimeError("alert pipeline down")

            def close(self) -> None:
                pass

        from xtrade.bridge import alerter as alerter_mod
        monkeypatch.setattr(
            alerter_mod.AlertBridge, "from_env",
            classmethod(lambda cls, env: CrashAlerter()),
        )

        result = runner.invoke(
            app,
            [
                "ops", "holding_report",
                "2026-05-23",
                "--current-mark", "210",
                "--meta-yaml", str(_meta_yaml(tmp_path)),
                "--fills-jsonl", str(_fills_jsonl(tmp_path)),
                "--output-dir", str(tmp_path / "reports"),
            ],
        )
        assert result.exit_code == 0, result.output

    def test_skip_alert_does_not_call_from_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        called = {"from_env": 0}

        from xtrade.bridge import alerter as alerter_mod

        def _spy(cls: Any, env: Any) -> None:
            called["from_env"] += 1
            return None

        monkeypatch.setattr(
            alerter_mod.AlertBridge, "from_env", classmethod(_spy)
        )

        result = runner.invoke(
            app,
            [
                "ops", "holding_report",
                "2026-05-23",
                "--current-mark", "210",
                "--meta-yaml", str(_meta_yaml(tmp_path)),
                "--fills-jsonl", str(_fills_jsonl(tmp_path)),
                "--output-dir", str(tmp_path / "reports"),
                "--skip-alert",
            ],
        )
        assert result.exit_code == 0, result.output
        assert called["from_env"] == 0
