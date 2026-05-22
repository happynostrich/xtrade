"""vectorbt ↔ Nautilus parity test (Phase 2 Task 7 / S7).

The brief asks us to prove that the offline `MomentumScanner`
(vectorbt-based) emits the same crossover-bar sequence as a Nautilus
strategy implementing the same arithmetic. We:

  1. Build a tmp catalog with sine-wave BTCUSDT-PERP 1m bars.
  2. Run `MomentumScanner` on the close panel with a fixed parameter
     pair (`fast=5, slow=20`) and collect bar timestamps where the
     `entries` boolean series is True (i.e. fast SMA crossed *above*
     slow SMA).
  3. Spawn a subprocess that runs a Nautilus `MomentumDemoSMA`
     strategy with the same windows through `BacktestEngine`; collect
     bar timestamps when the SMAs flip from fast<slow to fast>=slow
     (long entry). The subprocess hop is required because
     `BacktestEngine.__init__` aborts when called twice in the same
     Python process (`test_backtest_smoke.py` already burns one).
  4. Assert the two timestamp sequences are equal within a tolerance
     of ±1 bar (Nautilus updates indicators on bar close, vectorbt
     evaluates point-wise — they should line up exactly, but the
     brief allows ±1).

The Nautilus side lives in `tests/_parity_nautilus_runner.py`. That
module also defines `MomentumDemoSMA`, the parity-only strategy
fixture (intentionally *not* registered in
`xtrade.backtest.runner._STRATEGY_REGISTRY`).
"""

from __future__ import annotations

import json
import math
import subprocess
import sys
from pathlib import Path

from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.instruments import Instrument
from nautilus_trader.model.objects import Price, Quantity
from nautilus_trader.test_kit.providers import TestInstrumentProvider

from xtrade.data.catalog import bar_type_for, open_catalog, parse_bar_spec, write_bars
from xtrade.research.frames import bars_to_panel
from xtrade.research.scanners.momentum import MomentumScanner


_MIN_NS = 60 * 1_000_000_000
_PARAMS = {"fast": 5, "slow": 20}


# ---------------------------------------------------------------------------
# Catalog seeding
# ---------------------------------------------------------------------------


def _sine_bars(bar_type: BarType, instrument: Instrument, *, n: int, start_ns: int) -> list[Bar]:
    """Synthetic OHLCV with several SMA crossovers in the window."""
    pp = instrument.price_precision
    sp = instrument.size_precision
    bars: list[Bar] = []
    for i in range(n):
        ts = start_ns + i * _MIN_NS
        # ~50-bar period sin wave → multiple fast/slow crossovers.
        mid = 30_000.0 + 250.0 * math.sin(i / 8.0)
        open_p = mid
        close_p = mid + 5.0 * math.sin((i + 1) / 8.0)
        hi = max(open_p, close_p) + 2.0
        lo = min(open_p, close_p) - 2.0
        bars.append(
            Bar(
                bar_type=bar_type,
                open=Price.from_str(f"{open_p:.{pp}f}"),
                high=Price.from_str(f"{hi:.{pp}f}"),
                low=Price.from_str(f"{lo:.{pp}f}"),
                close=Price.from_str(f"{close_p:.{pp}f}"),
                volume=Quantity.from_str(f"{1.0:.{sp}f}"),
                ts_event=ts,
                ts_init=ts,
            )
        )
    return bars


def _seed_catalog(tmp_path: Path, *, n: int = 300) -> tuple[Path, BarType]:
    instrument = TestInstrumentProvider.btcusdt_perp_binance()
    bar_type = bar_type_for(instrument, parse_bar_spec("1m"))
    catalog = open_catalog(tmp_path / "catalog")
    bars = _sine_bars(
        bar_type, instrument, n=n, start_ns=1_700_000_000_000_000_000
    )
    write_bars(catalog, instrument, bars)
    return tmp_path / "catalog", bar_type


# ---------------------------------------------------------------------------
# Two sides
# ---------------------------------------------------------------------------


def _vectorbt_entry_timestamps(catalog_path: Path, bar_type: BarType) -> list[int]:
    """Run `MomentumScanner.compute_signals` and extract `ts_event` ns
    for every True row in the `entries` series."""
    panel = bars_to_panel(catalog_path, [bar_type], field="close")
    entries, _exits = MomentumScanner().compute_signals(panel, _PARAMS)
    series = entries.iloc[:, 0]  # single symbol
    ts_index = series.index[series.values.astype(bool)]
    return [int(t.value) for t in ts_index]


def _nautilus_entry_timestamps_subprocess(
    catalog_path: Path, tmp_path: Path
) -> list[int]:
    """Shell out to `tests._parity_nautilus_runner` and parse its stdout."""
    log_dir = tmp_path / "naut_logs"
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "tests._parity_nautilus_runner",
            str(catalog_path),
            str(log_dir),
            str(_PARAMS["fast"]),
            str(_PARAMS["slow"]),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, (
        f"parity runner failed (rc={proc.returncode}):\n"
        f"stdout: {proc.stdout!r}\nstderr: {proc.stderr!r}"
    )
    # The runner prints a single JSON line — find it (Nautilus may also
    # log to stdout from its Rust side).
    payload = None
    for line in proc.stdout.splitlines():
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            payload = json.loads(line)
            break
    assert payload is not None, (
        f"could not find JSON payload in runner stdout: {proc.stdout!r}"
    )
    return [int(x) for x in payload["long_entry_ts"]]


def _match_within_tolerance(
    vbt_ts: list[int], naut_ts: list[int], *, tolerance_bars: int = 1
) -> tuple[int, int]:
    """Return `(matched, unmatched)` counts using a one-pass greedy
    pairing with ±`tolerance_bars` tolerance (1 bar = 60s in ns)."""
    tol_ns = tolerance_bars * _MIN_NS
    naut = list(naut_ts)
    matched = 0
    for v in vbt_ts:
        for i, n in enumerate(naut):
            if abs(n - v) <= tol_ns:
                matched += 1
                naut.pop(i)
                break
    unmatched = len(vbt_ts) - matched
    return matched, unmatched


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


def test_momentum_scanner_matches_nautilus_demo_sma(tmp_path: Path) -> None:
    """vectorbt entries ≈ Nautilus long-entries on identical synthetic bars.

    SMA is closed-form so we assert exact alignment, not just the
    ±1-bar tolerance the brief allows. (The tolerance assertion is
    kept too as a soft floor for any future drift.)
    """
    catalog_path, bar_type = _seed_catalog(tmp_path, n=300)

    vbt_ts = _vectorbt_entry_timestamps(catalog_path, bar_type)
    naut_ts = _nautilus_entry_timestamps_subprocess(catalog_path, tmp_path)

    # The two sides must agree that *something* happened.
    assert vbt_ts, "vectorbt scanner produced no entries on the sine panel"
    assert naut_ts, "Nautilus parity strategy produced no entries on the sine panel"
    assert vbt_ts == sorted(vbt_ts)
    assert naut_ts == sorted(naut_ts)

    # Same count modulo a 1-bar tolerance is allowed by the brief.
    assert len(vbt_ts) == len(naut_ts), (
        f"entry-count mismatch: vbt={len(vbt_ts)} naut={len(naut_ts)}"
    )

    matched, unmatched = _match_within_tolerance(vbt_ts, naut_ts, tolerance_bars=1)
    assert unmatched == 0, (
        f"{unmatched} vectorbt entries had no Nautilus counterpart within ±1 bar; "
        f"vbt={vbt_ts}, naut={naut_ts}"
    )
    assert matched == len(vbt_ts)

    # Strict equality: SMA is closed-form, no incremental drift expected.
    assert vbt_ts == naut_ts, f"vbt={vbt_ts}\nnaut={naut_ts}"
