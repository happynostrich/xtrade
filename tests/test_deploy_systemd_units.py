"""Render + invariant tests for Phase 4 systemd unit templates.

Offline-only. We render each `.in` template with `envsubst` using a fixed
substitution set, then assert structural invariants:
 - no unresolved ${...} placeholders remain (we used the whitelist)
 - mandatory hardening / resource-cap directives are present
 - paths point at the expected install prefix
 - the bridge unit binds to localhost and denies all other addresses

Skips cleanly if `envsubst` is not installed.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATES_DIR = REPO_ROOT / "deploy" / "systemd"

ENV = {
    "OPT_XTRADE": "/opt/xtrade",
    "VAR_XTRADE": "/var/lib/xtrade",
    "ETC_XTRADE": "/etc/xtrade",
    "XTRADE_USER": "xtrade",
    "XTRADE_GROUP": "xtrade",
}
SUBST_VARS = "${OPT_XTRADE} ${VAR_XTRADE} ${ETC_XTRADE} ${XTRADE_USER} ${XTRADE_GROUP}"

UNITS = [
    "xtrade-supervisor.service",
    "xtrade-scanner.service",
    "xtrade-scanner.timer",
    "xtrade-bridge.service",
]


def _render(name: str) -> str:
    envsubst = shutil.which("envsubst")
    if envsubst is None:
        pytest.skip("envsubst not installed")
    src = TEMPLATES_DIR / f"{name}.in"
    assert src.is_file(), f"template missing: {src}"
    proc = subprocess.run(
        [envsubst, SUBST_VARS],
        input=src.read_text(),
        capture_output=True,
        text=True,
        env={**ENV, "PATH": "/usr/bin:/bin"},
        check=True,
    )
    return proc.stdout


@pytest.mark.parametrize("name", UNITS)
def test_template_exists(name: str) -> None:
    assert (TEMPLATES_DIR / f"{name}.in").is_file()


@pytest.mark.parametrize("name", UNITS)
def test_template_renders_without_residual_placeholders(name: str) -> None:
    rendered = _render(name)
    leftover = re.findall(r"\$\{[A-Z_]+\}", rendered)
    assert leftover == [], f"{name}: unresolved placeholders {leftover}"


@pytest.mark.parametrize("name", UNITS)
def test_template_substitutes_install_paths(name: str) -> None:
    rendered = _render(name)
    if name == "xtrade-scanner.timer":
        # timer references the .service unit by name, not by absolute path
        return
    assert ENV["OPT_XTRADE"] in rendered, f"{name}: missing OPT_XTRADE path"


def test_supervisor_unit_has_resource_caps() -> None:
    text = _render("xtrade-supervisor.service")
    for directive in (
        "MemoryMax=1500M",
        "MemorySwapMax=0",
        "CPUQuota=80%",
        "OOMScoreAdjust=500",
        "Restart=on-failure",
        "ProtectSystem=strict",
        "NoNewPrivileges=true",
    ):
        assert directive in text, f"supervisor missing {directive!r}"


def test_supervisor_unit_writable_paths_are_scoped() -> None:
    text = _render("xtrade-supervisor.service")
    rw_lines = [l for l in text.splitlines() if l.startswith("ReadWritePaths=")]
    assert rw_lines, "ReadWritePaths missing"
    line = rw_lines[0]
    assert ENV["VAR_XTRADE"] in line
    assert "/run/xtrade" in line
    # supervisor must NOT have write access to /etc or /opt
    assert ENV["ETC_XTRADE"] not in line
    assert f"{ENV['OPT_XTRADE']} " not in line


def test_bridge_unit_localhost_only() -> None:
    text = _render("xtrade-bridge.service")
    # The unit must explicitly deny all addresses and allow only loopback,
    # and the ExecStart must bind to 127.0.0.1 (not 0.0.0.0).
    assert "IPAddressDeny=any" in text
    assert "IPAddressAllow=127.0.0.0/8" in text
    assert "--bind 127.0.0.1" in text
    assert "0.0.0.0" not in text


def test_bridge_unit_caps_smaller_than_supervisor() -> None:
    text = _render("xtrade-bridge.service")
    assert "MemoryMax=200M" in text
    assert "CPUQuota=20%" in text


def test_scanner_service_is_oneshot() -> None:
    text = _render("xtrade-scanner.service")
    assert "Type=oneshot" in text
    # oneshot must NOT have Restart=on-failure (systemd will reject the combo)
    assert "Restart=on-failure" not in text


def test_scanner_timer_cadence() -> None:
    text = _render("xtrade-scanner.timer")
    assert "OnUnitActiveSec=5min" in text
    assert "Persistent=true" in text
    assert "Unit=xtrade-scanner.service" in text


def test_all_units_disable_capabilities() -> None:
    for name in ("xtrade-supervisor.service", "xtrade-scanner.service", "xtrade-bridge.service"):
        text = _render(name)
        assert "CapabilityBoundingSet=" in text, f"{name}: missing CapabilityBoundingSet"
        assert "AmbientCapabilities=" in text, f"{name}: missing AmbientCapabilities"
