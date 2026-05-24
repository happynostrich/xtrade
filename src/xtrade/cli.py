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
risk_app = typer.Typer(help="Phase 3.5 risk calibration: pre-flight strategy + risk.yaml without I/O.")
bridge_app = typer.Typer(help="Phase 4 openclaw bridge: localhost callback receiver.")
ops_app = typer.Typer(help="Phase 4 ops: status / pause / resume / kill (pure file-system; safe to call when supervisor is dead).")

app.add_typer(data_app, name="data")
app.add_typer(backtest_app, name="backtest")
app.add_typer(live_app, name="live")
app.add_typer(scan_app, name="scan")
app.add_typer(strategy_app, name="strategy")
app.add_typer(paper_app, name="paper")
app.add_typer(approve_app, name="approve")
app.add_typer(risk_app, name="risk")
app.add_typer(bridge_app, name="bridge")
app.add_typer(ops_app, name="ops")


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

_DEFAULT_LIVE_HEALTH_VENUES = "binance_spot,binance_futures,hyperliquid"


def _venue_for_instrument(iid_str: str) -> str:
    """Map a Nautilus instrument id to the venue key that handles it.

    Used by `xtrade live health` when the operator passes `--instrument`
    but leaves `--venues` at its default — probing all three venues is
    rarely what the operator means in that case, and (because Binance
    spot+futures share `Venue('BINANCE')` inside Nautilus) tripping
    both Binance subaccounts at once breaks `node.build()`.
    """
    if iid_str.endswith(".HYPERLIQUID"):
        return "hyperliquid"
    if iid_str.endswith(".BINANCE"):
        # Convention: futures perps carry "-PERP" in the symbol part.
        symbol = iid_str.split(".", 1)[0]
        return "binance_futures" if "-PERP" in symbol else "binance_spot"
    raise typer.BadParameter(
        f"cannot infer venue for instrument id {iid_str!r}; pass --venues explicitly."
    )


def _resolve_per_venue_yaml(venue_key: str, base_yaml: Path) -> Path | None:
    """Return the sibling per-venue yaml for `venue_key`, or None if it
    doesn't exist.

    Convention: `venues.<venue_key>.testnet.yaml` lives next to the
    `--venues-yaml` path (which on the default invocation is
    `config/venues.testnet.yaml`). This lets `xtrade live health`
    chain one TradingNode per venue from the same config directory
    without forcing the operator to pass `--venues-yaml` three times.
    """
    candidate = base_yaml.parent / f"venues.{venue_key}.testnet.yaml"
    return candidate if candidate.exists() else None


_DEFAULT_VENUES_YAML = Path("config/venues.testnet.yaml")


def _auto_resolve_default_venues_yaml(venues_yaml: Path, instrument_id: str) -> Path:
    """For single-instrument commands, swap the gutted pointer default
    in for the per-venue sibling that matches `instrument_id`.

    `config/venues.testnet.yaml` is intentionally gutted — it carries
    no venues; it exists only so `xtrade live health` can auto-discover
    per-venue siblings. Single-venue commands (`live run`,
    `live signal-run`) otherwise fail on the default invocation with
    "venues.testnet.yaml has no venues". Mapping the instrument id
    → venue key → sibling yaml lets the operator skip --venues-yaml
    in the common case.

    Returns `venues_yaml` unchanged when:
      - The operator supplied an explicit non-default path
      - The instrument id is unparseable (let load_venues raise)
      - The sibling yaml does not exist (let load_venues raise)
    """
    if venues_yaml != _DEFAULT_VENUES_YAML:
        return venues_yaml
    try:
        venue_key = _venue_for_instrument(instrument_id)
    except typer.BadParameter:
        return venues_yaml
    per_venue = _resolve_per_venue_yaml(venue_key, venues_yaml)
    if per_venue is None:
        return venues_yaml
    typer.echo(
        f"note: --venues-yaml defaulted; using {per_venue} for {instrument_id}.",
        err=True,
    )
    return per_venue


def _narrow_venues_cfg(cfg, venue_keys: list[str]):
    """Return a new VenuesConfig containing only the requested venue keys.

    The loaded yaml may populate `binance.spot`, `binance.futures`, and
    `hyperliquid` all at once even though a single `xtrade live health`
    call only needs a subset. Passing the full config through to
    `build_testnet_node` would trip the spot+futures coexistence guard
    (Nautilus registers both Binance subaccounts under the same
    `Venue('BINANCE')`), so we narrow here before handing off to
    `probe()`.

    Raises typer.Exit (via `_exit_config_error`) if a requested venue
    key isn't present in the loaded yaml.
    """
    from xtrade.config import BinanceVenueConfig, VenuesConfig

    want_spot = "binance_spot" in venue_keys
    want_fut = "binance_futures" in venue_keys
    want_hl = "hyperliquid" in venue_keys

    missing: list[str] = []
    if want_spot and (cfg.binance is None or cfg.binance.spot is None):
        missing.append("binance_spot")
    if want_fut and (cfg.binance is None or cfg.binance.futures is None):
        missing.append("binance_futures")
    if want_hl and cfg.hyperliquid is None:
        missing.append("hyperliquid")
    if missing:
        raise _exit_config_error(
            f"venue keys {missing} requested but not configured in "
            f"venues yaml ({cfg.source_path}). Either populate them in "
            f"the yaml or pass --venues with only the keys you've set up."
        )

    new_binance: BinanceVenueConfig | None = None
    if want_spot or want_fut:
        new_binance = BinanceVenueConfig(
            spot=cfg.binance.spot if want_spot else None,
            futures=cfg.binance.futures if want_fut else None,
        )

    new_hl = cfg.hyperliquid if want_hl else None

    return VenuesConfig(
        binance=new_binance,
        hyperliquid=new_hl,
        source_path=cfg.source_path,
    )


@live_app.command("health")
def live_health(
    venues: str = typer.Option(
        _DEFAULT_LIVE_HEALTH_VENUES,
        "--venues",
        help=(
            "Comma-separated venue keys from venues yaml. When --instrument is "
            "given and --venues is left at its default, venues are inferred "
            "from each instrument id (Binance spot+futures cannot probe in one "
            "node — see VenueConfigError)."
        ),
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
    from xtrade.node.factory import MainnetRefusedError, VenueConfigError
    from xtrade.node.health import probe
    from xtrade.observability import run_with_logging

    # If --instrument is given AND --venues is still the default, infer
    # the venue subset from the instrument id(s). This avoids probing
    # all three default venues when the operator clearly meant only one,
    # and dodges the spot+futures-coexist VenueConfigError in the common
    # case of a yaml that has both populated.
    if instruments and venues == _DEFAULT_LIVE_HEALTH_VENUES:
        raw_iids = [s.strip() for s in instruments.split(",") if s.strip()]
        try:
            inferred = sorted({_venue_for_instrument(s) for s in raw_iids})
        except typer.BadParameter as exc:
            raise _exit_config_error(str(exc)) from exc
        venue_keys = inferred
        typer.echo(
            f"note: --venues defaulted; inferred {venue_keys} from "
            f"--instrument={raw_iids}.",
            err=True,
        )
    else:
        venue_keys = [v.strip() for v in venues.split(",") if v.strip()]

    if not venue_keys:
        raise _exit_config_error("--venues must not be empty.")
    unknown = [v for v in venue_keys if v not in _DEFAULT_HEALTH_INSTRUMENTS]
    if unknown:
        raise _exit_config_error(
            f"unknown venue keys: {unknown}. "
            f"Valid: {sorted(_DEFAULT_HEALTH_INSTRUMENTS)}."
        )

    # Group requested instruments by their inferred venue. If --instrument
    # was not given, use the per-venue defaults. Anything that doesn't
    # map to one of the venues we're iterating is an error — operator
    # likely passed an instrument that doesn't match --venues.
    if instruments:
        iid_strs = [s.strip() for s in instruments.split(",") if s.strip()]
    else:
        iid_strs = [_DEFAULT_HEALTH_INSTRUMENTS[v] for v in venue_keys]

    instruments_by_venue: dict[str, list[str]] = {v: [] for v in venue_keys}
    for s in iid_strs:
        try:
            v = _venue_for_instrument(s)
        except typer.BadParameter as exc:
            raise _exit_config_error(str(exc)) from exc
        if v not in instruments_by_venue:
            raise _exit_config_error(
                f"instrument {s!r} maps to venue {v!r} but --venues only "
                f"requested {venue_keys}; add the venue or remove the instrument."
            )
        instruments_by_venue[v].append(s)

    # Now iterate one TradingNode per venue. Each gets its own yaml
    # (per-venue file next to --venues-yaml, or the explicit --venues-yaml
    # for ad-hoc single-venue setups), its own run_id, and its own log
    # dir. Sequential, not parallel — running two Binance nodes in
    # parallel re-introduces the spot+futures Venue collision at the
    # process level.
    overall_pass = True
    per_venue_outputs: list = []

    for venue_key in venue_keys:
        venue_iids_str = instruments_by_venue[venue_key]
        if not venue_iids_str:
            # Possible only if --instrument was given with no entries for
            # this venue. Skip with a warning rather than failing.
            typer.echo(
                f"note: no instruments specified for venue {venue_key!r}; skipping.",
                err=True,
            )
            continue

        per_venue_yaml = _resolve_per_venue_yaml(venue_key, venues_yaml)
        yaml_for_venue = per_venue_yaml if per_venue_yaml is not None else venues_yaml
        try:
            venues_cfg = load_venues(yaml_for_venue)
        except (ConfigError, MissingCredentialError) as exc:
            raise _exit_config_error(
                f"venue {venue_key!r}: {exc}"
            ) from exc

        # If we loaded the explicit (non-split) yaml, narrow to just
        # this venue so the factory guard doesn't trip on coexisting
        # subaccounts. If we loaded the per-venue file, the narrow is
        # a no-op but still validates the file contains what we expect.
        venues_cfg = _narrow_venues_cfg(venues_cfg, [venue_key])

        try:
            venue_iids = [InstrumentId.from_str(s) for s in venue_iids_str]
        except Exception as exc:  # noqa: BLE001
            raise _exit_config_error(f"failed to parse instrument id: {exc}") from exc

        # Per-venue run_id: when chaining, append the venue key so each
        # subrun has its own log dir. When only one venue is involved,
        # honor --run-id verbatim.
        sub_run_id: str | None
        if run_id is None:
            sub_run_id = None
        elif len(venue_keys) == 1:
            sub_run_id = run_id
        else:
            sub_run_id = f"{run_id}-{venue_key}"

        try:
            with run_with_logging(
                mode="health", run_id=sub_run_id, venues_cfg=venues_cfg
            ) as ctx:
                result = probe(
                    venues_cfg,
                    instruments=venue_iids,
                    timeout_s=float(timeout),
                    run_id=ctx.run_id,
                    logs_root=ctx.logs_root,
                )
        except (MainnetRefusedError, VenueConfigError) as exc:
            raise _exit_config_error(str(exc)) from exc

        per_venue_outputs.append((venue_key, result))
        if not result.passed:
            overall_pass = False

    typer.echo("")
    for venue_key, result in per_venue_outputs:
        typer.echo(f"== {venue_key} ==")
        typer.echo(f"  run_id:  {result.run_id}")
        typer.echo(f"  summary: {result.summary_path}")
        for iid_str, entry in result.summary["per_instrument"].items():
            if entry["first_quote_iso"] is None:
                typer.echo(f"    {iid_str}: NO QUOTE within {timeout}s")
            else:
                typer.echo(
                    f"    {iid_str}: first_quote={entry['first_quote_iso']} "
                    f"(+{entry['first_quote_latency_ms']} ms)"
                )

    if not overall_pass:
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
        help=(
            "Path to the venues yaml. If left at the default, the CLI "
            "auto-resolves to the per-venue sibling matching --instrument "
            "(e.g. config/venues.binance_futures.testnet.yaml)."
        ),
    ),
    run_id: str | None = typer.Option(None, "--run-id", help="Override the auto run id."),
) -> None:
    """Run a strategy live against testnets (places one far-from-market limit
    order, awaits accept + cancel)."""
    from decimal import Decimal, InvalidOperation

    from xtrade.config import ConfigError, MissingCredentialError, load_venues
    from xtrade.live.runner import available_live_strategies, run_live
    from xtrade.node.factory import MainnetRefusedError, VenueConfigError
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

    venues_yaml = _auto_resolve_default_venues_yaml(venues_yaml, instrument)
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
    except (MainnetRefusedError, VenueConfigError) as exc:
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
        help=(
            "Path to the venues yaml. If left at the default, the CLI "
            "auto-resolves to the per-venue sibling matching --instrument "
            "(e.g. config/venues.binance_futures.testnet.yaml)."
        ),
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
    from xtrade.node.factory import MainnetRefusedError, VenueConfigError
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

    venues_yaml = _auto_resolve_default_venues_yaml(venues_yaml, instrument)
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
    except (MainnetRefusedError, VenueConfigError) as exc:
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


@live_app.command("supervise")
def live_supervise(
    config_path: Path = typer.Option(
        ..., "--config", help="Path to supervisor.yaml."
    ),
    max_iterations: int | None = typer.Option(
        None,
        "--max-iterations",
        help="Stop after N poll iterations (smoke / drill only).",
    ),
    log_level: str = typer.Option(
        "INFO", "--log-level", help="Root logger level for the supervisor."
    ),
) -> None:
    """Run the always-on Phase 4 supervisor loop.

    The supervisor polls `signals_root` for new signals and drives each
    one through strategy → RiskGate → ApprovalGate. Manual approvals
    are dispatched to openclaw via `OpenclawBridge` (built from
    `OPENCLAW_*` env vars when present, else None). The loop runs
    forever until SIGINT/SIGTERM, which systemd sends on `systemctl
    stop xtrade-supervisor`.
    """
    import logging
    import os
    import signal as _signal
    import threading
    from typing import Any

    from xtrade.bridge.openclaw_webhook import BridgeConfigError, OpenclawBridge
    from xtrade.live.supervisor import (
        SupervisorIterationResult,
        load_supervisor_config,
        run_supervisor,
    )

    logging.basicConfig(
        level=log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    bridge: OpenclawBridge | None = None
    if "OPENCLAW_GATEWAY" in os.environ and "OPENCLAW_SHARED_SECRET" in os.environ:
        try:
            bridge = OpenclawBridge.from_env(os.environ)
        except BridgeConfigError as exc:
            raise _exit_config_error(f"OpenclawBridge config: {exc}") from exc

    try:
        config = load_supervisor_config(config_path, bridge=bridge)
    except FileNotFoundError as exc:
        raise _exit_config_error(f"supervisor config not found: {exc}") from exc
    except KeyError as exc:
        raise _exit_config_error(f"supervisor.yaml missing key: {exc}") from exc

    stop_event = threading.Event()

    def _on_signal(signum: int, _frame: Any) -> None:
        typer.echo(f"supervisor: received signal {signum}; draining...", err=True)
        stop_event.set()

    _signal.signal(_signal.SIGINT, _on_signal)
    _signal.signal(_signal.SIGTERM, _on_signal)

    results: list[SupervisorIterationResult] = run_supervisor(
        config,
        stop_event=stop_event,
        max_iterations=max_iterations,
    )
    typer.echo(
        f"supervisor: stopped after {len(results)} iterations; "
        f"submitted={sum(r.intents_submitted for r in results)}, "
        f"parked_manual={sum(r.intents_parked_manual for r in results)}"
    )


# ---------------------------------------------------------------------------
# `xtrade bridge ...` (Phase 4 — openclaw inbound callback receiver)
# ---------------------------------------------------------------------------


@bridge_app.command("serve")
def bridge_serve(
    approvals_root: Path = typer.Option(
        ..., "--approvals-root", help="Path to the ApprovalQueue root dir (writable)."
    ),
    bind: str = typer.Option(
        "127.0.0.1", "--bind", help="Loopback address to bind (127.0.0.1 / ::1)."
    ),
    port: int = typer.Option(
        18080, "--port", help="TCP port (loopback only).",
    ),
    ttl_s: int = typer.Option(
        900, "--ttl-s", help="Reject callbacks older than this many seconds.",
    ),
    log_level: str = typer.Option(
        "INFO", "--log-level", help="Root logger level for the bridge.",
    ),
) -> None:
    """Run the openclaw inbound webhook receiver (Phase 4 / T4).

    Reads `OPENCLAW_INBOUND_SECRET` from the environment (typically
    sourced via systemd `EnvironmentFile=/etc/xtrade/env`). Refuses to
    bind any non-loopback address — the systemd unit additionally sets
    `IPAddressDeny=any + IPAddressAllow=127.0.0.0/8`.
    """
    import logging
    import os
    import signal as _signal
    from typing import Any

    from xtrade.bridge.inbound import InboundConfig, build_server

    logging.basicConfig(
        level=log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    secret = os.environ.get("OPENCLAW_INBOUND_SECRET", "").strip()
    if not secret:
        raise _exit_config_error(
            "OPENCLAW_INBOUND_SECRET must be set (e.g. via /etc/xtrade/env)."
        )

    try:
        config = InboundConfig(
            approvals_root=approvals_root,
            shared_secret=secret,
            bind=bind,
            port=port,
            ttl_s=ttl_s,
        )
    except ValueError as exc:
        raise _exit_config_error(f"inbound config: {exc}") from exc

    try:
        server = build_server(config)
    except OSError as exc:
        raise _exit_config_error(f"bind {bind}:{port} failed: {exc}") from exc

    bind_host, bind_port = server.server_address[:2]
    typer.echo(
        f"bridge: listening on http://{bind_host}:{bind_port} "
        f"(approvals={approvals_root}, ttl={ttl_s}s)",
        err=True,
    )

    def _on_signal(signum: int, _frame: Any) -> None:
        typer.echo(f"bridge: signal {signum} received; shutting down...", err=True)
        # `shutdown()` is the documented thread-safe stop hook for
        # ThreadingHTTPServer; it joins the serve_forever loop.
        import threading

        threading.Thread(target=server.shutdown, daemon=True).start()

    _signal.signal(_signal.SIGINT, _on_signal)
    _signal.signal(_signal.SIGTERM, _on_signal)

    try:
        server.serve_forever()
    finally:
        server.server_close()
        typer.echo("bridge: stopped.", err=True)


# ---------------------------------------------------------------------------
# `xtrade ops ...` (Phase 4 — operator runtime status + pause/resume/kill)
# ---------------------------------------------------------------------------


@ops_app.command("status")
def ops_status(
    sentinel_path: Path = typer.Option(
        Path("/run/xtrade/paused.flag"),
        "--sentinel-path",
        help="Sentinel flag path (paused if file exists).",
    ),
    signals_root: Path = typer.Option(
        Path("/var/lib/xtrade/signals"),
        "--signals-root",
        help="Signals queue root (used only for path bundling — current implementation reads the cursor instead).",
    ),
    cursor_path: Path = typer.Option(
        Path("/var/lib/xtrade/signals/.cursor"),
        "--cursor-path",
        help="SignalConsumer cursor file (JSON; format defined in xtrade.strategy.cursor).",
    ),
    approvals_root: Path = typer.Option(
        Path("/var/lib/xtrade/approvals"),
        "--approvals-root",
        help="ApprovalQueue root (daily jsonl shards).",
    ),
    logs_root: Path = typer.Option(
        Path("/var/lib/xtrade/logs"),
        "--logs-root",
        help="Run logs root: scanned for the most recent <run-id>/live_signal_summary.json.",
    ),
    supervisor_unit: str = typer.Option(
        "xtrade-supervisor.service",
        "--supervisor-unit",
        help="systemd unit name probed via `systemctl show`. Reports 'unknown' if systemctl is unavailable.",
    ),
    json_output: bool = typer.Option(
        False, "--json", help="Emit JSON instead of the human one-liner + multi-line summary.",
    ),
) -> None:
    """Print supervisor + bridge + sentinel status as a one-liner + detail block (or JSON).

    Pure file-system reads (plus an optional `systemctl show` probe);
    safe to call when xtrade-supervisor.service is dead or crash-looping.
    """
    from xtrade.ops import (
        OpsPaths,
        collect_status,
        render_status_json,
        render_status_text,
    )

    paths = OpsPaths(
        signals_root=signals_root,
        approvals_root=approvals_root,
        cursor_path=cursor_path,
        sentinel_path=sentinel_path,
        logs_root=logs_root,
        supervisor_unit=supervisor_unit,
    )
    status = collect_status(paths)
    if json_output:
        typer.echo(render_status_json(status))
    else:
        typer.echo(render_status_text(status))


@ops_app.command("pause")
def ops_pause(
    reason: str = typer.Option("", "--reason", help="Free-text reason recorded in the sentinel body."),
    sentinel_path: Path = typer.Option(
        Path("/run/xtrade/paused.flag"),
        "--sentinel-path",
        help="Sentinel flag path (default: /run/xtrade/paused.flag on VPS).",
    ),
) -> None:
    """Park the supervisor: it will keep handling callbacks but submit no new venue orders.

    Writes ``/run/xtrade/paused.flag`` atomically. Idempotent — re-pausing
    while paused updates `paused_at` + `reason` in place.
    """
    from xtrade.live.sentinel import Sentinel

    sentinel = Sentinel(sentinel_path)
    body = sentinel.pause(reason=reason)
    typer.echo(
        f"paused: at={body['paused_at']} reason={body['reason']!r} path={sentinel_path}",
    )


@ops_app.command("resume")
def ops_resume(
    sentinel_path: Path = typer.Option(
        Path("/run/xtrade/paused.flag"),
        "--sentinel-path",
        help="Sentinel flag path (default: /run/xtrade/paused.flag on VPS).",
    ),
) -> None:
    """Clear the sentinel so the supervisor begins submitting orders again.

    Idempotent — resuming an already-resumed sentinel returns OK with a
    `not_paused` note.
    """
    from xtrade.live.sentinel import Sentinel

    sentinel = Sentinel(sentinel_path)
    removed = sentinel.resume()
    if removed:
        typer.echo(f"resumed: removed {sentinel_path}")
    else:
        typer.echo(f"resumed: not_paused (no sentinel at {sentinel_path})")


@ops_app.command("kill")
def ops_kill(
    supervisor_unit: str = typer.Option(
        "xtrade-supervisor.service",
        "--supervisor-unit",
        help="systemd unit to stop (default: xtrade-supervisor.service).",
    ),
    confirm: bool = typer.Option(
        False, "--yes", "-y", help="Required: skip interactive confirmation.",
    ),
) -> None:
    """Stop the supervisor unit via `systemctl stop`.

    Requires `--yes`: this is intentionally a destructive action and we
    refuse to make it a one-keystroke mistake. Install_vps.sh wires a
    polkit rule so the xtrade group can stop xtrade-* units without sudo.
    """
    import subprocess as _subprocess

    if not confirm:
        raise _exit_config_error(
            "refusing to stop a unit without --yes; this is a destructive op",
        )
    try:
        proc = _subprocess.run(
            ["systemctl", "stop", supervisor_unit],
            capture_output=True,
            text=True,
            timeout=15.0,
            check=False,
        )
    except FileNotFoundError as exc:
        raise _exit_config_error(f"systemctl not found on PATH: {exc}") from exc
    except _subprocess.TimeoutExpired as exc:
        raise _exit_config_error(f"systemctl stop timed out after 15s: {exc}") from exc

    if proc.returncode != 0:
        typer.echo(proc.stderr.strip(), err=True)
        raise typer.Exit(code=proc.returncode)
    typer.echo(f"stopped: {supervisor_unit}")


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


# ---------------------------------------------------------------------------
# `xtrade risk ...` (Phase 3.5 — calibration helper)
# ---------------------------------------------------------------------------


def _parse_kv_pairs(text: str, *, what: str) -> dict[str, str]:
    """Parse a ``"K=V,K=V"`` string into a dict; raise BadParameter on errors."""
    out: dict[str, str] = {}
    if not text.strip():
        return out
    for chunk in text.split(","):
        c = chunk.strip()
        if not c:
            continue
        if "=" not in c:
            raise typer.BadParameter(
                f"--{what} entry {c!r} must be of the form KEY=VALUE"
            )
        k, v = c.split("=", 1)
        k = k.strip()
        v = v.strip()
        if not k:
            raise typer.BadParameter(f"--{what} entry has empty key: {c!r}")
        out[k] = v
    return out


@risk_app.command("dry-run")
def risk_dry_run(
    strategy: str = typer.Option(..., "--strategy", help="SignalDrivenStrategy registry key."),
    instrument: str = typer.Option(
        ..., "--instrument", help="Default symbol (e.g. BTCUSDT-PERP.BINANCE)."
    ),
    risk_config: Path | None = typer.Option(
        None, "--risk-config", help="Path to risk.yaml."
    ),
    signals_from: Path | None = typer.Option(
        None,
        "--signals-from",
        help="Replay an existing signal from this SignalQueue root.",
    ),
    signal_id: str | None = typer.Option(
        None,
        "--signal-id",
        help="When --signals-from is set: composite '<gen_at>|<symbol>|<source>'; default newest.",
    ),
    synthetic_direction: str | None = typer.Option(
        None,
        "--synthetic-direction",
        help="Synthesise a signal: LONG / SHORT / FLAT (mutually exclusive with --signals-from).",
    ),
    synthetic_strength: float = typer.Option(
        0.6, "--synthetic-strength", help="Synthetic signal strength magnitude (0..1)."
    ),
    synthetic_price: str = typer.Option(
        "50000",
        "--synthetic-price",
        help="`last_price` stamped into metadata of the synthetic signal.",
    ),
    cash: str = typer.Option(
        "100000", "--cash", help="Cash (USD, Decimal-friendly)."
    ),
    positions: str = typer.Option(
        "", "--positions", help='Comma-separated "SYMBOL=AMT" (e.g. "BTCUSDT-PERP.BINANCE=-0.005").'
    ),
    marks: str = typer.Option(
        "",
        "--marks",
        help='Comma-separated "SYMBOL=PRICE"; defaults to "<instrument>=<synthetic-price>".',
    ),
    nav: str | None = typer.Option(None, "--nav", help="NAV USD (default: cash)."),
    peak_nav: str | None = typer.Option(
        None, "--peak-nav", help="Peak NAV USD (default: nav)."
    ),
    strategy_config: str = typer.Option(
        "",
        "--strategy-config",
        help='Comma-separated "KEY=VALUE" passed to the strategy constructor.',
    ),
    as_json: bool = typer.Option(
        False, "--json", help="Emit the full report as JSON on stdout."
    ),
) -> None:
    """Pre-flight a strategy against a signal + risk rules, with no I/O.

    Runs the same chain the paper/live runners walk
    (``strategy.on_signal`` → every rule individually) but stops short of
    any side effects: no orders, no jsonl writes, no Nautilus engine.
    Use it to calibrate a ``risk.yaml`` for a strategy/instrument combo
    before deploying to testnet or the cloud.
    """
    import datetime as _dt
    import json as _json
    from decimal import Decimal, InvalidOperation

    import xtrade.strategy  # noqa: F401 - registers plugins
    from xtrade.research.signals import Signal, SignalQueue
    from xtrade.risk import dry_run, load_rules_from_yaml
    from xtrade.strategy.base import (
        AccountSnapshot,
        StrategyRegistrationError,
        available_strategies,
        load_strategy,
    )

    # ---- mutual exclusivity of signal sources --------------------------
    if signals_from is not None and synthetic_direction is not None:
        raise _exit_config_error(
            "--signals-from and --synthetic-direction are mutually exclusive."
        )

    # ---- strategy ------------------------------------------------------
    cfg_pairs = _parse_kv_pairs(strategy_config, what="strategy-config")
    try:
        strat = load_strategy(strategy, config=cfg_pairs or None)
    except StrategyRegistrationError as exc:
        raise _exit_config_error(
            f"unknown strategy {strategy!r}; available: {available_strategies()}"
        ) from exc

    # ---- rules ---------------------------------------------------------
    rules = []
    if risk_config is not None:
        try:
            rules = load_rules_from_yaml(risk_config)
        except (FileNotFoundError, ValueError) as exc:
            raise _exit_config_error(str(exc)) from exc

    # ---- account -------------------------------------------------------
    try:
        cash_d = Decimal(cash)
        nav_d = Decimal(nav) if nav is not None else cash_d
        peak_d = Decimal(peak_nav) if peak_nav is not None else nav_d
    except (InvalidOperation, ValueError) as exc:
        raise _exit_config_error(f"--cash/--nav/--peak-nav must be decimals: {exc}") from exc

    pos_raw = _parse_kv_pairs(positions, what="positions")
    try:
        pos_d = {k: Decimal(v) for k, v in pos_raw.items()}
    except (InvalidOperation, ValueError) as exc:
        raise _exit_config_error(f"--positions amounts must be decimals: {exc}") from exc

    marks_raw = _parse_kv_pairs(marks, what="marks")
    if not marks_raw:
        marks_raw = {instrument: synthetic_price}
    try:
        marks_d = {k: Decimal(v) for k, v in marks_raw.items()}
    except (InvalidOperation, ValueError) as exc:
        raise _exit_config_error(f"--marks prices must be decimals: {exc}") from exc

    account = AccountSnapshot(
        cash_usd=cash_d,
        positions=pos_d,
        mark_prices=marks_d,
        nav_usd=nav_d,
        peak_nav_usd=peak_d,
    )

    # ---- signal --------------------------------------------------------
    sig: Signal
    if signals_from is not None:
        from xtrade.strategy.consumer import SignalConsumer

        queue = SignalQueue(signals_from)
        consumer = SignalConsumer(queue, symbol=instrument)
        all_sigs = consumer.list_all()
        if not all_sigs:
            raise _exit_config_error(
                f"no signals matched --instrument={instrument!r} in {signals_from}"
            )
        if signal_id is None:
            sig = all_sigs[-1]
        else:
            picked = None
            for s in all_sigs:
                composite = "|".join([s.generated_at.isoformat(), s.symbol, s.source])
                if composite == signal_id:
                    picked = s
                    break
            if picked is None:
                raise _exit_config_error(
                    f"no signal with composite id {signal_id!r} in queue."
                )
            sig = picked
    else:
        direction = (synthetic_direction or "LONG").upper()
        if direction not in {"LONG", "SHORT", "FLAT"}:
            raise _exit_config_error(
                f"--synthetic-direction must be LONG/SHORT/FLAT, got {synthetic_direction!r}"
            )
        if synthetic_strength < 0 or synthetic_strength > 1:
            raise _exit_config_error(
                f"--synthetic-strength must be in [0, 1], got {synthetic_strength}"
            )
        signed_strength = (
            synthetic_strength
            if direction == "LONG"
            else (-synthetic_strength if direction == "SHORT" else 0.0)
        )
        sig = Signal(
            symbol=instrument,
            venue=instrument.split(".")[-1].lower() if "." in instrument else "binance",
            direction=direction,  # type: ignore[arg-type]
            strength=signed_strength,
            generated_at=_dt.datetime.now(tz=_dt.timezone.utc),
            source="cli:risk-dry-run",
            metadata={"last_price": synthetic_price},
        )

    # ---- run + render --------------------------------------------------
    report = dry_run(strategy=strat, signal=sig, account=account, rules=rules)

    if as_json:
        typer.echo(_json.dumps(report.to_dict(), indent=2, default=str))
        return

    typer.echo(f"strategy:           {report.strategy}")
    typer.echo(
        f"signal:             {sig.symbol} {sig.direction} "
        f"strength={sig.strength:+.3f} source={sig.source}"
    )
    typer.echo(f"rules:              {len(rules)}")
    typer.echo(f"intents generated:  {report.intents_generated}")
    typer.echo(f"intents approved:   {report.intents_approved}")
    typer.echo(f"intents rejected:   {report.intents_rejected}")

    for i, ev in enumerate(report.intents):
        verdict = "APPROVED" if ev.aggregate_approved else "REJECTED"
        typer.echo(
            f"\nintent[{i}] {verdict}: {ev.intent.side} {ev.intent.quantity} "
            f"{ev.intent.symbol} ({ev.intent.order_type}"
            + (", reduce_only" if ev.intent.reduce_only else "")
            + ")"
        )
        if not ev.rule_results:
            typer.echo("  (no rules configured)")
        for r in ev.rule_results:
            status = "ok " if r["ok"] else "FAIL"
            reason = f"  — {r['reason']}" if r["reason"] else ""
            typer.echo(f"  [{status}] {r['name']}{reason}")

    if report.intents_generated == 0:
        typer.echo("\nNote: strategy emitted no intent for this signal/account combo.")


def main() -> None:  # pragma: no cover - thin shim
    """Module-level entry for `python -m xtrade.cli`."""
    app()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
