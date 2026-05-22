"""Tests for `xtrade.risk.gate` plus the §6 import-graph lint
(Phase 3 Task 2 / T3).

The import-graph lint walks `src/xtrade/strategy/` AST-only and asserts
that no Python source there imports Nautilus execution APIs or calls
`submit_order` — those names exist ONLY inside the runner / Phase 1
live adapter, both of which are downstream of `RiskGate.check`. This
is the mechanical guarantee that strategies cannot ship orders without
going through risk.
"""

from __future__ import annotations

import ast
import datetime as dt
from decimal import Decimal
from pathlib import Path

import pytest

from xtrade.risk import (
    MaxDrawdownPct,
    MaxNotionalPerOrder,
    MaxPositionPerSymbol,
    MaxTotalNotional,
    RiskDecision,
    RiskGate,
)
from xtrade.risk.rules import RiskRule, RuleResult
from xtrade.strategy.base import AccountSnapshot
from xtrade.strategy.intent import OrderIntent


UTC = dt.timezone.utc
SYMBOL = "BTCUSDT-PERP.BINANCE"
REPO_ROOT = Path(__file__).resolve().parents[1]
STRATEGY_PKG = REPO_ROOT / "src" / "xtrade" / "strategy"


def _intent(*, qty: str = "0.01", reduce_only: bool = False) -> OrderIntent:
    return OrderIntent(
        venue="binance",
        symbol=SYMBOL,
        side="BUY",
        order_type="MARKET",
        quantity=Decimal(qty),
        limit_price=None,
        reduce_only=reduce_only,
        time_in_force="IOC",
        source_signal_id="sig-1",
        created_at=dt.datetime(2026, 5, 22, 10, 0, 0, tzinfo=UTC),
    )


def _account(**kwargs) -> AccountSnapshot:  # noqa: ANN003
    defaults = dict(
        cash_usd=Decimal("100000"),
        positions={},
        mark_prices={SYMBOL: Decimal("50000")},
        nav_usd=Decimal("100000"),
        peak_nav_usd=Decimal("100000"),
    )
    defaults.update(kwargs)
    return AccountSnapshot(**defaults)


# ---- RiskDecision --------------------------------------------------------


def test_risk_decision_passed() -> None:
    d = RiskDecision.passed()
    assert d.approve
    assert d.reasons == ()


def test_risk_decision_rejected() -> None:
    d = RiskDecision.rejected(["one", "two"])
    assert not d.approve
    assert d.reasons == ("one", "two")


# ---- RiskGate ------------------------------------------------------------


def test_gate_with_no_rules_approves_everything() -> None:
    gate = RiskGate([])
    decision = gate.check(_intent(), _account())
    assert decision.approve


def test_gate_runs_rules_in_order_and_passes_when_all_ok() -> None:
    gate = RiskGate([
        MaxNotionalPerOrder(Decimal("10000")),
        MaxPositionPerSymbol(Decimal("10000")),
    ])
    assert gate.check(_intent(qty="0.01"), _account()).approve


def test_gate_short_circuits_by_default() -> None:
    # Rule 1 fails; rule 2 would also fail; gate stops after rule 1.
    gate = RiskGate([
        MaxNotionalPerOrder(Decimal("1")),  # 500 > 1, reject
        MaxPositionPerSymbol(Decimal("1")),  # also reject
    ])
    decision = gate.check(_intent(), _account())
    assert not decision.approve
    assert len(decision.reasons) == 1
    assert "max_notional_per_order" in decision.reasons[0]


def test_gate_collects_all_reasons_when_short_circuit_off() -> None:
    gate = RiskGate(
        [
            MaxNotionalPerOrder(Decimal("1")),
            MaxPositionPerSymbol(Decimal("1")),
        ],
        short_circuit=False,
    )
    decision = gate.check(_intent(), _account())
    assert not decision.approve
    assert len(decision.reasons) == 2


def test_gate_describe_returns_rule_metadata() -> None:
    gate = RiskGate([
        MaxNotionalPerOrder(Decimal("1000")),
        MaxDrawdownPct(Decimal("0.10")),
    ])
    desc = gate.describe()
    names = [d["name"] for d in desc]
    assert names == ["max_notional_per_order", "max_drawdown_pct"]


def test_gate_full_default_config_happy() -> None:
    """Smoke: all 4 default rules, modest intent, large account → pass."""
    gate = RiskGate([
        MaxNotionalPerOrder(Decimal("1000")),
        MaxPositionPerSymbol(Decimal("5000")),
        MaxTotalNotional(Decimal("20000")),
        MaxDrawdownPct(Decimal("0.10")),
    ])
    # 0.001 BTC at 50000 = 50 USD notional
    decision = gate.check(_intent(qty="0.001"), _account())
    assert decision.approve


def test_gate_full_default_config_drawdown_blocks() -> None:
    gate = RiskGate([
        MaxNotionalPerOrder(Decimal("1000")),
        MaxDrawdownPct(Decimal("0.05")),
    ])
    acc = _account(nav_usd=Decimal("80000"), peak_nav_usd=Decimal("100000"))
    decision = gate.check(_intent(qty="0.001"), acc)
    assert not decision.approve
    assert any("max_drawdown_pct" in r for r in decision.reasons)


def test_gate_full_default_config_reduce_only_bypasses_drawdown() -> None:
    gate = RiskGate([
        MaxNotionalPerOrder(Decimal("1000")),
        MaxDrawdownPct(Decimal("0.05")),
    ])
    acc = _account(nav_usd=Decimal("80000"), peak_nav_usd=Decimal("100000"))
    decision = gate.check(_intent(qty="0.001", reduce_only=True), acc)
    assert decision.approve


# ---- Custom rules can plug in --------------------------------------------


def test_custom_rule_can_be_added() -> None:
    class _Block(RiskRule):
        name = "blocker"

        def check(self, intent, account):  # noqa: ANN001
            return RuleResult(ok=False, reason="hard block")

    gate = RiskGate([_Block()])
    decision = gate.check(_intent(), _account())
    assert not decision.approve
    assert decision.reasons == ("hard block",)


# ---- §6 import-graph lint -----------------------------------------------


# Names that, if imported / called inside `xtrade.strategy.*`, would
# bypass the runner-mediated RiskGate path.
_FORBIDDEN_IMPORT_ROOTS: frozenset[str] = frozenset({
    "nautilus_trader.execution",
    "nautilus_trader.live.execution_client",
    "nautilus_trader.live.node",
})

_FORBIDDEN_ATTR_NAMES: frozenset[str] = frozenset({
    "submit_order",
    "submit_order_list",
    "modify_order",
    "cancel_all_orders",
})


# `runner.py` is the SOLE module in the strategy package that is allowed
# to call `submit_order` — that is the runner-mediated execution path
# everything else must route through (Phase 3 brief §6). The lint below
# scans all other strategy sources; if you add another runner-like module
# (e.g. a live runner), exempt it here and add a separate test asserting
# its submission path goes through RiskGate + ApprovalGate.
_RUNNER_EXEMPT: frozenset[str] = frozenset({"runner.py"})


def _walk_strategy_sources() -> list[Path]:
    return sorted(
        p for p in STRATEGY_PKG.rglob("*.py")
        if p.name not in _RUNNER_EXEMPT
    )


def test_strategy_package_does_not_import_nautilus_execution() -> None:
    """No file in `src/xtrade/strategy/` may import a forbidden module."""
    offenders: list[str] = []
    for path in _walk_strategy_sources():
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    for root in _FORBIDDEN_IMPORT_ROOTS:
                        if alias.name == root or alias.name.startswith(root + "."):
                            offenders.append(f"{path.name}: import {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                for root in _FORBIDDEN_IMPORT_ROOTS:
                    if mod == root or mod.startswith(root + "."):
                        names = ", ".join(a.name for a in node.names)
                        offenders.append(f"{path.name}: from {mod} import {names}")
    assert not offenders, (
        "strategy package must not import Nautilus execution APIs; "
        f"offenders: {offenders}"
    )


def test_strategy_package_does_not_call_submit_order() -> None:
    """No file in `src/xtrade/strategy/` may reference a forbidden
    execution method name (Attribute or Call)."""
    offenders: list[str] = []
    for path in _walk_strategy_sources():
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            # Attribute access: `self.submit_order`, `client.submit_order`.
            if isinstance(node, ast.Attribute) and node.attr in _FORBIDDEN_ATTR_NAMES:
                offenders.append(f"{path.name}:{node.lineno}: .{node.attr}")
            # Plain name: `submit_order(...)` after `from x import submit_order`.
            elif isinstance(node, ast.Name) and node.id in _FORBIDDEN_ATTR_NAMES:
                offenders.append(f"{path.name}:{node.lineno}: name {node.id}")
    assert not offenders, (
        "strategy package must route through RiskGate; "
        f"forbidden refs: {offenders}"
    )
