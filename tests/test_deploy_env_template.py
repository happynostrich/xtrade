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

# A6 Bug 6: keys must match what `config/venues.*.yaml` reference. The
# legacy `HYPERLIQUID_TESTNET_PRIVATE_KEY` / `_WALLET_ADDRESS` pair was
# never read by `src/xtrade/config.py` and is removed.
# `BINANCE_FUTURES_TESTNET_*` is the canonical name (matches
# `config/venues.binance_futures.testnet.yaml`); the legacy shared
# `BINANCE_TESTNET_*` is kept as an OPTIONAL fallback (consumed by the
# Phase 0 backward-compat loader) and therefore NOT required.
REQUIRED_KEYS = {
    "BINANCE_FUTURES_TESTNET_API_KEY",
    "BINANCE_FUTURES_TESTNET_API_SECRET",
    "HYPERLIQUID_TESTNET_ACCOUNT_ADDRESS",
    "HYPERLIQUID_TESTNET_API_WALLET_KEY",
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
    """Phase 5 Task A5 replaced the legacy `XTRADE_ALLOW_MAINNET` placeholder
    with the unlock-token ritual. Whatever the env template uses, the
    mainnet-related line must be commented out by default so a fresh
    deployment is testnet-only without operator intervention.
    """
    text = ENV_FILE.read_text()
    assert "XTRADE_MAINNET_UNLOCK_TOKEN" in text
    for line in text.splitlines():
        if (
            "XTRADE_MAINNET_UNLOCK_TOKEN" in line
            and "=" in line
            and not line.lstrip().startswith("#")
        ):
            raise AssertionError(
                f"mainnet unlock token must be commented out, got: {line!r}"
            )


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
