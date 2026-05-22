"""Scanner abstract base class + registry.

Contract (mirrors `docs/phase2_brief.md` §5 Task 3):

  - `name` is a registry-unique string used by the CLI (`xtrade scan run
    --scanner momentum`) and stamped into each Signal's `source` field.
  - `default_param_grid()` returns a dict of param-name → list-of-values
    used by `gridsearch.run_grid` when no explicit grid is given.
  - `compute_signals(panel, params)` returns `(entries, exits)` — two
    boolean DataFrames the same shape as `panel`. `entries[t, s] = True`
    means "enter LONG of symbol `s` at the close of ts_event `t`";
    `exits[t, s] = True` means "flatten the position at that bar".
  - `run(panel, params)` is the convenience wrapper that converts those
    boolean panels into the long-format Signal records the queue stores.

Implementations should be pure functions of `(panel, params)` so the
grid search can fan them out trivially.
"""

from __future__ import annotations

import hashlib
import json
from abc import ABC, abstractmethod
from typing import Any

import pandas as pd


_SCANNER_REGISTRY: dict[str, type["Scanner"]] = {}


def register_scanner(cls: type["Scanner"]) -> type["Scanner"]:
    """Class decorator: register a Scanner subclass by its `name` attribute."""
    name = getattr(cls, "name", None)
    if not name or not isinstance(name, str):
        raise TypeError(
            f"{cls.__name__} must declare a non-empty string `name` class attr "
            f"before registration"
        )
    existing = _SCANNER_REGISTRY.get(name)
    if existing is not None and existing is not cls:
        raise ValueError(
            f"scanner name {name!r} already registered to {existing.__name__}; "
            f"refusing to overwrite with {cls.__name__}"
        )
    _SCANNER_REGISTRY[name] = cls
    return cls


def get_scanner(name: str) -> type["Scanner"]:
    """Return the Scanner subclass registered under `name`.

    Raises `KeyError` (mapped to CLI exit 2 by callers) on unknown names.
    """
    try:
        return _SCANNER_REGISTRY[name]
    except KeyError as exc:
        raise KeyError(
            f"unknown scanner {name!r}; available: {sorted(_SCANNER_REGISTRY)}"
        ) from exc


def available_scanners() -> tuple[str, ...]:
    """Return registered scanner names in alphabetical order."""
    return tuple(sorted(_SCANNER_REGISTRY))


# ---------------------------------------------------------------------------
# Scanner base class
# ---------------------------------------------------------------------------


class Scanner(ABC):
    """Abstract base class for all Phase 2 scanners.

    Subclasses must:
      1. Set `name: str` as a class attribute (unique in the registry).
      2. Decorate the class with `@register_scanner`.
      3. Implement `compute_signals(panel, params)`.
      4. Implement `default_param_grid()` classmethod (used by gridsearch
         when caller doesn't supply one).
    """

    name: str = ""  # subclasses override

    @abstractmethod
    def compute_signals(
        self, panel: pd.DataFrame, params: dict[str, Any]
    ) -> tuple[pd.DataFrame, pd.DataFrame]:
        """Return `(entries, exits)` boolean panels matched to `panel.shape`."""

    @classmethod
    @abstractmethod
    def default_param_grid(cls) -> dict[str, list[Any]]:
        """Return the param-name → list-of-values grid used by gridsearch."""

    def run(self, panel: pd.DataFrame, params: dict[str, Any]) -> pd.DataFrame:
        """Compute signals and surface them as long-format Signal records.

        Output columns:
            ts_event   (UTC pd.Timestamp)
            symbol     (str — InstrumentId)
            direction  ("LONG" for entries, "FLAT" for exits)
            strength   (1.0 for LONG entries, 0.0 for FLAT exits)
            source     ("<scanner_name>:<8-char-params-hash>")
            params     (json string for traceability)
        """
        entries, exits = self.compute_signals(panel, params)
        _validate_signal_panels(panel, entries, exits)
        return _signals_to_records(
            entries, exits, scanner_name=self.name, params=params
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _validate_signal_panels(
    panel: pd.DataFrame, entries: pd.DataFrame, exits: pd.DataFrame
) -> None:
    if entries.shape != panel.shape:
        raise ValueError(
            f"entries shape {entries.shape} != panel shape {panel.shape}"
        )
    if exits.shape != panel.shape:
        raise ValueError(
            f"exits shape {exits.shape} != panel shape {panel.shape}"
        )
    if not entries.index.equals(panel.index) or not entries.columns.equals(panel.columns):
        raise ValueError("entries panel must share panel's index and columns")
    if not exits.index.equals(panel.index) or not exits.columns.equals(panel.columns):
        raise ValueError("exits panel must share panel's index and columns")


def params_hash(params: dict[str, Any]) -> str:
    """Stable 8-hex-char fingerprint of a params mapping (used in `source`)."""
    blob = json.dumps(params, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha1(blob).hexdigest()[:8]


def _signals_to_records(
    entries: pd.DataFrame,
    exits: pd.DataFrame,
    *,
    scanner_name: str,
    params: dict[str, Any],
) -> pd.DataFrame:
    """Stack boolean panels into a long-format record DataFrame."""
    source = f"{scanner_name}:{params_hash(params)}"
    params_json = json.dumps(params, sort_keys=True, default=str)

    rows: list[dict[str, Any]] = []
    # Iterate column-wise; vectorised but readable. Symbols rarely > 100.
    for sym in entries.columns:
        e_col = entries[sym]
        x_col = exits[sym]
        for ts in e_col.index[e_col.fillna(False).astype(bool)]:
            rows.append(
                {
                    "ts_event": ts,
                    "symbol": sym,
                    "direction": "LONG",
                    "strength": 1.0,
                    "source": source,
                    "params": params_json,
                }
            )
        for ts in x_col.index[x_col.fillna(False).astype(bool)]:
            rows.append(
                {
                    "ts_event": ts,
                    "symbol": sym,
                    "direction": "FLAT",
                    "strength": 0.0,
                    "source": source,
                    "params": params_json,
                }
            )

    if not rows:
        return pd.DataFrame(
            columns=["ts_event", "symbol", "direction", "strength", "source", "params"]
        )
    return pd.DataFrame.from_records(rows).sort_values(
        ["ts_event", "symbol", "direction"]
    ).reset_index(drop=True)
