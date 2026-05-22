"""XtradeStrategy base class (Phase 1 Task 5 / P6).

Thin `Strategy` subclass that exposes the runtime mode ("backtest" or
"live") to subclasses so the same strategy code can run under
`xtrade backtest run` and `xtrade live run` without duplication.

Design:
  - `XtradeStrategyConfig` adds a single `mode` field on top of
    Nautilus's `StrategyConfig`. The CLI stamps it at construction time.
  - `XtradeStrategy.on_start` dispatches to `on_start_backtest` or
    `on_start_live` based on `config.mode`, both of which default to
    calling a shared `on_start_common` hook. Subclasses typically only
    need to override `on_start_common` (and `on_bar`).

The dispatch lets a strategy keep mode-specific setup (e.g. requesting
historical bars in live mode but skipping that in backtest, where the
engine has already pre-loaded them) without polluting business logic.
"""

from __future__ import annotations

from typing import Literal

from nautilus_trader.config import StrategyConfig
from nautilus_trader.trading.strategy import Strategy


Mode = Literal["backtest", "live"]


class XtradeStrategyConfig(StrategyConfig, frozen=True, kw_only=True):
    """Shared base for all xtrade strategy configs.

    Subclasses extend this with strategy-specific parameters.
    """

    mode: Mode = "backtest"


class XtradeStrategy(Strategy):
    """Base class for xtrade strategies.

    Subclasses override `on_start_common` and `on_bar` (and friends).
    The CLI sets `config.mode` before instantiating the strategy so the
    same subclass works in both backtest and live runs.
    """

    @property
    def mode(self) -> Mode:
        return self.config.mode  # type: ignore[attr-defined]

    def on_start(self) -> None:  # noqa: D401 - Nautilus hook
        """Dispatch to mode-specific start hook."""
        self.on_start_common()
        if self.mode == "live":
            self.on_start_live()
        else:
            self.on_start_backtest()

    # Override points -------------------------------------------------------

    def on_start_common(self) -> None:
        """Setup that should run in both backtest and live (e.g. register
        indicators, look up the instrument in the cache)."""

    def on_start_backtest(self) -> None:
        """Backtest-only setup (default: no-op)."""

    def on_start_live(self) -> None:
        """Live-only setup (default: no-op)."""
