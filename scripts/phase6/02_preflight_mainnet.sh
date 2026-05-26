#!/usr/bin/env bash
# 02_preflight_mainnet.sh — Phase 6 mainnet startup gate.
#
# Mechanises every check from docs/phase6_runbook_vps.md §1 that can be
# done without operator-eyes-on-Binance-UI. Run on the VPS as the
# operator user (NOT root — the supervisor itself runs as `xtrade`).
#
# Exit codes:
#   0  every check PASS — safe to `systemctl start xtrade-supervisor`.
#   2  any check FAIL — DO NOT start the supervisor.
#
# Manual checks that the operator MUST do separately (see runbook §1
# steps 5+6): Binance UI key-permission audit, IP whitelist, alert
# end-to-end confirm. This script prints reminders but cannot verify
# them itself.
#
# Inputs come from env (sourced from /etc/xtrade/env on the VPS):
#   XTRADE_INSTRUMENT                  default SPCXUSDT-PERP.BINANCE
#   XTRADE_VENUES_YAML                 default /etc/xtrade/venues.binance_futures.mainnet.yaml
#   XTRADE_RISK_YAML                   default /etc/xtrade/risk.mainnet.yaml
#   XTRADE_META_YAML                   default /etc/xtrade/instrument_meta.yaml
#   XTRADE_UNLOCK_FILE                 default /etc/xtrade/mainnet_unlock
#   XTRADE_SIGNOFF_DIR                 default /etc/xtrade/signoffs
#   XTRADE_VAR_ROOT                    default /var/lib/xtrade
#   XTRADE_MIN_FREE_GIB                default 5
#   XTRADE_MAINNET_UNLOCK_TOKEN        REQUIRED for §1 Lock 3 check
#   XTRADE_MAINNET_BINANCE_FUTURES_API_KEY     REQUIRED for §5 API check
#   XTRADE_MAINNET_BINANCE_FUTURES_API_SECRET  REQUIRED for §5 API check
#   OPENCLAW_GATEWAY + OPENCLAW_SHARED_SECRET  REQUIRED for §6 alert check
#   XTRADE_ALERT_CHANNEL=yuanbao               REQUIRED for §6 alert check
#   XTRADE_PREFLIGHT_SKIP_ALERT        set to "1" to skip §6 (use sparingly)
#
# Brief §5 T11 + runbook §1 reference:
#   1. Three locks present (mainnet_unlock file + env token + phase signoffs)
#   2. Risk caps tight (mainnet ceiling)
#   3. Instrument meta fresh + parseable
#   4. Venue REST/WS ping (delegates to `xtrade live health`)
#   5. Mainnet API key permissions (canTrade=true, canWithdraw=false)
#   6. Alert channel dry-run (push one severity=info event)
#   7. Disk free ≥ XTRADE_MIN_FREE_GIB
#
# NOT -e: we want every check to run and aggregate, not stop on first FAIL.
set -uo pipefail

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

XTRADE_INSTRUMENT="${XTRADE_INSTRUMENT:-SPCXUSDT-PERP.BINANCE}"
XTRADE_VENUES_YAML="${XTRADE_VENUES_YAML:-/etc/xtrade/venues.binance_futures.mainnet.yaml}"
XTRADE_RISK_YAML="${XTRADE_RISK_YAML:-/etc/xtrade/risk.mainnet.yaml}"
XTRADE_META_YAML="${XTRADE_META_YAML:-/etc/xtrade/instrument_meta.yaml}"
XTRADE_UNLOCK_FILE="${XTRADE_UNLOCK_FILE:-/etc/xtrade/mainnet_unlock}"
XTRADE_SIGNOFF_DIR="${XTRADE_SIGNOFF_DIR:-/etc/xtrade/signoffs}"
XTRADE_VAR_ROOT="${XTRADE_VAR_ROOT:-/var/lib/xtrade}"
XTRADE_MIN_FREE_GIB="${XTRADE_MIN_FREE_GIB:-5}"
XTRADE_PREFLIGHT_SKIP_ALERT="${XTRADE_PREFLIGHT_SKIP_ALERT:-0}"

PASS_COUNT=0
FAIL_COUNT=0
FAILS=()

# ---------------------------------------------------------------------------
# Reporting helpers
# ---------------------------------------------------------------------------

section() {
  echo
  echo "================================================================"
  echo "  $1"
  echo "================================================================"
}

step_pass() {
  echo "  [PASS] $1"
  PASS_COUNT=$((PASS_COUNT + 1))
}

step_fail() {
  echo "  [FAIL] $1"
  FAIL_COUNT=$((FAIL_COUNT + 1))
  FAILS+=("$1")
}

step_warn() {
  echo "  [WARN] $1"
}

step_info() {
  echo "  [info] $1"
}

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    step_fail "required command not on PATH: $1"
    return 1
  fi
  return 0
}

# ---------------------------------------------------------------------------
# Step 1 — three locks present
# ---------------------------------------------------------------------------

check_locks() {
  section "1/7  Three locks (runbook §1.1)"

  # 1a — Lock 3 file
  if [[ -f "$XTRADE_UNLOCK_FILE" ]]; then
    # `stat -c` on GNU, `stat -f` on BSD; try GNU first.
    local mode owner
    if mode=$(stat -c '%a' "$XTRADE_UNLOCK_FILE" 2>/dev/null); then
      owner=$(stat -c '%U:%G' "$XTRADE_UNLOCK_FILE" 2>/dev/null)
    else
      mode=$(stat -f '%Lp' "$XTRADE_UNLOCK_FILE" 2>/dev/null || echo "?")
      owner=$(stat -f '%Su:%Sg' "$XTRADE_UNLOCK_FILE" 2>/dev/null || echo "?:?")
    fi
    if [[ "$mode" == "400" ]]; then
      step_pass "unlock file $XTRADE_UNLOCK_FILE mode 0400 (owner $owner)"
    else
      step_fail "unlock file $XTRADE_UNLOCK_FILE mode $mode (expected 0400)"
    fi
    if [[ "$owner" == "root:root" ]]; then
      step_pass "unlock file owner root:root"
    else
      step_fail "unlock file owner $owner (expected root:root)"
    fi
  else
    step_fail "unlock file missing: $XTRADE_UNLOCK_FILE"
    step_info "create with: token=\$(openssl rand -hex 32); echo \"\$token\" | sudo tee $XTRADE_UNLOCK_FILE; sudo chown root:root $XTRADE_UNLOCK_FILE; sudo chmod 0400 $XTRADE_UNLOCK_FILE"
  fi

  # 1b — env token present (we cannot read the file from the operator
  # account; the supervisor itself does the byte-equality compare at
  # startup, so we only assert non-empty here).
  if [[ -n "${XTRADE_MAINNET_UNLOCK_TOKEN:-}" ]]; then
    step_pass "XTRADE_MAINNET_UNLOCK_TOKEN is set in env"
  else
    step_fail "XTRADE_MAINNET_UNLOCK_TOKEN is not set in env"
    step_info "supervisor compares this against the unlock file at startup"
  fi

  # 1c — phase signoffs (operator touches each file after paper review)
  local signoffs=(
    "phase4_section_1_6.signoff"
    "phase4_section_1_7.signoff"
    "phase5_track_a.signoff"
    "phase6_preflight.signoff"
  )
  for s in "${signoffs[@]}"; do
    if [[ -f "$XTRADE_SIGNOFF_DIR/$s" ]]; then
      step_pass "signoff present: $XTRADE_SIGNOFF_DIR/$s"
    else
      step_fail "signoff missing: $XTRADE_SIGNOFF_DIR/$s"
      step_info "create after operator review: sudo install -m 0644 -o root -g xtrade /dev/null $XTRADE_SIGNOFF_DIR/$s"
    fi
  done
}

# ---------------------------------------------------------------------------
# Step 2 — risk caps tight
# ---------------------------------------------------------------------------

check_risk_caps() {
  section "2/7  Risk caps tight (runbook §1.2)"

  if [[ ! -f "$XTRADE_RISK_YAML" ]]; then
    step_fail "risk yaml missing: $XTRADE_RISK_YAML"
    return
  fi

  python3 - "$XTRADE_RISK_YAML" <<'PY'
import sys
from pathlib import Path

path = Path(sys.argv[1])
try:
    from xtrade.risk import (
        assert_mainnet_risk_ceiling,
        load_rules_from_yaml,
    )
    rules = tuple(load_rules_from_yaml(path))
    assert_mainnet_risk_ceiling(rules)
except Exception as exc:
    print(f"FAIL: {exc}")
    sys.exit(1)
print(f"OK: {len(rules)} rules within mainnet ceiling")
PY
  if [[ $? -eq 0 ]]; then
    step_pass "$XTRADE_RISK_YAML satisfies mainnet ceiling"
  else
    step_fail "$XTRADE_RISK_YAML fails mainnet ceiling"
  fi
}

# ---------------------------------------------------------------------------
# Step 3 — instrument metadata
# ---------------------------------------------------------------------------

check_meta() {
  section "3/7  Instrument meta fresh (runbook §1.3)"

  if [[ ! -f "$XTRADE_META_YAML" ]]; then
    step_fail "meta yaml missing: $XTRADE_META_YAML"
    return
  fi

  python3 - "$XTRADE_META_YAML" "$XTRADE_INSTRUMENT" <<'PY'
import sys
from pathlib import Path

meta_path = Path(sys.argv[1])
symbol = sys.argv[2]
try:
    from xtrade.instruments.meta import MetaRegistry
    reg = MetaRegistry.load(meta_path)
    meta = reg.get(symbol)
except Exception as exc:
    print(f"FAIL: {exc}")
    sys.exit(1)
print(
    f"OK: {symbol} shares={meta.shares_outstanding} "
    f"min_qty={meta.min_qty} tick={meta.tick_size} "
    f"mark_source={meta.mark_source}"
)
PY
  if [[ $? -eq 0 ]]; then
    step_pass "instrument meta loads for $XTRADE_INSTRUMENT"
  else
    step_fail "instrument meta load failed for $XTRADE_INSTRUMENT"
  fi

  step_warn "scanner-side 5% shares-outstanding cross-check runs at scanner start, NOT here"
}

# ---------------------------------------------------------------------------
# Step 4 — venue connectivity ping
# ---------------------------------------------------------------------------

check_venue_ping() {
  section "4/7  Venue connectivity ping (runbook §1.4)"

  require_cmd xtrade || return

  if [[ ! -f "$XTRADE_VENUES_YAML" ]]; then
    step_fail "venues yaml missing: $XTRADE_VENUES_YAML"
    return
  fi

  if xtrade live health \
      --instrument "$XTRADE_INSTRUMENT" \
      --venues-yaml "$XTRADE_VENUES_YAML" >/dev/null 2>&1; then
    step_pass "xtrade live health OK on $XTRADE_INSTRUMENT"
  else
    step_fail "xtrade live health FAILED on $XTRADE_INSTRUMENT"
    step_info "rerun without redirect to see the full diagnostic"
  fi
}

# ---------------------------------------------------------------------------
# Step 5 — mainnet API key permissions (canTrade=true, canWithdraw=false)
# ---------------------------------------------------------------------------

check_api_key_permissions() {
  section "5/7  Mainnet API key permissions (runbook §1.5)"

  if [[ -z "${XTRADE_MAINNET_BINANCE_FUTURES_API_KEY:-}" \
     || -z "${XTRADE_MAINNET_BINANCE_FUTURES_API_SECRET:-}" ]]; then
    step_fail "XTRADE_MAINNET_BINANCE_FUTURES_API_KEY / _API_SECRET not set"
    return
  fi

  python3 - <<'PY'
import os
import sys
import time
import hmac
import hashlib
import urllib.parse
import urllib.request

api_key = os.environ["XTRADE_MAINNET_BINANCE_FUTURES_API_KEY"]
api_secret = os.environ["XTRADE_MAINNET_BINANCE_FUTURES_API_SECRET"]

ts = int(time.time() * 1000)
qs = urllib.parse.urlencode({"recvWindow": 5000, "timestamp": ts})
sig = hmac.new(api_secret.encode(), qs.encode(), hashlib.sha256).hexdigest()
url = "https://fapi.binance.com/fapi/v2/account?" + qs + "&signature=" + sig
req = urllib.request.Request(url, headers={"X-MBX-APIKEY": api_key})

try:
    with urllib.request.urlopen(req, timeout=10) as resp:
        import json
        body = json.loads(resp.read().decode())
except Exception as exc:
    print(f"FAIL: REST call failed: {exc}")
    sys.exit(1)

fails = []
if body.get("canTrade") is not True:
    fails.append(f"canTrade={body.get('canTrade')!r} (want True)")
if body.get("canWithdraw") is not False:
    fails.append(f"canWithdraw={body.get('canWithdraw')!r} (want False)")
if fails:
    print(f"FAIL: {'; '.join(fails)}")
    sys.exit(1)

print(
    f"OK: canTrade={body['canTrade']} canWithdraw={body['canWithdraw']} "
    f"totalWalletBalance={body.get('totalWalletBalance')} "
    f"availableBalance={body.get('availableBalance')}"
)
PY
  if [[ $? -eq 0 ]]; then
    step_pass "Binance futures key has canTrade=true canWithdraw=false"
  else
    step_fail "Binance futures key permission check failed"
  fi

  step_warn "IP whitelist + recvWindow drift are NOT checked here — verify manually in Binance UI"
}

# ---------------------------------------------------------------------------
# Step 6 — alert channel dry-run
# ---------------------------------------------------------------------------

check_alert_dry_run() {
  section "6/7  Alert channel dry-run (runbook §1.6)"

  if [[ "$XTRADE_PREFLIGHT_SKIP_ALERT" == "1" ]]; then
    step_warn "alert dry-run SKIPPED (XTRADE_PREFLIGHT_SKIP_ALERT=1)"
    return
  fi

  if [[ -z "${OPENCLAW_GATEWAY:-}" || -z "${OPENCLAW_SHARED_SECRET:-}" ]]; then
    step_fail "OPENCLAW_GATEWAY / OPENCLAW_SHARED_SECRET not set"
    return
  fi
  if [[ -z "${XTRADE_ALERT_CHANNEL:-}" ]]; then
    step_fail "XTRADE_ALERT_CHANNEL not set (expected 'yuanbao')"
    return
  fi

  XTRADE_INSTRUMENT="$XTRADE_INSTRUMENT" python3 - <<'PY'
import os
import sys

try:
    from xtrade.bridge.alerter import AlertBridge, AlertBridgeConfigError
except Exception as exc:
    print(f"FAIL: import AlertBridge: {exc}")
    sys.exit(1)

try:
    alerter = AlertBridge.from_env(dict(os.environ))
except AlertBridgeConfigError as exc:
    print(f"FAIL: AlertBridge config: {exc}")
    sys.exit(1)
if alerter is None:
    print("FAIL: AlertBridge.from_env returned None (no channel configured)")
    sys.exit(1)

try:
    alerter.dispatch_alert(
        severity="info",
        event="ops.preflight.dry_run",
        message="phase 6 preflight: alert dry-run; ignore",
        instrument=os.environ["XTRADE_INSTRUMENT"],
        fields={"preflight": "true"},
    )
except Exception as exc:
    print(f"FAIL: dispatch_alert raised: {exc}")
    sys.exit(1)
finally:
    try:
        alerter.close()
    except Exception:
        pass
print("OK: dispatch_alert returned without raising")
PY
  if [[ $? -eq 0 ]]; then
    step_pass "alert dispatch returned without raising"
    step_warn "CONFIRM ON YOUR DEVICE: did the 'ops.preflight.dry_run' alert arrive?"
    step_warn "if no push was received, the channel is up but routing is broken — investigate before starting"
  else
    step_fail "alert dispatch failed"
  fi
}

# ---------------------------------------------------------------------------
# Step 7 — disk free
# ---------------------------------------------------------------------------

check_disk_free() {
  section "7/7  Disk free (runbook §1.7)"

  if [[ ! -d "$XTRADE_VAR_ROOT" ]]; then
    step_fail "var root missing: $XTRADE_VAR_ROOT"
    return
  fi

  # `df -BG --output=avail` on GNU; portable fallback on BSD.
  local avail_gib
  if avail_gib=$(df -BG --output=avail "$XTRADE_VAR_ROOT" 2>/dev/null | tail -1 | tr -d 'G '); then
    :
  else
    # BSD df: -g reports gigabytes (1024^3) in Avail column 4
    avail_gib=$(df -g "$XTRADE_VAR_ROOT" 2>/dev/null | tail -1 | awk '{print $4}')
  fi

  if [[ -z "$avail_gib" || ! "$avail_gib" =~ ^[0-9]+$ ]]; then
    step_fail "could not parse df output for $XTRADE_VAR_ROOT"
    return
  fi

  if (( avail_gib >= XTRADE_MIN_FREE_GIB )); then
    step_pass "$XTRADE_VAR_ROOT has ${avail_gib} GiB free (≥ ${XTRADE_MIN_FREE_GIB} required)"
  else
    step_fail "$XTRADE_VAR_ROOT has only ${avail_gib} GiB free (< ${XTRADE_MIN_FREE_GIB} required)"
  fi
}

# ---------------------------------------------------------------------------
# Manual-only reminders (printed at the end)
# ---------------------------------------------------------------------------

print_manual_reminders() {
  cat <<'EOF'

================================================================
  Manual checks (this script cannot verify these)
================================================================

  [ ] Binance UI → API Management:
        - "Enable Futures" is ON
        - "Enable Withdrawals" is OFF
        - IP whitelist contains the VPS public IP only
        - Key is NOT linked to a sub-account you didn't intend
  [ ] Alert push actually landed on your openclaw / yuanbao device
        in step 6 above (the script only proved the wire is up,
        not that you received it)
  [ ] data/risk.mainnet.yaml caps reflect your INTENDED first-boot
        budget (consider $50, not $200, for the first live boot)
  [ ] systemd unit xtrade-supervisor.service is installed but NOT
        yet started — `systemctl status xtrade-supervisor` should
        show "inactive (dead)"
  [ ] /etc/xtrade/env mode 0640 root:xtrade, contains all required
        env vars (see deploy/env/xtrade.env.example)

EOF
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

echo "Phase 6 preflight — $(date -u +%Y-%m-%dT%H:%M:%SZ) — instrument=$XTRADE_INSTRUMENT"

check_locks
check_risk_caps
check_meta
check_venue_ping
check_api_key_permissions
check_alert_dry_run
check_disk_free
print_manual_reminders

echo "================================================================"
echo "  Summary"
echo "================================================================"
echo "  PASS: $PASS_COUNT"
echo "  FAIL: $FAIL_COUNT"
if (( FAIL_COUNT > 0 )); then
  echo
  echo "FAILS:"
  for f in "${FAILS[@]}"; do
    echo "  - $f"
  done
  echo
  echo "PREFLIGHT REFUSED — fix the FAILs above before"
  echo "  sudo systemctl start xtrade-supervisor.service"
  exit 2
fi

cat <<'EOF'

preflight OK — automated checks passed.

Next step (only after you have ALSO walked the manual checklist above):

  sudo systemctl start xtrade-supervisor.service
  journalctl -u xtrade-supervisor -f --since '1 min ago'

Expect to see, within 30s:
  mainnet.unlock.ok
  supervisor.start
  scanner.threshold_ladder.start(instance=spcxusdt_threshold_ladder)
  drawdown.watcher.start
  mcap_softkill.watcher.start
  alerter.up
  + one severity=info "supervisor up" alert on your device
EOF

exit 0
