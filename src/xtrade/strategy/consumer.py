"""SignalConsumer — cursor-tracked reader over `SignalQueue` (Phase 3 Task 4 / T1).

Purpose
-------
The strategy layer must NOT read jsonl shards directly (Phase 2 →
Phase 3 contract boundary; see `docs/phase2_results.md` §3.3). All
access goes through `SignalConsumer`, which:

  - wraps a `xtrade.research.signals.SignalQueue`;
  - exposes the same `tail` / `since` / `filter` reads;
  - adds a cursor so the runner can call `iter_new()` repeatedly and
    only see signals that have appeared since the last drain.

Cursor semantics
----------------
`iter_new()` returns signals whose `dedup_key()` hasn't been yielded
by this consumer before, in the queue's natural (file-shard) order.

The cursor is **in-memory by default**; pass ``cursor_path=<path>`` to
also persist it to disk. When a path is given:

  - on construction, the persisted seen-set is loaded (missing or
    corrupt file → start empty; never raises);
  - the caller invokes :meth:`commit` at safe points to flush the
    current in-memory seen-set to disk (atomic-write).

The cursor is **never** auto-flushed on every yield: deciding when a
signal has been "successfully processed downstream" is the caller's
job (e.g. a Phase 4 long-running runner commits after each successful
intent submission). This keeps replay semantics explicit and avoids
hidden I/O in a hot loop.

All disk I/O for the cursor is delegated to
:mod:`xtrade.strategy.cursor` so that ``consumer.py`` itself contains
no direct file I/O — see the architectural guard in
``tests/test_signal_consumer.py::test_consumer_does_not_open_files_directly``.

Filtering
---------
Construct-time filters (`symbol`, `source`, `venue`, `direction`) are
applied to every read (both `iter_new()` and `tail`/`since`). This
keeps "this strategy only watches BTCUSDT-PERP momentum signals"
declarative at the consumer, not scattered through the strategy code.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Iterator

from xtrade.research.signals import Direction, Signal, SignalQueue


class SignalConsumer:
    """Cursor-tracked, filter-aware reader over a `SignalQueue`."""

    def __init__(
        self,
        queue: SignalQueue | Path | str,
        *,
        symbol: str | None = None,
        source: str | None = None,
        venue: str | None = None,
        direction: Direction | None = None,
        cursor_path: Path | str | None = None,
    ) -> None:
        if isinstance(queue, SignalQueue):
            self.queue = queue
        else:
            self.queue = SignalQueue(queue)
        self._symbol = symbol
        self._source = source
        self._venue = venue
        self._direction = direction
        self._cursor_path: Path | None = (
            Path(cursor_path) if cursor_path is not None else None
        )
        self._seen: set[tuple[str, str, str]] = set()
        if self._cursor_path is not None:
            # File I/O lives in `xtrade.strategy.cursor` so that this
            # module's architectural guard (no direct file I/O) holds.
            from xtrade.strategy import cursor as _cursor_store

            self._seen = _cursor_store.load(self._cursor_path)

    # ----- cursor read ---------------------------------------------------

    def iter_new(self) -> Iterator[Signal]:
        """Yield signals appearing since the last `iter_new()` call.

        The dedup key matches `SignalQueue.append`'s natural identity
        (`(generated_at_iso, symbol, source)`), so a re-emit of the
        same signal is skipped.
        """
        for sig in self.queue:
            if not self._matches(sig):
                continue
            key = sig.dedup_key()
            if key in self._seen:
                continue
            self._seen.add(key)
            yield sig

    def drain(self) -> list[Signal]:
        """Materialise `iter_new()` into a list (convenience for tests)."""
        return list(self.iter_new())

    def reset_cursor(self) -> None:
        """Clear the seen-set so the next `iter_new()` re-yields everything.

        Does **not** touch the persisted cursor file. Call :meth:`commit`
        afterwards if you also want the on-disk cursor cleared.
        """
        self._seen.clear()

    def commit(self) -> None:
        """Persist the current seen-set to ``cursor_path`` if configured.

        No-op when ``cursor_path`` was not given to the constructor.
        Safe to call after every batch, or only at shutdown — the
        atomic-write template means partial failures never leave the
        cursor file in a half-written state.
        """
        if self._cursor_path is None:
            return
        from xtrade.strategy import cursor as _cursor_store

        _cursor_store.save(self._cursor_path, self._seen)

    # ----- passthrough reads --------------------------------------------

    def tail(self, n: int) -> list[Signal]:
        """Most recent `n` signals (after filtering); does NOT touch cursor."""
        return [s for s in self.queue.tail(n) if self._matches(s)]

    def since(self, when: dt.datetime) -> list[Signal]:
        """Signals with `generated_at >= when` (filtered); cursor untouched."""
        return [s for s in self.queue.since(when) if self._matches(s)]

    def list_all(self) -> list[Signal]:
        """All matching signals; cursor untouched."""
        return [s for s in self.queue if self._matches(s)]

    # ----- internals -----------------------------------------------------

    def _matches(self, sig: Signal) -> bool:
        if self._symbol is not None and sig.symbol != self._symbol:
            return False
        if self._source is not None and sig.source != self._source:
            return False
        if self._venue is not None and sig.venue != self._venue:
            return False
        if self._direction is not None and sig.direction != self._direction:
            return False
        return True
