"""Tests for `xtrade.live.sentinel.Sentinel` (Phase 4 Task 5 / T5).

The sentinel is the file-backed pause/resume flag the supervisor
consults each poll iteration. Tests verify:

- Defaults: missing file means not paused, no body.
- Round-trip: `pause(reason)` is visible to `paused()` + `state()`.
- Resume: idempotent; second resume is a no-op (False).
- Atomicity: a real file appears at the target path after pause; no
  `.tmp` debris is left behind.
- Auto-parent-dir creation: pausing onto a path whose parent does not
  exist creates the parent automatically (production: `/run/xtrade`
  may not yet exist on a fresh tmpfs).
- Corrupt body: a truncated/invalid sentinel still counts as paused
  (the file's existence is the signal), and `state()` returns a
  marker dict rather than raising.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from xtrade.live.sentinel import Sentinel


UTC = dt.timezone.utc


# ---- paused() ------------------------------------------------------------


def test_paused_false_when_no_file(tmp_path: Path) -> None:
    s = Sentinel(tmp_path / "paused.flag")
    assert s.paused() is False
    assert s.state() is None


def test_paused_true_after_pause(tmp_path: Path) -> None:
    s = Sentinel(tmp_path / "paused.flag")
    s.pause(reason="manual drill")
    assert s.paused() is True


# ---- pause / state round-trip --------------------------------------------


def test_state_round_trip(tmp_path: Path) -> None:
    s = Sentinel(tmp_path / "paused.flag")
    when = dt.datetime(2026, 5, 24, 12, 0, 0, tzinfo=UTC)
    body = s.pause(reason="weekly drill", now=when)
    assert body["reason"] == "weekly drill"
    assert body["paused_at"] == when.isoformat()
    read_back = s.state()
    assert read_back == body


def test_pause_overwrites_existing(tmp_path: Path) -> None:
    """Re-pausing replaces the existing body (timestamps + reasons update)."""
    s = Sentinel(tmp_path / "paused.flag")
    s.pause(reason="first", now=dt.datetime(2026, 5, 24, 12, 0, tzinfo=UTC))
    body2 = s.pause(reason="second", now=dt.datetime(2026, 5, 24, 13, 0, tzinfo=UTC))
    assert body2["reason"] == "second"
    assert s.state() == body2


# ---- resume --------------------------------------------------------------


def test_resume_removes_file(tmp_path: Path) -> None:
    s = Sentinel(tmp_path / "paused.flag")
    s.pause()
    assert s.resume() is True
    assert s.paused() is False


def test_resume_idempotent_when_not_paused(tmp_path: Path) -> None:
    s = Sentinel(tmp_path / "paused.flag")
    # Calling resume() on a non-existent file returns False (not an error).
    assert s.resume() is False
    assert s.paused() is False


# ---- atomicity / parent dir ----------------------------------------------


def test_pause_creates_missing_parent_dir(tmp_path: Path) -> None:
    """Production sentinel lives at /run/xtrade/paused.flag; tmpfs dir may
    not exist at first pause."""
    target = tmp_path / "missing_parent" / "paused.flag"
    assert not target.parent.exists()
    s = Sentinel(target)
    s.pause(reason="bootstrap")
    assert target.exists()
    assert target.parent.is_dir()


def test_pause_leaves_no_tmp_debris(tmp_path: Path) -> None:
    """Atomic write must not leave the `.json.tmp` file behind."""
    target = tmp_path / "paused.flag"
    s = Sentinel(target)
    s.pause(reason="debris check")
    leftovers = [
        p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")
    ]
    assert leftovers == []


def test_pause_writes_sorted_json(tmp_path: Path) -> None:
    """Body must be a valid JSON object with both keys."""
    s = Sentinel(tmp_path / "paused.flag")
    s.pause(reason="json-check", now=dt.datetime(2026, 5, 24, tzinfo=UTC))
    on_disk = json.loads((tmp_path / "paused.flag").read_text(encoding="utf-8"))
    assert set(on_disk.keys()) == {"paused_at", "reason"}
    assert on_disk["reason"] == "json-check"


# ---- corrupt body --------------------------------------------------------


def test_corrupt_body_still_paused(tmp_path: Path) -> None:
    """A zero-byte / garbage sentinel still counts as paused; the file's
    existence is the pause signal."""
    target = tmp_path / "paused.flag"
    target.write_text("{not json", encoding="utf-8")
    s = Sentinel(target)
    assert s.paused() is True
    body = s.state()
    assert body is not None
    assert body.get("reason") == "<unreadable sentinel>"


def test_empty_body_still_paused(tmp_path: Path) -> None:
    target = tmp_path / "paused.flag"
    target.write_text("", encoding="utf-8")
    s = Sentinel(target)
    assert s.paused() is True
    body = s.state()
    assert body is not None and body.get("paused_at") is None
