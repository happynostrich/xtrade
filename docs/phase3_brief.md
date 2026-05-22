# Phase 3 实施简报 —— 策略框架与纸面交易

> 编制日期：2026-05-22
> 目标仓库：`/Users/bitcrab/xtrade`
> 上游依据：
> - 主路线图：`xtrade_plan.md` §七 "Phase 3 — 策略框架与纸面交易"
> - Phase 2 收尾：`docs/phase2_results.md`（结论：S1–S8 全 PASS → 进入 Phase 3）
> 执行方式：本简报交给 **Claude Code** 在 `/Users/bitcrab/xtrade` 中执行。

---

## 0. 进入 Phase 3 的前提（来自 Phase 1 / Phase 2 结论）

Phase 1 已交付：

- **执行内核**：`XtradeStrategy` 基类 + `mode: backtest|live` 路由；`xtrade backtest run` 与 `xtrade live run` 跑同一份策略代码。
- **数据底座**：`ParquetDataCatalog` + `xtrade data ingest` 幂等抓取。
- **可观测性**：`run_with_logging` 统一 `logs/<run-id>/{run.log, summary.json, config.snapshot.yaml}`；退出码 0/1/2 三档契约。

Phase 2 已交付：

- **研究 / 扫描环路**：`xtrade.research.{universe,frames,scanners,gridsearch,signals,runner}`。
- **信号契约**：`Signal`（frozen + slots，含凭据扫描）+ `SignalQueue`（per-day jsonl 分片、原子写、去重幂等）。
- **CLI**：`xtrade scan {universe,run,inspect}`。
- **Parity 验证**：`MomentumScanner` ↔ Nautilus `MomentumDemoSMA` 在同段 bars 上严格逐位相等。
- **测试矩阵**：`pytest tests/` 231 用例全 offline。

主路线图 §七 Phase 3 定位：

> **Phase 3 — 策略框架与纸面交易（2–3 周）**
> 定义策略插件结构与统一接口；实现风控模块与"自动/半自动审批网关"；在测试网/纸面环境跑实盘流程。

Phase 3 不替换 Phase 1 的执行内核、也不替换 Phase 2 的扫描器——它**架在两者之间**，把扫描器写到 `SignalQueue` 的信号消费成策略可执行的订单意图，并强制经过风控与审批网关，最终在纸面 / testnet 环境完成一轮端到端跑通。

---

## 1. Phase 3 的使命与非使命

**使命（Phase 3 要做的）**

1. 定义**策略插件契约**：在 `XtradeStrategy` 之上抽出一个 `SignalDrivenStrategy` 子类，统一从 `SignalQueue` 拉取信号、把信号翻译成 `OrderIntent`、提交订单。
2. 实现**强制单点风控**：所有订单出口走 `RiskGate.check(intent) -> Decision`；策略代码不得绕过。覆盖至少四类规则：单笔上限、单标的上限、组合总上限、最大回撤熔断。
3. 实现**审批网关**：三档 mode —— `auto`（直接下）/ `manual`（写入待审表，等显式确认）/ `dry_run`（只记录，不下单）。Phase 3 的 manual 通道是**本地文件 + CLI 确认**（不接 Telegram，那是 Phase 4）。
4. 实现**纸面交易跑通器**：`xtrade paper run --strategy ... --signals-from data/signals`，把扫描器历史信号回放成 paper fills，写 `logs/<run-id>/paper_summary.json`。
5. 实现**testnet 端到端**：`xtrade live run --strategy <signal_driven> --mode manual` 在 Binance Spot testnet（Phase 0/1 已验证）+ Hyperliquid testnet 上跑一轮"信号→风控→审批→testnet 下单→撤单"。
6. 在策略层引入**最小可重放的回测路径**：给定 `SignalQueue` 历史片段 + catalog bars，用 Phase 1 的 `BacktestEngine` 重放出与 paper 模式一致的 fills。
7. 暴露 CLI：`xtrade strategy list`、`xtrade strategy describe <name>`、`xtrade paper run`、`xtrade approve {list,confirm,reject}`。
8. 把策略运行接入 `logs/<run-id>/`，新增 `strategy_summary.json`（信号消费数、订单意图数、风控拒绝数、待审 / 已批 / 已驳数、fills、PnL）。

**非使命（Phase 3 不做的）**

- **不接主网真实资金**：所有 Phase 3 实盘路径都跑 testnet 或 paper。
- **不做 Telegram / 移动端审批 / WebUI**：审批通道仅 CLI + 本地文件队列（Phase 4 替换）。
- **不做容器化 / 云端部署 / Grafana**：本地可重复运行即可（Phase 4）。
- **不引入新数据源**：基本面 / 链上 / 宏观 / 新闻 / ML 信号仍延后。
- **不重写 Phase 2 的扫描器**：Phase 3 只**消费**信号，不改写。
- **不引入消息队列中间件**：Redis / Kafka / RabbitMQ 都不上；继续用文件 jsonl 队列。
- **不做组合优化器 / 头寸 sizing 算法**：风控只做"硬上限拒绝"，不做"动态 sizing"。Phase 5 再考虑。

> 边界原则：Phase 3 的输出是"研究 → 风控 → 审批 → 执行"全链路的最小闭环，可以在 testnet / paper 跑通；它**不是**真钱交易系统。

---

## 2. 验收标准（Go / No-Go 清单）

每项明确 PASS / FAIL，记入 `docs/phase3_results.md`。

| ID  | 名称 | 描述 |
|-----|------|------|
| T1  | 策略插件契约 | `xtrade.strategy.SignalDrivenStrategy`（`XtradeStrategy` 的纯 Python 子类，不绑死 Nautilus 事件循环）定义 `on_signal(signal) -> Iterable[OrderIntent]`、`on_fill(fill)`、`on_reject(reason)`；`@register_strategy` 装饰器维护 `_STRATEGY_REGISTRY`；至少一个示例插件 `MomentumFollow`（消费 `MomentumScanner` 信号，最简 long-only 翻转）。 |
| T2  | OrderIntent 与风控决策 | `OrderIntent`（frozen dataclass：`venue`、`symbol`、`side: Literal["BUY","SELL"]`、`order_type: Literal["MARKET","LIMIT"]`、`quantity: Decimal`、`limit_price: Decimal | None`、`reduce_only: bool`、`time_in_force`、`source_signal_id`）；`RiskDecision`（`approve`/`reject` + `reasons: list[str]`）；序列化为 jsonl 不丢精度。 |
| T3  | 风控模块（强制单点） | `xtrade.risk.RiskGate.check(intent, account_state) -> RiskDecision`：覆盖 max_notional_per_order、max_position_per_symbol、max_total_notional、max_drawdown_pct（账户峰值回撤熔断）；规则在 `config/risk.example.yaml` 可配；任何 strategy 路径不许直接构造 `Order` 绕过 RiskGate（由 import-graph lint + 测试守护）。 |
| T4  | 审批网关 | `xtrade.approval.ApprovalGate(mode: "auto"|"manual"|"dry_run", queue_root)`：`auto` 直通、`dry_run` 仅记录、`manual` 把意图写入 `data/approvals/<YYYY-MM-DD>.jsonl`（`status: pending`）。`xtrade approve {list,confirm,reject}` CLI 操作待审队列；确认后由 ApprovalGate 在下一轮 tick 重新发射 intent。审批文件原子写、幂等。 |
| T5  | 纸面交易跑通器 | `xtrade paper run --strategy momentum_follow --signals-from data/signals --since ... --until ... --bar 1m`：从 `SignalQueue` 拉取信号 → 策略翻译为 intent → RiskGate.check → ApprovalGate（默认 `auto`）→ 用 Phase 1 `BacktestEngine` 在同段 catalog bars 上撮合 → 输出 `paper_summary.json`。 |
| T6  | testnet 端到端冒烟 | `xtrade live run --strategy momentum_follow --venues binance_spot,hyperliquid --mode manual --signals-from data/signals` 在 testnet 上：拉一条最近信号 → 策略生成 intent → 写 manual 待审 → `xtrade approve confirm <id>` → 下达远离市价的 limit 单 → 拿到 testnet ack → 撤单 → 优雅退出。复用 Phase 1 的 testnet 工厂，不引入新 venue 配置。 |
| T7  | 回放一致性 | `tests/test_signal_replay_parity.py`：固定一段历史 signals + 同段 catalog bars，paper 模式与 Phase 1 `BacktestEngine` 回放（同一 strategy 类，但绕过 ApprovalGate 直接走 RiskGate）产出的 fills 序列**逐笔一致**（symbol、side、qty、ts_event）。 |
| T8  | 可观测性与测试 | 每次 `xtrade paper run` / `xtrade live run` 写 `logs/<run-id>/strategy_summary.json`（字段定义在 §5 Task 8）；`pytest tests/` 全绿，新增至少 25 个 offline 测试覆盖 T1–T5、T7。 |

**Phase 3 通过条件**：T1–T7 全 PASS；T8 为收尾要求。任何一项 FAIL 在结果报告中明确记录原因与解决路径。

---

## 3. 不在本阶段处理的事项（显式延后）

- **Telegram 通道 / 移动端审批 / WebUI**：审批仅 CLI + 文件（Phase 4）。
- **真实资金主网交易**：所有路径默认 testnet / paper；任何主网开关要求显式 `--mainnet --i-understand` 双确认，且 Phase 3 不开。
- **基本面 / 链上 / 宏观 / 新闻 / ML 信号**：Phase 5 增量。
- **动态头寸 sizing / 组合优化**：Phase 3 只做"硬上限拒绝"。
- **消息队列中间件 / 数据库**：继续 Parquet + jsonl，不引入 Redis / Kafka / PostgreSQL。
- **跨进程 / 跨主机调度**：Phase 4 再说；Phase 3 单进程跑。
- **多账户 / 子账户 / 多用户**：单账户。
- **熔断恢复演练**：Phase 4 的故障恢复演练范畴；Phase 3 只确保熔断会**触发**与**拒绝**，不做自愈。
- **新 venue 接入**：仅复用 Phase 0/1 已验证的 Binance Spot testnet + Hyperliquid testnet；Binance Futures testnet 仍按 venue 端可用性等待，不阻塞 Phase 3。

---

## 4. 目标仓库结构（增量于 Phase 2）

```
xtrade/
├── config/
│   ├── venues.example.yaml         (已有)
│   ├── venues.testnet.yaml         (已有)
│   ├── universe.example.yaml       (已有)
│   └── risk.example.yaml           (新增；风控规则)
├── src/xtrade/
│   ├── cli.py                      (已有；新增 `strategy`、`paper`、`approve` 子命令组)
│   ├── strategy/                   (新包，与现有 `strategies/` 区分)
│   │   ├── __init__.py
│   │   ├── base.py                 (SignalDrivenStrategy + register_strategy + _STRATEGY_REGISTRY)
│   │   ├── intent.py               (OrderIntent + Fill)
│   │   ├── consumer.py             (SignalConsumer：薄包装 SignalQueue.tail/since/filter，绝不直接读 jsonl)
│   │   ├── runner.py               (run_paper / run_live 编排：consume → strategy.on_signal → RiskGate → ApprovalGate → fill loop)
│   │   └── plugins/
│   │       ├── __init__.py
│   │       └── momentum_follow.py  (示例插件)
│   ├── risk/                       (新包)
│   │   ├── __init__.py
│   │   ├── rules.py                (RiskRule ABC + 4 个内置规则)
│   │   ├── gate.py                 (RiskGate + RiskDecision)
│   │   └── account.py              (AccountState：testnet/paper 共享的快照模型)
│   ├── approval/                   (新包)
│   │   ├── __init__.py
│   │   ├── gate.py                 (ApprovalGate 三档 mode)
│   │   └── queue.py                (ApprovalQueue：data/approvals/<date>.jsonl)
│   ├── research/                   (Phase 2 已有，不动)
│   ├── strategies/                 (Phase 1 已有的 Nautilus 风格策略；保留)
│   └── (其余 Phase 1/2 包不动)
├── scripts/phase3/                 (新增；薄包装)
│   ├── 01_describe_plugin.py
│   ├── 02_paper_run_momentum.py
│   ├── 03_approval_demo.py
│   └── 04_risk_lint.py             (跑 import-graph 检查，断 strategy/* 是否绕开 RiskGate)
├── data/
│   ├── catalog/                    (已有)
│   ├── signals/                    (Phase 2 已有)
│   └── approvals/                  (新增；jsonl 审批队列根，gitignored)
├── logs/                           (已有；新增 strategy_summary.json / paper_summary.json)
├── tests/
│   ├── test_order_intent.py        (新增)
│   ├── test_risk_rules.py          (新增)
│   ├── test_risk_gate.py           (新增)
│   ├── test_approval_gate.py       (新增)
│   ├── test_signal_consumer.py     (新增)
│   ├── test_strategy_base.py       (新增)
│   ├── test_momentum_follow.py     (新增)
│   ├── test_paper_runner.py        (新增)
│   ├── test_signal_replay_parity.py (新增；T7)
│   ├── test_cli_paper.py           (新增)
│   ├── test_cli_approve.py         (新增)
│   └── test_cli_strategy.py        (新增)
└── docs/
    ├── phase2_brief.md             (已有)
    ├── phase2_results.md           (已有)
    ├── phase3_brief.md             (本文件)
    └── phase3_results.md           (新增；执行中追加)
```

新增依赖：**无**。`Decimal` 走 stdlib；风控/审批/策略 runner 都是纯 Python，不引入 SQL / Redis / Web 框架。Nautilus `BacktestEngine` 与 Phase 0/1 已配置的 testnet client 都已经在依赖图里。

---

## 5. 任务分解

### Task 1 —— `OrderIntent` + 策略契约骨架（T1, T2）

- 新增 `xtrade.strategy` 包；`OrderIntent` / `Fill`（`@dataclass(frozen=True, slots=True)`），`Decimal` 字段，`__post_init__` 校验 side/order_type/quantity > 0/limit_price 与 order_type 匹配。
- `SignalDrivenStrategy` ABC：`on_signal(signal: Signal, account: AccountState) -> Iterable[OrderIntent]`，默认 `on_fill` / `on_reject` no-op。
- `@register_strategy(name)` 装饰器 + `_STRATEGY_REGISTRY`；`available_strategies()` 列表 API。
- 序列化：`OrderIntent.to_dict()` / `from_dict()` 往返保 `Decimal` 精度（`str(Decimal)` ↔ `Decimal(str)`）。
- 验收：`tests/test_order_intent.py`、`tests/test_strategy_base.py` 覆盖构造校验、注册重复名拒绝、序列化往返。

### Task 2 —— 风控模块（T3）

- `xtrade.risk.rules.RiskRule` ABC：`check(intent, account) -> RuleResult`（pass/fail + reason）。
- 四个内置规则：
  - `MaxNotionalPerOrder(usd_cap)`：`intent.quantity * mark_price > cap` → reject。
  - `MaxPositionPerSymbol(usd_cap)`：现持仓 +/- intent 后绝对值 > cap → reject。
  - `MaxTotalNotional(usd_cap)`：组合总名义 > cap → reject。
  - `MaxDrawdownPct(pct)`：`(peak_nav - current_nav) / peak_nav > pct` → reject 所有新开仓 intent（不挡平仓）。
- `RiskGate(rules, account_provider)`：按规则顺序短路；输出 `RiskDecision(approve|reject, reasons)`。
- `config/risk.example.yaml`：
  ```yaml
  max_notional_per_order_usd: 1000
  max_position_per_symbol_usd: 5000
  max_total_notional_usd: 20000
  max_drawdown_pct: 0.10
  ```
- 单元测试覆盖每个规则的边界（恰好等于上限、刚好越界、空持仓、空账户）。
- **守护**：在 `tests/test_risk_gate.py` 加 import-graph 校验：`import ast` 扫 `src/xtrade/strategy/**/*.py`，断言里面没有 `from nautilus_trader.execution` / `submit_order` 字符串（绕过 RiskGate 的硬证据）；`scripts/phase3/04_risk_lint.py` 是同一 lint 的命令行版本。

### Task 3 —— 审批网关（T4）

- `ApprovalQueue`：`data/approvals/<YYYY-MM-DD>.jsonl`，行格式 `{id, intent, status: "pending"|"confirmed"|"rejected", created_at, decided_at, reason}`。
  - 原子写：`tempfile.mkstemp` + `fsync` + `os.replace`。
  - 唯一 id：`hashlib.sha256(json.dumps(intent, sort_keys=True))[:16]`，保证幂等。
- `ApprovalGate(mode, queue_root)`：
  - `auto`：返回 `Decision(go=True)` 立即放行。
  - `dry_run`：写入 queue 状态 `confirmed`（仅记录），返回 `Decision(go=False)`，runner 不下单。
  - `manual`：写入 queue 状态 `pending`，返回 `Decision(go=False, awaiting=id)`；runner 把 id 记入 `strategy_summary.json.pending_approvals`。
- CLI `xtrade approve list [--status pending] [--since ...]` / `xtrade approve confirm <id>` / `xtrade approve reject <id> [--reason ...]`：原子 patch 行（重写 jsonl）。
- 单元测试：三档 mode、跨日分片、重复 id 幂等、损坏行恢复。

### Task 4 —— `SignalConsumer` + `SignalDrivenStrategy` 编排（T1, T5）

- `xtrade.strategy.consumer.SignalConsumer(queue: SignalQueue, ...)`：薄包装 `tail` / `since` / `filter`，**不直接读 jsonl**（这是 Phase 2 → Phase 3 唯一稳定的契约边界，见 `phase2_results.md` §3.3）。
- 实例化策略时注入 `SignalConsumer`；strategy 通过 `consumer.iter_new()` 取增量 signals（内部记 cursor）。
- 示例插件 `MomentumFollow`：当收到 `direction=LONG` → 平所有空仓 + 开 100% 名义多；`SHORT` 反向；`FLAT` 平仓。`source_signal_id = signal.id`。
- 验收：`tests/test_signal_consumer.py`、`tests/test_momentum_follow.py`。

### Task 5 —— 纸面跑通器（T5）

- `xtrade.strategy.runner.run_paper(strategy_name, signals_root, catalog_path, since, until, bar, risk_cfg, approval_mode="auto", run_id=None) -> PaperRunResult`：
  1. `load_strategy(name)` → 实例化。
  2. `SignalConsumer` 拉指定窗口的 signals（按 generated_at 排序）。
  3. `BacktestEngine` 用 catalog bars 起一个模拟 venue。
  4. 在 bar event loop 中按 ts 推进信号到 strategy，收 intents，过 RiskGate → ApprovalGate → 提交到模拟 venue。
  5. 收集 fills、最终账户、PnL，写 `logs/<run-id>/paper_summary.json`。
- CLI `xtrade paper run ...`：包在 `run_with_logging(mode="paper")` 中，沿用 Phase 1 退出码契约。
- **重要**：`run_paper` 不能与 `test_backtest_smoke.py` 同进程同时跑（Nautilus `BacktestEngine` 单进程二次实例化 abort，phase2 已识别）；测试侧用 subprocess 隔离（沿用 `tests/_parity_nautilus_runner.py` 的范式）。

### Task 6 —— testnet 端到端（T6）

- `xtrade live run` 扩展：`--strategy momentum_follow --mode manual --signals-from ...`。
  - 复用 Phase 1 的 testnet `TradingNode` 工厂。
  - 启动后从 SignalQueue 拉**最近一条**未消费信号（或 `--signal-id`）→ 走完 RiskGate / ApprovalGate（manual 模式停在等待）→ 操作员 `xtrade approve confirm <id>` → runner 提交远离市价的 limit 单 → 拿 testnet ack → 撤单 → 优雅退出。
  - 单次跑完即退出（不留长进程，避免与 Phase 0 `TradingNode` 长连接复杂度纠缠）。
- 验收：手工 runbook（`docs/phase3_runbook_testnet.md`）记录命令序列；不写自动化测试（依赖外部 venue，违反 offline 原则）。

### Task 7 —— 回放一致性（T7）

- `tests/test_signal_replay_parity.py`：
  - seed 一段 sine-wave catalog + 一段确定性 `MomentumScanner` 输出（直接 mock 或 fixture）。
  - 左路：`run_paper(approval_mode="auto")` → fills 序列 A。
  - 右路：同一 strategy 类，但绕开 `runner.run_paper`，直接构造 `BacktestEngine` 并把 signal 注入 strategy（仍走 RiskGate，但不走 ApprovalGate）→ fills 序列 B。
  - 断言 `A == B`（按 ts_event/symbol/side/qty 字段比较，Decimal 严格相等）。
- 为绕开 `BacktestEngine` 单进程双实例 abort，这里也走 subprocess hop：`tests/_paper_replay_runner.py` 接收 JSON args、emit JSON fills；测试主体只解析对比。

### Task 8 —— 测试与可观测性（T8）

- `strategy_summary.json` / `paper_summary.json` schema：
  ```json
  {
    "run_id": "...",
    "started_at": "ISO8601 (UTC)",
    "completed_at": "ISO8601 (UTC)",
    "mode": "paper|live",
    "strategy": "momentum_follow",
    "signals_consumed": 27,
    "intents_generated": 27,
    "risk_rejected": 3,
    "approvals_pending": 0,
    "approvals_confirmed": 24,
    "approvals_rejected": 0,
    "fills": 24,
    "final_nav_usd": "10180.55",
    "max_drawdown_pct": 0.034,
    "errors": []
  }
  ```
  Atomic 写盘（沿用 Phase 2 的 `tmp` + `os.replace` 模板）。
- `pytest tests/` 在断网环境通过；预计 +25 用例（实际可能更多）。
- `docs/phase3_results.md`：逐项 T1–T8 PASS/FAIL、关键数据样本（一次完整 paper run 输出、一次 testnet 端到端 runbook 截图/日志）、与 Phase 4 衔接建议。

---

## 6. 安全边界（沿用 Phase 0/1/2，新增几条策略 / 风控 / 审批层规则）

- **RiskGate 是强制单点**：`xtrade.strategy.*` 不得直接构造 `Order` / 调用 `submit_order`；任何下单都必须经由 runner 中的 `RiskGate.check(...) -> ApprovalGate.decide(...) -> venue.submit(...)` 三步链。由 `tests/test_risk_gate.py` 的 import-graph lint 守护。
- **审批文件不入 git**：`data/approvals/` 与 `data/signals/` 一样 gitignored。
- **私钥 / API key 隔离**：`OrderIntent.metadata` 与 `ApprovalQueue` 行 JSON 同样接 `_scan_metadata_for_secrets`（沿用 Phase 2 的凭据扫描），命中即拒绝写入。
- **manual 模式默认不放行**：未显式 `xtrade approve confirm <id>` 之前，runner 不下任何单；testnet 路径默认 `--mode manual`，paper 路径默认 `--mode auto`。
- **mainnet 双锁**：`xtrade live run` 任何 `--mainnet` 开关在 Phase 3 直接 `raise NotImplementedError("Phase 3 testnet only")`，避免误手。Phase 5 才解锁。
- **测试隔离**：Nautilus `BacktestEngine` 单进程二次实例化 abort 的历史问题在 Phase 2 已用 subprocess hop 解决；Phase 3 任何新增需要起 `BacktestEngine` 的测试也走同一范式（`tests/_paper_replay_runner.py`）。
- **Decimal 全链路**：`OrderIntent.quantity` / `limit_price`、`AccountState.cash_usd` / `position_qty` 一律 `Decimal`；禁止 `float` 与 `Decimal` 混算（runner 入口转换一次，内部纯 Decimal）。

---

## 7. 决策矩阵（Phase 3 收尾时）

| 结果 | 判断 | 下一步 |
|---|---|---|
| T1–T7 全 PASS | **进入 Phase 4** | Phase 4：容器化 + 云端部署 + Grafana / Loki / Telegram 接入 + 故障恢复演练。 |
| T6 fail（testnet 端到端） | **有条件 GO** | 通常是 testnet venue 侧抖动；若 paper（T5）+ replay 一致性（T7）都 PASS，可记录 venue 端不可用进 Phase 4，让监控层先就位。 |
| T3 fail（风控） | **暂停** | 风控是真钱前的硬底线，任何缺口都必须修。 |
| T4 fail（审批网关） | **暂停** | 没有审批就没有"半自动"故事，Phase 3 失去意义。 |
| T7 fail（回放一致性） | **暂停** | 说明 paper 与 backtest 之间存在不可解释偏差，下游 Phase 4/5 一切 PnL 报表都不可信。 |
| Binance Futures testnet 在此期间恢复 | **不影响 Phase 3** | T6 仍按 Binance Spot + Hyperliquid testnet 验证；futures 留给 Phase 5。 |

---

## 8. 交付物

1. `src/xtrade/strategy/` 包：`base.py`、`intent.py`、`consumer.py`、`runner.py`、`plugins/momentum_follow.py`。
2. `src/xtrade/risk/` 包：`rules.py`、`gate.py`、`account.py`。
3. `src/xtrade/approval/` 包：`queue.py`、`gate.py`。
4. `config/risk.example.yaml`。
5. `xtrade.cli` 新增 `strategy`、`paper`、`approve` 三个子命令组。
6. `scripts/phase3/` 下 4 个薄包装脚本。
7. `data/approvals/` 目录（gitignored）+ 至少一次完整 paper run 在 `data/approvals/<date>.jsonl` 写入（auto 模式也记录，便于审计）。
8. `tests/` 下 12 个新测试文件（intent / risk_rules / risk_gate / approval_gate / signal_consumer / strategy_base / momentum_follow / paper_runner / signal_replay_parity / cli_paper / cli_approve / cli_strategy），`pytest tests/` 全绿。
9. `docs/phase3_runbook_testnet.md`：testnet 端到端 runbook（T6）。
10. `docs/phase3_results.md`：T1–T8 PASS/FAIL、关键证据、与 Phase 4 衔接建议。

---

## 9. 建议执行顺序

1. Task 1（`OrderIntent` + 策略契约骨架）—— 后续所有 task 都依赖它的类型。
2. Task 2（风控模块）—— 早做早绑死强制单点；CLI 还没接也无所谓，单测能驱动。
3. Task 3（审批网关）—— 与 Task 2 并行无依赖，但建议串行写以便测试隔离。
4. Task 4（`SignalConsumer` + 示例插件）—— 串联 Phase 2 信号与 Phase 3 策略。
5. Task 5（paper runner）—— 把前 4 个 task 串成端到端 paper 路径；这是 Phase 3 的"主菜"。
6. Task 7（回放一致性）—— 放在 paper runner 稳定后立即做，防止 Phase 3 末期才发现 paper 与 backtest 不一致需要回炉。
7. Task 6（testnet 端到端）—— 最后做；依赖 venue 可达，且只跑一次手工 runbook。
8. Task 8 的可观测性与测试贯穿前述任务，每个 task 落地立即补对应 test。
9. 全部完成后写 `docs/phase3_results.md`，给出进入 Phase 4 的建议。

---

## 参考链接

- 主路线图 Phase 3 段：`xtrade_plan.md` §七
- Phase 2 结果与契约边界：`docs/phase2_results.md`（§3.3 衔接建议）
- NautilusTrader 风控与订单文档：https://nautilustrader.io/docs/latest/concepts/orders/
- NautilusTrader 回测引擎（继续作为 paper runner 内核）：https://nautilustrader.io/docs/latest/concepts/backtesting/
- Python `decimal` 模块（Decimal 全链路精度）：https://docs.python.org/3/library/decimal.html
