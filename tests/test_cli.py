"""Offline CLI exit-code contract tests (Phase 1 Task 8 / P8 + P7).

Exercises the error branches of `xtrade.cli` that exit with code 2
(configuration / precondition failure) without spinning up a
`BacktestEngine` or `TradingNode`. The success path of the backtest is
covered separately by `test_backtest_smoke.py`; the live path is covered
by `test_live_runner.py` (mainnet refusal et al.).

We avoid invoking any subcommand that constructs a Nautilus kernel from
this file — the Rust logger is global per-process and would collide with
`test_backtest_smoke.py` if both ran here.
"""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from xtrade.cli import app


runner = CliRunner(mix_stderr=False)


# ---------------------------------------------------------------------------
# Top-level help
# ---------------------------------------------------------------------------


def test_top_level_help_lists_subcommand_groups() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    out = result.stdout
    # Phase 1 brief Task 1 acceptance: `xtrade --help` lists data/backtest/live.
    assert "data" in out
    assert "backtest" in out
    assert "live" in out


def test_data_help_runs() -> None:
    result = runner.invoke(app, ["data", "--help"])
    assert result.exit_code == 0
    assert "ingest" in result.stdout
    assert "inspect" in result.stdout


def test_backtest_help_runs() -> None:
    result = runner.invoke(app, ["backtest", "--help"])
    assert result.exit_code == 0
    assert "run" in result.stdout


def test_live_help_runs() -> None:
    result = runner.invoke(app, ["live", "--help"])
    assert result.exit_code == 0
    assert "health" in result.stdout
    assert "run" in result.stdout


# ---------------------------------------------------------------------------
# `xtrade data ingest` — config / precondition errors (exit code 2)
# ---------------------------------------------------------------------------


def test_data_ingest_rejects_unknown_venue() -> None:
    result = runner.invoke(
        app,
        [
            "data", "ingest",
            "--venue", "kraken",
            "--symbol", "BTCUSDT",
            "--bar", "1m",
            "--since", "2026-01-01",
            "--until", "2026-01-02",
        ],
    )
    assert result.exit_code == 2
    assert "binance" in result.stderr.lower() or "hyperliquid" in result.stderr.lower()


def test_data_ingest_rejects_bad_bar_spec() -> None:
    result = runner.invoke(
        app,
        [
            "data", "ingest",
            "--venue", "binance",
            "--symbol", "BTCUSDT",
            "--bar", "garbage",
            "--since", "2026-01-01",
            "--until", "2026-01-02",
        ],
    )
    assert result.exit_code == 2


def test_data_ingest_rejects_inverted_window() -> None:
    result = runner.invoke(
        app,
        [
            "data", "ingest",
            "--venue", "binance",
            "--symbol", "BTCUSDT",
            "--bar", "1m",
            "--since", "2026-01-02",
            "--until", "2026-01-01",
        ],
    )
    assert result.exit_code == 2
    assert "after" in result.stderr.lower() or "until" in result.stderr.lower()


# ---------------------------------------------------------------------------
# `xtrade backtest run` — config errors only (no kernel construction)
# ---------------------------------------------------------------------------


def test_backtest_run_rejects_unknown_strategy() -> None:
    result = runner.invoke(
        app,
        [
            "backtest", "run",
            "--strategy", "does_not_exist",
            "--instrument", "BTCUSDT-PERP.BINANCE",
            "--bar", "1m",
        ],
    )
    assert result.exit_code == 2
    assert "strategy" in result.stderr.lower()


def test_backtest_run_rejects_bad_trade_size() -> None:
    result = runner.invoke(
        app,
        [
            "backtest", "run",
            "--strategy", "demo_ema",
            "--instrument", "BTCUSDT-PERP.BINANCE",
            "--bar", "1m",
            "--trade-size", "not-a-decimal",
        ],
    )
    assert result.exit_code == 2
    assert "trade-size" in result.stderr.lower() or "decimal" in result.stderr.lower()


def test_backtest_run_rejects_fast_ge_slow_ema() -> None:
    result = runner.invoke(
        app,
        [
            "backtest", "run",
            "--strategy", "demo_ema",
            "--instrument", "BTCUSDT-PERP.BINANCE",
            "--bar", "1m",
            "--fast-ema", "20",
            "--slow-ema", "10",
        ],
    )
    assert result.exit_code == 2
    assert "fast" in result.stderr.lower() and "slow" in result.stderr.lower()


def test_backtest_run_rejects_inverted_window() -> None:
    result = runner.invoke(
        app,
        [
            "backtest", "run",
            "--strategy", "demo_ema",
            "--instrument", "BTCUSDT-PERP.BINANCE",
            "--bar", "1m",
            "--since", "2026-02-01",
            "--until", "2026-01-01",
        ],
    )
    assert result.exit_code == 2


# ---------------------------------------------------------------------------
# `xtrade live health` — config errors (no kernel construction)
# ---------------------------------------------------------------------------


def test_live_health_rejects_unknown_venue_key(tmp_path: Path) -> None:
    # No yaml needed: venue-key validation happens before yaml load.
    result = runner.invoke(
        app,
        [
            "live", "health",
            "--venues", "ftx",
            "--timeout", "5",
            "--venues-yaml", str(tmp_path / "nope.yaml"),
        ],
    )
    assert result.exit_code == 2
    assert "ftx" in result.stderr.lower() or "venue" in result.stderr.lower()


def test_live_health_rejects_missing_yaml(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "live", "health",
            "--venues", "binance_spot",
            "--timeout", "5",
            "--venues-yaml", str(tmp_path / "does-not-exist.yaml"),
        ],
    )
    assert result.exit_code == 2
    assert "does not exist" in result.stderr.lower() or "config" in result.stderr.lower()


def test_live_health_rejects_empty_venues_list() -> None:
    result = runner.invoke(
        app,
        [
            "live", "health",
            "--venues", "",
            "--timeout", "5",
        ],
    )
    assert result.exit_code == 2


# ---------------------------------------------------------------------------
# `xtrade live health` — venue inference from --instrument
# ---------------------------------------------------------------------------


def test_venue_for_instrument_unit_mapping() -> None:
    """Per-instrument-id venue inference is the table the runbook documents."""
    from xtrade.cli import _venue_for_instrument

    assert _venue_for_instrument("BTCUSDT.BINANCE") == "binance_spot"
    assert _venue_for_instrument("ETHUSDT.BINANCE") == "binance_spot"
    assert _venue_for_instrument("BTCUSDT-PERP.BINANCE") == "binance_futures"
    assert _venue_for_instrument("ETHUSDT-PERP.BINANCE") == "binance_futures"
    assert _venue_for_instrument("BTC-USD-PERP.HYPERLIQUID") == "hyperliquid"


# ---------------------------------------------------------------------------
# `_narrow_venues_cfg` — filter a loaded VenuesConfig to a venue-key subset
# ---------------------------------------------------------------------------


def _full_venues_cfg():
    """In-memory VenuesConfig with all three subaccounts populated.

    Mirrors `config/venues.testnet.yaml` shape but with dummy creds, so we
    can exercise narrowing without any disk I/O or env-var resolution.
    """
    from xtrade.config import (
        BinanceFuturesConfig,
        BinanceSpotConfig,
        BinanceVenueConfig,
        HyperliquidVenueConfig,
        VenuesConfig,
    )

    return VenuesConfig(
        binance=BinanceVenueConfig(
            spot=BinanceSpotConfig(
                api_key="k", api_secret="s",
                key_type="HMAC", account_type="SPOT",
                environment="TESTNET",
            ),
            futures=BinanceFuturesConfig(
                api_key="k", api_secret="s",
                key_type="HMAC", account_type="USDT_FUTURE",
                environment="TESTNET",
            ),
        ),
        hyperliquid=HyperliquidVenueConfig(
            account_address="0x" + "0" * 40,
            api_wallet_key="0x" + "1" * 64,
            environment="TESTNET",
        ),
    )


def test_narrow_venues_cfg_keeps_only_binance_futures() -> None:
    """The original bug repro: full yaml + inferred ['binance_futures']
    must yield a VenuesConfig with binance.spot=None so the factory
    guard doesn't fire."""
    from xtrade.cli import _narrow_venues_cfg

    cfg = _full_venues_cfg()
    narrowed = _narrow_venues_cfg(cfg, ["binance_futures"])
    assert narrowed.binance is not None
    assert narrowed.binance.spot is None
    assert narrowed.binance.futures is not None
    assert narrowed.hyperliquid is None


def test_narrow_venues_cfg_keeps_only_binance_spot() -> None:
    from xtrade.cli import _narrow_venues_cfg

    cfg = _full_venues_cfg()
    narrowed = _narrow_venues_cfg(cfg, ["binance_spot"])
    assert narrowed.binance is not None
    assert narrowed.binance.spot is not None
    assert narrowed.binance.futures is None
    assert narrowed.hyperliquid is None


def test_narrow_venues_cfg_drops_binance_entirely_for_hyperliquid_only() -> None:
    from xtrade.cli import _narrow_venues_cfg

    cfg = _full_venues_cfg()
    narrowed = _narrow_venues_cfg(cfg, ["hyperliquid"])
    assert narrowed.binance is None
    assert narrowed.hyperliquid is not None


def test_resolve_per_venue_yaml_returns_sibling_when_present(tmp_path: Path) -> None:
    """The convention `venues.<venue_key>.testnet.yaml` next to the
    given base path is what the CLI uses to auto-discover per-venue
    files in chained `xtrade live health` runs."""
    from xtrade.cli import _resolve_per_venue_yaml

    base = tmp_path / "venues.testnet.yaml"
    base.write_text("# pointer\n")
    sibling = tmp_path / "venues.binance_futures.testnet.yaml"
    sibling.write_text("binance:\n  environment: TESTNET\n")

    resolved = _resolve_per_venue_yaml("binance_futures", base)
    assert resolved == sibling


def test_resolve_per_venue_yaml_returns_none_when_missing(tmp_path: Path) -> None:
    from xtrade.cli import _resolve_per_venue_yaml

    base = tmp_path / "venues.testnet.yaml"
    base.write_text("# pointer\n")
    # No sibling created — the CLI's caller will fall back to the
    # explicit `--venues-yaml` path in this case.
    assert _resolve_per_venue_yaml("hyperliquid", base) is None


def test_auto_resolve_default_passes_through_non_default(tmp_path: Path) -> None:
    """Explicit --venues-yaml is never rewritten — only the gutted
    `config/venues.testnet.yaml` default triggers the auto-resolve."""
    from xtrade.cli import _auto_resolve_default_venues_yaml

    explicit = tmp_path / "venues.custom.yaml"
    explicit.write_text("# user-supplied\n")
    assert (
        _auto_resolve_default_venues_yaml(explicit, "BTCUSDT-PERP.BINANCE")
        == explicit
    )


def test_auto_resolve_default_resolves_sibling_for_default(
    tmp_path: Path, monkeypatch
) -> None:
    """When the operator leaves --venues-yaml at the gutted default,
    the CLI swaps in the per-venue sibling matching --instrument."""
    from xtrade.cli import _DEFAULT_VENUES_YAML, _auto_resolve_default_venues_yaml

    # `_DEFAULT_VENUES_YAML` is a relative path; resolve siblings via
    # the working directory so the helper finds the per-venue file.
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "venues.testnet.yaml").write_text("# pointer\n")
    sibling = tmp_path / "config" / "venues.binance_futures.testnet.yaml"
    sibling.write_text("binance:\n  environment: TESTNET\n")

    resolved = _auto_resolve_default_venues_yaml(
        _DEFAULT_VENUES_YAML, "BTCUSDT-PERP.BINANCE"
    )
    # `Path.exists()` resolution makes the helper return the same
    # relative form it received its base from, so compare via resolve().
    assert resolved.resolve() == sibling.resolve()


def test_auto_resolve_default_falls_back_when_sibling_missing(
    tmp_path: Path, monkeypatch
) -> None:
    """If the per-venue sibling does not exist, return the default
    unchanged so `load_venues` raises its normal ConfigError."""
    from xtrade.cli import _DEFAULT_VENUES_YAML, _auto_resolve_default_venues_yaml

    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "venues.testnet.yaml").write_text("# pointer\n")
    # No `venues.hyperliquid.testnet.yaml` created.

    resolved = _auto_resolve_default_venues_yaml(
        _DEFAULT_VENUES_YAML, "BTC-USD-PERP.HYPERLIQUID"
    )
    assert resolved == _DEFAULT_VENUES_YAML


def test_auto_resolve_default_passes_through_unknown_instrument(
    tmp_path: Path, monkeypatch
) -> None:
    """An instrument id whose venue can't be inferred (no `.BINANCE`
    or `.HYPERLIQUID` suffix) does not rewrite the default — the
    caller's `load_venues` will surface the original ConfigError."""
    from xtrade.cli import _DEFAULT_VENUES_YAML, _auto_resolve_default_venues_yaml

    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir()

    resolved = _auto_resolve_default_venues_yaml(
        _DEFAULT_VENUES_YAML, "FOO.BAR-UNKNOWN"
    )
    assert resolved == _DEFAULT_VENUES_YAML


def test_live_health_rejects_instrument_outside_requested_venues(tmp_path: Path) -> None:
    """If the operator passes --venues binance_futures but --instrument
    BTCUSDT.BINANCE (a spot id), we fail loudly instead of silently
    dropping the instrument."""
    result = runner.invoke(
        app,
        [
            "live", "health",
            "--venues", "binance_futures",
            "--instrument", "BTCUSDT.BINANCE",
            "--timeout", "5",
            "--venues-yaml", str(tmp_path / "nope.yaml"),
        ],
    )
    assert result.exit_code == 2
    assert "binance_spot" in result.stderr
    assert "binance_futures" in result.stderr


def test_narrow_venues_cfg_rejects_missing_venue() -> None:
    """If the operator asks for a key not in the yaml we exit 2 with a
    clear message instead of silently dropping it."""
    import typer

    from xtrade.cli import _narrow_venues_cfg
    from xtrade.config import HyperliquidVenueConfig, VenuesConfig

    # Yaml only has hyperliquid; operator asks for binance_futures.
    cfg = VenuesConfig(
        hyperliquid=HyperliquidVenueConfig(
            account_address="0x" + "0" * 40,
            api_wallet_key="0x" + "1" * 64,
            environment="TESTNET",
        ),
    )
    try:
        _narrow_venues_cfg(cfg, ["binance_futures"])
    except typer.Exit as exc:
        assert exc.exit_code == 2
    else:
        raise AssertionError("expected typer.Exit(2) for missing venue key")


def test_live_health_infers_futures_venue_from_perp_instrument(tmp_path: Path) -> None:
    """`--instrument BTCUSDT-PERP.BINANCE` + default --venues → binance_futures only.

    The CLI prints an explanatory `note:` to stderr so the inference is
    visible to the operator. We don't reach the node build path (yaml
    intentionally missing), but the inference must happen before yaml
    load so the note appears regardless.
    """
    result = runner.invoke(
        app,
        [
            "live", "health",
            "--instrument", "BTCUSDT-PERP.BINANCE",
            "--timeout", "5",
            "--venues-yaml", str(tmp_path / "does-not-exist.yaml"),
        ],
    )
    # Exit 2 because the yaml doesn't exist — that's fine; what matters
    # is the inference note fired before the yaml-load error.
    assert result.exit_code == 2
    assert "binance_futures" in result.stderr
    assert "binance_spot" not in result.stderr
    assert "hyperliquid" not in result.stderr


def test_live_health_infers_spot_venue_from_spot_instrument(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "live", "health",
            "--instrument", "BTCUSDT.BINANCE",
            "--timeout", "5",
            "--venues-yaml", str(tmp_path / "does-not-exist.yaml"),
        ],
    )
    assert result.exit_code == 2
    assert "binance_spot" in result.stderr
    assert "binance_futures" not in result.stderr


def test_live_health_does_not_infer_when_venues_explicit(tmp_path: Path) -> None:
    """Explicit `--venues` suppresses inference — operator wins."""
    result = runner.invoke(
        app,
        [
            "live", "health",
            "--venues", "binance_spot",
            "--instrument", "BTCUSDT-PERP.BINANCE",
            "--timeout", "5",
            "--venues-yaml", str(tmp_path / "does-not-exist.yaml"),
        ],
    )
    assert result.exit_code == 2
    # No "inferred ..." note — operator's --venues should pass through.
    assert "inferred" not in result.stderr


# ---------------------------------------------------------------------------
# `xtrade live run` — config errors (no kernel construction)
# ---------------------------------------------------------------------------


def test_live_run_rejects_unknown_strategy(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "live", "run",
            "--strategy", "does_not_exist",
            "--instrument", "BTCUSDT.BINANCE",
            "--venues-yaml", str(tmp_path / "nope.yaml"),
        ],
    )
    assert result.exit_code == 2
    assert "strategy" in result.stderr.lower()


def test_live_run_rejects_bad_side(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "live", "run",
            "--instrument", "BTCUSDT.BINANCE",
            "--side", "FLOAT",
            "--venues-yaml", str(tmp_path / "nope.yaml"),
        ],
    )
    assert result.exit_code == 2
    assert "side" in result.stderr.lower()


def test_live_run_rejects_bad_quantity(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "live", "run",
            "--instrument", "BTCUSDT.BINANCE",
            "--quantity", "not-a-decimal",
            "--venues-yaml", str(tmp_path / "nope.yaml"),
        ],
    )
    assert result.exit_code == 2


def test_live_run_rejects_non_positive_safety_multiplier(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "live", "run",
            "--instrument", "BTCUSDT.BINANCE",
            "--safety-multiplier", "-0.5",
            "--venues-yaml", str(tmp_path / "nope.yaml"),
        ],
    )
    assert result.exit_code == 2
    assert "safety" in result.stderr.lower() or "multiplier" in result.stderr.lower()


def test_live_run_rejects_missing_yaml(tmp_path: Path) -> None:
    # Strategy, side, qty all valid: failure must come from yaml resolution.
    result = runner.invoke(
        app,
        [
            "live", "run",
            "--instrument", "BTCUSDT.BINANCE",
            "--venues-yaml", str(tmp_path / "does-not-exist.yaml"),
        ],
    )
    assert result.exit_code == 2
