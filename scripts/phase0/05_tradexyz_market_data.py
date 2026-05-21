"""Task E — C4 (step 2): trade.xyz market data via NautilusTrader (MAINNET, READ-ONLY).

Connects to Hyperliquid **mainnet** using NautilusTrader's Hyperliquid
adapter and subscribes to live market data for 1–2 trade.xyz stock
perps. This script is hard-coded to be READ-ONLY: a safety guard at
the top forbids placing any order while `is_testnet=False`.

It depends on the dex name discovered by `04_discover_hyperliquid_perp_dexs.py`
(written to `docs/tradexyz_discovery.json`).

Symbol form follows Hyperliquid HIP-3 convention `dex:SYMBOL`, e.g.
`xyz:TSLA`. The exact representation used by NautilusTrader for HIP-3
instruments should be confirmed against the adapter docs:
  https://nautilustrader.io/docs/latest/integrations/hyperliquid/
If the adapter encodes the dex differently in `InstrumentId`, adjust
`_instrument_id_for` below.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import append_result, assert_not_mainnet_order, run_node_until, stepwise  # noqa: E402


CHECK_ID = "C4b"
CHECK_NAME = "trade.xyz market data via NautilusTrader (mainnet read-only)"

DISCOVERY_PATH = Path(__file__).resolve().parents[2] / "docs" / "tradexyz_discovery.json"
PREFERRED_TICKERS = ("TSLA", "NVDA", "MSTR", "COIN", "AAPL")
DEFAULT_TIMEOUT_S = 60


def _instrument_id_for(prefixed_symbol: str):
    """Build the NautilusTrader InstrumentId for a HIP-3 perp.

    Hyperliquid's API returns symbols in `dex:SYMBOL` form (e.g.
    `xyz:TSLA`), but the NautilusTrader Hyperliquid adapter encodes
    them as `dex:SYMBOL-USD-PERP` (e.g. `xyz:TSLA-USD-PERP`). We
    construct that exact form here.
    """
    from nautilus_trader.model.identifiers import InstrumentId, Symbol, Venue

    return InstrumentId(Symbol(f"{prefixed_symbol}-USD-PERP"), Venue("HYPERLIQUID"))


async def _run() -> list[str]:
    notes: list[str] = []

    # Mainnet + read-only invariant.
    assert_not_mainnet_order(is_mainnet=True, will_place_order=False)
    notes.append("safety guard: mainnet read-only confirmed; no orders will be placed")

    if not DISCOVERY_PATH.exists():
        raise RuntimeError(
            f"{DISCOVERY_PATH} not found. Run "
            "`scripts/phase0/04_discover_hyperliquid_perp_dexs.py` first."
        )
    discovery = json.loads(DISCOVERY_PATH.read_text(encoding="utf-8"))
    dex = discovery["dex"]
    universe = discovery.get("universe", [])
    notes.append(f"using dex='{dex}', universe size={len(universe)}")

    # `universe` entries come back already in `dex:SYMBOL` form, e.g. 'xyz:TSLA'.
    # Pick our preferred tickers if present; otherwise fall back to the first
    # two non-numeric symbols.
    universe_set = set(universe)
    prefixed_preferred = [f"{dex}:{t}" for t in PREFERRED_TICKERS]
    selected = [s for s in prefixed_preferred if s in universe_set][:2]
    if not selected:
        selected = [
            s for s in universe
            if ":" in s and s.split(":", 1)[-1].isalpha()
        ][:2]
    if not selected:
        raise RuntimeError("No suitable stock tickers found in trade.xyz universe")
    notes.append(f"subscribing symbols: {selected}")

    from nautilus_trader.adapters.hyperliquid.config import HyperliquidDataClientConfig
    from nautilus_trader.adapters.hyperliquid.enums import HyperliquidProductType
    from nautilus_trader.adapters.hyperliquid.factories import HyperliquidLiveDataClientFactory
    from nautilus_trader.core.nautilus_pyo3.hyperliquid import HyperliquidEnvironment
    from nautilus_trader.config import (
        InstrumentProviderConfig,
        LiveDataEngineConfig,
        LoggingConfig,
        TradingNodeConfig,
    )
    from nautilus_trader.live.node import TradingNode
    from nautilus_trader.trading.strategy import Strategy, StrategyConfig

    class ReadOnlyConfig(StrategyConfig, frozen=True):
        instrument_ids: tuple
        timeout_s: float = DEFAULT_TIMEOUT_S

    class ReadOnlyMD(Strategy):
        def __init__(self, config: ReadOnlyConfig) -> None:
            super().__init__(config)
            self.events: list[str] = []
            self._first_ticks: dict = {}
            self._done = asyncio.Event()

        def on_start(self) -> None:
            for iid in self.config.instrument_ids:
                self.subscribe_quote_ticks(iid)
                self.subscribe_trade_ticks(iid)
                # Mark price / funding subscriptions may have adapter-
                # specific helpers; data also surfaces via custom data.
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
            if len(self._first_ticks) >= len(self.config.instrument_ids):
                self._done.set()

    instrument_ids = tuple(_instrument_id_for(s) for s in selected)

    instr_provider = InstrumentProviderConfig(load_all=True)
    # Mainnet data client — NO exec client, NO credentials required.
    # HIP-3 perps (trade.xyz markets) are NOT loaded by default; we must
    # explicitly request the PERP_HIP3 product type.
    data_cfg = HyperliquidDataClientConfig(
        environment=HyperliquidEnvironment.MAINNET,
        instrument_provider=instr_provider,
        product_types=(HyperliquidProductType.PERP_HIP3,),
    )
    node_cfg = TradingNodeConfig(
        trader_id="PHASE0-HL-RO-001",
        logging=LoggingConfig(log_level="INFO"),
        data_engine=LiveDataEngineConfig(),
        data_clients={"HYPERLIQUID": data_cfg},
        exec_clients={},
    )
    node = TradingNode(config=node_cfg)
    node.add_data_client_factory("HYPERLIQUID", HyperliquidLiveDataClientFactory)
    strategy = ReadOnlyMD(
        config=ReadOnlyConfig(instrument_ids=instrument_ids, timeout_s=DEFAULT_TIMEOUT_S),
    )
    node.trader.add_strategy(strategy)
    node.build()

    try:
        await run_node_until(node, strategy._done, timeout_s=DEFAULT_TIMEOUT_S + 30)
        if not strategy._first_ticks:
            raise RuntimeError(
                "No quote ticks received for any trade.xyz ticker. "
                "Verify the HIP-3 symbol convention used by your "
                "NautilusTrader Hyperliquid adapter version."
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
