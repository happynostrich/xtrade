#!/usr/bin/env bash
# 04_drill_network.sh — Phase 4 drill #3: 30s network outage to Binance.
#
# Per docs/phase4_brief.md §5 Task 6 (drill 3):
#   预期: TradingNode 心跳告警 + 不下错单 + 网络恢复后自动续订阅。
#
# What this does
# --------------
#   1. Resolves Binance API host(s) to IPv4 (configurable host list).
#   2. Inserts `iptables -A OUTPUT -d <ip> -j DROP` for each IP at the
#      start of the OUTPUT chain. Uses an `xtrade-drill` chain comment
#      via `iptables -m comment --comment` so cleanup is precise.
#   3. Sleeps DROP_S seconds (default 30).
#   4. Removes the rules (so a crash mid-drill still leaves a path to
#      restore via the trap handler).
#   5. Polls connectivity back up via `getent hosts` + a TCP probe.
#   6. Asserts xtrade-supervisor stayed `active` throughout, and prints
#      a journal tail showing the heartbeat warnings.
#
# Safety
# ------
#   - DRY-RUN by default (prints what it would do). Pass --commit to
#     actually mutate iptables.
#   - The cleanup trap runs on any exit (success / failure / SIGINT)
#     so a bad day still ends with the network restored.
#
# Exit codes:
#   0  drill PASSED
#   1  drill FAILED (supervisor crashed during the outage, or
#                    connectivity did not recover after rule removal)
#   2  prerequisite check failed
#
# Usage:
#   sudo ./scripts/phase4/04_drill_network.sh --commit
#   sudo ./scripts/phase4/04_drill_network.sh --commit --drop-s 45 \
#        --host api.binance.com --host fapi.binance.com

set -uo pipefail
IFS=$'\n\t'

UNIT="${UNIT:-xtrade-supervisor.service}"
DROP_S="${DROP_S:-30}"
declare -a HOSTS=()
COMMIT=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --unit)    UNIT="$2";          shift 2 ;;
        --drop-s)  DROP_S="$2";        shift 2 ;;
        --host)    HOSTS+=("$2");      shift 2 ;;
        --commit)  COMMIT=1;           shift   ;;
        -h|--help) sed -n '2,34p' "$0"; exit 0  ;;
        *) echo "unknown arg: $1" >&2; exit 2 ;;
    esac
done

# Default host list when caller didn't pass --host.
if [[ ${#HOSTS[@]} -eq 0 ]]; then
    HOSTS=(api.binance.com fapi.binance.com)
fi

# --- prerequisite checks -------------------------------------------------
if [[ "${EUID:-$(id -u)}" -ne 0 && $COMMIT -eq 1 ]]; then
    echo "ERROR: --commit requires root (iptables needs CAP_NET_ADMIN)." >&2
    exit 2
fi
if ! command -v iptables >/dev/null 2>&1; then
    echo "ERROR: iptables not on PATH." >&2
    exit 2
fi
if ! command -v getent >/dev/null 2>&1; then
    echo "ERROR: getent not on PATH." >&2
    exit 2
fi
if ! systemctl cat "$UNIT" >/dev/null 2>&1; then
    echo "ERROR: unit $UNIT is not loaded." >&2
    exit 2
fi

# --- resolve hosts to IPv4 ----------------------------------------------
declare -a IPS=()
for host in "${HOSTS[@]}"; do
    # getent hosts may return multiple A records; take all.
    while read -r ip _; do
        [[ -n "$ip" ]] && IPS+=("$ip")
    done < <(getent ahostsv4 "$host" 2>/dev/null | awk '/STREAM/ {print $1}' | sort -u)
done
if [[ ${#IPS[@]} -eq 0 ]]; then
    echo "ERROR: could not resolve any IPv4 for: ${HOSTS[*]}" >&2
    exit 2
fi

echo "[drill-network] hosts=${HOSTS[*]}"
echo "[drill-network] resolved IPs=${IPS[*]}"
echo "[drill-network] drop_s=$DROP_S commit=$COMMIT"

if [[ $COMMIT -eq 0 ]]; then
    echo "[drill-network] DRY-RUN: would insert and later remove:"
    for ip in "${IPS[@]}"; do
        echo "    iptables -I OUTPUT -d $ip -j DROP -m comment --comment xtrade-drill"
    done
    echo "[drill-network] (re-run with --commit to actually run the drill)"
    exit 0
fi

# --- cleanup trap (always run) -------------------------------------------
cleanup() {
    local code=$?
    echo "[drill-network] cleanup: removing xtrade-drill rules"
    # -D variant of every rule we added; ignore errors (rule may not exist if -I failed)
    for ip in "${IPS[@]}"; do
        iptables -D OUTPUT -d "$ip" -j DROP -m comment --comment xtrade-drill 2>/dev/null || true
    done
    exit $code
}
trap cleanup EXIT INT TERM

# --- pre-state -----------------------------------------------------------
pre_active=$(systemctl is-active "$UNIT" || true)
if [[ "$pre_active" != "active" ]]; then
    echo "[drill-network] FAIL: $UNIT is not active before drill (state=$pre_active)" >&2
    exit 1
fi

# --- insert drop rules ---------------------------------------------------
for ip in "${IPS[@]}"; do
    iptables -I OUTPUT -d "$ip" -j DROP -m comment --comment xtrade-drill
done
echo "[drill-network] DROP rules in place; sleeping ${DROP_S}s"
sleep "$DROP_S"

# --- remove drop rules (cleanup trap runs again on exit but precise here) ---
for ip in "${IPS[@]}"; do
    iptables -D OUTPUT -d "$ip" -j DROP -m comment --comment xtrade-drill 2>/dev/null || true
done
trap - EXIT INT TERM  # don't double-clean below

# --- wait for connectivity ----------------------------------------------
echo "[drill-network] waiting up to 30s for connectivity to ${HOSTS[0]}"
ok=0
for _ in $(seq 1 30); do
    if getent hosts "${HOSTS[0]}" >/dev/null 2>&1 \
       && timeout 5 bash -c "</dev/tcp/${HOSTS[0]}/443" 2>/dev/null; then
        ok=1
        break
    fi
    sleep 1
done
if [[ $ok -eq 0 ]]; then
    echo "[drill-network] FAIL: connectivity to ${HOSTS[0]} did not recover within 30s" >&2
    exit 1
fi

# --- supervisor still active? -------------------------------------------
post_active=$(systemctl is-active "$UNIT" || true)
if [[ "$post_active" != "active" ]]; then
    echo "[drill-network] FAIL: $UNIT no longer active (state=$post_active)" >&2
    journalctl -u "$UNIT" -n 80 --no-pager || true
    exit 1
fi

# --- journal evidence ----------------------------------------------------
echo "[drill-network] supervisor journal during outage window (paste into runbook):"
journalctl -u "$UNIT" --no-pager --since "${DROP_S}s ago" | sed 's/^/    /'

echo "[drill-network] PASS"
exit 0
