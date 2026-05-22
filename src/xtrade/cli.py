"""xtrade command-line interface.

Typer-based entry point exposing three subcommand groups (`data`,
`backtest`, `live`). Subcommands are filled in by Phase 1 Tasks 3–6.

Exit code contract (P7, partial — full plumbing lands in Task 7):
  0  success
  1  business failure (e.g. order rejected, no quote in timeout)
  2  configuration / precondition failure (missing env, bad config,
     not-yet-implemented commands)
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

app.add_typer(data_app, name="data")
app.add_typer(backtest_app, name="backtest")
app.add_typer(live_app, name="live")


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
            run_id=run_id,
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


@live_app.command("health")
def live_health(
    venues: str = typer.Option(
        "binance_spot,binance_futures,hyperliquid",
        "--venues",
        help="Comma-separated venue keys from venues yaml.",
    ),
    timeout: int = typer.Option(60, "--timeout", help="Seconds to wait for first quote per venue."),
) -> None:
    """Start a testnet node, subscribe to one instrument per venue, await first quote."""
    _not_yet_implemented(
        task="Phase 1 Task 3 — node health probe",
        module="xtrade.node.health",
    )


@live_app.command("run")
def live_run(
    strategy: str = typer.Option(..., "--strategy"),
    venues: str = typer.Option("binance_spot,hyperliquid", "--venues"),
) -> None:
    """Run a strategy live against testnets."""
    _not_yet_implemented(
        task="Phase 1 Task 6 — live testnet runner",
        module="xtrade.node.factory + xtrade.strategies.base",
    )


def main() -> None:  # pragma: no cover - thin shim
    """Module-level entry for `python -m xtrade.cli`."""
    app()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
