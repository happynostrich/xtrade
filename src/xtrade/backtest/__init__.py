"""xtrade.backtest — catalog-driven BacktestEngine runner.

Public surface (Phase 1 Task 5):
  - runner.run_backtest(...): load bars from a ParquetDataCatalog,
    spin up a BacktestEngine for the instrument's venue, drive a
    DemoEmaCross (or compatible XtradeStrategy), and return a
    structured summary written to `logs/<run_id>/summary.json`.
"""

__all__: list[str] = []
