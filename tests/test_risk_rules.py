"""Tests for `xtrade.risk.rules` (Phase 3 Task 2 / T3)."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path

import pytest

from xtrade.risk.rules import (
    MaxDrawdownPct,
    MaxNotionalPerOrder,
    MaxPositionPerSymbol,
    MaxTotalNotional,
    RuleResult,
    load_rules_from_yaml,
)
from xtrade.strategy.base import AccountSnapshot
from xtrade.strategy.intent import OrderIntent


UTC = dt.timezone.utc
SYMBOL = "BTCUSDT-PERP.BINANCE"


def _intent(
    side: str = "BUY",
    qty: str = "0.01",
    *,
    reduce_only: bool = False,
    order_type: str = "MARKET",
    limit_price: str | None = None,
) -> OrderIntent:
    return OrderIntent(
        venue="binance",
        symbol=SYMBOL,
        side=side,  # type: ignore[arg-type]
        order_type=order_type,  # type: ignore[arg-type]
        quantity=Decimal(qty),
        limit_price=Decimal(limit_price) if limit_price else None,
        reduce_only=reduce_only,
        time_in_force="IOC",
        source_signal_id="sig-1",
        created_at=dt.datetime(2026, 5, 22, 10, 0, 0, tzinfo=UTC),
    )


def _account(
    *,
    cash: str = "100000",
    positions: dict[str, str] | None = None,
    marks: dict[str, str] | None = None,
    nav: str = "100000",
    peak: str = "100000",
) -> AccountSnapshot:
    pos = {k: Decimal(v) for k, v in (positions or {}).items()}
    if marks is None:
        marks = {SYMBOL: "50000"}
    mk = {k: Decimal(v) for k, v in marks.items()}
    return AccountSnapshot(
        cash_usd=Decimal(cash),
        positions=pos,
        mark_prices=mk,
        nav_usd=Decimal(nav),
        peak_nav_usd=Decimal(peak),
    )


# ---- MaxNotionalPerOrder -------------------------------------------------


def test_notional_per_order_under_cap_passes() -> None:
    rule = MaxNotionalPerOrder(Decimal("1000"))
    # 0.01 * 50000 = 500 < 1000
    assert rule.check(_intent(qty="0.01"), _account()).ok


def test_notional_per_order_at_cap_passes() -> None:
    rule = MaxNotionalPerOrder(Decimal("500"))
    # exactly 0.01 * 50000 = 500
    assert rule.check(_intent(qty="0.01"), _account()).ok


def test_notional_per_order_above_cap_rejects() -> None:
    rule = MaxNotionalPerOrder(Decimal("499"))
    result = rule.check(_intent(qty="0.01"), _account())
    assert not result.ok
    assert "max_notional_per_order" in result.reason


def test_notional_falls_back_to_limit_price_when_no_mark() -> None:
    rule = MaxNotionalPerOrder(Decimal("100"))
    intent = _intent(
        qty="0.001", order_type="LIMIT", limit_price="50000"
    )
    # 0.001 * 50000 = 50 < 100
    acc = _account(marks={})
    assert rule.check(intent, acc).ok


def test_notional_rejects_when_no_mark_and_no_limit() -> None:
    rule = MaxNotionalPerOrder(Decimal("1000"))
    intent = _intent(qty="0.01", order_type="MARKET")
    acc = _account(marks={})
    result = rule.check(intent, acc)
    assert not result.ok
    assert "no mark price" in result.reason


def test_notional_init_rejects_non_positive_cap() -> None:
    with pytest.raises(ValueError):
        MaxNotionalPerOrder(Decimal("0"))
    with pytest.raises(ValueError):
        MaxNotionalPerOrder(Decimal("-1"))


# ---- MaxPositionPerSymbol ------------------------------------------------


def test_position_per_symbol_growing_long_under_cap_passes() -> None:
    rule = MaxPositionPerSymbol(Decimal("5000"))
    # current 0.05 long * 50000 = 2500; intent +0.05 → 0.10 * 50000 = 5000
    acc = _account(positions={SYMBOL: "0.05"})
    assert rule.check(_intent(side="BUY", qty="0.05"), acc).ok


def test_position_per_symbol_growing_long_above_cap_rejects() -> None:
    rule = MaxPositionPerSymbol(Decimal("5000"))
    # 0.10 * 50000 = 5000 already at cap; +0.001 takes us over
    acc = _account(positions={SYMBOL: "0.10"})
    result = rule.check(_intent(side="BUY", qty="0.001"), acc)
    assert not result.ok


def test_position_per_symbol_flipping_to_short_uses_absolute() -> None:
    rule = MaxPositionPerSymbol(Decimal("5000"))
    # current +0.10 (= 5000 long); SELL 0.21 → -0.11 (5500 short)
    acc = _account(positions={SYMBOL: "0.10"})
    result = rule.check(_intent(side="SELL", qty="0.21"), acc)
    assert not result.ok


def test_position_per_symbol_closing_passes() -> None:
    rule = MaxPositionPerSymbol(Decimal("1000"))
    # current +0.20 (= 10000); SELL 0.20 → 0 (= 0 < 1000)
    acc = _account(positions={SYMBOL: "0.20"})
    assert rule.check(_intent(side="SELL", qty="0.20"), acc).ok


# ---- MaxTotalNotional ----------------------------------------------------


def test_total_notional_single_symbol_under_cap_passes() -> None:
    rule = MaxTotalNotional(Decimal("10000"))
    acc = _account()
    # intent: +0.01 * 50000 = 500
    assert rule.check(_intent(qty="0.01"), acc).ok


def test_total_notional_multi_symbol_above_cap_rejects() -> None:
    rule = MaxTotalNotional(Decimal("10000"))
    # existing ETH position: 1.0 * 3000 = 3000
    # existing BTC position: 0.10 * 50000 = 5000
    # buying +0.05 BTC → 0.15 * 50000 = 7500 → total 7500 + ETH 3000 = 10500
    acc = _account(
        positions={SYMBOL: "0.10", "ETHUSDT-PERP.BINANCE": "1.0"},
        marks={SYMBOL: "50000", "ETHUSDT-PERP.BINANCE": "3000"},
    )
    result = rule.check(_intent(side="BUY", qty="0.05"), acc)
    assert not result.ok
    assert "max_total_notional" in result.reason


def test_total_notional_missing_other_symbol_mark_rejects() -> None:
    rule = MaxTotalNotional(Decimal("10000"))
    # ETH position present but ETH mark missing → cannot compute total
    acc = _account(
        positions={"ETHUSDT-PERP.BINANCE": "1.0"},
        marks={SYMBOL: "50000"},
    )
    result = rule.check(_intent(side="BUY", qty="0.01"), acc)
    assert not result.ok
    assert "missing mark for ETHUSDT-PERP.BINANCE" in result.reason


def test_total_notional_zero_positions_other_symbols_ignored() -> None:
    rule = MaxTotalNotional(Decimal("10000"))
    # Other symbol present but zero qty → ignored even without mark.
    acc = _account(
        positions={"ETHUSDT-PERP.BINANCE": "0"},
        marks={SYMBOL: "50000"},
    )
    assert rule.check(_intent(side="BUY", qty="0.01"), acc).ok


# ---- MaxDrawdownPct ------------------------------------------------------


def test_drawdown_under_cap_passes() -> None:
    rule = MaxDrawdownPct(Decimal("0.10"))
    acc = _account(nav="95000", peak="100000")  # 5% drawdown
    assert rule.check(_intent(), acc).ok


def test_drawdown_above_cap_blocks_new_exposure() -> None:
    rule = MaxDrawdownPct(Decimal("0.05"))
    acc = _account(nav="90000", peak="100000")  # 10% drawdown
    result = rule.check(_intent(reduce_only=False), acc)
    assert not result.ok
    assert "max_drawdown_pct" in result.reason


def test_drawdown_above_cap_lets_reduce_only_through() -> None:
    rule = MaxDrawdownPct(Decimal("0.05"))
    acc = _account(nav="90000", peak="100000")
    # reduce_only=True should bypass the block
    assert rule.check(_intent(reduce_only=True), acc).ok


def test_drawdown_at_exact_cap_passes() -> None:
    rule = MaxDrawdownPct(Decimal("0.10"))
    acc = _account(nav="90000", peak="100000")  # exactly 10%
    # Boundary: drawdown == cap → ok (strictly greater rejects).
    assert rule.check(_intent(), acc).ok


def test_drawdown_zero_peak_passes_trivially() -> None:
    rule = MaxDrawdownPct(Decimal("0.10"))
    acc = _account(nav="0", peak="0")
    assert rule.check(_intent(), acc).ok


def test_drawdown_init_rejects_out_of_range_pct() -> None:
    with pytest.raises(ValueError):
        MaxDrawdownPct(Decimal("0"))
    with pytest.raises(ValueError):
        MaxDrawdownPct(Decimal("1"))
    with pytest.raises(ValueError):
        MaxDrawdownPct(Decimal("-0.1"))


# ---- YAML loader ---------------------------------------------------------


def test_load_rules_from_yaml_happy(tmp_path: Path) -> None:
    yaml_path = tmp_path / "risk.yaml"
    yaml_path.write_text(
        "max_notional_per_order_usd: 1000\n"
        "max_position_per_symbol_usd: 5000\n"
        "max_total_notional_usd: 20000\n"
        "max_drawdown_pct: 0.10\n"
    )
    rules = load_rules_from_yaml(yaml_path)
    assert len(rules) == 4
    assert {type(r).__name__ for r in rules} == {
        "MaxNotionalPerOrder",
        "MaxPositionPerSymbol",
        "MaxTotalNotional",
        "MaxDrawdownPct",
    }


def test_load_rules_from_yaml_partial(tmp_path: Path) -> None:
    yaml_path = tmp_path / "risk.yaml"
    yaml_path.write_text("max_drawdown_pct: 0.05\n")
    rules = load_rules_from_yaml(yaml_path)
    assert len(rules) == 1
    assert isinstance(rules[0], MaxDrawdownPct)
    assert rules[0].pct == Decimal("0.05")


def test_load_rules_from_yaml_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_rules_from_yaml(tmp_path / "nope.yaml")


def test_load_rules_from_yaml_non_mapping_root_raises(tmp_path: Path) -> None:
    yaml_path = tmp_path / "risk.yaml"
    yaml_path.write_text("- not\n- a\n- mapping\n")
    with pytest.raises(ValueError):
        load_rules_from_yaml(yaml_path)


def test_load_rules_from_yaml_empty_yields_empty_list(tmp_path: Path) -> None:
    yaml_path = tmp_path / "risk.yaml"
    yaml_path.write_text("")
    assert load_rules_from_yaml(yaml_path) == []


def test_example_risk_yaml_is_loadable() -> None:
    """`config/risk.example.yaml` should be a valid load."""
    repo_root = Path(__file__).resolve().parents[1]
    example = repo_root / "config" / "risk.example.yaml"
    assert example.exists()
    rules = load_rules_from_yaml(example)
    assert len(rules) == 4
