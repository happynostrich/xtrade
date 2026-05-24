#!/usr/bin/env bash
# install_vps.sh — provision xtrade on a clean OpenCloudOS 9.4 (or RHEL-family) host.
#
# Run as root (sudo). Idempotent: re-running is safe and only applies diffs.
# Per docs/phase4_brief.md §2 T1 + §4.2 layout.
#
# Usage:
#   sudo ./scripts/phase4/install_vps.sh \
#        --release-tarball /tmp/xtrade-<sha>.tar.gz \
#        [--opt /opt/xtrade] [--var /var/lib/xtrade] [--etc /etc/xtrade] \
#        [--user xtrade] [--group xtrade] \
#        [--skip-enable]
#
# Exit codes:
#   0  install complete and all units active/enabled
#   1  prerequisite check failed (wrong OS, wrong invoker, missing tarball)
#   2  unit failed to come up (journalctl tail dumped)

set -euo pipefail
IFS=$'\n\t'

# --- defaults -------------------------------------------------------------
OPT_XTRADE="${OPT_XTRADE:-/opt/xtrade}"
VAR_XTRADE="${VAR_XTRADE:-/var/lib/xtrade}"
ETC_XTRADE="${ETC_XTRADE:-/etc/xtrade}"
XTRADE_USER="${XTRADE_USER:-xtrade}"
XTRADE_GROUP="${XTRADE_GROUP:-xtrade}"
RELEASE_TARBALL=""
SKIP_ENABLE=0

# --- arg parse ------------------------------------------------------------
while [[ $# -gt 0 ]]; do
    case "$1" in
        --release-tarball) RELEASE_TARBALL="$2"; shift 2 ;;
        --opt)             OPT_XTRADE="$2";      shift 2 ;;
        --var)             VAR_XTRADE="$2";      shift 2 ;;
        --etc)             ETC_XTRADE="$2";      shift 2 ;;
        --user)            XTRADE_USER="$2";     shift 2 ;;
        --group)           XTRADE_GROUP="$2";    shift 2 ;;
        --skip-enable)     SKIP_ENABLE=1;        shift   ;;
        -h|--help)
            sed -n '2,20p' "$0"
            exit 0
            ;;
        *) echo "unknown arg: $1" >&2; exit 1 ;;
    esac
done

# --- prerequisite checks --------------------------------------------------
if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
    echo "ERROR: must run as root (sudo)." >&2
    exit 1
fi

if [[ -z "$RELEASE_TARBALL" ]]; then
    echo "ERROR: --release-tarball is required." >&2
    exit 1
fi
if [[ ! -f "$RELEASE_TARBALL" ]]; then
    echo "ERROR: tarball not found: $RELEASE_TARBALL" >&2
    exit 1
fi

if ! command -v systemctl >/dev/null 2>&1; then
    echo "ERROR: systemctl not found; this script targets systemd hosts." >&2
    exit 1
fi

if ! command -v envsubst >/dev/null 2>&1; then
    echo "ERROR: envsubst not found; install gettext (dnf install -y gettext)." >&2
    exit 1
fi

# --- user/group -----------------------------------------------------------
if ! getent group "$XTRADE_GROUP" >/dev/null; then
    groupadd --system "$XTRADE_GROUP"
    echo "[+] created group $XTRADE_GROUP"
fi
if ! id "$XTRADE_USER" >/dev/null 2>&1; then
    useradd --system --gid "$XTRADE_GROUP" --home-dir "$VAR_XTRADE" \
            --shell /sbin/nologin "$XTRADE_USER"
    echo "[+] created user $XTRADE_USER"
fi

# --- directories ----------------------------------------------------------
install -d -m 0755 -o root         -g root         "$OPT_XTRADE"
install -d -m 0755 -o root         -g root         "$OPT_XTRADE/releases"
install -d -m 0750 -o root         -g "$XTRADE_GROUP" "$ETC_XTRADE"
install -d -m 0700 -o "$XTRADE_USER" -g "$XTRADE_GROUP" "$VAR_XTRADE"
install -d -m 0700 -o "$XTRADE_USER" -g "$XTRADE_GROUP" "$VAR_XTRADE/catalog"
install -d -m 0700 -o "$XTRADE_USER" -g "$XTRADE_GROUP" "$VAR_XTRADE/signals"
install -d -m 0700 -o "$XTRADE_USER" -g "$XTRADE_GROUP" "$VAR_XTRADE/approvals"
install -d -m 0700 -o "$XTRADE_USER" -g "$XTRADE_GROUP" "$VAR_XTRADE/logs"
install -d -m 0700 -o "$XTRADE_USER" -g "$XTRADE_GROUP" "$VAR_XTRADE/archive"
echo "[+] directories ready"

# --- release unpack -------------------------------------------------------
RELEASE_SHA="$(sha256sum "$RELEASE_TARBALL" | awk '{print substr($1,1,12)}')"
RELEASE_DIR="$OPT_XTRADE/releases/$RELEASE_SHA"
if [[ ! -d "$RELEASE_DIR" ]]; then
    install -d -m 0755 -o root -g root "$RELEASE_DIR"
    tar -xzf "$RELEASE_TARBALL" -C "$RELEASE_DIR"
    echo "[+] unpacked release -> $RELEASE_DIR"
else
    echo "[=] release $RELEASE_SHA already present"
fi
ln -sfn "$RELEASE_DIR" "$OPT_XTRADE/current"
echo "[+] $OPT_XTRADE/current -> $RELEASE_DIR"

# --- uv + python 3.12 venv ------------------------------------------------
UV_BIN="/usr/local/bin/uv"
if [[ ! -x "$UV_BIN" ]]; then
    echo "[+] installing uv"
    curl -fsSL https://astral.sh/uv/install.sh | UV_INSTALL_DIR=/usr/local/bin sh
fi
if [[ ! -d "$OPT_XTRADE/.venv" ]]; then
    "$UV_BIN" python install 3.12 >/dev/null
    "$UV_BIN" venv --python 3.12 "$OPT_XTRADE/.venv"
    echo "[+] created venv at $OPT_XTRADE/.venv"
fi
VIRTUAL_ENV="$OPT_XTRADE/.venv" "$UV_BIN" pip install --quiet --upgrade pip
VIRTUAL_ENV="$OPT_XTRADE/.venv" "$UV_BIN" pip install --quiet "$OPT_XTRADE/current"
chown -R root:root "$OPT_XTRADE/.venv"
echo "[+] xtrade installed into venv"

# --- /etc/xtrade configs --------------------------------------------------
if [[ ! -f "$ETC_XTRADE/env" ]]; then
    install -m 0640 -o root -g "$XTRADE_GROUP" \
            "$OPT_XTRADE/current/deploy/env/xtrade.env.example" \
            "$ETC_XTRADE/env"
    echo "[+] seeded $ETC_XTRADE/env (EDIT BEFORE FIRST START)"
fi
for cfg in venues.binance_spot.testnet.yaml \
           venues.binance_futures.testnet.yaml \
           venues.hyperliquid.testnet.yaml \
           universe.example.yaml \
           risk.example.yaml; do
    src="$OPT_XTRADE/current/config/$cfg"
    dst="$ETC_XTRADE/${cfg/.example/}"
    [[ -f "$src" && ! -f "$dst" ]] && install -m 0640 -o root -g "$XTRADE_GROUP" "$src" "$dst" \
        && echo "[+] seeded $dst"
done

# --- systemd unit render --------------------------------------------------
export OPT_XTRADE VAR_XTRADE ETC_XTRADE XTRADE_USER XTRADE_GROUP
SUBST_VARS='${OPT_XTRADE} ${VAR_XTRADE} ${ETC_XTRADE} ${XTRADE_USER} ${XTRADE_GROUP}'
for unit in xtrade-supervisor.service \
            xtrade-scanner.service \
            xtrade-scanner.timer \
            xtrade-bridge.service; do
    src="$OPT_XTRADE/current/deploy/systemd/${unit}.in"
    dst="/etc/systemd/system/${unit}"
    if [[ ! -f "$src" ]]; then
        echo "ERROR: template missing: $src" >&2
        exit 1
    fi
    envsubst "$SUBST_VARS" < "$src" > "$dst.new"
    if ! cmp -s "$dst.new" "$dst" 2>/dev/null; then
        mv "$dst.new" "$dst"
        echo "[+] installed $dst"
    else
        rm -f "$dst.new"
        echo "[=] $dst unchanged"
    fi
done

# --- logrotate + tmpfiles -------------------------------------------------
install -m 0644 "$OPT_XTRADE/current/deploy/logrotate/xtrade" /etc/logrotate.d/xtrade
install -m 0644 "$OPT_XTRADE/current/deploy/tmpfiles/xtrade.conf" /etc/tmpfiles.d/xtrade.conf
systemd-tmpfiles --create /etc/tmpfiles.d/xtrade.conf

# --- enable + start -------------------------------------------------------
systemctl daemon-reload
if [[ $SKIP_ENABLE -eq 1 ]]; then
    echo "[=] --skip-enable: not starting units"
    exit 0
fi
systemctl enable --now xtrade-supervisor.service xtrade-bridge.service xtrade-scanner.timer

# --- post-start health check ---------------------------------------------
sleep 3
fail=0
for unit in xtrade-supervisor.service xtrade-bridge.service xtrade-scanner.timer; do
    if systemctl is-active --quiet "$unit"; then
        echo "[OK] $unit active"
    else
        echo "[FAIL] $unit not active" >&2
        journalctl -u "$unit" -n 30 --no-pager >&2 || true
        fail=1
    fi
done

if [[ $fail -ne 0 ]]; then
    echo "ERROR: one or more units failed to start." >&2
    exit 2
fi

echo "[DONE] xtrade installed; release=$RELEASE_SHA"
