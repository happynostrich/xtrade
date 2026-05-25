"""Import-graph guard: live / bridge / supervisor modules must NOT pull
in the heavy Phase 5 Track B research stack at import time.

Why
---
The supervisor + bridge run inside a 512 MiB systemd slice (see
`deploy/systemd/xtrade-supervisor.service`). Pulling lightgbm into the
import graph would balloon RSS and slow startup. The brief §5
isolation contract for Track B is:

  * `xtrade.research.dataset.*`, `xtrade.research.train`,
    `xtrade.research.ml_gate`, `xtrade.research.news.*` must be
    imported lazily (or not at all) by the live path.
  * `lightgbm` must NEVER appear in the live path's `sys.modules`
    after importing supervisor/bridge entry modules.

Note: sklearn is pulled transitively by `vectorbt`, which is a runtime
dep for the Phase 2 scanner stack. That's a pre-Track-B fact of life;
this guard intentionally does NOT block sklearn.

We enforce this in a subprocess to avoid pollution from tests that have
already loaded these modules into the parent pytest process.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap


# Modules forbidden in the live import graph. sklearn is intentionally
# omitted — vectorbt (a Phase 2 runtime dep) already pulls it.
_FORBIDDEN = (
    "lightgbm",
    "xtrade.research.train",
    "xtrade.research.dataset",
    "xtrade.research.dataset.build",
    "xtrade.research.ml_gate",
    "xtrade.research.news",
    "xtrade.research.news.pipeline",
    "xtrade.research.news.scorers",
)


def _run_isolation_check(import_lines: str) -> tuple[int, str, str]:
    """Spawn a fresh Python; import the given live modules; report sys.modules."""
    script = textwrap.dedent(
        f"""
        import sys
        {import_lines}
        forbidden = {_FORBIDDEN!r}
        leaked = sorted(m for m in forbidden if m in sys.modules)
        if leaked:
            print("LEAKED:" + ",".join(leaked))
            sys.exit(1)
        print("OK")
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=60,
    )
    return result.returncode, result.stdout, result.stderr


def test_supervisor_import_does_not_pull_research_stack() -> None:
    rc, out, err = _run_isolation_check("import xtrade.live.supervisor")
    assert rc == 0, f"isolation violated: {out}\n{err}"
    assert "OK" in out


def test_bridge_inbound_import_does_not_pull_research_stack() -> None:
    rc, out, err = _run_isolation_check("import xtrade.bridge.inbound")
    assert rc == 0, f"isolation violated: {out}\n{err}"


def test_live_runner_import_does_not_pull_research_stack() -> None:
    rc, out, err = _run_isolation_check("import xtrade.live.runner")
    assert rc == 0, f"isolation violated: {out}\n{err}"


def test_signal_runner_import_does_not_pull_research_stack() -> None:
    rc, out, err = _run_isolation_check("import xtrade.live.signal_runner")
    assert rc == 0, f"isolation violated: {out}\n{err}"


def test_momentum_follow_import_does_not_pull_research_stack() -> None:
    # The strategy's lazy ml_gate import path: importing the module alone
    # (without constructing with ml_gate config) must NOT pull ml_gate.
    rc, out, err = _run_isolation_check("import xtrade.strategy.plugins.momentum_follow")
    assert rc == 0, f"isolation violated: {out}\n{err}"


def test_momentum_follow_default_construct_does_not_pull_ml_gate() -> None:
    # Even constructing the default strategy (no ml_gate config) must not pull ml.
    rc, out, err = _run_isolation_check(
        "from xtrade.strategy.plugins.momentum_follow import MomentumFollow; "
        "MomentumFollow()"
    )
    assert rc == 0, f"isolation violated: {out}\n{err}"
