"""Built-in risk rules (Phase 3 Task 2 / T3).

Each rule implements `check(intent, account) -> RuleResult`:
returning `RuleResult(ok=True)` lets the next rule run, or
`RuleResult(ok=False, reason=...)` short-circuits the gate.

All money math goes through `Decimal`. The rules consult the
`AccountSnapshot` for current cash, positions, mark prices, NAV and
peak NAV — they NEVER reach into Nautilus state directly.

Drawdown semantics
------------------
`MaxDrawdownPct` is asymmetric: when current drawdown exceeds the cap,
it BLOCKS any intent that would *open or extend* exposure, but lets
`reduce_only` intents through so the strategy can still de-risk.
"""

from __future__ import annotations

import abc
import dataclasses
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from xtrade.strategy.base import AccountSnapshot
    from xtrade.strategy.intent import OrderIntent


@dataclasses.dataclass(frozen=True)
class RuleResult:
    """Outcome of one rule check. `ok=True` passes; `ok=False` rejects."""

    ok: bool
    reason: str = ""


class RiskRule(abc.ABC):
    """Abstract risk rule. Subclasses override `check`."""

    #: Short identifier used in `RiskDecision.reasons`.
    name: str = "rule"

    @abc.abstractmethod
    def check(
        self,
        intent: "OrderIntent",
        account: "AccountSnapshot",
    ) -> RuleResult: ...

    def describe(self) -> dict[str, Any]:
        return {"name": self.name, "class": type(self).__qualname__}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _signed_delta(intent: "OrderIntent") -> Decimal:
    """Position-units delta this intent would apply: +qty BUY, -qty SELL."""
    return intent.quantity if intent.side == "BUY" else -intent.quantity


def _mark_price(intent: "OrderIntent", account: "AccountSnapshot") -> Decimal | None:
    """Resolve mark price for sizing: prefer the account mark, fall back
    to the intent's `limit_price` when present."""
    mark = account.mark_of(intent.symbol)
    if mark is not None:
        return mark
    if intent.limit_price is not None:
        return intent.limit_price
    return None


# ---------------------------------------------------------------------------
# Concrete rules
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class MaxNotionalPerOrder(RiskRule):
    """Reject single intents whose notional exceeds `usd_cap`."""

    usd_cap: Decimal
    name: str = "max_notional_per_order"

    def __post_init__(self) -> None:
        if not isinstance(self.usd_cap, Decimal):
            object.__setattr__(self, "usd_cap", Decimal(self.usd_cap))
        if self.usd_cap <= 0:
            raise ValueError(f"usd_cap must be > 0, got {self.usd_cap}")

    def check(self, intent, account):  # noqa: ANN001
        mark = _mark_price(intent, account)
        if mark is None:
            return RuleResult(
                ok=False,
                reason=(
                    f"{self.name}: no mark price for {intent.symbol}; "
                    f"cannot size intent"
                ),
            )
        notional = (intent.quantity * mark).copy_abs()
        if notional > self.usd_cap:
            return RuleResult(
                ok=False,
                reason=(
                    f"{self.name}: notional {notional} > cap {self.usd_cap}"
                ),
            )
        return RuleResult(ok=True)


@dataclasses.dataclass(frozen=True)
class MaxPositionPerSymbol(RiskRule):
    """Reject intents that would push abs(position * mark) over `usd_cap`."""

    usd_cap: Decimal
    name: str = "max_position_per_symbol"

    def __post_init__(self) -> None:
        if not isinstance(self.usd_cap, Decimal):
            object.__setattr__(self, "usd_cap", Decimal(self.usd_cap))
        if self.usd_cap <= 0:
            raise ValueError(f"usd_cap must be > 0, got {self.usd_cap}")

    def check(self, intent, account):  # noqa: ANN001
        mark = _mark_price(intent, account)
        if mark is None:
            return RuleResult(
                ok=False,
                reason=(
                    f"{self.name}: no mark price for {intent.symbol}; "
                    f"cannot size intent"
                ),
            )
        current = account.position_of(intent.symbol)
        projected = current + _signed_delta(intent)
        projected_notional = (projected * mark).copy_abs()
        if projected_notional > self.usd_cap:
            return RuleResult(
                ok=False,
                reason=(
                    f"{self.name}: projected {projected_notional} > cap "
                    f"{self.usd_cap} for {intent.symbol}"
                ),
            )
        return RuleResult(ok=True)


@dataclasses.dataclass(frozen=True)
class MaxTotalNotional(RiskRule):
    """Reject intents that would push gross portfolio notional over cap.

    Gross notional = sum over symbols of |position_i * mark_i|. The
    intent's projected delta is applied to its own symbol before the
    sum.
    """

    usd_cap: Decimal
    name: str = "max_total_notional"

    def __post_init__(self) -> None:
        if not isinstance(self.usd_cap, Decimal):
            object.__setattr__(self, "usd_cap", Decimal(self.usd_cap))
        if self.usd_cap <= 0:
            raise ValueError(f"usd_cap must be > 0, got {self.usd_cap}")

    def check(self, intent, account):  # noqa: ANN001
        mark = _mark_price(intent, account)
        if mark is None:
            return RuleResult(
                ok=False,
                reason=(
                    f"{self.name}: no mark price for {intent.symbol}"
                ),
            )
        # Project the intent onto the symbol's position.
        projected = dict(account.positions)
        projected[intent.symbol] = projected.get(intent.symbol, Decimal(0)) + _signed_delta(intent)

        total = Decimal(0)
        for sym, pos in projected.items():
            if pos == 0:
                continue
            sym_mark = mark if sym == intent.symbol else account.mark_of(sym)
            if sym_mark is None:
                return RuleResult(
                    ok=False,
                    reason=(
                        f"{self.name}: missing mark for {sym}; cannot "
                        f"compute total notional"
                    ),
                )
            total += (pos * sym_mark).copy_abs()
        if total > self.usd_cap:
            return RuleResult(
                ok=False,
                reason=(
                    f"{self.name}: projected total {total} > cap {self.usd_cap}"
                ),
            )
        return RuleResult(ok=True)


@dataclasses.dataclass(frozen=True)
class MaxDrawdownPct(RiskRule):
    """Block new exposure once drawdown from peak NAV exceeds `pct`.

    Passes intents with `reduce_only=True` so the strategy can still
    de-risk during the trip-out.
    """

    pct: Decimal
    name: str = "max_drawdown_pct"

    def __post_init__(self) -> None:
        if not isinstance(self.pct, Decimal):
            object.__setattr__(self, "pct", Decimal(self.pct))
        if not (Decimal(0) < self.pct < Decimal(1)):
            raise ValueError(f"pct must be in (0, 1), got {self.pct}")

    def check(self, intent, account):  # noqa: ANN001
        if account.peak_nav_usd <= 0:
            return RuleResult(ok=True)
        drawdown = (account.peak_nav_usd - account.nav_usd) / account.peak_nav_usd
        if drawdown <= self.pct:
            return RuleResult(ok=True)
        if intent.reduce_only:
            return RuleResult(ok=True)
        return RuleResult(
            ok=False,
            reason=(
                f"{self.name}: drawdown {drawdown:.4f} > cap {self.pct}; "
                f"blocking non-reduce_only intents"
            ),
        )


# ---------------------------------------------------------------------------
# YAML loader
# ---------------------------------------------------------------------------


def load_rules_from_yaml(path: str | Path) -> list[RiskRule]:
    """Build the rule list from `config/risk.example.yaml` shape.

    Expected keys (all optional; missing → rule not installed):
      max_notional_per_order_usd:  number
      max_position_per_symbol_usd: number
      max_total_notional_usd:      number
      max_drawdown_pct:            number in (0, 1)
    """
    import yaml  # local import: yaml is already a dependency

    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"risk config not found: {path}")
    raw = yaml.safe_load(path.read_text()) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"risk config root must be a mapping, got {type(raw).__name__}")

    rules: list[RiskRule] = []
    if "max_notional_per_order_usd" in raw:
        rules.append(MaxNotionalPerOrder(Decimal(str(raw["max_notional_per_order_usd"]))))
    if "max_position_per_symbol_usd" in raw:
        rules.append(MaxPositionPerSymbol(Decimal(str(raw["max_position_per_symbol_usd"]))))
    if "max_total_notional_usd" in raw:
        rules.append(MaxTotalNotional(Decimal(str(raw["max_total_notional_usd"]))))
    if "max_drawdown_pct" in raw:
        rules.append(MaxDrawdownPct(Decimal(str(raw["max_drawdown_pct"]))))
    return rules
