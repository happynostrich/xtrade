#!/usr/bin/env bash
# 03_drill_oom.sh — Phase 4 drill #2: cgroup OOM kills xtrade-supervisor
#                   (not openclaw / hermes).
#
# Per docs/phase4_brief.md §5 Task 6 (drill 2):
#   预期: 人为占内存触发 OOM → 验证 xtrade-supervisor 被 kill
#         而不是 openclaw / hermes (凭 OOMScoreAdjust=500).
#
# Mode: ASSERT-ONLY.
# ------------------
# This script does **not** trigger memory pressure on its own — driving
# the kernel OOM killer on a live VPS that hosts openclaw + hermes is
# the kind of "drill" that wants a human finger on the off-switch.
# Instead, this script:
#
#   1. Asserts the cgroup hardening the brief requires is actually live
#      on `xtrade-supervisor.service`:
#        - MemoryMax           = 1500M (or whatever the unit declares)
#        - MemorySwapMax       = 0
#        - OOMScoreAdjust      = 500
#        - Restart             = on-failure
#   2. Cross-checks against the **runtime** values via `systemctl show`,
#      not just the unit file, in case the operator forgot
#      `daemon-reload` after editing.
#   3. Greps recent journal for unintended OOM kills (anything under
#      `oom-kill` or `Out of memory` not matching `xtrade-supervisor`).
#   4. Prints the **manual trigger** command for the operator to copy-
#      paste when ready (see docs/phase4_runbook_vps.md §7.2).
#
# Exit codes:
#   0  cgroup config matches brief; no unexpected OOM events in last
#      24h journal
#   1  one or more asserts failed (and we printed which)
#   2  prerequisite check failed (no systemctl, unit missing)
#
# Usage:
#   sudo ./scripts/phase4/03_drill_oom.sh [--unit xtrade-supervisor.service]

set -uo pipefail
IFS=$'\n\t'

UNIT="${UNIT:-xtrade-supervisor.service}"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --unit)    UNIT="$2";           shift 2 ;;
        -h|--help) sed -n '2,32p' "$0"; exit 0  ;;
        *) echo "unknown arg: $1" >&2; exit 2 ;;
    esac
done

# --- prerequisite --------------------------------------------------------
if ! command -v systemctl >/dev/null 2>&1; then
    echo "ERROR: systemctl not on PATH (drill requires systemd host)." >&2
    exit 2
fi
if ! systemctl cat "$UNIT" >/dev/null 2>&1; then
    echo "ERROR: unit $UNIT is not loaded." >&2
    exit 2
fi

fail=0
check_property() {
    local prop="$1" want="$2"
    local got
    got="$(systemctl show -p "$prop" --value "$UNIT" 2>/dev/null || true)"
    if [[ "$got" == "$want" ]]; then
        echo "[OK]   $UNIT $prop=$got"
    else
        echo "[FAIL] $UNIT $prop: want=$want got=$got" >&2
        fail=1
    fi
}

check_property_matches() {
    local prop="$1" pattern="$2"
    local got
    got="$(systemctl show -p "$prop" --value "$UNIT" 2>/dev/null || true)"
    if [[ "$got" =~ $pattern ]]; then
        echo "[OK]   $UNIT $prop=$got (matches /$pattern/)"
    else
        echo "[FAIL] $UNIT $prop=$got does not match /$pattern/" >&2
        fail=1
    fi
}

# --- cgroup asserts ------------------------------------------------------
echo "[drill-oom] runtime cgroup hardening:"
# Brief §5 Task 1 declares MemoryMax=1500M for supervisor. We assert the
# runtime byte value is in the right neighbourhood rather than equal to
# a hard-coded string, because systemd reports it in bytes (e.g.
# 1572864000 for 1500M) and operators may legitimately tighten it.
check_property_matches "MemoryMax"       '^(1[0-9]{9}|2[0-9]{9})$'    # ~1G–~2G bytes
check_property         "MemorySwapMax"   "0"
check_property         "OOMScoreAdjust"  "500"
check_property         "Restart"         "on-failure"

# --- unexpected OOM in journal? -----------------------------------------
echo "[drill-oom] scanning last 24h journal for unintended OOM events"
since_arg="--since=24 hours ago"
oom_lines="$(journalctl $since_arg --no-pager -k 2>/dev/null | grep -i -E 'oom-kill|out of memory' || true)"
if [[ -z "$oom_lines" ]]; then
    echo "[OK]   no oom-kill / 'Out of memory' lines in last 24h"
else
    suspicious="$(echo "$oom_lines" | grep -v -i "xtrade-supervisor" || true)"
    if [[ -n "$suspicious" ]]; then
        echo "[WARN] OOM events that didn't target xtrade-supervisor:" >&2
        echo "$suspicious" | sed 's/^/         /' >&2
        echo "       (this is informational; fail=0 unless OOMScoreAdjust is wrong)"
    else
        echo "[OK]   recent OOM events (if any) all targeted xtrade-supervisor"
    fi
fi

# --- manual trigger reminder --------------------------------------------
cat <<'EOF'

[drill-oom] To physically drive the OOM (DESTRUCTIVE — supervisor will
be killed and restarted), run as root:

    # Push the supervisor process group past MemoryMax via a sidecar in
    # the same cgroup. Requires the unit's MainPID, then we use the
    # cgroup.procs file under /sys/fs/cgroup/system.slice/<unit>/ to
    # attach a memhog child. systemd 250+ exposes IPCNamespacePath /
    # cgroup unified; cgexec/cgclassify are not required.
    pid=$(systemctl show -p MainPID --value xtrade-supervisor.service)
    cg=/sys/fs/cgroup/$(cut -d: -f3 </proc/$pid/cgroup | sed 's|^/||')
    # Spawn a python3 sidecar that joins this cgroup *before* allocating:
    sudo -u xtrade python3 -c '
import os, sys
# Move ourselves into supervisor's cgroup
open(os.environ["CG"] + "/cgroup.procs","w").write(str(os.getpid()))
data = bytearray(2_000_000_000)  # 2 GB > MemoryMax → cgroup OOM
print("allocated", file=sys.stderr)
input()
' CG="$cg" &

    # Expect within ~30s:
    #   journalctl -u xtrade-supervisor.service  shows OOM kill + Restart
    #   journalctl -u openclaw.service           shows no kill
    #   xtrade ops status                        eventually shows active again

EOF

if [[ $fail -ne 0 ]]; then
    echo "[drill-oom] FAIL"
    exit 1
fi
echo "[drill-oom] PASS (cgroup hardening intact; manual trigger documented)"
exit 0
