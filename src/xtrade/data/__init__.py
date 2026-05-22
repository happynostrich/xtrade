"""xtrade.data — historical data ingestion and ParquetDataCatalog access.

Public surface (Phase 1 Task 4):

  - catalog: open_catalog, write_bars, read_bars, parse_bar_spec,
             bar_type_for, missing_intervals, intervals_for,
             default_catalog_path
  - instruments: resolve(venue, symbol, **kwargs)
  - binance_klines: fetch_klines_df, klines_df_to_bars,
                    fetch_bars, fetch_bars_chunks
  - hyperliquid_hip3: fetch_candles_df, candles_df_to_bars,
                      fetch_bars, fetch_bars_chunks
"""

__all__: list[str] = []
