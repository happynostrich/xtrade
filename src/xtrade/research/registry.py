"""Model registry (Phase 5 / Track C3).

A tiny on-disk pointer telling strategies which trained run is currently
"active". Eliminates the Phase 5 Track B operational pain of editing
strategy yaml and re-rolling supervisor every time we want to swap a
trained baseline.

Layout
------
::

    models/
      active.json                  # current pointer (atomic-replace target)
      active.history.jsonl         # append-only audit of every promote
      <run_id>/
        model.pkl
        metrics.json
        dataset_meta.json
        ...

`active.json` schema
~~~~~~~~~~~~~~~~~~~~
::

    {
      "run_id": "abc12345",
      "model_path": "models/abc12345/model.pkl",   # relative to models/
      "active_since": "2026-05-25T14:32:00+00:00",
      "promoted_by": "<user-or-runner>",
      "previous": {                                # null on first promote
        "run_id": "...",
        "active_since": "...",
      }
    }

`active.history.jsonl` schema (one line per promote, append-only)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Same fields as `active.json` plus ``"event": "promote"``.

Atomicity
---------
We write a sibling ``active.json.tmp`` then ``os.replace(tmp, active.json)``
so a crashed promote cannot leave a half-written pointer. History rows are
single ``os.write`` calls under ``O_APPEND`` — same POSIX guarantee as
`MLGateAuditWriter` (Track C2).

Trust boundary
--------------
``promote`` validates the candidate triple is present (``model.pkl``,
``metrics.json``, ``dataset_meta.json``) AND that ``metrics.json.auc >= 0.5``
to catch the "obvious bad model" case. It deliberately does NOT unpickle
the model — that's still the strategy's job at construction time. Promote
is pure file IO; no sklearn/lightgbm import.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


UTC = dt.timezone.utc


ACTIVE_FILENAME = "active.json"
HISTORY_FILENAME = "active.history.jsonl"
_MIN_AUC = 0.5


@dataclass(frozen=True)
class ActiveModel:
    """In-memory view of ``active.json``."""

    run_id: str
    model_path: Path
    active_since: str
    promoted_by: str
    previous: dict[str, Any] | None = None


class ModelRegistryError(ValueError):
    """Promote / read-active validation failure."""


# ---- read paths ----------------------------------------------------------


def read_active(models_root: Path | str) -> ActiveModel | None:
    """Return current active model pointer, or ``None`` if none promoted yet.

    ``models_root`` is the directory containing ``active.json`` (typically
    ``models/`` at repo root or ``/var/lib/xtrade/models/`` on VPS). The
    ``model_path`` in the returned struct is resolved against ``models_root``
    so callers receive an absolute path ready to feed `MLGate`.
    """
    root = Path(models_root)
    active_path = root / ACTIVE_FILENAME
    if not active_path.exists():
        return None
    try:
        body = json.loads(active_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ModelRegistryError(
            f"active.json is unreadable: {exc}"
        ) from exc
    if not isinstance(body, dict):
        raise ModelRegistryError("active.json must be a JSON object")
    for key in ("run_id", "model_path", "active_since", "promoted_by"):
        if key not in body:
            raise ModelRegistryError(f"active.json missing key {key!r}")
    rel = Path(str(body["model_path"]))
    resolved = rel if rel.is_absolute() else (root / rel)
    return ActiveModel(
        run_id=str(body["run_id"]),
        model_path=resolved,
        active_since=str(body["active_since"]),
        promoted_by=str(body["promoted_by"]),
        previous=body.get("previous") if isinstance(body.get("previous"), dict) else None,
    )


# ---- promote -------------------------------------------------------------


def promote(
    run_id: str,
    *,
    models_root: Path | str,
    promoted_by: str = "unknown",
    now: dt.datetime | None = None,
) -> ActiveModel:
    """Atomically promote ``models_root/<run_id>/`` to active.

    Validation
    ----------
    1. ``models_root/<run_id>/model.pkl`` exists
    2. ``models_root/<run_id>/metrics.json`` exists and is readable JSON
    3. ``models_root/<run_id>/dataset_meta.json`` exists
    4. ``metrics.json["auc"]`` >= 0.5 (any of ``auc`` / ``auc_test``)

    On success
    ----------
    - Writes ``models_root/active.json`` atomically via ``os.replace``
    - Appends one row to ``models_root/active.history.jsonl``

    Returns the new ``ActiveModel``. Raises ``ModelRegistryError`` on
    validation failure with the candidate untouched.
    """
    if not isinstance(run_id, str) or not run_id.strip():
        raise ModelRegistryError("run_id must be a non-empty string")
    root = Path(models_root)
    candidate_dir = root / run_id
    if not candidate_dir.is_dir():
        raise ModelRegistryError(
            f"candidate run directory not found: {candidate_dir}"
        )
    model_pkl = candidate_dir / "model.pkl"
    metrics_json = candidate_dir / "metrics.json"
    dataset_meta = candidate_dir / "dataset_meta.json"
    missing = [p.name for p in (model_pkl, metrics_json, dataset_meta) if not p.exists()]
    if missing:
        raise ModelRegistryError(
            f"candidate {run_id} missing files: {', '.join(missing)}"
        )

    try:
        metrics = json.loads(metrics_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ModelRegistryError(
            f"metrics.json for {run_id} unreadable: {exc}"
        ) from exc
    if not isinstance(metrics, dict):
        raise ModelRegistryError(f"metrics.json for {run_id} must be a JSON object")
    auc = metrics.get("auc")
    if not isinstance(auc, (int, float)):
        raise ModelRegistryError(
            f"metrics.json for {run_id} missing numeric 'auc'"
        )
    if float(auc) < _MIN_AUC:
        raise ModelRegistryError(
            f"metrics.json for {run_id} has auc={auc} < {_MIN_AUC}; refusing to promote"
        )

    when = now or dt.datetime.now(tz=UTC)
    if when.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    active_since = when.astimezone(UTC).isoformat()

    previous = None
    existing = read_active(root) if (root / ACTIVE_FILENAME).exists() else None
    if existing is not None:
        previous = {
            "run_id": existing.run_id,
            "active_since": existing.active_since,
        }

    rel_model_path = f"{run_id}/model.pkl"
    body = {
        "run_id": run_id,
        "model_path": rel_model_path,
        "active_since": active_since,
        "promoted_by": promoted_by,
        "previous": previous,
    }

    root.mkdir(parents=True, exist_ok=True)
    active_path = root / ACTIVE_FILENAME
    tmp_path = root / f"{ACTIVE_FILENAME}.tmp"
    tmp_path.write_text(
        json.dumps(body, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp_path, active_path)

    history_path = root / HISTORY_FILENAME
    history_line = json.dumps(
        {"event": "promote", **body},
        sort_keys=True,
        ensure_ascii=False,
    )
    fd = os.open(history_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o640)
    try:
        os.write(fd, (history_line + "\n").encode("utf-8"))
    finally:
        os.close(fd)

    return ActiveModel(
        run_id=run_id,
        model_path=root / rel_model_path,
        active_since=active_since,
        promoted_by=promoted_by,
        previous=previous,
    )
