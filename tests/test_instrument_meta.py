"""Tests for `xtrade.instruments.meta` (Phase 6 Task T3).

Brief §5 T3 names four contract surfaces:
  1. yaml parse                       → `MetaRegistry.load` / `get`
  2. `mcap_from_price` round-trip     ↔ `price_from_mcap`
  3. missing-field raises             → `InstrumentMetaError`
  4. `qty_step` round-down helper     → `quantize_qty`

We additionally cover:
  - Decimal coercion (yaml ints / floats / quoted-strings all land
    as `Decimal`).
  - Lookup-miss raises `InstrumentNotFoundError`.
  - Non-positive numeric fields rejected at dataclass construction.
"""

from __future__ import annotations

import textwrap
from decimal import Decimal
from pathlib import Path

import pytest

from xtrade.instruments.meta import (
    InstrumentMeta,
    InstrumentMetaError,
    InstrumentNotFoundError,
    MetaRegistry,
    load_instrument_meta,
    mcap_from_price,
    price_from_mcap,
    quantize_qty,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


_SPCX_YAML = textwrap.dedent(
    """
    SPCXUSDT-PERP.BINANCE:
      shares_outstanding: 11_870_000_000
      min_qty: '0.001'
      qty_step: '0.001'
      tick_size: '0.01'
      mark_source: oracle
    """
).strip()


def _write_yaml(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "instrument_meta.yaml"
    path.write_text(body, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# 1. yaml parse + registry lookup
# ---------------------------------------------------------------------------


def test_load_parses_spcx_entry(tmp_path: Path) -> None:
    registry = load_instrument_meta(_write_yaml(tmp_path, _SPCX_YAML))
    assert "SPCXUSDT-PERP.BINANCE" in registry
    assert len(registry) == 1
    assert registry.symbols() == ("SPCXUSDT-PERP.BINANCE",)

    meta = registry.get("SPCXUSDT-PERP.BINANCE")
    assert isinstance(meta, InstrumentMeta)
    assert meta.symbol == "SPCXUSDT-PERP.BINANCE"
    assert meta.shares_outstanding == Decimal("11870000000")
    assert isinstance(meta.shares_outstanding, Decimal)
    assert meta.min_qty == Decimal("0.001")
    assert meta.qty_step == Decimal("0.001")
    assert meta.tick_size == Decimal("0.01")
    assert meta.mark_source == "oracle"


def test_classmethod_load_matches_function(tmp_path: Path) -> None:
    path = _write_yaml(tmp_path, _SPCX_YAML)
    via_class = MetaRegistry.load(path)
    via_func = load_instrument_meta(path)
    assert via_class.symbols() == via_func.symbols()


def test_load_accepts_empty_yaml(tmp_path: Path) -> None:
    path = _write_yaml(tmp_path, "")
    registry = load_instrument_meta(path)
    assert len(registry) == 0
    assert registry.symbols() == ()


def test_load_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_instrument_meta(tmp_path / "nope.yaml")


# ---------------------------------------------------------------------------
# 2. mcap_from_price / price_from_mcap round-trip
# ---------------------------------------------------------------------------


@pytest.fixture
def spcx_meta(tmp_path: Path) -> InstrumentMeta:
    return load_instrument_meta(_write_yaml(tmp_path, _SPCX_YAML)).get(
        "SPCXUSDT-PERP.BINANCE"
    )


@pytest.mark.parametrize(
    "price,expected_mcap",
    [
        (Decimal("225"), Decimal("225") * Decimal("11870000000")),
        (Decimal("294.86"), Decimal("294.86") * Decimal("11870000000")),
        (Decimal("0.01"), Decimal("0.01") * Decimal("11870000000")),
    ],
)
def test_mcap_from_price_exact(
    spcx_meta: InstrumentMeta, price: Decimal, expected_mcap: Decimal
) -> None:
    assert mcap_from_price(price, spcx_meta) == expected_mcap


@pytest.mark.parametrize(
    "price",
    [
        Decimal("210"),
        Decimal("225"),
        Decimal("294.86"),
        Decimal("336.984"),  # ≈ $4T / 11.87B
    ],
)
def test_price_mcap_round_trip(
    spcx_meta: InstrumentMeta, price: Decimal
) -> None:
    mcap = mcap_from_price(price, spcx_meta)
    assert price_from_mcap(mcap, spcx_meta) == price


def test_helpers_accept_non_decimal_inputs(spcx_meta: InstrumentMeta) -> None:
    """Helpers coerce ints / strings to Decimal so callers don't have
    to wrap every literal."""
    assert mcap_from_price("225", spcx_meta) == Decimal("225") * Decimal(
        "11870000000"
    )
    assert price_from_mcap("4000000000000", spcx_meta) == Decimal(
        "4000000000000"
    ) / Decimal("11870000000")


# ---------------------------------------------------------------------------
# 3. Error surfaces
# ---------------------------------------------------------------------------


def test_missing_required_field_raises(tmp_path: Path) -> None:
    body = textwrap.dedent(
        """
        SPCXUSDT-PERP.BINANCE:
          shares_outstanding: 11_870_000_000
          min_qty: '0.001'
          qty_step: '0.001'
          # tick_size deliberately omitted
          mark_source: oracle
        """
    ).strip()
    with pytest.raises(InstrumentMetaError, match="tick_size"):
        load_instrument_meta(_write_yaml(tmp_path, body))


def test_non_mapping_entry_raises(tmp_path: Path) -> None:
    body = textwrap.dedent(
        """
        SPCXUSDT-PERP.BINANCE: 'not-a-mapping'
        """
    ).strip()
    with pytest.raises(InstrumentMetaError, match="must be a mapping"):
        load_instrument_meta(_write_yaml(tmp_path, body))


def test_non_mapping_root_raises(tmp_path: Path) -> None:
    body = "- just-a-list\n- of-strings"
    with pytest.raises(InstrumentMetaError, match="root must be a mapping"):
        load_instrument_meta(_write_yaml(tmp_path, body))


def test_unparseable_decimal_raises(tmp_path: Path) -> None:
    body = textwrap.dedent(
        """
        SPCXUSDT-PERP.BINANCE:
          shares_outstanding: 'not-a-number'
          min_qty: '0.001'
          qty_step: '0.001'
          tick_size: '0.01'
          mark_source: oracle
        """
    ).strip()
    with pytest.raises(InstrumentMetaError, match="shares_outstanding"):
        load_instrument_meta(_write_yaml(tmp_path, body))


def test_get_unknown_symbol_raises(spcx_meta: InstrumentMeta) -> None:  # noqa: ARG001
    registry = MetaRegistry({"SPCXUSDT-PERP.BINANCE": spcx_meta})
    with pytest.raises(InstrumentNotFoundError, match="BTCUSDT"):
        registry.get("BTCUSDT-PERP.BINANCE")


def test_non_positive_shares_rejected() -> None:
    with pytest.raises(ValueError, match="shares_outstanding"):
        InstrumentMeta(
            symbol="X.Y",
            shares_outstanding=Decimal(0),
            min_qty=Decimal("0.001"),
            qty_step=Decimal("0.001"),
            tick_size=Decimal("0.01"),
            mark_source="oracle",
        )


def test_empty_symbol_rejected() -> None:
    with pytest.raises(ValueError, match="symbol"):
        InstrumentMeta(
            symbol="",
            shares_outstanding=Decimal("11870000000"),
            min_qty=Decimal("0.001"),
            qty_step=Decimal("0.001"),
            tick_size=Decimal("0.01"),
            mark_source="oracle",
        )


# ---------------------------------------------------------------------------
# 4. quantize_qty — qty_step round-down helper
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        (Decimal("0.0019"), Decimal("0.001")),   # rounds down
        (Decimal("0.0021"), Decimal("0.002")),
        (Decimal("0.001"), Decimal("0.001")),    # exact multiple
        (Decimal("0.0009"), Decimal("0.000")),   # below step → 0 (caller checks min_qty)
        (Decimal("1.2345"), Decimal("1.234")),
        (Decimal("0"), Decimal(0)),
        (Decimal("-0.5"), Decimal(0)),           # negative → 0; sizer is responsible for sign
    ],
)
def test_quantize_qty_rounds_down(
    spcx_meta: InstrumentMeta, raw: Decimal, expected: Decimal
) -> None:
    result = quantize_qty(raw, spcx_meta)
    assert result == expected


def test_quantize_qty_accepts_non_decimal(spcx_meta: InstrumentMeta) -> None:
    assert quantize_qty("0.0049", spcx_meta) == Decimal("0.004")
    assert quantize_qty(0.0049, spcx_meta) == Decimal("0.004")


def test_quantize_qty_custom_step() -> None:
    """Coarser qty_step — exercises the divide-floor-multiply path."""
    meta = InstrumentMeta(
        symbol="X.Y",
        shares_outstanding=Decimal("1000"),
        min_qty=Decimal("0.1"),
        qty_step=Decimal("0.5"),
        tick_size=Decimal("0.01"),
        mark_source="oracle",
    )
    assert quantize_qty(Decimal("1.7"), meta) == Decimal("1.5")
    assert quantize_qty(Decimal("2.0"), meta) == Decimal("2.0")
    assert quantize_qty(Decimal("0.4"), meta) == Decimal("0.0")


# ---------------------------------------------------------------------------
# 5. Repo-shipped yaml smoke test — config/instrument_meta.yaml is the
#    authoritative source for SPCXUSDT and must always parse.
# ---------------------------------------------------------------------------


def test_repo_instrument_meta_yaml_loads() -> None:
    repo_yaml = (
        Path(__file__).resolve().parents[1] / "config" / "instrument_meta.yaml"
    )
    registry = load_instrument_meta(repo_yaml)
    meta = registry.get("SPCXUSDT-PERP.BINANCE")
    assert meta.shares_outstanding == Decimal("11870000000")
    assert meta.mark_source == "oracle"
