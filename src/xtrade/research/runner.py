"""End-to-end scan runner (Phase 2 Task 6 / S6, plus S8 scan_summary.json).

`run_scan(...)` is the function the `xtrade scan run` CLI wraps:

  1. Parse the universe yaml → list of `(venue, symbol)`.
  2. Resolve each entry to a Nautilus `Instrument` + `BarType`.
  3. `bars_to_panel(catalog, bar_types, since_ns, until_ns)` to one
     close panel across the universe.
  4. `run_grid(scanner, panel, grid)` to rank parameter combos.
  5. Materialise top-k combos back into Signals (one per scanner.run row).
  6. `SignalQueue.append(signals)` to the on-disk jsonl queue.
  7. Write `scan_summary.json` into the `logs/<run-id>/` dir (fields
     fixed by Phase 2 brief §5 Task 8).

`ScanRunResult` is a frozen dataclass so the CLI / tests can introspect
the run without re-parsing files.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import logging
import time
from pathlib import Path
from typing import Any

import pandas as pd
from nautilus_trader.model.data import BarType

from xtrade.data.catalog import bar_type_for, open_catalog, parse_bar_spec
from xtrade.data.instruments import InstrumentResolutionError, resolve
from xtrade.obs import emit_event
from xtrade.research.frames import bars_to_panel
from xtrade.research.gridsearch import run_grid
from xtrade.research.scanners.base import Scanner, get_scanner
from xtrade.research.signals import Signal, SignalQueue
from xtrade.research.universe import (
    SymbolSpec,
    UniverseConfig,
    UniverseConfigError,
    load_universe,
)


# Phase 5 A3 — structured event logger. The scanner is the only writer
# under this name; `tests/test_log_event.py` asserts every event emitted
# from this module carries the `scanner.` prefix.
log = logging.getLogger("xtrade.scanner")


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class ScanRunResult:
    """Headline result of a `run_scan` call.

    `summary` is the same dict written to `scan_summary.json`. `top_k`
    is the head of the gridsearch result (DataFrame, not serialised).
    """

    run_id: str
    summary: dict[str, Any]
    summary_path: Path
    top_k: pd.DataFrame
    signals_emitted: int
    passed: bool


class ScanError(RuntimeError):
    """Raised on business failure (empty universe, no bars, etc.)."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_bar_types(
    symbols: tuple[SymbolSpec, ...],
    bar: str,
    *,
    run_id: str | None = None,
) -> tuple[list[BarType], list[str]]:
    """Resolve a sequence of `SymbolSpec` to (bar_types, skipped) tuples.

    Symbols whose venue resolver fails (e.g. unknown ticker on Binance)
    are skipped and returned in the second tuple element so the summary
    can record them rather than aborting the entire run. When `run_id`
    is provided, one `scanner.signal.skipped` event is emitted per
    skipped symbol so journalctl can show the root cause.
    """
    spec = parse_bar_spec(bar)
    bar_types: list[BarType] = []
    skipped: list[str] = []
    for s in symbols:
        try:
            instrument = resolve(s.venue, s.symbol)
        except InstrumentResolutionError as exc:
            skipped.append(f"{s.venue}:{s.symbol} ({exc})")
            if run_id is not None:
                emit_event(
                    log,
                    "scanner.signal.skipped",
                    run_id=run_id,
                    instrument=f"{s.venue}:{s.symbol}",
                    reason=f"unresolved: {exc}",
                )
            continue
        bar_types.append(bar_type_for(instrument, spec))
    return bar_types, skipped


def _venue_for_instrument(instrument_id_str: str) -> str:
    """Extract a CLI-style venue tag from a Nautilus InstrumentId string.

    Format is `<symbol>.<VENUE>` (e.g. `BTCUSDT-PERP.BINANCE`).
    Returns the lower-cased venue tag, falling back to ``"unknown"`` if
    the string doesn't fit the expected shape.
    """
    if "." not in instrument_id_str:
        return "unknown"
    venue_raw = instrument_id_str.rsplit(".", 1)[1].lower()
    # Map Nautilus venue ids back to CLI tags. Binance perp catalog uses
    # the bare "BINANCE" venue id.
    if venue_raw.startswith("binance"):
        return "binance"
    if venue_raw.startswith("hyperliquid"):
        return "hyperliquid"
    return venue_raw


def _records_to_signals(
    records: pd.DataFrame,
    *,
    generated_at: dt.datetime,
    scanner_name: str,
) -> list[Signal]:
    """Convert the long-format scanner records into `Signal` objects."""
    if records.empty:
        return []
    sigs: list[Signal] = []
    for row in records.itertuples(index=False):
        sym = str(row.symbol)
        venue = _venue_for_instrument(sym)
        # The records' ts_event is the bar timestamp the signal fired on;
        # we keep that as `generated_at` so dedup keys match across runs.
        ts_event: pd.Timestamp = row.ts_event
        if ts_event.tzinfo is None:
            ts_event = ts_event.tz_localize("UTC")
        try:
            params = json.loads(row.params)
        except (TypeError, ValueError):
            params = {}
        sigs.append(
            Signal(
                symbol=sym,
                venue=venue,
                direction=row.direction,
                strength=float(row.strength),
                generated_at=ts_event.to_pydatetime(),
                source=str(row.source),
                metadata={"scanner": scanner_name, "params": params},
            )
        )
    return sigs


# ---------------------------------------------------------------------------
# Main entry
# ---------------------------------------------------------------------------


def run_scan(
    *,
    universe_path: Path | str,
    scanner_name: str,
    bar: str,
    since_ns: int | None,
    until_ns: int | None,
    param_grid: dict[str, list[Any]] | None,
    scoring: str,
    top_k: int,
    queue_root: Path,
    log_dir: Path,
    run_id: str,
    catalog_path: Path | None = None,
    strict: bool = False,
) -> ScanRunResult:
    """Run one scanner over a universe and persist the results.

    See module docstring for the algorithm. The function performs I/O
    (reads catalog, writes signals jsonl + scan_summary.json) but does
    not touch any live execution paths.

    Parameters
    ----------
    strict
        If True and zero signals were emitted, the result's `passed=False`
        (caller maps to exit 1). If False, an empty run still passes
        (caller prints "no signals" but exits 0).
    """
    started_at = dt.datetime.now(tz=dt.timezone.utc)
    t0 = time.monotonic()

    try:
        universe = load_universe(universe_path)
        scanner_cls = get_scanner(scanner_name)
        scanner: Scanner = scanner_cls()

        # Phase 5 A3 — structured run header. instruments_count is the
        # raw universe size; the per-instrument skip events below report
        # what later got dropped.
        emit_event(
            log,
            "scanner.run.start",
            run_id=run_id,
            universe_path=str(universe_path),
            instruments_count=len(universe),
            scanner=scanner_name,
            bar=bar,
        )

        bar_types, skipped = _resolve_bar_types(
            universe.symbols, bar, run_id=run_id
        )
        if not bar_types:
            raise ScanError(
                f"universe yielded zero resolvable instruments (skipped={skipped})"
            )

        catalog = open_catalog(catalog_path)
        panel = bars_to_panel(
            catalog, bar_types, since_ns=since_ns, until_ns=until_ns, field="close"
        )

        if panel.empty:
            # Empty catalog or empty window — write a degenerate summary
            # and bail rather than letting vectorbt explode.
            completed_at = dt.datetime.now(tz=dt.timezone.utc)
            elapsed = time.monotonic() - t0
            summary = _build_summary(
                run_id=run_id,
                started_at=started_at,
                completed_at=completed_at,
                universe=universe,
                skipped=skipped,
                scanner_name=scanner_name,
                param_combos=0,
                signals_emitted=0,
                top_k=top_k,
                elapsed_s=elapsed,
                errors=["panel is empty: no bars in catalog for requested window"],
            )
            summary_path = _write_summary(log_dir, summary)
            emit_event(
                log,
                "scanner.run.complete",
                run_id=run_id,
                instruments_count=len(universe),
                signals_emitted=0,
                duration_s=round(elapsed, 3),
                reason="panel_empty",
            )
            return ScanRunResult(
                run_id=run_id,
                summary=summary,
                summary_path=summary_path,
                top_k=pd.DataFrame(),
                signals_emitted=0,
                passed=not strict,
            )

        grid = param_grid if param_grid is not None else scanner.default_param_grid()
        ranked = run_grid(scanner, panel, grid, scoring=scoring, top_k=top_k)

        # Emit signals only for the top-k params. We *do not* emit one
        # signal per (ts_event, symbol, params) combination across the
        # whole grid — the queue would explode. Top-k is the contract.
        signals: list[Signal] = []
        for row in ranked.itertuples(index=False):
            params = json.loads(row.params)
            records = scanner.run(panel, params)
            new_signals = _records_to_signals(
                records, generated_at=started_at, scanner_name=scanner_name
            )
            for sig in new_signals:
                # Phase 5 A3 — one event per signal handed to the queue.
                # `signal_id` aligns with `Signal.source` (the dedup key
                # the queue uses), making journalctl ↔ jsonl traceable.
                emit_event(
                    log,
                    "scanner.signal.emitted",
                    run_id=run_id,
                    instrument=sig.symbol,
                    signal_id=sig.source,
                    decision=sig.direction,
                )
            signals.extend(new_signals)

        queue = SignalQueue(queue_root)
        written = queue.append(signals)

        completed_at = dt.datetime.now(tz=dt.timezone.utc)
        elapsed = time.monotonic() - t0
        summary = _build_summary(
            run_id=run_id,
            started_at=started_at,
            completed_at=completed_at,
            universe=universe,
            skipped=skipped,
            scanner_name=scanner_name,
            param_combos=int(len(ranked)),
            signals_emitted=written,
            top_k=top_k,
            elapsed_s=elapsed,
            errors=[],
        )
        summary_path = _write_summary(log_dir, summary)

        emit_event(
            log,
            "scanner.run.complete",
            run_id=run_id,
            instruments_count=len(universe),
            signals_emitted=written,
            duration_s=round(elapsed, 3),
        )

        passed = True if not strict else written > 0
        return ScanRunResult(
            run_id=run_id,
            summary=summary,
            summary_path=summary_path,
            top_k=ranked,
            signals_emitted=written,
            passed=passed,
        )
    except Exception as exc:
        # Phase 5 A3 — terminal-error envelope. We emit before re-raising
        # so journalctl always shows a paired `scanner.run.error` for
        # each `scanner.run.start` that didn't reach `scanner.run.complete`.
        emit_event(
            log,
            "scanner.run.error",
            level=logging.ERROR,
            run_id=run_id,
            error=f"{type(exc).__name__}: {exc}",
        )
        raise


# ---------------------------------------------------------------------------
# Summary plumbing
# ---------------------------------------------------------------------------


def _build_summary(
    *,
    run_id: str,
    started_at: dt.datetime,
    completed_at: dt.datetime,
    universe: UniverseConfig,
    skipped: list[str],
    scanner_name: str,
    param_combos: int,
    signals_emitted: int,
    top_k: int,
    elapsed_s: float,
    errors: list[str],
) -> dict[str, Any]:
    """Build the dict written to `scan_summary.json` (S8 schema)."""
    return {
        "run_id": run_id,
        "started_at": started_at.isoformat(),
        "completed_at": completed_at.isoformat(),
        "universe_size": len(universe),
        "universe_skipped": skipped,
        "scanner": scanner_name,
        "param_combos": param_combos,
        "signals_emitted": signals_emitted,
        "top_k": top_k,
        "elapsed_s": round(elapsed_s, 3),
        "errors": errors,
    }


def _write_summary(log_dir: Path, summary: dict[str, Any]) -> Path:
    """Atomically write `scan_summary.json` under `log_dir`."""
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / "scan_summary.json"
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)
    return path


# Re-export for convenience.
__all__ = ["ScanError", "ScanRunResult", "run_scan", "UniverseConfigError"]
