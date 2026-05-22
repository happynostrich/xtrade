"""Tests for `xtrade.config.load_venues` and friends (Phase 1 Task 2 / P8).

These tests are fully offline: they write tiny yaml fixtures to tmp_path
and drive `os.environ` via `monkeypatch`. No `.env` file is read here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from xtrade.config import (
    BinanceFuturesConfig,
    BinanceSpotConfig,
    BinanceVenueConfig,
    ConfigError,
    HyperliquidVenueConfig,
    MissingCredentialError,
    VenuesConfig,
    load_venues,
    mask,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_FULL_YAML = """\
binance:
  environment: TESTNET
  spot:
    api_key_env: BINANCE_SPOT_TESTNET_API_KEY
    api_secret_env: BINANCE_SPOT_TESTNET_API_SECRET
    key_type: ED25519
    account_type: SPOT
  futures:
    api_key_env: BINANCE_FUTURES_TESTNET_API_KEY
    api_secret_env: BINANCE_FUTURES_TESTNET_API_SECRET
    key_type: ED25519
    account_type: USDT_FUTURE
hyperliquid:
  environment: TESTNET
  account_address_env: HYPERLIQUID_TESTNET_ACCOUNT_ADDRESS
  api_wallet_key_env: HYPERLIQUID_TESTNET_API_WALLET_KEY
  vault_address_env: HYPERLIQUID_TESTNET_VAULT_ADDRESS
  unified_account: true
  trading_dex: xyz
"""

_BINANCE_ONLY_YAML = """\
binance:
  spot:
    api_key_env: BINANCE_SPOT_TESTNET_API_KEY
    api_secret_env: BINANCE_SPOT_TESTNET_API_SECRET
"""

_HL_ONLY_YAML = """\
hyperliquid:
  account_address_env: HYPERLIQUID_TESTNET_ACCOUNT_ADDRESS
  api_wallet_key_env: HYPERLIQUID_TESTNET_API_WALLET_KEY
"""


def _write_yaml(tmp_path: Path, body: str, name: str = "venues.yaml") -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


def _set_all_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BINANCE_SPOT_TESTNET_API_KEY", "spot-key-abcdef")
    monkeypatch.setenv("BINANCE_SPOT_TESTNET_API_SECRET", "spot-secret-abcdef")
    monkeypatch.setenv("BINANCE_FUTURES_TESTNET_API_KEY", "fut-key-abcdef")
    monkeypatch.setenv("BINANCE_FUTURES_TESTNET_API_SECRET", "fut-secret-abcdef")
    monkeypatch.setenv("HYPERLIQUID_TESTNET_ACCOUNT_ADDRESS", "0xabc")
    monkeypatch.setenv("HYPERLIQUID_TESTNET_API_WALLET_KEY", "0xdef")
    monkeypatch.setenv("HYPERLIQUID_TESTNET_VAULT_ADDRESS", "0xvault")


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_load_venues_full(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _set_all_env(monkeypatch)
    path = _write_yaml(tmp_path, _FULL_YAML)

    cfg = load_venues(path)

    assert isinstance(cfg, VenuesConfig)
    assert cfg.source_path == path

    assert isinstance(cfg.binance, BinanceVenueConfig)
    assert cfg.binance.has_spot and cfg.binance.has_futures
    assert isinstance(cfg.binance.spot, BinanceSpotConfig)
    assert cfg.binance.spot.api_key == "spot-key-abcdef"
    assert cfg.binance.spot.api_secret == "spot-secret-abcdef"
    assert cfg.binance.spot.key_type == "ED25519"
    assert cfg.binance.spot.account_type == "SPOT"
    assert cfg.binance.spot.environment == "TESTNET"

    assert isinstance(cfg.binance.futures, BinanceFuturesConfig)
    assert cfg.binance.futures.api_key == "fut-key-abcdef"
    assert cfg.binance.futures.account_type == "USDT_FUTURE"
    assert cfg.binance.futures.environment == "TESTNET"

    assert isinstance(cfg.hyperliquid, HyperliquidVenueConfig)
    assert cfg.hyperliquid.account_address == "0xabc"
    assert cfg.hyperliquid.api_wallet_key == "0xdef"
    assert cfg.hyperliquid.vault_address == "0xvault"
    assert cfg.hyperliquid.unified_account is True
    assert cfg.hyperliquid.environment == "TESTNET"
    assert cfg.hyperliquid.trading_dex == "xyz"


def test_load_venues_binance_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _set_all_env(monkeypatch)
    path = _write_yaml(tmp_path, _BINANCE_ONLY_YAML)
    cfg = load_venues(path)
    assert cfg.binance is not None
    assert cfg.hyperliquid is None
    # Defaults kick in: key_type=HMAC, account_type=SPOT, environment=TESTNET
    assert cfg.binance.spot is not None
    assert cfg.binance.spot.key_type == "HMAC"
    assert cfg.binance.spot.account_type == "SPOT"
    assert cfg.binance.spot.environment == "TESTNET"
    assert cfg.binance.futures is None


def test_load_venues_hyperliquid_only_optional_vault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_all_env(monkeypatch)
    # No vault_address_env in yaml at all -> field stays None
    path = _write_yaml(tmp_path, _HL_ONLY_YAML)
    cfg = load_venues(path)
    assert cfg.binance is None
    assert cfg.hyperliquid is not None
    assert cfg.hyperliquid.vault_address is None
    assert cfg.hyperliquid.trading_dex is None
    assert cfg.hyperliquid.unified_account is True  # default


def test_repo_pointer_yaml_is_empty_stub() -> None:
    """As of Phase 3.5 the aggregated `config/venues.testnet.yaml` is
    intentionally gutted to a comment-only pointer — operators must pass
    one of the per-venue siblings instead. `load_venues` rejects the
    empty pointer with `ConfigError`."""
    repo_yaml = Path(__file__).resolve().parents[1] / "config" / "venues.testnet.yaml"
    with pytest.raises(ConfigError):
        load_venues(repo_yaml)


def test_repo_per_venue_yamls_load_with_full_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each shipped per-venue testnet yaml must parse cleanly when its
    referenced env vars are set."""
    _set_all_env(monkeypatch)
    config_dir = Path(__file__).resolve().parents[1] / "config"

    spot_cfg = load_venues(config_dir / "venues.binance_spot.testnet.yaml")
    assert spot_cfg.binance is not None
    assert spot_cfg.binance.has_spot and not spot_cfg.binance.has_futures
    assert spot_cfg.hyperliquid is None

    futures_cfg = load_venues(config_dir / "venues.binance_futures.testnet.yaml")
    assert futures_cfg.binance is not None
    assert futures_cfg.binance.has_futures and not futures_cfg.binance.has_spot
    assert futures_cfg.hyperliquid is None

    hl_cfg = load_venues(config_dir / "venues.hyperliquid.testnet.yaml")
    assert hl_cfg.binance is None
    assert hl_cfg.hyperliquid is not None
    assert hl_cfg.hyperliquid.trading_dex == "xyz"


# ---------------------------------------------------------------------------
# Missing credentials (env var unset)
# ---------------------------------------------------------------------------


def test_missing_required_env_raises_missing_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_all_env(monkeypatch)
    monkeypatch.delenv("BINANCE_SPOT_TESTNET_API_SECRET", raising=False)
    path = _write_yaml(tmp_path, _FULL_YAML)

    with pytest.raises(MissingCredentialError) as exc:
        load_venues(path)

    msg = str(exc.value)
    # Actionable: must name both the env var and where to set it.
    assert "BINANCE_SPOT_TESTNET_API_SECRET" in msg
    assert ".env" in msg


def test_missing_optional_env_is_silent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_all_env(monkeypatch)
    monkeypatch.delenv("HYPERLIQUID_TESTNET_VAULT_ADDRESS", raising=False)
    path = _write_yaml(tmp_path, _FULL_YAML)

    cfg = load_venues(path)
    assert cfg.hyperliquid is not None
    assert cfg.hyperliquid.vault_address is None


# ---------------------------------------------------------------------------
# Malformed yaml -> ConfigError
# ---------------------------------------------------------------------------


def test_missing_file_raises_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as exc:
        load_venues(tmp_path / "nope.yaml")
    assert "does not exist" in str(exc.value)


def test_empty_file_raises_config_error(tmp_path: Path) -> None:
    path = _write_yaml(tmp_path, "")
    with pytest.raises(ConfigError):
        load_venues(path)


def test_root_not_mapping_raises_config_error(tmp_path: Path) -> None:
    path = _write_yaml(tmp_path, "- 1\n- 2\n")
    with pytest.raises(ConfigError):
        load_venues(path)


def test_no_venues_raises_config_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_all_env(monkeypatch)
    path = _write_yaml(tmp_path, "unrelated_key: 1\n")
    with pytest.raises(ConfigError) as exc:
        load_venues(path)
    assert "at least one" in str(exc.value).lower()


def test_binance_without_subaccount_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_all_env(monkeypatch)
    path = _write_yaml(tmp_path, "binance:\n  environment: TESTNET\n")
    with pytest.raises(ConfigError) as exc:
        load_venues(path)
    assert "spot" in str(exc.value) and "futures" in str(exc.value)


def test_bad_environment_value_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_all_env(monkeypatch)
    body = _FULL_YAML.replace("environment: TESTNET", "environment: SANDBOX", 1)
    path = _write_yaml(tmp_path, body)
    with pytest.raises(ConfigError) as exc:
        load_venues(path)
    assert "environment" in str(exc.value).lower()


def test_bad_key_type_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_all_env(monkeypatch)
    body = _FULL_YAML.replace("key_type: ED25519", "key_type: RSA", 1)
    path = _write_yaml(tmp_path, body)
    with pytest.raises(ConfigError) as exc:
        load_venues(path)
    assert "key_type" in str(exc.value)


def test_missing_env_ref_key_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_all_env(monkeypatch)
    # Strip `api_secret_env: ...` from spot section
    body = _FULL_YAML.replace(
        "    api_secret_env: BINANCE_SPOT_TESTNET_API_SECRET\n", "", 1
    )
    path = _write_yaml(tmp_path, body)
    with pytest.raises(ConfigError) as exc:
        load_venues(path)
    assert "api_secret_env" in str(exc.value)


def test_non_string_env_ref_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_all_env(monkeypatch)
    body = _FULL_YAML.replace(
        "    api_key_env: BINANCE_SPOT_TESTNET_API_KEY",
        "    api_key_env: 42",
        1,
    )
    path = _write_yaml(tmp_path, body)
    with pytest.raises(ConfigError) as exc:
        load_venues(path)
    assert "api_key_env" in str(exc.value)


# ---------------------------------------------------------------------------
# mask() smoke (Phase 0 utility but still exercised here)
# ---------------------------------------------------------------------------


def test_mask_redacts_secrets() -> None:
    assert mask(None) == "<unset>"
    assert mask("abcd") == "****"
    assert mask("abcdefghij", keep=4) == "abcd******"
