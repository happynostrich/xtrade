"""Offline tests for `xtrade.observability` (Phase 1 Task 7 / P7).

What we cover here:

  - `resolve_run_id` returns the supplied id verbatim and falls back to
    `<mode>-YYYYMMDDTHHMMSSZ` (UTC) when none is supplied.
  - `resolve_logs_root` defaults to `<repo>/logs` and honors overrides.
  - `snapshot_venues_config` copies `venues_cfg.source_path` verbatim and
    no-ops cleanly when `source_path is None` or the file is missing.
  - `run_with_logging` creates `logs/<run_id>/`, writes
    `config.snapshot.yaml` when a `venues_cfg` is provided, and skips the
    snapshot when `write_snapshot=False`.
"""

from __future__ import annotations

import re

from xtrade.config import (
    BinanceSpotConfig,
    BinanceVenueConfig,
    VenuesConfig,
)
from xtrade.observability import (
    DEFAULT_LOGS_ROOT,
    LOGS_ROOT_ENV_VAR,
    RunContext,
    resolve_logs_root,
    resolve_run_id,
    run_with_logging,
    snapshot_venues_config,
)


# ---------------------------------------------------------------------------
# resolve_run_id
# ---------------------------------------------------------------------------


def test_resolve_run_id_uses_supplied_value() -> None:
    assert resolve_run_id("custom-id", mode="backtest") == "custom-id"


def test_resolve_run_id_falls_back_to_timestamped_default() -> None:
    rid = resolve_run_id(None, mode="health")
    # Format: health-YYYYMMDDTHHMMSSZ
    assert re.fullmatch(r"health-\d{8}T\d{6}Z", rid), rid


def test_resolve_run_id_uses_mode_prefix() -> None:
    rid = resolve_run_id(None, mode="live")
    assert rid.startswith("live-")


# ---------------------------------------------------------------------------
# resolve_logs_root
# ---------------------------------------------------------------------------


def test_resolve_logs_root_default(monkeypatch) -> None:
    monkeypatch.delenv(LOGS_ROOT_ENV_VAR, raising=False)
    assert resolve_logs_root(None) == DEFAULT_LOGS_ROOT


def test_resolve_logs_root_override_path(tmp_path) -> None:
    assert resolve_logs_root(tmp_path) == tmp_path


def test_resolve_logs_root_override_string(tmp_path) -> None:
    assert resolve_logs_root(str(tmp_path)) == tmp_path


def test_resolve_logs_root_env_override(tmp_path, monkeypatch) -> None:
    # When no explicit argument is supplied, the env var takes precedence
    # over the repo-relative default. This is what saves the systemd
    # scanner unit from writing into `<venv>/lib/python3.12/logs` when
    # xtrade is installed (not run from a source checkout).
    monkeypatch.setenv(LOGS_ROOT_ENV_VAR, str(tmp_path))
    assert resolve_logs_root(None) == tmp_path


def test_resolve_logs_root_supplied_beats_env(tmp_path, monkeypatch) -> None:
    # Explicit argument always wins over the env var so `--logs-root`
    # on a CLI command is authoritative.
    monkeypatch.setenv(LOGS_ROOT_ENV_VAR, str(tmp_path / "envdir"))
    explicit = tmp_path / "argdir"
    assert resolve_logs_root(explicit) == explicit


def test_resolve_logs_root_empty_env_falls_back(monkeypatch) -> None:
    # An empty string in the env var must be treated as unset (a common
    # systemd EnvironmentFile artefact when a placeholder line like
    # `XTRADE_LOGS_ROOT=` is left in /etc/xtrade/env).
    monkeypatch.setenv(LOGS_ROOT_ENV_VAR, "")
    assert resolve_logs_root(None) == DEFAULT_LOGS_ROOT


# ---------------------------------------------------------------------------
# snapshot_venues_config
# ---------------------------------------------------------------------------


def _venues_with_source(source_path) -> VenuesConfig:
    """Build a minimal VenuesConfig whose `source_path` points at `source_path`."""
    spot = BinanceSpotConfig(
        api_key="dummy",
        api_secret="dummy",
        key_type="HMAC",
        account_type="SPOT",
        environment="TESTNET",
    )
    return VenuesConfig(
        binance=BinanceVenueConfig(spot=spot),
        source_path=source_path,
    )


def test_snapshot_copies_source_yaml(tmp_path) -> None:
    src = tmp_path / "venues.yaml"
    src.write_text("binance:\n  environment: TESTNET\n")
    dest = tmp_path / "config.snapshot.yaml"
    cfg = _venues_with_source(src)

    result = snapshot_venues_config(cfg, dest)

    assert result == dest
    assert dest.read_text() == src.read_text()


def test_snapshot_returns_none_when_source_path_unset(tmp_path) -> None:
    cfg = _venues_with_source(None)
    dest = tmp_path / "config.snapshot.yaml"

    assert snapshot_venues_config(cfg, dest) is None
    assert not dest.exists()


def test_snapshot_returns_none_when_source_file_missing(tmp_path) -> None:
    cfg = _venues_with_source(tmp_path / "does-not-exist.yaml")
    dest = tmp_path / "config.snapshot.yaml"

    assert snapshot_venues_config(cfg, dest) is None
    assert not dest.exists()


def test_snapshot_returns_none_when_venues_cfg_is_none(tmp_path) -> None:
    dest = tmp_path / "config.snapshot.yaml"
    assert snapshot_venues_config(None, dest) is None
    assert not dest.exists()


# ---------------------------------------------------------------------------
# run_with_logging
# ---------------------------------------------------------------------------


def test_run_with_logging_creates_log_dir(tmp_path) -> None:
    with run_with_logging(mode="backtest", logs_root=tmp_path) as ctx:
        assert isinstance(ctx, RunContext)
        assert ctx.mode == "backtest"
        assert ctx.logs_root == tmp_path
        assert ctx.log_dir == tmp_path / ctx.run_id
        assert ctx.log_dir.is_dir()
        assert ctx.snapshot_path is None


def test_run_with_logging_honors_supplied_run_id(tmp_path) -> None:
    with run_with_logging(
        mode="backtest", run_id="my-run", logs_root=tmp_path
    ) as ctx:
        assert ctx.run_id == "my-run"
        assert ctx.log_dir == tmp_path / "my-run"
        assert ctx.log_dir.is_dir()


def test_run_with_logging_writes_snapshot(tmp_path) -> None:
    src = tmp_path / "venues.yaml"
    src.write_text("binance:\n  environment: TESTNET\n")
    cfg = _venues_with_source(src)

    with run_with_logging(
        mode="live",
        run_id="snap-run",
        logs_root=tmp_path / "logs",
        venues_cfg=cfg,
    ) as ctx:
        assert ctx.snapshot_path == ctx.log_dir / "config.snapshot.yaml"
        assert ctx.snapshot_path.exists()
        assert ctx.snapshot_path.read_text() == src.read_text()


def test_run_with_logging_skip_snapshot(tmp_path) -> None:
    src = tmp_path / "venues.yaml"
    src.write_text("binance:\n  environment: TESTNET\n")
    cfg = _venues_with_source(src)

    with run_with_logging(
        mode="live",
        run_id="no-snap",
        logs_root=tmp_path / "logs",
        venues_cfg=cfg,
        write_snapshot=False,
    ) as ctx:
        assert ctx.snapshot_path is None
        assert not (ctx.log_dir / "config.snapshot.yaml").exists()


def test_run_with_logging_idempotent_dir(tmp_path) -> None:
    # Running twice with the same run_id should not error (mkdir exist_ok).
    with run_with_logging(mode="backtest", run_id="dup", logs_root=tmp_path):
        pass
    with run_with_logging(mode="backtest", run_id="dup", logs_root=tmp_path) as ctx:
        assert ctx.log_dir == tmp_path / "dup"
        assert ctx.log_dir.is_dir()
