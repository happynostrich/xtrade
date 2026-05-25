"""Tests for `xtrade.live.sizing.McapAnchoredSizer` (Phase 6 Task T4).

Brief §5 T4 contract surfaces under test:
  1. Table-driven `max_leverage_for_entry` across short / long × 4
     entries × 3 mcap_target × 3 MMR (72 cases — generated below).
  2. Wrong-side-of-p_liq boundary → `Decimal("Infinity")`.
  3. `validate_strategy_yaml`: requested ≤ L_max passes; requested >
     L_max raises `LeverageExceedsMcapCeilingError` with a message
     that names direction / entry / L_max / requested_L /
     target_mcap_liq.
  4. SPCX reference numbers: short @ $210 / $225, target $4T,
     mmr=0.025 → L_max comfortably > 1× (the operator-configured
     1× isolated must pass).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

import pytest

from xtrade.live.sizing import (
    Direction,
    LeverageExceedsMcapCeilingError,
    McapAnchoredSizer,
    SizingError,
)


# ---------------------------------------------------------------------------
# Reference values (SPCX baseline)
# ---------------------------------------------------------------------------

_SHARES = Decimal("11870000000")
_TARGET_4T = Decimal("4000000000000")
_MMR_BINANCE = Decimal("0.025")


def _sizer(
    *,
    target: Decimal = _TARGET_4T,
    mmr: Decimal = _MMR_BINANCE,
    shares: Decimal = _SHARES,
    direction: Direction = "short",
) -> McapAnchoredSizer:
    return McapAnchoredSizer(
        target_mcap_liq_usd=target,
        mmr=mmr,
        shares_outstanding=shares,
        direction=direction,
    )


def _expected_l_max(
    *,
    entry: Decimal,
    target: Decimal,
    mmr: Decimal,
    shares: Decimal,
    direction: Direction,
) -> Decimal:
    """Pure-Python reference implementation; the production code
    must match this for every parametrize row."""
    p_liq = target / shares
    ratio = p_liq / entry
    if direction == "short":
        denom = ratio - Decimal(1) + mmr
    else:
        denom = Decimal(1) - ratio + mmr
    if denom <= 0:
        return Decimal("Infinity")
    return Decimal(1) / denom


# ---------------------------------------------------------------------------
# 1. Table-driven short / long × entry × mcap × MMR
# ---------------------------------------------------------------------------


_ENTRIES = (Decimal("100"), Decimal("210"), Decimal("225"), Decimal("400"))
_MCAP_TARGETS = (
    Decimal("2000000000000"),   # $2T
    Decimal("4000000000000"),   # $4T (SPCX baseline)
    Decimal("8000000000000"),   # $8T
)
_MMRS = (Decimal("0.005"), Decimal("0.025"), Decimal("0.1"))
_DIRECTIONS: tuple[Direction, ...] = ("short", "long")


@pytest.mark.parametrize("direction", _DIRECTIONS)
@pytest.mark.parametrize("mmr", _MMRS)
@pytest.mark.parametrize("target", _MCAP_TARGETS)
@pytest.mark.parametrize("entry", _ENTRIES)
def test_max_leverage_matches_reference_impl(
    entry: Decimal, target: Decimal, mmr: Decimal, direction: Direction
) -> None:
    sizer = _sizer(target=target, mmr=mmr, direction=direction)
    expected = _expected_l_max(
        entry=entry,
        target=target,
        mmr=mmr,
        shares=_SHARES,
        direction=direction,
    )
    actual = sizer.max_leverage_for_entry(entry)
    if expected.is_infinite():
        assert actual.is_infinite()
    else:
        assert actual == expected


# ---------------------------------------------------------------------------
# 2. Boundary: wrong-side-of-p_liq → Infinity
# ---------------------------------------------------------------------------


def test_short_above_p_liq_returns_infinity() -> None:
    """Shorting at $400 when p_liq ≈ $337 means the constraint
    ($4T ≤ P_liq mcap) is already satisfied at *any* L."""
    sizer = _sizer(direction="short")
    # p_liq = 4e12 / 11.87e9 ≈ 336.98 — entry 400 is above p_liq.
    assert sizer.max_leverage_for_entry(Decimal("400")).is_infinite()


def test_long_below_p_liq_returns_infinity() -> None:
    """Long at $210 when p_liq ≈ $337 means P_liq (below entry) is
    trivially below p_liq for any L — constraint is free."""
    sizer = _sizer(direction="long")
    assert sizer.max_leverage_for_entry(Decimal("210")).is_infinite()


def test_short_at_exact_p_liq_is_finite() -> None:
    """At entry == p_liq, denom == mmr → L_max == 1 / mmr."""
    sizer = _sizer(direction="short")
    p_liq = sizer.p_liq
    assert sizer.max_leverage_for_entry(p_liq) == Decimal(1) / _MMR_BINANCE


# ---------------------------------------------------------------------------
# 3. SPCX reference numbers — operator-configured 1× isolated must pass
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "entry,expected_min_l_max",
    [
        (Decimal("210"), Decimal("1.5")),   # brief: 1× has ≥ 1.5× safety margin
        (Decimal("225"), Decimal("1.5")),
    ],
)
def test_spcx_short_one_isolated_well_within_ceiling(
    entry: Decimal, expected_min_l_max: Decimal
) -> None:
    sizer = _sizer(direction="short")
    l_max = sizer.max_leverage_for_entry(entry)
    assert not l_max.is_infinite()
    assert l_max > expected_min_l_max


def test_validate_passes_at_one_isolated_dca_floor() -> None:
    """Short instance ctor calls validate at requested_leverage=1
    and reference_entry=$210 (DCA lower bound)."""
    sizer = _sizer(direction="short")
    sizer.validate_strategy_yaml(
        requested_leverage=Decimal(1),
        reference_entry=Decimal("210"),
    )  # no raise


# ---------------------------------------------------------------------------
# 4. validate_strategy_yaml error surface
# ---------------------------------------------------------------------------


def test_validate_raises_when_leverage_exceeds_ceiling() -> None:
    sizer = _sizer(direction="short")
    # At entry=210, L_max ≈ 1.588. Request L=5 → exceeds.
    with pytest.raises(LeverageExceedsMcapCeilingError) as exc:
        sizer.validate_strategy_yaml(
            requested_leverage=Decimal(5),
            reference_entry=Decimal("210"),
        )
    msg = str(exc.value)
    # Brief §5 T4: error must name all five operator-facing fields.
    assert "direction=short" in msg
    assert "entry=210" in msg
    assert "L_max=" in msg
    assert "requested_L=5" in msg
    assert "target_mcap_liq_usd=4000000000000" in msg


def test_validate_raises_inherits_sizing_error() -> None:
    sizer = _sizer(direction="short")
    with pytest.raises(SizingError):
        sizer.validate_strategy_yaml(
            requested_leverage=Decimal(100),
            reference_entry=Decimal("210"),
        )


def test_validate_passes_when_constraint_unreachable() -> None:
    """Infinity → any requested_leverage is fine."""
    sizer = _sizer(direction="short")
    sizer.validate_strategy_yaml(
        requested_leverage=Decimal(125),
        reference_entry=Decimal("400"),  # above p_liq → Infinity
    )  # no raise


def test_validate_long_mirror_passes() -> None:
    """Long at entry $210 (below p_liq=$337) → Infinity → any L OK."""
    sizer = _sizer(direction="long")
    sizer.validate_strategy_yaml(
        requested_leverage=Decimal(50),
        reference_entry=Decimal("210"),
    )  # no raise


def test_validate_long_mirror_raises_above_p_liq() -> None:
    """Long at entry $400 (above p_liq=$337) → finite L_max ≈ 5.48."""
    sizer = _sizer(direction="long")
    with pytest.raises(LeverageExceedsMcapCeilingError, match="direction=long"):
        sizer.validate_strategy_yaml(
            requested_leverage=Decimal(10),
            reference_entry=Decimal("400"),
        )


# ---------------------------------------------------------------------------
# 5. Dataclass-level validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs,match",
    [
        (
            {"target_mcap_liq_usd": Decimal(0)},
            "target_mcap_liq_usd",
        ),
        (
            {"target_mcap_liq_usd": Decimal(-1)},
            "target_mcap_liq_usd",
        ),
        (
            {"mmr": Decimal(-1)},
            "mmr",
        ),
        (
            {"mmr": Decimal(1)},
            "mmr",
        ),
        (
            {"shares_outstanding": Decimal(0)},
            "shares_outstanding",
        ),
    ],
)
def test_invalid_fields_rejected(
    kwargs: dict, match: str
) -> None:
    base = dict(
        target_mcap_liq_usd=_TARGET_4T,
        mmr=_MMR_BINANCE,
        shares_outstanding=_SHARES,
        direction="short",
    )
    base.update(kwargs)
    with pytest.raises(ValueError, match=match):
        McapAnchoredSizer(**base)


def test_invalid_direction_rejected() -> None:
    with pytest.raises(ValueError, match="direction"):
        McapAnchoredSizer(
            target_mcap_liq_usd=_TARGET_4T,
            mmr=_MMR_BINANCE,
            shares_outstanding=_SHARES,
            direction="flat",  # type: ignore[arg-type]
        )


def test_non_decimal_coerced() -> None:
    sizer = McapAnchoredSizer(
        target_mcap_liq_usd=4_000_000_000_000,  # type: ignore[arg-type]
        mmr="0.025",  # type: ignore[arg-type]
        shares_outstanding="11870000000",  # type: ignore[arg-type]
        direction="short",
    )
    assert isinstance(sizer.target_mcap_liq_usd, Decimal)
    assert isinstance(sizer.mmr, Decimal)
    assert isinstance(sizer.shares_outstanding, Decimal)


def test_avg_entry_must_be_positive() -> None:
    sizer = _sizer(direction="short")
    with pytest.raises(ValueError, match="avg_entry"):
        sizer.max_leverage_for_entry(Decimal(0))
    with pytest.raises(ValueError, match="avg_entry"):
        sizer.max_leverage_for_entry(Decimal(-1))


def test_requested_leverage_must_be_positive() -> None:
    sizer = _sizer(direction="short")
    with pytest.raises(ValueError, match="requested_leverage"):
        sizer.validate_strategy_yaml(
            requested_leverage=Decimal(0),
            reference_entry=Decimal("210"),
        )


# ---------------------------------------------------------------------------
# 6. p_liq property
# ---------------------------------------------------------------------------


def test_p_liq_matches_target_div_shares() -> None:
    sizer = _sizer(direction="short")
    assert sizer.p_liq == _TARGET_4T / _SHARES


def test_short_long_share_same_p_liq() -> None:
    s = _sizer(direction="short")
    l = _sizer(direction="long")
    assert s.p_liq == l.p_liq
