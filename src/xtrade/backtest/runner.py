"""BacktestEngine runner driven by the ParquetDataCatalog.

Phase 1 Task 5 (P4): a single `run_backtest(...)` function that

  1. opens a catalog,
  2. finds the requested instrument + bar_type,
  3. reads bars (optionally within a since/until window),
  4. spins up a `BacktestEngine` for the instrument's venue,
  5. drives a configurable `XtradeStrategy` subclass (default
     `DemoEmaCross`),
  6. writes a `summary.json` under `logs/<run_id>/` with the headline
     accounting numbers and returns the dict.

The runner is intentionally tolerant: zero bars / zero fills are fine
(the caller decides whether that is a business failure). It is also
fully offline — no venue connectivity, no clock, no network.
"""

from __future__ import annotations

import datetime as dt
import json
import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from nautilus_trader.backtest.engine import BacktestEngine, BacktestEngineConfig
from nautilus_trader.config import LoggingConfig
from nautilus_trader.model.enums import AccountType, OmsType
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.objects import Money

from xtrade.data.catalog import (
    ParsedBarSpec,
    bar_type_for,
    open_catalog,
    parse_bar_spec,
    read_bars,
)
from xtrade.strategies.base import XtradeStrategy
from xtrade.strategies.demo_ema import DemoEmaCross, DemoEmaCrossConfig


# Strategy registry --------------------------------------------------------

_STRATEGY_REGISTRY: dict[str, type[XtradeStrategy]] = {
    "demo_ema": DemoEmaCross,
}


def available_strategies() -> list[str]:
    return sorted(_STRATEGY_REGISTRY)


def _strategy_class(name: str) -> type[XtradeStrategy]:
    try:
        return _STRATEGY_REGISTRY[name]
    except KeyError as exc:
        raise ValueError(
            f"unknown strategy {name!r}; available: {available_strategies()}"
        ) from exc


# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BacktestResult:
    run_id: str
    log_dir: Path
    summary_path: Path
    summary: dict[str, Any]


def _resolve_run_id(supplied: str | None) -> str:
    if supplied:
        return supplied
    stamp = dt.datetime.now(tz=dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"backtest-{stamp}"


def _make_log_dir(logs_root: Path, run_id: str) -> Path:
    log_dir = logs_root / run_id
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir


def _ns_to_iso(ns: int | None) -> str | None:
    if ns is None:
        return None
    return dt.datetime.fromtimestamp(ns / 1e9, tz=dt.timezone.utc).isoformat()


def run_backtest(
    *,
    catalog_path: Path | str | None,
    instrument_id: str,
    bar: str,
    strategy: str = "demo_ema",
    trade_size: Decimal = Decimal("0.010"),
    fast_ema_period: int = 10,
    slow_ema_period: int = 20,
    since_ns: int | None = None,
    until_ns: int | None = None,
    starting_balance: int = 1_000_000,
    run_id: str | None = None,
    logs_root: Path | str | None = None,
) -> BacktestResult:
    """Run a backtest end-to-end and return a structured result.

    Parameters
    ----------
    catalog_path : Path | str | None
        Catalog root (defaults to `<repo>/data/catalog`).
    instrument_id : str
        Canonical Nautilus instrument id, e.g. `"BTCUSDT-PERP.BINANCE"`.
    bar : str
        Bar spec (e.g. `"1m"`, `"5m"`, `"1h"`).
    strategy : str
        Strategy registry key (currently only `"demo_ema"`).
    trade_size : Decimal
        Strategy trade size.
    fast_ema_period, slow_ema_period : int
        EMA periods (for demo_ema).
    since_ns, until_ns : int | None
        Optional inclusive bar window in epoch nanoseconds.
    starting_balance : int
        Starting cash in the instrument's settlement currency.
    run_id : str | None
        Override the auto-generated id (else `backtest-YYYYMMDDTHHMMSSZ`).
    logs_root : Path | str | None
        Override `logs/` root (mainly for tests).

    Returns
    -------
    BacktestResult with `summary` dict and on-disk paths.
    """
    spec: ParsedBarSpec = parse_bar_spec(bar)
    instr_id = InstrumentId.from_str(instrument_id)
    venue = instr_id.venue

    catalog = open_catalog(catalog_path)
    instruments = [i for i in catalog.instruments() if i.id == instr_id]
    if not instruments:
        raise FileNotFoundError(
            f"instrument {instrument_id!r} not present in catalog {catalog.path}; "
            "did you run `xtrade data ingest` first?"
        )
    instrument = instruments[0]

    bar_type = bar_type_for(instrument, spec)
    bars = read_bars(catalog, bar_type, start_ns=since_ns, end_ns=until_ns)

    # Resolve log layout.
    repo_root = Path(__file__).resolve().parents[3]
    logs_root_p = Path(logs_root) if logs_root is not None else (repo_root / "logs")
    run_id = _resolve_run_id(run_id)
    log_dir = _make_log_dir(logs_root_p, run_id)

    settlement_ccy = instrument.settlement_currency
    starting = Money(starting_balance, settlement_ccy)

    strategy_cls = _strategy_class(strategy)
    if strategy_cls is DemoEmaCross:
        strategy_cfg = DemoEmaCrossConfig(
            mode="backtest",
            instrument_id=instr_id,
            bar_type=bar_type,
            trade_size=trade_size,
            fast_ema_period=fast_ema_period,
            slow_ema_period=slow_ema_period,
        )
    else:  # pragma: no cover - reserved for future strategies
        raise NotImplementedError(f"strategy {strategy!r} has no config factory yet")

    engine = BacktestEngine(
        config=BacktestEngineConfig(
            trader_id="BACKTESTER-001",
            logging=LoggingConfig(
                log_level="WARN",
                log_level_file="INFO",
                log_directory=str(log_dir),
                log_file_name="run",
            ),
        ),
    )
    engine.add_venue(
        venue=venue,
        oms_type=OmsType.NETTING,
        account_type=AccountType.MARGIN,
        base_currency=None,
        starting_balances=[starting],
    )
    engine.add_instrument(instrument)
    if bars:
        engine.add_data(bars)
    engine.add_strategy(strategy=strategy_cls(config=strategy_cfg))

    t0 = time.monotonic()
    engine.run()
    elapsed_s = time.monotonic() - t0

    account_report = engine.trader.generate_account_report(venue)
    positions_report = engine.trader.generate_positions_report()
    orders_report = engine.trader.generate_order_fills_report()

    orders_filled = int(len(orders_report)) if orders_report is not None else 0
    positions_opened = int(len(positions_report)) if positions_report is not None else 0

    # Headline account numbers — only USDT/USDC balances; we pluck the
    # last row of the report which Nautilus emits per cash event.
    account_tail = (
        account_report.tail(1).to_dict(orient="records")
        if account_report is not None and len(account_report)
        else []
    )

    summary: dict[str, Any] = {
        "run_id": run_id,
        "mode": "backtest",
        "strategy": strategy,
        "instrument_id": str(instr_id),
        "venue": str(venue),
        "bar_type": str(bar_type),
        "bars_loaded": len(bars),
        "first_bar_ts_event": _ns_to_iso(bars[0].ts_event) if bars else None,
        "last_bar_ts_event": _ns_to_iso(bars[-1].ts_event) if bars else None,
        "since_ns": since_ns,
        "until_ns": until_ns,
        "starting_balance": {
            "amount": float(starting_balance),
            "currency": str(settlement_ccy),
        },
        "orders_filled": orders_filled,
        "positions_opened": positions_opened,
        "account_final": account_tail,
        "elapsed_s": round(elapsed_s, 3),
        "config": {
            "strategy": strategy,
            "trade_size": str(trade_size),
            "fast_ema_period": fast_ema_period,
            "slow_ema_period": slow_ema_period,
        },
    }

    summary_path = log_dir / "summary.json"
    summary_path.write_text(json.dumps(summary, indent=2, default=str))

    engine.dispose()
    return BacktestResult(
        run_id=run_id,
        log_dir=log_dir,
        summary_path=summary_path,
        summary=summary,
    )
