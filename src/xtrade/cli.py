"""xtrade command-line interface.

This is the Phase 1 Task 1 deliverable (P1): a typer-based entry point
exposing three subcommand groups (`data`, `backtest`, `live`). The
groups and command names are pinned now so later tasks can fill in
implementations without renaming.

Phase mapping:

  data ingest        -> Task 4 (historical data pipeline)
  data inspect       -> Task 4 (read catalog metadata)
  backtest run       -> Task 5 (offline EMA-cross on catalog bars)
  live run           -> Task 6 (testnet TradingNode probe)
  live health        -> Task 3 (subscribe-only health check)

Until each underlying module is implemented, the commands raise a
clear NotImplementedError that points at the responsible task. This
lets `xtrade --help` and `xtrade <group> --help` work for users while
the rest of Phase 1 is in progress.

Exit codes (P7 contract, partial — full plumbing lands in Task 7):
  0  success
  1  business failure (e.g. order rejected, no quote in timeout)
  2  configuration / precondition failure (missing env, bad config)

Until Task 7 is done, NotImplementedError is mapped to exit code 2
because "this command is not yet wired" is a configuration-class issue
from the user's perspective.
"""

from __future__ import annotations

import sys

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
# `xtrade data ...`
# ---------------------------------------------------------------------------


@data_app.command("ingest")
def data_ingest(
    venue: str = typer.Option(..., "--venue", help="binance | hyperliquid"),
    symbol: str = typer.Option(..., "--symbol", help="e.g. BTCUSDT or xyz:TSLA"),
    bar: str = typer.Option("1m", "--bar", help="Bar spec, e.g. 1m, 5m, 1h"),
    since: str = typer.Option(..., "--since", help="ISO-8601 start, e.g. 2026-05-01"),
    until: str | None = typer.Option(None, "--until", help="ISO-8601 end (default: now)"),
) -> None:
    """Fetch historical bars and append them to the local catalog (idempotent)."""
    _not_yet_implemented(
        task="Phase 1 Task 4 — data ingest pipeline",
        module="xtrade.data.binance_klines / xtrade.data.hyperliquid_hip3",
    )


@data_app.command("inspect")
def data_inspect(
    venue: str | None = typer.Option(None, "--venue"),
    symbol: str | None = typer.Option(None, "--symbol"),
) -> None:
    """List instruments and bar ranges currently held in the catalog."""
    _not_yet_implemented(
        task="Phase 1 Task 4 — catalog inspector",
        module="xtrade.data.catalog",
    )


# ---------------------------------------------------------------------------
# `xtrade backtest ...`
# ---------------------------------------------------------------------------


@backtest_app.command("run")
def backtest_run(
    strategy: str = typer.Option(..., "--strategy", help="e.g. demo_ema"),
    instrument: str = typer.Option(..., "--instrument", help="e.g. BTCUSDT-PERP.BINANCE"),
    since: str = typer.Option(..., "--since"),
    until: str | None = typer.Option(None, "--until"),
) -> None:
    """Run a strategy against catalog bars and write a summary."""
    _not_yet_implemented(
        task="Phase 1 Task 5 — backtest runner",
        module="xtrade.strategies.demo_ema",
    )


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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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


def main() -> None:  # pragma: no cover - thin shim
    """Module-level entry for `python -m xtrade.cli`."""
    app()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
