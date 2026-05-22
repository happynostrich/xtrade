"""Offline tests for `xtrade.research.universe` (Phase 2 Task 1 / S1).

These cover the yaml-to-`UniverseConfig` parser:

  - happy path with venue-defaulted quote currency
  - unknown venue key (mapped to exit code 2 by CLI)
  - duplicate symbols, malformed entries, bad scalar types
  - the bundled `config/universe.example.yaml` actually loads
"""

from __future__ import annotations

from pathlib import Path

import pytest

from xtrade.research.universe import (
    SymbolSpec,
    UniverseConfig,
    UniverseConfigError,
    load_universe,
)


REPO_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


def test_load_universe_minimal(tmp_path: Path) -> None:
    p = tmp_path / "u.yaml"
    p.write_text(
        "binance:\n"
        "  - symbol: BTCUSDT\n"
        "  - symbol: ETHUSDT\n"
    )
    uni = load_universe(p)
    assert isinstance(uni, UniverseConfig)
    assert len(uni) == 2
    assert uni.source_path == p
    assert uni.symbols[0] == SymbolSpec(
        venue="binance", symbol="BTCUSDT", quote="USDT", min_volume=None
    )
    assert uni.symbols[1].quote == "USDT"  # venue default applied


def test_load_universe_quote_override(tmp_path: Path) -> None:
    p = tmp_path / "u.yaml"
    p.write_text(
        "binance:\n"
        "  - symbol: BTCUSDT\n"
        "    quote: USDC\n"
        "    min_volume: 1000.5\n"
    )
    uni = load_universe(p)
    assert uni.symbols[0] == SymbolSpec(
        venue="binance", symbol="BTCUSDT", quote="USDC", min_volume=1000.5
    )


def test_load_universe_multi_venue(tmp_path: Path) -> None:
    p = tmp_path / "u.yaml"
    p.write_text(
        "binance:\n"
        "  - symbol: BTCUSDT\n"
        "hyperliquid:\n"
        "  - symbol: xyz:TSLA\n"
        "  - symbol: xyz:NVDA\n"
    )
    uni = load_universe(p)
    assert len(uni) == 3
    grouped = uni.by_venue()
    assert set(grouped) == {"binance", "hyperliquid"}
    assert grouped["hyperliquid"][0].quote == "USDC"  # venue default


def test_load_universe_bundled_example_yaml() -> None:
    """The repo's `config/universe.example.yaml` must parse cleanly so
    `xtrade scan universe` works out of the box."""
    p = REPO_ROOT / "config" / "universe.example.yaml"
    uni = load_universe(p)
    assert len(uni) >= 10  # the file lists 15 today; lower bound guards drift
    grouped = uni.by_venue()
    assert "binance" in grouped and "hyperliquid" in grouped


def test_universe_config_is_hashable() -> None:
    """`UniverseConfig.symbols` is a tuple of frozen dataclasses, so the
    overall config should be hashable / usable as a cache key."""
    cfg = UniverseConfig(
        symbols=(SymbolSpec(venue="binance", symbol="BTCUSDT", quote="USDT"),),
    )
    assert hash(cfg) is not None


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


def test_load_universe_missing_file(tmp_path: Path) -> None:
    with pytest.raises(UniverseConfigError, match="does not exist"):
        load_universe(tmp_path / "nope.yaml")


def test_load_universe_empty_doc(tmp_path: Path) -> None:
    p = tmp_path / "u.yaml"
    p.write_text("")
    with pytest.raises(UniverseConfigError, match="empty"):
        load_universe(p)


def test_load_universe_top_level_must_be_mapping(tmp_path: Path) -> None:
    p = tmp_path / "u.yaml"
    p.write_text("- just_a_list_at_top_level\n")
    with pytest.raises(UniverseConfigError, match="mapping at the top level"):
        load_universe(p)


def test_load_universe_unknown_venue(tmp_path: Path) -> None:
    p = tmp_path / "u.yaml"
    p.write_text(
        "kraken:\n"
        "  - symbol: BTCUSD\n"
    )
    with pytest.raises(UniverseConfigError, match="unknown venue"):
        load_universe(p)


def test_load_universe_duplicate_symbol(tmp_path: Path) -> None:
    p = tmp_path / "u.yaml"
    p.write_text(
        "binance:\n"
        "  - symbol: BTCUSDT\n"
        "  - symbol: BTCUSDT\n"
    )
    with pytest.raises(UniverseConfigError, match="duplicate symbol"):
        load_universe(p)


def test_load_universe_missing_symbol_field(tmp_path: Path) -> None:
    p = tmp_path / "u.yaml"
    p.write_text(
        "binance:\n"
        "  - quote: USDT\n"  # no `symbol`
    )
    with pytest.raises(UniverseConfigError, match="missing or non-string"):
        load_universe(p)


def test_load_universe_entry_must_be_mapping(tmp_path: Path) -> None:
    p = tmp_path / "u.yaml"
    p.write_text(
        "binance:\n"
        "  - BTCUSDT\n"  # scalar, not mapping
    )
    with pytest.raises(UniverseConfigError, match="must be a\\s+mapping"):
        load_universe(p)


def test_load_universe_min_volume_must_be_numeric(tmp_path: Path) -> None:
    p = tmp_path / "u.yaml"
    p.write_text(
        "binance:\n"
        "  - symbol: BTCUSDT\n"
        "    min_volume: 'a lot'\n"
    )
    with pytest.raises(UniverseConfigError, match="numeric"):
        load_universe(p)


def test_load_universe_min_volume_must_be_non_negative(tmp_path: Path) -> None:
    p = tmp_path / "u.yaml"
    p.write_text(
        "binance:\n"
        "  - symbol: BTCUSDT\n"
        "    min_volume: -1\n"
    )
    with pytest.raises(UniverseConfigError, match=">= 0"):
        load_universe(p)


def test_load_universe_all_venues_empty(tmp_path: Path) -> None:
    p = tmp_path / "u.yaml"
    p.write_text("binance:\nhyperliquid:\n")
    with pytest.raises(UniverseConfigError, match="empty after parsing"):
        load_universe(p)


def test_load_universe_venue_value_must_be_list(tmp_path: Path) -> None:
    p = tmp_path / "u.yaml"
    p.write_text(
        "binance:\n"
        "  symbol: BTCUSDT\n"  # dict instead of list of dicts
    )
    with pytest.raises(UniverseConfigError, match="must be a list"):
        load_universe(p)
