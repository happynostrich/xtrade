"""Cross-check `config/venues.*.yaml` against `deploy/env/xtrade.env.example`.

A6 Bug 6: the env template drifted from the venues YAML naming. Each
`*_env: VAR_NAME` reference in a venues yaml names the exact env var
that `xtrade.config._resolve_env_ref` will look up at startup. If the
template doesn't document that name, a fresh VPS install fails with
`MissingCredentialError` and the operator has to reverse-engineer the
naming convention from source.

This test parses every `config/venues.*.yaml`, walks every value whose
key ends in `_env`, and asserts each name appears (commented or
uncommented) in `deploy/env/xtrade.env.example`. Mere presence is
enough: the template uses inline comments + section headers to mark
which keys are required vs optional, and `test_deploy_env_template.py`
already enforces the required subset.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = REPO_ROOT / "config"
ENV_FILE = REPO_ROOT / "deploy" / "env" / "xtrade.env.example"


def _walk_env_refs(node: Any) -> list[str]:
    """Return every env-var name referenced under an `*_env` key in a
    nested mapping/list structure."""
    refs: list[str] = []
    if isinstance(node, dict):
        for k, v in node.items():
            if isinstance(k, str) and k.endswith("_env") and isinstance(v, str):
                refs.append(v)
            else:
                refs.extend(_walk_env_refs(v))
    elif isinstance(node, list):
        for item in node:
            refs.extend(_walk_env_refs(item))
    return refs


def _venues_yamls() -> list[Path]:
    """All `config/venues.*.yaml` files except the intentional stub
    `venues.testnet.yaml` (Phase 3.5: replaced by per-venue siblings;
    loading it raises ConfigError by design)."""
    return [
        p
        for p in sorted(CONFIG_DIR.glob("venues.*.yaml"))
        if p.name != "venues.testnet.yaml"
    ]


def _env_template_keys() -> set[str]:
    """Return every env-var key documented in the template, whether
    commented out or not. Lines like `# FOO=bar` count as documented."""
    keys: set[str] = set()
    for raw in ENV_FILE.read_text().splitlines():
        line = raw.strip()
        if not line:
            continue
        # Strip a single leading `#` and surrounding whitespace so that
        # commented-out keys still count as documented.
        if line.startswith("#"):
            line = line.lstrip("#").strip()
        if "=" not in line:
            continue
        k = line.partition("=")[0].strip()
        # Require uppercase identifier shape (rules out narrative-style
        # comments that happen to contain an `=`).
        if k and k.replace("_", "").isalnum() and k.isupper():
            keys.add(k)
    return keys


def test_venues_yamls_present() -> None:
    """Sanity: we found at least the three per-venue siblings."""
    names = {p.name for p in _venues_yamls()}
    assert {
        "venues.binance_spot.testnet.yaml",
        "venues.binance_futures.testnet.yaml",
        "venues.hyperliquid.testnet.yaml",
    }.issubset(names), f"missing venues yamls; found {sorted(names)}"


@pytest.mark.parametrize("yaml_path", _venues_yamls(), ids=lambda p: p.name)
def test_venues_yaml_env_refs_documented_in_template(yaml_path: Path) -> None:
    """Every `*_env: VAR_NAME` reference must be documented (commented
    or uncommented) in `deploy/env/xtrade.env.example`. Otherwise a
    first-time operator can't tell what to set."""
    raw = yaml.safe_load(yaml_path.read_text()) or {}
    refs = _walk_env_refs(raw)
    if not refs:
        pytest.skip(f"{yaml_path.name}: no env refs")
    documented = _env_template_keys()
    missing = sorted(set(refs) - documented)
    assert not missing, (
        f"{yaml_path.name}: env-var(s) not documented in template "
        f"{ENV_FILE.relative_to(REPO_ROOT)}: {missing}. Add a line per "
        f"missing key (blank value, optional `# comment` prefix)."
    )


def test_template_does_not_document_dead_env_vars() -> None:
    """Defense in depth: the template must NOT carry the legacy
    `HYPERLIQUID_TESTNET_PRIVATE_KEY` / `_WALLET_ADDRESS` pair (A6 Bug
    6); they were never read by any loader and just confused operators."""
    text = ENV_FILE.read_text()
    for dead in ("HYPERLIQUID_TESTNET_PRIVATE_KEY", "HYPERLIQUID_TESTNET_WALLET_ADDRESS"):
        # Allow these to appear inside a free-form comment paragraph
        # (e.g. release notes), but reject them as actual `KEY=` lines.
        for raw in text.splitlines():
            line = raw.strip().lstrip("#").strip()
            if "=" not in line:
                continue
            k = line.partition("=")[0].strip()
            assert k != dead, (
                f"env template still documents removed key {dead!r}; "
                f"see A6 Bug 6"
            )
