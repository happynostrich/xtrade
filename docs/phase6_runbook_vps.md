# Phase 6 mainnet VPS runbook — SPCXUSDT-PERP holding period

This runbook is the **operational manual for the Phase 6 SPCXUSDT-PERP
short-mcap-anchored ladder** running on a single VPS against Binance
USDS-M Futures **mainnet**.

It exists to make the holding period — from first fill through final
close — a checklist instead of a judgement call. Every section ends
with the journald / file artefacts an operator should be able to see
when the system is healthy, plus the rollback step when it isn't.

> **Scope.** This document covers the per-instrument operational
> surface for the **first** instance of the mcap-anchored-ladder family
> (SPCXUSDT pre-IPO short). The strategy, scanner, and infra layers
> were designed direction-/boundary-parameterized so the next instance
> (different ticker, possibly long bias) should not need any new code —
> only a fresh yaml + an `instrument_meta` row. The numbered sections
> below stay generic where possible; SPCX-specific values appear under
> "SPCXUSDT-PERP specifics" callouts.

Brief reference: `docs/phase6_brief.md` §5 T11 (this runbook is delivered
as part of Task T11).

---

## 1. Preflight — before `systemctl start`

Goal: prove the VPS is in a state where it is *safe* to flip the
supervisor on. If any step below fails, do not start the service.

Driver: `scripts/phase6/02_preflight_mainnet.sh` (TODO — wraps the
checks below). Until that script exists, walk the list by hand.

1. **Three locks present** (Phase 6 brief gating prereqs):
   - Phase 4 §1.6 / §1.7 mainnet-readiness signoff present on disk.
   - Phase 5 Track A VPS rerun signoff present on disk.
   - Operator signoff on `docs/phase6_results.md` preflight block.

2. **Risk caps tight** — confirm `data/risk.yaml`:
   - `daily_loss_cap_usd` ≤ 100
   - `per_instrument_margin_cap_usd` ≤ 200
   - `max_leverage` = 1
   - `auto_pause_on_breach` = true

3. **`instrument_meta` fresh** — `config/instrument_meta.yaml`:
   - `SPCXUSDT-PERP.BINANCE.shares_outstanding` within 5 % of the
     value Binance's pre-IPO oracle publishes today (scanner will
     otherwise raise `InstrumentMetaStaleError`).
   - `mark_source: oracle` (flip to `spot_anchor` only after IPO).
   - `tick_size` / `min_qty` / `qty_step` match
     `GET /fapi/v1/exchangeInfo?symbol=SPCXUSDT`.

4. **Venue connectivity ping**
   ```bash
   xtrade live health \
     --instrument SPCXUSDT-PERP.BINANCE \
     --venues-yaml /etc/xtrade/venues.mainnet.yaml
   ```
   Expect `status: ok` for both the REST ping and the websocket probe.

5. **Mainnet API key permissions** — log into Binance UI and confirm:
   - "Enable Futures" **on**.
   - "Enable Withdrawals" **off** (mandatory — this is the blast-radius
     boundary; if it is on, halt the start).
   - IP whitelist contains the VPS public IP **only**.
   - `recvWindow` default (5000 ms) — no client clock skew warning in
     `/fapi/v1/time` round-trip.

6. **Alert channel dry-run** — push one explicit test alert through
   the configured `XTRADE_ALERT_*` outbound channel and confirm the
   operator's openclaw / yuanbao endpoint received it. The
   end-to-end SLA is "operator notices a `crit` alert within 6 h".

7. **Disk free** — at least 5 GiB free on `/var/lib/xtrade/`; journald
   `SystemMaxUse=2G`.

Artefacts after preflight pass:
- `/var/lib/xtrade/preflight.<DATE>.ok` (touch file written by
  the preflight script).
- A green checkmark line in the operator's pre-start log.

---

## 2. Startup — `systemctl start xtrade-supervisor.service`

Once preflight is green:

```bash
sudo systemctl start xtrade-supervisor.service
journalctl -u xtrade-supervisor -f --since '1 min ago'
```

Within ≤ 30 s the journal should show, in order:

1. `mainnet.unlock.ok` — supervisor confirmed the three locks.
2. `supervisor.start` — child processes spawning.
3. `scanner.threshold_ladder.start` with
   `instance=spcxusdt_threshold_ladder`.
4. `strategy.mcap_anchored_ladder.start` (instance =
   `spcxusdt_short_mcap`).
5. `drawdown.watcher.start`, `mcap_softkill.watcher.start`,
   `alerter.up`.
6. One severity=`info` alert "supervisor up" reaches openclaw.

Failure modes and rollback:
- If any of the lines above is missing within 30 s →
  `systemctl stop xtrade-supervisor.service` immediately, capture
  `journalctl -u xtrade-supervisor -n 200`, do not re-start.
- If `mainnet.unlock.refused` appears, the three-lock check failed —
  fix the failing lock and rerun preflight.
- If a child process exits non-zero, the supervisor will mark the unit
  failed within ~10 s. No partial state is left on the venue because
  scanner / strategy don't place orders until they have completed
  startup.

---

## 3. Semi-auto approval — scanner ↔ operator ↔ ApprovalGate

Order placement on mainnet is **always** behind the human approval
gate. End-to-end flow:

1. **Scanner** detects price ∈ entry band, mcap below soft-kill
   trigger; emits an `OrderIntent` envelope to `bridge_out`.
2. **bridge_out** → yuanbao client → push notification on the
   operator's openclaw device.
3. **Operator** reviews the envelope (instrument, side, qty,
   limit_px, intended ladder rung, expiry) and either:
   - Confirms in the openclaw UI → `bridge_in` pushes
     `OrderIntentApproval` back to the VPS.
   - Rejects / ignores → envelope expires after `ttl_sec` (default
     900 s = 15 min) and is purged.
4. **ApprovalGate** matches the approval id back to the pending
   intent and forwards to the strategy.
5. **Strategy** emits the child limit order to Binance.

Timeout and reject paths are identical to Phase 4 §3 (signal-run
testnet runbook); the only thing new on mainnet is that
ApprovalGate refuses any intent whose `risk.account_type` is not
`USDT_FUTURE` and whose venue's `environment` is not `LIVE`.

Operator decision rules:
- **Approve** only if the limit_px is inside the envelope's quoted
  band ± 0.2 % (slippage tolerance) and the size matches the rung
  table from `config/strategy.spcxusdt_short_mcap.yaml`.
- **Reject** if anything in the envelope feels off — there are
  enough rungs that missing one is recoverable.

---

## 4. Inspection cadence — every 1 h status, every 4 h reconciliation

### Every 1 h (operator runs this from openclaw or via SSH):

```bash
xtrade ops status --json | jq '{
  paused, disk, drawdown, bridge, counts, mcap_softkill
}'
```

Expected health signal:
- `paused.flag` is **absent** (no Sentinel on disk).
- `disk.free_gib` > 2.
- `drawdown.halted` is false and `drawdown_pct` < 0.05.
- `bridge.outbound_lag_s` < 60 and `bridge.last_approval_age_s`
  matches "envelopes you approved".
- `counts.open_orders` matches the TP ladder snapshot (1 entry +
  open rungs).
- `mcap_softkill.headroom_pct` > 0.10 (10 % off the trigger).

Any single one of those going negative → bump to every 15 min.

### Every 4 h — venue-side three-way reconciliation:

Compare three sources of truth for the same instrument:
1. Binance UI: position card (size, entry, unrealized).
2. Binance UI: open orders panel.
3. `xtrade ops holding_report <today> --skip-alert` then
   `cat reports/phase6/holding_<today>.json | jq` (T11 surface).

`pos_size`, `avg_entry_usd`, and TP ladder rung qty must match
between (1)+(2) and (3). Discrepancy > 0.001 in qty or > 0.5 % in
avg_entry → halt the system (`xtrade ops emergency_close --side
all --yes`), reconcile manually, file an incident note before
restarting.

### Every 24 h (cron):

`scripts/phase6/01_daily_holding_report.sh` runs at 00:05 UTC.
Output ends up at `/var/lib/xtrade/reports/phase6/holding_<date>.json`
and an `info` alert lands on openclaw with the four-key summary
`{avg_entry, current_mark, unrealized_pnl, soft_kill_headroom_pct}`.

---

## 5. Alert tier response

| Severity | Examples | Operator action |
|----------|----------|-----------------|
| `info` | Daily holding report; rung filled; scanner heartbeat. | Log only. No action. |
| `warn` | Bridge round-trip > 60 s; one rejected order; clock skew > 100 ms. | Note in journal. Continue. Re-check next 1 h cycle. |
| `crit` | venue 401 / 403; soft-kill triggered; drawdown halt; emergency_close invoked. | Immediately evaluate whether to run `xtrade ops emergency_close --side all --yes`. Decision window: 30 min. |

`crit` alerts have a 6 h SLA on the operator side: if a `crit` is not
acknowledged in the openclaw UI within 6 h, that is a Phase 6.4
infrastructure failure (the alerting channel is not delivering) and
the result column flips to NOT PASS regardless of trading P&L.

Auto-triggered `crit` events that pause the system always also write
the Sentinel file `/var/lib/xtrade/paused.flag` containing
`{reason, ts}`. While the Sentinel is on disk, the strategy will
refuse to place new orders; the operator removes it manually after
the post-incident review.

---

## 6. Drawdown halt handling

`DrawdownWatcher` (T7) tracks a high-water-mark and halts the
strategy when `(hwm - equity)/hwm > halt_pct` (default 5 %).

**This will fire at least once.** SPCXUSDT pre-IPO is high-vol; a 5 %
hwm-from-equity drop is well inside one-day move.

When you see `drawdown.halt` in journald + a `crit` alert:

1. Pull the holding report on demand:
   ```bash
   xtrade ops holding_report $(date -u +%F) \
     --current-mark <bind-to-current-mark>
   ```
2. Decide which of three buckets you're in:
   - **Thesis still valid + sizing fine**: just normal vol. The halt
     auto-resumes when equity recovers above `hwm * (1 - halt_pct)`.
     Do nothing.
   - **Mark broke through $225 (historical high)**: thesis is at risk.
     Re-evaluate position size; consider scaling out one rung
     manually via the venue UI.
   - **Sizing too large**: revise `strategy.spcxusdt_short_mcap.yaml`
     downward and restart (after closing the over-sized leg).
3. Do **not** flip the Sentinel off prematurely — the watcher will
   re-allow trading on its own once equity recovers. Manual unhalt
   is for incident response, not normal halt.

Halt frequency rules of thumb:
- 1–2 halts over the holding period: expected.
- ≥ 3 halts: review `halt_pct` (it may be too tight for SPCXUSDT
  vol). 8–10 % is acceptable if you keep the daily-loss cap intact.

---

## 7. Soft-kill handling

`McapSoftKillWatcher` (T8) trips when `current_mcap_usd` crosses the
`soft_kill.trigger_mcap_usd` boundary in the *adverse* direction:

- SPCXUSDT short bias → `boundary=above`, trigger
  $3.5 T (`current_mark ≈ $294.86`).
- Long-bias future instances will use `boundary=below`.

On trigger the watcher automatically:
1. Invokes `xtrade ops emergency_close --side reduce-only-tp-only --yes`
   — cancels open TP rungs to stop the strategy from accidentally
   eating into a now-extended ladder.
2. Writes the Sentinel `/var/lib/xtrade/paused.flag` with
   `reason=mcap.softkill.triggered:SPCXUSDT-PERP.BINANCE`.
3. Dispatches a `crit` alert.

The position itself is **not** force-closed. The operator decides
within 30 min:
- **Hold** (accept the open exposure, wait for mean-reversion below
  the trigger). Document the rationale in the journal.
- **Manual market-close** at the venue. Not recommended — slippage on
  pre-IPO oracle mark can be very large. Only consider if there is
  reason to believe mcap will keep extending.
- **Add to the short** at the trigger boundary. Strongly not
  recommended — the thesis edge is gone above the trigger.

Once decided, follow the decision matrix in §7 of `phase6_brief.md`
to decide PASS / NOT PASS status. Remove the Sentinel only after the
incident write-up is filed.

---

## 8. IPO event night watch

The mark-source switch (oracle → spot anchor) on IPO confirmation is
the single highest-risk operational event in the holding period. The
runbook treats `IPO day ± 24 h` as an enhanced-cadence window:

1. **T-24 h**: switch the daily holding-report cron to **hourly**:
   set `XTRADE_DATE` and run
   `scripts/phase6/01_daily_holding_report.sh` from a 1 h cron
   (override the default 24 h schedule).
2. **Inspection cadence**: every 15 min instead of every 1 h. Pay
   particular attention to `mcap_softkill.headroom_pct` — gap-up
   above $294.86 = soft-kill trip.
3. **At the mark-switch instant** (Binance posts an announcement):
   - Watch for mcap step-change in the next status snapshot.
   - Watch open-orders panel for venue-initiated cancel/replace (the
     venue's behaviour on perpetual contract spec changes is **not
     documented in advance**; treat anomalies as venue events and
     reconcile manually).
   - Hand-edit `config/instrument_meta.yaml` to set
     `mark_source: spot_anchor`; restart the supervisor:
     ```bash
     sudo systemctl restart xtrade-supervisor.service
     ```
4. **T+1 h after switch**: run the holding report once more on
   demand and snapshot it under `reports/phase6/ipo_event/` for
   audit.
5. **T+24 h**: revert to standard 1 h / 4 h / 24 h cadence.

If the venue performs a force-settle / re-pricing during the switch
(see §7 decision matrix bottom row), document it as a venue
behaviour incident; the strategy result is judged separately.

---

## 9. Wind-down — three exit paths

The holding period ends on exactly one of:

### 9a. All TP rungs filled (ideal path)

1. Strategy auto-detects `open_qty == 0` after the last TP fill.
2. The supervisor remains running (so the daily report continues),
   but no new orders are placed (no signal until a new entry band).
3. Operator runs final holding_report manually for the post-mortem.
4. Reconcile: cumulative `realized_pnl_usd` from holding_report ==
   Binance's "Closed Position" PnL ± 0.01 (rounding only).
5. `sudo systemctl stop xtrade-supervisor.service`.
6. Fill `docs/phase6_results.md` with the four-evidence-bucket
   PASS path.

### 9b. Operator manual close (early but voluntary)

1. Operator runs `xtrade ops emergency_close --side all --yes`
   from the VPS shell. This:
   - Cancels every open order.
   - Writes the Sentinel.
   - Dispatches a `crit` alert acknowledging the manual stop.
2. Then closes the remaining position **manually at the venue UI**
   (market or limit; market is acceptable on the close-out leg).
3. Run a final holding_report; reconcile; stop supervisor; fill
   `phase6_results.md` with the conservative-PASS path.

### 9c. Stop-loss accepted (failure path)

If cumulative loss exceeds $100 (half-margin), or any of the
NOT-PASS triggers in §7 fires:

1. `xtrade ops emergency_close --side all --yes`.
2. Close any remaining leg market-at-venue.
3. `sudo systemctl stop xtrade-supervisor.service`.
4. Write the incident report into
   `docs/phase6_results.md` (failure path), including:
   - Final P&L.
   - Soft-kill / drawdown halt timestamps (from journald).
   - Whether the alert channel met its 6 h SLA.
   - Recommendation: rerun on testnet vs reset and try a different
     instrument vs Phase 6.x re-scope.
5. Do **not** restart the supervisor on the failed instance. Move
   to the next instrument under a fresh brief.

Common reconciliation step for all three paths:
- Final P&L from holding_report = `realized_pnl_usd +
  unrealized_pnl_usd` (which should be 0 once closed) -
  `funding_paid_cumulative_usd`.
- Compare to Binance "Transaction History" → "Realized PnL" minus
  funding fees, on the same instrument, over the same window. Any
  delta > $1 is an investigation item.

---

## Appendix — useful one-liners

```bash
# Snapshot current TP ladder state and open orders for a manual diff.
xtrade ops status --json | jq '.tp_ladder, .open_orders'

# Tail every crit / warn alert since boot.
journalctl -u xtrade-supervisor --since boot \
  | grep -E '"severity":"(warn|crit)"'

# Stop trading without closing the position (for emergency triage).
xtrade ops emergency_close --side reduce-only-tp-only --yes

# Force a holding report right now with a manually-entered mark.
XTRADE_CURRENT_MARK_USD=215.50 \
  /usr/local/lib/xtrade/scripts/phase6/01_daily_holding_report.sh

# Verify the Sentinel reason after an auto-pause.
cat /var/lib/xtrade/paused.flag | jq
```

## Appendix — file map

| Path | Owner | Purpose |
|------|-------|---------|
| `/etc/xtrade/env` | root:xtrade 0640 | Mainnet API key / alert tokens |
| `/etc/xtrade/venues.mainnet.yaml` | root:xtrade 0644 | Mainnet venues config |
| `/etc/xtrade/instrument_meta.yaml` | root:xtrade 0644 | T3 instrument facts |
| `/var/lib/xtrade/state/fills.jsonl` | xtrade 0640 | Append-only fill journal |
| `/var/lib/xtrade/state/tp_ladder.json` | xtrade 0640 | T5 TP ladder snapshot |
| `/var/lib/xtrade/state/drawdown.json` | xtrade 0640 | T7 watcher state |
| `/var/lib/xtrade/paused.flag` | xtrade 0640 | T2 Sentinel (only present when paused) |
| `/var/lib/xtrade/reports/phase6/holding_<DATE>.json` | xtrade 0640 | T11 daily snapshot |
| `journalctl -u xtrade-supervisor` | systemd | All structured events |

---

*This runbook is intentionally framework-generic where possible. The
next mcap-anchored-ladder instance (different ticker, possibly long
bias) should reuse §§1–9 unchanged; only the "SPCXUSDT-PERP
specifics" callouts and the configured trigger values change. The
`docs/phase6_results.md` evidence-bucket template is per-instance.*
