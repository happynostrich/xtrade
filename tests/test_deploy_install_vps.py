"""Static checks for Phase 4 Task 1 install scripts.

Offline-only: we never actually invoke the scripts (they touch /opt, /etc,
/var, systemctl). We assert:
 - shell syntax is valid (`bash -n`)
 - executable bit is set
 - help block printable
 - shellcheck is clean if shellcheck is installed (skipped otherwise)
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = [
    REPO_ROOT / "scripts" / "phase4" / "install_vps.sh",
    REPO_ROOT / "scripts" / "phase4" / "uninstall_vps.sh",
    REPO_ROOT / "scripts" / "phase4" / "01_drill_sigkill.sh",
    REPO_ROOT / "scripts" / "phase4" / "03_drill_oom.sh",
    REPO_ROOT / "scripts" / "phase4" / "04_drill_network.sh",
    REPO_ROOT / "scripts" / "phase4" / "05_smoke_postdeploy.sh",
    REPO_ROOT / "scripts" / "phase4" / "06_drill_openclaw_outage.sh",
]


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_script_exists_and_executable(script: Path) -> None:
    assert script.is_file(), f"missing: {script}"
    mode = script.stat().st_mode
    assert mode & stat.S_IXUSR, f"not executable: {script}"


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_script_bash_syntax(script: Path) -> None:
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash not available")
    result = subprocess.run(
        [bash, "-n", str(script)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"bash -n failed for {script.name}:\n{result.stderr}"


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_script_uses_strict_mode(script: Path) -> None:
    text = script.read_text()
    # smoke script deliberately uses `set -uo pipefail` (no -e) so every
    # check runs and aggregates failures; install/uninstall use -euo.
    has_strict = "set -euo pipefail" in text or "set -uo pipefail" in text
    assert has_strict, f"{script.name} missing strict-mode preamble"


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_script_shellcheck_clean_if_available(script: Path) -> None:
    sc = shutil.which("shellcheck")
    if sc is None:
        pytest.skip("shellcheck not installed")
    result = subprocess.run(
        [sc, "--severity=warning", str(script)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"shellcheck warnings for {script.name}:\n{result.stdout}{result.stderr}"


def test_install_script_documents_required_args() -> None:
    text = (REPO_ROOT / "scripts" / "phase4" / "install_vps.sh").read_text()
    # The help block (lines 2-20) must mention --release-tarball, the only
    # required arg per the script's prerequisite checks.
    head = "\n".join(text.splitlines()[:25])
    assert "--release-tarball" in head
    assert "Exit codes" in head


def test_uninstall_script_preserves_state_by_default() -> None:
    text = (REPO_ROOT / "scripts" / "phase4" / "uninstall_vps.sh").read_text()
    # The default (no --purge) path must NOT rm -rf /var/lib/xtrade.
    # We assert the rm of VAR_XTRADE only appears inside a --purge branch.
    assert 'rm -rf "$VAR_XTRADE"' in text
    purge_idx = text.index("if [[ $PURGE -eq 1 ]]")
    purge_block = text[purge_idx:]
    assert 'rm -rf "$VAR_XTRADE"' in purge_block
    # And NOT outside the purge block:
    pre_block = text[:purge_idx]
    assert 'rm -rf "$VAR_XTRADE"' not in pre_block


def test_install_script_refuses_non_root() -> None:
    # Static check: the EUID guard is present (we don't actually invoke it).
    text = (REPO_ROOT / "scripts" / "phase4" / "install_vps.sh").read_text()
    assert '"${EUID:-$(id -u)}" -ne 0' in text
    assert "must run as root" in text


def test_install_script_resolves_uv_explicitly() -> None:
    text = (REPO_ROOT / "scripts" / "phase4" / "install_vps.sh").read_text()
    # uv path must be absolute (no PATH ambiguity in systemd context); the
    # installer must also avoid pulling system Python.
    assert "/usr/local/bin/uv" in text
    assert 'python install 3.12' in text
    assert 'venv --python 3.12' in text


def test_install_script_uses_envsubst_with_whitelist() -> None:
    """We must restrict envsubst to a known variable list so that stray
    ${X} fragments in the unit templates (e.g. systemd %i tokens we may
    add later) are not silently consumed.
    """
    text = (REPO_ROOT / "scripts" / "phase4" / "install_vps.sh").read_text()
    assert "SUBST_VARS=" in text
    assert 'envsubst "$SUBST_VARS"' in text


# --- A6 regressions ------------------------------------------------------


def _install_text() -> str:
    return (REPO_ROOT / "scripts" / "phase4" / "install_vps.sh").read_text()


def test_install_script_pins_uv_python_install_dir() -> None:
    """A6 Bug 1: uv's default Python install location is `~/.local/share/
    uv/python` which becomes `/root/...` when the installer runs as root.
    The xtrade system user cannot traverse mode-0550 `/root`, so systemd
    exec fails with status=203/EXEC. Installer must pin Python to a
    world-readable path and make it traversable.
    """
    text = _install_text()
    assert "UV_PYTHON_INSTALL_DIR" in text, (
        "installer must set UV_PYTHON_INSTALL_DIR (A6 Bug 1)"
    )
    assert "/opt/uv" in text, "installer must put uv toolchain under /opt/uv"
    assert "chmod -R a+rX /opt/uv" in text, (
        "installer must chmod /opt/uv traversable/readable for xtrade user"
    )


def test_install_script_seeds_supervisor_yaml() -> None:
    """A6 Bug 2: without a seeded `/etc/xtrade/supervisor.yaml` the
    supervisor unit crashes immediately with FileNotFoundError.
    """
    text = _install_text()
    assert "supervisor.example.yaml" in text, (
        "installer must seed supervisor.yaml from the example template "
        "(A6 Bug 2)"
    )


def test_install_script_creates_numba_cache_dir() -> None:
    """A6 Bug 3 (part 1): vectorbt's eager numba imports need a writable
    cache dir under ProtectSystem=strict + ProtectHome=yes.
    """
    text = _install_text()
    assert "numba_cache" in text, (
        "installer must create /var/lib/xtrade/numba_cache (A6 Bug 3)"
    )


def test_install_script_appends_numba_env_vars() -> None:
    """A6 Bug 3 (part 2): supervisor + bridge processes both need
    NUMBA_CACHE_DIR and HOME set in /etc/xtrade/env. Without them numba
    falls back to its in-tree locator which is read-only.
    """
    text = _install_text()
    assert "NUMBA_CACHE_DIR=" in text, "installer must append NUMBA_CACHE_DIR"
    assert "HOME=" in text, "installer must append HOME to env file"


def test_install_script_post_fail_hint_present() -> None:
    """A6 Bug 4-style: when the post-start health check fails, the
    installer must print an actionable triage checklist so the operator
    isn't stuck staring at `is-active` exit codes.
    """
    text = _install_text()
    assert "Operator triage checklist" in text
    # Must reference the key diagnostic commands.
    assert "journalctl -u xtrade-supervisor" in text
    assert "readlink -f" in text  # venv interpreter resolve hint


def test_uninstall_purge_removes_uv_toolchain() -> None:
    """A6 Bug 1: uninstall --purge must clean up /opt/uv we created;
    the default (no --purge) path must NOT touch it (defense in depth
    against accidentally breaking sibling tools that share /opt/uv).
    """
    text = (REPO_ROOT / "scripts" / "phase4" / "uninstall_vps.sh").read_text()
    assert "rm -rf /opt/uv" in text
    purge_idx = text.index("if [[ $PURGE -eq 1 ]]")
    purge_block = text[purge_idx:]
    pre_block = text[:purge_idx]
    assert "rm -rf /opt/uv" in purge_block
    assert "rm -rf /opt/uv" not in pre_block
