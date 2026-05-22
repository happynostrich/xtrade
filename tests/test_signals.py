"""Offline tests for `xtrade.research.signals` (Phase 2 Task 5 / S5)."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from xtrade.research.signals import Signal, SignalQueue


UTC = dt.timezone.utc


# ---------------------------------------------------------------------------
# Signal validation
# ---------------------------------------------------------------------------


def _ts(year: int = 2024, month: int = 1, day: int = 1, hour: int = 12) -> dt.datetime:
    return dt.datetime(year, month, day, hour, tzinfo=UTC)


def test_signal_minimal_construction() -> None:
    s = Signal(
        symbol="BTCUSDT-PERP",
        venue="binance",
        direction="LONG",
        strength=0.8,
        generated_at=_ts(),
        source="momentum:abc12345",
    )
    assert s.symbol == "BTCUSDT-PERP"
    assert s.strength == 0.8
    assert s.metadata == {}
    assert s.valid_until is None


@pytest.mark.parametrize("bad_dir", ["BUY", "long", "", "OTHER"])
def test_signal_rejects_invalid_direction(bad_dir) -> None:
    with pytest.raises(ValueError, match="direction must be"):
        Signal(
            symbol="X", venue="binance", direction=bad_dir,  # type: ignore[arg-type]
            strength=0.0, generated_at=_ts(), source="src",
        )


@pytest.mark.parametrize("bad_s", [-1.01, 1.01, 2.0, -2.0])
def test_signal_rejects_out_of_range_strength(bad_s) -> None:
    with pytest.raises(ValueError, match="strength must be"):
        Signal(
            symbol="X", venue="binance", direction="LONG",
            strength=bad_s, generated_at=_ts(), source="src",
        )


def test_signal_requires_tz_aware_generated_at() -> None:
    naive = dt.datetime(2024, 1, 1, 12)
    with pytest.raises(ValueError, match="generated_at must be timezone-aware"):
        Signal(
            symbol="X", venue="binance", direction="LONG",
            strength=0.0, generated_at=naive, source="src",
        )


def test_signal_rejects_valid_until_before_generated_at() -> None:
    with pytest.raises(ValueError, match="valid_until must be"):
        Signal(
            symbol="X", venue="binance", direction="LONG",
            strength=0.0, generated_at=_ts(hour=12), source="src",
            valid_until=_ts(hour=10),
        )


def test_signal_rejects_empty_symbol() -> None:
    with pytest.raises(ValueError, match="symbol must be"):
        Signal(
            symbol="", venue="binance", direction="LONG",
            strength=0.0, generated_at=_ts(), source="src",
        )


# ---------------------------------------------------------------------------
# Metadata secret guard
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "leaky",
    [
        "sk-abc1234567890123456",
        "sk-AAAAAAAAAAAAAAAAAAAA",
        "key=0x" + "a" * 64,
        "AKIA" + "A" * 16,
    ],
)
def test_signal_rejects_metadata_with_secret_string(leaky) -> None:
    with pytest.raises(ValueError, match="credential-like"):
        Signal(
            symbol="X", venue="binance", direction="LONG",
            strength=0.0, generated_at=_ts(), source="src",
            metadata={"leak": leaky},
        )


def test_signal_secret_guard_recurses_into_nested() -> None:
    """Secret guard walks dict/list/tuple recursively."""
    with pytest.raises(ValueError, match="credential-like"):
        Signal(
            symbol="X", venue="binance", direction="LONG",
            strength=0.0, generated_at=_ts(), source="src",
            metadata={"nested": {"deep": ["sk-abc1234567890123456"]}},
        )


def test_signal_accepts_benign_metadata() -> None:
    """Strings that aren't secret-like must pass."""
    Signal(
        symbol="X", venue="binance", direction="LONG",
        strength=0.0, generated_at=_ts(), source="src",
        metadata={"comment": "this is fine", "n": 3, "vec": [1, 2, 3]},
    )


# ---------------------------------------------------------------------------
# Serialisation round-trip
# ---------------------------------------------------------------------------


def test_signal_to_dict_round_trip() -> None:
    original = Signal(
        symbol="BTCUSDT-PERP",
        venue="binance",
        direction="LONG",
        strength=0.5,
        generated_at=_ts(hour=12),
        source="momentum:abc12345",
        valid_until=_ts(hour=14),
        metadata={"fast": 5, "slow": 20},
    )
    round_tripped = Signal.from_dict(original.to_dict())
    assert round_tripped == original


def test_signal_from_dict_rejects_naive_timestamp() -> None:
    payload = {
        "symbol": "X", "venue": "binance", "direction": "LONG",
        "strength": 0.0, "generated_at": "2024-01-01T12:00:00",  # naive!
        "source": "src", "valid_until": None, "metadata": {},
    }
    with pytest.raises(ValueError, match="missing timezone"):
        Signal.from_dict(payload)


# ---------------------------------------------------------------------------
# SignalQueue
# ---------------------------------------------------------------------------


def _sig(symbol: str = "X", *, ts: dt.datetime | None = None, source: str = "src") -> Signal:
    return Signal(
        symbol=symbol, venue="binance", direction="LONG", strength=0.5,
        generated_at=ts or _ts(),
        source=source,
    )


def test_queue_append_then_read_round_trip(tmp_path: Path) -> None:
    q = SignalQueue(tmp_path)
    written = q.append([_sig("BTC"), _sig("ETH")])
    assert written == 2
    rows = list(q)
    assert {r.symbol for r in rows} == {"BTC", "ETH"}


def test_queue_append_is_idempotent(tmp_path: Path) -> None:
    q = SignalQueue(tmp_path)
    sigs = [_sig("BTC"), _sig("ETH")]
    assert q.append(sigs) == 2
    assert q.append(sigs) == 0
    assert len(list(q)) == 2


def test_queue_dedups_within_single_append(tmp_path: Path) -> None:
    q = SignalQueue(tmp_path)
    s = _sig("BTC")
    assert q.append([s, s, s]) == 1


def test_queue_shards_by_utc_date(tmp_path: Path) -> None:
    q = SignalQueue(tmp_path)
    day1 = _sig("BTC", ts=dt.datetime(2024, 1, 1, 12, tzinfo=UTC))
    day2 = _sig("BTC", ts=dt.datetime(2024, 1, 2, 12, tzinfo=UTC))
    q.append([day1, day2])
    assert (tmp_path / "2024-01-01.jsonl").exists()
    assert (tmp_path / "2024-01-02.jsonl").exists()


def test_queue_tail_returns_most_recent(tmp_path: Path) -> None:
    q = SignalQueue(tmp_path)
    q.append([
        _sig("A", ts=_ts(hour=10)),
        _sig("B", ts=_ts(hour=11)),
        _sig("C", ts=_ts(hour=12)),
    ])
    tail = q.tail(2)
    assert len(tail) == 2
    assert tail[-1].symbol == "C"


def test_queue_tail_zero_returns_empty(tmp_path: Path) -> None:
    q = SignalQueue(tmp_path)
    q.append([_sig("BTC")])
    assert q.tail(0) == []


def test_queue_since_filters_by_timestamp(tmp_path: Path) -> None:
    q = SignalQueue(tmp_path)
    early = _sig("A", ts=_ts(hour=8))
    late = _sig("B", ts=_ts(hour=16))
    q.append([early, late])
    out = q.since(_ts(hour=12))
    assert [s.symbol for s in out] == ["B"]


def test_queue_since_requires_tz_aware(tmp_path: Path) -> None:
    q = SignalQueue(tmp_path)
    naive = dt.datetime(2024, 1, 1, 12)
    with pytest.raises(ValueError, match="timezone-aware"):
        q.since(naive)


def test_queue_filter_by_symbol_and_source(tmp_path: Path) -> None:
    q = SignalQueue(tmp_path)
    q.append([
        _sig("BTC", source="momentum:a"),
        _sig("BTC", source="meanrev:b"),
        _sig("ETH", source="momentum:a"),
    ])
    out = q.filter(symbol="BTC", source="momentum:a")
    assert len(out) == 1
    assert out[0].symbol == "BTC"
    assert out[0].source == "momentum:a"


def test_queue_filter_by_direction(tmp_path: Path) -> None:
    q = SignalQueue(tmp_path)
    q.append([
        Signal(symbol="X", venue="binance", direction="LONG",
               strength=0.5, generated_at=_ts(), source="src1"),
        Signal(symbol="X", venue="binance", direction="FLAT",
               strength=0.0, generated_at=_ts(hour=13), source="src2"),
    ])
    flats = q.filter(direction="FLAT")
    assert len(flats) == 1
    assert flats[0].direction == "FLAT"


def test_queue_skips_corrupt_line_with_warning(tmp_path: Path) -> None:
    q = SignalQueue(tmp_path)
    q.append([_sig("BTC")])
    shard = next(tmp_path.glob("*.jsonl"))
    # Append a broken line; reader must skip + warn, not crash.
    with shard.open("a", encoding="utf-8") as fh:
        fh.write("THIS IS NOT JSON\n")

    with pytest.warns(RuntimeWarning, match="corrupt signal"):
        rows = list(q)
    assert len(rows) == 1
    assert rows[0].symbol == "BTC"


def test_queue_atomic_append_preserves_existing_rows(tmp_path: Path) -> None:
    """Two separate append calls accumulate on disk; no overwrite."""
    q = SignalQueue(tmp_path)
    q.append([_sig("BTC")])
    q.append([_sig("ETH")])
    rows = list(q)
    assert {r.symbol for r in rows} == {"BTC", "ETH"}


def test_queue_jsonl_lines_are_sorted_key_json(tmp_path: Path) -> None:
    """Lines must be deterministic so git diffs stay sane."""
    q = SignalQueue(tmp_path)
    q.append([
        Signal(
            symbol="BTC", venue="binance", direction="LONG", strength=0.5,
            generated_at=_ts(), source="src", metadata={"z": 1, "a": 2},
        )
    ])
    shard = next(tmp_path.glob("*.jsonl"))
    line = shard.read_text(encoding="utf-8").strip()
    payload = json.loads(line)
    # JSON key order is preserved by json.loads; check keys are sorted.
    keys = list(payload.keys())
    assert keys == sorted(keys), f"keys not sorted: {keys}"


def test_queue_empty_directory_iterates_empty(tmp_path: Path) -> None:
    q = SignalQueue(tmp_path)
    assert list(q) == []
    assert q.tail(5) == []
    assert q.since(_ts()) == []
    assert q.filter(symbol="BTC") == []


def test_queue_creates_root_dir_if_missing(tmp_path: Path) -> None:
    new_dir = tmp_path / "nested" / "signals"
    assert not new_dir.exists()
    q = SignalQueue(new_dir)
    assert new_dir.exists()
    q.append([_sig("BTC")])
    assert len(list(q)) == 1
