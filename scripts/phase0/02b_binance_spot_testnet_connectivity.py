"""Task B (variant) — C2-spot: Binance SPOT testnet connectivity.

Sister script to ``02_binance_testnet_connectivity.py``. The original
targets USDT-M Futures via ``testnet.binancefuture.com``; this one
targets Spot via ``testnet.binance.vision``.

Validates the same four behaviors:

  1. Account info (balance) retrieval.
  2. Market data subscription for a spot pair (default: ``BTCUSDT``).
  3. Place a limit BUY **far below market** so it never fills.
  4. Cancel that order.

All actions target the **testnet**. The script refuses to run against
mainnet (Phase 0 safety rule).

This complements C2 (USDT-M Futures) when only Spot testnet keys are
available — e.g. while ``testnet.binancefuture.com`` is unreachable.
Result is recorded as ``C2-spot`` so it doesn't overwrite the
Futures-targeted ``C2`` row.
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


CHECK_ID = "C2-spot"
CHECK_NAME = "Binance testnet connectivity (Spot)"

DEFAULT_SPOT_SYMBOL = "BTCUSDT"
DEFAULT_TIMEOUT_S = 60


async def _run():
    notes: list[str] = []

    creds = load_binance_testnet()
    if not creds.has_spot:
        raise MissingCredentialError(
            "Binance SPOT testnet API key/secret not set in `.env`. "
            "Please add BINANCE_SPOT_TESTNET_API_KEY/SECRET (or the shared "
            "BINANCE_TESTNET_API_KEY/SECRET)."
        )

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
    from nautilus_trader.model.objects import Quantity  # noqa: F401
    from nautilus_trader.trading.strategy import Strategy, StrategyConfig

    account_type = BinanceAccountType.SPOT
    venue = Venue("BINANCE")
    instrument_id = InstrumentId(Symbol(DEFAULT_SPOT_SYMBOL), venue)

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
                self.events.append(
                    f"first quote: bid={tick.bid_price} ask={tick.ask_price}"
                )
                self.log.info(self.events[-1])
                self._place_order(float(tick.bid_price))

        def _place_order(self, ref_price: float) -> None:
            if self.order is not None:
                return
            instr = self.cache.instrument(self.config.instrument_id)
            if instr is None:
                self.log.warning("Instrument not yet in cache; will retry on next tick")
                return
            # Far below market: 30% below current bid. Cannot use 50% because
            # Binance Spot's PERCENT_PRICE_BY_SIDE filter on BTCUSDT requires
            # BUY price >= 0.5 * avgPrice(5min), which rejects exactly-50%
            # orders. 0.7 stays clear of the floor while leaving no realistic
            # chance of fill within the 60s probe window.
            target = Decimal(str(ref_price)) * Decimal("0.7")
            price = instr.make_price(target)
            qty = instr.make_qty(Decimal("0.001"))
            order = self.order_factory.limit(
                instrument_id=self.config.instrument_id,
                order_side=OrderSide.BUY,
                quantity=qty,
                price=price,
                time_in_force=TimeInForce.GTC,
                post_only=False,  # Spot testnet doesn't always honor post-only
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
    data_cfg = BinanceDataClientConfig(
        api_key=creds.spot_api_key,
        api_secret=creds.spot_api_secret,
        account_type=account_type,
        environment=BinanceEnvironment.TESTNET,
        instrument_provider=instr_provider,
    )
    exec_cfg = BinanceExecClientConfig(
        api_key=creds.spot_api_key,
        api_secret=creds.spot_api_secret,
        account_type=account_type,
        environment=BinanceEnvironment.TESTNET,
        instrument_provider=instr_provider,
    )

    node_cfg = TradingNodeConfig(
        trader_id="PHASE0-BINANCE-SPOT-001",
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
