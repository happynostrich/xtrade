"""Static checks for docs/phase4_runbook_vps.md.

Phase 4 brief §5 Task 6 says the runbook must enumerate 4 drill scripts
with the 预期/准备/触发/观察/判据/通过-未通过/日志样本 template, and §8
delivery #8 requires install/upgrade/rollback/stop/扩盘 sections. We
verify the document exists and contains those structural anchors so
the runbook can't silently drift away from the brief.
"""

from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
RUNBOOK = REPO_ROOT / "docs" / "phase4_runbook_vps.md"


def _runbook_text() -> str:
    assert RUNBOOK.is_file(), f"runbook missing: {RUNBOOK}"
    return RUNBOOK.read_text(encoding="utf-8")


def test_runbook_present_and_nontrivial() -> None:
    text = _runbook_text()
    assert len(text) > 1000, "runbook is suspiciously short"


def test_runbook_lifecycle_sections() -> None:
    """§8 交付物 #8: install / upgrade / rollback / stop / 扩盘."""
    text = _runbook_text()
    # We check section headings (## ...) rather than substrings so a
    # rename to a non-section context can't sneak past.
    for heading in (
        "## 1. Install",
        "## 2. Upgrade",
        "## 3. Rollback",
        "## 4. Stop",
        "## 5. 扩盘",
    ):
        assert heading in text, f"runbook missing heading: {heading}"


def test_runbook_state_gc_section() -> None:
    text = _runbook_text()
    assert "02_state_gc.py" in text
    assert "90" in text  # 90-day retention


def test_runbook_has_four_drills() -> None:
    text = _runbook_text()
    for header in (
        "### Drill 1 — SIGKILL supervisor",
        "### Drill 2 — cgroup OOM",
        "### Drill 3 — 网络抖断",
        "### Drill 4 — openclaw 5xx",
    ):
        assert header in text, f"runbook missing drill: {header}"


def test_runbook_drills_use_brief_template() -> None:
    """Brief §5 Task 6 fixes the per-drill section template."""
    text = _runbook_text()
    # Each label must appear at least 4 times (once per drill).
    for label in ("**预期**", "**准备**", "**触发**", "**观察**", "**判据**", "**通过/未通过**", "**日志样本**"):
        count = text.count(label)
        assert count >= 4, f"runbook template label {label} appears {count} times (need >= 4)"


def test_runbook_references_drill_scripts() -> None:
    text = _runbook_text()
    for script in (
        "01_drill_sigkill.sh",
        "03_drill_oom.sh",
        "04_drill_network.sh",
        "06_drill_openclaw_outage.sh",
    ):
        assert script in text, f"runbook does not reference {script}"


def test_runbook_calls_out_local_approval_fallback() -> None:
    """Drill 4 must document the local `xtrade approve confirm` fallback
    per brief §5 Task 6 drill 4 expectation."""
    text = _runbook_text()
    assert "xtrade approve confirm" in text


def test_runbook_warns_destructive_drills_require_commit() -> None:
    """Network + openclaw drills are DRY-RUN by default; the runbook
    must surface --commit so an operator doesn't assume the dry-run
    actually mutated state."""
    text = _runbook_text()
    assert "--commit" in text
