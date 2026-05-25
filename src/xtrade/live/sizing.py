"""mcap-anchored sizing primitive (Phase 6 Task T4).

What this module does
---------------------
Given a target "liquidation-price-implied market cap" the strategy is
willing to accept (e.g. SPCXUSDT short is OK only if `P_liq ×
shares_outstanding ≥ $4T`), compute the maximum leverage that keeps
that constraint satisfied at a chosen `avg_entry`.

The primitive is **direction-aware** — short and long mirror each
other through the sign of the `P_liq / avg_entry` term — so the same
class serves the SPCX short instance today and any future long
instance that lands in the same family without code changes.

Math (linear isolated-margin approximation, ignoring funding):

  short:  P_liq ≈ entry × (1 + 1/L − mmr)   →   need P_liq ≥ p_liq
          → L ≤ 1 / (p_liq / entry − 1 + mmr)

  long:   P_liq ≈ entry × (1 − 1/L + mmr)   →   need P_liq ≤ p_liq
          → L ≤ 1 / (1 − p_liq / entry + mmr)

with `p_liq = target_mcap_liq_usd / shares_outstanding`.

If the denominator is ≤ 0 the constraint is unreachable at this
`entry` (entry sits on the wrong side of p_liq; e.g. shorting at
$210 with `p_liq ≈ $337` → `p_liq/entry − 1 + mmr > 0`, denom > 0,
finite L_max; but shorting at $400 with `p_liq ≈ $337` →
`p_liq/entry − 1 + mmr < 0`, denom ≤ 0, any leverage is safe
because the constraint is already satisfied "for free" → `Decimal
("Infinity")`).

Why a separate primitive (vs. inline in the strategy plugin)
-----------------------------------------------------------
- Lets the strategy ctor call `validate_strategy_yaml(...)` at boot
  and refuse to start when the yaml's `leverage:` is inconsistent
  with `target_mcap_liq_usd:` — failing fast at start beats
  discovering the conflict on first fill.
- Lets T5 short instance, the (future) long instance, and any
  ad-hoc backtest reuse the same arithmetic without re-implementing
  the mirror.
- The Decimal-only contract keeps it auditable: no float anywhere on
  the sizing path.

Brief reference: docs/phase6_brief.md §5 T4.
"""

from __future__ import annotations

import dataclasses
from decimal import Decimal
from typing import Literal


Direction = Literal["short", "long"]


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class SizingError(RuntimeError):
    """Base error for `xtrade.live.sizing`."""


class LeverageExceedsMcapCeilingError(SizingError):
    """`validate_strategy_yaml` rejects the configured leverage.

    Brief §5 T4: the message must include direction / entry /
    L_max / requested_L / target_mcap_liq so an operator reading
    the supervisor crash log can immediately identify which knob
    to turn.
    """


# ---------------------------------------------------------------------------
# Sizer
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class McapAnchoredSizer:
    """Compute the leverage ceiling implied by a target P_liq mcap.

    Parameters
    ----------
    target_mcap_liq_usd
        The implied market cap at the liquidation price the operator
        wants to **respect**. For SPCX short this is `4_000_000_000_000`
        ($4T) — i.e. we are willing to lose only if the price moves so
        far against us that SpaceX's implied valuation crosses $4T.
    mmr
        Maintenance margin rate used in the linear P_liq approximation
        (Binance USDS-M Tier 1 is `0.025`). The primitive does not
        consult `exchangeInfo`; the caller wires the right value from
        venue config.
    shares_outstanding
        Reference shares used to convert `p_liq` price ↔ mcap. Must
        match the `InstrumentMeta` value the rest of the system uses.
    direction
        `"short"` or `"long"`. The short and long formulas are the
        mirror of each other around `p_liq / entry == 1`.
    """

    target_mcap_liq_usd: Decimal
    mmr: Decimal
    shares_outstanding: Decimal
    direction: Direction

    def __post_init__(self) -> None:
        for field in ("target_mcap_liq_usd", "mmr", "shares_outstanding"):
            value = getattr(self, field)
            if not isinstance(value, Decimal):
                object.__setattr__(self, field, Decimal(str(value)))
        if self.target_mcap_liq_usd <= 0:
            raise ValueError(
                f"target_mcap_liq_usd must be > 0, got {self.target_mcap_liq_usd}"
            )
        if self.mmr < 0 or self.mmr >= 1:
            raise ValueError(f"mmr must be in [0, 1), got {self.mmr}")
        if self.shares_outstanding <= 0:
            raise ValueError(
                f"shares_outstanding must be > 0, got {self.shares_outstanding}"
            )
        if self.direction not in ("short", "long"):
            raise ValueError(
                f"direction must be 'short' or 'long', got {self.direction!r}"
            )

    # ----- core math -----------------------------------------------------

    @property
    def p_liq(self) -> Decimal:
        """Liquidation price implied by `target_mcap_liq_usd`."""
        return self.target_mcap_liq_usd / self.shares_outstanding

    def max_leverage_for_entry(self, avg_entry: Decimal) -> Decimal:
        """Return the largest leverage L such that the liquidation
        price stays inside the target-mcap band at `avg_entry`.

        Returns `Decimal("Infinity")` when the constraint is already
        satisfied at *any* leverage (entry sits on the wrong side of
        `p_liq`); the caller is then free to use the venue's own
        leverage cap.
        """
        if not isinstance(avg_entry, Decimal):
            avg_entry = Decimal(str(avg_entry))
        if avg_entry <= 0:
            raise ValueError(f"avg_entry must be > 0, got {avg_entry}")
        ratio = self.p_liq / avg_entry
        if self.direction == "short":
            # P_liq above entry; need ratio ≥ 1 + (something positive)
            denom = ratio - Decimal(1) + self.mmr
        else:  # "long"
            # P_liq below entry; need ratio ≤ 1 + (something negative)
            denom = Decimal(1) - ratio + self.mmr
        if denom <= 0:
            return Decimal("Infinity")
        return Decimal(1) / denom

    # ----- yaml validation ----------------------------------------------

    def validate_strategy_yaml(
        self,
        *,
        requested_leverage: Decimal,
        reference_entry: Decimal,
    ) -> None:
        """Raise `LeverageExceedsMcapCeilingError` if the yaml-supplied
        `leverage:` exceeds the mcap-anchored ceiling at the chosen
        `reference_entry` (worst-case avg entry — DCA lower bound for
        short, DCA upper bound for long).
        """
        if not isinstance(requested_leverage, Decimal):
            requested_leverage = Decimal(str(requested_leverage))
        if not isinstance(reference_entry, Decimal):
            reference_entry = Decimal(str(reference_entry))
        if requested_leverage <= 0:
            raise ValueError(
                f"requested_leverage must be > 0, got {requested_leverage}"
            )
        l_max = self.max_leverage_for_entry(reference_entry)
        if l_max.is_infinite():
            return
        if requested_leverage > l_max:
            raise LeverageExceedsMcapCeilingError(
                f"requested leverage {requested_leverage} exceeds mcap-anchored "
                f"ceiling: direction={self.direction} "
                f"entry={reference_entry} L_max={l_max} "
                f"requested_L={requested_leverage} "
                f"target_mcap_liq_usd={self.target_mcap_liq_usd}"
            )
