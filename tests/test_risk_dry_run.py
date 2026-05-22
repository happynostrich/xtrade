"""Tests for `xtrade.risk.dry_run` (Phase 3.5 hardening).

`dry_run(...)` is the pre-flight calibration helper that walks the same
chain `xtrade.strategy.runner` walks (`strategy.on_signal` → `RiskGate`)
without any side effects. These tests pin:

  - the no-I/O contract (no file / network ops are reachable from the
    helper);
  - the full-matrix verdict shape (every rule's verdict is recorded for
    every intent — no short-circuit);
  - the JSON-round-trip shape of the report.
"""

from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal

import xtrade.strategy  # noqa: F401 — registers momentum_follow
from xtrade.research.signals import Signal
from xtrade.risk.dry_run import DryRunReport, IntentEvaluation, dry_run
from xtrade.risk.rules import (
    MaxDrawdownPct,
    MaxNotionalPerOrder,
    MaxPositionPerSymbol,
    MaxTotalNotional,
)
from xtrade.strategy.base import AccountSnapshot, load_strategy


UTC = dt.timezone.utc


def _signal(*, direction: str = "LONG", strength: float = 0.6, last_price: str = "50000") -> Signal:
    return Signal(
        symbol="BTCUSDT-PERP.BINANCE",
        venue="binance",
        direction=direction,  # type: ignore[arg-type]
        strength=strength if direction == "LONG" else (-strength if direction == "SHORT" else 0.0),
        generated_at=dt.datetime(2026, 5, 22, 12, 0, 0, tzinfo=UTC),
        source="momentum:dryrun",
        metadata={"last_price": last_price},
    )


def _account(
    *,
    cash: str = "10000",
    positions: dict[str, str] | None = None,
    marks: dict[str, str] | None = None,
    nav: str | None = None,
    peak_nav: str | None = None,
) -> AccountSnapshot:
    pos = {k: Decimal(v) for k, v in (positions or {}).items()}
    mk = {k: Decimal(v) for k, v in (marks or {"BTCUSDT-PERP.BINANCE": "50000"}).items()}
    nav_d = Decimal(nav) if nav is not None else Decimal(cash)
    peak_d = Decimal(peak_nav) if peak_nav is not None else nav_d
    return AccountSnapshot(
        cash_usd=Decimal(cash),
        positions=pos,
        mark_prices=mk,
        nav_usd=nav_d,
        peak_nav_usd=peak_d,
    )


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


def test_dry_run_emits_intent_and_all_rules_pass() -> None:
    """Modest signal + generous caps → 1 intent, every rule OK."""
    strat = load_strategy("momentum_follow", config={"notional_usd": "500", "qty_step": "0.001"})
    sig = _signal()
    acc = _account(cash="100000")

    rules = [
        MaxNotionalPerOrder(Decimal("1000")),
        MaxPositionPerSymbol(Decimal("5000")),
        MaxTotalNotional(Decimal("20000")),
        MaxDrawdownPct(Decimal("0.10")),
    ]

    report = dry_run(strategy=strat, signal=sig, account=acc, rules=rules)
    assert report.intents_generated == 1
    assert report.intents_approved == 1
    assert report.intents_rejected == 0

    ev = report.intents[0]
    assert len(ev.rule_results) == 4
    assert all(r["ok"] for r in ev.rule_results)
    assert ev.aggregate_approved is True


def test_dry_run_full_matrix_no_short_circuit() -> None:
    """Every rule is evaluated even after one fails (audit-friendly)."""
    strat = load_strategy("momentum_follow", config={"notional_usd": "500", "qty_step": "0.001"})
    sig = _signal()
    acc = _account(cash="100000")

    rules = [
        MaxNotionalPerOrder(Decimal("1")),  # impossibly tight → fails
        MaxPositionPerSymbol(Decimal("5000")),  # passes
        MaxTotalNotional(Decimal("20000")),  # passes
    ]
    report = dry_run(strategy=strat, signal=sig, account=acc, rules=rules)
    ev = report.intents[0]
    # All three rules' verdicts are present, not just the first failure.
    assert [r["name"] for r in ev.rule_results] == [
        "max_notional_per_order",
        "max_position_per_symbol",
        "max_total_notional",
    ]
    assert ev.rule_results[0]["ok"] is False
    assert ev.rule_results[1]["ok"] is True
    assert ev.rule_results[2]["ok"] is True
    assert ev.aggregate_approved is False


def test_dry_run_per_order_notional_calibration() -> None:
    """The exact cap that flips a single intent from approve to reject."""
    strat = load_strategy("momentum_follow", config={"notional_usd": "500", "qty_step": "0.001"})
    sig = _signal()
    acc = _account(cash="100000")

    # qty = 500/50000 = 0.01 BTC, notional ≈ 500 USD.
    # Cap 600 → passes; cap 400 → fails.
    above = dry_run(strategy=strat, signal=sig, account=acc, rules=[MaxNotionalPerOrder(Decimal("600"))])
    below = dry_run(strategy=strat, signal=sig, account=acc, rules=[MaxNotionalPerOrder(Decimal("400"))])
    assert above.intents_approved == 1
    assert below.intents_approved == 0
    assert "notional" in below.intents[0].rule_results[0]["reason"]


def test_dry_run_flat_signal_emits_no_intent_when_flat() -> None:
    """FLAT signal + zero position → strategy returns empty → empty report."""
    strat = load_strategy("momentum_follow", config={"notional_usd": "500", "qty_step": "0.001"})
    sig = _signal(direction="FLAT", strength=0.0)
    acc = _account()
    report = dry_run(strategy=strat, signal=sig, account=acc, rules=[])
    assert report.intents_generated == 0
    assert report.intents == ()


def test_dry_run_drawdown_blocks_only_non_reduce_only() -> None:
    """`MaxDrawdownPct` lets `reduce_only` intents through, blocks the rest."""
    strat = load_strategy("momentum_follow", config={"notional_usd": "500", "qty_step": "0.001"})
    sig = _signal()
    # Cash 10000, NAV 10000, peak NAV 20000 → drawdown = 0.5, cap 0.10
    # → drawdown trips, non-reduce_only intent should be rejected.
    acc = _account(cash="10000", nav="10000", peak_nav="20000")

    rules = [MaxDrawdownPct(Decimal("0.10"))]
    report = dry_run(strategy=strat, signal=sig, account=acc, rules=rules)
    assert report.intents_generated == 1
    # MomentumFollow's default intent is NOT reduce_only → rejected.
    assert report.intents_approved == 0
    assert "drawdown" in report.intents[0].rule_results[0]["reason"]


# ---------------------------------------------------------------------------
# Report shape / serialisation
# ---------------------------------------------------------------------------


def test_dry_run_report_to_dict_is_json_safe() -> None:
    """The report's `to_dict()` round-trips through `json.dumps` cleanly."""
    strat = load_strategy("momentum_follow", config={"notional_usd": "500", "qty_step": "0.001"})
    sig = _signal()
    acc = _account(cash="100000")
    rules = [MaxNotionalPerOrder(Decimal("1000"))]
    report = dry_run(strategy=strat, signal=sig, account=acc, rules=rules)

    payload = report.to_dict()
    # `default=str` is allowed for unforeseen types but we want the
    # common types serialisable natively.
    rendered = json.loads(json.dumps(payload, default=str))
    assert rendered["strategy"] == "momentum_follow"
    assert rendered["intents_generated"] == 1
    assert rendered["intents_approved"] == 1
    assert rendered["account"]["cash_usd"] == "100000"
    assert rendered["account"]["mark_prices"]["BTCUSDT-PERP.BINANCE"] == "50000"
    intent_dict = rendered["intents"][0]["intent"]
    assert intent_dict["side"] in {"BUY", "SELL"}
    # `quantity` must round-trip through Decimal.
    Decimal(intent_dict["quantity"])


def test_dry_run_account_decimal_strings_round_trip() -> None:
    """Every `*_usd` / `quantity` / `price` field in the report is `Decimal(s)`-stable."""
    strat = load_strategy("momentum_follow", config={"notional_usd": "500", "qty_step": "0.001"})
    sig = _signal()
    # Use a short position so the LONG signal flips → produces both the
    # reduce-only close and the fresh long intent. Cash + position
    # values are deliberately non-round to exercise Decimal precision.
    acc = _account(
        cash="12345.6789",
        positions={"BTCUSDT-PERP.BINANCE": "-0.005"},
    )
    rules = [MaxNotionalPerOrder(Decimal("100000"))]
    report = dry_run(strategy=strat, signal=sig, account=acc, rules=rules)
    payload = report.to_dict()

    Decimal(payload["account"]["cash_usd"])
    Decimal(payload["account"]["nav_usd"])
    Decimal(payload["account"]["peak_nav_usd"])
    Decimal(payload["account"]["positions"]["BTCUSDT-PERP.BINANCE"])
    assert len(payload["intents"]) >= 1
    Decimal(payload["intents"][0]["intent"]["quantity"])


# ---------------------------------------------------------------------------
# No-I/O contract (defensive)
# ---------------------------------------------------------------------------


def test_dry_run_module_has_no_disk_or_network_imports() -> None:
    """`xtrade.risk.dry_run` must not pull in `requests`, `socket`, etc."""
    import ast
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "src" / "xtrade" / "risk" / "dry_run.py"
    tree = ast.parse(src.read_text(encoding="utf-8"))
    forbidden_modules = {
        "requests",
        "socket",
        "http",
        "urllib",
        "asyncio",
        "aiohttp",
        "nautilus_trader",
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                assert root not in forbidden_modules, (
                    f"dry_run.py must not import {alias.name!r}"
                )
        if isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            assert root not in forbidden_modules, (
                f"dry_run.py must not import from {node.module!r}"
            )
