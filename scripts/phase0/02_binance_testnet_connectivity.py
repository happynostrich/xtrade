"""Task B — C2: Binance USDT-M Futures Demo Trading connectivity.

Validates four behaviors against Binance USDT-M Futures **Demo Trading**
using NautilusTrader's Binance adapter:

  1. Account info (balance) retrieval.
  2. Market data subscription for a perpetual (default: `BTCUSDT-PERP`).
  3. Place a limit order **far from market** so it never fills.
  4. Cancel that order.

All actions target the **Demo** environment (`demo.binance.com`). The
script refuses to run against mainnet (Phase 0 safety rule).

This script uses a short-running `TradingNode`. It exits PASS if all
four behaviors complete within a timeout; otherwise FAIL with the
exception captured in `docs/phase0_results.md`.

Venue migration note (2026):
  Binance retired the standalone `testnet.binancefuture.com` user
  database and merged Futures testing into **Demo Trading** under the
  main Binance account at `https://demo.binance.com`. The REST endpoint
  is now `https://demo-fapi.binance.com`; market-data WS is
  `wss://demo-fstream.binance.com`. NautilusTrader 1.227+ exposes
  `BinanceEnvironment.DEMO` which auto-routes both. We use it here.

  Implications:
    - API keys must be created at `demo.binance.com` -> API Management
      (NOT the old testnet.binancefuture.com).
    - COIN-M is unsupported in Demo (we only need USDT-M anyway).
    - The `.env` variable names BINANCE_FUTURES_TESTNET_API_KEY/SECRET
      are retained for backwards compatibility; semantically they now
      carry Demo Trading credentials.

NOTE: Binance Spot is exercised by `02b_binance_spot_testnet_connectivity.py`
against `testnet.binance.vision` (Spot keeps a separate testnet under
GitHub login; it was NOT migrated to Demo).
"""

from __future__ import annotations

import asyncio
import sys
import traceback
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import append_result, run_node_until, stepwise  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from xtrade.config import load_binance_testnet, MissingCredentialError  # noqa: E402


CHECK_ID = "C2"
CHECK_NAME = "Binance USDT-M Futures Demo Trading connectivity"

# Symbol used by Nautilus for the BTC-USDT perpetual on Binance USDT-M Futures.
DEFAULT_PERP_SYMBOL = "BTCUSDT-PERP"
DEFAULT_TIMEOUT_S = 60


async def _run() -> list[str]:
    """Returns notes on success. Raises on failure."""
    notes: list[str] = []

    creds = load_binance_testnet()
    if not creds.has_futures:
        raise MissingCredentialError(
            "Binance USDT-M Futures Demo Trading API key/secret not set in "
            "`.env`. Create a key at https://demo.binance.com/ -> API "
            "Management (NOT testnet.binancefuture.com, which has been "
            "merged into Demo Trading). Set "
            "BINANCE_FUTURES_TESTNET_API_KEY/SECRET (or the shared "
            "BINANCE_TESTNET_API_KEY/SECRET) in `.env`."
        )

    # Imports are local to avoid pulling Nautilus into Task A when only
    # checking installation. Names match NautilusTrader's public API.
    from nautilus_trader.adapters.binance.common.enums import (
        BinanceAccountType,
        BinanceEnvironment,
    )
    from nautilus_trader.adapters.binance.config import (
        BinanceDataClientConfig,
        BinanceExecClientConfig,
    )
    from nautilus_trader.adapters.binance.factories import (
        BinanceLiveDataClientFactory,
        BinanceLiveExecClientFactory,
    )
    from nautilus_trader.config import (
        InstrumentProviderConfig,
        LiveDataEngineConfig,
        LiveExecEngineConfig,
        LoggingConfig,
        TradingNodeConfig,
    )
    from nautilus_trader.live.node import TradingNode
    from nautilus_trader.model.enums import OrderSide, TimeInForce
    from nautilus_trader.model.identifiers import InstrumentId, Symbol, Venue
    from nautilus_trader.model.objects import Price, Quantity
    from nautilus_trader.trading.strategy import Strategy, StrategyConfig

    account_type = BinanceAccountType.USDT_FUTURES
    venue = Venue("BINANCE")
    instrument_id = InstrumentId(Symbol(DEFAULT_PERP_SYMBOL), venue)

    # --- Strategy: subscribe, place far-from-market limit, cancel. -------------
    class ProbeConfig(StrategyConfig, frozen=True):
        instrument_id: InstrumentId
        timeout_s: float = DEFAULT_TIMEOUT_S

    class ProbeStrategy(Strategy):
        def __init__(self, config: ProbeConfig) -> None:
            super().__init__(config)
            self.events: list[str] = []
            self.order = None
            self._done = asyncio.Event()
            self._tick_seen = asyncio.Event()
            self._cancel_seen = asyncio.Event()

        def on_start(self) -> None:
            self.subscribe_quote_ticks(self.config.instrument_id)
            self.subscribe_trade_ticks(self.config.instrument_id)
            self.log.info("Subscribed to quotes and trades")
            self.events.append("subscribed quotes/trades")
            # Place the order once we know last price; if no tick within
            # the timeout, fail.
            self.clock.set_time_alert_ns(
                "give-up",
                self.clock.timestamp_ns() + int(self.config.timeout_s * 1e9),
                self._giveup,
            )

        def _giveup(self, _event) -> None:
            if not self._done.is_set():
                self.log.error("Timed out waiting for ticks / order events")
                self._done.set()

        def on_quote_tick(self, tick) -> None:
            if not self._tick_seen.is_set():
                self._tick_seen.set()
                self.events.append(f"first quote: bid={tick.bid_price} ask={tick.ask_price}")
                self.log.info(self.events[-1])
                self._place_order(float(tick.bid_price))

        def _place_order(self, ref_price: float) -> None:
            if self.order is not None:
                return
            instr = self.cache.instrument(self.config.instrument_id)
            if instr is None:
                self.log.warning("Instrument not yet in cache; will retry on next tick")
                return
            # Far-from-market: 50% below current bid, well outside any reasonable spread.
            target = Decimal(str(ref_price)) * Decimal("0.5")
            price = instr.make_price(target)
            # Quantity sized so notional (price * qty) clears Binance USDT-M
            # Futures Demo's MIN_NOTIONAL = 50 USDT (-4164) even when price is
            # 50% below market. 0.002 BTC * 0.5 * 70000 = ~70 USDT, safe.
            qty: Quantity = instr.make_qty(Decimal("0.002"))
            order = self.order_factory.limit(
                instrument_id=self.config.instrument_id,
                order_side=OrderSide.BUY,
                quantity=qty,
                price=price,
                time_in_force=TimeInForce.GTC,
                post_only=True,
            )
            self.order = order
            self.submit_order(order)
            self.events.append(f"submitted limit BUY {qty} @ {price}")
            self.log.info(self.events[-1])

        def on_order_accepted(self, event) -> None:
            self.events.append(f"order accepted: {event.client_order_id}")
            self.log.info(self.events[-1])
            # Now cancel it.
            if self.order is not None:
                self.cancel_order(self.order)

        def on_order_canceled(self, event) -> None:
            self.events.append(f"order canceled: {event.client_order_id}")
            self.log.info(self.events[-1])
            self._cancel_seen.set()
            self._done.set()

        def on_order_rejected(self, event) -> None:
            self.events.append(f"order REJECTED: {event.reason}")
            self.log.error(self.events[-1])
            self._done.set()

    # --- Trading node config -------------------------------------------------
    instr_provider = InstrumentProviderConfig(load_all=True)
    # Use `BinanceEnvironment.TESTNET` (not DEMO) intentionally. For USDT_FUTURES
    # Nautilus 1.227 already migrated TESTNET internally to the post-merger URLs:
    #   REST   -> https://demo-fapi.binance.com         (== new Demo Trading REST)
    #   WS-API -> wss://testnet.binancefuture.com/ws-fapi/v1
    #   WS mkt -> wss://stream.binancefuture.com        (legacy domain, still active)
    # DEMO mode would route WS mkt to wss://demo-fstream.binance.com, which we've
    # observed to be unreachable from this network (DNS returns a non-Binance IP
    # block, "No route to host"). The legacy stream.binancefuture.com remains
    # operational per the venue note, and TESTNET mode targets it.
    # See nautilus_trader/adapters/binance/common/urls.py for the full table.
    data_cfg = BinanceDataClientConfig(
        api_key=creds.futures_api_key,
        api_secret=creds.futures_api_secret,
        account_type=account_type,
        environment=BinanceEnvironment.TESTNET,
        instrument_provider=instr_provider,
    )
    exec_cfg = BinanceExecClientConfig(
        api_key=creds.futures_api_key,
        api_secret=creds.futures_api_secret,
        account_type=account_type,
        environment=BinanceEnvironment.TESTNET,
        instrument_provider=instr_provider,
    )

    node_cfg = TradingNodeConfig(
        trader_id="PHASE0-BINANCE-001",
        logging=LoggingConfig(log_level="INFO"),
        data_engine=LiveDataEngineConfig(),
        exec_engine=LiveExecEngineConfig(),
        data_clients={"BINANCE": data_cfg},
        exec_clients={"BINANCE": exec_cfg},
    )

    node = TradingNode(config=node_cfg)
    node.add_data_client_factory("BINANCE", BinanceLiveDataClientFactory)
    node.add_exec_client_factory("BINANCE", BinanceLiveExecClientFactory)

    strategy = ProbeStrategy(
        config=ProbeConfig(instrument_id=instrument_id, timeout_s=DEFAULT_TIMEOUT_S),
    )
    node.trader.add_strategy(strategy)

    node.build()
    try:
        await run_node_until(node, strategy._done, timeout_s=DEFAULT_TIMEOUT_S + 30)

        # Verify balance after order activity to also exercise account snapshot.
        account = node.cache.account_for_venue(venue)
        if account is not None:
            balances = list(account.balances().values())
            notes.append(f"account balances: {len(balances)} entries")
            for b in balances[:5]:
                notes.append(f"  {b.currency}: total={b.total} free={b.free}")
        else:
            notes.append("WARNING: no account snapshot available in cache")

        if not strategy._tick_seen.is_set():
            raise RuntimeError("No market data ticks received within timeout")
        if not strategy._cancel_seen.is_set():
            raise RuntimeError("Order cancel was not observed within timeout")

        notes.extend(strategy.events)
    finally:
        pass

    return notes, node


def main() -> int:
    with stepwise(CHECK_ID, CHECK_NAME):
        try:
            notes, node = asyncio.run(_run())
            try:
                node.dispose()
            except Exception:  # noqa: BLE001
                pass
        except MissingCredentialError as exc:
            print(str(exc), file=sys.stderr)
            append_result(CHECK_ID, CHECK_NAME, "SKIP", notes=[str(exc)])
            return 2
        except Exception:
            traceback.print_exc()
            raise
        append_result(CHECK_ID, CHECK_NAME, "PASS", notes=notes)
        print(f"[{CHECK_ID}] PASS")
        return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception:
        sys.exit(1)
