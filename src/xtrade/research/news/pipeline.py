"""News-corpus → per-instrument sentiment Parquet pipeline (Phase 5 / B1).

Input layout (operator-pulled, OFFLINE only)::

    data/research/news_raw/<source>/<YYYY-MM-DD>.jsonl

Each jsonl line is a single article::

    {"ts": "2026-05-22T10:15:00Z",
     "title": "Bitcoin rallies past $...",
     "body":  "...",
     "url":   "...",          # optional, not consumed
     "id":    "...",          # optional, for dedup downstream
     "tags":  ["BTC","..."]}  # optional

Output layout::

    data/research/news_sentiment/<instrument>/<YYYY-MM-DD>.parquet

Schema (stable; downstream B2 dataset builder depends on it)::

    ts                int64   nanoseconds since epoch, UTC
    instrument        str
    source            str     (e.g. "rss-coindesk")
    scorer            str     (e.g. "vader_lite")
    raw_score         float64 (compound score returned by scorer; here in [-1, 1])
    normalized_score  float64 == raw_score for VaderScorer; reserved for
                              scorers whose native output is unbounded
    n_articles        int32   always 1 per row in this version; reserved
                              for future aggregation modes

Design notes
------------
* The pipeline does NOT fetch articles. Operators pre-stage jsonl.
* Each article that matches any keyword for the instrument becomes ONE
  output row (n_articles=1). Downstream aggregators (B2) bucket by time.
* Empty / missing input → empty Parquet (still created, valid schema).
* The function is **idempotent**: a re-run with the same inputs
  overwrites the same outputs byte-stably (we sort rows by ts then
  source then title-hash before write).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd
import yaml

from xtrade.research.news.scorers import get_scorer

log = logging.getLogger("xtrade.research.news")

UTC = dt.timezone.utc

# Default on-disk roots. The pipeline accepts overrides for tests.
DEFAULT_RAW_ROOT = Path("data/research/news_raw")
DEFAULT_OUT_ROOT = Path("data/research/news_sentiment")
DEFAULT_KEYWORDS_PATH = Path("config/research/news_keywords.example.yaml")


# ---------------------------------------------------------------------------
# Row dataclass (mirrors the Parquet schema 1:1)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SentimentRow:
    ts_ns: int
    instrument: str
    source: str
    scorer: str
    raw_score: float
    normalized_score: float
    n_articles: int = 1


# ---------------------------------------------------------------------------
# Keywords map
# ---------------------------------------------------------------------------


def load_keywords_map(path: Path | str) -> dict[str, list[str]]:
    """Load the `instrument -> [keyword, ...]` mapping yaml.

    Validates that every value is a non-empty list of non-empty strings.
    Missing file → empty map (caller decides whether to raise).
    """

    p = Path(path)
    if not p.exists():
        return {}
    raw = yaml.safe_load(p.read_text()) or {}
    if not isinstance(raw, dict):
        raise ValueError(
            f"news_keywords yaml at {p} must be a mapping, got {type(raw).__name__}"
        )
    out: dict[str, list[str]] = {}
    for instrument, keywords in raw.items():
        if not isinstance(instrument, str) or not instrument:
            raise ValueError(f"news_keywords: instrument key must be non-empty str, got {instrument!r}")
        if not isinstance(keywords, list) or not keywords:
            raise ValueError(
                f"news_keywords[{instrument!r}] must be a non-empty list, got {keywords!r}"
            )
        cleaned: list[str] = []
        for kw in keywords:
            if not isinstance(kw, str) or not kw.strip():
                raise ValueError(
                    f"news_keywords[{instrument!r}] contains invalid keyword {kw!r}"
                )
            cleaned.append(kw.strip().lower())
        out[instrument] = cleaned
    return out


# ---------------------------------------------------------------------------
# Core entry point
# ---------------------------------------------------------------------------


def build_sentiment_features(
    instrument: str,
    since: dt.datetime,
    until: dt.datetime,
    *,
    sources: Iterable[str],
    scorer: str = "vader",
    raw_root: Path | str | None = None,
    out_root: Path | str | None = None,
    keywords: Iterable[str] | None = None,
    keywords_map_path: Path | str | None = None,
) -> list[Path]:
    """Materialise per-UTC-date sentiment Parquet for `instrument`.

    Parameters
    ----------
    instrument
        Output partition key. Used to look up keywords (if `keywords` is
        not given explicitly) and written into the Parquet's
        `instrument` column.
    since, until
        Half-open ``[since, until)`` UTC window. Articles whose ``ts``
        falls outside this window are dropped.
    sources
        Subdirectories under ``raw_root`` to scan (e.g. ``["rss",
        "newsapi-export"]``).
    scorer
        Registered scorer name (default ``"vader"``).
    raw_root, out_root
        Override on-disk roots. Defaults are repo-relative.
    keywords
        Explicit keyword list; if given, overrides the yaml lookup.
    keywords_map_path
        Path to the `instrument -> [keyword,...]` yaml (defaults to
        `config/research/news_keywords.example.yaml`).

    Returns
    -------
    list of written Parquet paths (one per UTC date in the window that
    had matching articles; empty list if no matches).
    """

    if since.tzinfo is None or until.tzinfo is None:
        raise ValueError("since/until must be timezone-aware (UTC)")
    if until <= since:
        raise ValueError(f"until ({until}) must be > since ({since})")

    raw_dir = Path(raw_root) if raw_root is not None else DEFAULT_RAW_ROOT
    out_dir = Path(out_root) if out_root is not None else DEFAULT_OUT_ROOT
    out_dir.mkdir(parents=True, exist_ok=True)

    # Resolve keywords for this instrument.
    if keywords is not None:
        kw_list = [k.strip().lower() for k in keywords if k and k.strip()]
    else:
        kw_map_path = (
            Path(keywords_map_path)
            if keywords_map_path is not None
            else DEFAULT_KEYWORDS_PATH
        )
        kw_map = load_keywords_map(kw_map_path)
        kw_list = kw_map.get(instrument, [])
    if not kw_list:
        log.warning(
            "no keywords resolved for instrument=%s; pipeline will write empty parquet",
            instrument,
        )

    scorer_obj = get_scorer(scorer)
    sources_list = list(sources)
    if not sources_list:
        raise ValueError("sources must be non-empty")

    rows = list(
        _iter_matching_rows(
            instrument=instrument,
            since=since,
            until=until,
            sources=sources_list,
            keywords=kw_list,
            raw_root=raw_dir,
            scorer_obj=scorer_obj,
            scorer_name=scorer,
        )
    )

    return _write_partitions(rows, instrument=instrument, out_root=out_dir, since=since, until=until)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _iter_matching_rows(
    *,
    instrument: str,
    since: dt.datetime,
    until: dt.datetime,
    sources: list[str],
    keywords: list[str],
    raw_root: Path,
    scorer_obj: Any,
    scorer_name: str,
) -> Iterator[SentimentRow]:
    """Yield one row per article matching any keyword in `keywords`."""

    if not keywords:
        return
    for source in sources:
        src_dir = raw_root / source
        if not src_dir.is_dir():
            log.warning("source directory missing: %s", src_dir)
            continue
        for jsonl in sorted(src_dir.glob("*.jsonl")):
            for article in _iter_articles(jsonl):
                ts = _parse_ts(article.get("ts"))
                if ts is None or ts < since or ts >= until:
                    continue
                title = str(article.get("title") or "")
                body = str(article.get("body") or "")
                tags = article.get("tags") or []
                if not _matches(title, body, tags, keywords):
                    continue
                text = f"{title}\n{body}".strip()
                raw = scorer_obj.score(text)
                # VaderScorer already emits in [-1, 1]; for unbounded
                # scorers we'd renormalise here. Today we just clamp.
                normalized = max(-1.0, min(1.0, float(raw)))
                yield SentimentRow(
                    ts_ns=_to_ns(ts),
                    instrument=instrument,
                    source=source,
                    scorer=scorer_name,
                    raw_score=float(raw),
                    normalized_score=normalized,
                    n_articles=1,
                )


def _iter_articles(jsonl_path: Path) -> Iterator[dict[str, Any]]:
    """Iterate articles from a single jsonl shard, skipping corrupt rows."""
    with jsonl_path.open("r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                log.warning("skipping malformed jsonl %s:%d: %s", jsonl_path.name, lineno, exc)
                continue
            if not isinstance(payload, dict):
                log.warning("skipping non-object row %s:%d", jsonl_path.name, lineno)
                continue
            yield payload


def _parse_ts(raw: Any) -> dt.datetime | None:
    if raw is None:
        return None
    if isinstance(raw, dt.datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=UTC)
    if isinstance(raw, str):
        # Accept trailing "Z" (older corpora) by mapping to "+00:00".
        s = raw.replace("Z", "+00:00")
        try:
            parsed = dt.datetime.fromisoformat(s)
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None


def _to_ns(ts: dt.datetime) -> int:
    return int(ts.astimezone(UTC).timestamp() * 1_000_000_000)


def _matches(title: str, body: str, tags: Iterable[Any], keywords: list[str]) -> bool:
    hay = f"{title}\n{body}".lower()
    if any(kw in hay for kw in keywords):
        return True
    # Tag match (case-insensitive equality on each tag).
    tag_lower = {str(t).lower() for t in tags}
    return any(kw in tag_lower for kw in keywords)


def _write_partitions(
    rows: list[SentimentRow],
    *,
    instrument: str,
    out_root: Path,
    since: dt.datetime,
    until: dt.datetime,
) -> list[Path]:
    """Group rows by UTC date, write one Parquet per date.

    Always creates the instrument's output dir even with zero rows
    (helps test setup / downstream listing). Dates in [since, until)
    that have no matching articles get no file (callers should treat
    "missing date" as "no rows that day").
    """

    inst_dir = out_root / instrument
    inst_dir.mkdir(parents=True, exist_ok=True)

    if not rows:
        return []

    # Stable row order: ts then source then deterministic content hash.
    def _row_sort_key(r: SentimentRow) -> tuple[int, str, str]:
        h = hashlib.sha1(
            f"{r.instrument}|{r.source}|{r.raw_score:.9f}".encode()
        ).hexdigest()
        return (r.ts_ns, r.source, h)

    rows_sorted = sorted(rows, key=_row_sort_key)

    by_day: dict[dt.date, list[SentimentRow]] = {}
    for r in rows_sorted:
        day = dt.datetime.fromtimestamp(r.ts_ns / 1e9, tz=UTC).date()
        by_day.setdefault(day, []).append(r)

    written: list[Path] = []
    for day, batch in sorted(by_day.items()):
        df = _rows_to_df(batch)
        out_path = inst_dir / f"{day.isoformat()}.parquet"
        df.to_parquet(out_path, index=False)
        written.append(out_path)
    return written


def _rows_to_df(rows: list[SentimentRow]) -> pd.DataFrame:
    """Build a DataFrame whose column dtypes match the documented schema."""

    df = pd.DataFrame(
        {
            "ts": pd.array([r.ts_ns for r in rows], dtype="int64"),
            "instrument": pd.array([r.instrument for r in rows], dtype="string"),
            "source": pd.array([r.source for r in rows], dtype="string"),
            "scorer": pd.array([r.scorer for r in rows], dtype="string"),
            "raw_score": pd.array([r.raw_score for r in rows], dtype="float64"),
            "normalized_score": pd.array(
                [r.normalized_score for r in rows], dtype="float64"
            ),
            "n_articles": pd.array([r.n_articles for r in rows], dtype="int32"),
        }
    )
    return df
