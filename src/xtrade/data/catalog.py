"""ParquetDataCatalog wrapper.

Placeholder. Task 4 will implement:
  - default_catalog_path() -> Path  (under <repo>/data/catalog)
  - write_bars(catalog_path, bars, instrument) -> None  (idempotent)
  - read_bars(catalog_path, instrument, start, end) -> list[Bar]

The catalog layout follows Nautilus's
nautilus_trader.persistence.catalog.ParquetDataCatalog convention.
"""

from __future__ import annotations

from pathlib import Path


def default_catalog_path() -> Path:
    """Return the project-default ParquetDataCatalog root.

    Resolves to `<repo>/data/catalog`. The directory is created lazily
    by the writer functions; this getter does not touch the filesystem.
    """
    # src/xtrade/data/catalog.py -> repo root is parents[3].
    return Path(__file__).resolve().parents[3] / "data" / "catalog"
