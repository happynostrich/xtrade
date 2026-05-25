"""Unified credential / configuration loader.

All scripts must access secrets through this module. Secrets are read
from environment variables (optionally loaded from a `.env` file) and
NEVER hardcoded, logged in cleartext, or committed.

Two layers coexist here:

- The Phase 0 layer (`load_binance_testnet`, `load_hyperliquid_testnet`,
  `BinanceTestnetCreds`, `HyperliquidTestnetCreds`) — kept for the
  `scripts/phase0/*` reproducibility scripts.
- The Phase 1 layer (`load_venues`, `VenuesConfig`, `BinanceVenueConfig`,
  `HyperliquidVenueConfig`) — yaml-driven, strongly typed, feeds
  `xtrade.node.factory.build_testnet_node`. Per Phase 1 §6, the yaml
  references env-var *names* only; literal secrets never enter the file.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - dotenv is in deps but be defensive
    load_dotenv = None  # type: ignore[assignment]

try:
    import yaml
except ImportError:  # pragma: no cover - pyyaml is in deps but be defensive
    yaml = None  # type: ignore[assignment]


REPO_ROOT = Path(__file__).resolve().parents[2]
_ENV_PATH = REPO_ROOT / ".env"


def _resolve_env_path() -> Path:
    """Return the dotenv path to load. Honors `XTRADE_ENV_FILE` override
    (A6 Bug 7) so VPS deployments can point at `/etc/xtrade/env` instead
    of the dev-tree `.env`. The env-var path takes precedence even if
    the file does not exist (the caller's `exists()` check decides).
    """
    override = os.environ.get("XTRADE_ENV_FILE")
    if override:
        return Path(override)
    return _ENV_PATH


def _load_env_once() -> None:
    """Load `.env` (or `$XTRADE_ENV_FILE`) if present. Safe to call
    multiple times."""
    if load_dotenv is None:
        return
    path = _resolve_env_path()
    if path.exists():
        load_dotenv(dotenv_path=path, override=False)


_load_env_once()


class MissingCredentialError(RuntimeError):
    """Raised when a required credential is missing from the environment."""


class ConfigError(RuntimeError):
    """Raised when the venues yaml is malformed or references a nonexistent
    env var. Distinct from MissingCredentialError so callers can map it to
    exit code 2 (config/precondition failure) per Phase 1 P7."""


def _require(var: str) -> str:
    val = os.environ.get(var)
    if not val:
        raise MissingCredentialError(
            f"Required environment variable `{var}` is not set. "
            f"Please add it to your env file ({_resolve_env_path()}; "
            f"override via XTRADE_ENV_FILE). "
            f"Phase 0 scripts will NOT auto-generate credentials."
        )
    return val


def _optional(var: str) -> str | None:
    val = os.environ.get(var)
    return val if val else None


# ---------------------------------------------------------------------------
# Phase 0 layer (kept verbatim for backward compat)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BinanceTestnetCreds:
    spot_api_key: str | None
    spot_api_secret: str | None
    futures_api_key: str | None
    futures_api_secret: str | None

    @property
    def has_spot(self) -> bool:
        return bool(self.spot_api_key and self.spot_api_secret)

    @property
    def has_futures(self) -> bool:
        return bool(self.futures_api_key and self.futures_api_secret)


@dataclass(frozen=True)
class HyperliquidTestnetCreds:
    account_address: str
    api_wallet_key: str
    vault_address: str | None = None


def load_binance_testnet() -> BinanceTestnetCreds:
    """Load Binance testnet credentials. Falls back to the shared
    BINANCE_TESTNET_* pair if a venue-specific pair is not provided."""
    shared_key = _optional("BINANCE_TESTNET_API_KEY")
    shared_secret = _optional("BINANCE_TESTNET_API_SECRET")

    return BinanceTestnetCreds(
        spot_api_key=_optional("BINANCE_SPOT_TESTNET_API_KEY") or shared_key,
        spot_api_secret=_optional("BINANCE_SPOT_TESTNET_API_SECRET") or shared_secret,
        futures_api_key=_optional("BINANCE_FUTURES_TESTNET_API_KEY") or shared_key,
        futures_api_secret=_optional("BINANCE_FUTURES_TESTNET_API_SECRET") or shared_secret,
    )


def load_hyperliquid_testnet() -> HyperliquidTestnetCreds:
    """Load Hyperliquid testnet credentials. Raises MissingCredentialError
    if mandatory fields are absent."""
    return HyperliquidTestnetCreds(
        account_address=_require("HYPERLIQUID_TESTNET_ACCOUNT_ADDRESS"),
        api_wallet_key=_require("HYPERLIQUID_TESTNET_API_WALLET_KEY"),
        vault_address=_optional("HYPERLIQUID_TESTNET_VAULT_ADDRESS"),
    )


def mask(secret: str | None, *, keep: int = 4) -> str:
    """Return a masked representation of a secret for safe logging."""
    if not secret:
        return "<unset>"
    if len(secret) <= keep:
        return "*" * len(secret)
    return f"{secret[:keep]}{'*' * (len(secret) - keep)}"


# ---------------------------------------------------------------------------
# Phase 1 layer: yaml + env merging into strongly typed VenuesConfig
# ---------------------------------------------------------------------------

# Allowed enum-like values. Kept as literal sets (not enum.Enum) so the yaml
# stays human-readable and we don't drag enum coupling through the config
# surface. `node/factory.py` will translate to Nautilus enums at use time.
_VALID_BINANCE_ENVIRONMENTS = frozenset({"TESTNET", "DEMO", "LIVE"})
_VALID_BINANCE_KEY_TYPES = frozenset({"HMAC", "ED25519"})
_VALID_BINANCE_SPOT_ACCOUNT_TYPES = frozenset({"SPOT", "MARGIN"})
_VALID_BINANCE_FUTURES_ACCOUNT_TYPES = frozenset({"USDT_FUTURE", "COIN_FUTURE"})
_VALID_HL_ENVIRONMENTS = frozenset({"TESTNET", "LIVE"})


@dataclass(frozen=True)
class BinanceSpotConfig:
    """Binance spot account configuration. Phase 1 Task 5/6 will feed this
    into `BinanceLiveDataClientConfig` / `BinanceLiveExecClientConfig`."""

    api_key: str
    api_secret: str
    key_type: str  # HMAC | ED25519
    account_type: str  # SPOT | MARGIN
    environment: str  # TESTNET | DEMO | LIVE


@dataclass(frozen=True)
class BinanceFuturesConfig:
    """Binance USDT-M futures account configuration. `environment=TESTNET`
    is the post-2026-migration default — Nautilus 1.227 routes it to
    `demo-fapi.binance.com` REST with the reachable WS market URL."""

    api_key: str
    api_secret: str
    key_type: str  # HMAC | ED25519
    account_type: str  # USDT_FUTURE | COIN_FUTURE
    environment: str  # TESTNET | DEMO | LIVE


@dataclass(frozen=True)
class BinanceVenueConfig:
    """Top-level Binance venue config. At least one of `spot` / `futures`
    must be set; `node.factory` will build only the configured clients."""

    spot: BinanceSpotConfig | None = None
    futures: BinanceFuturesConfig | None = None

    @property
    def has_spot(self) -> bool:
        return self.spot is not None

    @property
    def has_futures(self) -> bool:
        return self.futures is not None


@dataclass(frozen=True)
class HyperliquidVenueConfig:
    """Hyperliquid / trade.xyz venue config. `unified_account=True` matches
    the Phase 0 C3 PASS path (Unified Account mode)."""

    account_address: str
    api_wallet_key: str
    environment: str  # TESTNET | LIVE
    vault_address: str | None = None
    unified_account: bool = True
    # HIP-3 trading DEX for trade.xyz-style perp DEXes ("xyz"). Optional
    # because spot-like usage doesn't need it.
    trading_dex: str | None = None


@dataclass(frozen=True)
class VenuesConfig:
    """Aggregate of all configured venues. At least one venue must be
    populated; consumers (`node.factory`) decide which to wire up."""

    binance: BinanceVenueConfig | None = None
    hyperliquid: HyperliquidVenueConfig | None = None
    # Echo back where this config came from, useful for log snapshots
    # (Task 7 writes `config.snapshot.yaml` per run).
    source_path: Path | None = field(default=None, compare=False)

    def __post_init__(self) -> None:
        if self.binance is None and self.hyperliquid is None:
            raise ConfigError(
                "VenuesConfig is empty: configure at least one of `binance:` "
                "or `hyperliquid:` in your venues yaml."
            )


# ---------------------------------------------------------------------------
# load_venues() and helpers
# ---------------------------------------------------------------------------


def _resolve_env_ref(section: dict[str, Any], key: str, *, required: bool) -> str | None:
    """Resolve a `<key>_env: VAR_NAME` reference in `section` against
    `os.environ`. Raises ConfigError if the yaml is malformed, and
    MissingCredentialError if a required env var is unset.

    Convention: yaml writes `api_key_env: BINANCE_SPOT_TESTNET_API_KEY` (env
    var *name* only). No literal-secret support — that's a Phase 1 §6
    requirement.
    """
    ref_key = f"{key}_env"
    if ref_key not in section:
        if required:
            raise ConfigError(
                f"Missing required key `{ref_key}` in venues yaml section "
                f"(near `{key}`). Add `{ref_key}: SOME_ENV_VAR_NAME`."
            )
        return None
    var_name = section[ref_key]
    if not isinstance(var_name, str) or not var_name:
        raise ConfigError(
            f"`{ref_key}` must be a non-empty string naming an env var, got "
            f"{var_name!r}."
        )
    val = os.environ.get(var_name)
    if not val:
        if required:
            raise MissingCredentialError(
                f"Environment variable `{var_name}` (referenced by `{ref_key}` "
                f"in venues yaml) is not set. Please add `{var_name}=...` to "
                f"your env file ({_resolve_env_path()}; override via "
                f"XTRADE_ENV_FILE)."
            )
        return None
    return val


def _validate_enum(value: str, allowed: frozenset[str], *, field_name: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise ConfigError(
            f"`{field_name}` must be one of {sorted(allowed)}, got {value!r}."
        )
    return value


def _parse_binance_spot(section: dict[str, Any], default_env: str) -> BinanceSpotConfig:
    api_key = _resolve_env_ref(section, "api_key", required=True)
    api_secret = _resolve_env_ref(section, "api_secret", required=True)
    assert api_key is not None and api_secret is not None  # narrowed by required=True
    key_type = _validate_enum(
        str(section.get("key_type", "HMAC")).upper(),
        _VALID_BINANCE_KEY_TYPES,
        field_name="binance.spot.key_type",
    )
    account_type = _validate_enum(
        str(section.get("account_type", "SPOT")).upper(),
        _VALID_BINANCE_SPOT_ACCOUNT_TYPES,
        field_name="binance.spot.account_type",
    )
    environment = _validate_enum(
        str(section.get("environment", default_env)).upper(),
        _VALID_BINANCE_ENVIRONMENTS,
        field_name="binance.spot.environment",
    )
    return BinanceSpotConfig(
        api_key=api_key,
        api_secret=api_secret,
        key_type=key_type,
        account_type=account_type,
        environment=environment,
    )


def _parse_binance_futures(section: dict[str, Any], default_env: str) -> BinanceFuturesConfig:
    api_key = _resolve_env_ref(section, "api_key", required=True)
    api_secret = _resolve_env_ref(section, "api_secret", required=True)
    assert api_key is not None and api_secret is not None
    key_type = _validate_enum(
        str(section.get("key_type", "HMAC")).upper(),
        _VALID_BINANCE_KEY_TYPES,
        field_name="binance.futures.key_type",
    )
    account_type = _validate_enum(
        str(section.get("account_type", "USDT_FUTURE")).upper(),
        _VALID_BINANCE_FUTURES_ACCOUNT_TYPES,
        field_name="binance.futures.account_type",
    )
    environment = _validate_enum(
        str(section.get("environment", default_env)).upper(),
        _VALID_BINANCE_ENVIRONMENTS,
        field_name="binance.futures.environment",
    )
    return BinanceFuturesConfig(
        api_key=api_key,
        api_secret=api_secret,
        key_type=key_type,
        account_type=account_type,
        environment=environment,
    )


def _parse_binance(section: dict[str, Any]) -> BinanceVenueConfig:
    if not isinstance(section, dict):
        raise ConfigError(f"`binance` section must be a mapping, got {type(section).__name__}.")
    default_env = _validate_enum(
        str(section.get("environment", "TESTNET")).upper(),
        _VALID_BINANCE_ENVIRONMENTS,
        field_name="binance.environment",
    )
    spot_section = section.get("spot")
    futures_section = section.get("futures")
    if spot_section is None and futures_section is None:
        raise ConfigError(
            "`binance` section must define at least one of `spot:` or `futures:`."
        )
    spot = _parse_binance_spot(spot_section, default_env) if spot_section else None
    futures = _parse_binance_futures(futures_section, default_env) if futures_section else None
    return BinanceVenueConfig(spot=spot, futures=futures)


def _parse_hyperliquid(section: dict[str, Any]) -> HyperliquidVenueConfig:
    if not isinstance(section, dict):
        raise ConfigError(
            f"`hyperliquid` section must be a mapping, got {type(section).__name__}."
        )
    account_address = _resolve_env_ref(section, "account_address", required=True)
    api_wallet_key = _resolve_env_ref(section, "api_wallet_key", required=True)
    vault_address = _resolve_env_ref(section, "vault_address", required=False)
    assert account_address is not None and api_wallet_key is not None
    environment = _validate_enum(
        str(section.get("environment", "TESTNET")).upper(),
        _VALID_HL_ENVIRONMENTS,
        field_name="hyperliquid.environment",
    )
    unified_account = bool(section.get("unified_account", True))
    trading_dex_raw = section.get("trading_dex")
    trading_dex = str(trading_dex_raw) if trading_dex_raw else None
    return HyperliquidVenueConfig(
        account_address=account_address,
        api_wallet_key=api_wallet_key,
        environment=environment,
        vault_address=vault_address,
        unified_account=unified_account,
        trading_dex=trading_dex,
    )


def load_venues(path: str | Path) -> VenuesConfig:
    """Load and validate a venues yaml file, returning a strongly typed
    `VenuesConfig`.

    Resolution order:

    1. Parse yaml from `path`.
    2. For each `*_env: VAR_NAME` reference, look up `VAR_NAME` in the
       environment (`.env` already loaded at import time).
    3. Validate enum-like fields against the allow-lists above.
    4. Cross-section sanity: at least one venue must be configured.

    Errors are split:

    - `ConfigError`     -> yaml is malformed, references bad keys, or
                           enum values are invalid (exit code 2).
    - `MissingCredentialError` -> referenced env var unset (exit code 2,
                                  but distinguishable for actionable
                                  user messages).
    """
    if yaml is None:  # pragma: no cover - pyyaml is in deps
        raise ConfigError("PyYAML is not installed; cannot parse venues yaml.")

    cfg_path = Path(path)
    if not cfg_path.exists():
        raise ConfigError(
            f"Venues config file does not exist: {cfg_path}. "
            f"Copy `config/venues.example.yaml` to `config/venues.testnet.yaml` "
            f"and adjust env-var references as needed."
        )

    with cfg_path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)

    if raw is None:
        raise ConfigError(f"Venues config file is empty: {cfg_path}.")
    if not isinstance(raw, dict):
        raise ConfigError(
            f"Venues config root must be a mapping, got {type(raw).__name__} "
            f"in {cfg_path}."
        )

    binance = _parse_binance(raw["binance"]) if "binance" in raw else None
    hyperliquid = _parse_hyperliquid(raw["hyperliquid"]) if "hyperliquid" in raw else None

    return VenuesConfig(
        binance=binance,
        hyperliquid=hyperliquid,
        source_path=cfg_path,
    )
