"""CLI exit-code tests for `xtrade scan ...` subcommands (Phase 2 / S6).

We avoid full happy-path catalog setup here (that's covered by
`test_scan_runner.py`); these tests exercise the CLI plumbing — help
output, argument validation, exit-code mapping — and a single end-to-
end success on a tmp catalog to prove the wiring.
"""

from __future__ import annotations

import datetime as dt
import json
import math
from pathlib import Path

import yaml
from nautilus_trader.model.data import Bar
from nautilus_trader.model.objects import Price, Quantity
from nautilus_trader.test_kit.providers import TestInstrumentProvider
import pytest
from typer.testing import CliRunner

from xtrade.cli import app
from xtrade.data.catalog import bar_type_for, open_catalog, parse_bar_spec, write_bars
from xtrade.research.signals import Signal, SignalQueue


runner = CliRunner(mix_stderr=False)


_MIN_NS = 60 * 1_000_000_000


@pytest.fixture(autouse=True)
def _isolated_logs_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect `run_with_logging`'s default logs root into tmp_path.

    Without this fixture, `xtrade scan run` happy-path tests would create
    real directories under `<repo>/logs/<run-id>/`, leaking state between
    test runs. We override the module-level `DEFAULT_LOGS_ROOT` so each
    test gets its own ephemeral root.
    """
    import xtrade.observability as obs

    logs_root = tmp_path / "logs_isolated"
    monkeypatch.setattr(obs, "DEFAULT_LOGS_ROOT", logs_root)
    return logs_root


# ---------------------------------------------------------------------------
# Help / discoverability
# ---------------------------------------------------------------------------


def test_scan_help_lists_subcommands() -> None:
    result = runner.invoke(app, ["scan", "--help"])
    assert result.exit_code == 0
    assert "universe" in result.stdout
    assert "run" in result.stdout
    assert "inspect" in result.stdout


def test_scan_run_help_runs() -> None:
    result = runner.invoke(app, ["scan", "run", "--help"])
    assert result.exit_code == 0
    assert "--scanner" in result.stdout
    assert "--universe" in result.stdout


def test_scan_inspect_help_runs() -> None:
    result = runner.invoke(app, ["scan", "inspect", "--help"])
    assert result.exit_code == 0
    assert "--source" in result.stdout
    assert "--symbol" in result.stdout


# ---------------------------------------------------------------------------
# Config-error branches (exit 2)
# ---------------------------------------------------------------------------


def test_scan_run_unknown_scanner_exits_2(tmp_path: Path) -> None:
    universe = tmp_path / "u.yaml"
    universe.write_text(yaml.safe_dump({"binance": [{"symbol": "BTCUSDT"}]}))
    result = runner.invoke(
        app,
        ["scan", "run", "--universe", str(universe), "--scanner", "does_not_exist"],
    )
    assert result.exit_code == 2
    assert "scanner must be one of" in result.stderr


def test_scan_run_missing_universe_yaml_exits_2(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        ["scan", "run", "--universe", str(tmp_path / "missing.yaml"), "--scanner", "momentum"],
    )
    assert result.exit_code == 2
    assert "does not exist" in result.stderr


def test_scan_run_until_before_since_exits_2(tmp_path: Path) -> None:
    universe = tmp_path / "u.yaml"
    universe.write_text(yaml.safe_dump({"binance": [{"symbol": "BTCUSDT"}]}))
    result = runner.invoke(
        app,
        [
            "scan", "run", "--universe", str(universe), "--scanner", "momentum",
            "--since", "2024-01-10", "--until", "2024-01-05",
        ],
    )
    assert result.exit_code == 2
    assert "must be after" in result.stderr


def test_scan_universe_missing_file_exits_2(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        ["scan", "universe", "--config", str(tmp_path / "no.yaml")],
    )
    assert result.exit_code == 2


# ---------------------------------------------------------------------------
# Happy path: end-to-end scan run against a tmp catalog
# ---------------------------------------------------------------------------


def _seed_catalog(tmp_path: Path) -> Path:
    instrument = TestInstrumentProvider.btcusdt_perp_binance()
    bar_type = bar_type_for(instrument, parse_bar_spec("1m"))
    catalog = open_catalog(tmp_path / "catalog")
    bars: list[Bar] = []
    pp = instrument.price_precision
    sp = instrument.size_precision
    start_ns = 1_700_000_000_000_000_000
    for i in range(200):
        phase = (i / 200) * 4 * math.pi
        close = 30_000.0 + 1_000.0 * math.sin(phase)
        bars.append(
            Bar(
                bar_type=bar_type,
                open=Price.from_str(f"{close:.{pp}f}"),
                high=Price.from_str(f"{close + 5.0:.{pp}f}"),
                low=Price.from_str(f"{close - 5.0:.{pp}f}"),
                close=Price.from_str(f"{close:.{pp}f}"),
                volume=Quantity.from_str(f"{1.0:.{sp}f}"),
                ts_event=start_ns + i * _MIN_NS,
                ts_init=start_ns + i * _MIN_NS,
            )
        )
    write_bars(catalog, instrument, bars)
    return tmp_path / "catalog"


def test_scan_run_full_loop_writes_summary(
    tmp_path: Path, _isolated_logs_root: Path
) -> None:
    catalog = _seed_catalog(tmp_path)
    universe = tmp_path / "u.yaml"
    universe.write_text(yaml.safe_dump({"binance": [{"symbol": "BTCUSDT"}]}))

    result = runner.invoke(
        app,
        [
            "scan", "run",
            "--universe", str(universe),
            "--scanner", "momentum",
            "--catalog", str(catalog),
            "--queue-root", str(tmp_path / "signals"),
            "--top-k", "2",
            "--run-id", "cli-test",
        ],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "signals_emitted" in result.stdout
    assert "summary:" in result.stdout

    # Queue file written under our tmp queue-root.
    queue_files = list((tmp_path / "signals").glob("*.jsonl"))
    assert queue_files, "expected signal queue files"
    # And scan_summary.json under the isolated logs root.
    summary = _isolated_logs_root / "cli-test" / "scan_summary.json"
    assert summary.exists()


def test_scan_run_strict_with_empty_catalog_exits_1(tmp_path: Path) -> None:
    """No bars → no signals; --strict maps to exit 1."""
    catalog = tmp_path / "empty"
    open_catalog(catalog)
    universe = tmp_path / "u.yaml"
    universe.write_text(yaml.safe_dump({"binance": [{"symbol": "BTCUSDT"}]}))
    result = runner.invoke(
        app,
        [
            "scan", "run",
            "--universe", str(universe),
            "--scanner", "momentum",
            "--catalog", str(catalog),
            "--queue-root", str(tmp_path / "signals"),
            "--strict",
            "--run-id", "empty-strict",
        ],
    )
    assert result.exit_code == 1
    assert "FAILED" in result.stderr


def test_scan_inspect_empty_queue_runs_clean(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        ["scan", "inspect", "--queue-root", str(tmp_path / "nope")],
    )
    assert result.exit_code == 0
    assert "does not exist" in result.stdout


def test_scan_inspect_lists_existing_signals(tmp_path: Path) -> None:
    queue = SignalQueue(tmp_path / "signals")
    queue.append([
        Signal(
            symbol="BTCUSDT-PERP.BINANCE",
            venue="binance",
            direction="LONG",
            strength=0.7,
            generated_at=dt.datetime(2024, 1, 1, 12, tzinfo=dt.timezone.utc),
            source="momentum:abc12345",
        )
    ])
    result = runner.invoke(
        app,
        ["scan", "inspect", "--queue-root", str(tmp_path / "signals")],
    )
    assert result.exit_code == 0
    assert "BTCUSDT-PERP.BINANCE" in result.stdout
    assert "LONG" in result.stdout
    assert "momentum:abc12345" in result.stdout


def test_scan_inspect_filter_by_symbol(tmp_path: Path) -> None:
    queue = SignalQueue(tmp_path / "signals")
    queue.append([
        Signal(
            symbol="BTCUSDT-PERP.BINANCE", venue="binance", direction="LONG",
            strength=0.7, generated_at=dt.datetime(2024, 1, 1, 12, tzinfo=dt.timezone.utc),
            source="momentum:abc12345",
        ),
        Signal(
            symbol="ETHUSDT-PERP.BINANCE", venue="binance", direction="LONG",
            strength=0.6, generated_at=dt.datetime(2024, 1, 1, 13, tzinfo=dt.timezone.utc),
            source="momentum:abc12345",
        ),
    ])
    result = runner.invoke(
        app,
        [
            "scan", "inspect",
            "--queue-root", str(tmp_path / "signals"),
            "--symbol", "BTCUSDT-PERP.BINANCE",
        ],
    )
    assert result.exit_code == 0
    assert "BTCUSDT" in result.stdout
    assert "ETHUSDT" not in result.stdout
