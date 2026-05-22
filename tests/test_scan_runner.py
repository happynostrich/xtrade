"""Offline tests for `xtrade.research.runner.run_scan` (Phase 2 Task 6 / S6).

Builds a tmp catalog with one Binance perp + a synthetic sine-wave price
series so the momentum scanner is guaranteed to produce crossover
signals. The runner then writes:

  - signals to `<queue_root>/<date>.jsonl`
  - `<log_dir>/scan_summary.json`

We assert those artefacts plus the in-memory `ScanRunResult`.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest
import yaml
from nautilus_trader.model.data import Bar
from nautilus_trader.model.objects import Price, Quantity
from nautilus_trader.test_kit.providers import TestInstrumentProvider

from xtrade.data.catalog import bar_type_for, open_catalog, parse_bar_spec, write_bars
from xtrade.research.runner import ScanError, run_scan


_MIN_NS = 60 * 1_000_000_000


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _sine_bars(bar_type, instrument, n: int, *, start_ns: int) -> list[Bar]:
    """Synthetic sine-wave OHLCV — picks up momentum crossover signals."""
    pp = instrument.price_precision
    sp = instrument.size_precision
    bars: list[Bar] = []
    for i in range(n):
        ts = start_ns + i * _MIN_NS
        # Two full periods over n bars → guaranteed MA crossovers.
        phase = (i / n) * 4 * math.pi
        close = 30_000.0 + 1_000.0 * math.sin(phase)
        bars.append(
            Bar(
                bar_type=bar_type,
                open=Price.from_str(f"{close:.{pp}f}"),
                high=Price.from_str(f"{close + 5.0:.{pp}f}"),
                low=Price.from_str(f"{close - 5.0:.{pp}f}"),
                close=Price.from_str(f"{close:.{pp}f}"),
                volume=Quantity.from_str(f"{1.0:.{sp}f}"),
                ts_event=ts,
                ts_init=ts,
            )
        )
    return bars


def _write_universe(path: Path, *, binance_symbols: list[str]) -> Path:
    """Build a minimal universe yaml at `path`."""
    doc = {
        "binance": [{"symbol": s} for s in binance_symbols],
    }
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    return path


@pytest.fixture
def populated_env(tmp_path: Path) -> dict[str, Path]:
    """Build a catalog with sine-wave BTCUSDT-PERP bars + matching universe."""
    instrument = TestInstrumentProvider.btcusdt_perp_binance()
    bar_type = bar_type_for(instrument, parse_bar_spec("1m"))
    catalog = open_catalog(tmp_path / "catalog")
    bars = _sine_bars(bar_type, instrument, n=200, start_ns=1_700_000_000_000_000_000)
    write_bars(catalog, instrument, bars)

    universe_path = _write_universe(tmp_path / "universe.yaml", binance_symbols=["BTCUSDT"])

    return {
        "catalog": tmp_path / "catalog",
        "universe": universe_path,
        "queue": tmp_path / "signals",
        "logs": tmp_path / "logs" / "test-run",
    }


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_run_scan_writes_summary_and_signals(populated_env) -> None:
    env = populated_env
    result = run_scan(
        universe_path=env["universe"],
        scanner_name="momentum",
        bar="1m",
        since_ns=None,
        until_ns=None,
        param_grid={"fast": [5, 10], "slow": [20]},
        scoring="sharpe",
        top_k=2,
        queue_root=env["queue"],
        log_dir=env["logs"],
        run_id="test-run",
        catalog_path=env["catalog"],
    )

    assert result.passed is True
    assert result.signals_emitted > 0
    assert result.summary_path.exists()
    assert result.summary_path.name == "scan_summary.json"

    # Summary schema (S8 contract).
    summary = json.loads(result.summary_path.read_text())
    assert summary["run_id"] == "test-run"
    assert summary["scanner"] == "momentum"
    assert summary["universe_size"] == 1
    assert summary["param_combos"] >= 1
    assert summary["signals_emitted"] == result.signals_emitted
    assert summary["top_k"] == 2
    assert summary["elapsed_s"] >= 0.0
    assert summary["errors"] == []
    assert "started_at" in summary
    assert "completed_at" in summary

    # Signal queue has rows on disk.
    jsonl_files = list(env["queue"].glob("*.jsonl"))
    assert jsonl_files, "expected at least one jsonl shard in queue root"
    rows = [json.loads(l) for f in jsonl_files for l in f.read_text().splitlines() if l]
    assert len(rows) == result.signals_emitted


def test_run_scan_top_k_ranked_frame(populated_env) -> None:
    env = populated_env
    result = run_scan(
        universe_path=env["universe"],
        scanner_name="momentum",
        bar="1m",
        since_ns=None,
        until_ns=None,
        param_grid={"fast": [5, 10], "slow": [20, 50]},
        scoring="sharpe",
        top_k=3,
        queue_root=env["queue"],
        log_dir=env["logs"],
        run_id="test-run",
        catalog_path=env["catalog"],
    )
    assert len(result.top_k) <= 3
    assert list(result.top_k.columns) == [
        "scanner", "params", "sharpe", "total_return", "win_rate", "n_trades"
    ]
    # Sorted descending.
    sharpes = result.top_k["sharpe"].tolist()
    assert sharpes == sorted(sharpes, reverse=True)


def test_run_scan_idempotent_dedup(populated_env) -> None:
    """Running the same scan twice writes signals once (queue dedup)."""
    env = populated_env
    kwargs = dict(
        universe_path=env["universe"],
        scanner_name="momentum",
        bar="1m",
        since_ns=None,
        until_ns=None,
        param_grid={"fast": [5], "slow": [20]},
        scoring="sharpe",
        top_k=1,
        queue_root=env["queue"],
        log_dir=env["logs"],
        catalog_path=env["catalog"],
    )
    r1 = run_scan(run_id="run-1", **kwargs)
    r2 = run_scan(run_id="run-2", **kwargs)
    assert r1.signals_emitted > 0
    assert r2.signals_emitted == 0  # all dedup'd against existing queue


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_run_scan_empty_panel_returns_degenerate_summary(tmp_path: Path) -> None:
    """An empty catalog yields a summary with signals_emitted=0; passes."""
    catalog = tmp_path / "empty_catalog"
    open_catalog(catalog)  # creates empty catalog dir
    universe = _write_universe(tmp_path / "universe.yaml", binance_symbols=["BTCUSDT"])

    result = run_scan(
        universe_path=universe,
        scanner_name="momentum",
        bar="1m",
        since_ns=None,
        until_ns=None,
        param_grid={"fast": [5], "slow": [20]},
        scoring="sharpe",
        top_k=1,
        queue_root=tmp_path / "signals",
        log_dir=tmp_path / "logs",
        run_id="empty-run",
        catalog_path=catalog,
    )
    assert result.signals_emitted == 0
    assert result.passed is True  # non-strict
    summary = json.loads(result.summary_path.read_text())
    assert summary["signals_emitted"] == 0
    assert summary["param_combos"] == 0
    assert "panel is empty" in " ".join(summary["errors"])


def test_run_scan_strict_with_no_signals_fails(tmp_path: Path) -> None:
    catalog = tmp_path / "empty_catalog"
    open_catalog(catalog)
    universe = _write_universe(tmp_path / "universe.yaml", binance_symbols=["BTCUSDT"])
    result = run_scan(
        universe_path=universe,
        scanner_name="momentum",
        bar="1m",
        since_ns=None,
        until_ns=None,
        param_grid={"fast": [5], "slow": [20]},
        scoring="sharpe",
        top_k=1,
        queue_root=tmp_path / "signals",
        log_dir=tmp_path / "logs",
        run_id="strict-run",
        catalog_path=catalog,
        strict=True,
    )
    assert result.signals_emitted == 0
    assert result.passed is False


def test_run_scan_all_symbols_unresolvable_raises(tmp_path: Path) -> None:
    """Universe with no resolvable instruments → ScanError."""
    universe = tmp_path / "universe.yaml"
    universe.write_text(
        yaml.safe_dump({"binance": [{"symbol": "NOTAREALCOIN123"}]}),
        encoding="utf-8",
    )
    catalog = tmp_path / "catalog"
    open_catalog(catalog)
    with pytest.raises(ScanError, match="zero resolvable instruments"):
        run_scan(
            universe_path=universe,
            scanner_name="momentum",
            bar="1m",
            since_ns=None,
            until_ns=None,
            param_grid={"fast": [5], "slow": [20]},
            scoring="sharpe",
            top_k=1,
            queue_root=tmp_path / "signals",
            log_dir=tmp_path / "logs",
            run_id="bad",
            catalog_path=catalog,
        )


def test_run_scan_uses_default_grid_when_param_grid_is_none(populated_env) -> None:
    env = populated_env
    result = run_scan(
        universe_path=env["universe"],
        scanner_name="momentum",
        bar="1m",
        since_ns=None,
        until_ns=None,
        param_grid=None,  # → MomentumScanner.default_param_grid()
        scoring="sharpe",
        top_k=2,
        queue_root=env["queue"],
        log_dir=env["logs"],
        run_id="default-grid",
        catalog_path=env["catalog"],
    )
    assert result.passed
    assert result.summary["param_combos"] >= 1


def test_run_scan_summary_is_json_serialisable(populated_env) -> None:
    env = populated_env
    result = run_scan(
        universe_path=env["universe"],
        scanner_name="momentum",
        bar="1m",
        since_ns=None,
        until_ns=None,
        param_grid={"fast": [5], "slow": [20]},
        scoring="sharpe",
        top_k=1,
        queue_root=env["queue"],
        log_dir=env["logs"],
        run_id="serialise",
        catalog_path=env["catalog"],
    )
    # Already round-tripped on disk by `run_scan`; sanity dump from memory.
    payload = json.dumps(result.summary)
    assert "run_id" in payload


# ---------------------------------------------------------------------------
# S8: scan_summary.json schema contract
# ---------------------------------------------------------------------------


_REQUIRED_SUMMARY_FIELDS = frozenset({
    "run_id",
    "started_at",
    "completed_at",
    "universe_size",
    "scanner",
    "param_combos",
    "signals_emitted",
    "top_k",
    "elapsed_s",
    "errors",
})


def test_scan_summary_contains_every_required_field(populated_env) -> None:
    """S8 contract: `scan_summary.json` must include every field listed
    in `docs/phase2_brief.md §5 Task 8`."""
    env = populated_env
    result = run_scan(
        universe_path=env["universe"],
        scanner_name="momentum",
        bar="1m",
        since_ns=None,
        until_ns=None,
        param_grid={"fast": [5], "slow": [20]},
        scoring="sharpe",
        top_k=1,
        queue_root=env["queue"],
        log_dir=env["logs"],
        run_id="schema-check",
        catalog_path=env["catalog"],
    )
    on_disk = json.loads(result.summary_path.read_text())
    missing = _REQUIRED_SUMMARY_FIELDS - on_disk.keys()
    assert not missing, f"scan_summary.json missing required fields: {sorted(missing)}"
    # Result.summary and on-disk file must agree byte-for-byte (atomic
    # write contract).
    assert on_disk == result.summary


def test_scan_summary_field_types(populated_env) -> None:
    env = populated_env
    result = run_scan(
        universe_path=env["universe"],
        scanner_name="momentum",
        bar="1m",
        since_ns=None,
        until_ns=None,
        param_grid={"fast": [5], "slow": [20]},
        scoring="sharpe",
        top_k=1,
        queue_root=env["queue"],
        log_dir=env["logs"],
        run_id="schema-types",
        catalog_path=env["catalog"],
    )
    s = result.summary
    assert isinstance(s["run_id"], str) and s["run_id"]
    assert isinstance(s["started_at"], str)
    assert isinstance(s["completed_at"], str)
    assert isinstance(s["universe_size"], int) and s["universe_size"] >= 0
    assert isinstance(s["scanner"], str)
    assert isinstance(s["param_combos"], int) and s["param_combos"] >= 0
    assert isinstance(s["signals_emitted"], int) and s["signals_emitted"] >= 0
    assert isinstance(s["top_k"], int) and s["top_k"] >= 0
    assert isinstance(s["elapsed_s"], (int, float)) and s["elapsed_s"] >= 0.0
    assert isinstance(s["errors"], list)
    # Timestamps must round-trip through fromisoformat.
    import datetime as dt

    started = dt.datetime.fromisoformat(s["started_at"])
    completed = dt.datetime.fromisoformat(s["completed_at"])
    assert completed >= started
