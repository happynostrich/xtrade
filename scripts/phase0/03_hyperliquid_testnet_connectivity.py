"""Task C — C3: Hyperliquid Testnet connectivity.

Validates against Hyperliquid testnet using NautilusTrader's Hyperliquid
adapter:

  1. Subscribe to a standard (non-HIP-3) crypto perp's market data
     (default: BTC perp).
  2. Place a far-from-market post-only limit order.
  3. Cancel that order.

All actions target **testnet**. Phase 0 forbids mainnet orders.

The exact field names of `HyperliquidDataClientConfig` /
`HyperliquidExecClientConfig` may evolve with NautilusTrader releases.
The strategy itself is identical in shape to the Binance probe in
`02_binance_testnet_connectivity.py`.
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
from xtrade.config import load_hyperliquid_testnet, MissingCredentialError  # noqa: E402


CHECK_ID = "C3"
CHECK_NAME = "Hyperliquid testnet connectivity"

# Hyperliquid standard perp symbol convention in Nautilus uses a `-PERP`
# suffix similar to Binance USDT-M. If the convention differs in your
# installed Nautilus version, adjust accordingly.
DEFAULT_PERP_SYMBOL = "BTC-USD-PERP"
DEFAULT_TIMEOUT_S = 60


async def _run() -> list[str]:
    notes: list[str] = []

    creds = load_hyperliquid_testnet()

    from nautilus_trader.adapters.hyperliquid.config import (
        HyperliquidDataClientConfig,
        HyperliquidExecClientConfig,
    )
    from nautilus_trader.adapters.hyperliquid.factories import (
        HyperliquidLiveDataClientFactory,
        HyperliquidLiveExecClientFactory,
    )
    from nautilus_trader.core.nautilus_pyo3.hyperliquid import HyperliquidEnvironment
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
    from nautilus_trader.trading.strategy import Strategy, StrategyConfig

    venue = Venue("HYPERLIQUID")
    instrument_id = InstrumentId(Symbol(DEFAULT_PERP_SYMBOL), venue)

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
            self.events.append("subscribed quotes/trades")
            self.log.info(self.events[-1])
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
            target = Decimal(str(ref_price)) * Decimal("0.5")
            price = instr.make_price(target)
            qty = instr.make_qty(Decimal("0.001"))
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

    instr_provider = InstrumentProviderConfig(load_all=True)
    data_cfg = HyperliquidDataClientConfig(
        environment=HyperliquidEnvironment.TESTNET,
        instrument_provider=instr_provider,
    )
    exec_cfg = HyperliquidExecClientConfig(
        account_address=creds.account_address,
        private_key=creds.api_wallet_key,
        vault_address=creds.vault_address,
        environment=HyperliquidEnvironment.TESTNET,
        instrument_provider=instr_provider,
    )

    node_cfg = TradingNodeConfig(
        trader_id="PHASE0-HL-001",
        logging=LoggingConfig(log_level="INFO"),
        data_engine=LiveDataEngineConfig(),
        exec_engine=LiveExecEngineConfig(),
        data_clients={"HYPERLIQUID": data_cfg},
        exec_clients={"HYPERLIQUID": exec_cfg},
    )

    node = TradingNode(config=node_cfg)
    node.add_data_client_factory("HYPERLIQUID", HyperliquidLiveDataClientFactory)
    node.add_exec_client_factory("HYPERLIQUID", HyperliquidLiveExecClientFactory)

    strategy = ProbeStrategy(
        config=ProbeConfig(instrument_id=instrument_id, timeout_s=DEFAULT_TIMEOUT_S),
    )
    node.trader.add_strategy(strategy)

    node.build()
    try:
        await run_node_until(node, strategy._done, timeout_s=DEFAULT_TIMEOUT_S + 30)

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
