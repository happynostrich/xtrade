# Phase 4 实施简报 —— 云端部署与 yuanbao 审批通道

> 编制日期：2026-05-23
> 目标仓库：`/Users/bitcrab/xtrade`
> 上游依据：
> - 主路线图：`xtrade_plan.md` §七 "Phase 4 — 云端部署与监控（1–2 周）"
> - Phase 3 收尾：`docs/phase3_results.md` §1.9（Phase 3.5 收尾签字，testnet runbook 已 2026-05-23 全 PASS）
> - 部署目标：腾讯云 Singapore VPS（2 CPU / 8 GB RAM / 80 GB SSD，OpenCloudOS 9.4，与 openclaw + hermes 同机共存）
> - 通知与审批通道：通过 openclaw 内置 Webhooks 插件桥接到 yuanbao（用户在上海，Telegram 受限不可达；yuanbao 已被 openclaw / hermes 原生支持）
> 执行方式：本简报交给 **Claude Code** 在 `/Users/bitcrab/xtrade` 中执行。

---

## 0. 进入 Phase 4 的前提（Phase 3 / Phase 3.5 结论）

Phase 3 已交付（参见 `docs/phase3_results.md`）：

- **策略契约 + RiskGate 强制单点 + 三档 ApprovalGate**（auto / manual / dry_run）。
- **paper + testnet 端到端**：T1–T8 全 PASS。
- **审批队列**：`data/approvals/<date>.jsonl`，CLI `xtrade approve {list,confirm,reject}`，行级原子 patch。
- **多 venue testnet**：Binance Spot + Binance Futures + Hyperliquid 三 venue 独立 yaml + `xtrade live health` 自动发现。

Phase 3.5 收尾增量：

- ApprovalQueue 幂等键从 `fingerprint` 收紧到 `(fingerprint, mode)`：dry_run 审计行不再 latch manual 审批通道。
- `--venues-yaml` 默认指向 gutted pointer 文件时按 `--instrument` 自动解析到 per-venue sibling。
- `pytest tests/` 446 用例全绿，offline 通过。
- testnet runbook 已由人手在 2026-05-23 实测：`live-20260523T030713Z`，approval `1de841fa7ed7a038` 手工 confirm，venue order `O-20260523-031036-001-000-1` accepted+canceled，全链路 PASS。

主路线图 §七 Phase 4 定位：

> **Phase 4 — 云端部署与监控（1–2 周）**
> 容器化；部署到云；接 Grafana 仪表盘、Telegram 告警与审批、熔断开关；做故障恢复演练。

**本简报对主路线图的两处调整（已与用户对齐）：**

1. **不容器化**。目标 VPS 与 openclaw + hermes 同机共存（2 CPU / 8 GB），引入 docker 仅是为了加 cgroup 隔离；OpenCloudOS 9.4 自带 cgroup v2，systemd `MemoryMax=` / `MemorySwapMax=` / `CPUQuota=` 给出等价隔离，避免再叠一层 daemon。
2. **不接 Telegram**。用户在上海，Telegram 不在可达通道；改走 yuanbao。xtrade 本身**不**直连 yuanbao SDK，而是把 yuanbao 当作 openclaw 的下游 channel：xtrade → HTTP POST → openclaw Webhooks plugin → openclaw TaskFlow → openclaw 大管家 → yuanbao。xtrade 侧零 vendor 耦合。
3. **不接 Grafana / Loki**。Phase 4 仅保证 `journalctl` + 现有 `logs/<run-id>/*.json` + 失败时 webhook 主动告警；可视化仪表盘延后到 Phase 5（届时若 openclaw / hermes 已提供共享 Grafana 实例则直接接入）。

---

## 1. Phase 4 的使命与非使命

**使命（Phase 4 要做的）**

1. **无容器部署到 Singapore VPS**：用 `uv` 安装独立 Python 3.12 与 venv（不污染系统 3.11.6），代码与状态分目录布局（`/opt/xtrade` 只读代码、`/var/lib/xtrade` 可写状态、`/etc/xtrade` 配置与凭据），全部由 systemd 拉起。
2. **systemd cgroup 隔离**：每个 xtrade 服务单元都设 `MemoryMax=` + `MemorySwapMax=0` + `CPUQuota=` + `OOMScoreAdjust=`，确保 OOM 时 xtrade 优先被 kill，不殃及 openclaw / hermes。
3. **always-on 监督进程**：8 GB RAM 解锁长跑的 `xtrade.live.supervisor`（Phase 3 是 one-shot runner，Phase 4 拉起一个可被 systemd 重启的长进程，监听 `data/signals/`、`data/approvals/` 触发动作）。
4. **scanner timer**：`xtrade scan run` 由 `systemd timer` 周期触发（不要 cron），写信号到 `/var/lib/xtrade/signals/`，supervisor 进程消费。
5. **yuanbao 审批通道（经 openclaw）**：在 manual 模式下，xtrade 把待审 intent 通过 HTTP POST 推给 openclaw 的 Webhooks 插件（路径 `/plugins/webhooks/xtrade`，Bearer 认证）；操作员在 yuanbao 中通过 openclaw 回执 confirm / reject，openclaw 反向回写 xtrade 的审批队列（机制见 §5 Task 4）。
6. **熔断开关与运维 CLI**：`xtrade ops {pause,resume,status,kill}` —— pause 写入 sentinel 文件，supervisor 进入 hold 模式（仍订阅市场数据，但不下任何单）；resume 清 sentinel；kill 走 systemd stop。
7. **故障恢复演练**：编排 4 个演练剧本（kill -9、OOM、网络抖断、openclaw 不可达），每个剧本给出预期行为与实际通过/未通过判据。
8. **可观测性最小集**：每个服务的 `journalctl` 标识、关键事件的结构化日志（JSON line）、`xtrade ops status` 一行汇总（uptime / last signal / last approval / last fill / pending count）。
9. **operator runbook**：`docs/phase4_runbook_vps.md`，包含安装、升级、回滚、停机、扩盘的命令序列。

**非使命（Phase 4 不做的）**

- **不接主网真实资金**：所有 venue 路径仍 testnet；Phase 5 才解锁 mainnet 双锁。
- **不容器化**：无 docker / podman / k8s。
- **不接 Telegram**。
- **不引入 Grafana / Loki / Prometheus / OpenTelemetry collector**：观测面用 journalctl + 现有 json summary。
- **不引入数据库**：仍 Parquet + jsonl；状态持久层不变。
- **不做多账户 / 多主机调度**：单 VPS、单账户。
- **不做新 venue / 新 instrument**：venue 矩阵冻结在 Phase 3 状态。
- **不做策略组合 / 仓位分配 / sizing 算法**：仍是"硬上限拒绝 + manual 审批"。
- **不在 VPS 上跑回测 / paper sweeps**：8 GB RAM 留给 supervisor + scanner + openclaw + hermes；研究类 backtest 仍在本地 mac 执行。

> 边界原则：Phase 4 把 Phase 3 的本地闭环搬到 Singapore VPS，并把审批通道从 `xtrade approve confirm` CLI 桥接到 yuanbao；它**仍然不是**真钱交易系统，但**是**一个可以 7×24 待命、人手在 yuanbao 一键放行的 testnet 自动化系统。

---

## 2. 验收标准（Go / No-Go 清单）

每项明确 PASS / FAIL，记入 `docs/phase4_results.md`。

| ID  | 名称 | 描述 |
|-----|------|------|
| T1  | 无容器安装链 | `scripts/phase4/install_vps.sh`：在干净 OpenCloudOS 9.4 上以非 root 系统用户 `xtrade` 执行；以 `uv python install 3.12` 安装独立解释器；建 `/opt/xtrade`（代码，root 拥有，755）、`/var/lib/xtrade`（状态，xtrade 拥有，700）、`/etc/xtrade`（配置 + 凭据，root 拥有 750，xtrade 组可读）；写入 4 个 systemd unit + 2 个 timer；脚本幂等可重跑、提供 `uninstall_vps.sh` 反向操作。 |
| T2  | systemd 单元矩阵 | `xtrade-supervisor.service`（always-on，`MemoryMax=1500M`、`MemorySwapMax=0`、`CPUQuota=80%`、`Restart=on-failure`、`RestartSec=10s`）、`xtrade-scanner.service` + `xtrade-scanner.timer`（每 5 分钟触发，`MemoryMax=600M`）、`xtrade-bridge.service`（接收 openclaw 回执，`MemoryMax=200M`），健康端口或 sentinel 文件可被外部探活。systemd unit 由仓库内 `deploy/systemd/*.service` 模板生成。 |
| T3  | 状态目录 + 日志切割 | `/var/lib/xtrade/{catalog,signals,approvals,logs}` 自动建立、权限正确；`logrotate.d/xtrade` 配置：`logs/*/run.log` 按 100 MB / 7 天 rotate，json summary 不动；`data/signals/` 与 `data/approvals/` 不 rotate（永久保留，供审计）。`scripts/phase4/02_state_gc.py` 提供 90 天以前的 signals jsonl 归档到 `/var/lib/xtrade/archive/` 的可选清理任务。 |
| T4  | yuanbao 审批桥（经 openclaw） | `xtrade.bridge.openclaw_webhook.OpenclawBridge`：manual 模式下当 `ApprovalQueue` 写入 `pending` 行时，supervisor 触发 `bridge.dispatch(record)`，POST `https://<openclaw-gateway>/plugins/webhooks/xtrade`，`Authorization: Bearer <shared_secret>`，body 见 §5 Task 4 schema；HTTP 5xx / 网络错误指数回退重试（1s/2s/4s/8s，最多 4 次）后写 `dispatch_failed` 字段但不阻塞队列。openclaw 端回执经独立 `xtrade-bridge.service` 接收（HTTP server，监听 `127.0.0.1:18080`，由 openclaw 经 localhost 调起），把 yuanbao 用户决策 patch 回 `ApprovalQueue` 对应 row。 |
| T5  | always-on supervisor | `xtrade.live.supervisor.run_supervisor(config_path)`：长驻进程，启动一个 Phase 1 testnet `TradingNode`、订阅 venue 矩阵中所有 instrument 的行情、watch `data/signals/`（inotify 或 `os.scandir` 轮询）、新信号到达即走 strategy → RiskGate → ApprovalGate 链；进入 manual 等待时挂起该 intent，不阻塞其它信号；ops sentinel 文件存在时拒绝所有新单（仍处理回执）。崩溃由 systemd 重启，重启后从 `ApprovalQueue` + `SignalConsumer` cursor 恢复，不重复执行已 fill 的 intent。 |
| T6  | 故障恢复演练 | `docs/phase4_runbook_vps.md` 收录 4 个演练剧本，逐项实测并记录通过/未通过：(a) `systemctl kill -s SIGKILL xtrade-supervisor`：10s 内自动重启，cursor 恢复，没有重复下单；(b) 内存压力 → cgroup OOM kill xtrade-supervisor 而非 openclaw / hermes；(c) 网络抖动（`iptables` 临时丢弃 binance.com 30s）：TradingNode 心跳告警、不下错单、网络恢复后自动续订阅；(d) openclaw gateway 5xx：bridge 重试耗尽 → manual 行标 `dispatch_failed` → `xtrade ops status` 在告警栏显示、运维可用 `xtrade approve confirm <id>` 本地兜底确认。 |
| T7  | 运维 CLI 与状态汇总 | `xtrade ops status`：单行+JSON 双格式，字段 `supervisor.state`、`supervisor.uptime_s`、`last_signal_id`、`last_signal_age_s`、`pending_approvals`、`last_fill_id`、`last_fill_age_s`、`bridge.last_dispatch_status`、`paused`（bool）；`xtrade ops pause` / `resume` 通过 sentinel 文件控制；`xtrade ops kill` 等价 `systemctl stop xtrade-supervisor`。`xtrade ops` 不依赖 supervisor 进程内 IPC，全部通过文件系统状态读取，因此 supervisor 崩溃时仍可调用。 |
| T8  | 测试与可观测性 | `pytest tests/` 全绿；新增至少 30 个 offline 测试覆盖 T1（脚本 dry-run）、T4（bridge dispatch + retry + secret scrub）、T5（supervisor cursor 恢复 + sentinel）、T7（ops CLI 字段）；演练 T6 的 4 个剧本逐一在 VPS 实测并附 journalctl 摘要 + `xtrade ops status` 快照到 `docs/phase4_results.md`。 |

**Phase 4 通过条件**：T1–T7 全 PASS；T8 为收尾要求。任何一项 FAIL 在结果报告中明确记录原因与解决路径。

---

## 3. 不在本阶段处理的事项（显式延后）

- **mainnet 真实资金**：仍 testnet；Phase 5。
- **容器化**：systemd + cgroup 即可，docker / podman / k8s 不上。
- **Grafana / Loki / Prometheus / OTel**：journalctl + json summary 已够 Phase 4 观测面；Phase 5 视 openclaw / hermes 是否共享一套监控栈再决定。
- **Telegram / WebUI / 自建 PWA**：yuanbao 已经是唯一审批通道。
- **跨主机 / HA / 双活**：单 VPS。崩溃由 systemd 重启，演练 T6 验证恢复时间窗口。
- **多账户 / 子账户**：单账户。
- **新数据源 / 链上 / 新闻 / ML**：Phase 5。
- **策略热加载 / 灰度发布**：Phase 4 仍是"改代码 → `systemctl restart xtrade-supervisor`"；策略热插拔到 Phase 5。
- **xtrade 直接调 yuanbao SDK**：xtrade 仅与 openclaw 通信；yuanbao 由 openclaw 下游处理。
- **持久化数据库迁移**：Parquet + jsonl 不动。

---

## 4. 目标仓库结构与 VPS 文件布局

### 4.1 仓库增量（在 Phase 3 之上）

```
xtrade/
├── deploy/                                  (新增)
│   ├── systemd/
│   │   ├── xtrade-supervisor.service        (模板，install 脚本 envsubst)
│   │   ├── xtrade-scanner.service
│   │   ├── xtrade-scanner.timer
│   │   └── xtrade-bridge.service
│   ├── logrotate/
│   │   └── xtrade
│   └── env/
│       └── xtrade.env.example               (BINANCE_*、OPENCLAW_GATEWAY、OPENCLAW_SHARED_SECRET)
├── scripts/phase4/                          (新增)
│   ├── install_vps.sh
│   ├── uninstall_vps.sh
│   ├── 02_state_gc.py
│   ├── 03_drill_oom.sh
│   ├── 04_drill_network.sh
│   └── 05_smoke_postdeploy.sh
├── src/xtrade/
│   ├── live/
│   │   ├── supervisor.py                    (新增；always-on entry point)
│   │   └── sentinel.py                      (新增；ops pause/resume sentinel)
│   ├── bridge/                              (新包)
│   │   ├── __init__.py
│   │   ├── openclaw_webhook.py              (出口：xtrade → openclaw POST)
│   │   ├── inbound.py                       (入口：openclaw → xtrade HTTP server)
│   │   └── schema.py                        (出入站 payload dataclass + 凭据扫描)
│   └── cli.py                               (新增 `ops` 子命令组)
├── tests/
│   ├── test_bridge_openclaw_webhook.py      (新增)
│   ├── test_bridge_inbound.py               (新增)
│   ├── test_bridge_schema.py                (新增)
│   ├── test_supervisor_cursor.py            (新增；崩溃恢复)
│   ├── test_supervisor_sentinel.py          (新增；pause/resume)
│   ├── test_cli_ops.py                      (新增)
│   ├── test_install_vps_dryrun.py           (新增；bash -n + shellcheck)
│   └── test_systemd_unit_render.py          (新增；envsubst 输出对比)
├── docs/
│   ├── phase4_brief.md                      (本文件)
│   ├── phase4_results.md                    (新增；执行中追加)
│   └── phase4_runbook_vps.md                (新增；安装 + 演练剧本)
└── (Phase 3 之前的目录全部不动)
```

### 4.2 VPS 文件系统布局（部署后）

```
/opt/xtrade/                                 (代码与 venv，755 root:root)
├── .venv/                                   (uv-managed Python 3.12)
├── src/                                     (从 release tarball 解压)
└── current -> /opt/xtrade/releases/<sha>/   (符号链接，便于回滚)

/etc/xtrade/                                 (配置与凭据，750 root:xtrade)
├── env                                      (640 root:xtrade；BINANCE_* / OPENCLAW_SHARED_SECRET)
├── venues.binance_spot.testnet.yaml
├── venues.binance_futures.testnet.yaml
├── venues.hyperliquid.testnet.yaml
├── universe.yaml
└── risk.yaml

/var/lib/xtrade/                             (状态，700 xtrade:xtrade)
├── catalog/                                 (parquet)
├── signals/<YYYY-MM-DD>.jsonl
├── approvals/<YYYY-MM-DD>.jsonl
├── archive/                                 (90 天前归档)
└── logs/<run-id>/{run.log, *_summary.json, config.snapshot.yaml}

/run/xtrade/                                 (volatile，tmpfs)
├── paused.flag                              (sentinel)
└── supervisor.pid                           (systemd 已管，此处仅给 ops CLI 探活)
```

新增依赖：**仅** `uv`（开发 + 部署一致），`httpx`（bridge 出/入站；项目内已用于 venue 探活，无新增）。**不**引入 `docker`、`requests`、`gunicorn`、`uvicorn`、`fastapi`、`prometheus_client`、`telegram` 任何一个。

---

## 5. 任务分解

### Task 1 —— 无容器安装链与 systemd 模板（T1, T2）

- `deploy/systemd/*.service.in`：使用 `${OPT_XTRADE}` / `${VAR_XTRADE}` / `${ETC_XTRADE}` 占位符；`install_vps.sh` 用 `envsubst` 渲染到 `/etc/systemd/system/`。
- `xtrade-supervisor.service` 关键字段：
  ```
  [Service]
  User=xtrade
  Group=xtrade
  EnvironmentFile=/etc/xtrade/env
  WorkingDirectory=/opt/xtrade/current
  ExecStart=/opt/xtrade/.venv/bin/xtrade live supervise --config /etc/xtrade/supervisor.yaml
  Restart=on-failure
  RestartSec=10s
  MemoryMax=1500M
  MemorySwapMax=0
  CPUQuota=80%
  OOMScoreAdjust=500
  ProtectSystem=strict
  ReadWritePaths=/var/lib/xtrade /run/xtrade
  NoNewPrivileges=yes
  ```
- `install_vps.sh` 步骤：
  1. 拒绝 root（要求以 sudo 升权但服务以 `xtrade` 用户跑）；幂等创建系统用户 `xtrade`、组 `xtrade`。
  2. `uv python install 3.12` → `/opt/xtrade/.venv` `uv venv` → `uv pip install -e /opt/xtrade/current`。
  3. 渲染并 `systemctl daemon-reload` + `systemctl enable --now xtrade-supervisor xtrade-bridge xtrade-scanner.timer`。
  4. 写 `logrotate.d/xtrade`；写 `/run/xtrade` tmpfs 挂载（systemd-tmpfiles）。
  5. 退出码 0 即"安装完成且 supervisor 已 healthy"，1 即"任一 unit 启动失败并已 dump journalctl 尾巴"。
- `uninstall_vps.sh`：`systemctl disable --now` 全部 unit、删 `/etc/systemd/system/xtrade-*`、保留 `/var/lib/xtrade`（默认；`--purge` 才删）。
- 验收：`tests/test_install_vps_dryrun.py`（`bash -n` 语法 + `shellcheck` 静态检查）、`tests/test_systemd_unit_render.py`（给定 env，envsubst 输出与 golden file 严格相等）。

### Task 2 —— `bridge.openclaw_webhook.OpenclawBridge`（T4 出站）

- `dispatch(record: ApprovalRecord) -> DispatchResult`：
  - 构造 payload：
    ```json
    {
      "action": "create_flow",
      "goal": "xtrade approval request",
      "metadata": {
        "approval_id": "1de841fa7ed7a038",
        "intent": {
          "venue": "binance",
          "symbol": "BTCUSDT-PERP.BINANCE",
          "side": "BUY",
          "order_type": "MARKET",
          "quantity": "0.002",
          "limit_price": null,
          "reduce_only": false,
          "time_in_force": "IOC",
          "source_signal_id": "..."
        },
        "risk_summary": {"rules_passed": ["max_notional_per_order", "max_position_per_symbol"], "marks_used": "..."},
        "callback": {
          "confirm_url": "http://127.0.0.1:18080/approvals/1de841fa7ed7a038/confirm",
          "reject_url":  "http://127.0.0.1:18080/approvals/1de841fa7ed7a038/reject",
          "ttl_s": 900
        }
      }
    }
    ```
  - `Authorization: Bearer ${OPENCLAW_SHARED_SECRET}`；`httpx` client，5 s connect / 10 s read。
  - 重试：指数回退 1/2/4/8 s，最多 4 次；HTTP 4xx 不重试（payload 错误）；HTTP 5xx + 网络错误重试。
  - 失败终态：写回 `ApprovalQueue` 行的 `dispatch_failed` 字段（`reason`、`attempts`、`last_status`），**不**改 `status`（仍 `pending`，可被本地 `xtrade approve confirm` 兜底）。
- 凭据扫描：payload 序列化前过 `_scan_metadata_for_secrets`（沿用 Phase 2 / Phase 3 的扫描器），命中 BINANCE_API_KEY / shared_secret 等字符串即拒绝发送并写错误。
- 验收：`tests/test_bridge_openclaw_webhook.py`（httpx mock：200/500/timeout/4xx；secret scrub；exponential backoff 时序）、`tests/test_bridge_schema.py`（payload 字段完整性 + 不可变 dataclass）。

### Task 3 —— `bridge.inbound`（T4 入站）

- 极简 HTTP server，绑定 `127.0.0.1:18080`（只接受 localhost；openclaw 经 localhost 调起），无 TLS、无外部端口暴露。
- 实现选项：`http.server.ThreadingHTTPServer` + 手写 handler，**不**引入 FastAPI / uvicorn。仅两个路由：
  - `POST /approvals/<id>/confirm` body `{"actor": "yuanbao:<user>", "reason": "..."}`
  - `POST /approvals/<id>/reject`  body 同上
- handler：
  1. `Authorization: Bearer <local_token>`（与 openclaw 共享，与 OPENCLAW_SHARED_SECRET 可不同；用 `OPENCLAW_INBOUND_SECRET`）。
  2. 校验 `id` 存在 + `status == pending` + ttl 未过期 → 调 `ApprovalQueue.patch(id, status, reason)`。
  3. 不存在或已决策返回 409 + JSON `{"code":"already_decided","status":"confirmed"}`，幂等于 openclaw 重投。
- supervisor 进程内 poll `ApprovalQueue` 取得新决策即恢复对应 intent（与 Phase 3 manual 模式现成的等待循环复用）。
- 验收：`tests/test_bridge_inbound.py`（confirm/reject/未授权/过期 ttl/重复投递）。

### Task 4 —— `live.supervisor.run_supervisor`（T5）

- 长驻进程，结构：
  ```python
  def run_supervisor(config: SupervisorConfig) -> None:
      node = build_testnet_trading_node(config.venues)
      consumer = SignalConsumer(SignalQueue(config.signals_root), cursor_path=config.cursor_path)
      gate = ApprovalGate(mode=config.approval_mode, queue_root=config.approvals_root)
      bridge = OpenclawBridge(config.openclaw)
      sentinel = Sentinel(config.sentinel_path)
      node.start()
      try:
          for signal in consumer.iter_new(poll_interval_s=2.0):
              if sentinel.paused():
                  log_event("paused, dropping signal", signal_id=signal.id); continue
              for intent in strategy.on_signal(signal, account.snapshot()):
                  decision = risk_gate.check(intent, account.snapshot())
                  if not decision.approve: continue
                  approval = gate.decide(intent)
                  if approval.go: submit_to_node(node, intent)
                  elif approval.awaiting: bridge.dispatch(load_pending(approval.awaiting))
      finally:
          node.stop()
  ```
- cursor 持久化：每消费一条 signal 后将 cursor 写入 `/var/lib/xtrade/signals/.cursor`（atomic write）；崩溃重启后从 cursor 继续，**不**回放已 fill 的 intent。
- 已经在 `pending` 但 supervisor 重启时尚未决策的 intent：重启时不重复 dispatch（去重键 = `ApprovalQueue` row 已有 `dispatch_at`）。
- 验收：`tests/test_supervisor_cursor.py`（注入 signals → kill → 重启 → 不回放）、`tests/test_supervisor_sentinel.py`（pause/resume）。

### Task 5 —— `xtrade ops` 子命令（T7）

- `xtrade ops status [--json]`：纯文件系统读取（`/run/xtrade/paused.flag`、`/var/lib/xtrade/signals/.cursor`、`approvals/<today>.jsonl` 最后一行、最近 `logs/<run-id>/strategy_summary.json`）；不依赖 supervisor 内存状态。
- `xtrade ops pause [--reason ...]` / `xtrade ops resume`：写/删 `/run/xtrade/paused.flag`，文件内容 `{"paused_at":"...","reason":"..."}`。
- `xtrade ops kill`：`subprocess.run(["systemctl", "stop", "xtrade-supervisor"])`（需 `sudo` 或 polkit rule，install 脚本预置 polkit 允许 xtrade 组 stop xtrade-* unit）。
- 验收：`tests/test_cli_ops.py`（mock 文件系统、断言输出字段；不实跑 systemctl）。

### Task 6 —— 故障恢复演练剧本（T6）

`docs/phase4_runbook_vps.md` 收录 4 个剧本，每个剧本格式统一：

```
## Drill N — 名称
预期：...
准备：...
触发：
  ssh vps "systemctl kill -s SIGKILL xtrade-supervisor"
观察：
  ssh vps "journalctl -u xtrade-supervisor -n 50 --no-pager"
  ssh vps "xtrade ops status --json"
判据：
  - supervisor 在 30s 内进入 active(running)
  - 无重复 fill（对比 approvals jsonl）
  - alert webhook 在 60s 内推到 openclaw（grep journalctl）
通过/未通过：（实测后填）
日志样本：（journalctl 截取）
```

4 个剧本：
1. **SIGKILL supervisor** → 自动重启 + cursor 恢复 + 无重复下单。
2. **cgroup OOM**：人为占内存触发 OOM → 验证 xtrade-supervisor 被 kill 而不是 openclaw / hermes（凭 OOMScoreAdjust=500）。
3. **网络抖断**：`iptables -A OUTPUT -d <binance-ip> -j DROP` 30s → TradingNode 心跳告警 + 不下错单 + 恢复后续订阅。
4. **openclaw 5xx**：`systemctl stop openclaw` 5 分钟 → bridge 4 次重试耗尽 → `dispatch_failed` 标记 → `xtrade ops status` 在告警栏可见 → 本地 `xtrade approve confirm <id>` 兜底成功。

### Task 7 —— openclaw 联调（T4 出/入站打通）

- 协调侧（与 openclaw 操作员）：
  - 在 `openclaw.json` `plugins.entries.webhooks.config.routes` 增加 `xtrade` 路由（`path: "/plugins/webhooks/xtrade"`、`secret: { source: "plain", id: "<shared_secret>" }`、`controllerId: "webhooks/xtrade"`、`sessionKey: "agent:main:main"`）。
  - 在 openclaw TaskFlow 增加 `xtrade-approval` 流程：解析 webhook body → 调 yuanbao push → 接收用户回复（confirm / reject）→ 回调 `http://127.0.0.1:18080/approvals/<id>/{confirm|reject}` 并带 `OPENCLAW_INBOUND_SECRET`。
- xtrade 侧：在 `/etc/xtrade/env` 写 `OPENCLAW_GATEWAY=https://<openclaw-gateway>`、`OPENCLAW_SHARED_SECRET=...`、`OPENCLAW_INBOUND_SECRET=...`。
- 打通测试：跑一次 testnet manual signal-run，验证：
  - xtrade journalctl 显示 `bridge dispatch -> 200`。
  - openclaw 显示收到 webhook 并触发 TaskFlow。
  - yuanbao 收到推送（人手验证）。
  - 用户在 yuanbao 回复 confirm → openclaw 回调 xtrade-bridge → ApprovalQueue patch → supervisor 提交 testnet 限价单 → cancel。

### Task 8 —— 测试与可观测性（T8）

- `pytest tests/` 全绿；新增预计 +30 用例（实际可能更多）。
- 关键 journalctl 标签：`xtrade.supervisor`、`xtrade.bridge.out`、`xtrade.bridge.in`、`xtrade.scanner`，每条结构化 log 是单行 JSON（`{"ts":"...","level":"INFO","event":"...","key1":"..."}`），便于 grep。
- `docs/phase4_results.md`：T1–T8 PASS/FAIL、关键数据样本（一次完整 manual 链路从 signal → bridge → yuanbao → 回执 → fill 的 journalctl 摘要 + approval jsonl 行）、4 个演练剧本的实测结果、与 Phase 5 衔接建议。

---

## 6. 安全边界（沿用 Phase 0–3，新增云端 / 桥接层规则）

- **xtrade 仅与 openclaw 通信，不直连 yuanbao**：xtrade 代码库不引入 yuanbao SDK / token / endpoint；yuanbao 是 openclaw 的下游 channel。
- **bridge 入口仅 localhost**：`xtrade-bridge.service` 监听 `127.0.0.1:18080`，systemd unit 同时设 `IPAddressAllow=127.0.0.0/8` + `IPAddressDeny=any`；防火墙额外冗余封 18080。
- **凭据走 systemd `EnvironmentFile`，不入 git**：`/etc/xtrade/env` 权限 `640 root:xtrade`；仓库内仅 `deploy/env/xtrade.env.example`（占位）。
- **payload 凭据扫描**：bridge 出站 payload 序列化前过 `_scan_metadata_for_secrets`，命中即拒绝发送。
- **bridge 失败默拒**：openclaw 不可达时 `ApprovalQueue` 行保持 `pending`，**不**自动 confirm；只有人手 `xtrade approve confirm` 才放行。
- **cgroup MemoryMax 硬上限**：xtrade-supervisor 1500M / scanner 600M / bridge 200M，三个加起来 2300M，留 5.7 GB 给系统 + openclaw + hermes + 缓冲。
- **RiskGate 仍是强制单点**：supervisor 进程也不许绕开 RiskGate；import-graph lint（Phase 3 已建）继续守护 `xtrade.live.*` 与 `xtrade.bridge.*`。
- **mainnet 三锁**：在 Phase 3 双锁基础上，云端环境额外加一锁——`/etc/xtrade/env` 中若出现 `BINANCE_MAINNET=1` 之类字段即 supervisor 启动失败并写显式错误。
- **systemd hardening**：所有 unit 设 `ProtectSystem=strict`、`ProtectHome=true`、`NoNewPrivileges=yes`、`PrivateTmp=true`、`ReadWritePaths=` 显式列出。
- **审计日志**：每次 bridge 出/入站、每次 approve 决策、每次 supervisor 启停都写一条结构化 journalctl 事件；保留窗口 ≥ 30 天（journalctl 默认 + logrotate）。

---

## 7. 决策矩阵（Phase 4 收尾时）

| 结果 | 判断 | 下一步 |
|---|---|---|
| T1–T7 全 PASS | **进入 Phase 5** | Phase 5：小资金 mainnet 上线 + 策略增量（ML / 新闻情绪）+ 仓位 sizing + 多策略并行。 |
| T4 fail（yuanbao 桥不通） | **暂停** | 没有审批通道 = 没有自动化（运维不可能 24h 蹲在 SSH 等 approve confirm）。先排查 openclaw 路由 / shared_secret / 网络。 |
| T5 fail（supervisor 不稳） | **暂停** | 长跑稳定是 Phase 4 的核心交付；崩溃循环或 cursor 丢失都必须修。 |
| T6 部分 fail（演练） | **有条件 GO** | 若 SIGKILL 与网络抖断剧本 PASS、OOM 或 openclaw 5xx 剧本仅"未达成预期 SLA"但无数据损坏，可记录进 Phase 5 监控强化项。 |
| 8 GB RAM 仍吃紧 | **有条件 GO** | 把 `xtrade-scanner` 的 `MemoryMax` 进一步收紧 + 把 catalog ingest 移到本地 mac 跑、VPS 只跑增量同步。 |
| openclaw / hermes 任一升级破坏路由 | **不阻塞** | Phase 4 的本地兜底 `xtrade approve confirm` 仍可用，记录互联约束进 Phase 5。 |

---

## 8. 交付物

1. `deploy/systemd/` 下 4 个 unit + 1 个 timer 模板。
2. `deploy/logrotate/xtrade`、`deploy/env/xtrade.env.example`。
3. `scripts/phase4/` 下 6 个脚本（install / uninstall / state_gc / 3 个 drill / postdeploy smoke）。
4. `src/xtrade/bridge/` 包：`openclaw_webhook.py`、`inbound.py`、`schema.py`。
5. `src/xtrade/live/supervisor.py`、`sentinel.py`。
6. `xtrade.cli` 新增 `ops` 子命令组（`status / pause / resume / kill`）。
7. `tests/` 下 8 个新测试文件，`pytest tests/` 全绿。
8. `docs/phase4_runbook_vps.md`：安装、升级、回滚、4 个故障演练剧本。
9. `docs/phase4_results.md`：T1–T8 PASS/FAIL、journalctl 与 approval jsonl 关键证据、与 Phase 5 衔接建议。
10. 在目标 VPS 上完成一次 testnet manual 链路全程实测（signal → bridge → yuanbao → 回执 → fill → cancel），结果与日志摘要归档进 `docs/phase4_results.md` §X。

---

## 9. 建议执行顺序

1. **Task 1**（无容器安装链 + systemd 模板）—— 先把"代码能跑到 VPS"的骨架立起来，后续任何变更都靠它部署。
2. **Task 5**（supervisor）—— 即便没有 bridge，supervisor 也能在 VPS 上以 `approval_mode=dry_run` 跑通 always-on 链路；早做能暴露内存与稳定性问题。
3. **Task 7 局部**（与 openclaw 操作员对齐路由与 secret）—— 与 Task 5 并行：xtrade 这边出站测试用 `httpbin.org/post` 临时桩，等 openclaw 路由就位再切换 endpoint。
4. **Task 2**（bridge 出站）+ **Task 3**（bridge 入站）—— 两个文件互相不依赖，可并行实现，但建议先出站再入站（出站不通则入站没意义）。
5. **Task 5 收尾**（接 bridge）—— 把 supervisor 与 bridge 串起来，在 VPS 上跑 manual 模式 + bridge dispatch + 本地 inbound 回执的内环（openclaw 端用 curl 模拟回执，先不接 yuanbao）。
6. **Task 7 全链路**（接 yuanbao 真实回执）—— 与 openclaw 操作员协调一次端到端 testnet 跑通。
7. **Task 4 与 Task 5 的安全/熔断字段** —— 补 `xtrade ops pause` / `sentinel` 与 `dispatch_failed` 兜底路径。
8. **Task 5 演练（T6）** —— 4 个剧本逐一在 VPS 实测，每个剧本结果实时写进 `docs/phase4_runbook_vps.md`。
9. **Task 6 ops CLI** —— 在 supervisor 与 bridge 稳定后再补 CLI；CLI 只是文件系统读取的薄包装。
10. **Task 8 测试与文档** —— 每个 Task 落地立即补 offline 测试；最后整理 `docs/phase4_results.md`，给出进入 Phase 5 的建议。

---

## 10. 与 openclaw 操作员的接口约定（拷贝即用）

```
xtrade 出站 POST
  URL:    https://<openclaw-gateway>/plugins/webhooks/xtrade
  Header: Authorization: Bearer <OPENCLAW_SHARED_SECRET>
  Method: POST
  Body:   见 §5 Task 2 schema

openclaw 回执 POST（localhost 调起，xtrade 接收）
  URL:    http://127.0.0.1:18080/approvals/<approval_id>/{confirm|reject}
  Header: Authorization: Bearer <OPENCLAW_INBOUND_SECRET>
  Method: POST
  Body:   {"actor":"yuanbao:<user_id>","reason":"<optional free text>"}
  返回:   200 OK 即 patch 成功
           409 + {"code":"already_decided","status":"confirmed"} 即幂等重复
           401 即 secret 不匹配
           404 即 approval_id 不存在或已过期 ttl

凭据 / 密钥（不入 git）
  /etc/xtrade/env:
    OPENCLAW_GATEWAY=https://<openclaw-gateway>
    OPENCLAW_SHARED_SECRET=...        # xtrade -> openclaw
    OPENCLAW_INBOUND_SECRET=...       # openclaw -> xtrade（可与 shared 共用，建议分开）
    BINANCE_TESTNET_API_KEY=...
    BINANCE_TESTNET_API_SECRET=...
```

---

## 参考链接

- 主路线图 Phase 4 段：`xtrade_plan.md` §七
- Phase 3 收尾与签字：`docs/phase3_results.md` §1.9、§6
- Phase 3 testnet runbook（被 Phase 4 supervisor 拉起的同一条链路）：`docs/phase3_runbook_testnet.md`
- systemd cgroup v2 资源控制：https://www.freedesktop.org/software/systemd/man/systemd.resource-control.html
- systemd unit hardening 选项：https://www.freedesktop.org/software/systemd/man/systemd.exec.html
- uv 安装独立 Python 解释器：https://docs.astral.sh/uv/concepts/python-versions/
- OpenCloudOS 9 release notes：https://docs.opencloudos.org/
