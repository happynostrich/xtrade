"""Diagnostic: ping Binance testnet endpoints with the keys in `.env`.

Hits BOTH spot testnet and USDT-M futures testnet `account` endpoints
directly (no NautilusTrader involved) and prints the HTTP status.

Reports success/failure per venue. The futures testnet account endpoint
is the one Phase 0 needs for C2; spot is exercised only if a spot key
pair is also configured.

This script ONLY queries account info (read-only). It places no orders.
"""

from __future__ import annotations

import hashlib
import hmac
import sys
import time
import urllib.parse
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from xtrade.config import load_binance_testnet, mask  # noqa: E402


SPOT_BASE = "https://testnet.binance.vision"
FUT_BASE = "https://testnet.binancefuture.com"


def _signed_get(base: str, path: str, key: str, secret: str) -> tuple[int, str]:
    ts = int(time.time() * 1000)
    qs = urllib.parse.urlencode({"timestamp": ts, "recvWindow": 5000})
    sig = hmac.new(secret.encode(), qs.encode(), hashlib.sha256).hexdigest()
    url = f"{base}{path}?{qs}&signature={sig}"
    headers = {"X-MBX-APIKEY": key}
    with httpx.Client(timeout=15.0) as client:
        r = client.get(url, headers=headers)
    return r.status_code, r.text[:500]


def check(label: str, base: str, path: str, key: str | None, secret: str | None) -> bool:
    print(f"\n== {label} ==")
    if not key or not secret:
        print("  no credentials set; skipping")
        return False
    print(f"  base : {base}")
    print(f"  key  : {mask(key)}")
    print(f"  path : {path}")
    try:
        status, body = _signed_get(base, path, key, secret)
    except Exception as exc:  # noqa: BLE001
        print(f"  ERROR: {type(exc).__name__}: {exc}")
        return False
    print(f"  HTTP {status}")
    print(f"  body (first 500 chars): {body}")
    return status == 200


def main() -> int:
    creds = load_binance_testnet()
    spot_ok = check(
        "Binance Spot Testnet (/api/v3/account)",
        SPOT_BASE, "/api/v3/account",
        creds.spot_api_key, creds.spot_api_secret,
    )
    fut_ok = check(
        "Binance USDT-M Futures Testnet (/fapi/v2/account)",
        FUT_BASE, "/fapi/v2/account",
        creds.futures_api_key, creds.futures_api_secret,
    )
    print("\nSummary:")
    print(f"  spot:    {'OK' if spot_ok else 'FAIL/SKIP'}")
    print(f"  futures: {'OK' if fut_ok else 'FAIL/SKIP'}")
    return 0 if fut_ok else 1


if __name__ == "__main__":
    sys.exit(main())
