"""Tests for scripts/phase4/02_state_gc.py.

Imports the script as a module via importlib so the top-level numeric
filename doesn't break a normal `import`.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "phase4" / "02_state_gc.py"


def _load_state_gc():
    spec = importlib.util.spec_from_file_location("state_gc_mod", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["state_gc_mod"] = module
    spec.loader.exec_module(module)
    return module


state_gc = _load_state_gc()


# --- parse_shard_date ---------------------------------------------------

def test_parse_shard_date_valid():
    assert state_gc.parse_shard_date("2025-04-15.jsonl") == dt.date(2025, 4, 15)


@pytest.mark.parametrize(
    "name",
    [
        "2025-04-15",                # no extension
        "2025-04-15.jsonl.gz",       # extra suffix
        "approvals-2025-04-15.jsonl",
        "2025-13-01.jsonl",          # bad month
        "2025-02-30.jsonl",          # bad day
        ".jsonl",
        "",
    ],
)
def test_parse_shard_date_invalid(name):
    assert state_gc.parse_shard_date(name) is None


# --- find_archivable ----------------------------------------------------

def test_find_archivable_strict_older(tmp_path):
    today = dt.date(2025, 4, 15)
    signals = tmp_path / "signals"
    signals.mkdir()
    # Files: old (must move), boundary (== cutoff, must NOT move),
    # recent (must NOT move), non-matching name (ignored).
    (signals / "2024-01-01.jsonl").write_text("{}\n")          # old
    (signals / "2025-01-15.jsonl").write_text("{}\n")          # cutoff (today-90)
    (signals / "2025-04-14.jsonl").write_text("{}\n")          # recent
    (signals / "README.md").write_text("ignore me\n")          # ignored
    (signals / "subdir").mkdir()                               # ignored

    out = state_gc.find_archivable(signals, today=today, days=90)
    names = sorted(p.name for p in out)
    assert names == ["2024-01-01.jsonl"]


def test_find_archivable_missing_root(tmp_path):
    assert state_gc.find_archivable(tmp_path / "nope", today=dt.date.today(), days=90) == []


# --- archive_files ------------------------------------------------------

def test_archive_files_moves_atomically(tmp_path):
    signals = tmp_path / "signals"
    signals.mkdir()
    archive = tmp_path / "archive" / "signals"

    src = signals / "2024-01-01.jsonl"
    src.write_text("row\n")

    moved, errors = state_gc.archive_files([src], archive_root=archive)
    assert errors == []
    assert moved == [archive / "2024-01-01.jsonl"]
    assert not src.exists()
    assert (archive / "2024-01-01.jsonl").read_text() == "row\n"


def test_archive_files_dry_run_does_not_touch(tmp_path):
    signals = tmp_path / "signals"
    signals.mkdir()
    archive = tmp_path / "archive" / "signals"

    src = signals / "2024-01-01.jsonl"
    src.write_text("row\n")

    moved, errors = state_gc.archive_files([src], archive_root=archive, dry_run=True)
    assert errors == []
    assert moved == [archive / "2024-01-01.jsonl"]
    assert src.exists()
    assert not archive.exists()


def test_archive_files_refuses_overwrite(tmp_path):
    signals = tmp_path / "signals"
    signals.mkdir()
    archive = tmp_path / "archive" / "signals"
    archive.mkdir(parents=True)

    src = signals / "2024-01-01.jsonl"
    src.write_text("new\n")
    existing = archive / "2024-01-01.jsonl"
    existing.write_text("old\n")

    moved, errors = state_gc.archive_files([src], archive_root=archive)
    assert moved == []
    assert len(errors) == 1
    assert "refusing to overwrite" in errors[0][1]
    # Source still in place, existing archive unchanged.
    assert src.read_text() == "new\n"
    assert existing.read_text() == "old\n"


def test_archive_files_empty_input(tmp_path):
    archive = tmp_path / "archive" / "signals"
    moved, errors = state_gc.archive_files([], archive_root=archive)
    assert moved == []
    assert errors == []
    # Should not create archive dir for empty input.
    assert not archive.exists()


# --- main() integration -------------------------------------------------

def test_main_happy_path(tmp_path, capsys):
    signals = tmp_path / "signals"
    signals.mkdir()
    (signals / "2024-01-01.jsonl").write_text("a\n")
    (signals / "2025-04-14.jsonl").write_text("b\n")

    rc = state_gc.main(
        [
            "--var",
            str(tmp_path),
            "--days",
            "90",
            "--today",
            "2025-04-15",
        ]
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "moved" in out
    assert (tmp_path / "archive" / "signals" / "2024-01-01.jsonl").exists()
    assert (signals / "2025-04-14.jsonl").exists()


def test_main_nothing_to_do(tmp_path, capsys):
    signals = tmp_path / "signals"
    signals.mkdir()
    (signals / "2025-04-14.jsonl").write_text("b\n")

    rc = state_gc.main(
        [
            "--var",
            str(tmp_path),
            "--days",
            "90",
            "--today",
            "2025-04-15",
        ]
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "nothing to archive" in out


def test_main_dry_run(tmp_path, capsys):
    signals = tmp_path / "signals"
    signals.mkdir()
    (signals / "2024-01-01.jsonl").write_text("a\n")

    rc = state_gc.main(
        [
            "--var",
            str(tmp_path),
            "--days",
            "90",
            "--today",
            "2025-04-15",
            "--dry-run",
        ]
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "would move" in out
    assert (signals / "2024-01-01.jsonl").exists()
    assert not (tmp_path / "archive").exists()


def test_main_bad_days(tmp_path, capsys):
    rc = state_gc.main(["--var", str(tmp_path), "--days", "0"])
    err = capsys.readouterr().err
    assert rc == 2
    assert "must be positive" in err


def test_main_missing_var(tmp_path, capsys):
    rc = state_gc.main(["--var", str(tmp_path / "nope"), "--days", "90"])
    err = capsys.readouterr().err
    assert rc == 2
    assert "state root not found" in err


def test_main_bad_today(tmp_path, capsys):
    rc = state_gc.main(
        ["--var", str(tmp_path), "--days", "90", "--today", "not-a-date"]
    )
    err = capsys.readouterr().err
    assert rc == 2
    assert "not ISO" in err
