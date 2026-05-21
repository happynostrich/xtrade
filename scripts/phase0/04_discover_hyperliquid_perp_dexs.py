"""Task D — C4 (step 1): Enumerate Hyperliquid perp DEXes.

Pure HTTP. No NautilusTrader, no credentials. The goal is to:

  1. Call the Hyperliquid `/info` endpoint with `{"type":"perpDexs"}` to
     list all builder-deployed perp DEXes (HIP-3).
  2. Identify the `dex` corresponding to **trade.xyz** (expected to be
     `xyz` or similar — confirm empirically).
  3. For that dex, fetch its `meta` to list at least a few stock perps
     (e.g. TSLA, NVDA) and capture exact symbol strings.

Writes findings into `docs/phase0_results.md` so subsequent tasks
(particularly Task E) can use the confirmed `dex:symbol` form.

By default this hits MAINNET (`https://api.hyperliquid.xyz/info`) since
trade.xyz markets only exist on mainnet. The script is **read-only** and
does not place orders. It does not require credentials.

References:
  * https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api
  * https://hyperliquid.gitbook.io/hyperliquid-docs/hyperliquid-improvement-proposals-hips/hip-3-builder-deployed-perpetuals
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import append_result, stepwise  # noqa: E402


CHECK_ID = "C4a"
CHECK_NAME = "Enumerate Hyperliquid perp DEXes; locate trade.xyz dex"

MAINNET_INFO_URL = "https://api.hyperliquid.xyz/info"
TIMEOUT_S = 20.0
# Heuristic name candidates we will check first when picking which dex
# corresponds to trade.xyz. We still log ALL dexes regardless.
TRADEXYZ_NAME_CANDIDATES = ("xyz", "tradexyz", "trade-xyz", "trade.xyz")
# Tickers we expect to find under the trade.xyz dex.
EXPECTED_STOCK_TICKERS = ("TSLA", "NVDA", "MSTR", "COIN", "AAPL", "META", "AMZN", "GOOG")


def _post(url: str, payload: dict[str, Any]) -> Any:
    with httpx.Client(timeout=TIMEOUT_S) as client:
        resp = client.post(url, json=payload, headers={"Content-Type": "application/json"})
        resp.raise_for_status()
        return resp.json()


def _pretty(obj: Any, *, limit: int = 800) -> str:
    s = json.dumps(obj, indent=2, ensure_ascii=False, default=str)
    if len(s) > limit:
        s = s[:limit] + f"\n... (truncated, total len={len(s)})"
    return s


def _extract_dex_names(perp_dexs: Any) -> list[str]:
    """`perpDexs` payload has historically been a list whose entries may
    be either strings or dicts with a `name` field. Tolerate both."""
    names: list[str] = []
    if not isinstance(perp_dexs, list):
        return names
    for entry in perp_dexs:
        if entry is None:
            continue
        if isinstance(entry, str):
            names.append(entry)
        elif isinstance(entry, dict):
            for key in ("name", "dex", "id"):
                if key in entry and isinstance(entry[key], str):
                    names.append(entry[key])
                    break
    return names


def _pick_tradexyz_dex(dex_names: list[str]) -> str | None:
    for cand in TRADEXYZ_NAME_CANDIDATES:
        if cand in dex_names:
            return cand
    # Fall back: any name that contains 'xyz' (case-insensitive).
    for name in dex_names:
        if "xyz" in name.lower():
            return name
    return None


def _list_meta_symbols(meta: Any) -> list[str]:
    """Hyperliquid `meta`/`perpDexMeta` response has `universe: [{name: ...}, ...]`."""
    if not isinstance(meta, dict):
        return []
    universe = meta.get("universe") or []
    syms: list[str] = []
    for u in universe:
        if isinstance(u, dict):
            name = u.get("name")
            if isinstance(name, str):
                syms.append(name)
    return syms


def main() -> int:
    with stepwise(CHECK_ID, CHECK_NAME):
        notes: list[str] = []

        # 1) List perp DEXes.
        print(f"POST {MAINNET_INFO_URL}  body={{'type':'perpDexs'}}")
        perp_dexs = _post(MAINNET_INFO_URL, {"type": "perpDexs"})
        print("perpDexs raw response (first 800 chars):")
        print(_pretty(perp_dexs))
        notes.append("perpDexs response captured")

        dex_names = _extract_dex_names(perp_dexs)
        print(f"\nDiscovered {len(dex_names)} perp DEX entries:")
        for n in dex_names:
            print(f"  - {n}")
        notes.append(f"perp DEX names: {dex_names!r}")

        # 2) Locate trade.xyz.
        tradexyz_dex = _pick_tradexyz_dex(dex_names)
        if tradexyz_dex is None:
            note = (
                "Could not locate a trade.xyz-like dex. "
                "Inspect the raw perpDexs payload above and update "
                "TRADEXYZ_NAME_CANDIDATES, then re-run."
            )
            print(note)
            notes.append(note)
            append_result(CHECK_ID, CHECK_NAME, "FAIL", notes=notes)
            return 1

        print(f"\nSelected trade.xyz dex: '{tradexyz_dex}'")
        notes.append(f"selected trade.xyz dex: '{tradexyz_dex}'")

        # 3) Fetch meta for that dex. Hyperliquid uses `perpDexMeta` for HIP-3.
        # If that endpoint is not available in your environment, the older
        # `meta` request with a `dex` field is tried as a fallback.
        meta: Any = None
        last_err: Exception | None = None
        for payload in (
            {"type": "perpDexMeta", "dex": tradexyz_dex},
            {"type": "meta", "dex": tradexyz_dex},
        ):
            try:
                print(f"\nPOST {MAINNET_INFO_URL}  body={payload}")
                meta = _post(MAINNET_INFO_URL, payload)
                print("meta response (first 800 chars):")
                print(_pretty(meta))
                notes.append(f"meta retrieved via payload={payload}")
                break
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                print(f"  failed: {type(exc).__name__}: {exc}")

        if meta is None:
            note = f"Could not retrieve meta for dex '{tradexyz_dex}': {last_err}"
            notes.append(note)
            append_result(CHECK_ID, CHECK_NAME, "FAIL", notes=notes)
            return 1

        # 4) Sanity-check we can see stock perps in the meta.
        symbols = _list_meta_symbols(meta)
        print(f"\n'{tradexyz_dex}' universe ({len(symbols)} symbols):")
        for s in symbols[:30]:
            print(f"  - {s}")
        if len(symbols) > 30:
            print(f"  ... ({len(symbols) - 30} more)")
        notes.append(f"{tradexyz_dex} universe size: {len(symbols)}")
        notes.append(f"first 30 symbols: {symbols[:30]!r}")

        # Symbols come back already prefixed (e.g. 'xyz:TSLA'). Compare by
        # the bare ticker after stripping the dex prefix.
        bare_symbols = {s.split(":", 1)[-1] for s in symbols}
        found_expected = [t for t in EXPECTED_STOCK_TICKERS if t in bare_symbols]
        print(f"\nExpected stock tickers found: {found_expected}")
        notes.append(f"expected stock tickers found: {found_expected}")

        if not found_expected:
            note = (
                f"No expected stock tickers ({EXPECTED_STOCK_TICKERS}) found "
                f"in dex '{tradexyz_dex}'. Inspect output above; this may "
                "simply mean trade.xyz currently lists a different universe."
            )
            print(note)
            notes.append(note)

        status = "PASS" if found_expected else "FAIL"
        append_result(CHECK_ID, CHECK_NAME, status, notes=notes)
        print(f"\n[{CHECK_ID}] {status}")
        # Persist the chosen dex name and symbols for Task E.
        out_path = Path(__file__).resolve().parents[2] / "docs" / "tradexyz_discovery.json"
        out_path.write_text(
            json.dumps(
                {
                    "dex": tradexyz_dex,
                    "all_dexes": dex_names,
                    "universe": symbols,
                    "expected_found": found_expected,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"Wrote discovery summary: {out_path}")
        return 0 if status == "PASS" else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception:
        sys.exit(1)
