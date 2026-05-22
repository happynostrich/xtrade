"""TradingNode health probes.

Placeholder. Task 3 will implement a structured probe that:
  - Builds the node (via `factory.build_testnet_node`).
  - Subscribes a small set of instruments per venue.
  - Awaits first quote/trade on each channel within a timeout.
  - Returns a dict of {venue: {instrument: {first_quote_ts, latency_ms}}}.
  - Writes a JSON summary to logs/<run-id>/health.json.

The 1.227 quirk noted in Phase 0: `cache = node.cache` (not
`node.trader.cache`). Pin once at the top of the probe.
"""

from __future__ import annotations


def probe():  # pragma: no cover - placeholder for Task 3
    raise NotImplementedError("health.probe is implemented in Phase 1 Task 3")
