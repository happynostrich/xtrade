"""Offline tests for `xtrade.live.runner` and `LiveOrderProbe`
(Phase 1 Task 6 / P5).

These tests deliberately do **not** construct a `TradingNode` (the
Rust logger collides with `BacktestEngine` from `test_backtest_smoke`
when both run in the same pytest process). End-to-end live behaviour
is exercised by `scripts/phase1/02_live_run.py` against real testnets.

What we cover here:

  - Strategy registry lookup + unknown-name rejection
  - `LiveOrderProbe` initial state, `passed` semantics
  - `LiveOrderProbeConfig` defaults
  - `run_live` refuses a mainnet `VenuesConfig` (the
    `_assert_testnet_only` guard fires before any node construction)
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from xtrade.config import (
    BinanceSpotConfig,
    BinanceVenueConfig,
    HyperliquidVenueConfig,
    VenuesConfig,
)
from xtrade.live.runner import (
    LiveResult,
    _live_strategy_class,
    available_live_strategies,
    run_live,
)
from xtrade.node.factory import MainnetRefusedError
from xtrade.strategies.live_order_probe import LiveOrderProbe, LiveOrderProbeConfig


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_available_live_strategies_lists_probe() -> None:
    assert available_live_strategies() == ["live_order_probe"]


def test_live_strategy_class_lookup() -> None:
    assert _live_strategy_class("live_order_probe") is LiveOrderProbe


def test_live_strategy_class_unknown_raises() -> None:
    with pytest.raises(ValueError, match="unknown live strategy"):
        _live_strategy_class("does_not_exist")


# ---------------------------------------------------------------------------
# LiveOrderProbeConfig defaults — pure msgspec construction, no kernel
# ---------------------------------------------------------------------------


def test_live_order_probe_config_defaults() -> None:
    from nautilus_trader.model.identifiers import InstrumentId

    cfg = LiveOrderProbeConfig(
        mode="live",
        instrument_id=InstrumentId.from_str("BTCUSDT.BINANCE"),
    )
    assert cfg.mode == "live"
    assert cfg.quantity == Decimal("0.001")
    assert cfg.side == "BUY"
    assert cfg.safety_multiplier == Decimal("0.7")
    assert cfg.timeout_s == 60.0


def test_live_order_probe_config_overrides() -> None:
    from nautilus_trader.model.identifiers import InstrumentId

    cfg = LiveOrderProbeConfig(
        mode="live",
        instrument_id=InstrumentId.from_str("BTC-USD-PERP.HYPERLIQUID"),
        quantity=Decimal("0.005"),
        side="SELL",
        safety_multiplier=Decimal("0.5"),
        timeout_s=120.0,
    )
    assert cfg.quantity == Decimal("0.005")
    assert cfg.side == "SELL"
    assert cfg.safety_multiplier == Decimal("0.5")
    assert cfg.timeout_s == 120.0


# ---------------------------------------------------------------------------
# Mainnet refusal — `_assert_testnet_only` fires before any kernel work
# ---------------------------------------------------------------------------


def _binance_spot(environment: str) -> BinanceSpotConfig:
    return BinanceSpotConfig(
        api_key="dummy",
        api_secret="dummy",
        key_type="HMAC",
        account_type="SPOT",
        environment=environment,
    )


def _hyperliquid(environment: str) -> HyperliquidVenueConfig:
    return HyperliquidVenueConfig(
        account_address="0x0000000000000000000000000000000000000000",
        api_wallet_key="0x" + "1" * 64,
        environment=environment,
    )


def test_run_live_refuses_binance_mainnet(tmp_path) -> None:
    cfg = VenuesConfig(binance=BinanceVenueConfig(spot=_binance_spot("LIVE")))
    with pytest.raises(MainnetRefusedError, match="binance.spot.environment"):
        run_live(
            cfg,
            instrument_id="BTCUSDT.BINANCE",
            timeout_s=1.0,
            run_id="refuse-binance",
            logs_root=tmp_path / "logs",
        )


def test_run_live_refuses_hyperliquid_mainnet(tmp_path) -> None:
    cfg = VenuesConfig(hyperliquid=_hyperliquid("LIVE"))
    with pytest.raises(MainnetRefusedError, match="hyperliquid.environment"):
        run_live(
            cfg,
            instrument_id="BTC-USD-PERP.HYPERLIQUID",
            timeout_s=1.0,
            run_id="refuse-hl",
            logs_root=tmp_path / "logs",
        )


def test_run_live_unknown_strategy_rejected(tmp_path) -> None:
    cfg = VenuesConfig(binance=BinanceVenueConfig(spot=_binance_spot("TESTNET")))
    with pytest.raises(ValueError, match="unknown live strategy"):
        run_live(
            cfg,
            instrument_id="BTCUSDT.BINANCE",
            strategy="does_not_exist",
            timeout_s=1.0,
            run_id="unknown-strategy",
            logs_root=tmp_path / "logs",
        )


# ---------------------------------------------------------------------------
# LiveResult.passed mirrors the summary dict
# ---------------------------------------------------------------------------


def test_live_result_passed_true(tmp_path) -> None:
    r = LiveResult(
        run_id="x",
        log_dir=tmp_path,
        summary_path=tmp_path / "summary.json",
        summary={"passed": True},
    )
    assert r.passed is True


def test_live_result_passed_false_default(tmp_path) -> None:
    r = LiveResult(
        run_id="x",
        log_dir=tmp_path,
        summary_path=tmp_path / "summary.json",
    )
    assert r.passed is False
