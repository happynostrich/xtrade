"""Tests for `config/venues.binance_futures.mainnet.yaml` (Phase 6 T1).

Brief §5 T1 contract surfaces under test:
  (a) yaml parses with `environment="LIVE"` and round-trips through
      `xtrade.config.load_venues` into a `BinanceFuturesConfig`.
  (b) any plaintext `api_key:` / `api_secret:` field is rejected by
      the schema (no literal secrets ever).
  (c) `_resolve_env_ref` env-missing message names the env var and
      the env file path (Phase 5 Bug 7 regression guard).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from xtrade.config import (
    BinanceFuturesConfig,
    ConfigError,
    MissingCredentialError,
    VenuesConfig,
    load_venues,
)


_REPO_MAINNET_YAML = (
    Path(__file__).resolve().parents[1]
    / "config"
    / "venues.binance_futures.mainnet.yaml"
)

_MAINNET_API_KEY_ENV = "XTRADE_MAINNET_BINANCE_FUTURES_API_KEY"
_MAINNET_API_SECRET_ENV = "XTRADE_MAINNET_BINANCE_FUTURES_API_SECRET"


def _set_mainnet_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_MAINNET_API_KEY_ENV, "fake-mainnet-key-do-not-use")
    monkeypatch.setenv(_MAINNET_API_SECRET_ENV, "fake-mainnet-secret-do-not-use")


# ---------------------------------------------------------------------------
# (a) Schema parse — repo yaml must always load with the right shape
# ---------------------------------------------------------------------------


def test_repo_mainnet_yaml_loads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_mainnet_env(monkeypatch)
    cfg = load_venues(_REPO_MAINNET_YAML)
    assert isinstance(cfg, VenuesConfig)
    assert cfg.binance is not None
    assert cfg.binance.futures is not None
    futures = cfg.binance.futures
    assert isinstance(futures, BinanceFuturesConfig)
    assert futures.environment == "LIVE"
    assert futures.account_type == "USDT_FUTURE"
    assert futures.key_type == "HMAC"
    # Secrets came from env, not yaml.
    assert futures.api_key == "fake-mainnet-key-do-not-use"
    assert futures.api_secret == "fake-mainnet-secret-do-not-use"


def test_repo_mainnet_yaml_only_configures_futures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Phase 6 §1.6: mainnet yaml is single-purpose (futures only).
    Spot / hyperliquid must not appear here."""
    _set_mainnet_env(monkeypatch)
    cfg = load_venues(_REPO_MAINNET_YAML)
    assert cfg.binance is not None
    assert cfg.binance.spot is None
    assert cfg.hyperliquid is None


# ---------------------------------------------------------------------------
# (b) Plaintext-secret rejection — the schema MUST refuse `api_key:` etc.
# ---------------------------------------------------------------------------


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "venues.test.yaml"
    path.write_text(body, encoding="utf-8")
    return path


def test_plaintext_api_key_field_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If someone replaces the env-ref with a literal `api_key:` the
    loader must refuse — `_resolve_env_ref` reads `api_key_env`, never
    `api_key`, so the missing `api_key_env` key raises ConfigError."""
    monkeypatch.setenv(_MAINNET_API_SECRET_ENV, "x")
    body = """
binance:
  environment: LIVE
  futures:
    api_key: literal-key-leaked-into-yaml
    api_secret_env: XTRADE_MAINNET_BINANCE_FUTURES_API_SECRET
    key_type: HMAC
    account_type: USDT_FUTURE
""".strip()
    path = _write(tmp_path, body)
    with pytest.raises(ConfigError) as exc:
        load_venues(path)
    # The reject message must point at the missing env-ref key, not
    # leak the literal value.
    assert "api_key_env" in str(exc.value)
    assert "literal-key-leaked-into-yaml" not in str(exc.value)


def test_plaintext_api_secret_field_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(_MAINNET_API_KEY_ENV, "x")
    body = """
binance:
  environment: LIVE
  futures:
    api_key_env: XTRADE_MAINNET_BINANCE_FUTURES_API_KEY
    api_secret: literal-secret-leaked-into-yaml
    key_type: HMAC
    account_type: USDT_FUTURE
""".strip()
    path = _write(tmp_path, body)
    with pytest.raises(ConfigError) as exc:
        load_venues(path)
    assert "api_secret_env" in str(exc.value)
    assert "literal-secret-leaked-into-yaml" not in str(exc.value)


def test_repo_yaml_contains_no_plaintext_secret_fields() -> None:
    """Belt + braces: the shipped yaml must not contain the literal
    keys `api_key:` or `api_secret:` (only the `*_env:` aliases)."""
    text = _REPO_MAINNET_YAML.read_text(encoding="utf-8")
    # `api_key_env:` / `api_secret_env:` are OK; `api_key:` /
    # `api_secret:` alone are not. We scan line-by-line for the bare
    # forms.
    for lineno, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith("#") or not stripped:
            continue
        for forbidden in ("api_key:", "api_secret:"):
            if forbidden in stripped and "_env:" not in stripped:
                pytest.fail(
                    f"plaintext secret field {forbidden!r} found in "
                    f"{_REPO_MAINNET_YAML.name} line {lineno}: {line!r}"
                )


# ---------------------------------------------------------------------------
# (c) Env-missing path message (Phase 5 Bug 7 regression)
# ---------------------------------------------------------------------------


def test_missing_env_var_raises_with_actionable_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When `XTRADE_MAINNET_BINANCE_FUTURES_API_KEY` is unset, the
    error must (i) name the env var, (ii) name the yaml key that
    referenced it, (iii) name the env file path so the operator
    knows where to add the value."""
    monkeypatch.delenv(_MAINNET_API_KEY_ENV, raising=False)
    monkeypatch.delenv(_MAINNET_API_SECRET_ENV, raising=False)
    with pytest.raises(MissingCredentialError) as exc:
        load_venues(_REPO_MAINNET_YAML)
    msg = str(exc.value)
    assert _MAINNET_API_KEY_ENV in msg, msg
    assert "api_key_env" in msg, msg
    # Phase 5 Bug 7: message must include the env-file path for
    # operator actionability. Default is `.env`-style; the resolver
    # surfaces `_resolve_env_path()` which always returns *something*.
    # We assert "env file" wording is present.
    assert (".env" in msg) or ("env file" in msg.lower()), msg


def test_missing_secret_raises_with_actionable_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same contract for the secret env var."""
    monkeypatch.setenv(_MAINNET_API_KEY_ENV, "x")
    monkeypatch.delenv(_MAINNET_API_SECRET_ENV, raising=False)
    with pytest.raises(MissingCredentialError) as exc:
        load_venues(_REPO_MAINNET_YAML)
    msg = str(exc.value)
    assert _MAINNET_API_SECRET_ENV in msg, msg


# ---------------------------------------------------------------------------
# (d) Mainnet routing is detected by Lock 3 (interaction with
#     `assert_mainnet_unlock`)
# ---------------------------------------------------------------------------


def test_mainnet_yaml_triggers_lock3(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sanity: loading the mainnet yaml produces a VenuesConfig that
    `assert_mainnet_unlock` classifies as mainnet (i.e. raises when
    the unlock ritual is not satisfied). Without this guarantee,
    Phase 6's "Lock 3 is the load-bearing mainnet gate" claim is
    silently broken if the yaml ever drifts away from `LIVE`."""
    _set_mainnet_env(monkeypatch)
    monkeypatch.delenv("XTRADE_MAINNET_UNLOCK_TOKEN", raising=False)
    cfg = load_venues(_REPO_MAINNET_YAML)

    from xtrade.live.mainnet_unlock import (
        MainnetUnlockError,
        assert_mainnet_unlock,
    )

    with pytest.raises(MainnetUnlockError):
        assert_mainnet_unlock(cfg)
