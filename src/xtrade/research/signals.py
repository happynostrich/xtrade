"""Signal data class + persistent jsonl signal queue (Phase 2 Task 5 / S5).

Persistence model
-----------------
Signals are sharded by their `generated_at` UTC date:

    data/signals/2026-05-22.jsonl
    data/signals/2026-05-23.jsonl
    ...

Each line is one Signal serialised as a single JSON object (UTC ISO-8601
timestamps). Atomic writes: the queue writes to `<file>.tmp` then renames
into place, so a crash mid-append leaves the visible file consistent
(possibly missing the in-flight batch, never corrupted).

Idempotency
-----------
`append()` dedupes by the natural key `(generated_at, symbol, source)`:
re-running the same scan on the same bars produces the same Signals,
which the queue silently skips on the second write. This matches Phase
2 brief §5 Task 5's "重复运行幂等" requirement.

Reader robustness
-----------------
A corrupted line (truncated write, manual edit) is skipped with a
`warnings.warn(...)` rather than crashing the consumer. The queue is
append-only mostly-write-once; we never re-write a historical file, so
the worst observed corruption is a half-written final line.

Security guards (brief §6)
--------------------------
`Signal.metadata` is forbidden from carrying credential-looking strings:
anything matching `sk-...` or `0x[0-9a-f]{64}` is rejected at construct
time. This prevents a misbehaving scanner from accidentally pickling an
API key into a "research" jsonl that's checked into the repo or copied
between machines.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import os
import re
import tempfile
import warnings
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any, Literal


# ---------------------------------------------------------------------------
# Signal
# ---------------------------------------------------------------------------


Direction = Literal["LONG", "SHORT", "FLAT"]
_VALID_DIRECTIONS: frozenset[str] = frozenset({"LONG", "SHORT", "FLAT"})

# Anything that *looks* like a key gets refused. These patterns are
# intentionally narrow — they catch the common leak vectors without
# rejecting plausibly legitimate metadata strings.
_FORBIDDEN_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bsk-[A-Za-z0-9]{16,}\b"),     # OpenAI / Anthropic prefix
    re.compile(r"\b0x[0-9a-fA-F]{64}\b"),       # EVM private key
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),        # AWS access key id
)


@dataclasses.dataclass(frozen=True, slots=True)
class Signal:
    """A research-layer trading idea produced by a Scanner.

    Field meanings
    --------------
    symbol        : exchange-side ticker (e.g. ``BTCUSDT-PERP``)
    venue         : exchange name (``binance`` / ``hyperliquid``)
    direction     : ``LONG`` / ``SHORT`` / ``FLAT``
    strength      : conviction in ``[-1, 1]`` (sign should match direction)
    generated_at  : UTC datetime the signal was computed
    source        : scanner identifier (e.g. ``momentum:a1b2c3d4``)
    valid_until   : UTC datetime past which the signal is stale
    metadata      : free-form params / context; **must not** contain secrets
    """

    symbol: str
    venue: str
    direction: Direction
    strength: float
    generated_at: dt.datetime
    source: str
    valid_until: dt.datetime | None = None
    metadata: dict[str, Any] = dataclasses.field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.symbol or not isinstance(self.symbol, str):
            raise ValueError(f"symbol must be a non-empty string, got {self.symbol!r}")
        if not self.venue or not isinstance(self.venue, str):
            raise ValueError(f"venue must be a non-empty string, got {self.venue!r}")
        if self.direction not in _VALID_DIRECTIONS:
            raise ValueError(
                f"direction must be one of {sorted(_VALID_DIRECTIONS)}, "
                f"got {self.direction!r}"
            )
        if not -1.0 <= float(self.strength) <= 1.0:
            raise ValueError(
                f"strength must be in [-1, 1], got {self.strength}"
            )
        if not isinstance(self.generated_at, dt.datetime):
            raise TypeError(
                f"generated_at must be a datetime, got {type(self.generated_at).__name__}"
            )
        if self.generated_at.tzinfo is None:
            raise ValueError("generated_at must be timezone-aware (UTC)")
        if self.valid_until is not None:
            if not isinstance(self.valid_until, dt.datetime):
                raise TypeError("valid_until must be a datetime or None")
            if self.valid_until.tzinfo is None:
                raise ValueError("valid_until must be timezone-aware (UTC)")
            if self.valid_until < self.generated_at:
                raise ValueError("valid_until must be >= generated_at")
        if not self.source or not isinstance(self.source, str):
            raise ValueError(f"source must be a non-empty string, got {self.source!r}")
        if not isinstance(self.metadata, dict):
            raise TypeError(
                f"metadata must be a dict, got {type(self.metadata).__name__}"
            )
        _scan_metadata_for_secrets(self.metadata)

    # ----- (de)serialisation helpers --------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "venue": self.venue,
            "direction": self.direction,
            "strength": float(self.strength),
            "generated_at": self.generated_at.astimezone(dt.timezone.utc).isoformat(),
            "source": self.source,
            "valid_until": (
                self.valid_until.astimezone(dt.timezone.utc).isoformat()
                if self.valid_until is not None
                else None
            ),
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Signal":
        generated_at = _parse_iso(data["generated_at"])
        raw_valid = data.get("valid_until")
        valid_until = _parse_iso(raw_valid) if raw_valid is not None else None
        return cls(
            symbol=data["symbol"],
            venue=data["venue"],
            direction=data["direction"],
            strength=float(data["strength"]),
            generated_at=generated_at,
            source=data["source"],
            valid_until=valid_until,
            metadata=dict(data.get("metadata", {})),
        )

    def dedup_key(self) -> tuple[str, str, str]:
        """Identity used by `SignalQueue.append` to skip duplicates."""
        return (self.generated_at.isoformat(), self.symbol, self.source)


# ---------------------------------------------------------------------------
# SignalQueue
# ---------------------------------------------------------------------------


class SignalQueue:
    """Append-only jsonl queue sharded by `generated_at` UTC date.

    Files live under `root_dir`; one file per day:
    `root_dir/<YYYY-MM-DD>.jsonl`. Within a file lines are one Signal-as-
    JSON each. The queue is single-writer-safe (per-file atomic rename);
    multi-writer concurrency on the *same day* is not guaranteed (out of
    scope for Phase 2; Phase 4's scheduler will revisit).
    """

    def __init__(self, root_dir: Path | str) -> None:
        self.root_dir = Path(root_dir)
        self.root_dir.mkdir(parents=True, exist_ok=True)

    # ----- write side ------------------------------------------------------

    def append(self, signals: Iterable[Signal]) -> int:
        """Append `signals` to their per-day jsonl shards. Returns count
        of newly-written rows (after dedup against existing files)."""
        by_day: dict[dt.date, list[Signal]] = {}
        for s in signals:
            day = s.generated_at.astimezone(dt.timezone.utc).date()
            by_day.setdefault(day, []).append(s)

        total_written = 0
        for day, batch in by_day.items():
            shard = self._shard_path(day)
            existing_keys = {s.dedup_key() for s in self._read_shard(shard)}
            fresh = [s for s in batch if s.dedup_key() not in existing_keys]
            # Also dedup within the incoming batch (same scan can emit
            # the same key twice if grid expands ambiguously).
            seen: set[tuple[str, str, str]] = set()
            unique: list[Signal] = []
            for s in fresh:
                key = s.dedup_key()
                if key in seen:
                    continue
                seen.add(key)
                unique.append(s)

            if not unique:
                continue
            self._append_atomic(shard, unique)
            total_written += len(unique)
        return total_written

    # ----- read side -------------------------------------------------------

    def tail(self, n: int) -> list[Signal]:
        """Return the most recent `n` signals across all shards."""
        if n <= 0:
            return []
        all_signals = list(self)
        # Already sorted by file order (date asc) + in-file order; we
        # want the *last* n in that ordering.
        return all_signals[-n:]

    def since(self, when: dt.datetime) -> list[Signal]:
        """Return all signals with `generated_at >= when`."""
        if when.tzinfo is None:
            raise ValueError("`when` must be timezone-aware (UTC)")
        when_utc = when.astimezone(dt.timezone.utc)
        return [s for s in self if s.generated_at >= when_utc]

    def filter(
        self,
        *,
        symbol: str | None = None,
        source: str | None = None,
        venue: str | None = None,
        direction: Direction | None = None,
    ) -> list[Signal]:
        """Return signals matching all provided filters (AND)."""
        out: list[Signal] = []
        for s in self:
            if symbol is not None and s.symbol != symbol:
                continue
            if source is not None and s.source != source:
                continue
            if venue is not None and s.venue != venue:
                continue
            if direction is not None and s.direction != direction:
                continue
            out.append(s)
        return out

    def __iter__(self) -> Iterator[Signal]:
        for shard in sorted(self.root_dir.glob("*.jsonl")):
            yield from self._read_shard(shard)

    # ----- internals -------------------------------------------------------

    def _shard_path(self, day: dt.date) -> Path:
        return self.root_dir / f"{day.isoformat()}.jsonl"

    def _read_shard(self, path: Path) -> list[Signal]:
        if not path.exists():
            return []
        out: list[Signal] = []
        with path.open("r", encoding="utf-8") as fh:
            for lineno, line in enumerate(fh, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                    out.append(Signal.from_dict(payload))
                except (json.JSONDecodeError, KeyError, ValueError, TypeError) as exc:
                    warnings.warn(
                        f"skipping corrupt signal at {path.name}:{lineno}: {exc}",
                        RuntimeWarning,
                        stacklevel=3,
                    )
                    continue
        return out

    def _append_atomic(self, path: Path, signals: list[Signal]) -> None:
        """Append `signals` to `path` via a temp-rename dance.

        We copy any existing content of `path` plus the new rows into a
        sibling `.tmp`, then `os.replace` it over `path`. On crash we
        either kept the old file or atomically swapped to the new one —
        never a half-written final file.
        """
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        fd, tmp_name = tempfile.mkstemp(
            prefix=f"{path.stem}.", suffix=".jsonl.tmp", dir=str(path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                if existing:
                    fh.write(existing)
                    if not existing.endswith("\n"):
                        fh.write("\n")
                for s in signals:
                    fh.write(json.dumps(s.to_dict(), sort_keys=True))
                    fh.write("\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_name, path)
        except Exception:
            # Best-effort cleanup of the half-written tmp file.
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_iso(raw: str) -> dt.datetime:
    """Parse an ISO-8601 string; require tz-aware UTC."""
    parsed = dt.datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        raise ValueError(f"timestamp {raw!r} is missing timezone info")
    return parsed.astimezone(dt.timezone.utc)


def _scan_metadata_for_secrets(metadata: dict[str, Any]) -> None:
    """Walk `metadata` and reject if any string value looks like a secret."""
    stack: list[Any] = [metadata]
    while stack:
        node = stack.pop()
        if isinstance(node, str):
            for pat in _FORBIDDEN_PATTERNS:
                if pat.search(node):
                    raise ValueError(
                        "metadata contains credential-like string; refusing to "
                        "persist signal (see xtrade.research.signals security guard)"
                    )
        elif isinstance(node, dict):
            stack.extend(node.values())
        elif isinstance(node, (list, tuple, set)):
            stack.extend(node)
