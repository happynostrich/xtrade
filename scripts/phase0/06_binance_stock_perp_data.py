"""Task F — C5: Binance MAINNET read-only market data for a US-equity perp.

Connects to Binance USDT-M Futures **mainnet** with NO API credentials
and subscribes to live quote/trade data for an equity-style perpetual
(default candidates: MSTR, COIN). This script is hard-locked to
read-only: a safety guard at the top forbids placing any order while
`testnet=False`.

Binance's USDT-M Futures public market data does not require API keys,
but if the adapter requires them anyway, ensure the keys provided have
NO trading permission (read-only) and re-run.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import append_result, assert_not_mainnet_order, run_node_until, stepwise  # noqa: E402


CHECK_ID = "C5"
CHECK_NAME = "Binance mainnet US-equity perp market data (read-only)"

# Try these in order. Symbol convention in Nautilus' Binance adapter is
# `<BASE><QUOTE>-PERP` for USDT-M perps.
CANDIDATE_SYMBOLS = ("MSTRUSDT-PERP", "COINUSDT-PERP")
DEFAULT_TIMEOUT_S = 60


async def _run() -> list[str]:
    notes: list[str] = []
    assert_not_mainnet_order(is_mainnet=True, will_place_order=False)
    notes.append("safety guard: mainnet read-only confirmed; no orders will be placed")

    from nautilus_trader.adapters.binance.common.enums import (
        BinanceAccountType,
        BinanceEnvironment,
    )
    from nautilus_trader.adapters.binance.config import BinanceDataClientConfig
    from nautilus_trader.adapters.binance.factories import BinanceLiveDataClientFactory
    from nautilus_trader.config import (
        InstrumentProviderConfig,
        LiveDataEngineConfig,
        LoggingConfig,
        TradingNodeConfig,
    )
    from nautilus_trader.live.node import TradingNode
    from nautilus_trader.model.identifiers import InstrumentId, Symbol, Venue
    from nautilus_trader.trading.strategy import Strategy, StrategyConfig

    venue = Venue("BINANCE")
    instrument_ids = tuple(InstrumentId(Symbol(s), venue) for s in CANDIDATE_SYMBOLS)

    class ROConfig(StrategyConfig, frozen=True):
        instrument_ids: tuple
        timeout_s: float = DEFAULT_TIMEOUT_S

    class RO(Strategy):
        def __init__(self, config: ROConfig) -> None:
            super().__init__(config)
            self.events: list[str] = []
            self._first_ticks: dict = {}
            self._done = asyncio.Event()

        def on_start(self) -> None:
            for iid in self.config.instrument_ids:
                self.subscribe_quote_ticks(iid)
                self.events.append(f"subscribed {iid}")
                self.log.info(self.events[-1])
            self.clock.set_time_alert_ns(
                "give-up",
                self.clock.timestamp_ns() + int(self.config.timeout_s * 1e9),
                lambda _e: self._done.set(),
            )

        def on_quote_tick(self, tick) -> None:
            key = str(tick.instrument_id)
            if key not in self._first_ticks:
                self._first_ticks[key] = (tick.bid_price, tick.ask_price)
                self.events.append(
                    f"first quote {key}: bid={tick.bid_price} ask={tick.ask_price}"
                )
                self.log.info(self.events[-1])
            if self._first_ticks:
                # As soon as we get at least one symbol's data, declare success.
                self._done.set()

    data_cfg = BinanceDataClientConfig(
        account_type=BinanceAccountType.USDT_FUTURES,
        environment=BinanceEnvironment.LIVE,
        instrument_provider=InstrumentProviderConfig(load_all=True),
    )

    node_cfg = TradingNodeConfig(
        trader_id="PHASE0-BNC-RO-001",
        logging=LoggingConfig(log_level="INFO"),
        data_engine=LiveDataEngineConfig(),
        data_clients={"BINANCE": data_cfg},
        exec_clients={},
    )
    node = TradingNode(config=node_cfg)
    node.add_data_client_factory("BINANCE", BinanceLiveDataClientFactory)
    strategy = RO(config=ROConfig(instrument_ids=instrument_ids, timeout_s=DEFAULT_TIMEOUT_S))
    node.trader.add_strategy(strategy)
    node.build()

    try:
        await run_node_until(node, strategy._done, timeout_s=DEFAULT_TIMEOUT_S + 30)
        if not strategy._first_ticks:
            raise RuntimeError(
                "No quotes received for any candidate equity perp. The "
                "exact contract symbols may have changed; inspect "
                "https://www.binance.com/en/futures and update "
                "CANDIDATE_SYMBOLS, then re-run."
            )
        notes.extend(strategy.events)
    finally:
        pass

    return notes, node


def main() -> int:
    with stepwise(CHECK_ID, CHECK_NAME):
        notes, node = asyncio.run(_run())
        try:
            node.dispose()
        except Exception:  # noqa: BLE001
            pass
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
