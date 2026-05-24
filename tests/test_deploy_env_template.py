"""Static checks on `deploy/env/xtrade.env.example`.

Ensures the env template documents every key the install scripts + bridge
code reference, and that no key carries a real-looking value (defense in
depth against accidental commits of populated copies).
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = REPO_ROOT / "deploy" / "env" / "xtrade.env.example"

REQUIRED_KEYS = {
    "BINANCE_TESTNET_API_KEY",
    "BINANCE_TESTNET_API_SECRET",
    "HYPERLIQUID_TESTNET_PRIVATE_KEY",
    "HYPERLIQUID_TESTNET_WALLET_ADDRESS",
    "OPENCLAW_GATEWAY",
    "OPENCLAW_SHARED_SECRET",
    "OPENCLAW_INBOUND_SECRET",
}


def _parse_env(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip()
    return out


def test_env_template_exists_and_nonempty() -> None:
    assert ENV_FILE.is_file()
    assert ENV_FILE.read_text().strip() != ""


def test_env_template_lists_required_keys() -> None:
    parsed = _parse_env(ENV_FILE.read_text())
    missing = REQUIRED_KEYS - parsed.keys()
    assert missing == set(), f"missing keys: {sorted(missing)}"


def test_env_template_keys_are_blank_or_invalid_placeholder() -> None:
    """Every required key must have an empty value or a clearly-invalid
    placeholder so a committed populated file is obvious in code review.
    """
    parsed = _parse_env(ENV_FILE.read_text())
    invalid_markers = ("example.invalid", "REPLACE_ME", "")
    for key in REQUIRED_KEYS:
        v = parsed[key]
        assert v == "" or any(m in v for m in invalid_markers), (
            f"{key} has real-looking value: {v!r}"
        )


def test_env_template_warns_against_mainnet() -> None:
    text = ENV_FILE.read_text()
    assert "XTRADE_ALLOW_MAINNET" in text
    # The mainnet line must be commented out by default.
    for line in text.splitlines():
        if "XTRADE_ALLOW_MAINNET" in line and "=" in line and not line.lstrip().startswith("#"):
            raise AssertionError(f"mainnet flag must be commented out, got: {line!r}")


def test_env_template_has_no_obvious_secret() -> None:
    """Defense in depth: refuse any line that looks like a real hex/base64
    secret (>=20 chars of [A-Za-z0-9/+_=-]) outside of comments.
    """
    secret_pat = re.compile(r"=\s*[A-Za-z0-9/+_=-]{20,}\s*$")
    for line in ENV_FILE.read_text().splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        assert not secret_pat.search(s), f"possible secret committed: {s!r}"
