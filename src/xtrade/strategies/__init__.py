"""xtrade.strategies — shared strategy base + sample strategies.

Phase 1 Task 5 will populate:
  - base.XtradeStrategy: thin Nautilus `Strategy` subclass exposing
    `self.mode` ("backtest" | "live") so a single subclass can run in
    either context per the Phase 1 brief's "one strategy, two modes"
    requirement (P6).
  - demo_ema.DemoEmaCross: reusable form of the C6b EMA-cross strategy.
"""

__all__: list[str] = []
