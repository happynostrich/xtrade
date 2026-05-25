"""Tests for `xtrade research promote` CLI (Phase 5 / Track C3)."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from xtrade.cli import app
from xtrade.research.registry import ACTIVE_FILENAME


runner = CliRunner()


def _seed_run(
    models_root: Path,
    run_id: str = "abc12345",
    *,
    auc: float = 0.71,
    include_dataset_meta: bool = True,
) -> None:
    run_dir = models_root / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "model.pkl").write_bytes(b"unused")
    (run_dir / "metrics.json").write_text(json.dumps({"auc": auc}))
    if include_dataset_meta:
        (run_dir / "dataset_meta.json").write_text(
            json.dumps({"feature_names": ["a"]})
        )


def test_promote_succeeds_and_writes_active(tmp_path: Path) -> None:
    _seed_run(tmp_path / "models", "abc12345", auc=0.71)
    result = runner.invoke(
        app,
        [
            "research",
            "promote",
            "abc12345",
            "--models-root",
            str(tmp_path / "models"),
            "--promoted-by",
            "tester",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "promoted" in result.output
    assert "abc12345" in result.output
    body = json.loads((tmp_path / "models" / ACTIVE_FILENAME).read_text())
    assert body["run_id"] == "abc12345"
    assert body["promoted_by"] == "tester"


def test_promote_fails_with_exit_code_2_on_bad_auc(tmp_path: Path) -> None:
    _seed_run(tmp_path / "models", "weak", auc=0.40)
    result = runner.invoke(
        app,
        [
            "research",
            "promote",
            "weak",
            "--models-root",
            str(tmp_path / "models"),
        ],
    )
    assert result.exit_code == 2, result.output


def test_promote_fails_when_dataset_meta_missing(tmp_path: Path) -> None:
    _seed_run(tmp_path / "models", "incomplete", include_dataset_meta=False)
    result = runner.invoke(
        app,
        [
            "research",
            "promote",
            "incomplete",
            "--models-root",
            str(tmp_path / "models"),
        ],
    )
    assert result.exit_code == 2, result.output


def test_promote_fails_when_run_id_unknown(tmp_path: Path) -> None:
    (tmp_path / "models").mkdir()
    result = runner.invoke(
        app,
        [
            "research",
            "promote",
            "ghost",
            "--models-root",
            str(tmp_path / "models"),
        ],
    )
    assert result.exit_code == 2, result.output
