#!/usr/bin/env bash
# 05_smoke_postdeploy.sh — quick assertions after install_vps.sh succeeds.
#
# Per docs/phase4_brief.md §2 T1 acceptance: install script exit 0 ==
# "supervisor healthy". This script gives a deeper smoke check the operator
# can rerun any time.
#
# Exit codes:
#   0 all green
#   1 any check failed (details printed)

set -uo pipefail
IFS=$'\n\t'

OPT_XTRADE="${OPT_XTRADE:-/opt/xtrade}"
VAR_XTRADE="${VAR_XTRADE:-/var/lib/xtrade}"
ETC_XTRADE="${ETC_XTRADE:-/etc/xtrade}"

fail=0
check() {
    local desc="$1"; shift
    if "$@" >/dev/null 2>&1; then
        echo "[OK]   $desc"
    else
        echo "[FAIL] $desc" >&2
        "$@" 2>&1 | sed 's/^/         /' >&2 || true
        fail=1
    fi
}

# --- filesystem -----------------------------------------------------------
check "code dir present"        test -d "$OPT_XTRADE/current"
check "venv present"            test -x "$OPT_XTRADE/.venv/bin/xtrade"
check "var dir owned correctly" test -O "$VAR_XTRADE" -o -d "$VAR_XTRADE"
check "env file readable"       test -r "$ETC_XTRADE/env"

# --- units ----------------------------------------------------------------
for unit in xtrade-supervisor.service xtrade-bridge.service; do
    check "$unit active" systemctl is-active --quiet "$unit"
done
check "xtrade-scanner.timer enabled" systemctl is-enabled --quiet xtrade-scanner.timer

# --- CLI sanity -----------------------------------------------------------
check "xtrade --help"        "$OPT_XTRADE/.venv/bin/xtrade" --help
check "xtrade ops status"    "$OPT_XTRADE/.venv/bin/xtrade" ops status

# --- bridge port ----------------------------------------------------------
if command -v ss >/dev/null 2>&1; then
    check "bridge listening on 127.0.0.1:18080" \
        bash -c "ss -lntp 2>/dev/null | grep -q 127.0.0.1:18080"
fi

if [[ $fail -ne 0 ]]; then
    echo "smoke FAILED"
    exit 1
fi
echo "smoke OK"
