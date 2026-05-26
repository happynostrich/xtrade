"""Shared run-context observability (Phase 1 Task 7 / P7).

Centralizes the `logs/<run-id>/` layout used by every CLI entry:

  - `run.log`             — Nautilus's own log file (written by the
                            kernel when a `LoggingConfig.log_directory`
                            is plumbed through).
  - `summary.json`        — written by each runner (backtest/live/health)
                            with the headline result fields.
  - `config.snapshot.yaml` — a copy of the venues yaml that drove the run
                            (only env-var *names* live in this file, so
                            it's safe to commit alongside a run).

Public surface:

  - `RunContext`           — frozen dataclass yielded by `run_with_logging`.
  - `resolve_run_id`       — `<mode>-YYYYMMDDTHHMMSSZ` if no override.
  - `resolve_logs_root`    — defaults to `<repo>/logs`.
  - `snapshot_venues_config` — copy `venues_cfg.source_path` to dest.
  - `run_with_logging`     — context manager. CLI entry points wrap the
                              underlying runner in this and forward
                              `ctx.run_id` / `ctx.logs_root` (and, for
                              node-based runners, `ctx.log_dir`).

Exit-code policy is enforced by the CLI:
  0  business success
  1  business failure (covered by runner result.passed False)
  2  config / precondition error (`_exit_config_error` in `xtrade.cli`)
"""

from __future__ import annotations

import datetime as dt
import os
import shutil
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from xtrade.config import VenuesConfig


# Default logs root sits next to `src/` in the repo. The `parents[2]`
# heuristic only resolves correctly when running from a source checkout;
# when xtrade is installed into a venv (`.venv/lib/pythonX.Y/site-packages/
# xtrade/observability.py`), `parents[2]` lands inside the venv tree,
# which is read-only under `ProtectSystem=strict`. In that case the
# operator MUST set `XTRADE_LOGS_ROOT` (or pass `--logs-root` /
# `logs_root=` to the call site) to a writable path under
# `ReadWritePaths=` — see `resolve_logs_root` below.
REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_LOGS_ROOT = REPO_ROOT / "logs"

# Env-var name honored by `resolve_logs_root` when no explicit value is
# supplied. Systemd units source `/etc/xtrade/env` which sets this to
# `/var/lib/xtrade/logs` on VPS installs.
LOGS_ROOT_ENV_VAR = "XTRADE_LOGS_ROOT"


@dataclass(frozen=True)
class RunContext:
    """Frozen snapshot of the run-time identity yielded by `run_with_logging`.

    `log_dir` is created on entry; `snapshot_path` is set iff a venues
    yaml was copied in.
    """

    run_id: str
    log_dir: Path
    logs_root: Path
    mode: str
    started_at: dt.datetime
    snapshot_path: Path | None = None


# ---------------------------------------------------------------------------
# Pure resolvers — kept side-effect-free for ease of testing
# ---------------------------------------------------------------------------


def resolve_run_id(supplied: str | None, *, mode: str) -> str:
    """Return `supplied` if given, else `<mode>-YYYYMMDDTHHMMSSZ` (UTC)."""
    if supplied:
        return supplied
    stamp = dt.datetime.now(tz=dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{mode}-{stamp}"


def resolve_logs_root(supplied: Path | str | None) -> Path:
    """Return the first non-empty source in this priority order:

    1. ``supplied`` (explicit caller argument; e.g. CLI ``--logs-root``).
    2. ``XTRADE_LOGS_ROOT`` env var (set by the systemd EnvironmentFile
       on VPS installs so installed wheels don't fall back to a
       venv-internal path).
    3. ``DEFAULT_LOGS_ROOT`` — only safe in source-checkout dev.
    """
    if supplied is not None:
        return Path(supplied)
    env_root = os.environ.get(LOGS_ROOT_ENV_VAR)
    if env_root:
        return Path(env_root)
    return DEFAULT_LOGS_ROOT


# ---------------------------------------------------------------------------
# Config snapshot
# ---------------------------------------------------------------------------


def snapshot_venues_config(
    venues_cfg: VenuesConfig | None,
    dest: Path,
) -> Path | None:
    """Copy `venues_cfg.source_path` to `dest` (verbatim).

    The venues yaml convention (see `xtrade.config`) is that it only
    references env-var *names*, never literal secrets. So a verbatim copy
    is safe to commit alongside a run for reproducibility.

    Returns the destination path on success, or `None` if no source was
    recorded (e.g. config built programmatically in tests) or the source
    no longer exists on disk.
    """
    if venues_cfg is None or venues_cfg.source_path is None:
        return None
    src = Path(venues_cfg.source_path)
    if not src.exists():
        return None
    shutil.copyfile(src, dest)
    return dest


# ---------------------------------------------------------------------------
# Context manager
# ---------------------------------------------------------------------------


@contextmanager
def run_with_logging(
    *,
    mode: str,
    run_id: str | None = None,
    logs_root: Path | str | None = None,
    venues_cfg: VenuesConfig | None = None,
    write_snapshot: bool = True,
) -> Iterator[RunContext]:
    """Yield a `RunContext` with `log_dir` created and (optionally) a
    `config.snapshot.yaml` written inside it.

    Parameters
    ----------
    mode : str
        One of `"backtest" | "live" | "health" | "ingest" | "inspect"`. Used
        as the prefix when auto-generating a run id.
    run_id : str | None
        Override the auto-generated id (else `<mode>-YYYYMMDDTHHMMSSZ`).
    logs_root : Path | str | None
        Override the logs root (else `<repo>/logs`).
    venues_cfg : VenuesConfig | None
        If provided and `write_snapshot=True`, copy `venues_cfg.source_path`
        to `<log_dir>/config.snapshot.yaml`.
    write_snapshot : bool
        Toggle the yaml snapshot; tests pass `False` to keep tmp dirs lean.

    The context manager intentionally does no per-exit work: each runner is
    responsible for writing its own `summary.json` (and Nautilus writes
    `run.log` when the runner plumbs `log_directory` into `LoggingConfig`).
    """
    rid = resolve_run_id(run_id, mode=mode)
    root = resolve_logs_root(logs_root)
    log_dir = root / rid
    log_dir.mkdir(parents=True, exist_ok=True)
    snapshot_path: Path | None = None
    if write_snapshot:
        snapshot_path = snapshot_venues_config(
            venues_cfg, log_dir / "config.snapshot.yaml"
        )
    ctx = RunContext(
        run_id=rid,
        log_dir=log_dir,
        logs_root=root,
        mode=mode,
        started_at=dt.datetime.now(tz=dt.timezone.utc),
        snapshot_path=snapshot_path,
    )
    yield ctx
