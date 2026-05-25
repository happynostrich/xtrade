"""News-article sentiment feature pipeline (Phase 5 Track B / B1).

This subpackage is **research-only**: it reads pre-pulled news corpora from
``data/research/news_raw/`` and writes per-instrument sentiment Parquet
to ``data/research/news_sentiment/``. It does NOT call any external HTTP
service — operators arrange the raw jsonl ingestion via a separate
batch job (cron / one-shot script).

Import isolation
----------------
This module must NEVER be imported by the live supervisor / signal_runner
code paths (`xtrade.live.*`). The guard test in
`tests/test_research_import_isolation.py` enforces this — the live VPS
process does not need pandas-Parquet writers or sentiment lexicons.
"""

from __future__ import annotations

from xtrade.research.news.pipeline import (
    SentimentRow,
    build_sentiment_features,
    load_keywords_map,
)
from xtrade.research.news.scorers import (
    Scorer,
    VaderScorer,
    available_scorers,
    get_scorer,
)

__all__ = [
    "Scorer",
    "SentimentRow",
    "VaderScorer",
    "available_scorers",
    "build_sentiment_features",
    "get_scorer",
    "load_keywords_map",
]
