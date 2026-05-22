"""XtradeStrategy base class (placeholder).

Phase 1 Task 5 will implement a thin `Strategy` subclass that records
the runtime mode (backtest / live) on `self.mode` and provides
`on_start_backtest` / `on_start_live` hooks that default to a shared
`on_start`. Concrete strategies should inherit from this rather than
Nautilus's `Strategy` directly so the CLI can stamp the mode at
construction time.
"""

from __future__ import annotations
