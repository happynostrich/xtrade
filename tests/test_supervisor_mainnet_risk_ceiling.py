"""Tests for Phase 6 Task T2 — mainnet-strict risk ceiling.

Two layers of coverage:

1. **Unit** — direct calls to `assert_mainnet_risk_ceiling` cover every
   leg of the contract:
     * all four rule classes present, all under ceiling → pass
     * one rule's cap loose → raise, message names the rule + cap
     * drawdown pct loose → raise, message names the cap
     * a required rule missing → raise, message names the missing
       class
     * empty / extra rules behave as the brief specifies
     * the function reads off the public ceiling constants, so tests
       passing custom ceilings via kwargs verify the knob works.

2. **Integration** — `run_supervisor` is the load-bearing call site.
   Four scenarios mirroring the brief §5 Task T2 acceptance matrix:
     * testnet + loose caps   → pass (ceiling check skipped)
     * mainnet + loose caps   → raise `MainnetRiskTooLooseError`
     * mainnet + tight caps   → pass into the loop
     * mainnet + missing rule → raise `MainnetRiskTooLooseError`

   We monkeypatch `assert_mainnet_unlock` to a no-op so the supervisor
   doesn't fight the Phase 5 Lock 3 ritual in unit tests; Lock 3's own
   coverage lives in `test_mainnet_unlock.py`.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from xtrade.config import (
    BinanceFuturesConfig,
    BinanceVenueConfig,
    VenuesConfig,
)
from xtrade.live.supervisor import SupervisorConfig, run_supervisor
from xtrade.risk import (
    MAINNET_MAX_DRAWDOWN_PCT_CEILING,
    MAINNET_MAX_NOTIONAL_CEILING_USD,
    MainnetRiskTooLooseError,
    MaxDrawdownPct,
    MaxNotionalPerOrder,
    MaxPositionPerSymbol,
    MaxTotalNotional,
    assert_mainnet_risk_ceiling,
)


# ---------------------------------------------------------------------------
# Fixtures — rules + venues + supervisor configs
# ---------------------------------------------------------------------------


def _tight_rules() -> tuple:
    """All four required rules, every cap at the Phase 6 ceiling."""
    return (
        MaxNotionalPerOrder(usd_cap=Decimal("160")),
        MaxPositionPerSymbol(usd_cap=Decimal("200")),
        MaxTotalNotional(usd_cap=Decimal("200")),
        MaxDrawdownPct(pct=Decimal("0.05")),
    )


def _loose_rules() -> tuple:
    """All four rules present but each cap is too loose for mainnet."""
    return (
        MaxNotionalPerOrder(usd_cap=Decimal("1000")),
        MaxPositionPerSymbol(usd_cap=Decimal("1000")),
        MaxTotalNotional(usd_cap=Decimal("1000")),
        MaxDrawdownPct(pct=Decimal("0.20")),
    )


def _testnet_venues() -> VenuesConfig:
    return VenuesConfig(
        binance=BinanceVenueConfig(
            futures=BinanceFuturesConfig(
                api_key="x",
                api_secret="y",
                key_type="HMAC",
                account_type="USDT_FUTURE",
                environment="TESTNET",
            )
        )
    )


def _mainnet_venues() -> VenuesConfig:
    return VenuesConfig(
        binance=BinanceVenueConfig(
            futures=BinanceFuturesConfig(
                api_key="x",
                api_secret="y",
                key_type="HMAC",
                account_type="USDT_FUTURE",
                environment="LIVE",
            )
        )
    )


def _supervisor_config(
    tmp_path: Path,
    *,
    venues_cfg: Any,
    risk_rules: tuple,
) -> SupervisorConfig:
    return SupervisorConfig(
        instrument_id="SPCXUSDT-PERP.BINANCE",
        strategy_name="momentum_follow",
        signals_root=tmp_path / "signals",
        approvals_root=tmp_path / "approvals",
        cursor_path=tmp_path / "cursor.json",
        sentinel_path=tmp_path / "paused.flag",
        logs_root=tmp_path / "logs",
        strategy_config={"notional_usd": Decimal("100")},
        poll_interval_s=0.0,
        venue_timeout_s=5.0,
        safety_multiplier=Decimal("0.7"),
        risk_rules=risk_rules,
        venues_cfg=venues_cfg,
        bridge=None,
    )


# ---------------------------------------------------------------------------
# Unit — assert_mainnet_risk_ceiling
# ---------------------------------------------------------------------------


def test_unit_tight_rules_pass() -> None:
    """Phase 6 mainnet yaml @ caps exactly at the ceiling must pass."""
    assert_mainnet_risk_ceiling(_tight_rules())  # no raise


def test_unit_loose_notional_per_order_raises() -> None:
    rules = (
        MaxNotionalPerOrder(usd_cap=Decimal("500")),
        MaxPositionPerSymbol(usd_cap=Decimal("200")),
        MaxTotalNotional(usd_cap=Decimal("200")),
        MaxDrawdownPct(pct=Decimal("0.05")),
    )
    with pytest.raises(MainnetRiskTooLooseError, match="MaxNotionalPerOrder"):
        assert_mainnet_risk_ceiling(rules)


def test_unit_loose_position_per_symbol_raises() -> None:
    rules = (
        MaxNotionalPerOrder(usd_cap=Decimal("160")),
        MaxPositionPerSymbol(usd_cap=Decimal("500")),
        MaxTotalNotional(usd_cap=Decimal("200")),
        MaxDrawdownPct(pct=Decimal("0.05")),
    )
    with pytest.raises(MainnetRiskTooLooseError, match="MaxPositionPerSymbol"):
        assert_mainnet_risk_ceiling(rules)


def test_unit_loose_total_notional_raises() -> None:
    rules = (
        MaxNotionalPerOrder(usd_cap=Decimal("160")),
        MaxPositionPerSymbol(usd_cap=Decimal("200")),
        MaxTotalNotional(usd_cap=Decimal("500")),
        MaxDrawdownPct(pct=Decimal("0.05")),
    )
    with pytest.raises(MainnetRiskTooLooseError, match="MaxTotalNotional"):
        assert_mainnet_risk_ceiling(rules)


def test_unit_loose_drawdown_pct_raises() -> None:
    rules = (
        MaxNotionalPerOrder(usd_cap=Decimal("160")),
        MaxPositionPerSymbol(usd_cap=Decimal("200")),
        MaxTotalNotional(usd_cap=Decimal("200")),
        MaxDrawdownPct(pct=Decimal("0.20")),
    )
    with pytest.raises(MainnetRiskTooLooseError, match="MaxDrawdownPct"):
        assert_mainnet_risk_ceiling(rules)


def test_unit_missing_notional_per_order_raises() -> None:
    rules = (
        MaxPositionPerSymbol(usd_cap=Decimal("200")),
        MaxTotalNotional(usd_cap=Decimal("200")),
        MaxDrawdownPct(pct=Decimal("0.05")),
    )
    with pytest.raises(MainnetRiskTooLooseError, match="MaxNotionalPerOrder"):
        assert_mainnet_risk_ceiling(rules)


def test_unit_missing_drawdown_pct_raises() -> None:
    rules = (
        MaxNotionalPerOrder(usd_cap=Decimal("160")),
        MaxPositionPerSymbol(usd_cap=Decimal("200")),
        MaxTotalNotional(usd_cap=Decimal("200")),
    )
    with pytest.raises(MainnetRiskTooLooseError, match="MaxDrawdownPct"):
        assert_mainnet_risk_ceiling(rules)


def test_unit_empty_rules_raises_with_all_missing() -> None:
    with pytest.raises(MainnetRiskTooLooseError, match="missing required rule"):
        assert_mainnet_risk_ceiling(())


def test_unit_custom_ceilings_via_kwargs() -> None:
    """Operator can dial the ceilings via kwargs (used by the brief's
    sample yaml + e2e tests that want a different bar)."""
    rules = (
        MaxNotionalPerOrder(usd_cap=Decimal("80")),
        MaxPositionPerSymbol(usd_cap=Decimal("100")),
        MaxTotalNotional(usd_cap=Decimal("100")),
        MaxDrawdownPct(pct=Decimal("0.02")),
    )
    # Default ceilings (200 / 0.05) — caps are tighter, pass.
    assert_mainnet_risk_ceiling(rules)
    # Tighter ceiling (50) — now MaxNotionalPerOrder.usd_cap=80 > 50.
    with pytest.raises(MainnetRiskTooLooseError, match="MaxNotionalPerOrder"):
        assert_mainnet_risk_ceiling(
            rules, notional_ceiling_usd=Decimal("50")
        )


def test_unit_module_constants_are_decimal() -> None:
    """The public ceiling constants must be Decimal so callers can
    compare/scale them without re-coercing."""
    assert isinstance(MAINNET_MAX_NOTIONAL_CEILING_USD, Decimal)
    assert isinstance(MAINNET_MAX_DRAWDOWN_PCT_CEILING, Decimal)
    assert MAINNET_MAX_NOTIONAL_CEILING_USD == Decimal("200")
    assert MAINNET_MAX_DRAWDOWN_PCT_CEILING == Decimal("0.05")


# ---------------------------------------------------------------------------
# Integration — run_supervisor wiring
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _stub_unlock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Lock 3 (unlock ritual) is tested elsewhere; here we only want
    to prove the risk-ceiling check fires for mainnet venues. Stub
    `assert_mainnet_unlock` so the supervisor passes Lock 3
    unconditionally inside this file."""
    monkeypatch.setattr(
        "xtrade.live.mainnet_unlock.assert_mainnet_unlock",
        lambda *a, **kw: None,
    )


def _stub_executor(*_args: Any, **_kwargs: Any) -> Any:
    """A `live_executor` stub that satisfies the supervisor's call
    signature. Returning None is fine — the supervisor only needs the
    callable; with `max_iterations=0` we never actually invoke it."""
    return None


def test_integration_testnet_loose_rules_pass(tmp_path: Path) -> None:
    """Testnet config skips the ceiling check entirely. Loose rules
    must NOT raise."""
    cfg = _supervisor_config(
        tmp_path, venues_cfg=_testnet_venues(), risk_rules=_loose_rules()
    )
    # max_iterations=0 → loop exits before any signal work; this proves
    # the ceiling gate is the only thing we're exercising.
    run_supervisor(cfg, live_executor=_stub_executor, max_iterations=0)


def test_integration_mainnet_loose_rules_raise(tmp_path: Path) -> None:
    """Mainnet + loose rules must trip `MainnetRiskTooLooseError`
    before any executor or cursor is touched."""
    cfg = _supervisor_config(
        tmp_path, venues_cfg=_mainnet_venues(), risk_rules=_loose_rules()
    )
    with pytest.raises(MainnetRiskTooLooseError):
        run_supervisor(cfg, live_executor=_stub_executor, max_iterations=0)


def test_integration_mainnet_tight_rules_pass(tmp_path: Path) -> None:
    """Mainnet + tight rules (Phase 6 yaml) must pass the gate."""
    cfg = _supervisor_config(
        tmp_path, venues_cfg=_mainnet_venues(), risk_rules=_tight_rules()
    )
    run_supervisor(cfg, live_executor=_stub_executor, max_iterations=0)


def test_integration_mainnet_missing_rule_raises(tmp_path: Path) -> None:
    """Mainnet config with only 3 of the 4 required rules must raise —
    a partial spec is treated as unsafe per Phase 6 brief §5 T2."""
    partial = (
        MaxNotionalPerOrder(usd_cap=Decimal("160")),
        MaxPositionPerSymbol(usd_cap=Decimal("200")),
        MaxTotalNotional(usd_cap=Decimal("200")),
        # MaxDrawdownPct intentionally omitted.
    )
    cfg = _supervisor_config(
        tmp_path, venues_cfg=_mainnet_venues(), risk_rules=partial
    )
    with pytest.raises(MainnetRiskTooLooseError, match="MaxDrawdownPct"):
        run_supervisor(cfg, live_executor=_stub_executor, max_iterations=0)


def test_integration_mainnet_with_repo_yaml_caps_pass(tmp_path: Path) -> None:
    """The actual `config/risk.mainnet.yaml` shipped in the repo must
    pass the ceiling check (regression guard against accidental
    loosening of the committed yaml)."""
    from xtrade.risk.rules import load_rules_from_yaml

    yaml_path = (
        Path(__file__).resolve().parent.parent
        / "config"
        / "risk.mainnet.yaml"
    )
    rules = tuple(load_rules_from_yaml(yaml_path))
    cfg = _supervisor_config(
        tmp_path, venues_cfg=_mainnet_venues(), risk_rules=rules
    )
    run_supervisor(cfg, live_executor=_stub_executor, max_iterations=0)
