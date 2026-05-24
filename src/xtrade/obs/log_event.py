"""Structured single-line JSON log emission.

Used by `xtrade.live.supervisor`, `xtrade.bridge.openclaw_webhook`,
`xtrade.bridge.inbound` and the scanner CLI to satisfy
`docs/phase4_brief.md` §8:

>  关键 journalctl 标签:`xtrade.supervisor`、`xtrade.bridge.out`、
>  `xtrade.bridge.in`、`xtrade.scanner`,每条结构化 log 是单行 JSON
>  (`{"ts":"...","level":"INFO","event":"...","key1":"..."}`),
>  便于 grep。

The envelope is deterministic across services:

    {"ts": "<iso-utc-Z>", "level": "INFO|WARNING|ERROR",
     "event": "<dot.path>", ...}

Any extra kwargs are sorted alphabetically after the three header
keys. Values must be JSON-serialisable (we coerce a few common
non-JSON types — Decimal, Path, datetime — defensively so a typo
upstream doesn't crash the dispatch loop).
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from decimal import Decimal
from pathlib import Path
from typing import Any


_HEADER_KEYS = ("ts", "level", "event")
_LEVEL_NAMES = {
    logging.DEBUG: "DEBUG",
    logging.INFO: "INFO",
    logging.WARNING: "WARNING",
    logging.ERROR: "ERROR",
    logging.CRITICAL: "CRITICAL",
}


def _coerce(value: Any) -> Any:
    """JSON-friendly best-effort coercion. Never raises."""
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, Decimal):
        # Decimals serialise to str to keep precision intact across the
        # journal (operators reading e.g. order quantities should not
        # see them silently float-rounded).
        return str(value)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dt.datetime):
        return value.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, (list, tuple)):
        return [_coerce(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _coerce(v) for k, v in value.items()}
    # Fallback: stringify. We deliberately don't raise here — a log
    # call site is the wrong place to discover a non-serialisable type.
    return str(value)


def format_event(
    event: str,
    *,
    level: int = logging.INFO,
    now: dt.datetime | None = None,
    **fields: Any,
) -> str:
    """Return the JSON line string for `event` + `fields`.

    Pure function; exposed for tests so we don't have to capture
    handlers.
    """
    if now is None:
        now = dt.datetime.now(tz=dt.timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=dt.timezone.utc)
    else:
        now = now.astimezone(dt.timezone.utc)
    header = {
        "ts": now.isoformat().replace("+00:00", "Z"),
        "level": _LEVEL_NAMES.get(level, logging.getLevelName(level)),
        "event": event,
    }
    extra = {k: _coerce(v) for k, v in fields.items() if k not in _HEADER_KEYS}
    # Header first (insertion order), then alphabetical extras for
    # determinism. json.dumps(sort_keys=True) would re-sort the
    # header; emit by hand.
    merged: dict[str, Any] = {}
    merged.update(header)
    for key in sorted(extra):
        merged[key] = extra[key]
    return json.dumps(merged, ensure_ascii=False, separators=(",", ":"))


def emit_event(
    logger: logging.Logger,
    event: str,
    *,
    level: int = logging.INFO,
    now: dt.datetime | None = None,
    **fields: Any,
) -> None:
    """Emit `event` as a single JSON line on `logger` at `level`.

    Use the supervisor / bridge / scanner module-level logger so
    journald picks up the right SyslogIdentifier.
    """
    line = format_event(event, level=level, now=now, **fields)
    logger.log(level, "%s", line)
