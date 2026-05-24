#!/usr/bin/env bash
# uninstall_vps.sh — reverse of install_vps.sh.
#
# By default preserves /var/lib/xtrade (state); pass --purge to wipe it.
# Per docs/phase4_brief.md §5 Task 1.

set -euo pipefail
IFS=$'\n\t'

OPT_XTRADE="${OPT_XTRADE:-/opt/xtrade}"
VAR_XTRADE="${VAR_XTRADE:-/var/lib/xtrade}"
ETC_XTRADE="${ETC_XTRADE:-/etc/xtrade}"
XTRADE_USER="${XTRADE_USER:-xtrade}"
XTRADE_GROUP="${XTRADE_GROUP:-xtrade}"
PURGE=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --opt)   OPT_XTRADE="$2"; shift 2 ;;
        --var)   VAR_XTRADE="$2"; shift 2 ;;
        --etc)   ETC_XTRADE="$2"; shift 2 ;;
        --user)  XTRADE_USER="$2"; shift 2 ;;
        --group) XTRADE_GROUP="$2"; shift 2 ;;
        --purge) PURGE=1; shift ;;
        -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
        *) echo "unknown arg: $1" >&2; exit 1 ;;
    esac
done

if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
    echo "ERROR: must run as root (sudo)." >&2
    exit 1
fi

for unit in xtrade-supervisor.service xtrade-bridge.service \
            xtrade-scanner.service xtrade-scanner.timer; do
    systemctl disable --now "$unit" 2>/dev/null || true
    rm -f "/etc/systemd/system/$unit"
done
systemctl daemon-reload

rm -f /etc/logrotate.d/xtrade /etc/tmpfiles.d/xtrade.conf
rm -rf /run/xtrade

rm -rf "$OPT_XTRADE"

if [[ $PURGE -eq 1 ]]; then
    rm -rf "$VAR_XTRADE" "$ETC_XTRADE"
    if id "$XTRADE_USER" >/dev/null 2>&1; then
        userdel "$XTRADE_USER" 2>/dev/null || true
    fi
    if getent group "$XTRADE_GROUP" >/dev/null; then
        groupdel "$XTRADE_GROUP" 2>/dev/null || true
    fi
    echo "[DONE] xtrade purged (state + config + user removed)"
else
    echo "[DONE] xtrade uninstalled; state preserved at $VAR_XTRADE and $ETC_XTRADE"
fi
