"""Tests for `xtrade.research.news` (Phase 5 / B1).

Covers:
  * Lexicon scorer determinism + range bounds.
  * Negation handling (a negator within the window flips sign).
  * `build_sentiment_features` end-to-end: jsonl → Parquet, schema lock,
    date partitioning, time-window filter, keyword filter, empty corpus.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

pytest.importorskip("pyarrow")
import pandas as pd

from xtrade.research.news import (
    VaderScorer,
    available_scorers,
    build_sentiment_features,
    get_scorer,
    load_keywords_map,
)


UTC = dt.timezone.utc


# ---------- scorer ----------------------------------------------------------


def test_vader_scorer_range_and_determinism() -> None:
    s = VaderScorer()
    # Bullish text → positive.
    pos = s.score("Bitcoin rallies as bulls dominate; surge continues.")
    assert 0.0 < pos <= 1.0
    # Bearish text → negative.
    neg = s.score("Crash and panic as bears slump the market.")
    assert -1.0 <= neg < 0.0
    # Empty / no lexicon hits → 0.
    assert s.score("") == 0.0
    assert s.score("a generic sentence about nothing relevant here") == 0.0
    # Determinism.
    assert s.score("Bitcoin rallies") == s.score("Bitcoin rallies")


def test_vader_scorer_negation_flips_sign() -> None:
    s = VaderScorer()
    pos = s.score("the market is bullish today")
    neg = s.score("the market is not bullish today")
    assert pos > 0
    assert neg < 0


def test_get_scorer_registry_has_vader_alias() -> None:
    names = available_scorers()
    assert "vader_lite" in names
    assert "vader" in names
    a = get_scorer("vader")
    b = get_scorer("vader_lite")
    text = "Bitcoin rallies past resistance"
    assert a.score(text) == b.score(text)


def test_get_scorer_unknown_raises() -> None:
    with pytest.raises(ValueError, match="unknown scorer"):
        get_scorer("nope-not-a-real-scorer")


# ---------- keywords map ----------------------------------------------------


def test_load_keywords_map_empty_when_missing(tmp_path: Path) -> None:
    assert load_keywords_map(tmp_path / "missing.yaml") == {}


def test_load_keywords_map_rejects_invalid(tmp_path: Path) -> None:
    p = tmp_path / "bad.yaml"
    p.write_text("FOO.BAR: []\n")  # empty list rejected
    with pytest.raises(ValueError):
        load_keywords_map(p)


def test_load_keywords_map_normalises(tmp_path: Path) -> None:
    p = tmp_path / "kw.yaml"
    p.write_text(
        "BTCUSDT-PERP.BINANCE:\n"
        "  - BTC\n"
        "  - bitcoin\n"
        "  - \"Bitcoin \"\n"
    )
    out = load_keywords_map(p)
    assert out["BTCUSDT-PERP.BINANCE"] == ["btc", "bitcoin", "bitcoin"]


# ---------- pipeline end-to-end --------------------------------------------


SCHEMA_COLS = (
    "ts",
    "instrument",
    "source",
    "scorer",
    "raw_score",
    "normalized_score",
    "n_articles",
)


def _write_jsonl(p: Path, articles: list[dict]) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(json.dumps(a) for a in articles) + "\n")


def test_build_sentiment_features_writes_schema_locked_parquet(tmp_path: Path) -> None:
    raw_root = tmp_path / "raw"
    out_root = tmp_path / "out"
    _write_jsonl(
        raw_root / "rss-fake" / "2026-05-22.jsonl",
        [
            {
                "ts": "2026-05-22T10:15:00Z",
                "title": "Bitcoin rallies past resistance, bulls dominate",
                "body": "Strong upside as buyers surge.",
                "tags": ["BTC"],
            },
            {
                "ts": "2026-05-22T11:00:00Z",
                "title": "Crash for bitcoin as bears panic the market",
                "body": "Heavy losses; slump deepens.",
                "tags": ["BTC"],
            },
            {
                # Outside time window → must be dropped.
                "ts": "2026-05-23T08:00:00Z",
                "title": "Bitcoin moonshot",
                "body": "Rally continues.",
                "tags": ["BTC"],
            },
            {
                # No keyword match → must be dropped.
                "ts": "2026-05-22T12:00:00Z",
                "title": "Cricket world cup highlights",
                "body": "Sport unrelated to crypto.",
            },
        ],
    )
    written = build_sentiment_features(
        instrument="BTCUSDT-PERP.BINANCE",
        since=dt.datetime(2026, 5, 22, tzinfo=UTC),
        until=dt.datetime(2026, 5, 23, tzinfo=UTC),
        sources=["rss-fake"],
        scorer="vader",
        raw_root=raw_root,
        out_root=out_root,
        keywords=["bitcoin", "btc"],
    )
    assert len(written) == 1
    parquet = written[0]
    assert parquet.exists()
    assert parquet.name == "2026-05-22.parquet"
    df = pd.read_parquet(parquet)
    assert tuple(df.columns) == SCHEMA_COLS
    # Only the two in-window, keyword-matching rows survived.
    assert len(df) == 2
    # Schema dtypes.
    assert str(df["ts"].dtype) == "int64"
    assert str(df["raw_score"].dtype) == "float64"
    assert str(df["normalized_score"].dtype) == "float64"
    assert str(df["n_articles"].dtype) == "int32"
    # One bullish + one bearish.
    assert (df["raw_score"] > 0).sum() == 1
    assert (df["raw_score"] < 0).sum() == 1
    # Range bound.
    assert df["normalized_score"].between(-1.0, 1.0).all()
    # Instrument propagated.
    assert (df["instrument"] == "BTCUSDT-PERP.BINANCE").all()
    # Scorer name persisted.
    assert (df["scorer"] == "vader").all()


def test_build_sentiment_features_empty_corpus_writes_no_files(tmp_path: Path) -> None:
    raw_root = tmp_path / "raw"
    out_root = tmp_path / "out"
    # No matching keyword in body/title/tags.
    _write_jsonl(
        raw_root / "rss-fake" / "2026-05-22.jsonl",
        [
            {
                "ts": "2026-05-22T10:00:00Z",
                "title": "weather report",
                "body": "sunny in tokyo today",
                "tags": ["weather"],
            }
        ],
    )
    written = build_sentiment_features(
        instrument="BTCUSDT-PERP.BINANCE",
        since=dt.datetime(2026, 5, 22, tzinfo=UTC),
        until=dt.datetime(2026, 5, 23, tzinfo=UTC),
        sources=["rss-fake"],
        raw_root=raw_root,
        out_root=out_root,
        keywords=["btc", "bitcoin"],
    )
    assert written == []
    # Instrument dir is created even when empty (helps downstream listing).
    assert (out_root / "BTCUSDT-PERP.BINANCE").is_dir()


def test_build_sentiment_features_rejects_naive_window() -> None:
    with pytest.raises(ValueError):
        build_sentiment_features(
            instrument="BTCUSDT-PERP.BINANCE",
            since=dt.datetime(2026, 5, 22),  # naive
            until=dt.datetime(2026, 5, 23, tzinfo=UTC),
            sources=["rss-fake"],
            keywords=["btc"],
        )


def test_build_sentiment_features_rejects_empty_sources(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        build_sentiment_features(
            instrument="BTCUSDT-PERP.BINANCE",
            since=dt.datetime(2026, 5, 22, tzinfo=UTC),
            until=dt.datetime(2026, 5, 23, tzinfo=UTC),
            sources=[],
            keywords=["btc"],
            raw_root=tmp_path / "raw",
            out_root=tmp_path / "out",
        )


def test_build_sentiment_features_idempotent_byte_stable(tmp_path: Path) -> None:
    raw_root = tmp_path / "raw"
    out_root_a = tmp_path / "out_a"
    out_root_b = tmp_path / "out_b"
    _write_jsonl(
        raw_root / "rss-fake" / "2026-05-22.jsonl",
        [
            {"ts": "2026-05-22T10:00:00Z", "title": "Bitcoin rally", "body": "bulls", "tags": ["btc"]},
            {"ts": "2026-05-22T11:00:00Z", "title": "Bitcoin crash", "body": "bears", "tags": ["btc"]},
        ],
    )

    def _run(out: Path) -> Path:
        files = build_sentiment_features(
            instrument="BTCUSDT-PERP.BINANCE",
            since=dt.datetime(2026, 5, 22, tzinfo=UTC),
            until=dt.datetime(2026, 5, 23, tzinfo=UTC),
            sources=["rss-fake"],
            raw_root=raw_root,
            out_root=out,
            keywords=["btc", "bitcoin"],
        )
        return files[0]

    p_a = _run(out_root_a)
    p_b = _run(out_root_b)
    df_a = pd.read_parquet(p_a).reset_index(drop=True)
    df_b = pd.read_parquet(p_b).reset_index(drop=True)
    pd.testing.assert_frame_equal(df_a, df_b)
