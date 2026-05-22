"""TradingNode factory.

Placeholder. Task 3 will implement:

    def build_testnet_node(venues_cfg: VenuesConfig) -> TradingNode: ...

The factory must:
  - Refuse to build if any client targets mainnet.
  - Default Binance USDT_FUTURES to `BinanceEnvironment.TESTNET` so the
    REST hits demo-fapi.binance.com while WS market stays on the
    reachable stream.binancefuture.com (see docs/phase0_results.md §C2).
  - Default Binance SPOT to TESTNET (testnet.binance.vision).
  - Set Hyperliquid to testnet=True; the adapter handles Unified Account
    automatically.
"""

from __future__ import annotations


def build_testnet_node():  # pragma: no cover - placeholder for Task 3
    raise NotImplementedError(
        "build_testnet_node is implemented in Phase 1 Task 3"
    )
