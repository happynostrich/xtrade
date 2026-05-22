"""Symbol universe loader for Phase 2 scanners.

A *universe* is the explicit list of `(venue, symbol)` pairs a scanner
will consider on a given run. It is declared in yaml — never inferred
from the catalog — so reproducibility is easy and the operator owns the
selection.

YAML shape (see `config/universe.example.yaml`):

```yaml
binance:
  - symbol: BTCUSDT
  - symbol: ETHUSDT
    quote: USDT          # optional, default per-venue
    min_volume: 1000.0   # optional filter hint for scanners
hyperliquid:
  - symbol: xyz:TSLA
  - symbol: xyz:NVDA
```

The flat `UniverseConfig.symbols` list is the only thing scanners see;
the venue-grouped yaml is purely a readability convenience.

Phase 2 brief §6: the universe loader is read-only and never imports
from `xtrade.live.*` — research layer is execution-free.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover - pyyaml is in deps
    yaml = None  # type: ignore[assignment]


# Recognised venue keys. Mirrors `xtrade.data.instruments.resolve` dispatch.
_KNOWN_VENUES: frozenset[str] = frozenset({"binance", "hyperliquid"})

# Default quote currency per venue. Matches Phase 1 ingest convention.
_DEFAULT_QUOTE: dict[str, str] = {
    "binance": "USDT",
    "hyperliquid": "USDC",
}


class UniverseConfigError(RuntimeError):
    """Raised when a universe yaml is malformed, references an unknown venue,
    or contains duplicate symbols. Mapped to CLI exit code 2."""


@dataclass(frozen=True)
class SymbolSpec:
    """One row in the universe — a venue-qualified tradable symbol.

    `venue` is a CLI-style tag (`"binance"` / `"hyperliquid"`), matching
    `xtrade.data.instruments.resolve`. `symbol` is the venue-native ticker
    (e.g. `"BTCUSDT"`, `"xyz:TSLA"`). `quote` is informational metadata for
    the scanner; it does *not* override Nautilus instrument resolution.
    `min_volume` is a soft hint scanners may use to skip illiquid bars.
    """

    venue: str
    symbol: str
    quote: str
    min_volume: float | None = None

    def key(self) -> tuple[str, str]:
        """Identity tuple used for dedup inside `UniverseConfig`."""
        return (self.venue, self.symbol)


@dataclass(frozen=True)
class UniverseConfig:
    """Parsed universe.yaml contents.

    `symbols` is a tuple (not a list) so the config is hashable and safe
    to use as a cache key. `source_path` is set by `load_universe` and
    carried through so callers can snapshot the originating yaml into
    `logs/<run-id>/config.snapshot.yaml`.
    """

    symbols: tuple[SymbolSpec, ...]
    source_path: Path | None = None

    def by_venue(self) -> dict[str, tuple[SymbolSpec, ...]]:
        """Group symbols by venue while preserving original order."""
        out: dict[str, list[SymbolSpec]] = {}
        for s in self.symbols:
            out.setdefault(s.venue, []).append(s)
        return {v: tuple(items) for v, items in out.items()}

    def __len__(self) -> int:
        return len(self.symbols)


def _parse_symbol_entry(
    venue: str, raw: Any, *, src: Path | None
) -> SymbolSpec:
    """Coerce one yaml row into a `SymbolSpec` with venue defaults applied."""
    if not isinstance(raw, dict):
        raise UniverseConfigError(
            f"{src or '<inline>'}: each entry under venue {venue!r} must be a "
            f"mapping, got {type(raw).__name__}: {raw!r}"
        )
    symbol = raw.get("symbol")
    if not symbol or not isinstance(symbol, str):
        raise UniverseConfigError(
            f"{src or '<inline>'}: missing or non-string `symbol` under venue "
            f"{venue!r}: {raw!r}"
        )
    quote = raw.get("quote", _DEFAULT_QUOTE[venue])
    if not isinstance(quote, str):
        raise UniverseConfigError(
            f"{src or '<inline>'}: `quote` must be a string for {venue}/{symbol}, "
            f"got {type(quote).__name__}"
        )
    min_volume = raw.get("min_volume")
    if min_volume is not None:
        try:
            min_volume = float(min_volume)
        except (TypeError, ValueError) as exc:
            raise UniverseConfigError(
                f"{src or '<inline>'}: `min_volume` must be numeric for "
                f"{venue}/{symbol}, got {min_volume!r}"
            ) from exc
        if min_volume < 0:
            raise UniverseConfigError(
                f"{src or '<inline>'}: `min_volume` must be >= 0 for "
                f"{venue}/{symbol}, got {min_volume}"
            )
    return SymbolSpec(venue=venue, symbol=symbol, quote=quote, min_volume=min_volume)


def load_universe(path: str | Path) -> UniverseConfig:
    """Parse a universe yaml file into a `UniverseConfig`.

    Raises `UniverseConfigError` (mapped to CLI exit 2) on any malformed
    structure, unknown venue, duplicate symbol, or unreadable file.
    """
    if yaml is None:  # pragma: no cover - pyyaml is in deps
        raise UniverseConfigError("pyyaml is not installed but is required to load universe yaml")
    p = Path(path)
    if not p.exists():
        raise UniverseConfigError(f"universe yaml does not exist: {p}")
    if not p.is_file():
        raise UniverseConfigError(f"universe yaml must be a regular file: {p}")
    try:
        raw_doc = yaml.safe_load(p.read_text())
    except yaml.YAMLError as exc:
        raise UniverseConfigError(f"failed to parse universe yaml {p}: {exc}") from exc
    if raw_doc is None:
        raise UniverseConfigError(f"universe yaml {p} is empty")
    if not isinstance(raw_doc, dict):
        raise UniverseConfigError(
            f"universe yaml {p} must be a mapping at the top level, got "
            f"{type(raw_doc).__name__}"
        )

    unknown = sorted(set(raw_doc) - _KNOWN_VENUES)
    if unknown:
        raise UniverseConfigError(
            f"{p}: unknown venue key(s) {unknown!r}. "
            f"Valid: {sorted(_KNOWN_VENUES)}."
        )

    parsed: list[SymbolSpec] = []
    seen: set[tuple[str, str]] = set()
    for venue, rows in raw_doc.items():
        if rows is None:
            continue
        if not isinstance(rows, list):
            raise UniverseConfigError(
                f"{p}: entries under venue {venue!r} must be a list, got "
                f"{type(rows).__name__}"
            )
        for row in rows:
            spec = _parse_symbol_entry(venue, row, src=p)
            if spec.key() in seen:
                raise UniverseConfigError(
                    f"{p}: duplicate symbol {spec.venue}:{spec.symbol}"
                )
            seen.add(spec.key())
            parsed.append(spec)

    if not parsed:
        raise UniverseConfigError(
            f"{p}: universe is empty after parsing (no symbols listed)"
        )
    return UniverseConfig(symbols=tuple(parsed), source_path=p)
