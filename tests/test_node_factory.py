"""Offline tests for `xtrade.node.factory` (P2 / Task 3).

These tests don't connect to any network and deliberately avoid
constructing a full `TradingNode` (which initialises Nautilus's
global Rust logger and clashes with the `BacktestEngine` kernel
spun up by `tests/test_backtest_smoke.py`, causing
`Fatal Python error: Aborted`).

Instead, the mainnet hard guard and per-venue client wiring are
exercised through the pure-Python helpers that `build_testnet_node`
composes:

  - `_assert_testnet_only` — pure Python, no Nautilus kernel
  - `_build_binance_clients` / `_build_hyperliquid_clients` — return
    `(data_clients, exec_clients, factories)` triples; no kernel
    initialisation

The end-to-end `build_testnet_node(...) -> TradingNode` path is
exercised by `scripts/phase1/01_node_health.py` and the
`xtrade live health` CLI against real testnets.
"""

from __future__ import annotations

import pytest

from xtrade.config import (
    BinanceFuturesConfig,
    BinanceSpotConfig,
    BinanceVenueConfig,
    HyperliquidVenueConfig,
    VenuesConfig,
)
from xtrade.node.factory import (
    MainnetRefusedError,
    VenueConfigError,
    _assert_testnet_only,
    _build_binance_clients,
    _build_hyperliquid_clients,
)


# ---------------------------------------------------------------------------
# Helpers — minimal, all-dummy credentials. No env vars touched.
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


# ---------------------------------------------------------------------------
# Mainnet refusal — `_assert_testnet_only` is the hard guard and never
# touches Nautilus internals.
# ---------------------------------------------------------------------------


def test_refuses_binance_spot_mainnet() -> None:
    cfg = VenuesConfig(binance=BinanceVenueConfig(spot=_binance_spot("LIVE")))
    with pytest.raises(MainnetRefusedError, match="binance.spot.environment"):
        _assert_testnet_only(cfg)


def test_refuses_binance_futures_mainnet() -> None:
    cfg = VenuesConfig(binance=BinanceVenueConfig(futures=_binance_futures("LIVE")))
    with pytest.raises(MainnetRefusedError, match="binance.futures.environment"):
        _assert_testnet_only(cfg)


def test_refuses_hyperliquid_mainnet() -> None:
    cfg = VenuesConfig(hyperliquid=_hyperliquid("LIVE"))
    with pytest.raises(MainnetRefusedError, match="hyperliquid.environment"):
        _assert_testnet_only(cfg)


def test_refuses_mixed_partial_mainnet() -> None:
    """If only one of N venues is on mainnet, we still refuse."""
    cfg = VenuesConfig(
        binance=BinanceVenueConfig(spot=_binance_spot("TESTNET")),
        hyperliquid=_hyperliquid("LIVE"),
    )
    with pytest.raises(MainnetRefusedError, match="hyperliquid"):
        _assert_testnet_only(cfg)


def test_accepts_binance_demo_and_testnet() -> None:
    """`DEMO` is Binance's post-2026 Demo Trading routing; both must pass."""
    for env in ("TESTNET", "DEMO"):
        cfg = VenuesConfig(binance=BinanceVenueConfig(spot=_binance_spot(env)))
        _assert_testnet_only(cfg)  # must not raise


def test_accepts_pure_testnet_multivenue() -> None:
    cfg = VenuesConfig(
        binance=BinanceVenueConfig(
            spot=_binance_spot("TESTNET"),
            futures=_binance_futures("TESTNET"),
        ),
        hyperliquid=_hyperliquid("TESTNET"),
    )
    _assert_testnet_only(cfg)  # must not raise


# ---------------------------------------------------------------------------
# Per-venue client wiring — pure-Python helpers
# ---------------------------------------------------------------------------


def test_build_binance_clients_spot_only_uses_binance_key() -> None:
    b = BinanceVenueConfig(spot=_binance_spot("TESTNET"))
    data_clients, exec_clients, factories = _build_binance_clients(b)
    assert set(data_clients) == {"BINANCE"}
    assert set(exec_clients) == {"BINANCE"}
    assert [k for k, _, _ in factories] == ["BINANCE"]
    # Factories must be the live (not testnet-only) classes; the
    # `environment` field on the config routes to testnet.
    from nautilus_trader.adapters.binance.factories import (
        BinanceLiveDataClientFactory,
        BinanceLiveExecClientFactory,
    )

    assert factories[0][1] is BinanceLiveDataClientFactory
    assert factories[0][2] is BinanceLiveExecClientFactory


def test_build_binance_clients_rejects_spot_and_futures_coexistence() -> None:
    """Spot + futures cannot share one TradingNode.

    Even though the factory used to disambiguate at the dict-key level
    (`"BINANCE"` vs `"BINANCE_FUTURES"`), Nautilus's ExecutionEngine
    derives the registered `Venue` from the `BinanceExecClientConfig`
    payload (always `Venue('BINANCE')`) regardless of the outer key, so
    both subaccount clients collide at `register_client`. The factory
    now refuses this configuration with a clear `VenueConfigError`
    instead of letting Nautilus blow up at `node.build()` time.
    """
    b = BinanceVenueConfig(
        spot=_binance_spot("TESTNET"),
        futures=_binance_futures("TESTNET"),
    )
    with pytest.raises(VenueConfigError, match="spot and futures cannot coexist"):
        _build_binance_clients(b)


def test_build_binance_clients_futures_only_uses_binance_key() -> None:
    """When only futures is configured, it claims the `BINANCE` key
    (not `BINANCE_FUTURES`) so single-account setups stay tidy."""
    b = BinanceVenueConfig(futures=_binance_futures("TESTNET"))
    data_clients, _exec, _fa = _build_binance_clients(b)
    assert set(data_clients) == {"BINANCE"}


def test_build_binance_clients_translates_account_type() -> None:
    """Our config uses singular `USDT_FUTURE`; Nautilus enum is plural
    `USDT_FUTURES`. Verify the translation."""
    from nautilus_trader.adapters.binance.common.enums import BinanceAccountType

    b = BinanceVenueConfig(futures=_binance_futures("TESTNET"))
    data_clients, _exec, _fa = _build_binance_clients(b)
    assert data_clients["BINANCE"].account_type is BinanceAccountType.USDT_FUTURES


def test_build_hyperliquid_clients_keys_and_creds() -> None:
    h = _hyperliquid("TESTNET")
    data_clients, exec_clients, factories = _build_hyperliquid_clients(h)
    assert set(data_clients) == {"HYPERLIQUID"}
    assert set(exec_clients) == {"HYPERLIQUID"}
    assert [k for k, _, _ in factories] == ["HYPERLIQUID"]
    # Exec config carries the account address + wallet key
    exec_cfg = exec_clients["HYPERLIQUID"]
    assert exec_cfg.account_address == h.account_address
    assert exec_cfg.private_key == h.api_wallet_key


# ---------------------------------------------------------------------------
# `build_testnet_node` raises on an empty VenuesConfig — sanity check
# that doesn't construct a TradingNode (the ValueError fires after the
# testnet check and before TradingNodeConfig construction).
# ---------------------------------------------------------------------------


def test_build_testnet_node_with_no_venues_raises() -> None:
    """`VenuesConfig.__post_init__` already raises if both venues are
    None, so this exercises the layered guard."""
    from xtrade.config import ConfigError

    with pytest.raises(ConfigError):
        VenuesConfig()
