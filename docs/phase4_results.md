# Phase 4 执行结果报告

> 编制日期：2026-05-24
> 上游依据：`docs/phase4_brief.md`
> 目标仓库：`/Users/bitcrab/xtrade`
> 执行人：Claude Code（Opus 4）
> 状态：**T1–T5、T8 PASS（offline 部分）；T6 / T7 标记 PENDING-VPS（剧本与协议交付完成，等待 VPS 上线后操作员实测填回）**

---

## 0. 总览

Phase 4 的使命是把 Phase 3 的本地策略闭环搬到 Singapore VPS，并将
manual 审批通道从 `xtrade approve confirm` CLI 桥接到 yuanbao（经
openclaw Webhooks plugin）。本阶段**不**容器化、**不**接 Telegram /
Grafana / 数据库；交付物只是一组 systemd unit + 两个 HTTP 桥（出 +
入站）+ 一个 always-on 监督进程 + 运维 CLI + 4 个故障恢复演练
剧本 + runbook。

**结论：T1–T5 + T8 的 offline 交付物已全部入库且 `pytest tests/` →
647 passed / 7 skipped / 0 failed（647 - 446 = +201 个 Phase 4 用例，远超
brief §2 T8 "+30" 的要求）。T6 (drill scripts) 与 T7 (openclaw 联调)
的 offline 部分已就绪（drill 脚本 + runbook + 出入站协议），实际
VPS 执行需要操作员上线后填写剧本中的"通过/未通过"格。**

| ID  | 名称 | 状态 | 关键证据 |
|-----|------|------|---------|
| T1  | 无容器安装链 | PASS | `scripts/phase4/install_vps.sh` + `uninstall_vps.sh` + 4 个 `deploy/systemd/*.service.in` 模板；`tests/test_deploy_install_vps.py`（`bash -n` + executable + strict-mode + 可选 shellcheck，覆盖 7 个脚本）；`tests/test_install_vps_dryrun.py` 与 `tests/test_systemd_unit_render.py` envsubst 输出 vs golden（commit `493690a`） |
| T2  | systemd 单元矩阵 | PASS（offline 渲染） | `deploy/systemd/`：`xtrade-supervisor.service.in`（`MemoryMax=1500M` / `MemorySwapMax=0` / `CPUQuota=80%` / `OOMScoreAdjust=500` / `Restart=on-failure RestartSec=10s` / `ProtectSystem=strict`）、`xtrade-bridge.service.in`（`MemoryMax=200M` / `IPAddressAllow=127.0.0.0/8` + `IPAddressDeny=any`）、`xtrade-scanner.service.in` + `xtrade-scanner.timer.in`（5 min 周期，`MemoryMax=600M`）；`SyslogIdentifier` 分别为 `xtrade.supervisor` / `xtrade.bridge` / `xtrade.scanner`，与 brief §8 标签集对齐（commit `493690a`，scanner CLI flag 修正 `64c8757`） |
| T3  | 状态目录 + 日志切割 | PASS | `install_vps.sh` 建立 `/opt/xtrade/{releases,.venv,current}` / `/etc/xtrade/` / `/var/lib/xtrade/{catalog,signals,approvals,logs,archive}` 三段权限；`deploy/logrotate/xtrade` 由 install 脚本部署；`scripts/phase4/02_state_gc.py` 提供 90 天 signals jsonl → `/var/lib/xtrade/archive/signals/` 的归档（atomic `os.replace`，`tests/test_state_gc.py` 20 用例）（commit `a1da63d`） |
| T4  | yuanbao 审批桥（经 openclaw） | PASS | **出站** `src/xtrade/bridge/openclaw_webhook.py::OpenclawBridge`：4 次指数回退（1/2/4/8 s）、4xx 不重试、5xx + 网络错误重试；失败写 `dispatch_failed`，**不**改 `status`，本地 `xtrade approve confirm` 仍可兜底；payload 序列化前过 `scrub_payload_for_secrets`。**入站** `src/xtrade/bridge/inbound.py`：`http.server.ThreadingHTTPServer` 绑 `127.0.0.1:18080`；`Bearer <OPENCLAW_INBOUND_SECRET>` 常时间比较；TTL 服务器侧强制；handler 仅 confirm / reject，幂等 409。`tests/test_bridge_openclaw_webhook.py` + `tests/test_bridge_schema.py` + `tests/test_bridge_inbound.py` 共覆盖 secret scrub、retry 时序、4xx 立即返回、TTL 过期、未授权、重复投递（commits `31273e7` outbound、`0d3172a` inbound） |
| T5  | always-on supervisor | PASS | `src/xtrade/live/supervisor.py::run_supervisor`：长驻 polling loop（默认 2 s）、`SignalConsumer` cursor 持久化（崩溃重启不回放）、manual intent 入 in-process `pending` 映射并经 `bridge.dispatch` 推 openclaw；`/run/xtrade/paused.flag` sentinel 暂停新单（仍 drain 已派回执）；`xtrade live supervise --config ...` CLI 入口；`tests/test_supervisor_sentinel.py` + `tests/test_supervisor_cursor.py` + `tests/test_supervisor_manual_bridge.py` 共 72 用例覆盖 cursor 恢复、pause/resume、bridge dispatch 失败、pending 回执 promotion（commit `b925f30`） |
| T6  | 故障恢复演练 | **offline 交付 PASS / VPS 执行 PENDING-VPS** | 4 个 drill 脚本入库：`scripts/phase4/01_drill_sigkill.sh`（active drill，approval-count 不变性）、`scripts/phase4/03_drill_oom.sh`（assert-only：cgroup 字段 + 24h journal OOM 扫描 + 手工触发命令）、`scripts/phase4/04_drill_network.sh`（30 s `iptables OUTPUT DROP` + cleanup trap，DRY-RUN 默认）、`scripts/phase4/06_drill_openclaw_outage.sh`（`systemctl stop openclaw` N s + cleanup trap，DRY-RUN 默认）；`docs/phase4_runbook_vps.md` 收录每个 drill 的 预期 / 准备 / 触发 / 观察 / 判据 / 通过-未通过 / 日志样本 模板与对应触发命令；`tests/test_phase4_runbook.py` 与 `tests/test_deploy_install_vps.py` 静态守护脚本与文档结构（commit `a1da63d`）。**实际 4 个 drill 的 VPS 执行结果待操作员上线后填回，见 §1.6**。 |
| T7  | 运维 CLI 与状态汇总 | PASS（CLI），**openclaw 联调 PENDING-VPS** | `src/xtrade/ops/status.py::collect_status`：纯文件系统读取（sentinel / cursor / approvals jsonl tail / 最近 `logs/<run-id>/live_signal_summary.json` / 最近 bridge dispatch 注解 / 可选 `systemctl show` 探活），supervisor 崩溃时仍可用；`xtrade ops {status,pause,resume,kill}` 4 个子命令，`status --json` 单行 grep 友好；`tests/test_cli_ops.py`（24 用例）覆盖 collector、renderer、`--yes` 守护、systemctl mock；openclaw 联调（出入站全链 + yuanbao 端 TaskFlow）需 VPS + openclaw 操作员协同，见 §1.7（commit `63a9ebd`） |
| T8  | 测试与可观测性 | PASS | `pytest tests/` → **647 passed / 7 skipped**（skips = shellcheck 未装时的 4 项 + 几个 deprecation marker）；Phase 4 累计新增 ≈ 201 用例（brief 预期 ≥ 30）。**结构化 log 单行 JSON**：`src/xtrade/obs/log_event.py` 提供 `emit_event(logger, event, **fields)` 公共助手，envelope `{"ts": "...Z", "level": "...", "event": "...", ...}`；supervisor / bridge.out / bridge.in 全部三块已转换；`tests/test_log_event.py`（17 用例）锁定 envelope 形状、header 顺序、Decimal/Path/datetime coercion、跨模块 event 命名空间（`supervisor.*` / `bridge.out.*` / `bridge.in.*`）。 |

Phase 4 通过条件：**T1–T5 + T7 (CLI) + T8 全 PASS（offline）；T6 与 T7 (openclaw 联调) 的 VPS 实测部分 PENDING-VPS，剧本与协议已就绪**。当 §1.6 / §1.7 由操作员填入实测证据后，Phase 4 即可整体签字进入 Phase 5。

---

## 1. 各任务交付与证据

### 1.1 Task 1 —— 无容器安装链与 systemd 模板（T1, T2, T3）

- `scripts/phase4/install_vps.sh`（commit `493690a`，scanner flag 微修 `64c8757`）：
  - 拒绝非 root；幂等创建系统用户 / 组 `xtrade`；`uv python install 3.12` → `/opt/xtrade/.venv`；`uv pip install -e /opt/xtrade/current`。
  - `envsubst` 渲染 4 个 unit + 1 个 timer（变量白名单 `$SUBST_VARS`，防止系统 `%i` token 被吞）。
  - `systemctl daemon-reload` + `enable --now`；`logrotate.d/xtrade` 部署；`systemd-tmpfiles` 挂 `/run/xtrade` tmpfs。
  - 退出码 0 = 全 unit `active`；1 = prereq 失败；2 = unit 启动失败并 dump journalctl 尾巴。
- `scripts/phase4/uninstall_vps.sh`：`disable --now` 全 unit + 删 `/etc/systemd/system/xtrade-*`；默认**保留** `/var/lib/xtrade`，`--purge` 才删。`test_deploy_install_vps.py` 静态守护 purge 块边界。
- 验证：
  - `tests/test_deploy_install_vps.py`：7 个脚本 × {`bash -n`, executable, strict-mode, optional shellcheck}，加上 install 脚本的 `--release-tarball` 文档检查 / `uv install 3.12` 资源回收 / envsubst 变量白名单。
  - `tests/test_install_vps_dryrun.py`：跑 install 脚本到 envsubst 渲染后立即 stop，对比 4 个 unit 与 1 个 timer 的渲染输出（golden file）。
  - `tests/test_systemd_unit_render.py`：给定确定 env，每个 `*.service.in` 模板的 envsubst 输出与 fixture 严格相等。

### 1.2 Task 2 —— `OpenclawBridge` 出站（T4 出）

`src/xtrade/bridge/openclaw_webhook.py`（commit `31273e7`）：

- `dispatch(record) -> DispatchResult`：构造 `BridgePayload`（`action="create_flow"` / `goal` / `metadata.intent` / `metadata.callback.{confirm_url,reject_url,ttl_s}`） → `scrub_payload_for_secrets` → `httpx.post`（5 s connect / 10 s read）。
- 重试矩阵：网络错误 + HTTP 5xx → exponential backoff `[1, 2, 4, 8]` s（最多 4 次）；HTTP 4xx 立即返回 `DispatchResult(ok=False)`。
- 失败终态调 `ApprovalQueue.annotate_dispatch_failure(record_id, reason, attempts, last_status)`，**不**改 `status`，本地 CLI 仍可 confirm。
- 凭据扫描复用 Phase 3 `_scan_metadata_for_secrets`，命中即拒绝发送。
- 单测 `tests/test_bridge_openclaw_webhook.py` 用 httpx `MockTransport` 覆盖 200 / 500 / 502 / timeout / 4xx / secret-leak / 重试时序 / 失败后 annotation 写入。

### 1.3 Task 3 —— bridge 入站（T4 入）

`src/xtrade/bridge/inbound.py`（commit `0d3172a`）：

- `http.server.ThreadingHTTPServer` 手写 handler；两个路由 `POST /approvals/<id>/{confirm,reject}`。
- 安全包络：绑 `127.0.0.1`；body 上限 4 KiB；`Authorization: Bearer <OPENCLAW_INBOUND_SECRET>` 走 `hmac.compare_digest`；TTL 服务器侧强制（older than `ttl_s` 返回 404，强迫 openclaw 重派而非重新满足陈旧 approval）。
- 幂等：已 decided 的 row 重投返 409 + JSON `{"code":"already_decided","status":"confirmed|rejected"}`。
- 单测 `tests/test_bridge_inbound.py` 覆盖：合法 confirm / reject、未授权 401、超大 body 413、TTL 过期 404、id 不存在 404、重复 confirm 409、JSON malformed 400。

### 1.4 Task 4 —— always-on supervisor（T5）

`src/xtrade/live/supervisor.py`（commit `b925f30`）：

- `run_supervisor(config, *, stop_event=None, max_iterations=None, live_executor=..., now=..., logger=...)`：可注入 stop_event（systemd SIGTERM → 优雅退出）+ 测试用 max_iterations + live_executor 桩。
- 每 iteration：
  1. **Drain pending decisions**：扫 in-process `pending` 映射，每个 `record_id` 在 ApprovalQueue 中找 manual 行；`confirmed` → `_submit_intent`，`rejected` → drop。
  2. **Pause 短路**：`Sentinel(/run/xtrade/paused.flag)` paused 时不消费新 signal，但 drain 仍跑（避免 paused supervisor "忘掉" 已派发到 yuanbao 的回执）。
  3. **消费新信号**：`SignalConsumer.iter_new()` → strategy → RiskGate → ApprovalGate → 按 mode 分流（`dry_run` 记审计、`auto` 直接 `_submit_intent`、`manual` 入 `pending` + `bridge.dispatch`）。
  4. **Commit cursor**：仅在整批信号处理完成后 commit，崩溃中段重启重放整批（ApprovalGate `(fingerprint, mode)` 幂等保证不重复下单）。
- 错误隔离：strategy / intent / dispatch 任一抛异常都被 catch 并记入 `SupervisorIterationResult.errors`，**绝不**让 iteration loop 退出。
- 单测：`test_supervisor_sentinel.py`（pause / resume / drain-while-paused）、`test_supervisor_cursor.py`（注入 signals → 模拟 kill → 重启 → 不回放）、`test_supervisor_manual_bridge.py`（manual 模式 + bridge dispatch ok / 4xx / 5xx → `pending` 状态机正确演进）。

### 1.5 Task 5 —— `xtrade ops` 子命令（T7 CLI 部分）

`src/xtrade/ops/`（commit `63a9ebd`）：

- `collect_status(paths, *, now=None, probe_systemd=None) -> OpsStatus`：纯文件系统读取，6 个数据源：
  - `OpsPaths.sentinel_path` → paused 状态。
  - `OpsPaths.cursor_path` → 最近 signal 处理时间 (lex-max of `seen` rows)。
  - `OpsPaths.approvals_root/<today>.jsonl` → pending 行计数（迭代 `ApprovalQueue`）。
  - `OpsPaths.logs_root` → 最近 `live_signal_summary.json` 的 mtime + run_id + approval.record_id。
  - 最近 approval 行的 `dispatch` 注解（latest by ISO `dispatched_at`）。
  - 可选 `subprocess.run(["systemctl", "show", "-p", "ActiveState", ...])` 探活（macOS dev host 无 systemctl 即返 `SupervisorState(state="unknown")`，测试 monkey-patch）。
- `render_status_json` / `render_status_text`：text 第一行为 grep 友好单行汇总 (`xtrade.ops state=... paused=... ...`)，后续行展开 supervisor / bridge / counts 三块。
- 4 个 CLI 子命令：
  - `status [--json]` — 纯只读。
  - `pause [--reason ...]` — atomic write `/run/xtrade/paused.flag`（content = JSON `{paused_at, reason}`）。
  - `resume` — `unlink` sentinel。
  - `kill --yes` — `subprocess.run(["systemctl", "stop", unit])`；缺 `--yes` 立即报错；处理 `FileNotFoundError`（host 无 systemctl 时给操作员可读 message 而非 traceback）。
- 单测 `tests/test_cli_ops.py`（24 用例）：collector × {empty, sentinel, cursor, corrupt cursor, pending count, latest summary by mtime, corrupt summary, dispatch annotation latest-of-many, default probe signature} + renderers × {JSON key set, text one-liner ordering} + CLI × {status text/json, pause idempotent, resume noop, kill `--yes` 拒空, kill happy path mock, kill missing systemctl, rich status full render}。

### 1.6 Task 6 —— 故障恢复演练剧本（T6）

Phase 4 brief §5 Task 6 列出的 4 个 drill，全部在 `scripts/phase4/` +
`docs/phase4_runbook_vps.md` 落地（commit `a1da63d`）：

| Drill | 脚本 | 模式 | 关键不变性 / assertion |
|---|---|---|---|
| 1. SIGKILL supervisor | `01_drill_sigkill.sh` | active | 发 `systemctl kill -s SIGKILL`；poll `is-active` 30 s 内 → active；approvals jsonl 行数前后**严格相等**（捕获"重启后重复下单"回归）。 |
| 2. cgroup OOM | `03_drill_oom.sh` | assert-only | `systemctl show -p MemoryMax/MemorySwapMax/OOMScoreAdjust/Restart` 与 brief 字段匹配；扫 24 h 内核 journal 中 OOM 行；**不**主动制造内存压力（在共存 openclaw + hermes 的 VPS 上需要操作员手动触发，脚本打印 cgroup.procs 注入命令）。 |
| 3. 网络抖断 | `04_drill_network.sh` | DRY-RUN / `--commit` | `getent ahostsv4 api.binance.com fapi.binance.com` → `iptables -I OUTPUT -d <ip> -j DROP -m comment --comment xtrade-drill`；sleep 30 s；移除规则；poll TCP/443 连通性 30 s 内恢复；supervisor 全程 `active`。`trap cleanup EXIT INT TERM` 保证 SIGINT 中断也复原网络。 |
| 4. openclaw 5xx | `06_drill_openclaw_outage.sh` | DRY-RUN / `--commit` | `systemctl stop openclaw`；OUTAGE_S/2 后采样 `xtrade ops status --json` 中 `bridge.last_dispatch_ok`；OUTAGE_S 满后 `systemctl start openclaw`；assert 两服务都恢复 active 且 mid-sample 不是 `true`。`trap cleanup` 保证退出时 openclaw 必定 start。 |

测试与 lint：
- `tests/test_deploy_install_vps.py` 扩展 `SCRIPTS` 列表覆盖 4 个新脚本（`bash -n` + +x + strict-mode + optional shellcheck）。
- `tests/test_phase4_runbook.py` 8 用例锁定 runbook 结构：每个 drill 的 7 段模板（预期/准备/触发/观察/判据/通过未通过/日志样本）出现 ≥ 4 次；`xtrade approve confirm` 兜底路径被记入 drill 4；脚本名 1/3/4/6 全部被 runbook 引用。

**VPS 执行待填**（每个 drill 在操作员上线后用以下模板补全到本节）：

```
#### Drill N — 实测 2026-XX-XX (run_id …)
- 触发命令：(粘贴脚本输出 + 退出码)
- 观察：
  - systemctl is-active xtrade-supervisor.service: ...
  - journalctl -u xtrade-supervisor.service -n 50 摘要: ...
  - xtrade ops status --json 前后快照: ...
- 通过/未通过: PASS / FAIL（含原因）
- approval jsonl 关键行: ...
```

### 1.7 Task 7 —— openclaw 联调（T4 联调 + T7 联调）

xtrade 侧 offline 部分 PASS：

- `/etc/xtrade/env` 模板（`deploy/env/xtrade.env.example`）列全 `OPENCLAW_GATEWAY` / `OPENCLAW_SHARED_SECRET` / `OPENCLAW_INBOUND_SECRET`。
- 出站协议：`docs/phase4_brief.md` §10 已固化 URL / Header / Body schema，xtrade 端实现严格按此（`tests/test_bridge_schema.py` 锁字段）。
- 入站协议：localhost:18080 + Bearer，返回码 200 / 401 / 404 / 409 / 413 / 400 全部按 §10 规约（`tests/test_bridge_inbound.py` 锁）。

VPS 执行 PENDING-VPS（操作员上线后填回）：

- openclaw `openclaw.json` 的 `plugins.entries.webhooks.config.routes` 是否已增加 `xtrade` 路由（`controllerId: "webhooks/xtrade"`）？
- openclaw TaskFlow `xtrade-approval` 流程是否上线？解析 body → yuanbao push → 接收回复 → 回调 `127.0.0.1:18080`？
- 端到端 testnet 手测：一次完整 manual signal-run（信号 → bridge 出 → openclaw → yuanbao → 用户 confirm → openclaw 回执 → bridge 入 → ApprovalQueue patch → supervisor 提交 testnet 限价单 → cancel）。

待操作员上线后补：xtrade 与 openclaw 两侧的 journalctl 摘要、一条 approval jsonl 完整生命周期、yuanbao 截图（可选）。

### 1.8 Task 8 —— 测试与可观测性（T8）

#### 测试

`pytest tests/` 在本仓库当前 commit 下：

```
647 passed, 7 skipped in 45.57s
```

skipped 全部来自 shellcheck 未安装（CI / VPS 上会自动跑）或 deprecation
marker。Phase 4 累计新增测试文件（按贡献顺序）：

| 文件 | 用例数 | 覆盖 |
|---|---:|---|
| `tests/test_install_vps_dryrun.py` | n | T1 install bash -n + dry-run |
| `tests/test_systemd_unit_render.py` | n | T2 envsubst golden |
| `tests/test_deploy_install_vps.py` | n | T1+T6 脚本静态检查（含 4 个 drill） |
| `tests/test_bridge_schema.py` | n | T4 payload schema |
| `tests/test_bridge_openclaw_webhook.py` | n | T4 出站重试/凭据扫描 |
| `tests/test_bridge_inbound.py` | n | T4 入站协议 |
| `tests/test_supervisor_cursor.py` | n | T5 cursor 恢复 |
| `tests/test_supervisor_sentinel.py` | n | T5 pause/resume |
| `tests/test_supervisor_manual_bridge.py` | n | T5 + T4 联动 |
| `tests/test_cli_ops.py` | 24 | T7 CLI + collector |
| `tests/test_state_gc.py` | 20 | T3 jsonl 归档 |
| `tests/test_phase4_runbook.py` | 8 | T6 runbook 结构 |
| `tests/test_log_event.py` | 17 | T8 JSON envelope |

合计 Phase 4 增量 **远超 brief §2 T8 的 +30 阈值**。

#### 可观测性

每个 systemd unit 在 `deploy/systemd/*.service.in` 中设 `SyslogIdentifier`：

| Unit | SyslogIdentifier | Python logger |
|---|---|---|
| `xtrade-supervisor.service` | `xtrade.supervisor` | `xtrade.supervisor` |
| `xtrade-bridge.service` (inbound) | `xtrade.bridge` | `xtrade.bridge.in` |
| `xtrade-bridge.service` (outbound, in supervisor process) | (via supervisor unit) | `xtrade.bridge.out` |
| `xtrade-scanner.service` | `xtrade.scanner` | (delegates to `run_scan`; writes `scan_summary.json`) |

**所有结构化事件经 `src/xtrade/obs/log_event.py::emit_event` 走单行 JSON
envelope**：

```json
{"ts":"2026-05-24T12:34:56Z","level":"INFO","event":"supervisor.start","instrument":"BTCUSDT-PERP.BINANCE","mode":"manual","pending":0,"signals_root":"/var/lib/xtrade/signals","strategy":"momentum_follow"}
```

事件命名空间（`tests/test_log_event.py::test_*_event_names_documented`
强制守护）：

- `supervisor.start` / `supervisor.stop` / `supervisor.iteration.paused` / `supervisor.iteration.crash` / `supervisor.strategy.crash` / `supervisor.intent.{crash,risk_rejected,dry_run,queue_miss,parked}` / `supervisor.pending.{promoted,rejected,submit_failed}` / `supervisor.bridge.dispatch{,.unhandled}`
- `bridge.out.dispatch_ok` / `bridge.out.dispatch_retry` / `bridge.out.dispatch_failed` / `bridge.out.refused`
- `bridge.in.start` / `bridge.in.stop` / `bridge.in.request`

操作员检索（runbook §8）：

```bash
journalctl -u xtrade-supervisor.service --since "10 min ago" -o cat | jq -c .
journalctl -u xtrade-bridge.service --since "1 hour ago" -o cat | jq 'select(.event=="bridge.in.request" and .status>=400)'
```

---

## 2. 与 Phase 3 / Phase 3.5 的差异

| 维度 | Phase 3 (本地) | Phase 4 (VPS) |
|---|---|---|
| 执行宿主 | macOS dev box | Singapore VPS / OpenCloudOS 9.4，与 openclaw + hermes 同机 |
| 进程模型 | one-shot `run_live_signal` / `run_paper` | always-on `run_supervisor`，systemd 拉起 |
| 资源隔离 | 无 | systemd cgroup v2：`MemoryMax` / `MemorySwapMax=0` / `CPUQuota` / `OOMScoreAdjust` |
| 审批通道 | `xtrade approve {list,confirm,reject}` CLI | manual mode 走 openclaw → yuanbao；CLI 仍是本地兜底路径 |
| 状态目录 | `data/{signals,approvals,catalog,logs}/` | `/var/lib/xtrade/{...}` + `/run/xtrade/paused.flag` tmpfs |
| 配置目录 | `config/` + 环境变量 | `/etc/xtrade/{env,supervisor.yaml,venues.*.yaml,...}` (`640 root:xtrade`) |
| 观测面 | stdout / `logs/<run-id>/*.json` | journalctl + 同上 jsonl/json artefacts + 结构化 JSON 单行日志 |
| 网络面 | 仅 outbound 到 venue testnet | outbound 到 venue + openclaw；inbound localhost:18080 (openclaw 回执) |
| 自愈 | 操作员手动重跑 | systemd `Restart=on-failure RestartSec=10s`；4 个 drill 验证恢复时间窗口 |

不变量：venue 矩阵冻结在 Phase 3 状态（Binance Spot / Binance Futures /
Hyperliquid），mainnet 仍硬拒绝（`_assert_testnet_only`），RiskGate 仍
是强制单点。

---

## 3. 进入 Phase 5 的建议

T1–T5 + T8 全 PASS，T6 / T7 的 offline 交付已就绪。**进入 Phase 5 的
先决条件是在 VPS 上跑完 §1.6 与 §1.7 的 PENDING 部分**，把：

1. 4 个 drill 的实测通过 / 未通过 + journalctl 摘要填回 §1.6。
2. 一次完整 testnet manual 链路（signal → openclaw → yuanbao → 回执 →
   fill → cancel）的证据填回 §1.7。

如果以上两步在不可控 SLA 内未能完成（openclaw 端排期、yuanbao 路由
问题等），可按 brief §7 决策矩阵的 "T6 部分 fail" 行 "有条件 GO"：
最少要求 drill 1 (SIGKILL) 与 drill 3 (网络抖断) 在 VPS 上 PASS；
drill 2 (OOM) 与 drill 4 (openclaw 5xx) 允许暂时仅 offline 通过，并
把 SLA 项记入 Phase 5 监控强化项。

Phase 5 入口处建议补做的项（已识别但不阻塞 Phase 4 签字）：

1. **持久 TradingNode**：Phase 4 仍是"一 intent 一 node"（继承 Phase 3
   testnet runbook 的最稳定路径）；Phase 5 应迁到单进程持久 node，
   节省启动开销并支持持仓滚动。
2. **bridge 出站事件 → audit JSONL**：当前 `dispatch_failed` 是
   approval 行的注解；Phase 5 应额外写 `/var/lib/xtrade/audit/bridge_out.<date>.jsonl`，
   把所有 dispatch 尝试都落 audit。
3. **scanner 结构化日志**：scanner 当前只产出 `scan_summary.json` 文件
   artefact，运行时事件未走 `emit_event`；Phase 5 加 scanner-side
   `emit_event` 让 `journalctl -t xtrade.scanner | jq` 也可用。
4. **`/var/lib/xtrade` 容量告警**：state GC 已就位，但 VPS 上还需要一个
   `df` 守望（Phase 5 监控仪表盘里加一项即可，brief §1 已显式延后到
   Phase 5）。
5. **mainnet 三锁**：Phase 4 已在 `_assert_testnet_only` + `/etc/xtrade/env`
   中布两锁；Phase 5 上小资金 mainnet 时补第三锁（CLI 自闭锁 + Grafana
   alert + 操作员 OTP）。

---

## 4. 关键 commit / 文件清单

Phase 4 主线 commit：

```
0b13a7f (本提交) Phase 4 Task 8: structured JSON log envelope + results doc
a1da63d Phase 4 Task 6 (brief §5): fault-recovery drills + VPS runbook
63a9ebd Phase 4 Task 5 (brief §5): xtrade ops status / pause / resume / kill
0d3172a Phase 4 Task 3: openclaw inbound webhook receiver
b925f30 Phase 4 Task 5: always-on supervisor + sentinel
31273e7 Phase 4 Task 2: OpenclawBridge outbound HTTP dispatch
64c8757 Phase 4 Task 1: fix scanner unit CLI flags
493690a Phase 4 Task 1: VPS install chain + systemd unit templates
89341f8 Phase 4 brief: VPS deployment + yuanbao bridge via openclaw
```

新增源码：

```
src/xtrade/obs/{__init__.py,log_event.py}
src/xtrade/bridge/{__init__.py,openclaw_webhook.py,inbound.py,schema.py}
src/xtrade/live/{supervisor.py,sentinel.py}
src/xtrade/ops/{__init__.py,status.py}
```

新增脚本 / 部署模板：

```
scripts/phase4/{install_vps.sh,uninstall_vps.sh,01_drill_sigkill.sh,
                02_state_gc.py,03_drill_oom.sh,04_drill_network.sh,
                05_smoke_postdeploy.sh,06_drill_openclaw_outage.sh}
deploy/systemd/{xtrade-supervisor,xtrade-bridge,xtrade-scanner}.service.in
deploy/systemd/xtrade-scanner.timer.in
deploy/logrotate/xtrade
deploy/env/xtrade.env.example
```

文档：

```
docs/phase4_brief.md
docs/phase4_runbook_vps.md
docs/phase4_results.md   (本文件)
```

---

## 5. 签字

- Phase 4 offline 交付（T1–T5、T7 CLI、T8）：**PASS（2026-05-24，Claude Code Opus 4 执行）**。
- Phase 4 VPS 执行（T6 drills、T7 openclaw 联调）：**PENDING-VPS（操作员上线后填回 §1.6 / §1.7）**。

进入 Phase 5 的前提：§1.6 / §1.7 PENDING 项完成实测填回 → 在本文件
§5 追加 VPS 签字段（含日期、run_id、操作员姓名 / handle）后整体进入
Phase 5。
