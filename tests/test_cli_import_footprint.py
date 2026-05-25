"""CLI import-footprint guard (Phase 5 Track A6(a)).

Why
---
`xtrade.cli` is the entry point for every operator command, including the
"is the supervisor dead?" commands `xtrade ops status`, `xtrade bridge serve`,
and `xtrade live supervise`. The Phase 5 brief mandates that these cold
paths boot into stdlib + nautilus core only — vectorbt / numba / the
`xtrade.research.*` scanner stack must NOT be pulled into the import graph
just by importing `xtrade.cli` or running `xtrade --help`.

The previous guard (`test_research_import_isolation.py`) covers the live /
bridge / supervisor module imports specifically and intentionally permits
sklearn-via-vectorbt for the strategy layer. This guard is a *narrower,
CLI-only* contract: the top-level `xtrade.cli` module — used to install
the `xtrade` console script and to dispatch any subcommand — must stay free
of the Phase 2 research stack so cold-start RSS stays small and shells
don't pay vectorbt / numba init cost for `xtrade --help`.

Per brief §A6 deferred item (a):
  "在 tests/test_cli_import_footprint.py（新增）锁住：
   python -X importtime -c 'import xtrade.cli' 输出不得包含
   vectorbt 或 numba 行（regex 守护）。"

We run the checks in a *subprocess* with a clean interpreter so any module
the parent pytest process has already pulled in (vectorbt for scanner
tests, numba for jit warmup, etc.) does not pollute the guard.
"""

from __future__ import annotations

import re
import subprocess
import sys
import textwrap


# Modules forbidden in the CLI cold-start import graph.
#
# vectorbt / numba ship with sklearn / pandas / pyarrow as transitive deps;
# we list the heavy hitters explicitly so a regression that pulls any of
# them in fails loudly with a readable diagnostic.
_FORBIDDEN_TOP_LEVEL = (
    "vectorbt",
    "numba",
    "pandas",
    "sklearn",
    "pyarrow",
    "tqdm",
    "lightgbm",
)

# Forbidden xtrade subpackages — the research / scanner / training stack
# is the whole point of this guard. `xtrade.research` itself is forbidden
# because importing it eagerly loads scanners → vectorbt.
_FORBIDDEN_XTRADE = (
    "xtrade.research",
    "xtrade.research.scanners",
    "xtrade.research.frames",
    "xtrade.research.gridsearch",
    "xtrade.research.train",
    "xtrade.research.dataset",
    "xtrade.research.ml_gate",
    "xtrade.research.news",
)


def _run(script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=60,
    )


# ---------------------------------------------------------------------------
# 1. The explicit brief deliverable: regex guard on `-X importtime` output.
# ---------------------------------------------------------------------------


def test_importtime_output_contains_no_vectorbt_or_numba_lines() -> None:
    """`python -X importtime -c 'import xtrade.cli'` stderr must not log any
    `vectorbt` or `numba` import line. Format of each line is
    `import time: <self_us> | <cumulative_us> | <module_path>`.
    """
    result = subprocess.run(
        [sys.executable, "-X", "importtime", "-c", "import xtrade.cli"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, (
        f"importtime probe failed: rc={result.returncode}\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )

    # `-X importtime` writes to stderr. Match the trailing module path on
    # each "import time:" line.
    pattern = re.compile(
        r"^import time:\s*\d+\s*\|\s*\d+\s*\|\s*[ ]*(?P<mod>[\w\.]+)\s*$",
        re.MULTILINE,
    )
    bad_pattern = re.compile(r"(?:^|\.)(vectorbt|numba)(?:\.|$)")
    leaks = sorted(
        {
            m.group("mod")
            for m in pattern.finditer(result.stderr)
            if bad_pattern.search(m.group("mod"))
        }
    )
    assert not leaks, (
        "import xtrade.cli leaked forbidden heavy modules into the import "
        f"graph: {leaks}\nFull importtime trace:\n{result.stderr}"
    )


# ---------------------------------------------------------------------------
# 2. sys.modules guard — same contract, cheaper to introspect.
# ---------------------------------------------------------------------------


def test_bare_cli_import_does_not_pull_heavy_modules() -> None:
    """`import xtrade.cli` alone must not pull vectorbt / numba / pandas /
    sklearn / pyarrow / tqdm / lightgbm into sys.modules."""
    script = textwrap.dedent(
        f"""
        import sys
        import xtrade.cli  # noqa: F401
        forbidden = {_FORBIDDEN_TOP_LEVEL!r}
        leaked = sorted(
            mod for mod in sys.modules
            if any(mod == f or mod.startswith(f + ".") for f in forbidden)
        )
        if leaked:
            print("LEAKED:" + ",".join(leaked))
            sys.exit(1)
        print("OK")
        """
    )
    result = _run(script)
    assert result.returncode == 0, (
        f"bare-cli-import isolation violated: rc={result.returncode}\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "OK" in result.stdout


def test_bare_cli_import_does_not_pull_research_subpackages() -> None:
    """`import xtrade.cli` alone must not pull any `xtrade.research.*`
    submodule. The research stack is for `xtrade scan` / `xtrade backtest`
    only and must be loaded inside the corresponding command body."""
    script = textwrap.dedent(
        f"""
        import sys
        import xtrade.cli  # noqa: F401
        forbidden = {_FORBIDDEN_XTRADE!r}
        leaked = sorted(
            mod for mod in sys.modules
            if any(mod == f or mod.startswith(f + ".") for f in forbidden)
        )
        if leaked:
            print("LEAKED:" + ",".join(leaked))
            sys.exit(1)
        print("OK")
        """
    )
    result = _run(script)
    assert result.returncode == 0, (
        f"bare-cli-import research isolation violated: rc={result.returncode}\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "OK" in result.stdout


# ---------------------------------------------------------------------------
# 3. `xtrade --help` cold path — operators run this constantly, must be cheap.
# ---------------------------------------------------------------------------


def test_xtrade_help_invocation_does_not_pull_heavy_modules() -> None:
    """Invoking `xtrade --help` via typer's CliRunner must not pull any
    heavy module. Help rendering only walks Typer's registered command
    tree and must not eagerly evaluate command bodies."""
    script = textwrap.dedent(
        f"""
        import sys
        from xtrade.cli import app
        from typer.testing import CliRunner
        runner = CliRunner()
        result = runner.invoke(app, ["--help"])
        if result.exit_code != 0:
            print("HELP_FAILED:" + str(result.exit_code))
            print(result.stdout)
            sys.exit(2)
        forbidden = {_FORBIDDEN_TOP_LEVEL + _FORBIDDEN_XTRADE!r}
        leaked = sorted(
            mod for mod in sys.modules
            if any(mod == f or mod.startswith(f + ".") for f in forbidden)
        )
        if leaked:
            print("LEAKED:" + ",".join(leaked))
            sys.exit(1)
        print("OK")
        """
    )
    result = _run(script)
    assert result.returncode == 0, (
        f"xtrade --help isolation violated: rc={result.returncode}\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "OK" in result.stdout
