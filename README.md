# xtrade

Automated multi-venue trading research project.

> **Current stage: Phase 0 — Foundation Validation**
> The goal of this phase is *only* to verify the technical foundation
> (NautilusTrader as engine; Binance + trade.xyz/Hyperliquid as venues)
> and produce a clear **go / no-go** decision. **No real trading strategy
> and no real-money order placement happens in Phase 0.**

## Venues

- **Binance** — centralized exchange. Crypto spot + USD-M perpetuals
  (including some "US-equity-style" perps like MSTR/COIN).
- **trade.xyz** — first HIP-3 builder-deployed perp DEX on **Hyperliquid**.
  Offers stock perps (TSLA, NVDA, ...), commodity perps, index perps,
  cash-settled and funded by funding rate. Technically reached via the
  Hyperliquid API; symbols are namespaced as `dex:symbol` (e.g. `xyz:TSLA`).

## Engine

[NautilusTrader](https://nautilustrader.io/) — Rust core + Python API
multi-asset trading engine. Same strategy code runs in backtest and live.
Has built-in adapters for both Binance and Hyperliquid.

## Layout

```
xtrade/
├── .env.example            # required env vars (no real values)
├── pyproject.toml
├── config/                 # venue configs (examples only)
├── scripts/phase0/         # Phase 0 verification scripts (8 of them)
├── src/xtrade/             # shared code (config loader, helpers)
├── tests/
└── docs/
    └── phase0_results.md   # filled in as Phase 0 progresses
```

## Quick start

1. Install Python 3.12 (64-bit).
2. Create a virtual environment, e.g. with `uv`:
   ```bash
   uv venv --python 3.12
   source .venv/bin/activate
   uv pip install -e .
   ```
   or with stdlib `venv`:
   ```bash
   python3.12 -m venv .venv
   source .venv/bin/activate
   pip install -U pip
   pip install -e .
   ```
3. Copy `.env.example` to `.env` and fill in the credentials you generated
   yourself (see "Prerequisites" below). **Never commit `.env`.**
4. Run Phase 0 scripts in the recommended order (see `docs/phase0_results.md`).

## Prerequisites you must do yourself

These steps involve real accounts / private keys and **must not** be done
by an AI agent:

1. Create **Binance Testnet** accounts (spot + futures) and generate
   API key/secret in each. Put them in `.env`.
2. Create a **dedicated Hyperliquid Testnet wallet** (separate from any
   wallet you use for real funds), fund it from the testnet faucet,
   generate an API/agent wallet on Hyperliquid testnet, and put the
   account address + API wallet private key in `.env`.

Mainnet read-only checks (C4 / C5) require no credentials.

## Safety rules (Phase 0)

- Orders are only placed on **testnet**.
- Mainnet checks are **read-only** market data only.
- Secrets are loaded from `.env` via `src/xtrade/config.py`.
  They are never logged in cleartext, never committed, never hardcoded.
- The Hyperliquid testnet wallet must be **separate** from any wallet
  holding real assets.
