"""Tests for `xtrade.obs.log_event` and the JSON envelope contract.

Brief §8 requires every structured log emitted by xtrade services to
be a single line of JSON with `ts` / `level` / `event` header keys.
These tests pin the format so a future refactor can't silently break
the journalctl + grep operator surface.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from decimal import Decimal
from pathlib import Path

import pytest

from xtrade.obs.log_event import emit_event, format_event


# --- envelope shape ----------------------------------------------------

def test_format_event_envelope_has_header_keys():
    line = format_event("foo.bar", baz=1)
    payload = json.loads(line)
    assert set(["ts", "level", "event"]).issubset(payload.keys())
    assert payload["event"] == "foo.bar"
    assert payload["level"] == "INFO"
    # ts must be ISO-Z (UTC)
    assert payload["ts"].endswith("Z")
    dt.datetime.fromisoformat(payload["ts"].replace("Z", "+00:00"))  # parseable


def test_format_event_is_single_line():
    line = format_event("foo", x="a\nb", y={"z": "c\nd"})
    assert "\n" not in line
    parsed = json.loads(line)
    # Newlines inside string values are preserved as escape sequences
    assert parsed["x"] == "a\nb"
    assert parsed["y"] == {"z": "c\nd"}


def test_format_event_header_order_then_alpha_extras():
    line = format_event("evt", zeta=1, alpha=2, mu=3)
    # Parse to dict (Python preserves insertion order); confirm header
    # comes first, extras sorted.
    parsed = json.loads(line, object_pairs_hook=list)
    keys = [k for k, _ in parsed]
    assert keys[:3] == ["ts", "level", "event"]
    assert keys[3:] == ["alpha", "mu", "zeta"]


def test_format_event_levels():
    for lvl, name in [
        (logging.DEBUG, "DEBUG"),
        (logging.INFO, "INFO"),
        (logging.WARNING, "WARNING"),
        (logging.ERROR, "ERROR"),
        (logging.CRITICAL, "CRITICAL"),
    ]:
        line = format_event("e", level=lvl)
        assert json.loads(line)["level"] == name


def test_format_event_now_override_is_utc():
    fixed = dt.datetime(2026, 5, 24, 12, 34, 56, tzinfo=dt.timezone.utc)
    line = format_event("e", now=fixed)
    assert json.loads(line)["ts"] == "2026-05-24T12:34:56Z"


def test_format_event_naive_now_treated_as_utc():
    naive = dt.datetime(2026, 5, 24, 0, 0, 0)
    line = format_event("e", now=naive)
    assert json.loads(line)["ts"].startswith("2026-05-24T00:00:00")


# --- coercion ---------------------------------------------------------

def test_format_event_coerces_decimal_to_string():
    line = format_event("e", qty=Decimal("0.0001"))
    assert json.loads(line)["qty"] == "0.0001"


def test_format_event_coerces_path_to_string():
    line = format_event("e", path=Path("/var/lib/xtrade/signals"))
    assert json.loads(line)["path"] == "/var/lib/xtrade/signals"


def test_format_event_coerces_datetime_to_iso_z():
    when = dt.datetime(2026, 1, 1, 0, 0, tzinfo=dt.timezone.utc)
    line = format_event("e", when=when)
    assert json.loads(line)["when"] == "2026-01-01T00:00:00Z"


def test_format_event_coerces_nested_structures():
    line = format_event("e", inner={"q": Decimal("1.5"), "p": Path("/a")})
    parsed = json.loads(line)
    assert parsed["inner"] == {"q": "1.5", "p": "/a"}


def test_format_event_drops_header_collisions():
    """Caller can't override ts via **fields (the helper builds it
    itself). `event` and `level` are dedicated parameters and python
    rejects the collision at call time."""
    line = format_event("real", ts="bad")
    parsed = json.loads(line)
    assert parsed["ts"] != "bad"
    # The ts must look like an ISO-Z timestamp, not the literal "bad".
    assert parsed["ts"].endswith("Z")


def test_format_event_handles_unserializable_with_str_fallback():
    class X:
        def __repr__(self):
            return "X()"

    line = format_event("e", x=X())
    assert json.loads(line)["x"] == "X()"


# --- emit_event behaviour ---------------------------------------------

def test_emit_event_writes_at_requested_level(caplog):
    logger = logging.getLogger("test.emit.level")
    logger.setLevel(logging.DEBUG)
    with caplog.at_level(logging.DEBUG, logger="test.emit.level"):
        emit_event(logger, "e1")
        emit_event(logger, "e2", level=logging.WARNING)
        emit_event(logger, "e3", level=logging.ERROR)
    levels = [r.levelno for r in caplog.records if r.name == "test.emit.level"]
    assert levels == [logging.INFO, logging.WARNING, logging.ERROR]


def test_emit_event_message_is_json(caplog):
    logger = logging.getLogger("test.emit.json")
    logger.setLevel(logging.DEBUG)
    with caplog.at_level(logging.DEBUG, logger="test.emit.json"):
        emit_event(logger, "e", k="v")
    msg = caplog.records[-1].getMessage()
    parsed = json.loads(msg)
    assert parsed["event"] == "e"
    assert parsed["k"] == "v"


# --- supervisor.* event shape pin -------------------------------------

def test_supervisor_event_names_documented():
    """Sanity: the event names supervisor.py emits all carry the
    `supervisor.` prefix so journalctl filters work."""
    from xtrade.live import supervisor as supmod

    src = Path(supmod.__file__).read_text()
    # Every emit_event call must have a quoted event name on the next
    # non-blank line. We do a coarse extraction.
    import re

    events = re.findall(r'emit_event\(\s*log\s*,\s*"([^"]+)"', src)
    assert events, "supervisor.py emits no events?"
    for ev in events:
        assert ev.startswith("supervisor."), f"non-supervisor event: {ev}"


def test_bridge_out_event_names_documented():
    from xtrade.bridge import openclaw_webhook as mod

    src = Path(mod.__file__).read_text()
    import re

    events = re.findall(r'emit_event\(\s*log\s*,\s*"([^"]+)"', src)
    assert events, "openclaw_webhook.py emits no events?"
    for ev in events:
        assert ev.startswith("bridge.out."), f"non-bridge.out event: {ev}"


def test_bridge_in_event_names_documented():
    from xtrade.bridge import inbound as mod

    src = Path(mod.__file__).read_text()
    import re

    events = re.findall(r'emit_event\(\s*log\s*,\s*"([^"]+)"', src)
    assert events, "inbound.py emits no events?"
    for ev in events:
        assert ev.startswith("bridge.in."), f"non-bridge.in event: {ev}"
