"""Offline tests for `xtrade.strategies.base` (Phase 1 Task 8 / P8 + P6).

These tests do NOT instantiate a `XtradeStrategy` subclass — Nautilus's
`Strategy.__init__` requires a kernel-issued component id machinery that
isn't safe to bootstrap from pytest without colliding with the global
Rust logger. We test the mode-dispatch *contract* by:

  1. Verifying `XtradeStrategyConfig` exposes a default `mode="backtest"`
     and accepts `mode="live"` (the toggle the runners flip).
  2. Confirming `XtradeStrategy.on_start` calls `on_start_common` and
     then routes to the mode-specific hook — verified by patching the
     three hook methods on the class and invoking `on_start` against a
     minimal stub `self` that only exposes `.config.mode`.
"""

from __future__ import annotations

from types import SimpleNamespace

from xtrade.strategies.base import XtradeStrategy, XtradeStrategyConfig


# ---------------------------------------------------------------------------
# XtradeStrategyConfig
# ---------------------------------------------------------------------------


def test_xtrade_strategy_config_default_mode_is_backtest() -> None:
    cfg = XtradeStrategyConfig()
    assert cfg.mode == "backtest"


def test_xtrade_strategy_config_accepts_live_mode() -> None:
    cfg = XtradeStrategyConfig(mode="live")
    assert cfg.mode == "live"


def test_xtrade_strategy_config_is_frozen() -> None:
    cfg = XtradeStrategyConfig(mode="backtest")
    # msgspec frozen Structs raise AttributeError on assignment.
    try:
        cfg.mode = "live"  # type: ignore[misc]
    except (AttributeError, TypeError):
        return
    raise AssertionError("XtradeStrategyConfig should be frozen")


# ---------------------------------------------------------------------------
# on_start dispatch contract (kernel-free via bound-method invocation)
# ---------------------------------------------------------------------------


def _make_stub(mode: str):
    """Build an object that mimics the minimal `self` surface
    `XtradeStrategy.on_start` touches: `.config.mode`, `.on_start_common`,
    `.on_start_backtest`, `.on_start_live`. Each hook records a call."""
    calls: list[str] = []

    stub = SimpleNamespace(
        # `XtradeStrategy.on_start` reads `self.mode` (a property that
        # normally proxies to `self.config.mode`). The property is defined
        # on `XtradeStrategy` and won't resolve on a `SimpleNamespace`, so
        # we surface `mode` directly on the stub.
        mode=mode,
        config=SimpleNamespace(mode=mode),
        on_start_common=lambda: calls.append("common"),
        on_start_backtest=lambda: calls.append("backtest"),
        on_start_live=lambda: calls.append("live"),
    )
    return stub, calls


def test_on_start_routes_to_backtest_hook_in_backtest_mode() -> None:
    stub, calls = _make_stub("backtest")
    XtradeStrategy.on_start(stub)  # type: ignore[arg-type]
    assert calls == ["common", "backtest"]


def test_on_start_routes_to_live_hook_in_live_mode() -> None:
    stub, calls = _make_stub("live")
    XtradeStrategy.on_start(stub)  # type: ignore[arg-type]
    assert calls == ["common", "live"]


def test_on_start_common_always_runs_before_mode_branch() -> None:
    """common must precede the mode-specific hook (so a subclass can rely
    on setup done in on_start_common when on_start_live fires)."""
    for mode, expected_branch in [("backtest", "backtest"), ("live", "live")]:
        stub, calls = _make_stub(mode)
        XtradeStrategy.on_start(stub)  # type: ignore[arg-type]
        assert calls[0] == "common"
        assert calls[1] == expected_branch


# ---------------------------------------------------------------------------
# Default hook bodies are no-ops (subclass-overridable)
# ---------------------------------------------------------------------------


def test_default_hooks_are_noops() -> None:
    # All three default hooks accept only `self` and return None — exercise
    # them via a minimal stub so we don't need a Nautilus kernel.
    stub = SimpleNamespace()
    assert XtradeStrategy.on_start_common(stub) is None  # type: ignore[arg-type]
    assert XtradeStrategy.on_start_backtest(stub) is None  # type: ignore[arg-type]
    assert XtradeStrategy.on_start_live(stub) is None  # type: ignore[arg-type]
