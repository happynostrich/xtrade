"""TradingNode factory (Phase 1 Task 3 / P2).

`build_testnet_node(venues_cfg)` returns an **unbuilt** `TradingNode`
wired with data + exec client factories for each configured venue.

Safety contract (per Phase 1 brief §6 "主网执行硬禁用"):

  - Every `BinanceVenueConfig.{spot,futures}.environment` must be one of
    `TESTNET` / `DEMO`. `LIVE` raises `RuntimeError` *before* any client
    factory is added to the node.
  - `HyperliquidVenueConfig.environment` must be `TESTNET`.

The caller is expected to:

  1. Add strategies via `node.trader.add_strategy(...)`.
  2. Call `node.build()`.
  3. Drive the node with `node.run_async()` / `node.stop_async()`.

Phase 0 reference: `scripts/phase0/02b_binance_spot_testnet_connectivity.py`
and `scripts/phase0/03_hyperliquid_testnet_connectivity.py`.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from xtrade.config import (
    BinanceFuturesConfig,
    BinanceSpotConfig,
    BinanceVenueConfig,
    HyperliquidVenueConfig,
    VenuesConfig,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from nautilus_trader.live.node import TradingNode


# Acceptable testnet-class environments. `DEMO` is Binance's post-2026
# Demo Trading routing; the brief explicitly keeps it allowed alongside
# `TESTNET` so the same factory works on either Binance routing mode.
_BINANCE_TESTNET_LIKE = frozenset({"TESTNET", "DEMO"})
_HL_TESTNET_LIKE = frozenset({"TESTNET"})


# Map our config enums to Nautilus enums lazily (heavy imports).
_BINANCE_ACCOUNT_TYPE_MAP = {
    "SPOT": "SPOT",
    "MARGIN": "MARGIN",
    "USDT_FUTURE": "USDT_FUTURES",  # singular in our config -> plural in Nautilus
    "COIN_FUTURE": "COIN_FUTURES",
}


class MainnetRefusedError(RuntimeError):
    """Raised when a venue config would route a client at mainnet.

    Phase 1 forbids live mainnet trading; this is the hard guard the
    brief calls out in §6.
    """


class VenueConfigError(RuntimeError):
    """Raised when a venue config is internally inconsistent.

    Examples:

      - Binance spot and futures both populated on a single yaml. Nautilus
        derives the registered ``Venue`` from the ``BinanceExecClientConfig``
        payload (always ``Venue('BINANCE')``) regardless of the outer
        ``exec_clients`` dict key, so two Binance subaccount clients
        collide at ``ExecutionEngine.register_client``. The fix is to
        split into two yamls (or null one subaccount out of the other).
    """


def _assert_testnet_only(venues: VenuesConfig) -> None:
    """Hard-fail if any configured client points at mainnet."""
    b = venues.binance
    if b is not None:
        if b.spot is not None and b.spot.environment not in _BINANCE_TESTNET_LIKE:
            raise MainnetRefusedError(
                f"binance.spot.environment={b.spot.environment!r} is not testnet. "
                f"Phase 1 forbids mainnet routing; must be one of "
                f"{sorted(_BINANCE_TESTNET_LIKE)}."
            )
        if b.futures is not None and b.futures.environment not in _BINANCE_TESTNET_LIKE:
            raise MainnetRefusedError(
                f"binance.futures.environment={b.futures.environment!r} is not testnet."
            )
    h = venues.hyperliquid
    if h is not None and h.environment not in _HL_TESTNET_LIKE:
        raise MainnetRefusedError(
            f"hyperliquid.environment={h.environment!r} is not TESTNET."
        )


def _build_binance_clients(b: BinanceVenueConfig) -> tuple[dict, dict, list]:
    """Build BINANCE data+exec client configs for whichever subaccounts
    are present. Returns (data_clients, exec_clients, factories) where
    `factories` is a list of (key, data_factory, exec_factory) tuples
    the caller must register on the TradingNode.

    Raises
    ------
    VenueConfigError
        If both ``b.spot`` and ``b.futures`` are populated. Nautilus
        registers both clients under ``Venue('BINANCE')`` regardless of
        the outer ``exec_clients`` dict key (see ``VenueConfigError``);
        the operator must split into two yamls or null one out.
    """
    if b.spot is not None and b.futures is not None:
        raise VenueConfigError(
            "Binance spot and futures cannot coexist on a single TradingNode: "
            "Nautilus's ExecutionEngine registers both clients under "
            "Venue('BINANCE'). Split your venues yaml into two files (one for "
            "spot, one for futures) and pass --venues-yaml accordingly, or set "
            "the unused subaccount to null in this yaml."
        )

    from nautilus_trader.adapters.binance.common.enums import (
        BinanceAccountType,
        BinanceEnvironment,
    )
    from nautilus_trader.adapters.binance.config import (
        BinanceDataClientConfig,
        BinanceExecClientConfig,
    )
    from nautilus_trader.adapters.binance.factories import (
        BinanceLiveDataClientFactory,
        BinanceLiveExecClientFactory,
    )
    from nautilus_trader.config import InstrumentProviderConfig

    provider = InstrumentProviderConfig(load_all=True)

    data_clients: dict = {}
    exec_clients: dict = {}

    # Prefer SPOT if configured; futures registers under a separate
    # client key so both can coexist on the same node.
    if b.spot is not None:
        spot: BinanceSpotConfig = b.spot
        acct = BinanceAccountType[_BINANCE_ACCOUNT_TYPE_MAP[spot.account_type]]
        env = BinanceEnvironment[spot.environment]
        data_clients["BINANCE"] = BinanceDataClientConfig(
            api_key=spot.api_key,
            api_secret=spot.api_secret,
            account_type=acct,
            environment=env,
            instrument_provider=provider,
        )
        exec_clients["BINANCE"] = BinanceExecClientConfig(
            api_key=spot.api_key,
            api_secret=spot.api_secret,
            account_type=acct,
            environment=env,
            instrument_provider=provider,
        )

    if b.futures is not None:
        fut: BinanceFuturesConfig = b.futures
        acct = BinanceAccountType[_BINANCE_ACCOUNT_TYPE_MAP[fut.account_type]]
        env = BinanceEnvironment[fut.environment]
        # spot+futures coexistence is rejected above, so the futures
        # client always claims the canonical "BINANCE" key.
        data_clients["BINANCE"] = BinanceDataClientConfig(
            api_key=fut.api_key,
            api_secret=fut.api_secret,
            account_type=acct,
            environment=env,
            instrument_provider=provider,
        )
        exec_clients["BINANCE"] = BinanceExecClientConfig(
            api_key=fut.api_key,
            api_secret=fut.api_secret,
            account_type=acct,
            environment=env,
            instrument_provider=provider,
        )

    factories = [
        (key, BinanceLiveDataClientFactory, BinanceLiveExecClientFactory)
        for key in data_clients
    ]
    return data_clients, exec_clients, factories


def _build_hyperliquid_clients(h: HyperliquidVenueConfig) -> tuple[dict, dict, list]:
    from nautilus_trader.adapters.hyperliquid.config import (
        HyperliquidDataClientConfig,
        HyperliquidExecClientConfig,
    )
    from nautilus_trader.adapters.hyperliquid.factories import (
        HyperliquidLiveDataClientFactory,
        HyperliquidLiveExecClientFactory,
    )
    from nautilus_trader.config import InstrumentProviderConfig
    from nautilus_trader.core.nautilus_pyo3.hyperliquid import HyperliquidEnvironment

    provider = InstrumentProviderConfig(load_all=True)
    env = HyperliquidEnvironment.TESTNET  # _assert_testnet_only guarantees this

    data_cfg = HyperliquidDataClientConfig(
        environment=env,
        instrument_provider=provider,
    )
    exec_cfg = HyperliquidExecClientConfig(
        account_address=h.account_address,
        private_key=h.api_wallet_key,
        vault_address=h.vault_address,
        environment=env,
        instrument_provider=provider,
    )

    data_clients = {"HYPERLIQUID": data_cfg}
    exec_clients = {"HYPERLIQUID": exec_cfg}
    factories = [
        ("HYPERLIQUID", HyperliquidLiveDataClientFactory, HyperliquidLiveExecClientFactory)
    ]
    return data_clients, exec_clients, factories


def build_testnet_node(
    venues_cfg: VenuesConfig,
    *,
    trader_id: str = "XTRADE-NODE-001",
    log_level: str = "INFO",
    log_directory: Path | str | None = None,
) -> "TradingNode":
    """Build (but don't call `.build()` on) a multi-venue testnet TradingNode.

    Parameters
    ----------
    venues_cfg : VenuesConfig
        Loaded by `xtrade.config.load_venues()`.
    trader_id : str
        Stamped into Nautilus's trader id (shows up in client order ids).
    log_level : str
        Forwarded to `LoggingConfig.log_level`.
    log_directory : Path | str | None
        If set, Nautilus writes `<log_directory>/run.log` (Phase 1 Task 7).
        `log_level_file` is also set to `log_level` so file output is
        actually emitted (Nautilus suppresses file logs without it).

    Returns
    -------
    A `TradingNode` with data + exec client factories registered for every
    configured venue. The caller is responsible for adding strategies and
    calling `node.build()`.

    Raises
    ------
    MainnetRefusedError
        If any configured venue/subaccount targets mainnet.
    """
    _assert_testnet_only(venues_cfg)

    from nautilus_trader.config import (
        LiveDataEngineConfig,
        LiveExecEngineConfig,
        LoggingConfig,
        TradingNodeConfig,
    )
    from nautilus_trader.live.node import TradingNode

    data_clients: dict = {}
    exec_clients: dict = {}
    all_factories: list = []

    if venues_cfg.binance is not None:
        dc, ec, fa = _build_binance_clients(venues_cfg.binance)
        data_clients.update(dc)
        exec_clients.update(ec)
        all_factories.extend(fa)

    if venues_cfg.hyperliquid is not None:
        dc, ec, fa = _build_hyperliquid_clients(venues_cfg.hyperliquid)
        data_clients.update(dc)
        exec_clients.update(ec)
        all_factories.extend(fa)

    if not data_clients:
        raise RuntimeError(
            "build_testnet_node received a VenuesConfig with no configured "
            "venues; this shouldn't happen — VenuesConfig.__post_init__ "
            "should have raised earlier."
        )

    if log_directory is not None:
        logging_cfg = LoggingConfig(
            log_level=log_level,
            log_level_file=log_level,
            log_directory=str(log_directory),
            log_file_name="run",
        )
    else:
        logging_cfg = LoggingConfig(log_level=log_level)

    node_cfg = TradingNodeConfig(
        trader_id=trader_id,
        logging=logging_cfg,
        data_engine=LiveDataEngineConfig(),
        exec_engine=LiveExecEngineConfig(),
        data_clients=data_clients,
        exec_clients=exec_clients,
    )

    node = TradingNode(config=node_cfg)
    for key, data_factory, exec_factory in all_factories:
        node.add_data_client_factory(key, data_factory)
        node.add_exec_client_factory(key, exec_factory)

    return node
