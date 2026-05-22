# Phase 3 testnet runbook — signal → strategy → testnet (T6)

This runbook is the **manual verification artefact for Phase 3 Task 6**.
It walks an operator through one end-to-end signal → strategy → risk →
approval → testnet limit-and-cancel run on a real exchange testnet
(Binance USDT-M Futures by default), using exactly the same code path
production would use.

The end-to-end test is **deliberately manual** — Phase 3's brief §3
forbids automated network calls in `pytest`. Automated tests cover the
orchestration up to the venue hop (`tests/test_live_signal_runner.py`,
`tests/test_cli_live_signal.py`); the venue hop itself is exercised
here.

> **Mainnet refusal.** `run_live_signal` reuses Phase 1's
> `xtrade.live.runner.run_live`, which refuses any venues yaml whose
> `testnet:` block is `false`. There is no flag to override; this
> runbook will not place an order on mainnet.

---

## 0. Prerequisites

1. A populated `config/venues.testnet.yaml` with at least one venue
   (default: `binance` USDT-M futures testnet) and `testnet: true`.
2. `BINANCE_TESTNET_API_KEY` / `BINANCE_TESTNET_API_SECRET` (or
   whichever env vars the yaml references) exported in the shell.
3. A small balance on the testnet sub-account that owns the API key.
   The default `safety_multiplier=0.7` places a BUY at 30 % below the
   bid, so the order parks rather than fills, but the venue still
   reserves the notional.
4. (Optional) `data/risk.yaml` if you want non-default risk caps. See
   `src/xtrade/risk/rules.py` for the schema.

```bash
export BINANCE_TESTNET_API_KEY=...
export BINANCE_TESTNET_API_SECRET=...
xtrade live healthcheck --instrument BTCUSDT-PERP.BINANCE
```

`live healthcheck` is the cheapest way to confirm credentials + network
before spending a signal.

---

## 1. Seed one signal

Two options.

### 1a. Real scanner (preferred)

Run any Phase 2 scanner over a recent catalog window so a fresh signal
lands in `data/signals/`:

```bash
xtrade scan run \
  --universe config/universe.example.yaml \
  --scanner momentum \
  --bar 1m \
  --since 2026-05-22 \
  --until 2026-05-22 \
  --queue-root data/signals
```

Check what landed:

```bash
xtrade scan list --queue-root data/signals --tail 5
```

### 1b. Hand-built signal (CI / smoke)

For a deterministic dry-run with no scanner dependency, drop a one-line
jsonl file under `data/signals/`:

```bash
python - <<'PY'
import datetime as dt, json, pathlib
sig = {
  "symbol": "BTCUSDT-PERP.BINANCE", "venue": "binance",
  "direction": "LONG", "strength": 0.6,
  "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
  "source": "manual:runbook", "valid_until": None,
  "metadata": {"last_price": "50000"},
}
day = dt.datetime.now(dt.timezone.utc).date().isoformat()
path = pathlib.Path("data/signals") / f"{day}.jsonl"
path.parent.mkdir(parents=True, exist_ok=True)
path.open("a").write(json.dumps(sig) + "\n")
print("wrote", path)
PY
```

---

## 2. Dry-run first

Verify the strategy emits an intent and the gate logic agrees before
spending an exchange round-trip:

```bash
xtrade live signal-run \
  --strategy momentum_follow \
  --instrument BTCUSDT-PERP.BINANCE \
  --signals-from data/signals \
  --mode dry_run
```

Expected output (one block):

```
run_id:        live-signal-...
strategy:      momentum_follow
instrument:    BTCUSDT-PERP.BINANCE
approval_mode: dry_run
signal:        BTCUSDT-PERP.BINANCE LONG strength=0.6 @ ...
intent:        BUY 0.002 BTCUSDT-PERP.BINANCE (MARKET)
approval:      <16-hex> status=dry_run mode=dry_run go=False
summary:       logs/<run_id>/live_signal_summary.json
note:          dry_run: intent recorded but not submitted
dry_run: intent recorded, no venue submission.
```

`logs/<run_id>/live_signal_summary.json` should contain the full intent
and `passed: false`, `approval.status: "dry_run"`. **Nothing has been
sent to the exchange.**

If `intent.quantity` looks wrong (zero, way too large), stop here and
adjust strategy config / risk caps — do not proceed to manual mode.

---

## 3. Manual-mode run (the actual testnet hop)

This is the verification step that earns the Task 6 sign-off.

In **terminal A**:

```bash
xtrade live signal-run \
  --strategy momentum_follow \
  --instrument BTCUSDT-PERP.BINANCE \
  --signals-from data/signals \
  --mode manual \
  --venues-yaml config/venues.testnet.yaml \
  --safety-multiplier 0.7 \
  --venue-timeout 60 \
  --approval-timeout 600
```

The command will:
1. Pull the newest matching signal.
2. Run `momentum_follow.on_signal(...)` → one `OrderIntent`.
3. Pass it through `RiskGate` (rules from `--risk-config` if given, else
   the empty default — add caps for real runs).
4. Write a `pending` row to `data/approvals/` and **block silently,
   polling the approval queue every 2 s** (default `--poll-interval`)
   until the row's status flips or `--approval-timeout` expires.

   Terminal A produces no further output while waiting. To see the
   pending row, use terminal B.

In **terminal B**:

```bash
xtrade approve list --status pending
xtrade approve confirm <id>      # release, OR
xtrade approve reject  <id> --reason "manual veto"
```

Back in terminal A, on `confirm`:

5. The runner delegates to Phase 1's `run_live(...)`, which:
   - Spins a `TradingNode` against the testnet config.
   - Places one far-from-market GTC limit
     (`safety_multiplier × bid` for BUY).
   - Awaits `OrderAccepted` (testnet ack).
   - Issues `cancel_order` and awaits `OrderCanceled`.
   - Disposes the node.
6. Final block prints `live signal-run PASSED.` and the venue summary
   embeds inside `logs/<run_id>/live_signal_summary.json` under
   `live_summary`.

On `reject`, the runner exits with code 1 and the summary records
`approval.status = "rejected"` — no exchange call is made.

---

## 4. Verification checklist

After step 3, confirm all six:

- [ ] Terminal A printed `live signal-run PASSED.`
- [ ] `logs/<run_id>/live_signal_summary.json` exists and contains:
      - `"passed": true`
      - `"approval": { "status": "confirmed", "go": true, ... }`
      - `"live_summary": { "order": { "accepted": true, "canceled": true, ... }, ... }`
- [ ] `logs/<run_id>/<run_id>.venue/summary.json` (the Phase 1 inner
      summary) exists and shows `"order": {"accepted": true, "canceled":
      true, "rejected": false}`.
- [ ] Testnet UI (or `GET /fapi/v1/openOrders`) shows the order as
      `CANCELED`, not `FILLED`.
- [ ] `xtrade approve list --status confirmed` shows the row whose
      `record_id` matches `summary["approval"]["record_id"]`.
- [ ] No matching `pending` rows remain
      (`xtrade approve list --status pending` reports none for this id).

If any box is empty, capture `logs/<run_id>/run.log` and
`live_signal_summary.json` and file an issue rather than retrying — the
failure mode is the signal.

---

## 5. Common failure modes

| symptom | likely cause | fix |
| --- | --- | --- |
| `error: --venues-yaml ... refused (mainnet)` | yaml has `testnet: false` | flip to `testnet: true`, or use the supplied testnet yaml |
| `error: missing env var BINANCE_TESTNET_API_KEY` | shell didn't export creds | `export ...` and re-run |
| `error: no signals match instrument ...` | no matching signal in queue, or instrument mismatch | re-run scan, or fix `--instrument` to match `signal.symbol` |
| `error: strategy 'momentum_follow' emitted no intents` | account snapshot missing mark (signal has no `metadata.last_price`) | re-emit signal with `metadata.last_price` set, or use a strategy that doesn't need a mark |
| `live signal-run FAILED: intent blocked by RiskGate: ...` | risk rule cap too tight | adjust `--risk-config` |
| `live signal-run FAILED: approval ... rejected (status=rejected)` | operator chose reject | intentional; check audit trail in `data/approvals/` |
| `live signal-run FAILED: manual approval ... did not complete within Ns` | timeout fired before operator confirmed | re-run with `--approval-timeout` raised, or confirm faster |
| venue order shows as `FILLED` | testnet price moved into your limit during the run | re-run; `safety_multiplier=0.7` is conservative but not bulletproof on illiquid pairs |

---

## 6. Artefacts to keep

For each successful Task 6 run, archive:

```
logs/<run_id>/
  ├── run.log                       # Nautilus log
  ├── config.snapshot.yaml          # venues yaml at run time
  ├── live_signal_summary.json      # Phase 3 outer summary
  └── <run_id>.venue/
        └── summary.json            # Phase 1 inner summary
data/approvals/<YYYY-MM-DD>.jsonl   # the approval row(s) for this run
data/signals/<YYYY-MM-DD>.jsonl     # the signal that drove it
```

These four together let any reviewer reconstruct exactly which signal
fired, what intent the strategy emitted, what an operator approved,
and what the exchange acknowledged — the full audit chain Phase 3
exists to deliver.
