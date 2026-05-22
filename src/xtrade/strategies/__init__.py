"""xtrade.strategies — shared strategy base + sample strategies.

Phase 1 Task 5:
  - base.XtradeStrategy / XtradeStrategyConfig: thin Nautilus `Strategy`
    subclass exposing `self.mode` ("backtest" | "live") so a single
    subclass runs in either context (P6).
  - demo_ema.DemoEmaCross / DemoEmaCrossConfig: reusable form of the
    C6b EMA-cross.
"""

__all__: list[str] = []
