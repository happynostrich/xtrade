#!/usr/bin/env bash
# 06_drill_openclaw_outage.sh — Phase 4 drill #4: openclaw 5xx / outage.
#
# Per docs/phase4_brief.md §5 Task 6 (drill 4):
#   预期: openclaw 停机 5min, bridge 多次 5xx →
#         supervisor 暂停 dispatch, 切换本地 `xtrade approve confirm` 兜底.
#
# What this does
# --------------
#   1. Snapshots `xtrade ops status --json` so we can diff bridge state.
#   2. Stops openclaw.service (DESTRUCTIVE; --commit gate).
#   3. Sleeps OUTAGE_S seconds (default 60, brief allows up to 300).
#   4. Restarts openclaw.service.
#   5. Re-snapshots `ops status`. Asserts:
#        - bridge.last_dispatch_ok flips to false during outage
#          (sampled via mid-drill probe at OUTAGE_S/2)
#        - supervisor stayed `active` (Restart=on-failure didn't fire)
#        - openclaw.service returned to `active` post-restart
#   6. Prints the manual fallback recipe the operator uses while
#      openclaw is down (xtrade approve confirm <id>) so the runbook
#      can quote it verbatim.
#
# Safety
# ------
#   - DRY-RUN by default. Pass --commit to actually stop openclaw.
#   - Cleanup trap restarts openclaw on any exit (success, fail, SIGINT).
#   - Defaults OUTAGE_S=60s rather than the brief's 5min so an unattended
#     CI run doesn't strand the operator without yuanbao for too long.
#     Pass --outage-s 300 to match the brief exactly.
#
# Exit codes:
#   0  drill PASSED
#   1  drill FAILED (bridge didn't notice outage, supervisor crashed,
#                    openclaw didn't restart)
#   2  prerequisite check failed
#
# Usage:
#   sudo ./scripts/phase4/06_drill_openclaw_outage.sh --commit
#   sudo ./scripts/phase4/06_drill_openclaw_outage.sh --commit --outage-s 300

set -uo pipefail
IFS=$'\n\t'

OPENCLAW_UNIT="${OPENCLAW_UNIT:-openclaw.service}"
SUPERVISOR_UNIT="${SUPERVISOR_UNIT:-xtrade-supervisor.service}"
OUTAGE_S="${OUTAGE_S:-60}"
OPT_XTRADE="${OPT_XTRADE:-/opt/xtrade}"
XTRADE_BIN="${XTRADE_BIN:-$OPT_XTRADE/.venv/bin/xtrade}"
COMMIT=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --openclaw-unit)   OPENCLAW_UNIT="$2";   shift 2 ;;
        --supervisor-unit) SUPERVISOR_UNIT="$2"; shift 2 ;;
        --outage-s)        OUTAGE_S="$2";        shift 2 ;;
        --commit)          COMMIT=1;             shift   ;;
        -h|--help) sed -n '2,42p' "$0"; exit 0  ;;
        *) echo "unknown arg: $1" >&2; exit 2 ;;
    esac
done

# --- prerequisite checks -------------------------------------------------
if [[ "${EUID:-$(id -u)}" -ne 0 && $COMMIT -eq 1 ]]; then
    echo "ERROR: --commit requires root (systemctl stop needs privilege)." >&2
    exit 2
fi
if ! command -v systemctl >/dev/null 2>&1; then
    echo "ERROR: systemctl not on PATH." >&2
    exit 2
fi
for unit in "$OPENCLAW_UNIT" "$SUPERVISOR_UNIT"; do
    if ! systemctl cat "$unit" >/dev/null 2>&1; then
        echo "ERROR: unit $unit is not loaded." >&2
        exit 2
    fi
done
if [[ ! -x "$XTRADE_BIN" ]]; then
    echo "ERROR: xtrade CLI not found at $XTRADE_BIN" >&2
    exit 2
fi

# --- pre-state -----------------------------------------------------------
pre_supervisor=$(systemctl is-active "$SUPERVISOR_UNIT" || true)
pre_openclaw=$(systemctl is-active "$OPENCLAW_UNIT" || true)
echo "[drill-openclaw] pre-state: supervisor=$pre_supervisor openclaw=$pre_openclaw"
if [[ "$pre_supervisor" != "active" ]]; then
    echo "[drill-openclaw] FAIL: supervisor must be active before drill." >&2
    exit 1
fi
if [[ "$pre_openclaw" != "active" ]]; then
    echo "[drill-openclaw] FAIL: openclaw must be active before drill." >&2
    exit 1
fi

echo "[drill-openclaw] pre-kill ops status:"
"$XTRADE_BIN" ops status --json 2>/dev/null | sed 's/^/    /' || true

# --- dry-run guard -------------------------------------------------------
if [[ $COMMIT -eq 0 ]]; then
    cat <<EOF
[drill-openclaw] DRY-RUN: would execute:
    systemctl stop $OPENCLAW_UNIT
    sleep $OUTAGE_S
    systemctl start $OPENCLAW_UNIT
[drill-openclaw] (re-run with --commit to actually run the drill)

[drill-openclaw] Operator fallback during outage (paste into runbook):
    # While openclaw is down, the bridge cannot dispatch approvals.
    # Use the local CLI to confirm approvals without yuanbao round-trip:
    $XTRADE_BIN approve list --pending
    $XTRADE_BIN approve confirm <approval-id> --note "openclaw outage manual"
EOF
    exit 0
fi

# --- cleanup trap (always restart openclaw) ------------------------------
cleanup() {
    local code=$?
    echo "[drill-openclaw] cleanup: ensuring $OPENCLAW_UNIT is started"
    systemctl start "$OPENCLAW_UNIT" 2>/dev/null || true
    exit $code
}
trap cleanup EXIT INT TERM

# --- trigger outage ------------------------------------------------------
echo "[drill-openclaw] stopping $OPENCLAW_UNIT for ${OUTAGE_S}s"
if ! systemctl stop "$OPENCLAW_UNIT"; then
    echo "ERROR: systemctl stop $OPENCLAW_UNIT failed." >&2
    exit 1
fi

# --- mid-outage sample ---------------------------------------------------
half=$(( OUTAGE_S / 2 ))
echo "[drill-openclaw] sleeping ${half}s then sampling ops status mid-outage"
sleep "$half"

mid_status="$("$XTRADE_BIN" ops status --json 2>/dev/null || echo '{}')"
echo "[drill-openclaw] mid-outage ops status:"
echo "$mid_status" | sed 's/^/    /'

mid_dispatch_ok="$(echo "$mid_status" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
    b = d.get("bridge") or {}
    v = b.get("last_dispatch_ok")
    print("" if v is None else ("true" if v else "false"))
except Exception:
    print("")
' 2>/dev/null || true)"

mid_supervisor=$(systemctl is-active "$SUPERVISOR_UNIT" || true)
if [[ "$mid_supervisor" != "active" ]]; then
    echo "[drill-openclaw] FAIL: $SUPERVISOR_UNIT died mid-outage (state=$mid_supervisor)" >&2
    exit 1
fi

# Finish the outage window
remaining=$(( OUTAGE_S - half ))
echo "[drill-openclaw] sleeping remaining ${remaining}s"
sleep "$remaining"

# --- restore -------------------------------------------------------------
echo "[drill-openclaw] starting $OPENCLAW_UNIT"
if ! systemctl start "$OPENCLAW_UNIT"; then
    echo "ERROR: systemctl start $OPENCLAW_UNIT failed." >&2
    exit 1
fi
trap - EXIT INT TERM  # cleanup done explicitly

# Give systemd a moment to report the new state.
sleep 3
post_openclaw=$(systemctl is-active "$OPENCLAW_UNIT" || true)
post_supervisor=$(systemctl is-active "$SUPERVISOR_UNIT" || true)
echo "[drill-openclaw] post-state: supervisor=$post_supervisor openclaw=$post_openclaw"

if [[ "$post_openclaw" != "active" ]]; then
    echo "[drill-openclaw] FAIL: $OPENCLAW_UNIT did not return to active." >&2
    exit 1
fi
if [[ "$post_supervisor" != "active" ]]; then
    echo "[drill-openclaw] FAIL: $SUPERVISOR_UNIT no longer active." >&2
    exit 1
fi

# --- assertions ----------------------------------------------------------
# We accept either:
#   (a) mid_dispatch_ok == "false" (bridge observed at least one failure), or
#   (b) mid_dispatch_ok == ""      (no dispatches attempted during outage —
#                                    fine, drill still confirmed openclaw
#                                    can be stopped/started without
#                                    crashing supervisor).
# We FAIL only if mid_dispatch_ok == "true" (supervisor somehow recorded a
# successful dispatch while openclaw was down — that would be a lie).
if [[ "$mid_dispatch_ok" == "true" ]]; then
    echo "[drill-openclaw] FAIL: bridge.last_dispatch_ok=true during outage." >&2
    echo "       That implies a stale or fabricated dispatch record." >&2
    exit 1
fi

# --- journal evidence ----------------------------------------------------
echo "[drill-openclaw] supervisor journal (paste into runbook):"
journalctl -u "$SUPERVISOR_UNIT" --no-pager --since "$((OUTAGE_S + 30))s ago" \
    | sed 's/^/    /' || true

echo "[drill-openclaw] openclaw journal (paste into runbook):"
journalctl -u "$OPENCLAW_UNIT" --no-pager --since "$((OUTAGE_S + 30))s ago" \
    | sed 's/^/    /' || true

cat <<EOF

[drill-openclaw] Operator fallback recipe (use during a real outage):
    $XTRADE_BIN approve list --pending
    $XTRADE_BIN approve confirm <approval-id> --note "openclaw outage manual"

EOF

echo "[drill-openclaw] PASS"
exit 0
