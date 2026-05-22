"""xtrade command-line interface.

Typer-based entry point exposing three subcommand groups (`data`,
`backtest`, `live`).

Exit code contract (Phase 1 Task 7 / P7):
  0  business success
  1  business failure (e.g. order rejected, no quote within timeout)
  2  configuration / precondition failure (missing env, bad config,
     unknown strategy / venue, etc.)

Every non-trivial command runs inside a `run_with_logging(...)` context
(see `xtrade.observability`) so all runs share the same on-disk layout
under `logs/<run-id>/`:

  - `run.log`             — Nautilus output (kernel-written)
  - `summary.json`        — runner-written headline result
  - `config.snapshot.yaml` — verbatim copy of the venues yaml that
                             drove the run (env-var names only; safe)
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import typer


app = typer.Typer(
    name="xtrade",
    help="xtrade — multi-venue automated trading research CLI.",
    no_args_is_help=True,
    add_completion=False,
)

data_app = typer.Typer(help="Historical data ingest and catalog inspection.")
backtest_app = typer.Typer(help="Run backtests against the local ParquetDataCatalog.")
live_app = typer.Typer(help="Run testnet TradingNode probes and live strategies.")
scan_app = typer.Typer(help="Phase 2 opportunity discovery: scanners over the catalog.")
strategy_app = typer.Typer(help="Phase 3 strategy plugin registry: list and describe.")
paper_app = typer.Typer(help="Phase 3 paper-mode runs: signals + RiskGate + ApprovalGate + BacktestEngine.")
approve_app = typer.Typer(help="Phase 3 approval queue: list / confirm / reject pending intents.")

app.add_typer(data_app, name="data")
app.add_typer(backtest_app, name="backtest")
app.add_typer(live_app, name="live")
app.add_typer(scan_app, name="scan")
app.add_typer(strategy_app, name="strategy")
app.add_typer(paper_app, name="paper")
app.add_typer(approve_app, name="approve")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_iso_to_ms(text: str, *, end_of_day: bool = False) -> int:
    """Parse an ISO-8601 date / datetime to epoch ms (UTC).

    Bare dates (`YYYY-MM-DD`) snap to 00:00:00 UTC; `end_of_day=True`
    bumps the result to the next-day boundary (exclusive end), matching
    Binance/HL's `endTime` semantics.
    """
    s = text.strip()
    try:
        if "T" in s or " " in s.replace("T", ""):
            ts = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        else:
            ts = dt.datetime.fromisoformat(s).replace(tzinfo=dt.timezone.utc)
    except ValueError as exc:
        raise typer.BadParameter(f"Invalid ISO datetime: {text!r}") from exc
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=dt.timezone.utc)
    if end_of_day and "T" not in s and " " not in s:
        ts = ts + dt.timedelta(days=1)
    return int(ts.timestamp() * 1000)


def _exit_config_error(message: str) -> "typer.Exit":
    """Print a config error to stderr and return a typer.Exit(2)."""
    typer.echo(f"error: {message}", err=True)
    return typer.Exit(code=2)


def _not_yet_implemented(*, task: str, module: str) -> None:
    """Exit with code 2 and a clear pointer to the task that fills this in."""
    msg = (
        f"This command is not yet implemented.\n"
        f"  Responsible task: {task}\n"
        f"  Module(s):        {module}\n"
        f"See docs/phase1_brief.md §5 for the full task list."
    )
    typer.echo(msg, err=True)
    raise typer.Exit(code=2)


# ---------------------------------------------------------------------------
# `xtrade data ...`
# ---------------------------------------------------------------------------


@data_app.command("ingest")
def data_ingest(
    venue: str = typer.Option(..., "--venue", help="binance | hyperliquid"),
    symbol: str = typer.Option(..., "--symbol", help="e.g. BTCUSDT or xyz:TSLA"),
    bar: str = typer.Option("1m", "--bar", help="Bar spec, e.g. 1m, 5m, 1h"),
    since: str = typer.Option(..., "--since", help="ISO-8601 start, e.g. 2026-05-01"),
    until: str | None = typer.Option(None, "--until", help="ISO-8601 end (default: now UTC)"),
    catalog_path: Path | None = typer.Option(
        None, "--catalog", help="Catalog root (default: <repo>/data/catalog)"
    ),
    dex: str | None = typer.Option(
        None,
        "--dex",
        help="Hyperliquid HIP-3 dex name (default: parse from `dex:SYMBOL`)",
    ),
) -> None:
    """Fetch historical bars and append them to the local catalog (idempotent)."""
    from xtrade.data import binance_klines, hyperliquid_hip3
    from xtrade.data.catalog import (
        bar_type_for,
        intervals_for,
        missing_intervals,
        open_catalog,
        parse_bar_spec,
        write_bars,
    )
    from xtrade.data.instruments import InstrumentResolutionError, resolve

    venue_l = venue.lower()
    if venue_l not in ("binance", "hyperliquid"):
        raise _exit_config_error(f"--venue must be 'binance' or 'hyperliquid', got {venue!r}")

    try:
        spec = parse_bar_spec(bar)
    except ValueError as exc:
        raise _exit_config_error(str(exc)) from exc

    start_ms = _parse_iso_to_ms(since)
    if until is None:
        end_ms = int(
            dt.datetime.now(tz=dt.timezone.utc).replace(second=0, microsecond=0).timestamp() * 1000
        )
    else:
        end_ms = _parse_iso_to_ms(until, end_of_day=True)
    if end_ms <= start_ms:
        raise _exit_config_error(f"--until ({until}) must be after --since ({since})")

    try:
        if venue_l == "hyperliquid":
            instrument = resolve(venue_l, symbol, dex=dex, mainnet=True)
        else:
            instrument = resolve(venue_l, symbol)
    except InstrumentResolutionError as exc:
        raise _exit_config_error(str(exc)) from exc

    bar_type = bar_type_for(instrument, spec)
    catalog = open_catalog(catalog_path)
    start_ns = start_ms * 1_000_000
    end_ns = end_ms * 1_000_000

    missing = missing_intervals(catalog, bar_type, start_ns, end_ns)
    typer.echo(
        f"ingest target: {bar_type} from "
        f"{dt.datetime.fromtimestamp(start_ms / 1000, tz=dt.timezone.utc).isoformat()} to "
        f"{dt.datetime.fromtimestamp(end_ms / 1000, tz=dt.timezone.utc).isoformat()}"
    )
    if not missing:
        typer.echo("catalog already covers the requested range; nothing to do.")
        existing = intervals_for(catalog, bar_type)
        typer.echo(f"existing intervals: {existing}")
        return

    typer.echo(f"missing intervals: {len(missing)}")
    total = 0
    interval = spec.binance_interval() if venue_l == "binance" else spec.hyperliquid_interval()
    for start_ns_chunk, end_ns_chunk in missing:
        if venue_l == "binance":
            bars = binance_klines.fetch_bars(
                symbol=symbol,
                interval=interval,
                start_ms=start_ns_chunk // 1_000_000,
                end_ms=end_ns_chunk // 1_000_000,
                instrument=instrument,
                bar_type=bar_type,
            )
        else:
            # Hyperliquid: ensure we have a `dex` to query and a bare ticker.
            if ":" in symbol:
                dex_arg, ticker = symbol.split(":", 1)
            else:
                if dex is None:
                    raise _exit_config_error(
                        "Hyperliquid HIP-3 ingest requires either `--dex` or "
                        "a `dex:TICKER` --symbol."
                    )
                dex_arg, ticker = dex, symbol
            bars = hyperliquid_hip3.fetch_bars(
                dex=dex_arg,
                symbol=ticker,
                interval=interval,
                start_ms=start_ns_chunk // 1_000_000,
                end_ms=end_ns_chunk // 1_000_000,
                instrument=instrument,
                bar_type=bar_type,
                mainnet=True,
            )
        wrote = write_bars(catalog, instrument, bars)
        total += wrote
        typer.echo(
            f"  chunk {start_ns_chunk}..{end_ns_chunk}: "
            f"{wrote} bars written"
        )

    typer.echo(f"done. total bars written this run: {total}")
    typer.echo(f"intervals now: {intervals_for(catalog, bar_type)}")


@data_app.command("inspect")
def data_inspect(
    catalog_path: Path | None = typer.Option(
        None, "--catalog", help="Catalog root (default: <repo>/data/catalog)"
    ),
) -> None:
    """List instruments and bar ranges currently held in the catalog."""
    from nautilus_trader.model.data import Bar

    from xtrade.data.catalog import open_catalog

    catalog = open_catalog(catalog_path)
    instruments = catalog.instruments()
    typer.echo(f"catalog root: {catalog.path}")
    typer.echo(f"instruments: {len(instruments)}")
    for inst in instruments:
        typer.echo(f"  - {inst.id}  "
                   f"(price_precision={inst.price_precision}, "
                   f"size_precision={inst.size_precision})")

    # List bar identifiers via Nautilus's directory listing.
    bar_ids = catalog.list_data_types()  # returns list[str]
    typer.echo(f"data types: {len(bar_ids)}")
    for dtype in bar_ids:
        typer.echo(f"  - {dtype}")

    typer.echo("\nbar ranges:")
    for inst in instruments:
        # We don't know all bar_types for an instrument from Bar storage
        # alone; rely on catalog.get_intervals' identifier API and try
        # the canonical EXTERNAL-aggregation form for common bar specs.
        for spec in ("1-MINUTE-LAST-EXTERNAL", "5-MINUTE-LAST-EXTERNAL",
                     "1-HOUR-LAST-EXTERNAL", "1-DAY-LAST-EXTERNAL"):
            ident = f"{inst.id}-{spec}"
            intervals = catalog.get_intervals(Bar, ident)
            if intervals:
                start_ns, end_ns = intervals[0][0], intervals[-1][1]
                typer.echo(
                    f"  {ident}: "
                    f"{dt.datetime.fromtimestamp(start_ns / 1e9, tz=dt.timezone.utc).isoformat()} "
                    f".. "
                    f"{dt.datetime.fromtimestamp(end_ns / 1e9, tz=dt.timezone.utc).isoformat()} "
                    f"({len(intervals)} segments)"
                )


# ---------------------------------------------------------------------------
# `xtrade backtest ...`
# ---------------------------------------------------------------------------


@backtest_app.command("run")
def backtest_run(
    strategy: str = typer.Option("demo_ema", "--strategy", help="Strategy registry key (e.g. demo_ema)."),
    instrument: str = typer.Option(..., "--instrument", help="e.g. BTCUSDT-PERP.BINANCE"),
    bar: str = typer.Option("1m", "--bar", help="Bar spec, e.g. 1m, 5m, 1h"),
    since: str | None = typer.Option(None, "--since", help="ISO-8601 lower bound (inclusive)."),
    until: str | None = typer.Option(None, "--until", help="ISO-8601 upper bound (inclusive)."),
    trade_size: str = typer.Option("0.010", "--trade-size", help="Trade size in instrument units."),
    fast_ema_period: int = typer.Option(10, "--fast-ema", help="Fast EMA period."),
    slow_ema_period: int = typer.Option(20, "--slow-ema", help="Slow EMA period."),
    starting_balance: int = typer.Option(1_000_000, "--starting-balance", help="Starting cash in settlement ccy."),
    catalog_path: Path | None = typer.Option(
        None, "--catalog", help="Catalog root (default: <repo>/data/catalog)"
    ),
    run_id: str | None = typer.Option(None, "--run-id", help="Override the auto-generated run id."),
) -> None:
    """Run a strategy against catalog bars and write a summary."""
    from decimal import Decimal, InvalidOperation

    from xtrade.backtest.runner import available_strategies, run_backtest
    from xtrade.observability import run_with_logging

    if strategy not in available_strategies():
        raise _exit_config_error(
            f"--strategy must be one of {available_strategies()}, got {strategy!r}"
        )

    try:
        ts = Decimal(trade_size)
    except (InvalidOperation, ValueError) as exc:
        raise _exit_config_error(f"--trade-size must be a decimal, got {trade_size!r}") from exc

    if fast_ema_period >= slow_ema_period:
        raise _exit_config_error(
            f"--fast-ema ({fast_ema_period}) must be < --slow-ema ({slow_ema_period})"
        )

    since_ns = _parse_iso_to_ms(since) * 1_000_000 if since else None
    until_ns = _parse_iso_to_ms(until, end_of_day=True) * 1_000_000 if until else None
    if since_ns is not None and until_ns is not None and until_ns <= since_ns:
        raise _exit_config_error(f"--until ({until}) must be after --since ({since})")

    try:
        with run_with_logging(mode="backtest", run_id=run_id) as ctx:
            result = run_backtest(
                catalog_path=catalog_path,
                instrument_id=instrument,
                bar=bar,
                strategy=strategy,
                trade_size=ts,
                fast_ema_period=fast_ema_period,
                slow_ema_period=slow_ema_period,
                since_ns=since_ns,
                until_ns=until_ns,
                starting_balance=starting_balance,
                run_id=ctx.run_id,
                logs_root=ctx.logs_root,
            )
    except FileNotFoundError as exc:
        raise _exit_config_error(str(exc)) from exc
    except ValueError as exc:
        raise _exit_config_error(str(exc)) from exc

    s = result.summary
    typer.echo(f"run_id:           {s['run_id']}")
    typer.echo(f"instrument:       {s['instrument_id']}")
    typer.echo(f"bar_type:         {s['bar_type']}")
    typer.echo(f"bars loaded:      {s['bars_loaded']}")
    if s["bars_loaded"]:
        typer.echo(f"window:           {s['first_bar_ts_event']} .. {s['last_bar_ts_event']}")
    typer.echo(f"orders filled:    {s['orders_filled']}")
    typer.echo(f"positions opened: {s['positions_opened']}")
    typer.echo(f"summary:          {result.summary_path}")


# ---------------------------------------------------------------------------
# `xtrade live ...`
# ---------------------------------------------------------------------------


# Default instrument per venue key for `xtrade live health`. Matches the
# Phase 0 connectivity scripts (02b / 03) so we know these are reachable
# on the corresponding testnets.
_DEFAULT_HEALTH_INSTRUMENTS: dict[str, str] = {
    "binance_spot": "BTCUSDT.BINANCE",
    "binance_futures": "BTCUSDT-PERP.BINANCE",
    "hyperliquid": "BTC-USD-PERP.HYPERLIQUID",
}


@live_app.command("health")
def live_health(
    venues: str = typer.Option(
        "binance_spot,binance_futures,hyperliquid",
        "--venues",
        help="Comma-separated venue keys from venues yaml.",
    ),
    instruments: str | None = typer.Option(
        None,
        "--instrument",
        help=(
            "Comma-separated instrument ids to probe instead of the per-venue "
            "defaults (e.g. ETHUSDT.BINANCE,BTC-USD-PERP.HYPERLIQUID)."
        ),
    ),
    timeout: int = typer.Option(60, "--timeout", help="Seconds to wait for first quote per channel."),
    venues_yaml: Path = typer.Option(
        Path("config/venues.testnet.yaml"),
        "--venues-yaml",
        help="Path to the venues yaml (default: config/venues.testnet.yaml).",
    ),
    run_id: str | None = typer.Option(None, "--run-id", help="Override the auto-generated run id."),
) -> None:
    """Start a testnet node, subscribe to one instrument per venue, await first quote."""
    from nautilus_trader.model.identifiers import InstrumentId

    from xtrade.config import ConfigError, MissingCredentialError, load_venues
    from xtrade.node.factory import MainnetRefusedError
    from xtrade.node.health import probe
    from xtrade.observability import run_with_logging

    venue_keys = [v.strip() for v in venues.split(",") if v.strip()]
    if not venue_keys:
        raise _exit_config_error("--venues must not be empty.")
    unknown = [v for v in venue_keys if v not in _DEFAULT_HEALTH_INSTRUMENTS]
    if unknown:
        raise _exit_config_error(
            f"unknown venue keys: {unknown}. "
            f"Valid: {sorted(_DEFAULT_HEALTH_INSTRUMENTS)}."
        )

    if instruments:
        iid_strs = [s.strip() for s in instruments.split(",") if s.strip()]
    else:
        iid_strs = [_DEFAULT_HEALTH_INSTRUMENTS[v] for v in venue_keys]

    try:
        iids = [InstrumentId.from_str(s) for s in iid_strs]
    except Exception as exc:  # noqa: BLE001
        raise _exit_config_error(f"failed to parse instrument id: {exc}") from exc

    try:
        venues_cfg = load_venues(venues_yaml)
    except (ConfigError, MissingCredentialError) as exc:
        raise _exit_config_error(str(exc)) from exc

    try:
        with run_with_logging(
            mode="health", run_id=run_id, venues_cfg=venues_cfg
        ) as ctx:
            result = probe(
                venues_cfg,
                instruments=iids,
                timeout_s=float(timeout),
                run_id=ctx.run_id,
                logs_root=ctx.logs_root,
            )
    except MainnetRefusedError as exc:
        raise _exit_config_error(str(exc)) from exc

    typer.echo(f"run_id:       {result.run_id}")
    typer.echo(f"summary:      {result.summary_path}")
    for iid_str, entry in result.summary["per_instrument"].items():
        if entry["first_quote_iso"] is None:
            typer.echo(f"  {iid_str}: NO QUOTE within {timeout}s")
        else:
            typer.echo(
                f"  {iid_str}: first_quote={entry['first_quote_iso']} "
                f"(+{entry['first_quote_latency_ms']} ms)"
            )

    if not result.passed:
        typer.echo("health check FAILED (one or more channels saw no quote).", err=True)
        raise typer.Exit(code=1)
    typer.echo("health check PASSED.")


@live_app.command("run")
def live_run(
    instrument: str = typer.Option(
        ..., "--instrument", help="Instrument id (e.g. BTCUSDT.BINANCE)."
    ),
    strategy: str = typer.Option(
        "live_order_probe",
        "--strategy",
        help="Live strategy registry key (currently: live_order_probe).",
    ),
    side: str = typer.Option("BUY", "--side", help="BUY | SELL."),
    quantity: str = typer.Option(
        "0.001", "--quantity", help="Order size in instrument units."
    ),
    safety_multiplier: str = typer.Option(
        "0.7",
        "--safety-multiplier",
        help=(
            "BUY price = multiplier × bid; SELL price = ask / multiplier. "
            "Default 0.7 matches Phase 0 C2-spot."
        ),
    ),
    timeout: int = typer.Option(
        60, "--timeout", help="Probe timeout (seconds)."
    ),
    venues_yaml: Path = typer.Option(
        Path("config/venues.testnet.yaml"),
        "--venues-yaml",
        help="Path to the venues yaml (default: config/venues.testnet.yaml).",
    ),
    run_id: str | None = typer.Option(None, "--run-id", help="Override the auto run id."),
) -> None:
    """Run a strategy live against testnets (places one far-from-market limit
    order, awaits accept + cancel)."""
    from decimal import Decimal, InvalidOperation

    from xtrade.config import ConfigError, MissingCredentialError, load_venues
    from xtrade.live.runner import available_live_strategies, run_live
    from xtrade.node.factory import MainnetRefusedError
    from xtrade.observability import run_with_logging

    if strategy not in available_live_strategies():
        raise _exit_config_error(
            f"--strategy must be one of {available_live_strategies()}, "
            f"got {strategy!r}"
        )
    if side.upper() not in ("BUY", "SELL"):
        raise _exit_config_error(f"--side must be BUY or SELL, got {side!r}")
    try:
        qty = Decimal(quantity)
        mult = Decimal(safety_multiplier)
    except (InvalidOperation, ValueError) as exc:
        raise _exit_config_error(f"--quantity/--safety-multiplier must be decimals: {exc}") from exc
    if mult <= 0:
        raise _exit_config_error("--safety-multiplier must be > 0")

    try:
        venues_cfg = load_venues(venues_yaml)
    except (ConfigError, MissingCredentialError) as exc:
        raise _exit_config_error(str(exc)) from exc

    try:
        with run_with_logging(
            mode="live", run_id=run_id, venues_cfg=venues_cfg
        ) as ctx:
            result = run_live(
                venues_cfg,
                instrument_id=instrument,
                strategy=strategy,
                quantity=qty,
                side=side.upper(),
                safety_multiplier=mult,
                timeout_s=float(timeout),
                run_id=ctx.run_id,
                logs_root=ctx.logs_root,
            )
    except MainnetRefusedError as exc:
        raise _exit_config_error(str(exc)) from exc

    s = result.summary
    typer.echo(f"run_id:       {s['run_id']}")
    typer.echo(f"summary:      {result.summary_path}")
    typer.echo(f"instrument:   {s['instrument_id']}")
    typer.echo(f"first quote:  {s['first_quote_iso']}")
    order = s["order"]
    typer.echo(
        f"order:        accepted={order['accepted']} "
        f"canceled={order['canceled']} rejected={order['rejected']}"
    )
    if order["rejection_reason"]:
        typer.echo(f"  reason:     {order['rejection_reason']}")
    if s["account_snapshot"]:
        typer.echo("account_snapshot:")
        for row in s["account_snapshot"]:
            typer.echo(
                f"  {row['currency']}: total={row['total']} "
                f"locked={row['locked']} free={row['free']}"
            )

    if not result.passed:
        typer.echo("live run FAILED (order lifecycle incomplete).", err=True)
        raise typer.Exit(code=1)
    typer.echo("live run PASSED.")


@live_app.command("signal-run")
def live_signal_run(
    strategy: str = typer.Option(
        ..., "--strategy", help="SignalDrivenStrategy registry key."
    ),
    instrument: str = typer.Option(
        ..., "--instrument", help="Instrument id (e.g. BTCUSDT.BINANCE)."
    ),
    signals_from: Path = typer.Option(
        ..., "--signals-from", help="SignalQueue root directory."
    ),
    mode: str = typer.Option(
        "manual",
        "--mode",
        help="auto / dry_run / manual (default manual for testnet hop).",
    ),
    signal_id: str | None = typer.Option(
        None,
        "--signal-id",
        help="Composite '<generated_at>|<symbol>|<source>'; default newest.",
    ),
    venues_yaml: Path = typer.Option(
        Path("config/venues.testnet.yaml"),
        "--venues-yaml",
        help="Path to the venues yaml (default: config/venues.testnet.yaml).",
    ),
    safety_multiplier: str = typer.Option(
        "0.7",
        "--safety-multiplier",
        help="Far-from-market multiplier for the testnet limit order.",
    ),
    approval_timeout: int = typer.Option(
        600,
        "--approval-timeout",
        help="Max wall-clock seconds to wait for manual approval.",
    ),
    poll_interval: float = typer.Option(
        2.0, "--poll-interval", help="Seconds between approval-queue polls."
    ),
    venue_timeout: int = typer.Option(
        60, "--venue-timeout", help="Per-probe testnet timeout (seconds)."
    ),
    risk_config: Path | None = typer.Option(
        None, "--risk-config", help="Path to risk.yaml."
    ),
    approvals_root: Path | None = typer.Option(
        None, "--approvals-root", help="Approvals queue root."
    ),
    run_id: str | None = typer.Option(
        None, "--run-id", help="Override the auto run id."
    ),
) -> None:
    """Drive one signal → RiskGate → ApprovalGate → testnet limit-and-cancel.

    This is the Phase 3 Task 6 testnet runbook entry point. Manual mode
    parks the intent in the approval queue and polls until an operator
    runs `xtrade approve confirm <id>` (or the timeout expires).
    """
    from decimal import Decimal, InvalidOperation

    from xtrade.config import ConfigError, MissingCredentialError, load_venues
    from xtrade.live.signal_runner import (
        ApprovalRejectedError,
        ApprovalTimeoutError,
        LiveSignalError,
        NoMatchingSignalError,
        RiskRejectedError,
        StrategyEmittedNothingError,
        run_live_signal,
    )
    from xtrade.node.factory import MainnetRefusedError
    from xtrade.observability import run_with_logging
    from xtrade.risk import load_rules_from_yaml

    if mode not in {"auto", "dry_run", "manual"}:
        raise _exit_config_error(
            f"--mode must be auto/dry_run/manual, got {mode!r}"
        )
    try:
        mult = Decimal(safety_multiplier)
    except (InvalidOperation, ValueError) as exc:
        raise _exit_config_error(f"--safety-multiplier must be decimal: {exc}") from exc
    if mult <= 0:
        raise _exit_config_error("--safety-multiplier must be > 0")

    rules = []
    if risk_config is not None:
        try:
            rules = load_rules_from_yaml(risk_config)
        except (FileNotFoundError, ValueError) as exc:
            raise _exit_config_error(str(exc)) from exc

    try:
        venues_cfg = load_venues(venues_yaml)
    except (ConfigError, MissingCredentialError) as exc:
        raise _exit_config_error(str(exc)) from exc

    try:
        with run_with_logging(
            mode="live", run_id=run_id, venues_cfg=venues_cfg
        ) as ctx:
            result = run_live_signal(
                venues_cfg,
                strategy_name=strategy,
                signals_root=signals_from,
                instrument_id=instrument,
                approval_mode=mode,
                signal_id=signal_id,
                risk_rules=rules,
                safety_multiplier=mult,
                approval_timeout_s=float(approval_timeout),
                poll_interval_s=float(poll_interval),
                venue_timeout_s=float(venue_timeout),
                approvals_root=approvals_root,
                run_id=ctx.run_id,
                logs_root=ctx.logs_root,
            )
    except MainnetRefusedError as exc:
        raise _exit_config_error(str(exc)) from exc
    except (NoMatchingSignalError, StrategyEmittedNothingError) as exc:
        raise _exit_config_error(str(exc)) from exc
    except (RiskRejectedError, ApprovalRejectedError, ApprovalTimeoutError) as exc:
        typer.echo(f"live signal-run FAILED: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    except LiveSignalError as exc:
        typer.echo(f"live signal-run FAILED: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    s = result.summary
    typer.echo(f"run_id:        {s['run_id']}")
    typer.echo(f"strategy:      {s['strategy']}")
    typer.echo(f"instrument:    {s['instrument_id']}")
    typer.echo(f"approval_mode: {s['approval_mode']}")
    sig = s["signal"]
    typer.echo(
        f"signal:        {sig['symbol']} {sig['direction']} "
        f"strength={sig['strength']} @ {sig['generated_at']}"
    )
    intent = s["intent"]
    typer.echo(
        f"intent:        {intent['side']} {intent['quantity']} "
        f"{intent['symbol']} ({intent['order_type']})"
    )
    appr = s["approval"]
    typer.echo(
        f"approval:      {appr['record_id']} status={appr['status']} "
        f"mode={appr['mode']} go={appr['go']}"
    )
    if s.get("live_summary"):
        order = s["live_summary"].get("order", {})
        typer.echo(
            f"venue order:   accepted={order.get('accepted')} "
            f"canceled={order.get('canceled')} rejected={order.get('rejected')}"
        )
    typer.echo(f"summary:       {result.summary_path}")
    typer.echo(f"note:          {s['note']}")

    if not result.passed:
        # auto/manual paths only — dry_run intentionally writes passed=False
        # but should not be treated as a process failure.
        if s["approval_mode"] == "dry_run":
            typer.echo("dry_run: intent recorded, no venue submission.")
            return
        typer.echo("live signal-run FAILED (lifecycle incomplete).", err=True)
        raise typer.Exit(code=1)
    typer.echo("live signal-run PASSED.")


# ---------------------------------------------------------------------------
# `xtrade scan ...` (Phase 2 — opportunity discovery / scanner layer)
# ---------------------------------------------------------------------------


@scan_app.command("universe")
def scan_universe(
    config_path: Path = typer.Option(
        Path("config/universe.example.yaml"),
        "--config",
        help="Path to the universe yaml (default: config/universe.example.yaml).",
    ),
) -> None:
    """Parse a universe yaml and print the resolved symbol list."""
    from xtrade.research.universe import UniverseConfigError, load_universe

    try:
        universe = load_universe(config_path)
    except UniverseConfigError as exc:
        raise _exit_config_error(str(exc)) from exc

    typer.echo(f"universe: {universe.source_path}")
    typer.echo(f"symbols:  {len(universe)}")
    for venue, rows in universe.by_venue().items():
        typer.echo(f"  {venue} ({len(rows)}):")
        for spec in rows:
            extras = f" quote={spec.quote}"
            if spec.min_volume is not None:
                extras += f" min_volume={spec.min_volume}"
            typer.echo(f"    - {spec.symbol}{extras}")


@scan_app.command("run")
def scan_run(
    universe_path: Path = typer.Option(
        Path("config/universe.example.yaml"),
        "--universe",
        help="Path to the universe yaml (default: config/universe.example.yaml).",
    ),
    scanner: str = typer.Option(
        "momentum",
        "--scanner",
        help="Scanner registry key (momentum / mean_reversion / breakout / spread).",
    ),
    bar: str = typer.Option("1m", "--bar", help="Bar spec, e.g. 1m, 5m, 1h"),
    since: str | None = typer.Option(
        None, "--since", help="ISO-8601 lower bound (inclusive)."
    ),
    until: str | None = typer.Option(
        None, "--until", help="ISO-8601 upper bound (inclusive)."
    ),
    scoring: str = typer.Option(
        "sharpe", "--scoring", help="Ranking rule: sharpe | total_return | robust."
    ),
    top_k: int = typer.Option(
        5, "--top-k", help="Keep this many top-ranked parameter combos."
    ),
    catalog_path: Path | None = typer.Option(
        None, "--catalog", help="Catalog root (default: <repo>/data/catalog)."
    ),
    queue_root: Path = typer.Option(
        Path("data/signals"),
        "--queue-root",
        help="Signal queue root directory (default: data/signals).",
    ),
    strict: bool = typer.Option(
        False, "--strict", help="Exit 1 when zero signals are emitted."
    ),
    run_id: str | None = typer.Option(
        None, "--run-id", help="Override the auto-generated run id."
    ),
) -> None:
    """Run one scanner over a universe and write signals to the queue."""
    from xtrade.observability import run_with_logging
    from xtrade.research.runner import ScanError, run_scan
    from xtrade.research.scanners import available_scanners
    from xtrade.research.universe import UniverseConfigError

    if scanner not in available_scanners():
        raise _exit_config_error(
            f"--scanner must be one of {available_scanners()}, got {scanner!r}"
        )

    since_ns = _parse_iso_to_ms(since) * 1_000_000 if since else None
    until_ns = _parse_iso_to_ms(until, end_of_day=True) * 1_000_000 if until else None
    if since_ns is not None and until_ns is not None and until_ns <= since_ns:
        raise _exit_config_error(f"--until ({until}) must be after --since ({since})")

    try:
        with run_with_logging(mode="scan", run_id=run_id) as ctx:
            result = run_scan(
                universe_path=universe_path,
                scanner_name=scanner,
                bar=bar,
                since_ns=since_ns,
                until_ns=until_ns,
                param_grid=None,  # use scanner default for now
                scoring=scoring,
                top_k=top_k,
                queue_root=queue_root,
                log_dir=ctx.log_dir,
                run_id=ctx.run_id,
                catalog_path=catalog_path,
                strict=strict,
            )
    except UniverseConfigError as exc:
        raise _exit_config_error(str(exc)) from exc
    except ValueError as exc:
        raise _exit_config_error(str(exc)) from exc
    except ScanError as exc:
        typer.echo(f"scan failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    s = result.summary
    typer.echo(f"run_id:           {s['run_id']}")
    typer.echo(f"scanner:          {s['scanner']}")
    typer.echo(f"universe_size:    {s['universe_size']}")
    if s["universe_skipped"]:
        typer.echo(f"  skipped:        {len(s['universe_skipped'])}")
    typer.echo(f"param_combos:     {s['param_combos']}")
    typer.echo(f"signals_emitted:  {s['signals_emitted']}")
    typer.echo(f"elapsed_s:        {s['elapsed_s']}")
    typer.echo(f"summary:          {result.summary_path}")

    if not result.top_k.empty:
        typer.echo("\ntop-k parameter combos:")
        for row in result.top_k.itertuples(index=False):
            typer.echo(
                f"  {row.params}  sharpe={row.sharpe:.3f}  "
                f"return={row.total_return:.3f}  n_trades={row.n_trades}"
            )

    if not result.passed:
        typer.echo("scan FAILED (strict mode: zero signals emitted).", err=True)
        raise typer.Exit(code=1)


@scan_app.command("inspect")
def scan_inspect(
    queue_root: Path = typer.Option(
        Path("data/signals"),
        "--queue-root",
        help="Signal queue root (default: data/signals).",
    ),
    since: str | None = typer.Option(
        None, "--since", help="ISO-8601 lower bound (inclusive)."
    ),
    source: str | None = typer.Option(
        None, "--source", help="Filter by source (scanner:hash)."
    ),
    symbol: str | None = typer.Option(
        None, "--symbol", help="Filter by symbol (Nautilus InstrumentId)."
    ),
    limit: int = typer.Option(20, "--limit", help="Max signals to display."),
) -> None:
    """List recent signals from the on-disk queue."""
    from xtrade.research.signals import SignalQueue

    if not queue_root.exists():
        typer.echo(f"queue root does not exist: {queue_root}")
        return

    queue = SignalQueue(queue_root)

    if since is not None:
        since_ms = _parse_iso_to_ms(since)
        since_dt = dt.datetime.fromtimestamp(since_ms / 1000, tz=dt.timezone.utc)
        candidates = queue.since(since_dt)
    else:
        candidates = list(queue)

    if source is not None or symbol is not None:
        candidates = [
            s
            for s in candidates
            if (source is None or s.source == source)
            and (symbol is None or s.symbol == symbol)
        ]

    tail = candidates[-limit:] if limit > 0 else candidates
    typer.echo(f"queue:    {queue_root}")
    typer.echo(f"matching: {len(candidates)} (showing last {len(tail)})")
    for s in tail:
        typer.echo(
            f"  {s.generated_at.isoformat()}  {s.symbol:<24}  "
            f"{s.direction:<5}  strength={s.strength:+.2f}  source={s.source}"
        )


# ---------------------------------------------------------------------------
# `xtrade strategy ...`
# ---------------------------------------------------------------------------


@strategy_app.command("list")
def strategy_list() -> None:
    """List registered `SignalDrivenStrategy` plugins."""
    # Importing the package triggers plugin registration via its
    # __init__.py side effect.
    import xtrade.strategy  # noqa: F401
    from xtrade.strategy.base import available_strategies, load_strategy

    names = available_strategies()
    if not names:
        typer.echo("(no strategies registered)")
        return
    for name in names:
        try:
            doc = (load_strategy(name).__class__.__doc__ or "").strip().splitlines()
            tagline = doc[0] if doc else ""
        except Exception:
            tagline = ""
        typer.echo(f"{name:<24}  {tagline}")


@strategy_app.command("describe")
def strategy_describe(
    name: str = typer.Argument(..., help="Strategy registry key."),
) -> None:
    """Print a JSON description of one strategy."""
    import json as _json

    import xtrade.strategy  # noqa: F401
    from xtrade.strategy.base import (
        StrategyRegistrationError,
        available_strategies,
        load_strategy,
    )

    try:
        strat = load_strategy(name)
    except StrategyRegistrationError as exc:
        raise _exit_config_error(
            f"unknown strategy {name!r}; available: {available_strategies()}"
        ) from exc
    typer.echo(_json.dumps(strat.describe(), indent=2, default=str, sort_keys=True))


# ---------------------------------------------------------------------------
# `xtrade approve ...`
# ---------------------------------------------------------------------------


def _approvals_root(override: Path | None) -> Path:
    if override is not None:
        return override
    repo_root = Path(__file__).resolve().parents[2]
    return repo_root / "data" / "approvals"


@approve_app.command("list")
def approve_list(
    status: str | None = typer.Option(None, "--status", help="Filter: pending / confirmed / rejected."),
    since: str | None = typer.Option(None, "--since", help="ISO-8601 UTC lower bound (inclusive)."),
    root: Path | None = typer.Option(None, "--root", help="Approvals queue root (default: <repo>/data/approvals)."),
) -> None:
    """List rows in the approval queue."""
    from xtrade.approval import ApprovalQueue, ApprovalQueueError

    if status is not None and status not in {"pending", "confirmed", "rejected"}:
        raise _exit_config_error(
            f"--status must be one of pending/confirmed/rejected, got {status!r}"
        )
    since_dt: dt.datetime | None = None
    if since is not None:
        try:
            since_dt = dt.datetime.fromisoformat(since.replace("Z", "+00:00"))
        except ValueError as exc:
            raise _exit_config_error(f"--since must be ISO-8601, got {since!r}") from exc
        if since_dt.tzinfo is None:
            since_dt = since_dt.replace(tzinfo=dt.timezone.utc)
    q = ApprovalQueue(_approvals_root(root))
    try:
        rows = q.list(status=status, since=since_dt)  # type: ignore[arg-type]
    except ApprovalQueueError as exc:
        raise _exit_config_error(str(exc)) from exc
    if not rows:
        typer.echo("(no approvals match)")
        return
    for r in rows:
        typer.echo(
            f"{r.id}  {r.status:<9}  mode={r.mode:<8}  "
            f"{r.intent.side} {r.intent.quantity} {r.intent.symbol}  "
            f"created={r.created_at.isoformat()}"
        )


@approve_app.command("confirm")
def approve_confirm(
    approval_id: str = typer.Argument(..., help="Approval row id (16 hex chars)."),
    root: Path | None = typer.Option(None, "--root", help="Approvals queue root."),
) -> None:
    """Flip a pending row to `confirmed`."""
    from xtrade.approval import ApprovalQueue, ApprovalQueueError

    q = ApprovalQueue(_approvals_root(root))
    try:
        rec = q.patch(approval_id, status="confirmed")
    except ApprovalQueueError as exc:
        raise _exit_config_error(str(exc)) from exc
    typer.echo(f"confirmed: {rec.id} at {rec.decided_at.isoformat() if rec.decided_at else '-'}")


@approve_app.command("reject")
def approve_reject(
    approval_id: str = typer.Argument(..., help="Approval row id (16 hex chars)."),
    reason: str = typer.Option("", "--reason", help="Optional reason string."),
    root: Path | None = typer.Option(None, "--root", help="Approvals queue root."),
) -> None:
    """Flip a pending row to `rejected`."""
    from xtrade.approval import ApprovalQueue, ApprovalQueueError

    q = ApprovalQueue(_approvals_root(root))
    try:
        rec = q.patch(approval_id, status="rejected", reason=reason)
    except ApprovalQueueError as exc:
        raise _exit_config_error(str(exc)) from exc
    typer.echo(f"rejected: {rec.id} reason={rec.reason!r}")


# ---------------------------------------------------------------------------
# `xtrade paper ...`
# ---------------------------------------------------------------------------


@paper_app.command("run")
def paper_run(
    strategy: str = typer.Option(..., "--strategy", help="SignalDrivenStrategy registry key."),
    instrument: str = typer.Option(..., "--instrument", help="e.g. BTCUSDT-PERP.BINANCE"),
    signals_from: Path = typer.Option(..., "--signals-from", help="SignalQueue root directory."),
    bar: str = typer.Option("1m", "--bar", help="Bar spec, e.g. 1m, 5m, 1h."),
    since: str | None = typer.Option(None, "--since", help="ISO-8601 lower bound (inclusive)."),
    until: str | None = typer.Option(None, "--until", help="ISO-8601 upper bound (inclusive)."),
    mode: str = typer.Option("auto", "--mode", help="auto / dry_run / manual."),
    risk_config: Path | None = typer.Option(None, "--risk-config", help="Path to risk.yaml."),
    starting_balance: int = typer.Option(1_000_000, "--starting-balance", help="Starting cash in settlement ccy."),
    catalog_path: Path | None = typer.Option(None, "--catalog", help="Catalog root (default: <repo>/data/catalog)."),
    approvals_root: Path | None = typer.Option(None, "--approvals-root", help="Approvals queue root."),
    run_id: str | None = typer.Option(None, "--run-id", help="Override the auto-generated run id."),
) -> None:
    """Drive a `SignalDrivenStrategy` over catalog bars + signals."""
    from xtrade.observability import run_with_logging
    from xtrade.risk import load_rules_from_yaml
    from xtrade.strategy.runner import run_paper

    if mode not in {"auto", "dry_run", "manual"}:
        raise _exit_config_error(
            f"--mode must be auto/dry_run/manual, got {mode!r}"
        )
    rules = []
    if risk_config is not None:
        try:
            rules = load_rules_from_yaml(risk_config)
        except (FileNotFoundError, ValueError) as exc:
            raise _exit_config_error(str(exc)) from exc

    def _parse_iso(text: str | None) -> dt.datetime | None:
        if text is None:
            return None
        try:
            parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise _exit_config_error(f"invalid ISO datetime {text!r}") from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed

    since_dt = _parse_iso(since)
    until_dt = _parse_iso(until)
    if since_dt is not None and until_dt is not None and until_dt <= since_dt:
        raise _exit_config_error(f"--until ({until}) must be after --since ({since})")

    try:
        with run_with_logging(mode="paper", run_id=run_id) as ctx:
            result = run_paper(
                strategy_name=strategy,
                catalog_path=catalog_path,
                instrument_id=instrument,
                bar=bar,
                signals_root=signals_from,
                since=since_dt,
                until=until_dt,
                approval_mode=mode,
                risk_rules=rules,
                starting_balance=starting_balance,
                approvals_root=approvals_root,
                run_id=ctx.run_id,
                logs_root=ctx.logs_root,
            )
    except FileNotFoundError as exc:
        raise _exit_config_error(str(exc)) from exc
    except ValueError as exc:
        raise _exit_config_error(str(exc)) from exc

    s = result.summary
    typer.echo(f"run_id:             {s['run_id']}")
    typer.echo(f"instrument:         {s['instrument_id']}")
    typer.echo(f"bars loaded:        {s['bars_loaded']}")
    typer.echo(f"signals consumed:   {s['signals_consumed']}")
    typer.echo(f"intents generated:  {s['intents_generated']}")
    typer.echo(f"risk rejected:      {s['risk_rejected']}")
    typer.echo(f"approvals pending:  {s['approvals_pending']}")
    typer.echo(f"approvals confirmed:{s['approvals_confirmed']}")
    typer.echo(f"approvals dry_run:  {s['approvals_dry_run']}")
    typer.echo(f"fills:              {s['fills']}")
    typer.echo(f"final NAV (USD):    {s['final_nav_usd']}")
    typer.echo(f"summary:            {result.summary_path}")


def main() -> None:  # pragma: no cover - thin shim
    """Module-level entry for `python -m xtrade.cli`."""
    app()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
