"""Offline tests for `xtrade.live.mainnet_unlock` (Phase 5 Task A5).

These exercise Lock 3 in isolation: testnet-only configs short-circuit,
mainnet configs require the env+file ritual. The unlock file is
simulated via `tmp_path` + an injected `stat_fn` so the tests never
need actual root ownership.

Conventions follow `tests/test_node_factory.py`:
  - dummy credentials only; no env vars touched outside the explicit
    `env=` parameter to `assert_mainnet_unlock`.
  - `VenuesConfig` helpers mirror that test module's `_binance_spot` /
    `_hyperliquid` fixtures.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Callable

import pytest

from xtrade.config import (
    BinanceFuturesConfig,
    BinanceSpotConfig,
    BinanceVenueConfig,
    HyperliquidVenueConfig,
    VenuesConfig,
)
from xtrade.live.mainnet_unlock import (
    MAINNET_UNLOCK_TOKEN_ENV,
    MainnetUnlockError,
    _is_mainnet,
    assert_mainnet_unlock,
)


# ---------------------------------------------------------------------------
# Helpers — dummy credentials, mirroring test_node_factory.py
# ---------------------------------------------------------------------------


def _binance_spot(environment: str = "TESTNET") -> BinanceSpotConfig:
    return BinanceSpotConfig(
        api_key="dummy-spot-key",
        api_secret="dummy-spot-secret",
        key_type="HMAC",
        account_type="SPOT",
        environment=environment,
    )


def _binance_futures(environment: str = "TESTNET") -> BinanceFuturesConfig:
    return BinanceFuturesConfig(
        api_key="dummy-fut-key",
        api_secret="dummy-fut-secret",
        key_type="HMAC",
        account_type="USDT_FUTURE",
        environment=environment,
    )


def _hyperliquid(environment: str = "TESTNET") -> HyperliquidVenueConfig:
    return HyperliquidVenueConfig(
        account_address="0x0000000000000000000000000000000000000000",
        api_wallet_key="0x" + "1" * 64,
        environment=environment,
    )


def _testnet_cfg() -> VenuesConfig:
    return VenuesConfig(binance=BinanceVenueConfig(spot=_binance_spot("TESTNET")))


def _mainnet_cfg() -> VenuesConfig:
    """Use hyperliquid LIVE so we exercise the third `_is_mainnet` branch."""
    return VenuesConfig(hyperliquid=_hyperliquid("LIVE"))


def _fake_stat_fn(
    *, mode: int = 0o400, uid: int = 0
) -> Callable[[Path], os.stat_result]:
    """Synthetic `path.stat()` returning the (mode, uid) tuple the test
    needs. All other fields are zero — the unlock check only inspects
    `st_mode` and `st_uid`.
    """

    def _fn(_path: Path) -> os.stat_result:
        # os.stat_result requires a 10-tuple: mode, ino, dev, nlink, uid, gid,
        # size, atime, mtime, ctime.
        return os.stat_result((stat.S_IFREG | mode, 0, 0, 1, uid, 0, 0, 0, 0, 0))

    return _fn


# ---------------------------------------------------------------------------
# _is_mainnet — coverage for each branch
# ---------------------------------------------------------------------------


def test_is_mainnet_pure_testnet_is_false() -> None:
    cfg = VenuesConfig(
        binance=BinanceVenueConfig(
            spot=_binance_spot("TESTNET"),
            futures=_binance_futures("TESTNET"),
        ),
        hyperliquid=_hyperliquid("TESTNET"),
    )
    assert _is_mainnet(cfg) is False


def test_is_mainnet_binance_demo_is_false() -> None:
    """Binance DEMO routes to the new Demo Trading endpoint and counts
    as testnet for unlock purposes (same as Lock 1)."""
    cfg = VenuesConfig(binance=BinanceVenueConfig(spot=_binance_spot("DEMO")))
    assert _is_mainnet(cfg) is False


def test_is_mainnet_binance_spot_live_is_true() -> None:
    cfg = VenuesConfig(binance=BinanceVenueConfig(spot=_binance_spot("LIVE")))
    assert _is_mainnet(cfg) is True


def test_is_mainnet_binance_futures_live_is_true() -> None:
    cfg = VenuesConfig(binance=BinanceVenueConfig(futures=_binance_futures("LIVE")))
    assert _is_mainnet(cfg) is True


def test_is_mainnet_hyperliquid_live_is_true() -> None:
    cfg = _mainnet_cfg()
    assert _is_mainnet(cfg) is True


# ---------------------------------------------------------------------------
# Testnet-only path: assert_mainnet_unlock is a no-op (and does NOT
# read env or filesystem).
# ---------------------------------------------------------------------------


def test_testnet_only_is_noop_without_env_or_file(tmp_path: Path) -> None:
    """No env, no file, no stat_fn — must still return cleanly because
    `_is_mainnet` short-circuits before touching either."""
    # Point unlock_path at a nonexistent location to prove we never
    # stat it on testnet.
    assert_mainnet_unlock(
        _testnet_cfg(),
        env={},
        unlock_path=tmp_path / "definitely-does-not-exist",
    )


# ---------------------------------------------------------------------------
# Mainnet path: each of the 5 failure conditions must raise.
# ---------------------------------------------------------------------------


def test_mainnet_without_env_token_raises(tmp_path: Path) -> None:
    with pytest.raises(MainnetUnlockError, match=MAINNET_UNLOCK_TOKEN_ENV):
        assert_mainnet_unlock(
            _mainnet_cfg(),
            env={},
            unlock_path=tmp_path / "unlock",
        )


def test_mainnet_with_blank_env_token_raises(tmp_path: Path) -> None:
    with pytest.raises(MainnetUnlockError, match="unset or empty"):
        assert_mainnet_unlock(
            _mainnet_cfg(),
            env={MAINNET_UNLOCK_TOKEN_ENV: "   "},  # whitespace-only
            unlock_path=tmp_path / "unlock",
        )


def test_mainnet_with_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(MainnetUnlockError, match="is missing"):
        assert_mainnet_unlock(
            _mainnet_cfg(),
            env={MAINNET_UNLOCK_TOKEN_ENV: "secret-token"},
            unlock_path=tmp_path / "unlock-not-there",
        )


def test_mainnet_with_loose_file_mode_raises(tmp_path: Path) -> None:
    unlock = tmp_path / "unlock"
    unlock.write_text("secret-token\n")
    with pytest.raises(MainnetUnlockError, match="insecure mode"):
        assert_mainnet_unlock(
            _mainnet_cfg(),
            env={MAINNET_UNLOCK_TOKEN_ENV: "secret-token"},
            unlock_path=unlock,
            stat_fn=_fake_stat_fn(mode=0o644, uid=0),
        )


def test_mainnet_with_non_root_owner_raises(tmp_path: Path) -> None:
    unlock = tmp_path / "unlock"
    unlock.write_text("secret-token\n")
    with pytest.raises(MainnetUnlockError, match="owned by root"):
        assert_mainnet_unlock(
            _mainnet_cfg(),
            env={MAINNET_UNLOCK_TOKEN_ENV: "secret-token"},
            unlock_path=unlock,
            stat_fn=_fake_stat_fn(mode=0o400, uid=1000),
        )


def test_mainnet_with_empty_file_raises(tmp_path: Path) -> None:
    unlock = tmp_path / "unlock"
    unlock.write_text("")
    with pytest.raises(MainnetUnlockError, match="is empty"):
        assert_mainnet_unlock(
            _mainnet_cfg(),
            env={MAINNET_UNLOCK_TOKEN_ENV: "secret-token"},
            unlock_path=unlock,
            stat_fn=_fake_stat_fn(mode=0o400, uid=0),
        )


def test_mainnet_with_token_mismatch_raises(tmp_path: Path) -> None:
    unlock = tmp_path / "unlock"
    unlock.write_text("different-token\n")
    with pytest.raises(MainnetUnlockError, match="does not match"):
        assert_mainnet_unlock(
            _mainnet_cfg(),
            env={MAINNET_UNLOCK_TOKEN_ENV: "secret-token"},
            unlock_path=unlock,
            stat_fn=_fake_stat_fn(mode=0o400, uid=0),
        )


# ---------------------------------------------------------------------------
# Mainnet success path
# ---------------------------------------------------------------------------


def test_mainnet_with_full_ritual_passes(tmp_path: Path) -> None:
    """All three conditions satisfied → returns without raising."""
    unlock = tmp_path / "unlock"
    unlock.write_text("secret-token\n# trailing comment ignored\n")
    assert_mainnet_unlock(
        _mainnet_cfg(),
        env={MAINNET_UNLOCK_TOKEN_ENV: "secret-token"},
        unlock_path=unlock,
        stat_fn=_fake_stat_fn(mode=0o400, uid=0),
    )


def test_mainnet_with_whitespace_in_file_first_line_passes(tmp_path: Path) -> None:
    """Whitespace around the token in the file is stripped; the env
    token is also stripped — both must equal after stripping."""
    unlock = tmp_path / "unlock"
    unlock.write_text("  secret-token  \n")
    assert_mainnet_unlock(
        _mainnet_cfg(),
        env={MAINNET_UNLOCK_TOKEN_ENV: "  secret-token  "},
        unlock_path=unlock,
        stat_fn=_fake_stat_fn(mode=0o400, uid=0),
    )


# ---------------------------------------------------------------------------
# Token confidentiality — the error messages must never reveal the
# token value.
# ---------------------------------------------------------------------------


def test_error_messages_never_leak_env_token(tmp_path: Path) -> None:
    """If a future regression adds the token to a message, this test
    fails loudly."""
    secret = "PLEASE-DO-NOT-LEAK-ME-aabbccddeeff"
    unlock = tmp_path / "unlock"
    unlock.write_text("different\n")
    with pytest.raises(MainnetUnlockError) as excinfo:
        assert_mainnet_unlock(
            _mainnet_cfg(),
            env={MAINNET_UNLOCK_TOKEN_ENV: secret},
            unlock_path=unlock,
            stat_fn=_fake_stat_fn(mode=0o400, uid=0),
        )
    assert secret not in str(excinfo.value)
