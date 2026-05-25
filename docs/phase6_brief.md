# Phase 6 实施简报 —— SPCXUSDT 高位做空机会单（mcap-anchored short-only）

> 编制日期：2026-05-26（v2 改写：从 BTCUSDT 24h 守夜样本，转为 SPCXUSDT 机会单 + 同等级基础设施）
> 目标仓库：`/Users/bitcrab/xtrade`
> 上游依据：
> - 主路线图：`xtrade_plan.md` §七（Phase 5 的"小资金上线与迭代"在 Phase 5 brief 中被推迟，本期承接）
> - Phase 5 收尾：`docs/phase5_results.md`（offline ALL PASS；Track A VPS 复跑、Track B mac e2e、Phase 4 §1.6 / §1.7 VPS 签字三项 PENDING）
> - Phase 5 决策矩阵：`docs/phase5_brief.md` §7 row 1
> - v1 brief（commit `c040e37`）锁住了 mainnet 第三锁 / drawdown / heartbeat / alerter / emergency_close / EOD replay 这套基础设施；本次 v2 改写 **保留** 这套基础设施，**替换** 策略层从"BTCUSDT momentum_follow 24h 守夜"为"SPCXUSDT 短期高位做空 + mcap-anchored 仓位上限"
> - **泛化原则（v2.1 引入）**：T5 / T6 / T8 三处面向未来 2–3 个新标的策略复用而设计，命名与参数化都做了去 SPCX 化处理；SPCXUSDT 本期是这套框架的**首个 instance**，下次同家族策略（mcap 锚 + 阶梯止盈 + 价格阈值入场）目标是"加 yaml + 加 instrument_meta 行 + 0 行新代码"。跨家族策略（如趋势跟踪、配对、新闻驱动）需要新写 plugin / scanner 类，但 infra（T1 / T2 / T3 / T4 / T7 / T9 / T10 / T11）零改动。
> - 与用户对齐（2026-05-26）：
>   - 标的：**`SPCXUSDT` Binance USDS-M Pre-IPO Perpetual**（SpaceX 盘前合约）
>   - reference shares outstanding：**11.87B**（用户提供；scanner 需 fetch 校验）
>   - 策略：**只做空**，**永不做多**；高位入场，跌下来分阶段止盈，等下一次机会
>   - 仓位约束硬性条件：**liquidation 价对应市值 ≥ $4T**
>   - margin 上限：**$200 USDT**（margin = collateral / 抵押品额度，A 含义）
>   - leverage：**1× isolated**（避免交叉保证金，且 1× → mcap_liq 自然 ≥ 2× entry mcap，对 $200 margin 完全够用）
>   - 入场规则：**$225 重仓入** + **mark ≥ $210 时每日小仓位 DCA**（依据：迄今历史最高未超 $225，期望出现更高价容易落空）
>   - soft kill 阈值：**mcap > $3.5T**（mark ≈ $294.86）由 supervisor watchdog 触发 `emergency_close`
>   - IPO gap：**接受**，不主动 IPO 前平仓
>   - 持仓时间：**无 hard time stop**；只看价 / mcap
>   - ML gate **OFF**（保留 momentum_follow 旧 plugin，但本期 supervisor.yaml 不挂它）
>   - 告警 / 推送：**yuanbao bot**（不接 Telegram / Grafana / Prometheus）
>   - 半自动：**scanner 发信号 → manual 审批 → 自动下单 + 自动 TP 阶梯**
> 执行方式：Claude Code 在 `/Users/bitcrab/xtrade` 中实现；操作员在 VPS 上完成 mainnet 启用、签字与持仓期守夜。

---

## 0. 进入 Phase 6 的前提（硬阻塞 gate）

Phase 5 brief §7 第 6 行：**"即使 Phase 5 全 PASS，没有 Phase 4 VPS 签字不允许动 mainnet。"** 本期沿用：

| Gate | 状态 | 说明 |
|---|---|---|
| Phase 4 §1.6 — 4 个 drill 在 VPS PASS（SIGKILL / OOM / 网络抖断 / openclaw 5xx） | PENDING-VPS | 操作员跑 `scripts/phase4/0{1,3,4,6}_drill_*.sh` 填回 `docs/phase4_results.md` |
| Phase 4 §1.7 — openclaw 端 `xtrade-approval` TaskFlow + testnet manual e2e PASS | PENDING-VPS | 见 `docs/phase5_results.md` §5.2 |
| Phase 5 Track A VPS 复跑（持久 node + audit jsonl + disk check + 三锁齐备 + scanner 事件） | PENDING-VPS | 见 `docs/phase5_results.md` §2.1 |

Track B mac e2e 不阻塞 —— ML gate 本期默认关闭。

> Phase 6 **代码 / 测试**部分（§5 T1–T11）**不**依赖上述 gate，可与 VPS 实测并行开发。任何 mainnet 实启用（`systemctl start` 指向 mainnet venues yaml）必须等三 gate 全 PASS。

---

## 1. Phase 6 的使命与非使命

### 使命

1. **SPCXUSDT 高位做空机会单**：在 Binance USDS-M `SPCXUSDT` 上验证一次完整的小资金、有交易论点的 short-only 持仓 —— 从 scanner 发现入场触发，到 manual 审批，到 strategy 自动下单 + 自动放置 reduce-only 阶梯止盈，到 mcap 阈值的 supervisor 软熔断，到操作员在 yuanbao 端实时获取每一档 fill / TP / kill-switch 的推送。
2. **mainnet 闭环 + 第三锁实启用**：把 Phase 4–5 已稳定的 testnet 链路（scanner → strategy → RiskGate → ApprovalGate → 持久 TradingNode → bridge → yuanbao → confirm → fill → cancel）首次切到 mainnet；mainnet 第三锁 `assert_mainnet_unlock` 首次真实被调用。
3. **mcap-aware sizing primitive**：新增"基于市值的 sizing 原语"—— short / long 仓位的 leverage 上限由"liquidation 价对应 mcap 在 entry 反方向距离 ≥ 目标值"反推（短头：P_liq 上方 ≥ $4T；长头：P_liq 下方 ≤ 目标值）。本期作为通用底层提供，SPCX instance 跑短头版。
4. **laddered reduce-only TP placer**：fill 完成后由 strategy 自动在 mcap $2.0T / $1.6T / $1.2T / $0.8T 四档放置 reduce-only limit 卖单（对应 mark 价见 §1.X 表）。任何一档成交后剩余档自动按 remaining size 重新分摊（保持 25%/25%/25%/25% 比例约束）。
5. **soft kill-switch（mcap 上行）**：supervisor 每 iteration 读取 `SPCXUSDT` mark，乘 `shares_outstanding=11.87B` 得 implied mcap；若 mcap > **$3.5T**（mark ≈ $294.86）连续 ≥ 3 个 iteration（防瞬时尖刺），写 sentinel + emit `supervisor.mcap.softkill` + 调 `emergency_close` 取消所有 open TP 单并推 `severity=crit` alert。**不**自动平仓 —— 由操作员手动决定是否再加 short 或市价对冲。
6. **drawdown watcher（资金端）**：保留 v1 设计，HWM 跌 5% halt + alert。在 SPCXUSDT 单标的、$200 margin、1× lev、且入场即"逆势"持仓的语境下，drawdown halt 阈值意义重于普通策略 —— 因为 entry 后浮亏触发 halt 是大概率事件，需在 runbook §11 明确"halt 后不自动 resume，操作员判断是否继续等价格回落"。
7. **24/7 alert + heartbeat 通道**：复用 v1 设计的 yuanbao alert outbound（payload `action=create_alert`）；heartbeat watchdog 用于探测 supervisor / TradingNode 假死。
8. **emergency_close CLI（崩溃可用）**：保留 v1 设计：写 sentinel + 直接调 Binance API 取消所有 open orders（不经 supervisor）+ 推 crit alert。本期增 `--side reduce-only-tp-only` 参数：默认行为是只取消 reduce-only TP 单，**不**取消正在等待的 limit 入场单（因为入场单可能正是当前你要保留的）；操作员可选 `--side all` 取消全部。
9. **Daily holding report**：v1 的 EOD replay 改造为"持仓快照报告" —— 每 24h（UTC 0:00 cron）生成 `reports/phase6/holding_<date>.json`：含 `avg_entry / current_mark / current_mcap / pos_size / unrealized_pnl_usd / hwm_drawdown_pct / tp_ladder_state / soft_kill_distance`，推一条 `info` alert 摘要。
10. **Runbook + 持仓期守夜**：操作员在 mainnet 启用并完成第一次 fill 后跑满 **完整持仓周期**（直到全部 4 档 TP 成交 / 操作员手动平仓 / soft kill 触发 / 接受失败收尾），整理 4 类证据（fill 列表、alert jsonl、audit jsonl、daily holding report 序列）填回 `docs/phase6_results.md`。

### 非使命

- **不接 Telegram / Grafana / Loki / Prometheus / OpenTelemetry**：本期沿用 journalctl + jsonl + yuanbao push 三件套。
- **不开 ML gate**：strategy yaml 不挂 `ml_gate:` 段，default 构造路径已被 Phase 5 `test_momentum_follow_default_construct_does_not_pull_ml_gate` 守护。`mcap_anchored_ladder` 也不挂 ML gate。
- **不开第二 venue**：Binance Spot / Hyperliquid 仍在 venue yaml 中保留 testnet 配置；mainnet supervisor.yaml 只指向 `venues.binance_futures.mainnet.yaml`。
- **不开第二 instrument**：SPCXUSDT 单标的；其它候选品（用户提到的"将更多品种引入候选"）走 Phase 6.x 子任务，每个标的独立 brief（因为策略论点是按品种定制的）。
- **不上 live paper mirror**：用户决定；本期 daily holding report 即代替对照。
- **不引入数据库 / 容器化 / k8s**。
- **不做多策略并行 / 策略组合 / 资金调度**：单策略单标的。
- **不做信号发现增量**：只新增 `threshold_ladder_entry` scanner（SPCX instance 是其首个使用方），其它 Phase 5 scanners 冻结。
- **不做高频 / 低延迟优化**。
- **不做主动 IPO 前平仓**：用户明确接受 IPO gap 风险。
- **不做 funding rate 主动管理**：用户决定忽略（pre-IPO perp 在用户分析中暂不需要 hedging）；本期只在 daily holding report 中**展示**累计 funding 字段，不据此决策。

---

### 1.X 标的与数学约束

#### 合约规格（基于 2026-05-26 公开来源 + scanner 端运行时校验）

| 字段 | 值 | 来源 / 备注 |
|---|---|---|
| 交易对 | `SPCXUSDT` | Binance USDS-M Futures（Pre-IPO Perpetual 系列） |
| 计价 / margin | USDT | USDS-M 系列 |
| 最大杠杆（venue） | **5×** | Binance 对 pre-IPO 品种偏紧档（vs 主流币 75–125×） |
| 本期实际 leverage | **1×** isolated | hard-coded in venues yaml + `binance_futures_mainnet.py::start_node` hook |
| 交易时段 | 24/7 | pre-IPO 不受股市时段限制 |
| 结算 / 锚定 | mark price 由 Binance 内部 oracle 维护（无现货锚） | IPO 之后切换为现货锚（不会 force-settle / 不会强行平仓重定价） |
| MMR (Tier 1) | ~1–2.5% | scanner 端 fetch `exchangeInfo` 真实值；下方 sizing 取保守 2.5% |
| reference shares outstanding | **11.87B**（用户提供） | scanner 启动时 fetch 公开来源（多源比对：S-1 / 媒体），与 `instrument_meta.yaml` 中固定值偏差 > 5% 即 raise，要求人工更新 |

#### 价 ↔ 隐含市值表（shares = 11.87B）

| Implied mcap | mark price | 角色 |
|---|---|---|
| $0.8T | **$67.40** | TP4（最末 25%） |
| $1.2T | **$101.10** | TP3（25%） |
| $1.6T | **$134.79** | TP2（25%） |
| $2.0T | **$168.49** | TP1（首档 25%） |
| $2.4T | $202.19 | 用户论点的"基本上限"（参考线，无 action） |
| $3.0T | $252.74 | （参考线，无 action） |
| $3.5T | **$294.86** | **soft kill-switch**（supervisor 自动触发 emergency_close） |
| $4.0T | **$336.99** | **hard liquidation 安全线**（sizing 公式必须保证 P_liq ≥ 此价） |

#### Sizing 公式（mcap-anchored）

对 isolated short，给定平均入场价 `P_entry`、目标 liq 价 `P_liq_target`、维持保证金率 `MMR`：

```
P_liq_short ≈ P_entry × (1 + 1/L - MMR)
要求 P_liq_short ≥ P_liq_target
=> L ≤ 1 / (P_liq_target / P_entry - 1 + MMR)
```

代入 `P_liq_target = $336.99`，`MMR = 0.025`（保守取 2.5%）：

| 平均入场价 `P_entry` | 最大允许 leverage `L_max` |
|---|---|
| $225（heavy trigger） | ≈ 1.94× → 取 **1×** 安全 |
| $220 | ≈ 1.79× → 取 **1×** |
| $215 | ≈ 1.66× → 取 **1×** |
| $210（DCA trigger 下限） | ≈ 1.54× → 取 **1×** |

> 结论：**1× isolated** 在 entry ∈ [$210, $225] 区间内对 $4T liq 约束有 1.5× 以上余量，本期不需要走"动态降杠杆"逻辑；sizing primitive 在 strategy yaml 设 `target_mcap_liq_usd: 4_000_000_000_000` + `shares_outstanding: 11_870_000_000`，校验逻辑只是"如 leverage 设值 > L_max 即 raise"。

#### 默认策略参数（`strategy.spcxusdt_short_mcap.yaml`，全部 overridable；plugin kind=`mcap_anchored_ladder`）

| 参数 | 值 | 说明 |
|---|---|---|
| `instrument` | `SPCXUSDT-PERP.BINANCE` | 单标的 |
| `direction` | `short` | `short` / `long`；ctor 据此选 sizing 公式与 TP 阶梯方向 |
| `leverage` | `1` | 1× isolated |
| `margin_budget_usd` | `200` | 抵押品上限（含 heavy + DCA） |
| `heavy_entry.trigger_mark_usd` | `225` | 首次触发（fingerprint：策略实例 lifetime 内只 fire 一次） |
| `heavy_entry.margin_usd` | `150` | 占 75% 预算 |
| `heavy_entry.order_type` | `limit` | limit @ `225 - slip_bps`（默认 5 bps） |
| `dca_entry.trigger_mark_usd` | `210` | mark ≥ 210 时每个 UTC 日 fire 一次 |
| `dca_entry.daily_margin_usd` | `10` | 每日 |
| `dca_entry.max_days` | `5` | 共 $50 预算 |
| `dca_entry.fingerprint` | `{strategy}.{instrument}.dca.{utc_date}` | 同日重复 emit 被 strategy 内去重 |
| `tp_ladder` | 见下 | 每档 25%；reduce-only limit |
| `soft_kill.boundary` | `above` | `above` / `below`；short 用 above，long 用 below |
| `soft_kill.trigger_mcap_usd` | `3_500_000_000_000` | mark ≥ 294.86 持续 3 iteration（short / above 语义） |
| `soft_kill.action` | `emergency_close + alert(crit)` | 取消所有 open TP 单（限定 reduce-only），**不**自动平仓 |
| `target_mcap_liq_usd` | `4_000_000_000_000` | sizing 校验硬约束 |
| `time_stop` | `none` | 无 hard time stop |
| `ipo_event_policy` | `continue` | 接受 gap |

`tp_ladder`:
```yaml
tp_ladder:
  - { mcap_usd: 2_000_000_000_000, fraction: 0.25 }  # mark = 168.49
  - { mcap_usd: 1_600_000_000_000, fraction: 0.25 }  # mark = 134.79
  - { mcap_usd: 1_200_000_000_000, fraction: 0.25 }  # mark = 101.10
  - { mcap_usd:   800_000_000_000, fraction: 0.25 }  # mark = 67.40
```

#### 资金风险窗口（绝对值）

- 总 margin 上限 $200。1× isolated。
- 极端单笔最大理论亏损：margin 全损 = **$200**。
- soft kill 触发线对应预期亏损（假设全 $200 margin 已用、avg_entry = $217.50 实测值 / mark = $294.86）：
  - notional_short = $200，份额 = 200/217.5 ≈ 0.9195
  - 浮亏 = 0.9195 × (294.86 − 217.50) ≈ **$71** → 35.5% 浮亏
  - 远低于爆仓 → soft kill 设计意图就是"先于爆仓"主动停手判断
- HWM drawdown halt 阈值 5%：在 $200 margin / 1× / SPCXUSDT 这种高波动 instrument 上，**几乎肯定**会在持仓期内触发至少一次。runbook §11 明确"drawdown halt 后不自动 resume；操作员判断'当前浮亏是预期内还是论点失效'"。

---

## 2. 验收标准（Go / No-Go 清单）

每项明确 PASS / FAIL，记入 `docs/phase6_results.md`。

| ID | 名称 | 描述 |
|---|---|---|
| T1 | mainnet venues yaml + 第三锁实测生效 | `config/venues.binance_futures.mainnet.yaml` 入库（API key / secret 走 `*_env`，永不落 git）；`/etc/xtrade/mainnet_unlock` 由操作员手动 create（0400 root:root）；Phase 5 A5 `assert_mainnet_unlock` 在 supervisor 启动时真实被调用（journalctl `mainnet.unlock.ok`）；offline `tests/test_mainnet_venues_yaml.py` 锁 schema + 拒明文 secret。 |
| T2 | mainnet 紧档 risk yaml + supervisor 拒绝越界 | `config/risk.mainnet.yaml` 4 条 rule（`MaxNotionalPerOrder.usd_cap=160` / `MaxPositionPerSymbol.usd_cap=200` / `MaxTotalNotional.usd_cap=200` / `MaxDrawdownPct.pct=0.05`）入库；`MaxNotionalPerOrder` 上限 $160 允许 heavy 单一次性走完（150 margin × 1× lev = 150 notional，留 10 slip 余量）但拒绝把全部 200 一次性堆进单笔；mainnet 解析触发 `_assert_mainnet_risk_ceiling` 校验 `usd_cap ≤ 200 且 pct ≤ 0.05` 否则 `MainnetRiskTooLooseError`；`tests/test_supervisor_mainnet_risk_ceiling.py` 覆盖 happy / 越界两路径。 |
| T3 | instrument metadata + mcap conversion primitive | 新增 `config/instrument_meta.yaml`：`SPCXUSDT-PERP.BINANCE.shares_outstanding: 11_870_000_000`，`min_qty / qty_step / tick_size`（从 `exchangeInfo` 同步）；新增 `src/xtrade/instruments/meta.py::InstrumentMeta` + `MetaRegistry`；helper `mcap_from_price(price, meta) -> Decimal`、`price_from_mcap(mcap, meta) -> Decimal`；启动时 scanner 校验 venue 端 `exchangeInfo` 拉到的 contract 仍然合法（symbol 存在 + leverage cap 不变小）+ shares_outstanding 与 yaml 差 > 5% 即 raise。 |
| T4 | mcap-aware sizing primitive（**通用，direction 参数化**） | `src/xtrade/live/sizing.py::McapAnchoredSizer(target_mcap_liq_usd, mmr, shares_outstanding, direction: "short"\|"long")`；`max_leverage_for_entry(avg_entry: Decimal) -> Decimal`（short：P_liq 在 entry 之上 / long：在 entry 之下，公式镜像）；`validate_strategy_yaml(...) -> None`（在 strategy ctor 内调，越界 raise `LeverageExceedsMcapCeilingError`，含 `entry`/`computed_L_max`/`requested_L`/`target_mcap_liq`/`direction`）；`tests/test_mcap_sizing.py` 表驱动锁 short / long 两 direction × 4 个 entry 价 × 3 个 mcap_target × MMR ∈ {0.01, 0.025, 0.05}。 |
| T5 | `mcap_anchored_ladder` strategy plugin（**通用，本期首个 instance = SPCX short**） | `src/xtrade/strategy/plugins/mcap_anchored_ladder.py`：kind=`mcap_anchored_ladder`；`direction: short\|long` yaml 参数化；constructor 校验 `intent.side` 与 `cfg.direction` 一致（short→拒 long signal，反之）；`on_start` 调 `McapAnchoredSizer.validate_strategy_yaml`；`on_signal(SignalEnvelope)` 根据 fingerprint 路由（heavy / dca / tp / soft_kill）；`on_fill` 触发 `_rebalance_tp_ladder()` 计算剩余仓位 → 取消 stale TP 单 → 重放新 N 档 limit；TP 阶梯按 `direction` 选取 mcap 低/高方向；不接 ML gate；offline `tests/test_strategy_mcap_anchored_ladder.py` 覆盖：short instance（拒 long signal）/ long instance（拒 short signal）/ heavy fire 一次 / dca 同日去重 / fill 后 TP 重排 / soft_kill 路径只取消不平仓 / leverage 越界 ctor raise。 |
| T6 | `threshold_ladder_entry` scanner（**通用**） | `src/xtrade/research/scanners/threshold_ladder_entry.py`：kind=`threshold_ladder_entry`；yaml 参数化：`instrument / direction: above\|below / heavy_trigger_mark_usd / dca_trigger_mark_usd / dca_window: daily\|hourly / fingerprint_prefix`；watch instrument mark；emit 两类 envelope —— `heavy_entry`（fingerprint `<prefix>.heavy.v1`，lifetime 内首次满足 `direction` 触发条件即 emit），`dca_entry`（fingerprint `<prefix>.dca.<utc_yyyymmdd>`，每个 UTC 日首次满足 dca 条件即 emit）；short 实例：`direction=above` 表示 mark ≥ trigger 触发；long 实例：`direction=below` 表示 mark ≤ trigger 触发；envelope `decision_payload` 含 `intent_side（由 direction 推导）/ qty / order_type=limit / limit_price_bps_offset / margin_usd`；本期 SPCX instance 配 `direction=above / heavy=225 / dca=210 / fingerprint_prefix=spcxusdt.short_mcap.v1`；offline `tests/test_scanner_threshold_ladder_entry.py` 表驱动 short / long 两个 direction × heavy / dca 两类信号 × frozen clock 锁去重与触发。 |
| T7 | drawdown halt（资金端）+ HWM 持久化 | `src/xtrade/live/drawdown.py::DrawdownWatcher`（v1 设计保留）：`/var/lib/xtrade/state/drawdown.json` atomic write；`halt_pct=0.05`；halt → sentinel + event + alert；**不**自动 resume；offline `tests/test_drawdown_watcher.py` + `tests/test_supervisor_drawdown_integration.py`。 |
| T8 | soft kill watchdog（mcap 端，**通用**） | `src/xtrade/live/mcap_softkill.py::McapSoftKillWatcher(meta, trigger_mcap_usd, boundary: "above"\|"below", consecutive_iterations=3)`：`boundary=above` 短头版（mcap 上穿 trigger 触发）；`boundary=below` 长头版（mcap 下穿 trigger 触发）；在 supervisor iteration loop 内拉 mark → 算 mcap → 按 boundary 检查越线 ≥ 3 次连续 iteration 则 `should_trigger()=True`；触发 → 写 sentinel `reason=mcap.softkill:<boundary>:<mcap>` + emit `supervisor.mcap.softkill` 事件 + 调 `emergency_close_runner(side="reduce-only-tp-only")` 内部入口 + 推 `crit` alert；本期 SPCX instance 配 `boundary=above / trigger=3.5T`；offline `tests/test_mcap_softkill_watcher.py` 表驱动 above / below 两 boundary × 单点尖刺不触发 / 连续 3 次触发 / 恢复后状态重置 / 持久化重启。 |
| T9 | heartbeat watchdog + yuanbao alerter | `src/xtrade/live/heartbeat.py::HeartbeatWatcher(idle_warn_s=600, idle_crit_s=1800)`（v1 保留）；`src/xtrade/bridge/alerter.py::AlertBridge`（v1 保留，复用 OpenclawBridge 重试 / scrub / audit）；payload `action=create_alert` + `severity ∈ {info, warn, crit}`；offline `tests/test_heartbeat_watcher.py` + `tests/test_bridge_alerter.py` + `tests/test_supervisor_alerter_integration.py`。 |
| T10 | emergency_close CLI | `xtrade ops emergency_close --yes [--instrument SPCXUSDT-PERP.BINANCE] [--side {reduce-only-tp-only,all}]`：默认 `reduce-only-tp-only`（只取消 reduce-only TP 单，保留入场限价单）；`--side all` 取消全部 open orders；写 sentinel + 直接调 Binance API + 推 crit alert + 退出码 0 / 2；`tests/test_cli_emergency_close.py`：httpx MockTransport 4 路径 + `--side` 两路径 + 缺 `--yes` 拒空。 |
| T11 | daily holding report + runbook + 持仓期实测 | `xtrade ops holding_report <date>` 子命令 + `scripts/phase6/01_daily_holding_report.sh`；输出 `reports/phase6/holding_<date>.json`，schema 见 §5.T11；推一条 `info` alert。`docs/phase6_runbook_vps.md` 覆盖：preflight / 启动 / 半自动审批流程 / 巡检节奏 / 告警分级响应 / drawdown halt 处置 / soft kill 处置 / IPO event 守夜 / 平仓收尾。操作员在 mainnet 启用 + 第一次 fill 后跑满**完整持仓周期**，4 类证据填回 `docs/phase6_results.md`。 |

---

## 3. 不在本阶段处理的事项（显式延后）

- **ML gate 上 mainnet**：推到 Phase 6.1。
- **多 venue mainnet**：Binance Spot / Hyperliquid mainnet 推到 Phase 6.2 / 6.3。
- **第二 instrument**：用户接下来想引入更多品种（"将更多品种引入候选"），按品种独立 brief，每个品种独立 strategy plugin（因为论点 / sizing 公式 / TP 阶梯都按品种定制）。
- **HIP-3 真实股票永续**：仍未验证；独立 phase。
- **Grafana / Loki / Prometheus / Telegram**：本期不接；如 yuanbao 通道在持仓期暴露缺陷，开 Phase 6.4 "double-channel alerting"。
- **多策略 / 策略组合 / 资金调度 / 自动加减仓**：单策略单标的。
- **数据库 / 容器化**：延续 Phase 4–5 决策。
- **OTP / 二人复核**：mainnet 第三锁已是 root-only token 文件 + env 双比对。
- **funding rate 自动 hedge / 主动管理**：本期只展示，不据此决策。
- **IPO 前主动平仓 / IPO 后调整 sizing 模型**：用户明确接受 gap；IPO 后随着锚切到现货价，shares_outstanding 不变 → 公式不需要改，仅 `mark_source` 在 scanner 端从 pre-IPO oracle 切到现货 anchor 永续 oracle（同一 Binance 内部字段）。

---

## 4. 仓库结构变更

### 新增源码

```
src/xtrade/instruments/__init__.py              # T3
src/xtrade/instruments/meta.py                  # T3 InstrumentMeta + MetaRegistry
src/xtrade/live/sizing.py                       # T4 McapAnchoredSizer
src/xtrade/live/drawdown.py                     # T7 drawdown watcher
src/xtrade/live/heartbeat.py                    # T9 heartbeat watcher
src/xtrade/live/mcap_softkill.py                # T8 mcap soft kill watcher
src/xtrade/bridge/alerter.py                    # T9 yuanbao alert outbound
src/xtrade/strategy/plugins/mcap_anchored_ladder.py       # T5 strategy plugin（通用，direction 参数化）
src/xtrade/research/scanners/threshold_ladder_entry.py    # T6 scanner（通用，direction + trigger 参数化）
src/xtrade/ops/holding_report.py                # T11 daily holding report
src/xtrade/ops/emergency_close.py               # T10 emergency_close 共享 runner
src/xtrade/cli.py                               # +emergency_close, +holding_report 子命令
src/xtrade/live/supervisor.py                   # 集成 T7 / T8 / T9
src/xtrade/live/binance_futures_mainnet.py      # 1× leverage 强制 + isolated 强制
```

### 新增测试

```
tests/test_mainnet_venues_yaml.py                       (T1)
tests/test_supervisor_mainnet_risk_ceiling.py           (T2)
tests/test_instrument_meta.py                           (T3)
tests/test_mcap_sizing.py                               (T4)
tests/test_strategy_mcap_anchored_ladder.py             (T5)
tests/test_scanner_threshold_ladder_entry.py            (T6)
tests/test_drawdown_watcher.py                          (T7)
tests/test_supervisor_drawdown_integration.py           (T7)
tests/test_mcap_softkill_watcher.py                     (T8)
tests/test_supervisor_mcap_softkill_integration.py      (T8)
tests/test_heartbeat_watcher.py                         (T9)
tests/test_bridge_alerter.py                            (T9)
tests/test_supervisor_alerter_integration.py            (T9)
tests/test_cli_emergency_close.py                       (T10)
tests/test_holding_report.py                            (T11)
tests/test_cli_holding_report.py                        (T11)
```

### 配置与部署

```
config/risk.mainnet.yaml                                # T2 紧档
config/venues.binance_futures.mainnet.yaml              # T1（所有 secret 走 *_env）
config/instrument_meta.yaml                             # T3
config/strategy.spcxusdt_short_mcap.yaml                # T5 SPCX instance（plugin kind=mcap_anchored_ladder + direction=short）
config/scanner.spcxusdt_threshold_ladder.yaml           # T6 SPCX instance（kind=threshold_ladder_entry + direction=above + heavy=225 + dca=210）
config/supervisor.mainnet.spcxusdt.example.yaml         # 串起 venue + risk + meta + strategy + scanner
deploy/env/xtrade.env.example                           # +XTRADE_MAINNET_BINANCE_FUTURES_{API_KEY,API_SECRET}, +XTRADE_MAINNET_UNLOCK_TOKEN, +XTRADE_ALERT_CHANNEL=yuanbao
scripts/phase6/01_daily_holding_report.sh               # T11
scripts/phase6/02_preflight_mainnet.sh                  # 启动前 sanity check
docs/phase6_brief.md                                    # 本文件
docs/phase6_runbook_vps.md                              # 持仓期操作手册（T11）
docs/phase6_results.md                                  # 收尾报告
```

### VPS 文件布局新增

```
/var/lib/xtrade/state/drawdown.json                     # 0640 xtrade:xtrade
/var/lib/xtrade/state/mcap_softkill.json                # 0640 xtrade:xtrade（连续越线计数 + 上次状态）
/var/lib/xtrade/audit/alerts.<YYYY-MM-DD>.jsonl         # 0640 xtrade:xtrade
/etc/xtrade/risk.mainnet.yaml                           # 0640 root:xtrade
/etc/xtrade/venues.binance_futures.mainnet.yaml         # 0640 root:xtrade
/etc/xtrade/instrument_meta.yaml                        # 0640 root:xtrade
/etc/xtrade/strategy.spcxusdt_short_mcap.yaml           # 0640 root:xtrade
/etc/xtrade/scanner.spcxusdt_threshold_ladder.yaml      # 0640 root:xtrade
/etc/xtrade/supervisor.yaml                             # 复写：指向 mainnet venue + 紧档 risk + meta + strategy
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
- `src/xtrade/live/binance_futures_mainnet.py`：node start hook（**通用**，从 supervisor.yaml 的 `mainnet_instrument_overrides:` 段拉 instrument 列表 + 每 instrument 的 `leverage` / `margin_type` 配置；本期 SPCX 配 `{SPCXUSDT: {leverage: 1, margin_type: ISOLATED}}`）：
  - 对每个 instrument 调 `client.futures_change_leverage(symbol, leverage)` 并断言返回值与配置一致
  - 对每个 instrument 调 `client.futures_change_margin_type(symbol, marginType)`
  - 若任何调用失败 → raise `MainnetVenueBootstrapError`，supervisor 启动失败
- 测试 `tests/test_mainnet_venues_yaml.py`：(a) yaml 解析 `environment="MAINNET"`；(b) 任何 `api_key:` 明文字段 → schema 拒；(c) `_resolve_env_ref` env 缺失给路径化 message（Phase 5 Bug 7 守护回归）。

#### Task T2 —— mainnet 紧档 risk yaml

- `config/risk.mainnet.yaml`：
  ```yaml
  rules:
    - type: max_notional_per_order_usd
      usd_cap: 160          # 允许 heavy $150 一次性走完，留 10 slip 余量；同时拒绝把 $200 全部堆进单笔
    - type: max_position_per_symbol_usd
      usd_cap: 200
    - type: max_total_notional_usd
      usd_cap: 200
    - type: max_drawdown_pct
      pct: 0.05
  ```
- supervisor 解析为 mainnet venue 时调 `_assert_mainnet_risk_ceiling(rules)` 校验 `MaxNotional*.usd_cap ≤ 200` + `MaxDrawdownPct.pct ≤ 0.05`，违规 raise `MainnetRiskTooLooseError`。
- 测试 `tests/test_supervisor_mainnet_risk_ceiling.py`：testnet+大 cap=OK；mainnet+大 cap=raise；mainnet+紧档=OK；mainnet 缺 rule=raise。

#### Task T3 —— instrument metadata + mcap conversion

- `config/instrument_meta.yaml`：
  ```yaml
  SPCXUSDT-PERP.BINANCE:
    shares_outstanding: 11_870_000_000
    min_qty: 0.001          # 占位，scanner 启动时从 exchangeInfo 校准
    qty_step: 0.001
    tick_size: 0.01
    mark_source: oracle     # IPO 后切 spot_anchor（手动 yaml 更新）
  ```
- `src/xtrade/instruments/meta.py`：
  ```python
  @dataclass(frozen=True)
  class InstrumentMeta:
      symbol: str
      shares_outstanding: Decimal
      min_qty: Decimal
      qty_step: Decimal
      tick_size: Decimal
      mark_source: str

  class MetaRegistry:
      @classmethod
      def load(cls, path: Path) -> "MetaRegistry": ...
      def get(self, symbol: str) -> InstrumentMeta: ...

  def mcap_from_price(price: Decimal, meta: InstrumentMeta) -> Decimal:
      return price * meta.shares_outstanding

  def price_from_mcap(mcap: Decimal, meta: InstrumentMeta) -> Decimal:
      return mcap / meta.shares_outstanding
  ```
- scanner 启动时 sanity check：venue `exchangeInfo` 拉到的 contract `symbol` 存在；shares_outstanding 与公开来源（用户提供 + 抓 2 个公开页面比对）差 > 5% 即 raise `InstrumentMetaStaleError`，要求人工更新 yaml；该校验可被 `--skip-shares-recheck` 跳过（在 brief 中不推荐）。
- 测试 `tests/test_instrument_meta.py`：yaml 解析 / `mcap_from_price` 与 `price_from_mcap` round-trip / 缺字段 raise / `qty_step` round-down helper。

#### Task T4 —— mcap-aware sizing primitive

- `src/xtrade/live/sizing.py`：
  ```python
  Direction = Literal["short", "long"]

  @dataclass(frozen=True)
  class McapAnchoredSizer:
      target_mcap_liq_usd: Decimal       # 4_000_000_000_000 for SPCX short
      mmr: Decimal                        # 0.025
      shares_outstanding: Decimal         # 11_870_000_000
      direction: Direction                # "short" → P_liq above entry; "long" → below

      def max_leverage_for_entry(self, avg_entry: Decimal) -> Decimal:
          p_liq = self.target_mcap_liq_usd / self.shares_outstanding
          if self.direction == "short":
              # short: P_liq ≈ entry × (1 + 1/L − mmr) ≥ p_liq
              # → L ≤ 1 / (p_liq/avg_entry − 1 + mmr)
              denom = (p_liq / avg_entry) - Decimal(1) + self.mmr
          else:
              # long: P_liq ≈ entry × (1 − 1/L + mmr) ≤ p_liq
              # → L ≤ 1 / (1 − p_liq/avg_entry + mmr)
              denom = Decimal(1) - (p_liq / avg_entry) + self.mmr
          if denom <= 0:
              return Decimal("Infinity")
          return Decimal(1) / denom

      def validate_strategy_yaml(self, *, requested_leverage: Decimal,
                                  reference_entry: Decimal) -> None: ...
  ```
- short instance：strategy ctor 内调 `validate_strategy_yaml(requested_leverage=1, reference_entry=Decimal("210"))`（取 DCA 下限作 worst-case 平均入场假设）。
- long instance（未来用）：reference_entry 取 dca_trigger_mark_usd 作 best-case 入场假设（long 下行突破入场 → 入场价高估 = leverage 上限低估 = 更保守）。
- 越界 raise `LeverageExceedsMcapCeilingError`，message 含 `direction / entry / L_max / requested_L / target_mcap_liq`。
- 测试 `tests/test_mcap_sizing.py`：表驱动 short / long 两 direction × 4 entry × 3 mcap_target × 3 MMR；边界（entry 与 p_liq 同侧 → Infinity）；越界 raise message 内容。

#### Task T5 —— `mcap_anchored_ladder` strategy plugin（通用，direction 参数化）

> 设计目标：本期 SPCX short 是第一个 instance；下一个标的若也走"mcap 锚 + 阶梯止盈 + 价格阈值入场"模式，**只加 yaml 不改代码**。direction=long 路径与 short 镜像，本期 offline 测试就锁住 long instance 的合规性，避免下次接 long 时还要回头改 plugin。

- `src/xtrade/strategy/plugins/mcap_anchored_ladder.py`：
  ```python
  class McapAnchoredLadderStrategy(StrategyBase):
      kind = "mcap_anchored_ladder"

      def __init__(self, cfg: StrategyCfg, meta_registry: MetaRegistry, ...):
          assert cfg.direction in ("short", "long"), f"bad direction: {cfg.direction}"
          self._direction = cfg.direction
          self._expected_side: Literal["short", "long"] = cfg.direction
          self._meta = meta_registry.get(cfg.instrument)
          self._sizer = McapAnchoredSizer(
              target_mcap_liq_usd=cfg.target_mcap_liq_usd,
              mmr=cfg.mmr,
              shares_outstanding=self._meta.shares_outstanding,
              direction=cfg.direction,             # short → P_liq above entry; long → below
          )
          self._sizer.validate_strategy_yaml(
              requested_leverage=cfg.leverage,
              reference_entry=cfg.dca_entry.trigger_mark_usd,
          )
          self._heavy_fired: bool = False
          self._dca_fired_dates: set[date] = set()
          self._tp_state: TpLadderState = TpLadderState.empty()

      def on_signal(self, env: SignalEnvelope) -> StrategyDecision:
          if env.intent.side != self._expected_side:
              return StrategyDecision.reject(
                  f"side {env.intent.side} does not match strategy direction {self._direction}"
              )
          ...

      def on_fill(self, fill: FillEvent) -> list[ChildOrder]:
          # 入场 fill 之后：按 direction 计算 tp ladder（short=mcap 递减 / long=mcap 递增）
          # tp fill 之后：rebalance 剩余仓位到剩余档
          ...
  ```
- TP 阶梯重排逻辑（fill → rebalance）：保留 N 档比例（默认 25%/25%/25%/25%）；若 TP1 已 fill，剩余 75% 仓位重新分配到 TP2/3/4 → 各 33.3%。
- soft kill 信号路由：来自 `McapSoftKillWatcher` 的 `signal.kind="soft_kill"` envelope → strategy 不 emit 新 child order，而是 `_cancel_all_open_tp_orders_only()`（不动入场限价单）；supervisor 端同时挂 sentinel + alert。
- SPCX instance 在 `config/strategy.spcxusdt_short_mcap.yaml` 里实例化：`kind: mcap_anchored_ladder` + `direction: short` + 完整参数表（见 §1.X）。
- 测试 `tests/test_strategy_mcap_anchored_ladder.py`：
  - short instance：拒 long signal / heavy fire 一次 / dca 同日去重 / fill 后 TP 递减档重排 / soft kill 路径只 cancel 不平仓 / leverage 越界 ctor raise
  - long instance：mirror 用例 —— 拒 short signal / fill 后 TP 递增档重排
  - 边界：`direction` 缺失 / 非法值 → ctor raise

#### Task T6 —— `threshold_ladder_entry` scanner（通用，direction 参数化）

> 设计目标：本期 SPCX 是第一个 instance（direction=above，heavy=$225，dca=$210）；下一个标的若也走"价格阈值跨越 + 首次重仓 + 日 DCA"模式，**加 yaml 即可**。

- `src/xtrade/research/scanners/threshold_ladder_entry.py`：
  ```python
  class ThresholdLadderEntryScanner(ScannerBase):
      kind = "threshold_ladder_entry"

      def __init__(self, cfg: ScannerCfg, clock: Clock, meta: InstrumentMeta):
          assert cfg.direction in ("above", "below"), f"bad direction: {cfg.direction}"
          self._direction = cfg.direction
          self._intent_side = "short" if cfg.direction == "above" else "long"
          self._fingerprint_prefix = cfg.fingerprint_prefix     # e.g. "spcxusdt.short_mcap.v1"
          ...

      def _crossed(self, mark: Decimal, trigger: Decimal) -> bool:
          return mark >= trigger if self._direction == "above" else mark <= trigger

      def on_mark(self, ts: datetime, mark: Decimal) -> list[SignalEnvelope]:
          out: list[SignalEnvelope] = []
          if not self._heavy_fired and self._crossed(mark, self._cfg.heavy_trigger_mark_usd):
              out.append(self._emit_heavy(ts, mark))   # fingerprint = f"{prefix}.heavy.v1"
              self._heavy_fired = True
          today = ts.astimezone(UTC).date()
          if (self._crossed(mark, self._cfg.dca_trigger_mark_usd)
                  and today not in self._dca_fired_dates):
              out.append(self._emit_dca(ts, mark, today))  # fingerprint = f"{prefix}.dca.{yyyymmdd}"
              self._dca_fired_dates.add(today)
          return out
  ```
- envelope `decision_payload`：strategy 端只验签 + 转 RiskGate，不重新算 qty / price；scanner 已在 envelope 里写好（含 `intent_side` 由 `direction` 推导）。
- mark 数据源：复用 Phase 5 已有的 Binance Futures 行情订阅；本期不引入额外 websocket。
- SPCX instance 在 `config/scanner.spcxusdt_threshold_ladder.yaml`：
  ```yaml
  kind: threshold_ladder_entry
  instrument: SPCXUSDT-PERP.BINANCE
  direction: above
  heavy_trigger_mark_usd: 225
  dca_trigger_mark_usd: 210
  dca_window: daily
  fingerprint_prefix: spcxusdt.short_mcap.v1
  ```
- 测试 `tests/test_scanner_threshold_ladder_entry.py`：
  - direction=above（SPCX-style）：heavy fire 一次 / mark 回落再越线不再 emit / dca 同日去重 / 跨 UTC 日重新 emit
  - direction=below（长头 mirror）：同样表驱动用例
  - envelope `intent_side` 与 direction 对应（above→short，below→long）
  - envelope 内 `qty` 满足 `qty_step` 量化
  - `fingerprint_prefix` 缺失 → ctor raise

#### Task T7 —— drawdown watcher（资金端）

- 完全沿用 v1 设计：`DrawdownWatcher(hwm_path, halt_pct=Decimal("0.05"))`；atomic write；halt → sentinel + event + alert；**不**自动 resume；可选 `xtrade ops reset_drawdown_hwm --yes` 写新 HWM。
- 在 SPCXUSDT 高波动语境下，runbook 必须说明 "drawdown halt 后 90% 概率是浮亏正常波动而非论点失效"，操作员判断时机。
- 测试 `tests/test_drawdown_watcher.py` + `tests/test_supervisor_drawdown_integration.py`。

#### Task T8 —— mcap soft kill watcher（通用，boundary 参数化）

> 设计目标：short 头版（boundary=above，mcap 上穿 trigger 触发）与 long 头版（boundary=below，mcap 下穿 trigger 触发）共享同一 watcher 类；SPCX instance 本期跑 above。

- `src/xtrade/live/mcap_softkill.py`：
  ```python
  @dataclass(frozen=True)
  class McapSoftKillState:
      consecutive_breaches: int
      last_mcap_usd: Decimal
      triggered: bool

  Boundary = Literal["above", "below"]

  class McapSoftKillWatcher:
      def __init__(self, meta: InstrumentMeta, trigger_mcap_usd: Decimal,
                   boundary: Boundary,
                   consecutive_iterations: int = 3,
                   state_path: Path = Path("/var/lib/xtrade/state/mcap_softkill.json")): ...

      def _is_breached(self, mcap: Decimal) -> bool:
          return (mcap >= self._trigger if self._boundary == "above"
                  else mcap <= self._trigger)

      def update(self, now: datetime, mark: Decimal) -> McapSoftKillState: ...
  ```
- supervisor iteration 内：拉 instrument mark → `watcher.update(now, mark)` → 若 `triggered=True` 且 sentinel 中尚无 `mcap.softkill` 条目，则：
  1. 写 sentinel `reason=mcap.softkill:<boundary>:<mcap_usd>`
  2. emit `supervisor.mcap.softkill` 事件（fields 含 `boundary`）
  3. 调 `xtrade.ops.emergency_close.run(side="reduce-only-tp-only", instrument=...)`（共享 runner）
  4. 推 `severity=crit` alert
- SPCX instance 配 `boundary=above / trigger_mcap_usd=3_500_000_000_000`。
- recovery 不自动放开（与 drawdown halt 同语义）：操作员 `xtrade ops resume` 清 sentinel 之后 watcher state 才会回到 `consecutive=0`。
- 测试 `tests/test_mcap_softkill_watcher.py`：
  - boundary=above：连续 3 次越线触发 / 单点尖刺不触发 / 恢复后 consecutive 重置 / 持久化重启不丢
  - boundary=below：mirror 用例
  - `boundary` 非法值 → ctor raise
- `tests/test_supervisor_mcap_softkill_integration.py`：fake mark feed → 验证 sentinel + event + alert + emergency_close runner 调用四件齐发；sentinel reason 含 boundary 字段。

#### Task T9 —— heartbeat watcher + alerter outbound

- 完全沿用 v1 设计：`HeartbeatWatcher(idle_warn_s=600, idle_crit_s=1800)` + `AlertBridge(openclaw_endpoint, secret, audit_writer)`；payload `action=create_alert`；同档不重复推；恢复推一次 info。
- 测试 `tests/test_heartbeat_watcher.py` + `tests/test_bridge_alerter.py` + `tests/test_supervisor_alerter_integration.py`。
- 操作员侧依赖：openclaw 端 `create_alert` 路由 / TaskFlow 与 Phase 4 §1.7 `create_flow` 一致框架，对齐细节进 `phase6_runbook_vps.md`。

#### Task T10 —— emergency_close CLI

- `xtrade ops emergency_close --yes [--instrument SPCXUSDT-PERP.BINANCE] [--side {reduce-only-tp-only,all}]`：
  - 默认 `--side reduce-only-tp-only`（与 supervisor mcap soft kill 路径一致语义）
  - `--side all` 取消该 instrument 所有 open orders
  - sentinel 写入 / Binance API 自构 httpx client（不经 supervisor）/ crit alert / 退出码 0=全成功 / 2=至少一笔失败
- `src/xtrade/ops/emergency_close.py` 共享 runner（CLI + supervisor 都调）：
  ```python
  def run(*, side: Literal["reduce-only-tp-only","all"], instrument: str,
          venues_yaml: Path, sentinel_path: Path, alerter: AlertBridge | None) -> int: ...
  ```
- 测试 `tests/test_cli_emergency_close.py`：httpx MockTransport 4 路径（200 / 401 / 429 / 5xx）+ `--side` 两路径 + `reduce-only-tp-only` 路径下确保未取消非 reduce-only 单（mock filter assertion）+ 缺 `--yes` 拒空。

#### Task T11 —— daily holding report + 持仓期 runbook

- `src/xtrade/ops/holding_report.py::compute_holding_report(date, fills_source, mark_source, meta) -> HoldingReport`：
  - schema：
    ```json
    {
      "date": "2026-MM-DD",
      "instrument": "SPCXUSDT-PERP.BINANCE",
      "avg_entry_usd": "...",
      "current_mark_usd": "...",
      "current_mcap_usd": "...",
      "pos_size": "...",
      "unrealized_pnl_usd": "...",
      "realized_pnl_usd": "...",
      "hwm_drawdown_pct": "...",
      "tp_ladder_state": [
        {"target_mcap_usd": "2000000000000", "target_mark_usd": "168.49", "filled_qty": "...", "open_qty": "..."},
        ...
      ],
      "soft_kill_distance": {
        "mcap_now_usd": "...",
        "mcap_trigger_usd": "3500000000000",
        "headroom_pct": "..."
      },
      "funding_paid_cumulative_usd": "...",
      "generated_at": "...Z"
    }
    ```
- `xtrade ops holding_report <date>`：写 `reports/phase6/holding_<date>.json` + 推一条 `severity=info` alert 摘要（`{avg_entry, current_mark, unrealized_pnl, soft_kill_headroom_pct}`）。
- `scripts/phase6/01_daily_holding_report.sh`：thin wrapper，默认 `date=$(date -u +%Y-%m-%d)`。
- `docs/phase6_runbook_vps.md` 章节：
  1. **启动前 preflight**（`scripts/phase6/02_preflight_mainnet.sh`）：三锁齐备 / risk 紧档 / instrument_meta 校验 / venue ping / mainnet API key 权限校验（必须 disable withdrawals + IP 白名单）/ alert 通道 dry-run。
  2. **启动**：`systemctl start xtrade-supervisor.service`；journalctl 确认 `mainnet.unlock.ok` + `supervisor.start` + `scanner.threshold_ladder.start(instance=spcxusdt_threshold_ladder)` + 一条 info alert。
  3. **半自动审批流程**：scanner emit envelope → bridge_out → yuanbao → 操作员看到 push → 在 openclaw 端 confirm → bridge_in → ApprovalGate → strategy → child order；超时 / reject 路径与 Phase 4 一致。
  4. **巡检节奏**：每 1h `xtrade ops status --json | jq '{paused,disk,drawdown,bridge,counts,mcap_softkill}'`；每 4h venue 端账户余额 / 持仓 / open orders 三向对账。
  5. **告警分级响应**：warn = 日志 + 继续；crit = 立即评估是否需 `xtrade ops emergency_close --side all`；soft kill 自动触发后 30 min 内操作员到位决策。
  6. **drawdown halt 处置**：SPCXUSDT 高波动 → halt 大概率是浮亏正常波动；判断"是否论点失效（mark 上升 / 上行突破 $225 历史高）/ 是否仓位过大（重审 sizing）/ 是否继续等价格回落"；不轻易 resume。
  7. **soft kill 处置**：mark / mcap 越线 → emergency_close 已自动 cancel reduce-only TP；操作员决策"是否市价对冲（不推荐，滑点大）/ 是否再加 short（不推荐，已在论点边界）/ 是否硬扛"。
  8. **IPO event 守夜**：IPO 公布前后 ±24h 加密巡检（每 15 min）；mark 锚切换瞬间观察 mcap 跳变 + open orders 是否被 venue 自动取消重排（实测看 venue 行为，runbook 留 placeholder）；事件后立即跑 holding_report 留快照。
  9. **收尾**：所有 TP 都成交 / 操作员手动平仓 / 接受失败止损平仓 三种路径分别的对账 + 复盘模板。
- `docs/phase6_results.md` 由操作员在持仓周期结束后填回 4 类证据。

---

## 6. 风险模型与 sizing 边界（综述）

### 资金风险窗口

- 总 margin 上限 $200，1× isolated，单 instrument。
- 极端最大理论亏损：margin 全损 = **$200**（前提是 soft kill 失效 + venue 端流动性塌方 + liq engine 故障三重失效）。
- soft kill 触发预期亏损：~$71（35% 浮亏，见 §1.X）—— 此时已远在爆仓之上，目的是让操作员二次决策而非 venue 强平。
- HWM drawdown halt 在 SPCXUSDT 上**几乎必然**触发至少一次；runbook §11 明确"halt 不等价于论点失效"。

### Sizing 硬约束

- `target_mcap_liq_usd = 4_000_000_000_000` + `shares_outstanding = 11_870_000_000` + `MMR = 0.025`
- → `P_liq_target = $336.99` （对应 $4T mcap）
- → 在 entry ∈ [$210, $225] 区间内，**1×** leverage 自动满足约束（L_max ≈ 1.5–1.9×，1× 留 50%+ 余量）
- 若 scanner 后续允许 entry 价上移到 > $225（本期不允许，但 6.x 子任务可能改），sizing primitive 自动卡 leverage 上限（写 stratergy yaml 时 ctor 会 raise）

### IPO event 风险

- mark 锚切换瞬间可能跳变（gap up = 浮亏放大 / gap down = 浮盈兑现）。
- 用户接受此风险；策略 / supervisor 不做主动 IPO 前平仓。
- soft kill 在 gap up 大幅突破 $294.86 时会立即触发 → emergency_close cancel TP → 操作员手动决策。
- daily holding report 在 IPO 公布日运行加密版本（每 1h 一次代替每 24h）—— runbook §11.8 强制操作员执行。

### API key 安全

- mainnet key 仅赋 `Enable Futures` + **禁用** `Enable Withdrawals`（Binance 后台配置；runbook checklist）。
- IP 白名单：仅 VPS 公网 IP。
- key 在 `/etc/xtrade/env` 0640 root:xtrade，不入 git。

---

## 7. 决策矩阵（Phase 6 收尾时）

不同于 v1 的"24h 时钟"语义，本期是**事件驱动**收尾。

| 结果 | 判断 | 下一步 |
|---|---|---|
| 全部 4 档 TP 成交、累计实盈 > $0、过程中无 crit alert、soft kill 未触发 | **Phase 6 PASS（理想路径）** | 复盘 alpha 是否可推广；按用户路线引入第二 instrument（独立 brief）；考虑 Phase 6.1 ML gate paper mirror |
| 部分 TP 成交（如 TP1 + TP2）+ 操作员主动平仓收尾、累计 P&L > 0 | **Phase 6 PASS（保守路径）** | 流程已验证；与理想路径同后续 |
| soft kill 自动触发 + 操作员手动平仓收尾 + 亏损 ≤ $50 | **有条件 PASS** | 流程验证 OK；但论点边界比预期更近；复盘是否需要把 `soft_kill.trigger_mcap_usd` 下移到 $3.0T / 是否需要紧档 sizing |
| drawdown halt 多次触发（≥ 3 次）但都因价格回落自动放开 | **有条件 PASS** | drawdown halt 频率超预期 → 复盘 halt_pct 是否过紧（5% 在高波动 SPCXUSDT 上确实易触）；若可接受，进入下一阶段；若不可接受，把 halt_pct 调到 8–10% 后再 24h 烟测 |
| 累计亏损超 $100（半数 margin） | **NOT PASS** | 立即 `xtrade ops emergency_close --side all` + `systemctl stop`；写 incident report；论点失效 → 不补跑；回退到 testnet 跑 1 周再考虑下一品种 |
| crit alert 发出但未由 yuanbao push 接收（操作员 6h 内未感知） | **NOT PASS（基础设施层）** | 告警通道未达 SLA；不论 P&L 如何，触发 Phase 6.4 "double-channel alerting"，单独立项后再回头补 Phase 6 收尾 |
| Phase 4 §1.6 / §1.7 或 Phase 5 Track A VPS 复跑任一未签字就启用 mainnet | **流程违规，自动 FAIL** | 不应发生；如发生需 incident review |
| IPO event 期间 venue 端发生 force-settle / 强行平仓重定价（即与公开说明不一致） | **Phase 6 中断、非策略失败** | 留 venue-behavior incident report；本期判定为基础设施验证完成但策略验证未完成，下一品种 brief 内重做 |

---

## 8. 交付物

1. `src/xtrade/instruments/meta.py` + `config/instrument_meta.yaml`（T3）
2. `src/xtrade/live/sizing.py`（T4）
3. `src/xtrade/strategy/plugins/mcap_anchored_ladder.py`（通用，direction 参数化）+ `config/strategy.spcxusdt_short_mcap.yaml`（SPCX instance）（T5）
4. `src/xtrade/research/scanners/threshold_ladder_entry.py`（通用，direction + trigger 参数化）+ `config/scanner.spcxusdt_threshold_ladder.yaml`（SPCX instance）（T6）
5. `src/xtrade/live/drawdown.py` + supervisor 集成（T7）
6. `src/xtrade/live/mcap_softkill.py` + supervisor 集成（T8）
7. `src/xtrade/live/heartbeat.py` + supervisor 集成（T9）
8. `src/xtrade/bridge/alerter.py` + supervisor 集成（T9）
9. `src/xtrade/ops/emergency_close.py` + `xtrade ops emergency_close` CLI（T10）
10. `src/xtrade/ops/holding_report.py` + `xtrade ops holding_report` CLI（T11）
11. `config/risk.mainnet.yaml` + `config/venues.binance_futures.mainnet.yaml` + `config/supervisor.mainnet.spcxusdt.example.yaml`（T1 + T2）
12. `deploy/env/xtrade.env.example` 增 mainnet env 占位（T1）
13. `scripts/phase6/01_daily_holding_report.sh` + `scripts/phase6/02_preflight_mainnet.sh`（T11）
14. `tests/` 下 16 个新测试文件，`pytest tests/` 全绿，新增用例 ≥ 110（T5 / T6 / T8 因 direction 参数化各加 mirror 用例集）
15. `docs/phase6_brief.md`（本文件 v2）、`docs/phase6_runbook_vps.md`、`docs/phase6_results.md`
16. operator 在 VPS 上完成 **完整持仓周期** mainnet 守夜（启动前 preflight PASS + 4 类证据齐 + daily holding report 序列），结果记入 `docs/phase6_results.md`

---

## 9. 建议执行顺序

代码 / 测试侧（与 Phase 4 §1.6/§1.7 + Phase 5 Track A VPS 复跑并行）：

1. **T3 instrument metadata**（最底层，所有上层都引用）
2. **T4 mcap-aware sizing primitive**（独立工具，纯函数易测）
3. **T1 mainnet venues yaml + T2 紧档 risk yaml**（配置 + schema 校验）
4. **T5 mcap_anchored_ladder strategy plugin**（依赖 T3 / T4；direction 参数化，short + long 两路径同步落测试）
5. **T6 threshold_ladder_entry scanner**（依赖 T3；direction + trigger 参数化；与 T5 形成最小闭环）
6. **T9 alerter outbound**（独立 bridge，被 T7 / T8 / T10 / T11 共用）
7. **T7 drawdown watcher**（v1 移植，独立模块）
8. **T8 mcap soft kill watcher**（依赖 T3 + T9 + T10 共享 runner）
9. **T10 emergency_close CLI + 共享 runner**（依赖 T1 + T9）
10. **T9 heartbeat watcher**（与 T7 / T8 平行）
11. **T11 daily holding report**（依赖 T3 + 已有 fill / mark 流）
12. supervisor 集成 wire-up（T7 / T8 / T9）+ `supervisor.mainnet.spcxusdt.example.yaml`

操作员（VPS）侧顺序：

1. 把 Phase 4 §1.6 4 个 drill + §1.7 openclaw 全链跑完并填回 `docs/phase4_results.md`
2. 把 Phase 5 Track A VPS 复跑跑完并填回 `docs/phase5_results.md` §2.1
3. 在 Binance 后台创建 mainnet API key（只 enable Futures + disable Withdrawals + IP 白名单 = VPS 公网 IP）
4. 把 key 注入 `/etc/xtrade/env`，创建 `/etc/xtrade/mainnet_unlock`
5. 跑 `scripts/phase6/02_preflight_mainnet.sh` → 全 PASS 才启动 supervisor
6. 启动；scanner 监听；等 heavy 触发（mark ≥ $225 首次）+ DCA 日触发（mark ≥ $210 每日）；逐次审批
7. 持仓期按 `docs/phase6_runbook_vps.md` 守夜，每日跑 `holding_report`，IPO 公布期加密
8. 收尾后填回 `docs/phase6_results.md`

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
      "event": "supervisor.mcap.softkill" | "supervisor.drawdown.halt"
             | "supervisor.heartbeat.idle" | "supervisor.start"
             | "scanner.threshold_ladder.heavy_emit" | "scanner.threshold_ladder.dca_emit"
             | "ops.emergency_close.invoked" | "ops.holding_report.daily" | "...",
      "message": "human-readable one-liner, ≤ 200 字符",
      "instrument": "SPCXUSDT-PERP.BINANCE" (可选),
      "fields": { ... 任意 key → 标量；不得含 secret },
      "dispatched_at": "ISO 8601 Z"
    }
  期望 HTTP 状态: 200 ack；4xx 立即停止重试；5xx + net err 走 4 次指数 backoff (1/2/4/8 s)
  openclaw 侧 TaskFlow: parse body → 按 severity 选择 yuanbao push 频道 → 用户收到 push → 无需回执（alert 单向）

scanner / strategy / risk / approval 路径（与 Phase 4–5 一致，单向 confirm/reject）
  保持不变。本期 SPCX entry envelope 的 decision_payload schema 与 momentum_follow 一致，
  `intent_side` 由 scanner 的 direction 推导（above→short / below→long），
  `strategy_kind="mcap_anchored_ladder"` + `scanner_kind="threshold_ladder_entry"` 在 audit 中可见，
  `strategy_instance_id="spcxusdt_short_mcap"` / `scanner_instance_id="spcxusdt_threshold_ladder"` 标识具体实例。

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

drawdown 状态文件
  /var/lib/xtrade/state/drawdown.json    # 0640 xtrade:xtrade
  schema: {"hwm_usd": "...", "last_equity_usd": "...", "last_update_ts": "...Z"}

mcap soft kill 状态文件
  /var/lib/xtrade/state/mcap_softkill.json    # 0640 xtrade:xtrade
  schema: {"consecutive_breaches": int, "last_mcap_usd": "...", "triggered": bool,
           "last_update_ts": "...Z"}

alert audit jsonl
  /var/lib/xtrade/audit/alerts.<YYYY-MM-DD>.jsonl    # 0640 xtrade:xtrade
  schema: 与 bridge_out audit 同构，增 severity / event 字段
```

---

## 11. 持仓期 / 收尾的硬定义

不同于 v1 的"连续 24 自然小时 supervisor active"，本期收尾采用**事件驱动**定义：

- **持仓期起点**：第一次入场 fill 完成（heavy 或 DCA 首单成交时间）。
- **持仓期终点**：以下任一发生即视为持仓期结束：
  - 全部 4 档 TP 成交（理想路径）
  - 操作员手动平仓（保守路径 / 紧急路径）
  - soft kill 触发后操作员决策"市价对冲收尾"
  - 操作员决策"接受失败、止损平仓"
- **持仓期最小要求**（为保证流程被实测覆盖）：
  - supervisor 在持仓期内累计 `systemctl is-active = active` 时长 ≥ 持仓期 95%（允许 5% 重启 / 维护窗口）
  - 期间至少触发 1 次 daily holding report（即持仓期 ≥ 24h 自然小时）
  - 期间至少 1 次手动 `xtrade ops status --json` 巡检
- 如持仓期 < 24h（极端情形：scanner 触发 → fill → 接 soft kill 全部发生在 24h 内），仍可视为 Phase 6 完成，但 §7 决策矩阵按"持仓期短 + 事件序列异常密集"另行复盘。

---

## 参考链接

- `xtrade_plan.md`（主路线图）
- `docs/phase4_brief.md` + `docs/phase4_results.md`（Phase 4 VPS 基础 + drill 模板）
- `docs/phase5_brief.md` + `docs/phase5_results.md`（Phase 5 加固 + ML 离线 + Track C 上线护栏）
- `docs/phase5_track_c_brief.md`（Track C 模型 registry / replay-gate / ml_gate audit 设计）
- `src/xtrade/live/mainnet_unlock.py`（Phase 5 A5 第三锁实现，Phase 6 真实启用点）
- `src/xtrade/bridge/openclaw_webhook.py`（Phase 4 出站 bridge，Phase 6 alerter 复用框架）
- `src/xtrade/research/replay_gate.py`（Phase 5 C1 重放路径，本期 holding_report 复用）
- Binance USDS-M Pre-IPO Perpetual 公告（合约规格 / 5× cap / IPO 后切现货锚的说明）—— 操作员在 runbook 中记录抓取时点
