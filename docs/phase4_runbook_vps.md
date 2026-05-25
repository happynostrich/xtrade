# Phase 4 VPS runbook — install / upgrade / rollback / 故障演练

This runbook is the **operator artefact for Phase 4 Task 6** (per
`docs/phase4_brief.md` §5 Task 6 + §8 交付物 #8). It is meant to be
opened in a terminal on the operator's mac next to an `ssh vps`
session, and walked top-to-bottom on the first install, or section-by-
section for upgrades and incident drills.

The four fault-recovery drills in §7 are the verification of T6 in the
acceptance matrix. Each drill has the brief's exact 预期 / 准备 / 触发 /
观察 / 判据 / 通过-未通过 / 日志样本 sections; run them in order the
first time, individually thereafter when re-validating a change.

---

## 0. 前提

1. Singapore VPS provisioned (2 CPU / 8 GB RAM / 80 GB SSD, OpenCloudOS
   9.4 or RHEL-family). openclaw + hermes already running and healthy
   on the same host (xtrade is the third tenant).
2. SSH key access; operator is in `wheel` or has `sudo` without
   password prompt for the install hour.
3. `/etc/xtrade/env` populated with at minimum:
   ```
   BINANCE_FUTURES_TESTNET_API_KEY=...
   BINANCE_FUTURES_TESTNET_API_SECRET=...
   OPENCLAW_GATEWAY=https://<openclaw-gateway>
   OPENCLAW_SHARED_SECRET=...
   OPENCLAW_INBOUND_SECRET=...
   ```
   File mode must be `640 root:xtrade`. The xtrade systemd units source
   it via `EnvironmentFile=`; never commit it.
4. A release tarball built on the dev mac. The tarball must be **flat**
   (no leading `xtrade/` prefix) — `install_vps.sh` does a plain
   `tar -xzf … -C "$RELEASE_DIR"` without `--strip-components`, so any
   prefix lands `pyproject.toml` one directory too deep and `pip install`
   fails:
   ```bash
   # local mac:
   git -C ~/xtrade archive --format=tar.gz \
       -o /tmp/xtrade-$(git -C ~/xtrade rev-parse --short HEAD).tar.gz HEAD
   scp /tmp/xtrade-*.tar.gz vps:/tmp/
   ```

---

## 1. Install (first time on a fresh host)

Stage the installer script into a temp directory (the tarball is flat,
so extracting `cd /tmp; tar xzf …` would scatter files across `/tmp`):

```bash
# on the VPS, as root
rm -rf /tmp/xtrade-staging && mkdir /tmp/xtrade-staging
tar xzf /tmp/xtrade-<sha>.tar.gz -C /tmp/xtrade-staging
sudo bash /tmp/xtrade-staging/scripts/phase4/install_vps.sh \
    --release-tarball /tmp/xtrade-<sha>.tar.gz
```

`install_vps.sh` is idempotent (per brief §5 Task 1). On exit code 0
all four units (`xtrade-supervisor.service`, `xtrade-bridge.service`,
`xtrade-scanner.service`, `xtrade-scanner.timer`) are `active` /
`enabled`.

Smoke-check immediately after:

```bash
sudo /tmp/xtrade-staging/scripts/phase4/05_smoke_postdeploy.sh
sudo -u xtrade /opt/xtrade/.venv/bin/xtrade ops status
```

Expected: `smoke OK` and an `ops status` line with `paused=false`,
non-empty `supervisor.state` (active).

---

## 2. Upgrade (deploy a new release)

```bash
# on the VPS
rm -rf /tmp/xtrade-staging && mkdir /tmp/xtrade-staging
tar xzf /tmp/xtrade-<new-sha>.tar.gz -C /tmp/xtrade-staging
sudo bash /tmp/xtrade-staging/scripts/phase4/install_vps.sh \
    --release-tarball /tmp/xtrade-<new-sha>.tar.gz
```

The install script:
- extracts the tarball under `/opt/xtrade/releases/<sha>/`
- flips `/opt/xtrade/current` symlink atomically
- runs `uv pip install -e /opt/xtrade/current` against the existing
  `/opt/xtrade/.venv`
- `systemctl daemon-reload` + `systemctl restart xtrade-supervisor
  xtrade-bridge xtrade-scanner.timer`

Verify post-upgrade:

```bash
sudo /tmp/xtrade/scripts/phase4/05_smoke_postdeploy.sh
sudo -u xtrade /opt/xtrade/.venv/bin/xtrade ops status --json
```

Confirm `last_cursor_update_age_s` is small (< 30s — supervisor
resumed promptly) and no `dispatch_failed` rows accumulated during the
restart window (`xtrade approve list --pending`).

---

## 3. Rollback

`/opt/xtrade/releases/` keeps the previous N tarballs. To roll back to
the prior sha:

```bash
sudo ln -sfn /opt/xtrade/releases/<prev-sha> /opt/xtrade/current
sudo systemctl daemon-reload
sudo systemctl restart xtrade-supervisor xtrade-bridge xtrade-scanner.timer
sudo -u xtrade /opt/xtrade/.venv/bin/xtrade ops status
```

If the previous release shipped a different set of Python deps:

```bash
sudo -u xtrade /opt/xtrade/.venv/bin/uv pip install -e /opt/xtrade/current
sudo systemctl restart xtrade-supervisor
```

State (`/var/lib/xtrade/{signals,approvals,catalog,logs}/`) is **not**
touched by rollback — Phase 3's append-only jsonl + parquet layout is
forward-compatible by design.

---

## 4. Stop / restart / pause

| Action | Command | When to use |
|---|---|---|
| Pause (don't submit new orders; keep market data subscribed) | `xtrade ops pause --reason "..."` | Maintenance window, suspicious signal stream, openclaw outage where you want manual confirms only |
| Resume | `xtrade ops resume` | After pause |
| Stop supervisor (controlled) | `sudo systemctl stop xtrade-supervisor` | OS reboot, host maintenance |
| Kill supervisor (escalation) | `xtrade ops kill --yes` | Supervisor is wedged but won't respond to SIGTERM |
| Restart all xtrade units | `sudo systemctl restart xtrade-supervisor xtrade-bridge xtrade-scanner.timer` | After `/etc/xtrade/env` edit |
| Disable everything (uninstall keeps state) | `sudo bash scripts/phase4/uninstall_vps.sh` | Decommission |

`xtrade ops pause` writes `/run/xtrade/paused.flag` (a tmpfs sentinel,
gone on reboot). To make the pause survive a reboot, also
`sudo systemctl disable xtrade-supervisor`.

---

## 5. 扩盘 (adding a venue or instrument)

Phase 4 freezes the venue matrix at Phase 3 state (per brief §1
non-mission #7). 扩盘 here means **rotating** the existing venue's
instrument list, not introducing a new venue.

1. Edit `/etc/xtrade/universe.yaml` on the VPS.
2. Validate locally first if possible:
   ```bash
   sudo -u xtrade /opt/xtrade/.venv/bin/xtrade scan list --config /etc/xtrade
   ```
3. Restart the scanner timer so the next tick picks the new universe:
   ```bash
   sudo systemctl restart xtrade-scanner.timer
   sudo systemctl start xtrade-scanner.service   # one-shot, run-now
   ```
4. Supervisor watches `/var/lib/xtrade/signals/` and will pick up
   matching signals on its 2 s poll; no restart needed.

To add a venue, defer to Phase 5 (brief §3 non-mission item).

---

## 6. State maintenance — 90 天 signals 归档

Per brief §2 T3, `scripts/phase4/02_state_gc.py` moves jsonl shards
older than 90 days from `signals/` to `archive/signals/`. Run weekly
from cron or by hand:

```bash
# dry-run first
sudo -u xtrade /opt/xtrade/.venv/bin/python3 \
    /opt/xtrade/current/scripts/phase4/02_state_gc.py --dry-run

# actually move
sudo -u xtrade /opt/xtrade/.venv/bin/python3 \
    /opt/xtrade/current/scripts/phase4/02_state_gc.py
```

The script:
- only touches files matching `YYYY-MM-DD.jsonl` under
  `/var/lib/xtrade/signals/`
- never touches `approvals/`, `logs/`, `catalog/`, or the `.cursor`
  file
- moves with `os.replace` (atomic) — a crash mid-run leaves no
  half-written shard
- refuses to overwrite an existing archive target (exit 1 + error
  line, but other files still archive)

Suggested crontab (under the `xtrade` user):

```
# weekly state GC, 04:17 Mondays UTC
17 4 * * 1  /opt/xtrade/.venv/bin/python3 /opt/xtrade/current/scripts/phase4/02_state_gc.py >> /var/lib/xtrade/logs/state_gc.log 2>&1
```

---

## 7. 故障恢复演练 (Drills 1–4)

Drill format follows brief §5 Task 6 template verbatim. Each drill has
a helper script under `scripts/phase4/`; the script is the
*recommended* trigger so the assertions and cleanup traps are
identical between operators.

The DESTRUCTIVE drills (2, 3, 4) all default to dry-run and require
`--commit` to actually mutate state. Drill 1 is non-destructive
(supervisor is restarted automatically by systemd).

---

### Drill 1 — SIGKILL supervisor

**预期**: supervisor 在 30s 内进入 active(running)，cursor 恢复，
无重复下单 (对比 approvals jsonl)。

**准备**:
- supervisor 当前 `active`
- 最近一条 approval 在 `/var/lib/xtrade/approvals/$(date -u +%F).jsonl`

**触发**:
```bash
sudo bash /opt/xtrade/current/scripts/phase4/01_drill_sigkill.sh
```

The script snapshots `xtrade ops status --json` + approvals jsonl line
count pre-kill, sends `systemctl kill -s SIGKILL
xtrade-supervisor.service`, polls `systemctl is-active` for 30 s,
and asserts the approvals jsonl length is unchanged across the kill.

**观察**:
```bash
sudo journalctl -u xtrade-supervisor.service -n 50 --no-pager
sudo -u xtrade /opt/xtrade/.venv/bin/xtrade ops status --json
```

**判据**:
- `01_drill_sigkill.sh` exits 0
- `systemctl is-active xtrade-supervisor.service` == `active`
  within 30 s of the kill
- `wc -l < approvals/<today>.jsonl` unchanged across the kill (no
  duplicate row)
- `xtrade ops status` shows `last_cursor_update_age_s` smaller than the
  outage window (supervisor resumed work, not just respawned and
  idled)

**通过/未通过**: (实测后填)

**日志样本**: (粘贴 `journalctl -u xtrade-supervisor.service` 中
从 `Killing` 到下一次 `cursor advanced` 的区间)

---

### Drill 2 — cgroup OOM (assert-only by default)

**预期**: 人为占内存触发 OOM → 验证 xtrade-supervisor 被 kill
而不是 openclaw / hermes (凭 OOMScoreAdjust=500)。

**准备**:
- cgroup hardening 必须已生效（脚本先 assert，pass 才允许往下走）

**触发** (assert-only — what 03 actually does):
```bash
sudo bash /opt/xtrade/current/scripts/phase4/03_drill_oom.sh
```

The script does **not** trigger OOM on its own — driving the kernel
OOM killer on a live VPS that hosts openclaw + hermes wants a human
finger on the off-switch. It instead asserts at runtime that:
- `MemoryMax` ≈ 1500M (regex on bytes value)
- `MemorySwapMax` == 0
- `OOMScoreAdjust` == 500
- `Restart` == on-failure

then prints the manual trigger commands below.

**触发** (manual — only when you want to actually exercise the
killer; PRODUCTION operators run this with eyes on `journalctl -f`
for all three services):
```bash
# In the supervisor's own cgroup, allocate 2 GB > MemoryMax → kernel
# selects the process with the highest OOMScoreAdjust (xtrade-supervisor).
pid=$(systemctl show -p MainPID --value xtrade-supervisor.service)
cg=/sys/fs/cgroup/$(cut -d: -f3 </proc/$pid/cgroup | sed 's|^/||')
sudo -u xtrade python3 -c '
import os, sys
open(os.environ["CG"] + "/cgroup.procs","w").write(str(os.getpid()))
data = bytearray(2_000_000_000)
print("allocated", file=sys.stderr); input()
' CG="$cg" &
```

**观察**:
```bash
sudo journalctl -u xtrade-supervisor.service --since "5 minutes ago" --no-pager
sudo journalctl -u openclaw.service           --since "5 minutes ago" --no-pager
sudo journalctl -u hermes.service             --since "5 minutes ago" --no-pager
sudo journalctl -k --since "5 minutes ago" --no-pager | grep -iE 'oom|out of memory'
```

**判据**:
- `03_drill_oom.sh` exits 0 (cgroup config matches)
- Kernel OOM log line targets the supervisor's cgroup, **not**
  openclaw / hermes
- `systemctl is-active openclaw.service` and `hermes.service` remain
  `active` throughout
- supervisor returns to `active` within `RestartSec=10s` window

**通过/未通过**: (实测后填)

**日志样本**: (粘贴 `journalctl -k` 的 oom-kill 行 +
`journalctl -u xtrade-supervisor.service` 中 Restart 行)

---

### Drill 3 — 网络抖断 (Binance 30s outage)

**预期**: TradingNode 心跳告警 + 不下错单 + 网络恢复后自动续订阅。

**准备**:
- `iptables` 可用 (脚本检查)
- supervisor 当前 `active`

**触发**:
```bash
# Dry-run (recommended before the first real attempt):
sudo bash /opt/xtrade/current/scripts/phase4/04_drill_network.sh

# Real:
sudo bash /opt/xtrade/current/scripts/phase4/04_drill_network.sh --commit
```

The script resolves `api.binance.com` + `fapi.binance.com` to IPv4,
inserts `iptables -I OUTPUT -d <ip> -j DROP -m comment --comment
xtrade-drill` for each, sleeps 30 s (override via `--drop-s`),
removes the rules, and polls for TCP/443 connectivity recovery. A
`trap cleanup EXIT INT TERM` restores connectivity even on SIGINT.

**观察**:
```bash
sudo journalctl -u xtrade-supervisor.service --since "2 minutes ago" --no-pager \
    | grep -iE 'heartbeat|reconnect|subscribe|disconnect'
sudo -u xtrade /opt/xtrade/.venv/bin/xtrade ops status --json
```

**判据**:
- `04_drill_network.sh` exits 0
- supervisor stays `active` throughout (no Restart)
- supervisor journal shows TradingNode heartbeat warnings during the
  outage window, then `subscribe` resumed within 30 s of rule removal
- no orders submitted during the outage (compare approvals jsonl line
  count pre/post — script does not assert this automatically, operator
  to spot-check)

**通过/未通过**: (实测后填)

**日志样本**: (脚本 `[drill-network]` 段 + supervisor journal during
the outage window)

---

### Drill 4 — openclaw 5xx / 不可达

**预期**: openclaw 停机 ≥ 1min, bridge 多次 5xx → supervisor
暂停 dispatch, 切换本地 `xtrade approve confirm` 兜底。

**准备**:
- openclaw + supervisor 当前都 `active`
- 操作员清楚如何手工 confirm approvals (本地兜底路径)

**触发**:
```bash
# Dry-run:
sudo bash /opt/xtrade/current/scripts/phase4/06_drill_openclaw_outage.sh

# Real (default OUTAGE_S=60; brief specifies 5min — pass --outage-s 300
# to match exactly, but 60s is safer for an unattended run):
sudo bash /opt/xtrade/current/scripts/phase4/06_drill_openclaw_outage.sh --commit
sudo bash /opt/xtrade/current/scripts/phase4/06_drill_openclaw_outage.sh --commit --outage-s 300
```

The script: snapshots `ops status`, stops openclaw, sleeps
`OUTAGE_S/2`, samples `ops status` mid-outage, sleeps the rest, starts
openclaw, asserts both services returned to `active`. A `trap`
guarantees openclaw is restarted on any exit (success, fail, SIGINT).

**观察**:
```bash
sudo journalctl -u xtrade-bridge.service     --since "5 minutes ago" --no-pager
sudo journalctl -u xtrade-supervisor.service --since "5 minutes ago" --no-pager
sudo -u xtrade /opt/xtrade/.venv/bin/xtrade ops status --json
sudo -u xtrade /opt/xtrade/.venv/bin/xtrade approve list --pending
```

**判据**:
- `06_drill_openclaw_outage.sh` exits 0
- supervisor stayed `active`; openclaw returned to `active`
- `xtrade ops status` mid-outage shows `bridge.last_dispatch_ok=false`
  *or* no dispatches were attempted (both acceptable; the script fails
  only if `last_dispatch_ok=true` during the outage — that would be a
  fabricated record)
- Any approvals that hit `dispatch_failed` during the outage can be
  cleared via:
  ```bash
  sudo -u xtrade /opt/xtrade/.venv/bin/xtrade approve list --pending
  sudo -u xtrade /opt/xtrade/.venv/bin/xtrade approve confirm <id> \
      --note "openclaw outage manual"
  ```

**通过/未通过**: (实测后填)

**日志样本**: (`xtrade-bridge` journal 中 5xx/retry 行 + 任意
`dispatch_failed` approval row)

---

## 8. 应急参考

| 症状 | 排查命令 | 兜底操作 |
|---|---|---|
| `xtrade ops status` 卡住 | `systemctl status xtrade-supervisor` + `journalctl -u xtrade-supervisor -n 200` | `xtrade ops pause` → 排查 → `xtrade ops resume`；最后才 `xtrade ops kill --yes` |
| approvals 队列堆积 | `xtrade approve list --pending` | `xtrade approve confirm <id>` 或 `xtrade approve reject <id> --reason "..."` |
| openclaw 不可达持续 > 5min | 见 Drill 4 | 本地 `xtrade approve confirm` 兜底；不要在 yuanbao 端尝试，回执无法到达 |
| supervisor 反复 OOM 重启 | `journalctl -u xtrade-supervisor -k` + `systemctl show -p MemoryMax --value xtrade-supervisor.service` | 临时 `MemoryMax=2000M` (drop-in `/etc/systemd/system/xtrade-supervisor.service.d/oom.conf`)，记进 Phase 5 容量规划 |
| 磁盘吃紧 (`/var/lib/xtrade` > 80%) | `du -sh /var/lib/xtrade/*` | 运行 §6 state GC；最后才动 `logs/` (logrotate 已管) |

---

## 9. 参考

- Phase 4 brief: `docs/phase4_brief.md`
- Phase 3 testnet runbook (supervisor 拉起的同一条链路): `docs/phase3_runbook_testnet.md`
- systemd cgroup v2: https://www.freedesktop.org/software/systemd/man/systemd.resource-control.html
- systemd hardening: https://www.freedesktop.org/software/systemd/man/systemd.exec.html
