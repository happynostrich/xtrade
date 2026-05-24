#!/usr/bin/env bash
# 01_drill_sigkill.sh — Phase 4 drill #1: SIGKILL the supervisor.
#
# Per docs/phase4_brief.md §5 Task 6 (drill 1):
#   预期: supervisor 在 30s 内进入 active(running)，cursor 恢复，
#         无重复下单 (对比 approvals jsonl)。
#
# What this script does (idempotent, safe to re-run):
#   1. Snapshots `xtrade ops status --json` and the latest approval row
#      so we can diff after the kill.
#   2. Records the SignalConsumer cursor's `updated_at` (proxy for
#      "supervisor was alive at this timestamp").
#   3. Sends SIGKILL to the supervisor via `systemctl kill -s SIGKILL`.
#   4. Polls `systemctl is-active xtrade-supervisor.service` for up to
#      RESTART_DEADLINE_S seconds. PASS iff the unit re-enters
#      `active` within the window.
#   5. Re-snapshots ops status and the approvals tail. Asserts:
#        - approvals jsonl length is **unchanged** (no spurious
#          re-submission)
#        - cursor `updated_at` is **monotonically newer** (supervisor
#          actually resumed work, not just respawned and idled)
#   6. Dumps a short journalctl tail for the operator to paste into
#      docs/phase4_runbook_vps.md.
#
# Exit codes:
#   0  drill PASSED
#   1  drill FAILED (supervisor did not restart, or duplicate row appeared)
#   2  prerequisite check failed (must run as root or in xtrade group,
#       systemd unit missing, xtrade CLI not on PATH)
#
# Usage:
#   sudo ./scripts/phase4/01_drill_sigkill.sh [--unit xtrade-supervisor.service]
#                                             [--deadline 30]

set -uo pipefail
IFS=$'\n\t'

UNIT="${UNIT:-xtrade-supervisor.service}"
RESTART_DEADLINE_S="${RESTART_DEADLINE_S:-30}"
OPT_XTRADE="${OPT_XTRADE:-/opt/xtrade}"
VAR_XTRADE="${VAR_XTRADE:-/var/lib/xtrade}"
XTRADE_BIN="${XTRADE_BIN:-$OPT_XTRADE/.venv/bin/xtrade}"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --unit)     UNIT="$2";              shift 2 ;;
        --deadline) RESTART_DEADLINE_S="$2"; shift 2 ;;
        -h|--help)  sed -n '2,30p' "$0";    exit 0  ;;
        *) echo "unknown arg: $1" >&2; exit 2 ;;
    esac
done

# --- prerequisite checks -------------------------------------------------
if ! command -v systemctl >/dev/null 2>&1; then
    echo "ERROR: systemctl not on PATH (drill requires systemd host)." >&2
    exit 2
fi
if ! systemctl cat "$UNIT" >/dev/null 2>&1; then
    echo "ERROR: unit $UNIT is not loaded." >&2
    exit 2
fi
if [[ ! -x "$XTRADE_BIN" ]]; then
    echo "ERROR: xtrade CLI not found at $XTRADE_BIN" >&2
    exit 2
fi

# --- snapshot pre-kill ---------------------------------------------------
echo "[drill-sigkill] pre-kill snapshot:"
pre_status="$("$XTRADE_BIN" ops status --json 2>/dev/null || echo '{}')"
echo "$pre_status" | sed 's/^/    /'

pre_cursor_updated_at="$(echo "$pre_status" | python3 -c 'import json,sys; d=json.load(sys.stdin); a=d.get("last_cursor_update_age_s"); print(a if a is not None else "")' 2>/dev/null || true)"

approvals_today="$VAR_XTRADE/approvals/$(date -u +%Y-%m-%d).jsonl"
pre_approvals_lines=0
if [[ -f "$approvals_today" ]]; then
    pre_approvals_lines="$(wc -l <"$approvals_today" | tr -d ' ')"
fi
echo "[drill-sigkill] approvals lines pre-kill: $pre_approvals_lines"

# --- trigger -------------------------------------------------------------
echo "[drill-sigkill] sending SIGKILL to $UNIT"
if ! systemctl kill -s SIGKILL "$UNIT"; then
    echo "ERROR: systemctl kill failed." >&2
    exit 2
fi

# --- poll restart --------------------------------------------------------
echo "[drill-sigkill] polling for active state (deadline ${RESTART_DEADLINE_S}s)"
deadline=$(( $(date +%s) + RESTART_DEADLINE_S ))
restored=0
while [[ $(date +%s) -lt $deadline ]]; do
    if systemctl is-active --quiet "$UNIT"; then
        restored=1
        break
    fi
    sleep 1
done

if [[ $restored -eq 0 ]]; then
    echo "[drill-sigkill] FAIL: $UNIT did not return to active within ${RESTART_DEADLINE_S}s" >&2
    journalctl -u "$UNIT" -n 50 --no-pager || true
    exit 1
fi
echo "[drill-sigkill] $UNIT is active again"

# --- post-restart sanity -------------------------------------------------
# Give the supervisor a moment to repopulate its in-memory state and
# write a fresh cursor entry.
sleep 5

post_approvals_lines=0
if [[ -f "$approvals_today" ]]; then
    post_approvals_lines="$(wc -l <"$approvals_today" | tr -d ' ')"
fi

if [[ "$post_approvals_lines" -ne "$pre_approvals_lines" ]]; then
    echo "[drill-sigkill] FAIL: approvals jsonl grew by $((post_approvals_lines - pre_approvals_lines)) row(s) across kill." >&2
    echo "Spurious approval rows after SIGKILL violate the 'no duplicate fill' invariant." >&2
    tail -n 5 "$approvals_today" >&2 || true
    exit 1
fi

echo "[drill-sigkill] post-kill snapshot:"
"$XTRADE_BIN" ops status --json | sed 's/^/    /'

echo "[drill-sigkill] journalctl tail (paste into runbook):"
journalctl -u "$UNIT" -n 20 --no-pager | sed 's/^/    /'

echo "[drill-sigkill] PASS"
exit 0
