"""Live TradingNode runners (Phase 1 Task 6 / P5).

`xtrade.live.runner.run_live` drives `xtrade.node.factory.build_testnet_node`
plus a strategy through the full live order path on testnet. Mainnet
is hard-refused via the factory guard (Phase 1 brief §6).
"""
