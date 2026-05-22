"""Paper-mode runner (Phase 3 Task 5 / T5).

Wires the four Phase 3 chokepoints into one Nautilus `BacktestEngine`
execution:

    SignalConsumer  ─►  SignalDrivenStrategy.on_signal
                              │
                              ▼
                        RiskGate.check
                              │
                              ▼
                        ApprovalGate.decide
                              │
                       go=True │ awaiting / dry_run
                              ▼
                  Nautilus order_factory + submit_order
                              │
                              ▼
                         BacktestEngine fills

The runner registers a thin Nautilus `XtradeStrategy` (`_PaperBridge`)
that listens to bars from the catalog. On each bar it:

  1. updates the internal mark-price for the instrument;
  2. drains any signals with `generated_at <= bar.ts_event`;
  3. invokes the user strategy → list of `OrderIntent`;
  4. runs each intent through `RiskGate` then `ApprovalGate`;
  5. if `go=True`, translates the intent to a Nautilus order and submits.

When the engine completes, the runner extracts fills and writes
`logs/<run-id>/paper_summary.json` with the Task 8 schema (signals
consumed, intents, risk rejects, approval bucket counts, final NAV,
peak drawdown, etc.).

Single-process invariant
------------------------
Nautilus `BacktestEngine.__init__` aborts on second instantiation in
the same Python process; the canonical workaround is a subprocess hop
(see `tests/_paper_runner_subprocess.py`). Anything that needs to run
`run_paper` alongside an existing in-process engine MUST shell out.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

from xtrade.research.signals import Signal, SignalQueue
from xtrade.strategy.base import (
    AccountSnapshot,
    SignalDrivenStrategy,
    load_strategy,
)
from xtrade.strategy.consumer import SignalConsumer
from xtrade.strategy.intent import OrderIntent


# ---------------------------------------------------------------------------
# Result + counters
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class PaperRunResult:
    """Public return value of `run_paper`."""

    run_id: str
    log_dir: Path
    summary_path: Path
    summary: dict[str, Any]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_run_id(supplied: str | None) -> str:
    if supplied:
        return supplied
    stamp = dt.datetime.now(tz=dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"paper-{stamp}"


def _ns_to_iso(ns: int | None) -> str | None:
    if ns is None:
        return None
    return dt.datetime.fromtimestamp(ns / 1e9, tz=dt.timezone.utc).isoformat()


def _ns_of(ts: dt.datetime) -> int:
    return int(ts.astimezone(dt.timezone.utc).timestamp() * 1_000_000_000)


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """Write JSON via tempfile + os.replace (sibling rename)."""
    import os
    import tempfile

    fd, tmp_name = tempfile.mkstemp(
        prefix=f"{path.stem}.", suffix=".json.tmp", dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, default=str, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------


def run_paper(
    *,
    strategy_name: str,
    catalog_path: Path | str | None,
    instrument_id: str,
    bar: str,
    signals_root: Path | str,
    since: dt.datetime | None = None,
    until: dt.datetime | None = None,
    approval_mode: str = "auto",
    risk_rules: list | None = None,
    strategy_config: dict | None = None,
    starting_balance: int = 1_000_000,
    approvals_root: Path | str | None = None,
    run_id: str | None = None,
    logs_root: Path | str | None = None,
) -> PaperRunResult:
    """End-to-end paper-mode run.

    Parameters
    ----------
    strategy_name
        Registry key of a `SignalDrivenStrategy` (e.g. `"momentum_follow"`).
    catalog_path
        ParquetDataCatalog root (defaults to `<repo>/data/catalog`).
    instrument_id
        Canonical Nautilus instrument id (e.g. `"BTCUSDT-PERP.BINANCE"`).
    bar
        Bar spec (`"1m"` etc.).
    signals_root
        Root directory for the `SignalQueue` jsonl shards.
    since, until
        Optional inclusive UTC window for both bars and signals.
    approval_mode
        `"auto"` (default), `"dry_run"`, or `"manual"`.
    risk_rules
        Optional list of `RiskRule` instances; default = empty (RiskGate
        approves everything). Callers usually load this from
        `config/risk.yaml`.
    strategy_config
        Optional dict forwarded to the strategy's `__init__`.
    starting_balance
        Starting cash (in settlement currency units).
    approvals_root
        Override for `data/approvals/` root.
    run_id
        Override the auto-generated id (else `paper-YYYYMMDDTHHMMSSZ`).
    logs_root
        Override `logs/` root.
    """
    # Imports are local so importing this module doesn't drag in Nautilus
    # (which is heavy and not needed for the CLI's `paper run --help`).
    from nautilus_trader.backtest.engine import (  # noqa: PLC0415
        BacktestEngine,
        BacktestEngineConfig,
    )
    from nautilus_trader.config import LoggingConfig  # noqa: PLC0415
    from nautilus_trader.model.enums import AccountType, OmsType  # noqa: PLC0415
    from nautilus_trader.model.identifiers import InstrumentId  # noqa: PLC0415
    from nautilus_trader.model.objects import Money  # noqa: PLC0415

    from xtrade.approval import ApprovalGate  # noqa: PLC0415
    from xtrade.data.catalog import (  # noqa: PLC0415
        bar_type_for,
        open_catalog,
        parse_bar_spec,
        read_bars,
    )
    from xtrade.risk import RiskGate  # noqa: PLC0415
    # Importing plugins registers them with the strategy registry.
    import xtrade.strategy.plugins  # noqa: F401, PLC0415

    # --- 1. Resolve catalog + instrument + bars
    spec = parse_bar_spec(bar)
    instr_id = InstrumentId.from_str(instrument_id)
    venue = instr_id.venue
    catalog = open_catalog(catalog_path)
    instruments = [i for i in catalog.instruments() if i.id == instr_id]
    if not instruments:
        raise FileNotFoundError(
            f"instrument {instrument_id!r} not present in catalog "
            f"{catalog.path}; did you run `xtrade data ingest` first?"
        )
    instrument = instruments[0]
    bar_type = bar_type_for(instrument, spec)
    since_ns = _ns_of(since) if since is not None else None
    until_ns = _ns_of(until) if until is not None else None
    bars = read_bars(catalog, bar_type, start_ns=since_ns, end_ns=until_ns)

    # --- 2. Resolve logs + run id
    repo_root = Path(__file__).resolve().parents[3]
    logs_root_p = Path(logs_root) if logs_root is not None else (repo_root / "logs")
    run_id = _resolve_run_id(run_id)
    log_dir = logs_root_p / run_id
    log_dir.mkdir(parents=True, exist_ok=True)

    # --- 3. Pull signals
    queue = SignalQueue(signals_root)
    consumer = SignalConsumer(queue, symbol=instrument_id)
    raw_signals = consumer.list_all()
    if since is not None:
        raw_signals = [s for s in raw_signals if s.generated_at >= since]
    if until is not None:
        raw_signals = [s for s in raw_signals if s.generated_at <= until]
    raw_signals.sort(key=lambda s: s.generated_at)

    # --- 4. Strategy + gates
    user_strategy = load_strategy(strategy_name, config=strategy_config)
    risk_gate = RiskGate(rules=tuple(risk_rules or ()))
    if approvals_root is None:
        approvals_root = repo_root / "data" / "approvals"
    approval_gate = ApprovalGate(approval_mode, approvals_root)  # type: ignore[arg-type]

    # --- 5. Build engine
    starting = Money(starting_balance, instrument.settlement_currency)
    engine = BacktestEngine(
        config=BacktestEngineConfig(
            trader_id="PAPER-001",
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

    bridge = _build_bridge_strategy(
        instrument=instrument,
        bar_type=bar_type,
        signals=raw_signals,
        user_strategy=user_strategy,
        risk_gate=risk_gate,
        approval_gate=approval_gate,
        starting_cash=Decimal(starting_balance),
    )
    engine.add_strategy(strategy=bridge)

    # --- 6. Run
    started_at = dt.datetime.now(tz=dt.timezone.utc)
    t0 = time.monotonic()
    engine.run()
    elapsed_s = time.monotonic() - t0

    # --- 7. Pull reports & build summary
    orders_report = engine.trader.generate_order_fills_report()
    orders_filled = int(len(orders_report)) if orders_report is not None else 0

    summary: dict[str, Any] = {
        "run_id": run_id,
        "started_at": started_at.isoformat(),
        "completed_at": dt.datetime.now(tz=dt.timezone.utc).isoformat(),
        "mode": "paper",
        "strategy": strategy_name,
        "approval_mode": approval_mode,
        "instrument_id": str(instr_id),
        "venue": str(venue),
        "bar_type": str(bar_type),
        "bars_loaded": len(bars),
        "first_bar_ts_event": _ns_to_iso(bars[0].ts_event) if bars else None,
        "last_bar_ts_event": _ns_to_iso(bars[-1].ts_event) if bars else None,
        "signals_consumed": bridge.signals_consumed,
        "intents_generated": bridge.intents_generated,
        "risk_rejected": bridge.risk_rejected,
        "approvals_pending": bridge.approvals_pending,
        "approvals_confirmed": bridge.approvals_confirmed,
        "approvals_rejected": bridge.approvals_rejected,
        "approvals_dry_run": bridge.approvals_dry_run,
        "fills": orders_filled,
        "final_cash_usd": str(bridge.cash_usd),
        "final_position_qty": str(bridge.positions.get(str(instr_id), Decimal(0))),
        "final_nav_usd": str(bridge.nav_usd),
        "peak_nav_usd": str(bridge.peak_nav_usd),
        "max_drawdown_pct": float(bridge.max_drawdown_pct),
        "elapsed_s": round(elapsed_s, 3),
        "errors": list(bridge.errors),
        "config": {
            "strategy_config": dict(strategy_config or {}),
            "starting_balance": starting_balance,
            "risk_rules": [type(r).__name__ for r in (risk_rules or ())],
        },
    }
    summary_path = log_dir / "paper_summary.json"
    _atomic_write_json(summary_path, summary)

    engine.dispose()
    return PaperRunResult(
        run_id=run_id,
        log_dir=log_dir,
        summary_path=summary_path,
        summary=summary,
    )


# ---------------------------------------------------------------------------
# Bridge strategy (built lazily so importing this module is Nautilus-free
# when the runner is not actually called)
# ---------------------------------------------------------------------------


def _build_bridge_strategy(
    *,
    instrument,
    bar_type,
    signals: list[Signal],
    user_strategy: SignalDrivenStrategy,
    risk_gate,
    approval_gate,
    starting_cash: Decimal,
):
    """Return a configured `_PaperBridge` Nautilus strategy instance."""
    from nautilus_trader.model.data import Bar  # noqa: PLC0415
    from nautilus_trader.model.enums import OrderSide, TimeInForce  # noqa: PLC0415
    from nautilus_trader.model.identifiers import InstrumentId  # noqa: PLC0415

    from xtrade.strategies.base import (  # noqa: PLC0415
        XtradeStrategy,
        XtradeStrategyConfig,
    )

    _BAR_TYPE = bar_type
    _INSTR_ID = instrument.id
    _SYMBOL_KEY = str(_INSTR_ID)

    class _PaperBridgeConfig(XtradeStrategyConfig, frozen=True, kw_only=True):
        instrument_id: InstrumentId
        # We can't carry runtime objects through msgspec config; the
        # bridge instance picks them up via attribute injection below.

    class _PaperBridge(XtradeStrategy):
        def __init__(self, config: _PaperBridgeConfig) -> None:
            super().__init__(config)
            self.instrument = None
            self._remaining: list[Signal] = list(signals)
            # Counters (Task 8 schema)
            self.signals_consumed = 0
            self.intents_generated = 0
            self.risk_rejected = 0
            self.approvals_pending = 0
            self.approvals_confirmed = 0
            self.approvals_rejected = 0
            self.approvals_dry_run = 0
            self.errors: list[str] = []
            # Internal paper-mode book-keeping (Decimal end-to-end)
            self.cash_usd: Decimal = starting_cash
            self.positions: dict[str, Decimal] = {_SYMBOL_KEY: Decimal(0)}
            self.marks: dict[str, Decimal] = {}
            self.nav_usd: Decimal = starting_cash
            self.peak_nav_usd: Decimal = starting_cash
            self.max_drawdown_pct: Decimal = Decimal(0)

        # ---- lifecycle -------------------------------------------------

        def on_start_common(self) -> None:
            self.instrument = self.cache.instrument(_INSTR_ID)
            if self.instrument is None:
                self.log.error(f"instrument {_INSTR_ID} missing from cache")
                self.stop()
                return
            self.subscribe_bars(_BAR_TYPE)

        # ---- driven by bars -------------------------------------------

        def on_bar(self, bar: Bar) -> None:
            # Update mark from this bar's close.
            close_str = str(bar.close)
            try:
                mark = Decimal(close_str)
            except Exception:  # pragma: no cover - defensive
                return
            self.marks[_SYMBOL_KEY] = mark
            self._recompute_nav()

            # Drain due signals.
            while self._remaining and _ns_of(self._remaining[0].generated_at) <= bar.ts_event:
                sig = self._remaining.pop(0)
                self._process_signal(sig)

        def _process_signal(self, signal: Signal) -> None:
            self.signals_consumed += 1
            account = self._snapshot()
            try:
                intents = list(user_strategy.on_signal(signal, account))
            except Exception as exc:  # pragma: no cover
                self.errors.append(f"strategy.on_signal raised: {exc!r}")
                return
            for intent in intents:
                self.intents_generated += 1
                rd = risk_gate.check(intent, account)
                if not rd.approve:
                    self.risk_rejected += 1
                    user_strategy.on_reject(intent, "; ".join(rd.reasons))
                    continue
                decision = approval_gate.decide(intent, now=signal.generated_at)
                if decision.awaiting:
                    self.approvals_pending += 1
                    continue
                if decision.mode == "dry_run":
                    self.approvals_dry_run += 1
                    continue
                if not decision.go:
                    # rejected externally between submit and decide
                    self.approvals_rejected += 1
                    continue
                self.approvals_confirmed += 1
                self._submit(intent)

        # ---- intent → Nautilus order ----------------------------------

        def _submit(self, intent: OrderIntent) -> None:
            assert self.instrument is not None
            side = OrderSide.BUY if intent.side == "BUY" else OrderSide.SELL
            qty = self.instrument.make_qty(intent.quantity)
            if intent.order_type == "MARKET":
                order = self.order_factory.market(
                    instrument_id=_INSTR_ID,
                    order_side=side,
                    quantity=qty,
                    time_in_force=TimeInForce.IOC,
                )
            else:
                assert intent.limit_price is not None
                price = self.instrument.make_price(intent.limit_price)
                order = self.order_factory.limit(
                    instrument_id=_INSTR_ID,
                    order_side=side,
                    quantity=qty,
                    price=price,
                    time_in_force=TimeInForce.GTC,
                )
            self.submit_order(order)

        # ---- fill callback updates internal book ----------------------

        def on_order_filled(self, event) -> None:  # noqa: ANN001
            try:
                qty = Decimal(str(event.last_qty))
                price = Decimal(str(event.last_px))
            except Exception:  # pragma: no cover
                return
            sym = _SYMBOL_KEY
            sign = Decimal(1) if str(event.order_side) == "BUY" else Decimal(-1)
            self.positions[sym] = self.positions.get(sym, Decimal(0)) + sign * qty
            self.cash_usd -= sign * qty * price
            self.marks[sym] = price  # latest exec is a good mark too
            self._recompute_nav()

        # ---- snapshot + nav -------------------------------------------

        def _snapshot(self) -> AccountSnapshot:
            return AccountSnapshot(
                cash_usd=self.cash_usd,
                positions=dict(self.positions),
                mark_prices=dict(self.marks),
                nav_usd=self.nav_usd,
                peak_nav_usd=self.peak_nav_usd,
            )

        def _recompute_nav(self) -> None:
            exposure = Decimal(0)
            for sym, qty in self.positions.items():
                if qty == 0:
                    continue
                mk = self.marks.get(sym)
                if mk is None:
                    continue
                exposure += qty * mk
            self.nav_usd = self.cash_usd + exposure
            if self.nav_usd > self.peak_nav_usd:
                self.peak_nav_usd = self.nav_usd
            if self.peak_nav_usd > 0:
                dd = (self.peak_nav_usd - self.nav_usd) / self.peak_nav_usd
                if dd > self.max_drawdown_pct:
                    self.max_drawdown_pct = dd

    cfg = _PaperBridgeConfig(mode="backtest", instrument_id=instrument.id)
    return _PaperBridge(config=cfg)
