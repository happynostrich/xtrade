"""Unified credential / configuration loader.

All scripts must access secrets through this module. Secrets are read
from environment variables (optionally loaded from a `.env` file) and
NEVER hardcoded, logged in cleartext, or committed.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - dotenv is in deps but be defensive
    load_dotenv = None  # type: ignore[assignment]


REPO_ROOT = Path(__file__).resolve().parents[2]
_ENV_PATH = REPO_ROOT / ".env"


def _load_env_once() -> None:
    """Load `.env` if present. Safe to call multiple times."""
    if load_dotenv is None:
        return
    if _ENV_PATH.exists():
        load_dotenv(dotenv_path=_ENV_PATH, override=False)


_load_env_once()


class MissingCredentialError(RuntimeError):
    """Raised when a required credential is missing from the environment."""


def _require(var: str) -> str:
    val = os.environ.get(var)
    if not val:
        raise MissingCredentialError(
            f"Required environment variable `{var}` is not set. "
            f"Please add it to your `.env` (see `.env.example`). "
            f"Phase 0 scripts will NOT auto-generate credentials."
        )
    return val


def _optional(var: str) -> str | None:
    val = os.environ.get(var)
    return val if val else None


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
