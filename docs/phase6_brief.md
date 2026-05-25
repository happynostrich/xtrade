# Phase 6 实施简报 —— 小资金 mainnet tap test（Binance Futures，24h 守夜样本）

> 编制日期：2026-05-26
> 目标仓库：`/Users/bitcrab/xtrade`
> 上游依据：
> - 主路线图：`xtrade_plan.md` §七 "Phase 5 — 小资金上线与迭代"（在 Phase 5 brief 中已被推迟，本期承接）
> - Phase 5 收尾：`docs/phase5_results.md`（offline ALL PASS；Track A VPS 复跑、Track B mac e2e、Phase 4 §1.6 / §1.7 VPS 签字三项 PENDING）
> - Phase 5 决策矩阵：`docs/phase5_brief.md` §7 row 1 "进入 Phase 6（小资金 mainnet tap test）"
> - 与用户对齐（2026-05-26）：
>   - 单 venue：**Binance Futures USD-M（mainnet）**，初始 margin **$100–200**，**1× 杠杆**。
>   - ML gate **OFF**（保留 Phase 5 momentum_follow 纯规则路径，便于 P&L 归因）。
>   - 告警 / 守夜推送走 **yuanbao bot**（不接 Telegram / Grafana / Loki / Prometheus）；openclaw + yuanbao 通道在 Phase 4 § 1.7 已经为 manual 审批跑通，本期复用其出站路径增量加 "alert" payload action。
>   - **不**开 live paper mirror；以 Phase 5 paper signal_runner 在同一 signals snapshot 上做 **end-of-day offline replay** 作为对照。
> 执行方式：本简报交给 **Claude Code** 在 `/Users/bitcrab/xtrade` 中执行；同时 / 之后由操作员在 VPS 上完成 24h 守夜实测。

---

## 0. 进入 Phase 6 的前提（硬阻塞 gate）

Phase 5 brief §7 决策矩阵第 6 行明确：**"即使 Phase 5 全 PASS，没有 Phase 4 VPS 签字不允许动 mainnet。"** 因此 Phase 6 的 mainnet 任何代码路径 **必须** 在以下三项全部填回 PASS 之后才进入实测：

| Gate | 状态 | 说明 |
|---|---|---|
| Phase 4 §1.6 — 4 个 drill 在 VPS PASS（SIGKILL / OOM / 网络抖断 / openclaw 5xx） | PENDING-VPS | 操作员在 VPS 上跑 `scripts/phase4/0{1,3,4,6}_drill_*.sh` 并把模板填回 `docs/phase4_results.md` §1.6 |
| Phase 4 §1.7 — openclaw 端 `xtrade-approval` TaskFlow + testnet 端到端 manual 一次 PASS | PENDING-VPS | 见 `docs/phase5_results.md` §5.2 |
| Phase 5 Track A VPS 复跑（持久 node + audit jsonl + disk check + 三锁齐备 + scanner 事件） | PENDING-VPS | 见 `docs/phase5_results.md` §2.1 |

Track B mac e2e（`docs/phase5_results.md` §2.2）**不**阻塞 Phase 6 —— ML gate 本期默认关闭。

> Phase 6 **代码 / 测试**部分（即本简报 §5 的 T1–T8）**不**依赖上述 gate，可与 VPS 实测并行开发。但**任何一条 mainnet 路径的实际启用**（最终 `systemctl start xtrade-supervisor.service` 指向 mainnet venues yaml）必须等 gate 全 PASS。

---

## 1. Phase 6 的使命与非使命

### 使命

1. **Binance Futures USD-M mainnet 闭环**：把 Phase 4–5 已稳定的 testnet 链路（signal → strategy → RiskGate → ApprovalGate → 持久 TradingNode → bridge → yuanbao → confirm → fill → cancel）首次切到 mainnet；标的固定 `BTCUSDT-PERP.BINANCE`（或与用户最终确认的单一蓝筹永续），初始 margin **$100–200 USDT**，**1× 杠杆**（`positionRisk.leverage=1`，由 `xtrade.live.binance.adapter` 在 node start 时显式设定）。
2. **小资金 risk ceiling**：新增 `config/risk.mainnet.yaml`（`MaxNotionalPerOrder.usd_cap=50` / `MaxPositionPerSymbol.usd_cap=200` / `MaxTotalNotional.usd_cap=200` / `MaxDrawdownPct.pct=0.05`），与 testnet `config/risk.example.yaml` 完全独立；supervisor.yaml 通过 `risk_yaml: /etc/xtrade/risk.mainnet.yaml` 引用；RiskGate 在 mainnet venue 下额外硬校验"任何 rule.usd_cap > 200 即 raise"，防止误把 testnet rules 拷过来。
3. **回撤熔断（drawdown halt）**：supervisor 每 iteration 读 venue account equity；维护一个 high-water mark（持久化到 `/var/lib/xtrade/state/drawdown.json`，atomic write）；当前 equity 相对 HWM 下跌 ≥ **5%** 即自动写 `/run/xtrade/paused.flag`（reason=`drawdown.halt:<pct>`）+ 发 `supervisor.drawdown.halt` 事件 + 推 yuanbao alert；该 pause **不**自动 resume（与 Phase 5 disk.halt 自动 resume 不同），必须人工 `xtrade ops resume` 才放开 —— 触底反弹后继续单是要操作员二次决策。
4. **24h 守夜守望 + yuanbao alert 通道**：复用 Phase 4 § 10 的 openclaw 出站协议，扩 payload `action="create_alert"`（或与操作员对齐后选定的 action name），envelope 含 `severity ∈ {info, warn, crit}` / `event` / `message` / `attached_fields`；新增 `xtrade.bridge.alerter::AlertBridge` 复用 `OpenclawBridge` 的 retry / scrub / audit 框架；触发源 3 类：(a) **heartbeat watchdog**（supervisor 自身 iteration loop 内监测：连续 N 次 iteration 无新 signal 处理 + N 次 iteration 间隔超过 M 秒 → `warn`）、(b) **drawdown halt** → `crit`、(c) **disk halt / mainnet unlock failure / node crash restart** → `crit`。所有 alert 同样落 `/var/lib/xtrade/audit/alerts.<YYYY-MM-DD>.jsonl`。
5. **Kill-switch CLI**：`xtrade ops kill --yes` 在 Phase 4 已存在（`systemctl stop`），本期增 `xtrade ops emergency_close --yes`：(1) 写 sentinel + reason，(2) 调 venue API 取消该 instrument 所有 open orders，(3) **不**自动平仓（避免市价单滑点放大损失，由操作员手动决策是否平），(4) 推 `severity=crit` alert，(5) 退出码 0=全部 cancel 成功 / 2=有 cancel 失败需人工介入。该命令必须能在 supervisor **崩溃** 时由独立 CLI 进程跑通（即不依赖 supervisor 进程）。
6. **End-of-day offline replay 对照**：新增 `scripts/phase6/01_eod_replay.sh` —— 取当日 `/var/lib/xtrade/signals/<YYYY-MM-DD>.jsonl` snapshot + `logs/<run-id>/live_signal_summary.json`，跑 Phase 5 Track C paper 路径（`xtrade live signal --paper --strategy momentum_follow --signals <date> --replay`）产出 `reports/phase6/eod_<date>.json` 含 `{real_pnl, paper_pnl, divergence, intent_count_real, intent_count_paper, divergence_reasons[]}`；脚本最后写一行 yuanbao alert（`severity=info`）汇总当日。
7. **24h 守夜样本 + runbook**：操作员在 mainnet 启用后跑满 **连续 24 小时**（自然时区不重要），结束时整理：
   - 真实 fill 列表（venue 端拉取 + xtrade 端 approval jsonl 双向对账）。
   - alert jsonl 全部行（heartbeat / drawdown / disk / node crash）。
   - bridge_out audit jsonl 全部行。
   - EOD offline replay 报告（§5）。
   - 一份 `docs/phase6_runbook_vps.md` 风格的守夜日志（人手填回模板，类比 phase4_runbook §1.6 的 drill 格式）。

### 非使命

- **不接 Telegram / Grafana / Loki / Prometheus / OpenTelemetry**：本期沿用 journalctl + jsonl + yuanbao push 三件套。Telegram 不上是用户明确决定；Grafana 等可推到 Phase 6.x 长尾。
- **不开 ML gate**：strategy yaml 不挂 `ml_gate:` 段，default 构造路径已被 Phase 5 `test_momentum_follow_default_construct_does_not_pull_ml_gate` 守护。Phase 6.1 子任务再单开。
- **不开第二 venue**：Binance Spot / Hyperliquid 仍在 venue yaml 中保留 testnet 配置（便于 mainnet 切失败时迅速回退到 testnet），但 mainnet supervisor.yaml 只指向 `venues.binance_futures.mainnet.yaml`。
- **不上 live paper mirror process**：用户明确决定。同进程 dual-dispatch / 同 VPS dual-systemd 都不做；以 EOD offline replay 替代。
- **不引入数据库 / 容器化 / k8s**。
- **不做多策略并行 / 策略组合 / 资金调度**：单策略 `momentum_follow`、单标的、单 venue。
- **不引入新的 risk rule 类型**：复用 Phase 3–5 的 4 条 rule；仅参数从 testnet 调到 mainnet 紧档。
- **不做高频 / 低延迟优化**：Phase 6 是验证流程而不是最大化收益。
- **不做信号发现增量**：scanner 与策略层冻结在 Phase 5 状态。

---

## 2. 验收标准（Go / No-Go 清单）

每项明确 PASS / FAIL，记入 `docs/phase6_results.md`。

| ID | 名称 | 描述 |
|---|---|---|
| T1 | mainnet venues yaml + 第三锁实测生效 | `config/venues.binance_futures.mainnet.yaml` 入库（API key / secret 走 `*_env: XTRADE_MAINNET_BINANCE_FUTURES_*`，**永不**落 git）；`/etc/xtrade/mainnet_unlock` 在 VPS 上由操作员手动 create（0400 root:root，单行 token，与 `/etc/xtrade/env` 的 `XTRADE_MAINNET_UNLOCK_TOKEN` 完全一致）；Phase 5 A5 的 `assert_mainnet_unlock` 在 supervisor 启动时 **真实** 被调用一次（journalctl `mainnet.unlock.ok` 事件）；offline 测试 `tests/test_mainnet_venues_yaml.py` 锁定 schema + 拒绝任何明文 secret。 |
| T2 | mainnet 紧档 risk yaml + supervisor 拒绝越界 | `config/risk.mainnet.yaml` 4 条 rule（`MaxNotionalPerOrder.usd_cap=50` / `MaxPositionPerSymbol.usd_cap=200` / `MaxTotalNotional.usd_cap=200` / `MaxDrawdownPct.pct=0.05`）入库；`load_supervisor_config` 在 venue 解析为 mainnet 时**额外**校验"所有 usd_cap ≤ 200 且 pct ≤ 0.05"，否则 raise `MainnetRiskTooLooseError`；`tests/test_supervisor_mainnet_risk_ceiling.py` 覆盖 happy / 越界两路径。 |
| T3 | drawdown halt 实现 + 自动 pause | `src/xtrade/live/drawdown.py::DrawdownWatcher(equity_source, hwm_path, halt_pct=Decimal("0.05"))`：维护 `/var/lib/xtrade/state/drawdown.json`（atomic write），每 iteration 调 `update(now_ts, equity)` → `WatcherState{hwm, equity, drawdown_pct, halted}`；`halted=True` 时 supervisor 写 sentinel `reason=drawdown.halt:<pct>`、发 `supervisor.drawdown.halt` 事件、推 alert；offline 测试覆盖：HWM 单调上升、跌破阈值即 halt、自动 resume **不**触发、HWM json corrupt 时按当前 equity 重置且发 warn。 |
| T4 | 心跳 / 闲置 watchdog | `src/xtrade/live/heartbeat.py::HeartbeatWatcher(idle_warn_s=600, idle_crit_s=1800)`：在 supervisor iteration loop 中跟 `last_signal_processed_at` / `last_iteration_at`；超过 `idle_warn_s` 推 `warn` alert（去重：同一档不重复推），超 `idle_crit_s` 推 `crit`；恢复后推一次 `info`；offline 测试用 frozen clock + fake alerter 锁状态机。 |
| T5 | yuanbao alert outbound 通道 | `src/xtrade/bridge/alerter.py::AlertBridge(openclaw_endpoint, secret, audit_writer)`：复用 `OpenclawBridge` 的 4 次指数 retry + secret scrub + audit jsonl 框架；payload action 与操作员对齐后定为 `create_alert`，envelope `{action, severity, event, message, instrument?, fields, dispatched_at}`；失败时 envelope 落 `/var/lib/xtrade/audit/alerts.<date>.jsonl` 的 `kind="fail"` 行，**不**因 alert 失败拖死 supervisor iteration；offline 测试 `tests/test_bridge_alerter.py` 用 httpx MockTransport 锁 5 类（200 / 5xx after retry / 4xx / secret refused / 离线（无 endpoint） 直接 audit-only）。 |
| T6 | emergency_close CLI（崩溃可用） | `xtrade ops emergency_close --yes [--instrument BTCUSDT-PERP.BINANCE]`：(1) 写 sentinel `reason=emergency.close:<ts>`；(2) 直接用 venues yaml 中的 API key 调 Binance Futures `DELETE /fapi/v1/allOpenOrders?symbol=...`（**不**经 supervisor，CLI 自构 client）；(3) 推 `crit` alert；(4) 打印 cancel 结果摘要；(5) 退出码 0 / 2；`tests/test_cli_emergency_close.py` 覆盖：sentinel 写入、API mock 200、API mock 4xx 返回 2、`--instrument` 缺省回退 supervisor.yaml 中默认 instrument、缺 `--yes` 拒空。 |
| T7 | EOD offline replay 工具 | `scripts/phase6/01_eod_replay.sh <YYYY-MM-DD>` + `src/xtrade/cli.py` 增 `xtrade ops eod_replay <date>` 子命令：读 signals snapshot + venue fill 历史，调 Phase 5 `xtrade.research.replay_gate` 或新增 `xtrade.ops.eod_replay::compute_eod_report`，产出 `reports/phase6/eod_<date>.json`；schema `{date, real_pnl_usd, paper_pnl_usd, divergence_usd, divergence_pct, intent_count_real, intent_count_paper, divergence_reasons:[{intent_id, reason}]}`；`tests/test_eod_replay.py` 用 fixture signals + 模拟 fill 锁 6 字段。 |
| T8 | 24h 守夜 runbook + 实测填回 | `docs/phase6_runbook_vps.md` 入库（启动 / 巡检 / 告警分级响应 / drawdown halt 处置 / emergency_close 流程 / 收尾对账 6 块）；操作员在 VPS 上跑满 24h 后把 4 类证据填回 `docs/phase6_results.md`（fill 列表、alert jsonl 摘要、audit jsonl 摘要、EOD replay 报告）+ 写一段叙述性"通过 / 未通过 + 原因"； Track A VPS 复跑 + Phase 4 §1.6/§1.7 签字在 Phase 5 `phase5_results.md` 中已经填回 PASS 才能进入此 24h 实测。 |

---

## 3. 不在本阶段处理的事项（显式延后）

- **ML gate 上 mainnet**：推到 Phase 6.1（先做 paper mirror 在线 A/B 至少 7 个交易日，证明 ML gate 在 mainnet 流量下不引入新负偏差，再考虑切流量）。
- **多 venue mainnet**：Binance Spot / Hyperliquid mainnet 推到 Phase 6.2 / 6.3，按 venue 单独 brief。
- **真实股票永续（HIP-3 builder-deployed perps）**：`xtrade_plan.md` § 一 Phase 0 头号风险，仍未验证；推到独立 phase。
- **Grafana / Loki / Prometheus / Telegram**：Phase 6 内不接；若 24h 守夜样本暴露 yuanbao push 单通道不够（如延迟过高、断点丢失），单独立项做 Phase 6.4 "double-channel alerting"。
- **多策略 / 策略组合 / 资金调度 / 自动加减仓**：仍冻结在单策略单标的。
- **数据库 / 容器化**：延续 Phase 4–5 决策。
- **OTP / 二人复核**：mainnet 第三锁已是 root-only token 文件 + env 双比对；操作员二人复核制度（如必须的话）属于 ops policy 而不是代码变更，本期文档化即可。
- **历史回填 / dataset 增量训练**：Track B 数据流冻结在 Phase 5 状态。

---

## 4. 仓库结构变更

### 新增源码

```
src/xtrade/live/drawdown.py                     # T3 drawdown watcher
src/xtrade/live/heartbeat.py                    # T4 idle / heartbeat watcher
src/xtrade/bridge/alerter.py                    # T5 yuanbao alert outbound
src/xtrade/ops/eod_replay.py                    # T7 EOD replay computation
src/xtrade/cli.py                               # +emergency_close, +eod_replay 子命令（T6, T7）
src/xtrade/live/supervisor.py                   # 集成 drawdown + heartbeat + alerter（T3 / T4 / T5）
src/xtrade/live/binance_futures_mainnet.py      # mainnet venue 适配薄层（仅做 1× leverage 强制 + position-mode 设定）
```

### 新增测试

```
tests/test_mainnet_venues_yaml.py               (T1)
tests/test_supervisor_mainnet_risk_ceiling.py   (T2)
tests/test_drawdown_watcher.py                  (T3)
tests/test_supervisor_drawdown_integration.py   (T3)
tests/test_heartbeat_watcher.py                 (T4)
tests/test_bridge_alerter.py                    (T5)
tests/test_supervisor_alerter_integration.py    (T4 + T5)
tests/test_cli_emergency_close.py               (T6)
tests/test_eod_replay.py                        (T7)
tests/test_cli_eod_replay.py                    (T7 CLI 层)
```

### 配置与部署

```
config/risk.mainnet.yaml                        # T2 紧档
config/venues.binance_futures.mainnet.yaml      # T1（所有 secret 走 *_env）
config/supervisor.mainnet.example.yaml          # 引用上述两份 + persistent_node=true + 指向 mainnet alert channel
deploy/env/xtrade.env.example                   # +XTRADE_MAINNET_BINANCE_FUTURES_{API_KEY,API_SECRET}, +XTRADE_MAINNET_UNLOCK_TOKEN, +XTRADE_ALERT_CHANNEL=yuanbao
scripts/phase6/01_eod_replay.sh                 # T7
scripts/phase6/02_preflight_mainnet.sh          # 启动前 sanity check（三锁齐备 + 紧档 risk + ext API connectivity）
docs/phase6_brief.md                            # 本文件
docs/phase6_runbook_vps.md                      # 24h 守夜操作手册（T8）
docs/phase6_results.md                          # 收尾报告（Phase 6 末写）
```

### VPS 文件布局新增

```
/var/lib/xtrade/state/drawdown.json             # 0640 xtrade:xtrade（HWM 持久化）
/var/lib/xtrade/audit/alerts.<YYYY-MM-DD>.jsonl # 0640 xtrade:xtrade
/etc/xtrade/risk.mainnet.yaml                   # 0640 root:xtrade
/etc/xtrade/venues.binance_futures.mainnet.yaml # 0640 root:xtrade
/etc/xtrade/supervisor.yaml                     # 复写：risk_yaml + venues_yaml 指向 mainnet
```

---

## 5. 任务分解

#### Task T1 —— mainnet venues yaml + 第三锁实测生效

- `config/venues.binance_futures.mainnet.yaml`：
  ```yaml
  binance:
    futures:
      environment: MAINNET
      account_type: USDT-FUTURE
      api_key_env: XTRADE_MAINNET_BINANCE_FUTURES_API_KEY
      api_secret_env: XTRADE_MAINNET_BINANCE_FUTURES_API_SECRET
      key_type: HMAC
  ```
- `deploy/env/xtrade.env.example` 增对应空占位（不提交真值）。
- `src/xtrade/live/binance_futures_mainnet.py`：在 node start 阶段 hook `client.futures_change_leverage(symbol, leverage=1)` 且 assert 返回值 `leverage == 1`；`futures_change_margin_type(symbol, marginType="ISOLATED")` 强制 isolated（防交叉保证金把别的仓位拖下水）。
- `assert_mainnet_unlock` 已在 Phase 5 A5 落地，本期任务仅是首次"真实被调用并打日志"。
- 测试 `tests/test_mainnet_venues_yaml.py`：(a) yaml 解析为 `BinanceVenueConfig.futures.environment="MAINNET"`；(b) 任何 `api_key:` 明文字段 → schema 拒绝；(c) `_resolve_env_ref` 在 env 缺失时给出含路径的 message（Phase 5 Bug 7 守护回归）。

#### Task T2 —— mainnet 紧档 risk yaml + supervisor 拒绝越界

- `config/risk.mainnet.yaml`：
  ```yaml
  rules:
    - type: max_notional_per_order_usd
      usd_cap: 50
    - type: max_position_per_symbol_usd
      usd_cap: 200
    - type: max_total_notional_usd
      usd_cap: 200
    - type: max_drawdown_pct
      pct: 0.05
  ```
- `xtrade.live.supervisor.load_supervisor_config` 在 venue 解析为 mainnet 时调 `_assert_mainnet_risk_ceiling(rules)`：
  - 所有 `MaxNotional*.usd_cap ≤ 200` 且 `MaxDrawdownPct.pct ≤ 0.05`，否则 raise `MainnetRiskTooLooseError`。
  - 错误 message 列出违规字段 + 当前值 + 上限。
- 测试 `tests/test_supervisor_mainnet_risk_ceiling.py`：testnet 配 + 大 cap = OK；mainnet 配 + 大 cap = raise；mainnet 配 + 紧档 = OK；mainnet 配缺少某 rule = raise。

#### Task T3 —— drawdown halt 实现

- `src/xtrade/live/drawdown.py::DrawdownWatcher`：
  ```python
  @dataclass(frozen=True)
  class WatcherState:
      hwm: Decimal
      equity: Decimal
      drawdown_pct: Decimal
      halted: bool
      reason: str | None

  class DrawdownWatcher:
      def __init__(self, hwm_path: Path, halt_pct: Decimal): ...
      def update(self, now: datetime, equity: Decimal) -> WatcherState: ...
  ```
- HWM 持久化：`/var/lib/xtrade/state/drawdown.json` atomic write（`tmp + os.replace`），corrupt → log warn + 用当前 equity 重置。
- `run_supervisor` 每 iteration 顶部（disk check 之后）调用 `equity = node.cache.account.equity()` → `watcher.update(...)`；`halted=True` → 写 sentinel `reason=drawdown.halt:<pct>` + emit `supervisor.drawdown.halt` 事件 + 推 alert。
- **不**自动 resume：sentinel 由 `xtrade ops resume` 显式清理；resume 后 watcher 不重置 HWM，重新跌破依然 halt（防"上下震荡反复 halt-resume 套利"）。
- 测试 `tests/test_drawdown_watcher.py`：HWM 单调上升；equity 跌至 95% 临界值（边界）；持久化重启不丢；json corrupt；`tests/test_supervisor_drawdown_integration.py`：fake account.equity 注入 → 验证 sentinel + event + alert 三件齐发。

#### Task T4 —— 心跳 / 闲置 watchdog

- `src/xtrade/live/heartbeat.py::HeartbeatWatcher`：
  - 跟两个时间戳：`last_iteration_at`（每 iteration 必更新）与 `last_signal_processed_at`（处理到 ≥ 1 个新 signal 时更新）。
  - 阈值：`idle_warn_s` 默认 600，`idle_crit_s` 默认 1800。
  - 去重：同一档不重复推（用 `last_alert_severity` 状态记忆）；恢复后推 `info` 一次。
- supervisor iteration loop 末尾 `heartbeat.tick(now, processed_new_signal: bool) -> Optional[Alert]`；返回非 None 即调 `alerter.send(alert)`.
- 测试用 `time-machine` / 自注入 frozen clock 锁状态机：normal → warn → crit → recover → info。

#### Task T5 —— yuanbao alert outbound 通道

- `src/xtrade/bridge/alerter.py::AlertBridge(openclaw_endpoint, secret, audit_writer)`：
  - 与 `OpenclawBridge` 共享 httpx client 配置 / retry 矩阵 / `scrub_payload_for_secrets`。
  - 出站 payload：
    ```json
    {
      "action": "create_alert",
      "severity": "warn",
      "event": "supervisor.heartbeat.idle",
      "message": "no signals processed in 12 min",
      "instrument": "BTCUSDT-PERP.BINANCE",
      "fields": {"idle_s": 720, "last_iteration_ts": "...Z"},
      "dispatched_at": "...Z"
    }
    ```
  - 每次 attempt 落 `/var/lib/xtrade/audit/alerts.<YYYY-MM-DD>.jsonl` 一行（schema 与 bridge_out 同构，多 `severity` / `event` 字段）。
  - alert 自身失败 **绝不** 拖死 supervisor iteration —— 框架 catch 全部异常 + 落 audit `kind=fail`。
- 测试 `tests/test_bridge_alerter.py`：httpx MockTransport 跑 5 路径；`tests/test_supervisor_alerter_integration.py`：注入 heartbeat watcher 输出 → 锁 alert 出站 + audit 行。
- **依赖**：openclaw 端的 `create_alert` 路由 / TaskFlow 与操作员对齐（与 Phase 4 §1.7 `create_flow` 风格一致，但目的不同）。该对齐在 `docs/phase6_runbook_vps.md` 中作为 ops 侧前置依赖列出。

#### Task T6 —— emergency_close CLI

- `xtrade ops emergency_close --yes [--instrument SYMBOL]`：
  - 入口在 `xtrade.cli`，不复用 `xtrade.ops.status` 路径（防 supervisor 崩溃时 collector 也跟着死）。
  - 流程：
    1. 写 sentinel `/run/xtrade/paused.flag` reason=`emergency.close:<ts>`（即使 supervisor 死，下次重启也会 paused）。
    2. 从 `/etc/xtrade/venues.binance_futures.mainnet.yaml` + env 解析 API key/secret，自构 `httpx.Client`（**不**走 nautilus，因为 supervisor 可能死）。
    3. `DELETE /fapi/v1/allOpenOrders?symbol=<SYMBOL>` HMAC 签名。
    4. 打印 cancel 数量；推 `severity=crit` alert（用 audit-only fallback，因为 alerter 也可能 down）。
    5. 退出码 0=全 cancel / 2=至少一笔失败。
  - **不**做平仓（即不下市价对冲）；操作员手动判断是否平、用什么价格平。
- 测试 `tests/test_cli_emergency_close.py`：httpx MockTransport 锁 4 路径（200 / 401 / 429 / 5xx）+ 缺 `--yes` 拒空 + sentinel 写入存在性。

#### Task T7 —— EOD offline replay

- `src/xtrade/ops/eod_replay.py::compute_eod_report(date, signals_path, fills_source, paper_strategy) -> EODReport`：
  - real_pnl：venue fill 历史 → 计算实际 P&L（用 Phase 5 已有的 `xtrade.live.pnl` 或新加 minimal helper）。
  - paper_pnl：在 Phase 5 `xtrade.research.replay_gate` 或 `xtrade.live.signal_runner --paper` 路径上重跑同 signals。
  - divergence_reasons：逐 intent 对比 real vs paper 的 `submit_status / fill_price / fill_qty`，给出 `slippage` / `risk_rejected_real_only` / `partial_fill` 等枚举原因。
- `xtrade ops eod_replay <date>` 子命令把 EODReport 写 `reports/phase6/eod_<date>.json` + 推一行 `severity=info` yuanbao alert（含 `real_pnl_usd` 与 `divergence_pct` 两字段）。
- `scripts/phase6/01_eod_replay.sh` 是 thin wrapper（默认 `date=$(date -u +%Y-%m-%d)`）。

#### Task T8 —— 24h 守夜 runbook + 实测填回

- `docs/phase6_runbook_vps.md` 章节：
  1. **启动前 preflight**（`scripts/phase6/02_preflight_mainnet.sh`）：mainnet 三锁齐备 / risk 紧档 / venue API ping / alert 通道 dry-run。
  2. **启动**：`systemctl start xtrade-supervisor.service`，确认 journalctl `mainnet.unlock.ok` + `supervisor.start` + 一条 yuanbao info alert "supervisor.start"。
  3. **巡检节奏**：每 1h 一次 `xtrade ops status --json | jq '{paused,disk,drawdown,bridge,counts}'`；每 4h 一次 venue 端账户余额 / 持仓 / open orders 三向对账。
  4. **告警分级响应**：warn = 记日志 / 继续观察；crit = 立即 `xtrade ops emergency_close --yes`，然后 30 分钟内做 root-cause + 是否手动平仓判断。
  5. **drawdown halt 处置**：halt 触发即停手判断 —— (a) 是否市场异动 / (b) 是否策略错误 / (c) 是否要 manual close 收尾 / (d) 是否 `xtrade ops resume` 继续观察。
  6. **24h 收尾对账**：`xtrade ops eod_replay <date>` + venue 端 fill 历史下载 + alert jsonl 全量摘要。
- `docs/phase6_results.md` 在 24h 后由操作员填回：4 类证据 + 通过/未通过叙述。

---

## 6. 风险模型与 drawdown 语义

### 资金风险窗口

- 初始 margin **$100–200 USDT**，1× 杠杆，单 instrument `BTCUSDT-PERP.BINANCE`，isolated margin → 单笔最大理论亏损 = 当前 margin（极端情况，如 venue 端 liquidation engine 故障 / 滑点拉满）。
- RiskGate 紧档 + 风险熔断双层：(a) `MaxTotalNotional.usd_cap=200` 保证 notional 不超 margin；(b) `MaxDrawdownPct.pct=0.05` + `DrawdownWatcher.halt_pct=0.05` 保证账户级 5% 回撤即硬停。
- 期望 24h 守夜亏损上限：**$10**（5% × $200）。超出此线 → Phase 6 整体失败，回退到 testnet。

### Drawdown 语义边界

- HWM 在 supervisor 首次 boot 时初始化为当时 equity；之后单调取 `max(hwm, current_equity)`；持仓亏损本身不更新 HWM。
- 跌破 HWM × (1 - 5%) → halted；持仓**不**被脚本自动平仓；操作员判断。
- HWM 持久化在 `/var/lib/xtrade/state/drawdown.json` —— 重启 supervisor 不重置 HWM，重启不会"洗掉"昨日高点。
- 如操作员显式 `xtrade ops resume`，watcher 不重置 HWM；行情进一步下跌再次跌破即再次 halt（防"反复套 halt-resume"）。
- 如操作员判断行情已经反弹、HWM 应该重置（罕见路径），手动 `xtrade ops reset_drawdown_hwm --yes` 子命令（T3 子任务）写新 HWM = 当前 equity，留 audit 行。

### API key 安全

- mainnet key 仅赋予 `Enable Futures` + **禁用** `Enable Withdrawals`（在 Binance 后台配置；runbook 强制 checklist）。
- IP 白名单：仅 VPS 公网 IP；runbook 列出"VPS IP 变更后必须同步"。
- key 在 `/etc/xtrade/env` 0640 root:xtrade，不入 git；env 备份只许在操作员本地加密保管。

---

## 7. 决策矩阵（Phase 6 收尾时）

| 结果 | 判断 | 下一步 |
|---|---|---|
| 24h 守夜全部 PASS（无 crit alert / 无 drawdown halt / EOD replay divergence < 5%） | **Phase 6 PASS，进入 Phase 6.1（ML gate paper mirror A/B）** | 开始 7 个交易日的 paper mirror 在线对照 |
| 24h 中触发 1 次 drawdown halt 但 emergency_close 干净收尾 | **有条件 PASS** | 复盘 root cause；如确认是策略缺陷而非外部抖动 → 修策略后重跑 24h；如外部抖动 → 接受并 Phase 6.1 |
| 24h 中触发 crit alert（节点崩溃 / venue API 5xx 长断 / yuanbao 通道断流）但 P&L 未实质受损 | **有条件 PASS** | 修 root cause + 跑 6h 烟测确认 + 进入 Phase 6.1 |
| EOD replay divergence > 5%（real vs paper 大幅偏离） | **不 PASS** | 分析 divergence_reasons —— 若是 slippage 主导，考虑 strategy 端加 marketability 检查；若是 RiskGate 拒单差异，对齐两侧 rule |
| 24h 内累计亏损超 $10（5%） | **NOT PASS / 紧急回退** | 立刻 emergency_close + `systemctl stop xtrade-supervisor.service`；写 incident report；考虑回退到 testnet 长跑 1 周再重试 |
| Phase 4 §1.6 / §1.7 或 Phase 5 Track A VPS 复跑 **任一**未签字就强行启用 mainnet | **流程违规，自动 FAIL** | 不应发生；如发生需 incident review |

---

## 8. 交付物

1. `src/xtrade/live/drawdown.py` + supervisor 集成（T3）。
2. `src/xtrade/live/heartbeat.py` + supervisor 集成（T4）。
3. `src/xtrade/bridge/alerter.py` + supervisor 集成（T5）。
4. `src/xtrade/ops/eod_replay.py` + CLI 子命令（T7）。
5. `xtrade ops emergency_close` CLI + `reset_drawdown_hwm` 子命令（T6 + T3 子任务）。
6. `config/risk.mainnet.yaml` + `config/venues.binance_futures.mainnet.yaml` + `config/supervisor.mainnet.example.yaml`（T1 + T2）。
7. `deploy/env/xtrade.env.example` 增 mainnet env 占位（T1）。
8. `scripts/phase6/01_eod_replay.sh` + `scripts/phase6/02_preflight_mainnet.sh`（T7 + T8）。
9. `tests/` 下 10 个新测试文件，`pytest tests/` 全绿，新增用例 ≥ 80。
10. `docs/phase6_brief.md`（本文件）、`docs/phase6_runbook_vps.md`（24h 守夜操作手册）、`docs/phase6_results.md`（收尾）。
11. operator 在 VPS 上完成 **连续 24 小时** mainnet 守夜（启动前 preflight PASS + 24h 中 4 类证据齐 + EOD replay divergence 报告），结果记入 `docs/phase6_results.md` §X。

---

## 9. 建议执行顺序

代码 / 测试侧（与 Phase 4 §1.6/§1.7 + Phase 5 Track A VPS 复跑并行）：

1. **T1 mainnet venues yaml + T2 紧档 risk yaml**（纯配置 + schema 校验，最先）。
2. **T3 drawdown watcher**（独立模块，offline 可完整测，是 mainnet 资金保护底线）。
3. **T5 alerter outbound**（独立 bridge 改造，与 T4 解耦先做，因 T4 / T6 都依赖它）。
4. **T4 heartbeat watcher**（与 T3 平行，依赖 T5 落地后立即 wire）。
5. **T6 emergency_close CLI**（独立模块，依赖 T1 venues yaml + T5 alerter）。
6. **T7 EOD replay**（依赖 Phase 5 replay_gate 已有路径，相对独立可后做）。
7. **T8 runbook + 实测**：runbook 文档同步 T1–T7 推进；操作员实测必须等三项 gate 全 PASS 之后。

操作员（VPS）侧顺序：

1. 把 Phase 4 §1.6 4 个 drill + §1.7 openclaw 全链跑完并填回 `docs/phase4_results.md`。
2. 把 Phase 5 Track A VPS 复跑跑完并填回 `docs/phase5_results.md` §2.1。
3. 跑 `scripts/phase6/02_preflight_mainnet.sh` → 全 PASS 才进 24h 守夜。
4. 24h 守夜按 `docs/phase6_runbook_vps.md` 走 → 填回 `docs/phase6_results.md`。

---

## 10. 与 openclaw 操作员的接口约定（Phase 5 沿用 + Phase 6 增量）

Phase 4 §10 + Phase 5 §10 全部保留。Phase 6 增量：

```
yuanbao alert outbound payload（xtrade → openclaw → yuanbao bot）
  URL:     POST <OPENCLAW_GATEWAY>/webhooks/xtrade/alerts
  Headers: Authorization: Bearer <OPENCLAW_SHARED_SECRET>
           Content-Type: application/json
  Body schema:
    {
      "action": "create_alert",
      "severity": "info" | "warn" | "crit",
      "event": "supervisor.heartbeat.idle" | "supervisor.drawdown.halt" | "...",
      "message": "human-readable one-liner, ≤ 200 字符",
      "instrument": "BTCUSDT-PERP.BINANCE" (可选),
      "fields": { ... 任意 key → 标量；不得含 secret },
      "dispatched_at": "ISO 8601 Z"
    }
  期望 HTTP 状态：200 = 已 ack；4xx = 立即停止重试（payload 格式 / 鉴权问题）；5xx + 网络错 = 走 OpenclawBridge 同款 4 次指数 backoff（1/2/4/8 s）。
  openclaw 侧 TaskFlow：parse body → 按 severity 选择 yuanbao push 频道 → 用户收到一则 push 消息 → **无需回执**（alert 单向，区别于 approval 双向）。

mainnet 第三锁文件（仅 root，xtrade 用户不可读）
  /etc/xtrade/mainnet_unlock        # 0400 root:root，单行 token
  /etc/xtrade/env                   # XTRADE_MAINNET_UNLOCK_TOKEN=<token>

mainnet venue 凭据（仅 xtrade 用户可读）
  /etc/xtrade/env                   # XTRADE_MAINNET_BINANCE_FUTURES_API_KEY=...
                                    # XTRADE_MAINNET_BINANCE_FUTURES_API_SECRET=...
  权限:                              0640 root:xtrade
  Binance 后台 API key 设置:
    - 启用: Enable Futures
    - 禁用: Enable Withdrawals
    - IP 白名单: <VPS 公网 IP>

drawdown 状态文件（仅 xtrade 用户可读写）
  /var/lib/xtrade/state/drawdown.json    # 0640 xtrade:xtrade
  schema: {"hwm_usd": "...", "last_equity_usd": "...", "last_update_ts": "...Z"}

alert audit jsonl
  /var/lib/xtrade/audit/alerts.<YYYY-MM-DD>.jsonl    # 0640 xtrade:xtrade
  schema: 与 bridge_out audit 同构，增 severity / event 字段
```

---

## 11. 24h 守夜样本最小定义

为避免"什么算 24h 跑完"产生歧义，本期签字采用如下硬定义：

- **连续 24 自然小时**（UTC 或本地时区均可，runbook 内记录哪一种）supervisor 处于 `systemctl is-active = active` 状态。
- 期间 venue 端账户余额 / 持仓 / open orders 每 4h 至少 1 次记录入对账表。
- 期间允许 1 次主动重启（如修配置）但需在守夜日志记明原因 + 重启前后 5 分钟内的 ops status 快照 + alert 摘要；超过 1 次 → 24h 时钟重置。
- 期间 emergency_close 触发即视为 24h 提前终止；后续是否补跑由 §7 决策矩阵裁决。

---

## 参考链接

- `xtrade_plan.md`（主路线图）
- `docs/phase4_brief.md` + `docs/phase4_results.md`（Phase 4 VPS 基础 + drill 模板）
- `docs/phase5_brief.md` + `docs/phase5_results.md`（Phase 5 加固 + ML 离线 + Track C ml_gate 上线护栏）
- `docs/phase5_track_c_brief.md`（Track C 模型 registry / replay-gate / ml_gate audit 设计）
- `src/xtrade/live/mainnet_unlock.py`（Phase 5 A5 第三锁实现，Phase 6 真实启用点）
- `src/xtrade/bridge/openclaw_webhook.py`（Phase 4 出站 bridge，Phase 6 alerter 复用框架）
- `src/xtrade/research/replay_gate.py`（Phase 5 C1 重放路径，Phase 6 EOD replay 复用）
