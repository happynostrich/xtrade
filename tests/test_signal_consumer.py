"""Tests for `xtrade.strategy.consumer.SignalConsumer` (Phase 3 Task 4 / T1)."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from xtrade.research.signals import Signal, SignalQueue
from xtrade.strategy.consumer import SignalConsumer


UTC = dt.timezone.utc


def _sig(
    *,
    symbol: str = "BTCUSDT-PERP",
    venue: str = "binance",
    direction: str = "LONG",
    source: str = "momentum:abc12345",
    h: int = 10,
    m: int = 0,
) -> Signal:
    return Signal(
        symbol=symbol,
        venue=venue,
        direction=direction,  # type: ignore[arg-type]
        strength=0.5 if direction == "LONG" else (-0.5 if direction == "SHORT" else 0.0),
        generated_at=dt.datetime(2026, 5, 22, h, m, 0, tzinfo=UTC),
        source=source,
    )


# ---- construction + cursor ---------------------------------------------


def test_consumer_accepts_queue_instance(tmp_path: Path) -> None:
    queue = SignalQueue(tmp_path)
    queue.append([_sig()])
    consumer = SignalConsumer(queue)
    assert len(consumer.drain()) == 1


def test_consumer_accepts_path(tmp_path: Path) -> None:
    queue = SignalQueue(tmp_path)
    queue.append([_sig()])
    consumer = SignalConsumer(tmp_path)
    assert len(consumer.drain()) == 1


def test_iter_new_yields_each_signal_once(tmp_path: Path) -> None:
    queue = SignalQueue(tmp_path)
    queue.append([_sig(h=10), _sig(h=11)])
    consumer = SignalConsumer(queue)

    first = consumer.drain()
    assert len(first) == 2

    # No new signals in the queue → next drain is empty.
    second = consumer.drain()
    assert second == []


def test_iter_new_sees_signals_appended_between_calls(tmp_path: Path) -> None:
    queue = SignalQueue(tmp_path)
    queue.append([_sig(h=10)])
    consumer = SignalConsumer(queue)

    assert len(consumer.drain()) == 1
    queue.append([_sig(h=11)])
    new = consumer.drain()
    assert len(new) == 1
    assert new[0].generated_at.hour == 11


def test_reset_cursor_re_yields_everything(tmp_path: Path) -> None:
    queue = SignalQueue(tmp_path)
    queue.append([_sig(h=10), _sig(h=11)])
    consumer = SignalConsumer(queue)
    consumer.drain()
    consumer.reset_cursor()
    assert len(consumer.drain()) == 2


# ---- filtering ----------------------------------------------------------


def test_filter_by_symbol(tmp_path: Path) -> None:
    queue = SignalQueue(tmp_path)
    queue.append([
        _sig(symbol="BTCUSDT-PERP", h=10),
        _sig(symbol="ETHUSDT-PERP", h=10, m=1),
    ])
    consumer = SignalConsumer(queue, symbol="BTCUSDT-PERP")
    out = consumer.drain()
    assert len(out) == 1
    assert out[0].symbol == "BTCUSDT-PERP"


def test_filter_by_direction(tmp_path: Path) -> None:
    queue = SignalQueue(tmp_path)
    queue.append([
        _sig(direction="LONG", h=10),
        _sig(direction="SHORT", h=10, m=1),
        _sig(direction="FLAT", h=10, m=2),
    ])
    consumer = SignalConsumer(queue, direction="LONG")
    out = consumer.drain()
    assert len(out) == 1
    assert out[0].direction == "LONG"


def test_filter_by_source(tmp_path: Path) -> None:
    queue = SignalQueue(tmp_path)
    queue.append([
        _sig(source="momentum:aaaaaaaa", h=10),
        _sig(source="meanrev:bbbbbbbb", h=10, m=1),
    ])
    consumer = SignalConsumer(queue, source="momentum:aaaaaaaa")
    out = consumer.drain()
    assert len(out) == 1
    assert out[0].source == "momentum:aaaaaaaa"


def test_filter_by_venue(tmp_path: Path) -> None:
    queue = SignalQueue(tmp_path)
    queue.append([
        _sig(venue="binance", h=10),
        _sig(venue="hyperliquid", h=10, m=1),
    ])
    consumer = SignalConsumer(queue, venue="binance")
    out = consumer.drain()
    assert len(out) == 1
    assert out[0].venue == "binance"


# ---- passthrough reads --------------------------------------------------


def test_tail_passthrough_does_not_touch_cursor(tmp_path: Path) -> None:
    queue = SignalQueue(tmp_path)
    queue.append([_sig(h=10), _sig(h=11)])
    consumer = SignalConsumer(queue)
    assert len(consumer.tail(1)) == 1
    # Cursor untouched → iter_new still yields everything.
    assert len(consumer.drain()) == 2


def test_since_passthrough(tmp_path: Path) -> None:
    queue = SignalQueue(tmp_path)
    queue.append([_sig(h=10), _sig(h=11), _sig(h=12)])
    consumer = SignalConsumer(queue)
    later = consumer.since(dt.datetime(2026, 5, 22, 11, 0, tzinfo=UTC))
    assert len(later) == 2


def test_list_all_applies_filters(tmp_path: Path) -> None:
    queue = SignalQueue(tmp_path)
    queue.append([
        _sig(direction="LONG", h=10),
        _sig(direction="SHORT", h=10, m=1),
    ])
    consumer = SignalConsumer(queue, direction="LONG")
    assert len(consumer.list_all()) == 1


# ---- cursor persistence (Phase 3.5) -------------------------------------


def test_cursor_persistence_round_trip(tmp_path: Path) -> None:
    """After `commit()`, a new consumer with the same cursor path skips replay."""
    queue_root = tmp_path / "signals"
    cursor_path = tmp_path / "cursors" / "momentum_follow.json"

    queue = SignalQueue(queue_root)
    queue.append([_sig(h=10), _sig(h=11)])

    c1 = SignalConsumer(queue, cursor_path=cursor_path)
    assert len(c1.drain()) == 2
    c1.commit()

    assert cursor_path.exists(), "commit() must create the cursor file"

    # New consumer instance, same cursor path: no replay.
    c2 = SignalConsumer(queue, cursor_path=cursor_path)
    assert c2.drain() == []

    # A new signal lands; only it should be yielded.
    queue.append([_sig(h=12)])
    new = c2.drain()
    assert len(new) == 1
    assert new[0].generated_at.hour == 12

    # Commit again; restart again; still no replay.
    c2.commit()
    c3 = SignalConsumer(queue, cursor_path=cursor_path)
    assert c3.drain() == []


def test_cursor_commit_without_path_is_noop(tmp_path: Path) -> None:
    """`commit()` is a no-op (no exception, no file) when no `cursor_path`."""
    queue = SignalQueue(tmp_path / "signals")
    queue.append([_sig(h=10)])

    c = SignalConsumer(queue)  # no cursor_path
    c.drain()
    c.commit()  # must not raise

    # Nothing should have been written anywhere under tmp_path/cursors/.
    assert not (tmp_path / "cursors").exists()


def test_cursor_corrupt_file_safe_replay(tmp_path: Path) -> None:
    """A corrupt cursor file is treated as empty (safer than crashing)."""
    queue = SignalQueue(tmp_path / "signals")
    queue.append([_sig(h=10)])

    cursor_path = tmp_path / "cursor.json"
    cursor_path.parent.mkdir(parents=True, exist_ok=True)
    cursor_path.write_text("definitely not json {{{", encoding="utf-8")

    c = SignalConsumer(queue, cursor_path=cursor_path)
    # Corrupt file → empty seen → first drain replays the signal.
    assert len(c.drain()) == 1


def test_cursor_atomic_write(tmp_path: Path) -> None:
    """`commit()` leaves no `.cursor.*.tmp` stragglers in the target dir."""
    queue = SignalQueue(tmp_path / "signals")
    queue.append([_sig(h=10), _sig(h=11)])

    # Use an isolated dir so we don't see the queue's directory listing.
    cursor_dir = tmp_path / "cursors"
    cursor_path = cursor_dir / "momentum_follow.json"
    c = SignalConsumer(queue, cursor_path=cursor_path)
    c.drain()
    c.commit()

    # No `.cursor.*.tmp` stragglers in the cursor directory.
    leftovers = [p.name for p in cursor_dir.iterdir() if p.name.startswith(".cursor.")]
    assert leftovers == [], f"unexpected tempfile stragglers: {leftovers}"
    # And the cursor file itself is there.
    assert cursor_path.exists()


def test_cursor_file_schema(tmp_path: Path) -> None:
    """The persisted cursor JSON has the documented shape."""
    import json as _json

    queue = SignalQueue(tmp_path / "signals")
    queue.append([_sig(h=10)])

    cursor_path = tmp_path / "cursor.json"
    c = SignalConsumer(queue, cursor_path=cursor_path)
    c.drain()
    c.commit()

    payload = _json.loads(cursor_path.read_text())
    assert payload["version"] == 1
    assert isinstance(payload["updated_at"], str)
    assert isinstance(payload["seen"], list)
    assert len(payload["seen"]) == 1
    row = payload["seen"][0]
    assert isinstance(row, list) and len(row) == 3
    assert all(isinstance(c, str) for c in row)


# ---- contract boundary: consumer never reads jsonl directly -------------


def test_consumer_does_not_open_files_directly() -> None:
    """`SignalConsumer` must go through `SignalQueue` — no Path I/O of its own."""
    import ast
    from pathlib import Path as _P

    src = _P(__file__).resolve().parents[1] / "src" / "xtrade" / "strategy" / "consumer.py"
    tree = ast.parse(src.read_text(encoding="utf-8"))
    forbidden = {"open"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id not in forbidden, (
                f"SignalConsumer must not call open(); use SignalQueue"
            )
        if isinstance(node, ast.Attribute) and node.attr in {
            "read_text", "read_bytes", "open",
        }:
            # Allow attribute access on imports unrelated to file I/O
            # (none expected in this module).
            raise AssertionError(
                f"SignalConsumer must not perform direct file I/O ({node.attr})"
            )
