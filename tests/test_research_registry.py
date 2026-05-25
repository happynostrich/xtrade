"""Tests for `xtrade.research.registry` (Phase 5 / Track C3).

Coverage
--------
* `read_active` returns None when active.json is absent.
* `read_active` resolves relative `model_path` against `models_root`.
* `promote` validates the candidate triple (model.pkl + metrics.json +
  dataset_meta.json) — missing any → ModelRegistryError.
* `promote` rejects metrics.json with auc < 0.5.
* `promote` rejects metrics.json with non-numeric / missing auc.
* `promote` writes active.json atomically and appends to active.history.jsonl.
* Second promote rolls `previous` field correctly.
* Registry path is pure file IO: no sklearn / lightgbm in sys.modules.
"""

from __future__ import annotations

import datetime as dt
import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from xtrade.research.registry import (
    ACTIVE_FILENAME,
    HISTORY_FILENAME,
    ModelRegistryError,
    promote,
    read_active,
)


UTC = dt.timezone.utc
NOW = dt.datetime(2026, 5, 25, 14, 30, 0, tzinfo=UTC)


def _seed_run(
    models_root: Path,
    run_id: str,
    *,
    auc: float | str | None = 0.71,
    include_model: bool = True,
    include_metrics: bool = True,
    include_dataset_meta: bool = True,
) -> Path:
    run_dir = models_root / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    if include_model:
        (run_dir / "model.pkl").write_bytes(b"unused-by-promote")
    if include_metrics:
        body: dict = {
            "run_id": run_id,
            "model_name": "logistic",
            "seed": 0,
        }
        if auc is not None:
            body["auc"] = auc
        (run_dir / "metrics.json").write_text(
            json.dumps(body, sort_keys=True), encoding="utf-8"
        )
    if include_dataset_meta:
        (run_dir / "dataset_meta.json").write_text(
            json.dumps({"feature_names": ["a", "b"]}, sort_keys=True),
            encoding="utf-8",
        )
    return run_dir


# ---- read_active --------------------------------------------------------


def test_read_active_returns_none_when_missing(tmp_path: Path) -> None:
    assert read_active(tmp_path) is None


def test_read_active_returns_none_when_no_file(tmp_path: Path) -> None:
    (tmp_path / "other.json").write_text("{}")
    assert read_active(tmp_path) is None


def test_read_active_rejects_non_dict(tmp_path: Path) -> None:
    (tmp_path / ACTIVE_FILENAME).write_text("[]")
    with pytest.raises(ModelRegistryError):
        read_active(tmp_path)


def test_read_active_rejects_missing_keys(tmp_path: Path) -> None:
    (tmp_path / ACTIVE_FILENAME).write_text(
        json.dumps({"run_id": "x"})
    )
    with pytest.raises(ModelRegistryError):
        read_active(tmp_path)


def test_read_active_resolves_relative_model_path(tmp_path: Path) -> None:
    (tmp_path / ACTIVE_FILENAME).write_text(
        json.dumps(
            {
                "run_id": "abc",
                "model_path": "abc/model.pkl",
                "active_since": NOW.isoformat(),
                "promoted_by": "test",
                "previous": None,
            }
        )
    )
    out = read_active(tmp_path)
    assert out is not None
    assert out.model_path == tmp_path / "abc" / "model.pkl"
    assert out.run_id == "abc"
    assert out.previous is None


# ---- promote validation -------------------------------------------------


def test_promote_rejects_unknown_run(tmp_path: Path) -> None:
    with pytest.raises(ModelRegistryError, match="not found"):
        promote("missing", models_root=tmp_path, now=NOW)


def test_promote_rejects_missing_model_pkl(tmp_path: Path) -> None:
    _seed_run(tmp_path, "abc", include_model=False)
    with pytest.raises(ModelRegistryError, match="model.pkl"):
        promote("abc", models_root=tmp_path, now=NOW)


def test_promote_rejects_missing_metrics_json(tmp_path: Path) -> None:
    _seed_run(tmp_path, "abc", include_metrics=False)
    with pytest.raises(ModelRegistryError, match="metrics.json"):
        promote("abc", models_root=tmp_path, now=NOW)


def test_promote_rejects_missing_dataset_meta(tmp_path: Path) -> None:
    _seed_run(tmp_path, "abc", include_dataset_meta=False)
    with pytest.raises(ModelRegistryError, match="dataset_meta.json"):
        promote("abc", models_root=tmp_path, now=NOW)


def test_promote_rejects_auc_below_threshold(tmp_path: Path) -> None:
    _seed_run(tmp_path, "abc", auc=0.49)
    with pytest.raises(ModelRegistryError, match="auc"):
        promote("abc", models_root=tmp_path, now=NOW)


def test_promote_rejects_missing_auc(tmp_path: Path) -> None:
    _seed_run(tmp_path, "abc", auc=None)
    with pytest.raises(ModelRegistryError, match="auc"):
        promote("abc", models_root=tmp_path, now=NOW)


def test_promote_rejects_non_numeric_auc(tmp_path: Path) -> None:
    _seed_run(tmp_path, "abc", auc="great")  # type: ignore[arg-type]
    with pytest.raises(ModelRegistryError, match="auc"):
        promote("abc", models_root=tmp_path, now=NOW)


def test_promote_rejects_empty_run_id(tmp_path: Path) -> None:
    with pytest.raises(ModelRegistryError, match="run_id"):
        promote("   ", models_root=tmp_path, now=NOW)


def test_promote_rejects_naive_now(tmp_path: Path) -> None:
    _seed_run(tmp_path, "abc")
    with pytest.raises(ValueError, match="timezone-aware"):
        promote(
            "abc",
            models_root=tmp_path,
            now=dt.datetime(2026, 5, 25, 14, 30),  # naive
        )


# ---- promote happy path -------------------------------------------------


def test_promote_writes_active_json_and_history(tmp_path: Path) -> None:
    _seed_run(tmp_path, "abc12345", auc=0.71)
    active = promote("abc12345", models_root=tmp_path, promoted_by="ci", now=NOW)
    assert active.run_id == "abc12345"
    assert active.model_path == tmp_path / "abc12345" / "model.pkl"
    assert active.active_since.startswith("2026-05-25T14:30:00")
    assert active.previous is None

    body = json.loads((tmp_path / ACTIVE_FILENAME).read_text())
    assert body["run_id"] == "abc12345"
    assert body["model_path"] == "abc12345/model.pkl"
    assert body["promoted_by"] == "ci"
    assert body["previous"] is None

    history = (tmp_path / HISTORY_FILENAME).read_text().splitlines()
    assert len(history) == 1
    row = json.loads(history[0])
    assert row["event"] == "promote"
    assert row["run_id"] == "abc12345"


def test_second_promote_rolls_previous(tmp_path: Path) -> None:
    _seed_run(tmp_path, "first", auc=0.71)
    _seed_run(tmp_path, "second", auc=0.72)
    promote("first", models_root=tmp_path, now=NOW)
    later = NOW + dt.timedelta(hours=1)
    active = promote("second", models_root=tmp_path, now=later)
    assert active.previous == {
        "run_id": "first",
        "active_since": NOW.isoformat(),
    }
    body = json.loads((tmp_path / ACTIVE_FILENAME).read_text())
    assert body["run_id"] == "second"
    assert body["previous"]["run_id"] == "first"

    history = (tmp_path / HISTORY_FILENAME).read_text().splitlines()
    assert len(history) == 2
    row2 = json.loads(history[1])
    assert row2["run_id"] == "second"


def test_promote_no_tmp_left_behind(tmp_path: Path) -> None:
    _seed_run(tmp_path, "abc")
    promote("abc", models_root=tmp_path, now=NOW)
    assert not (tmp_path / f"{ACTIVE_FILENAME}.tmp").exists()


# ---- read after promote -------------------------------------------------


def test_read_active_after_promote_round_trips(tmp_path: Path) -> None:
    _seed_run(tmp_path, "abc12345")
    promote("abc12345", models_root=tmp_path, now=NOW)
    active = read_active(tmp_path)
    assert active is not None
    assert active.run_id == "abc12345"
    assert active.model_path == tmp_path / "abc12345" / "model.pkl"


# ---- import isolation ---------------------------------------------------


def test_registry_promote_does_not_pull_research_stack(tmp_path: Path) -> None:
    """`promote` is pure file IO — no sklearn / lightgbm / xtrade.research.ml_gate."""
    # We seed the candidate, then run a subprocess that only imports the
    # registry and calls promote — and asserts sys.modules stays light.
    _seed_run(tmp_path, "abc12345")
    script = textwrap.dedent(
        f"""
        import sys
        from xtrade.research.registry import promote
        from pathlib import Path
        import datetime as dt
        promote(
            "abc12345",
            models_root=Path({str(tmp_path)!r}),
            promoted_by="t",
            now=dt.datetime(2026, 5, 25, 14, 30, tzinfo=dt.timezone.utc),
        )
        forbidden = ("lightgbm", "sklearn.linear_model", "xtrade.research.ml_gate")
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
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert "OK" in result.stdout
