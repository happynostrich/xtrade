"""Phase 5 / B2 — feature & label dataset builder for the ML baseline.

Public surface:

    from xtrade.research.dataset import (
        DatasetBundle, build_dataset, load_ohlcv_dataframe,
    )

This subpackage is research-only and import-isolated from the live
supervisor (see `tests/test_research_import_isolation.py`).
"""

from __future__ import annotations

from xtrade.research.dataset.build import (
    DatasetBundle,
    build_dataset,
    load_ohlcv_dataframe,
    load_sentiment_dataframe,
)

__all__ = [
    "DatasetBundle",
    "build_dataset",
    "load_ohlcv_dataframe",
    "load_sentiment_dataframe",
]
