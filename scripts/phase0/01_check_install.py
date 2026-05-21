"""Task A — C1: NautilusTrader installation self-check.

Verifies that NautilusTrader is importable, prints its version, and that
a few core symbols are reachable. A trivial backtest scaffold is also
constructed (engine instantiation only) to confirm the engine class
imports do not blow up. A full end-to-end backtest with synthetic data
is executed by `08_sample_backtest.py` (Task G).

Exit code 0 on PASS, 1 on FAIL.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

# Allow running this file directly: `python scripts/phase0/01_check_install.py`
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import append_result, stepwise  # noqa: E402


CHECK_ID = "C1"
CHECK_NAME = "NautilusTrader installation self-check"


def main() -> int:
    with stepwise(CHECK_ID, CHECK_NAME):
        notes: list[str] = []

        # 1. Import nautilus_trader and print version.
        nt = importlib.import_module("nautilus_trader")
        version = getattr(nt, "__version__", "unknown")
        print(f"nautilus_trader version: {version}")
        notes.append(f"nautilus_trader.__version__ = {version}")

        # 2. Probe core modules.
        probes = [
            "nautilus_trader.core",
            "nautilus_trader.model",
            "nautilus_trader.model.identifiers",
            "nautilus_trader.model.objects",
            "nautilus_trader.backtest.engine",
            "nautilus_trader.config",
        ]
        for mod_name in probes:
            importlib.import_module(mod_name)
            print(f"  imported: {mod_name}")
        notes.append("imported core modules: " + ", ".join(probes))

        # 3. Probe adapter packages (presence only).
        for adapter in (
            "nautilus_trader.adapters.binance",
            "nautilus_trader.adapters.hyperliquid",
        ):
            try:
                importlib.import_module(adapter)
                print(f"  adapter available: {adapter}")
                notes.append(f"adapter available: {adapter}")
            except Exception as exc:  # noqa: BLE001
                print(f"  adapter MISSING: {adapter} ({exc})")
                notes.append(f"adapter MISSING: {adapter} -- {exc}")

        # 4. Instantiate the backtest engine to confirm the Rust core loads.
        from nautilus_trader.backtest.engine import BacktestEngine, BacktestEngineConfig

        engine = BacktestEngine(config=BacktestEngineConfig())
        print(f"  BacktestEngine instantiated: {type(engine).__name__}")
        notes.append("BacktestEngine instantiated successfully")
        engine.dispose()

        append_result(CHECK_ID, CHECK_NAME, "PASS", notes=notes)
        print(f"[{CHECK_ID}] PASS")
        return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception:
        sys.exit(1)
