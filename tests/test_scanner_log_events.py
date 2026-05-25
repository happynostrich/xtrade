"""Tests for `scanner.*` structured events (Phase 5 / Track A3).

What this proves
----------------
* `run_scan(...)` emits the canonical event sequence:
    - exactly one `scanner.run.start` at entry
    - 1..N `scanner.signal.emitted` rows (one per Signal handed to the queue)
    - exactly one `scanner.run.complete` at exit
* When the universe contains an unresolvable ticker, the runner emits
  a `scanner.signal.skipped` row with `reason` populated.
* On terminal failure (e.g. unknown scanner_name) the runner emits a
  `scanner.run.error` envelope at ERROR level before re-raising.
* Every `scanner.*` envelope carries the required fields per the brief:
    - start:    run_id, universe_path, instruments_count
    - emitted:  run_id, instrument, signal_id, decision
    - skipped:  run_id, instrument, reason
    - complete: run_id, instruments_count, signals_emitted, duration_s
    - error:    run_id, error
* Source audit: every `emit_event(log, "...")` call inside the runner
  carries the `scanner.` prefix (mirrors the existing supervisor /
  bridge tests in `tests/test_log_event.py`).

The runner-side fixture borrows the synthetic-sine-wave catalog from
`tests/test_scan_runner.py` so this file stays self-contained.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import math
import re
from pathlib import Path

import pytest
import yaml
from nautilus_trader.model.data import Bar
from nautilus_trader.model.objects import Price, Quantity
from nautilus_trader.test_kit.providers import TestInstrumentProvider

from xtrade.data.catalog import bar_type_for, open_catalog, parse_bar_spec, write_bars
from xtrade.research.runner import ScanError, run_scan


UTC = dt.timezone.utc
SCANNER_LOG = "xtrade.scanner"
_MIN_NS = 60 * 1_000_000_000


# ---- fixtures (mirrors test_scan_runner.py) ------------------------------


def _sine_bars(bar_type, instrument, n: int, *, start_ns: int) -> list[Bar]:
    pp = instrument.price_precision
    sp = instrument.size_precision
    bars: list[Bar] = []
    for i in range(n):
        ts = start_ns + i * _MIN_NS
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
    doc = {"binance": [{"symbol": s} for s in binance_symbols]}
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    return path


@pytest.fixture
def populated_env(tmp_path: Path) -> dict[str, Path]:
    instrument = TestInstrumentProvider.btcusdt_perp_binance()
    bar_type = bar_type_for(instrument, parse_bar_spec("1m"))
    catalog = open_catalog(tmp_path / "catalog")
    bars = _sine_bars(bar_type, instrument, n=200, start_ns=1_700_000_000_000_000_000)
    write_bars(catalog, instrument, bars)
    universe_path = _write_universe(
        tmp_path / "universe.yaml", binance_symbols=["BTCUSDT"]
    )
    return {
        "catalog": tmp_path / "catalog",
        "universe": universe_path,
        "queue": tmp_path / "signals",
        "logs": tmp_path / "logs" / "evt-run",
    }


def _events_of(caplog, name: str) -> list[dict]:
    """Parse caplog records emitted by `xtrade.scanner` matching `event==name`."""
    out: list[dict] = []
    for rec in caplog.records:
        if rec.name != SCANNER_LOG:
            continue
        try:
            payload = json.loads(rec.getMessage())
        except (TypeError, ValueError):
            continue
        if payload.get("event") == name:
            out.append(payload)
    return out


# ---- happy path ----------------------------------------------------------


def test_run_scan_emits_start_and_complete_once(populated_env, caplog) -> None:
    env = populated_env
    with caplog.at_level(logging.INFO, logger=SCANNER_LOG):
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
            run_id="evt-run",
            catalog_path=env["catalog"],
        )

    starts = _events_of(caplog, "scanner.run.start")
    completes = _events_of(caplog, "scanner.run.complete")
    assert len(starts) == 1, starts
    assert len(completes) == 1, completes

    start = starts[0]
    assert start["run_id"] == "evt-run"
    assert start["instruments_count"] == 1
    assert "universe_path" in start
    assert start["scanner"] == "momentum"

    end = completes[0]
    assert end["run_id"] == "evt-run"
    assert end["instruments_count"] == 1
    assert end["signals_emitted"] == result.signals_emitted
    assert isinstance(end["duration_s"], (int, float))
    assert end["duration_s"] >= 0.0


def test_run_scan_emits_signal_emitted_per_signal(populated_env, caplog) -> None:
    env = populated_env
    with caplog.at_level(logging.INFO, logger=SCANNER_LOG):
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
            run_id="evt-emit",
            catalog_path=env["catalog"],
        )

    emitted = _events_of(caplog, "scanner.signal.emitted")
    # The emitted event fires once per Signal — before queue dedup, so
    # the count is >= what landed on disk.
    assert len(emitted) >= result.signals_emitted > 0, (len(emitted), result.signals_emitted)
    for ev in emitted:
        assert ev["run_id"] == "evt-emit"
        assert ev["instrument"], ev
        assert ev["signal_id"], ev
        assert ev["decision"] in ("LONG", "SHORT", "FLAT"), ev


def test_run_scan_no_summary_drift(populated_env, caplog) -> None:
    """Sanity: structured events do NOT mutate the scan_summary.json schema."""
    env = populated_env
    with caplog.at_level(logging.INFO, logger=SCANNER_LOG):
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
            run_id="evt-schema",
            catalog_path=env["catalog"],
        )
    summary = json.loads(result.summary_path.read_text())
    for required in (
        "run_id",
        "started_at",
        "completed_at",
        "universe_size",
        "universe_skipped",
        "scanner",
        "param_combos",
        "signals_emitted",
        "top_k",
        "elapsed_s",
        "errors",
    ):
        assert required in summary, f"summary missing {required}"


# ---- skipped path --------------------------------------------------------


def test_run_scan_emits_signal_skipped_for_unresolvable_symbol(
    populated_env, caplog
) -> None:
    env = populated_env
    # Re-write universe with one resolvable + one bogus symbol.
    _write_universe(
        env["universe"],
        binance_symbols=["BTCUSDT", "DOES_NOT_EXIST_42"],
    )
    with caplog.at_level(logging.INFO, logger=SCANNER_LOG):
        run_scan(
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
            run_id="evt-skip",
            catalog_path=env["catalog"],
        )

    skipped = _events_of(caplog, "scanner.signal.skipped")
    assert skipped, "expected at least one scanner.signal.skipped event"
    for ev in skipped:
        assert ev["run_id"] == "evt-skip"
        assert "DOES_NOT_EXIST_42" in ev["instrument"]
        assert ev["reason"], ev


# ---- error path ----------------------------------------------------------


def test_run_scan_emits_error_on_failure(populated_env, caplog) -> None:
    env = populated_env
    # `get_scanner("bogus_scanner")` raises before any logging happens —
    # but our try/except in run_scan catches it after `start` fires.
    # To force a failure AFTER start, we point at a universe whose only
    # symbol is unresolvable: `_resolve_bar_types` returns empty and
    # ScanError is raised.
    _write_universe(env["universe"], binance_symbols=["DOES_NOT_EXIST_99"])
    with caplog.at_level(logging.ERROR, logger=SCANNER_LOG):
        with pytest.raises(ScanError):
            run_scan(
                universe_path=env["universe"],
                scanner_name="momentum",
                bar="1m",
                since_ns=None,
                until_ns=None,
                param_grid=None,
                scoring="sharpe",
                top_k=1,
                queue_root=env["queue"],
                log_dir=env["logs"],
                run_id="evt-error",
                catalog_path=env["catalog"],
            )

    errors = _events_of(caplog, "scanner.run.error")
    assert len(errors) == 1, errors
    err = errors[0]
    assert err["run_id"] == "evt-error"
    assert "ScanError" in err["error"]
    # Level must be ERROR.
    error_records = [
        r for r in caplog.records
        if r.name == SCANNER_LOG and "scanner.run.error" in r.getMessage()
    ]
    assert error_records[0].levelno == logging.ERROR


# ---- empty-panel path ----------------------------------------------------


def test_run_scan_emits_complete_with_panel_empty_reason(
    tmp_path: Path, caplog
) -> None:
    """A resolvable universe but an empty catalog window → complete event
    with `reason='panel_empty'` and `signals_emitted=0`."""
    # Catalog with a resolvable instrument but no bars in our window.
    instrument = TestInstrumentProvider.btcusdt_perp_binance()
    bar_type = bar_type_for(instrument, parse_bar_spec("1m"))
    catalog = open_catalog(tmp_path / "catalog")
    bars = _sine_bars(bar_type, instrument, n=20, start_ns=1_700_000_000_000_000_000)
    write_bars(catalog, instrument, bars)
    universe_path = _write_universe(
        tmp_path / "universe.yaml", binance_symbols=["BTCUSDT"]
    )

    with caplog.at_level(logging.INFO, logger=SCANNER_LOG):
        result = run_scan(
            universe_path=universe_path,
            scanner_name="momentum",
            bar="1m",
            # Window completely outside the catalog → empty panel.
            since_ns=1_500_000_000_000_000_000,
            until_ns=1_500_000_000_000_000_000 + _MIN_NS,
            param_grid=None,
            scoring="sharpe",
            top_k=1,
            queue_root=tmp_path / "signals",
            log_dir=tmp_path / "logs",
            run_id="evt-empty",
            catalog_path=tmp_path / "catalog",
        )

    assert result.signals_emitted == 0
    completes = _events_of(caplog, "scanner.run.complete")
    assert len(completes) == 1, completes
    assert completes[0]["signals_emitted"] == 0
    assert completes[0]["reason"] == "panel_empty"


# ---- source-regex audit (mirrors test_log_event.py) ----------------------


def test_scanner_event_names_all_have_scanner_prefix() -> None:
    from xtrade.research import runner as runner_mod

    src = Path(runner_mod.__file__).read_text(encoding="utf-8")
    events = re.findall(r'emit_event\(\s*log\s*,\s*"([^"]+)"', src)
    assert events, "scanner runner emits no events?"
    for ev in events:
        assert ev.startswith("scanner."), f"non-scanner event in runner.py: {ev}"

    # And the exact event vocabulary matches the brief.
    expected = {
        "scanner.run.start",
        "scanner.run.complete",
        "scanner.run.error",
        "scanner.signal.emitted",
        "scanner.signal.skipped",
    }
    assert set(events) <= expected, (
        f"unexpected event names in runner: {set(events) - expected}"
    )
    # And the documented set is a subset of what's actually emitted —
    # otherwise the brief promises something we don't ship.
    assert set(events) == expected, (
        f"missing events vs brief: {expected - set(events)}"
    )
