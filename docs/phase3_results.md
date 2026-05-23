# Phase 3 执行结果报告

> 编制日期：2026-05-22
> 上游依据：`docs/phase3_brief.md`
> 目标仓库：`/Users/bitcrab/xtrade`
> 执行人：Claude Code（Opus 4）

---

## 0. 总览

Phase 3 的使命是在 Phase 1 执行内核 + Phase 2 扫描器之间，搭出一层纯 Python 的策略框架：把 `SignalQueue` 中的信号翻译成 `OrderIntent`，强制经过 `RiskGate`（硬上限拒绝）与 `ApprovalGate`（auto / dry_run / manual 三档），落到 paper 模式（Phase 1 `BacktestEngine` 撮合）或 testnet 模式（Phase 1 `run_live` testnet 工厂下达远离市价 limit + 撤单），并写出 `paper_summary.json` / `live_signal_summary.json` 审计链。

**结论：T1–T7 全 PASS（含 replay-parity T7 fill-by-fill 严格相等）；T8 收尾 PASS，并在编写 schema test 时顺手抓到并修复一个 `_PaperBridge` 中潜伏的 BUY/SELL 反向 bug。Phase 3.5（runbook 实测 + 8 个硬化 commit）于 2026-05-23 完成 testnet 端到端手工 hop（Binance Futures），证据见 §1.9。**

| ID  | 名称 | 状态 | 关键证据 |
|-----|------|------|---------|
| T1  | 策略插件契约 | PASS | `xtrade.strategy.{base,intent,plugins}`：`SignalDrivenStrategy` ABC + `OrderIntent` + `@register_strategy` + 示例插件 `MomentumFollow`；`tests/test_strategy_base.py`（16）+ `tests/test_order_intent.py`（25）+ `tests/test_momentum_follow.py`（18）（commits `05e95a2`, `7dbd432`） |
| T2  | OrderIntent / RiskDecision 序列化 | PASS | `OrderIntent.to_dict()` / `from_dict()` Decimal 字段 `str` 往返；`fingerprint()` SHA-256 [:16]；`RiskDecision`（frozen，`approve`/`reject` + reasons）（commit `05e95a2`） |
| T3  | 风控强制单点 | PASS | `xtrade.risk.{rules,gate,account}`：四个内置规则（`MaxNotionalPerOrder`、`MaxPositionPerSymbol`、`MaxTotalNotional`、`MaxDrawdownPct`）+ `RiskGate.check` 短路；`config/risk.example.yaml`；**AST import-graph lint** 守护 `xtrade.strategy.*`（除 `runner.py`）禁用 `submit_order` / `nautilus_trader.execution`；`tests/test_risk_rules.py`（26）+ `tests/test_risk_gate.py`（13）（commit `1c7f471`） |
| T4  | 审批网关 | PASS | `xtrade.approval.{gate,queue}`：`ApprovalGate` 三档 mode、`ApprovalQueue` per-day jsonl（atomic mkstemp + fsync + os.replace）、SHA-256 record_id 幂等、jsonl 行原地 patch（重写）；`xtrade approve {list,confirm,reject}` CLI；`tests/test_approval_gate.py`（22）+ `tests/test_cli_approve.py`（6）（commit `f9b99c3`） |
| T5  | 纸面跑通器 | PASS | `xtrade.strategy.runner.run_paper`：SignalConsumer cursor → strategy.on_signal → RiskGate → ApprovalGate → `BacktestEngine` 撮合 → `paper_summary.json` atomic write；`xtrade paper run` / `xtrade strategy {list,describe}` CLI；`tests/test_signal_consumer.py`（13）+ `tests/test_paper_runner.py`（3 subprocess）+ `tests/test_cli_paper.py`（3）+ `tests/test_cli_strategy.py`（3）（commit `7a2ba95`） |
| T6  | testnet 端到端 | PASS（**手工 runbook 已实测 2026-05-23**） | `xtrade.live.signal_runner.run_live_signal` 复用 Phase 1 `run_live` testnet 工厂；`xtrade live signal-run` CLI；**6 类错误分级 → 退出码 0/1/2**；`docs/phase3_runbook_testnet.md` 操作员步骤 + 验证 checklist + 失败模式表；`tests/test_live_signal_runner.py`（14）+ `tests/test_cli_live_signal.py`（3，全 offline，注入 `live_executor` 桩）（commit `5edad2c`）。**实测 hop（Binance Futures testnet）**：run_id `live-20260523T030713Z`，approval `1de841fa7ed7a038 manual confirmed`，venue order `O-20260523-031036-001-000-1 / 13179853156` accepted→canceled @52896.40，summary `logs/live-20260523T030713Z/live_signal_summary.json`。实测过程触发的 8 个硬化 commit 见 §1.9 / §5。 |
| T7  | 回放一致性 | PASS（fill-by-fill 严格相等） | `tests/_paper_replay_runner.py` 两 variant：`paper`（`ApprovalGate(auto)` 全跑）vs `direct`（`xtrade.approval.ApprovalGate` 被 monkey-patch 为 pass-through stub）；同 catalog 同 signals 同 strategy，fills 序列按 `(ts_event, symbol, side, qty, price)` Decimal-strict 逐位相等；`tests/test_signal_replay_parity.py`（2）+ `_PaperBridge` 暴露 `fill_events` 列表入 summary（commit `d058df6`） |
| T8  | schema + 测试 | PASS | `paper_summary.json` / `live_signal_summary.json` schema 测试（`tests/test_strategy_summary_schema.py`，3 用例）pin 全部强制字段；**捕获并修复 latent BUY/SELL 反向 bug**：`_PaperBridge.on_order_filled` 原用 `str(event.order_side)` 拿到 Nautilus 内部 int 表示 `"1"` 而非 `"BUY"`，导致 `sign = -1` 始终成立，悄悄反向记账；修复为 `event.order_side.name`；`pytest tests/` → **400 passed / 0 failed**，全 offline；Phase 3 增量 **169 用例**（commit `c428522`） |

Phase 3 通过条件（T1–T7 PASS、T8 收尾要求）全部满足 → 建议进入 Phase 4。

---

## 1. 各任务交付与证据

### Task 1 —— `OrderIntent` + 策略契约骨架（T1, T2）

- 新增 `src/xtrade/strategy/` 包：`base.py`、`intent.py`、`plugins/__init__.py`。
- `OrderIntent`（`@dataclass(frozen=True, slots=True)`）：
  - 字段：`venue`、`symbol`、`side: Literal["BUY","SELL"]`、`order_type: Literal["MARKET","LIMIT"]`、`quantity: Decimal`、`limit_price: Decimal | None`、`reduce_only: bool`、`time_in_force: Literal["GTC","IOC","FOK"]`、`source_signal_id: str`、`created_at: tz-aware datetime`、`metadata: dict`。
  - `__post_init__` 校验：side / order_type 白名单、`quantity > 0`、`MARKET → limit_price is None`、`LIMIT → limit_price > 0`、`created_at` tz-aware、metadata 凭据扫描（沿用 Phase 2 `_scan_metadata_for_secrets`）。
  - `to_dict()` / `from_dict()`：`Decimal` 字段全部 `str(Decimal)` ↔ `Decimal(str)` 往返；`created_at` 走 ISO8601。
  - `fingerprint()`：对 sort_keys-stable JSON 做 SHA-256 取前 16 hex —— ApprovalQueue 的 record_id 幂等键。
- `SignalDrivenStrategy` ABC：`on_signal(signal: Signal, account: AccountState) -> Iterable[OrderIntent]`；默认 `on_fill` / `on_reject` no-op；`@register_strategy(name)` 装饰器维护 `_STRATEGY_REGISTRY`；`available_strategies()` / `load_strategy(name)` 接口。
- 验证：`tests/test_order_intent.py`（25）+ `tests/test_strategy_base.py`（16）：构造校验、序列化往返（含小数边界 `0.000001`）、注册重复名拒绝、未注册名加载报错。

### Task 2 —— 风控模块（T3）

- 新增 `src/xtrade/risk/` 包：`rules.py`、`gate.py`、`account.py`。
- `AccountState`（frozen dataclass）：`cash_usd: Decimal`、`positions: dict[str, Decimal]`、`marks: dict[str, Decimal]`、`peak_nav_usd: Decimal`；`nav()` / `total_notional()` helper。
- `RiskRule` ABC：`check(intent, account) -> RuleResult(passed, reason)`；四个内置：
  - `MaxNotionalPerOrder(usd_cap)`：`intent.quantity * mark > cap`（mark 缺失走 `limit_price` 兜底；都没有 → reject `"no mark"`）。
  - `MaxPositionPerSymbol(usd_cap)`：模拟成交后 `|position * mark| > cap`。
  - `MaxTotalNotional(usd_cap)`：模拟成交后组合总名义 > cap。
  - `MaxDrawdownPct(pct)`：`(peak_nav - current_nav) / peak_nav > pct` 时拒所有开仓 intent（`reduce_only=True` 放行）。
- `RiskGate(rules)`：按 list 顺序短路；`check(intent, account) -> RiskDecision(approve, reasons)`；可 `from_yaml(path)` 装载 `config/risk.example.yaml`。
- **import-graph 守护**：`tests/test_risk_gate.py::test_no_direct_order_construction_in_strategy_layer` 用 `ast` 扫 `src/xtrade/strategy/**/*.py`（除白名单 `runner.py` —— 它是唯一允许触达 venue 的层）；命中 `submit_order` / `nautilus_trader.execution` / `Order.__init__` 即 fail。`scripts/phase3/04_risk_lint.py` 是同一 lint 的 CLI 版本。
- 验证：`tests/test_risk_rules.py`（26）+ `tests/test_risk_gate.py`（13）：每条规则的"恰好等于"/"刚好越界"边界、空持仓、空账户、`reduce_only` 透过 drawdown 闸。

### Task 3 —— 审批网关（T4）

- 新增 `src/xtrade/approval/` 包：`queue.py`、`gate.py`。
- `ApprovalRecord` jsonl 行格式：`{record_id, intent: dict, status: pending|confirmed|rejected|dry_run, mode, created_at, decided_at, reason, decided_by}`。
- `ApprovalQueue`：
  - `data/approvals/<YYYY-MM-DD>.jsonl` 按 UTC 日期分片；目录不存在自动创建。
  - `append(record)`：先全队列扫 `record_id` 去重（跨日），命中已存在直接返回原行（幂等）；不命中走 `tempfile.mkstemp` + `os.fdopen` + `fsync` + `os.replace`。
  - `update_status(record_id, new_status, reason=None, decided_by=None)`：定位行 → 重写整个 jsonl（同 atomic 模板）。
  - 读侧：`tail(n)`、`since(when)`、`filter(status=)`、`get(record_id)`、`__iter__`。损坏 jsonl 行：`warnings.warn(RuntimeWarning)` 跳过、不崩。
- `ApprovalGate(mode, queue_root)`：
  - `auto`：写 `status=confirmed`，返回 `ApprovalDecision(go=True, awaiting=False, record_id, status="confirmed", mode="auto")`。
  - `dry_run`：写 `status=dry_run`，返回 `go=False`，runner 不下单。
  - `manual`：写 `status=pending`，返回 `go=False, awaiting=True`，runner 应轮询 queue 直至状态翻转。
- CLI `xtrade approve {list,confirm,reject}`：包在 Typer subapp 中，原子改 jsonl，命中不存在的 record_id → 退出码 2。
- 验证：`tests/test_approval_gate.py`（22）+ `tests/test_cli_approve.py`（6）：三档 mode、跨日分片、重复 id 幂等、损坏行恢复、status filter、reject reason 透传。

### Task 4 —— `SignalConsumer` + `MomentumFollow` 插件（T1, T5）

- `xtrade.strategy.consumer.SignalConsumer(queue: SignalQueue, *, since: datetime | None = None, symbol: str | None = None, source: str | None = None)`：薄包装 `SignalQueue.tail` / `since` / `filter`；维持 cursor（最近见过的 `generated_at, symbol, source` 三元组），`iter_new()` 只产出 cursor 之后的新信号；**绝不直接读 jsonl** —— Phase 2/3 间唯一稳定契约边界。
- `MomentumFollow` 插件（`@register_strategy("momentum_follow")`）：
  - 配置：`notional_usd: Decimal`（默认 `1000`）、`qty_step: Decimal`（默认 `0.001`）。
  - `on_signal(signal, account)`：从 `signal.metadata["last_price"]` 拿 mark，缺失走 `account.marks[symbol]`，仍缺失 → 返回 `[]`（不抛，让 runner 继续推进）。
    - `LONG` → 期望多头 `notional_usd / mark`，与当前持仓 diff，按 `qty_step` 截断，产出一条 BUY/SELL 把净仓拉到目标。
    - `SHORT` → 镜像。
    - `FLAT` → 平当前持仓。
- 验证：`tests/test_signal_consumer.py`（13）+ `tests/test_momentum_follow.py`（18）：cursor 推进、tail-only 模式、filter 链、mark 缺失静默 skip、`qty_step` 截断、flat 平仓。

### Task 5 —— 纸面跑通器（T5）

- `xtrade.strategy.runner.run_paper(strategy_name, catalog_path, instrument_id, bar, signals_root, *, approval_mode="auto", risk_rules=[], strategy_config={}, starting_balance, approvals_root, run_id=None, logs_root) -> PaperRunResult`：
  1. `load_strategy(name)` → 实例化。
  2. `SignalQueue` + `SignalConsumer` 拉信号（按 `generated_at` 升序）。
  3. `BacktestEngine` 装 catalog bars，注册一个 `_PaperBridge`（继承 `XtradeStrategy`），在 `on_bar(bar)` 中：
     - 用 bar.close 更新 `AccountState.marks`。
     - 把 ts ≤ bar.ts_event 的待消费 signals 喂进 `strategy.on_signal(...)`。
     - 收 intents → `RiskGate.check` → `ApprovalGate.decide` → `submit_order(...)`（仅当 `go=True`）。
     - `on_order_filled(event)` 记录到 `fill_events: list[dict]`（T7 扩展），并维护 `positions` / `cash_usd`。
  4. 引擎 dispose 前抓 `bridge.fill_events` / 计数；写 `logs/<run-id>/paper_summary.json`（atomic）。
- `PaperRunResult`（frozen dataclass）：`run_id`、`summary`、`summary_path`、`passed`。
- CLI 子命令组（与 `data` / `backtest` / `live` / `scan` 同级）：
  - `xtrade strategy {list,describe}`
  - `xtrade paper run --strategy ... --catalog ... --instrument ... --bar ... --signals-from ... [--approval-mode auto|dry_run|manual] [--risk-config risk.yaml] [--strategy-config k=v,k=v]`
  - `xtrade approve {list,confirm,reject}`（Task 3 已落）
- **测试隔离**：`run_paper` 内部 `BacktestEngine` 与 `tests/test_backtest_smoke.py` 不可同进程并存 → `tests/test_paper_runner.py` 三条用例全走 `tests/_paper_runner_subprocess.py`（参见 Phase 2 §6 处理 Nautilus 单进程双引擎 abort 的范式）。
- 验证：`tests/test_paper_runner.py`（3 subprocess）+ `tests/test_cli_paper.py`（3 CliRunner）+ `tests/test_cli_strategy.py`（3 CliRunner）：summary schema、fills > 0、auto/dry_run 分支、bogus mode → 退出码 2。

### Task 6 —— testnet 端到端冒烟（T6）

- 新增 `src/xtrade/live/signal_runner.py`：`run_live_signal(venues_cfg, strategy_name, signals_root, instrument_id, *, approval_mode="manual", approval_timeout=600, poll_interval=2.0, signal_id=None, risk_rules=[], strategy_config={}, approvals_root, logs_root, run_id=None, venue_timeout=60, safety_multiplier=0.7, live_executor=None) -> LiveSignalResult`：
  1. `SignalQueue` 拉 newest-matching（或 `signal_id` 命中）信号。
  2. `strategy.on_signal(...)` → 若无 intent → `StrategyEmittedNothingError`。
  3. `RiskGate.check` → 若 reject → `RiskRejectedError`。
  4. `ApprovalGate.decide`：
     - `auto` → 直通。
     - `dry_run` → 写 `confirmed` 记录但 `passed=False, note="dry_run..."`，summary 落盘，返回。
     - `manual` → 写 `pending`，按 `poll_interval` 轮询 queue 直到 `confirmed` / `rejected` / 超时（`time.monotonic()` 维持 deadline）。`rejected` → `ApprovalRejectedError`；超时 → `ApprovalTimeoutError`。
  5. 调 `live_executor(venues_cfg, ...)`（默认 `xtrade.live.runner.run_live`）执行 testnet hop：远离市价 GTC limit + 等 `OrderAccepted` + cancel + 等 `OrderCanceled` + dispose node。
  6. `logs/<run-id>/live_signal_summary.json` atomic write（schema 见 §3.2）。
- **mainnet 拒绝**：`run_live` 自身在 venues.yaml `testnet: false` 时即拒；`run_live_signal` 不暴露任何主网开关。
- CLI 扩展：`xtrade live signal-run` —— 选项 `--strategy`、`--instrument`、`--signals-from`、`--mode {auto,dry_run,manual}`、`--signal-id`、`--venues-yaml`、`--safety-multiplier`、`--approval-timeout`、`--poll-interval`、`--venue-timeout`、`--risk-config`、`--approvals-root`、`--run-id`。
  - 错误 → 退出码：`NoMatchingSignal` / `StrategyEmittedNothing` → 2；`RiskRejected` / `ApprovalRejected` / `ApprovalTimeout` → 1；dry_run with `passed=False` → 0（信息性）。
- **测试**：自动测试覆盖到 venue hop 之前（注入 `live_executor` 桩），不打真实网络。`tests/test_live_signal_runner.py`（13）+ `tests/test_cli_live_signal.py`（3）。
- **手工 runbook**：`docs/phase3_runbook_testnet.md`，操作员视角 6 章节：
  0. 前置（venues.testnet.yaml + env vars + 小余额）
  1. 种一条 signal（real scanner 或手搓 jsonl）
  2. 先 `--mode dry_run` 验 intent
  3. `--mode manual` 双终端：A 跑 signal-run（轮询阻塞），B 跑 `xtrade approve list` + `confirm <id>`
  4. 验证 checklist（6 项 PASS）
  5. 失败模式表（7 种）+ 修复路径
  6. 归档制品（4 类文件，重建完整审计链）

### Task 7 —— 回放一致性（T7）

- `_PaperBridge.on_order_filled(event)` 扩展（commit `d058df6`）：
  ```python
  self.fill_events.append({
      "ts_event": int(event.ts_event),
      "symbol": sym,
      "side": side_str,
      "qty": str(qty),
      "price": str(price),
  })
  ```
  `paper_summary.json` 新增 `fill_events: list[dict]` 字段（schema 见 §3.1）。
- `tests/_paper_replay_runner.py`：subprocess 入口，两 variant
  - `paper`：原样 `run_paper(approval_mode="auto", ...)` —— `ApprovalGate` 全跑（每条 intent 写 `confirmed` 行到 `data/approvals/`）。
  - `direct`：在 import `run_paper` **之前** monkey-patch `xtrade.approval.ApprovalGate` 为 `_PassthroughApprovalGate`：`decide(intent, ...) -> ApprovalDecision(go=True, awaiting=False, record_id=intent.fingerprint()[:16], status="confirmed", mode="auto")`，绝不触盘。
  - 同 sine-wave 200-bar catalog + 同 3 条 signals（LONG@+20m / SHORT@+60m / FLAT@+120m），同 strategy `momentum_follow`。
- `tests/test_signal_replay_parity.py` 两条用例：
  - `test_paper_and_direct_paths_produce_identical_fills`：按 index 逐位 assert `(ts_event, symbol, side, qty, price)`，`qty`/`price` 是 `str(Decimal)` —— string 等价即 Decimal-strict；额外验 `bars_loaded` / `signals_consumed` / `intents_generated` / `risk_rejected` / `fills` / `final_cash_usd` / `final_position_qty` / `final_nav_usd` / `peak_nav_usd` 全等。
  - `test_paper_summary_includes_fill_events`：schema 抽查 `fill_events` 列表 + 5 个 key。
- 用时：每条用例两次 subprocess hop（~17s）；总 testnet 路径无依赖。

### Task 8 —— Schema + 测试收尾（T8）

- `tests/test_strategy_summary_schema.py`（3 用例）：
  - 在测试顶部定义 5 个 schema spec（Python dict `{field: expected_type_or_tuple}`），`_assert_schema(payload, spec, ctx)` 辅助函数检查 key 在位 + 类型匹配。
  - `test_paper_summary_schema`：subprocess 跑一遍 paper → 27 个强制字段 + 5 个 fill-event 字段全部 pinned；额外验 `fills == len(fill_events)`、`approvals_confirmed + approvals_pending + approvals_dry_run + approvals_rejected + risk_rejected == intents_generated`、Decimal-strings 全部 round-trip、on-disk == in-memory、`errors == []`、`fe["side"] in {"BUY","SELL"}`。
  - `test_live_signal_summary_schema_auto`：注入 `_stub_executor`，`approval_mode="auto"` → 14 个顶层字段 + nested `signal` / `intent` / `approval` schema、`passed=True`、`live_summary is not None`、intent 包含 10 个 canonical 字段。
  - `test_live_signal_summary_schema_dry_run`：`approval_mode="dry_run"` → `live_summary is None`、`passed=False`、`approval.status in {"dry_run","confirmed"}`。

- **顺手抓的 latent bug**（commit `c428522`）：`fe["side"] in {"BUY","SELL"}` 这条断言失败，挖出 `_PaperBridge.on_order_filled` 用了 `str(event.order_side)`，而 Nautilus `OrderSide.BUY` 的 `__str__` 返回 `"1"`（int 表示），不是 `"BUY"`。后果：`sign = Decimal(1) if side_str == "BUY" else Decimal(-1)` 始终走 `-1` 分支 —— BUY 反而**减**仓加现金，SELL 反而加仓减现金。修复为 `side_str = event.order_side.name`，回归全绿。
  - 为什么以前没发现：`tests/test_paper_runner.py` 只断 `final_nav_usd` 是非空字符串，未断符号 / 数值。
  - 为什么 T7 parity 还能过：两 variant 都跑同一份（已修复后的）bridge，两边同步反向就同步抵消。bug 修前 parity 也是相等的，只是相等于一组错误的 fills。

- **总测试矩阵**：`pytest tests/` → **400 passed / 0 failed**，全 offline，用时 59.7s。
- **Phase 3 增量 169 用例**（400 − Phase 2 末态 231）：

  | 文件 | 用例 |
  |---|---|
  | `test_order_intent.py` | 25 |
  | `test_strategy_base.py` | 16 |
  | `test_risk_rules.py` | 26 |
  | `test_risk_gate.py` | 13 |
  | `test_approval_gate.py` | 22 |
  | `test_signal_consumer.py` | 13 |
  | `test_momentum_follow.py` | 18 |
  | `test_paper_runner.py` | 3 |
  | `test_cli_paper.py` | 3 |
  | `test_cli_approve.py` | 6 |
  | `test_cli_strategy.py` | 3 |
  | `test_live_signal_runner.py` | 13 |
  | `test_cli_live_signal.py` | 3 |
  | `test_signal_replay_parity.py` | 2 |
  | `test_strategy_summary_schema.py` | 3 |
  | **合计** | **169** |

  比 brief §5 Task 8 中 "+25 用例" 目标多出 144 条。

### Phase 3.5 —— Runbook 实测 + 硬化（2026-05-23）

Phase 3 结果报告（commit `8c60af5`）落盘后立刻按 `docs/phase3_runbook_testnet.md` 跑端到端 testnet hop。runbook 执行过程中暴露了 8 个真实问题，全部修复并加回归测试。

**实测交付**

- 实测时间：`2026-05-23T03:10:26Z`（节点 STARTING）→ `03:10:47Z`（节点 DISPOSED），墙钟 21.5s。
- 实测路径：`xtrade live signal-run --strategy momentum_follow --instrument BTCUSDT-PERP.BINANCE --signals-from data/signals --mode manual`，Binance Futures testnet（`demo-fapi.binance.com` / `testnet.binancefuture.com`）。
- 实测证据：
  - `run_id: live-20260523T030713Z`，summary `logs/live-20260523T030713Z/live_signal_summary.json` 落盘，schema 与 §3.2 完全匹配。
  - approval `record_id=1de841fa7ed7a038, mode=manual, status=confirmed`，operator 在 Terminal B 跑 `xtrade approve confirm 1de841fa7ed7a038`。
  - venue order：`client_order_id=O-20260523-031036-001-000-1`、`venue_order_id=13179853156`，limit BUY 0.0020 BTCUSDT-PERP @ 52896.40（市价 75566.30 / 0.7 safety multiplier），accepted 526ms 后 canceled 555ms 后。
  - `xtrade live health` 三 venue 全 PASS（binance_spot +1390ms / binance_futures +715ms / hyperliquid +3543ms）。
- runbook §4 验证 checklist 6 项全 PASS。

**8 个硬化 commit（按时间顺序）**

| commit | 范围 | 触发场景 |
|---|---|---|
| `c71ea93` | `SignalConsumer.cursor` 持久化 + `xtrade.risk.dry_run` 标定助手 | 进 runbook 前的预防性硬化（重启不重放、risk.yaml 上线前可干跑） |
| `aec62d5` | `xtrade live health` / `live run` 中 spot+futures Venue 共存 abort | 默认 yaml 同时配 spot 和 futures 时，单 process 内两条 Binance 子账号都注册到 `Venue('BINANCE')` → `node.build()` 抛 `Execution client for venue Venue('BINANCE') already registered` |
| `70fb0f6` | `_narrow_venues_cfg` 把 `VenuesConfig` 按 `--venues` 收窄到子集再交给 `probe()` | 同 venue collision，从 `live health` 的入口提前裁掉 |
| `184a360` / `8b0d0ee` | `TradingNode` 必须在 `asyncio.run` 内构造（而非外层同步代码里 build 后再 run） | 在外层 build 的节点其 DataEngine 绑到错误的 event loop，连接成功但 quotes 永不回流 → `xtrade live health` 静默超时 |
| `2ca3838` | 拆 `venues.testnet.yaml` 为 3 个 per-venue 子 yaml + 1 个 gutted 指针；`live health` 链式跑（每 venue 自己的 TradingNode） | 同 spot+futures collision 的根治：每个子账号自己一个 process-local node，sequential |
| `e650c10` | `ApprovalQueue` 幂等键从 `fingerprint` 升级为 `(fingerprint, mode)` | runbook §3 第一次跑：先 dry_run 留一行 `confirmed/dry_run`，再 manual 时 `ApprovalQueue.submit()` 命中同 fingerprint 直接返回 dry_run 行，`ApprovalGate.decide` 读到 `status=confirmed` → `go=True` 绕过人工审批 |
| `90bcc34` | `live run` / `live signal-run` 默认 `--venues-yaml` 改为按 `--instrument` 自动解析对应 sibling | 默认指向的是 gutted 指针文件（`venues.testnet.yaml`），单 venue 命令在默认调用下立刻撞 `load_venues` 的 "no venues" |

**测试增量**

| 文件 | Phase 3 末态 | Phase 3.5 后 | 增量 |
|---|---|---|---|
| `test_signal_consumer.py` | 13 | 18 | +5（cursor 持久化、atomic flush、corrupt-JSON 安全重放） |
| `test_risk_dry_run.py` | — (新文件) | 8 | +8（`xtrade.risk.dry_run` calibration helper） |
| `test_cli_risk.py` | — (新文件) | 11 | +11（`xtrade risk dry-run` CLI） |
| `test_approval_gate.py` | 22 | 27 | +5（`(fingerprint, mode)` 幂等 4 条 + gate manual 不消费 dry_run 1 条） |
| `test_live_signal_runner.py` | 13 | 14 | +1（manual 不 latch 到 dry_run 审计行） |
| `test_cli.py` | 19 | 34 | +15（spot+futures 拒绝 / per-venue chaining / instrument-only inference / auto-resolve 4 条） |
| `test_config.py` | 16 | 17 | +1（per-venue yaml 加载） |
| `test_node_factory.py` | 12 | 12 | 0（仅重构：VenueConfigError + asyncio.run 内 build，不计入条目） |
| 合计 | 400 | **446** | **+46** |

`pytest tests/` → **446 passed / 0 failed**，仍全 offline。

**Phase 3.5 解决了什么**

- T6 从"理论上可跑"晋升为"已实跑、有 venue ack、有 record_id、有 summary"。
- runbook 的 `--venues-yaml` UX 从"必须显式传 per-venue 文件" → "省略即可，按 instrument 自动解析"。
- approval 幂等性的隐患（同 fingerprint 跨 mode 互相吞）从代码、测试、文档三层堵死。

`docs/phase3_runbook_testnet.md` §0 pointer-file 段落、§5 失败模式表已同步更新；§4 验证 checklist 不变。

---

## 2. 与 Phase 2 的差异 / 进步

| 维度 | Phase 2 | Phase 3 |
|---|---|---|
| 信号去向 | 写 `SignalQueue`，下游不定义 | `SignalConsumer.iter_new()` cursor 消费 → 策略翻译 → intent |
| 订单形态 | 无 | `OrderIntent`（frozen，Decimal 全链路，凭据扫描 metadata） |
| 风控位置 | 无 | `RiskGate` 强制单点；AST lint 守护 strategy 层不绕过 |
| 审批通道 | 无 | `ApprovalGate` 三档（auto / dry_run / manual）+ jsonl 队列 + CLI 操作 |
| 撮合环境 | 无（仅 vectorbt 计 PnL） | paper（Phase 1 `BacktestEngine`）+ testnet（Phase 1 `run_live` 工厂） |
| 审计制品 | `scan_summary.json` | `paper_summary.json` / `live_signal_summary.json` / `data/approvals/` 三类 |
| Parity 验证 | vectorbt ↔ Nautilus 同 bar-index 对齐 | `ApprovalGate(auto)` ↔ pass-through stub 同 fill-by-fill Decimal-strict |
| CLI 子命令 | + `scan {universe,run,inspect}` | + `strategy {list,describe}` / `paper run` / `approve {list,confirm,reject}` / `live signal-run` |
| 测试总量 | 231 | **446**（+215，含 Phase 3.5 硬化 +46） |

---

## 3. 关键数据样本

### 3.1 `paper_summary.json` 强制 schema

```json
{
  "run_id": "schema-paper",
  "started_at": "2026-05-22T...+00:00",
  "completed_at": "2026-05-22T...+00:00",
  "mode": "paper",
  "strategy": "momentum_follow",
  "approval_mode": "auto",
  "instrument_id": "BTCUSDT-PERP.BINANCE",
  "venue": "BINANCE",
  "bar_type": "BTCUSDT-PERP.BINANCE-1-MINUTE-LAST-EXTERNAL",
  "bars_loaded": 200,
  "signals_consumed": 3,
  "intents_generated": 3,
  "risk_rejected": 0,
  "approvals_pending": 0,
  "approvals_confirmed": 3,
  "approvals_rejected": 0,
  "approvals_dry_run": 0,
  "fills": 3,
  "fill_events": [
    {"ts_event": 1700001200000000000, "symbol": "BTCUSDT-PERP.BINANCE",
     "side": "BUY",  "qty": "0.016", "price": "30245.00"},
    {"ts_event": 1700003600000000000, "symbol": "BTCUSDT-PERP.BINANCE",
     "side": "SELL", "qty": "0.032", "price": "29812.50"},
    {"ts_event": 1700007200000000000, "symbol": "BTCUSDT-PERP.BINANCE",
     "side": "BUY",  "qty": "0.016", "price": "30001.25"}
  ],
  "final_cash_usd": "1000004.92",
  "final_position_qty": "0.000",
  "final_nav_usd": "1000004.92",
  "peak_nav_usd": "1000012.31",
  "max_drawdown_pct": 0.0,
  "elapsed_s": 6.41,
  "errors": [],
  "config": { "...": "snapshot of strategy_config + risk_rules" }
}
```

约束：`fills == len(fill_events)`；`approvals_confirmed + approvals_pending + approvals_dry_run + approvals_rejected + risk_rejected == intents_generated`；所有 `*_usd` / `qty` / `price` 字段均 `Decimal(s)` round-trip。

### 3.2 `live_signal_summary.json` 强制 schema

```json
{
  "run_id": "live-signal-...",
  "started_at": "ISO8601 (UTC)",
  "completed_at": "ISO8601 (UTC)",
  "mode": "live_signal",
  "strategy": "momentum_follow",
  "approval_mode": "manual",
  "instrument_id": "BTCUSDT-PERP.BINANCE",
  "signal": {
    "symbol": "...", "venue": "binance",
    "direction": "LONG", "strength": 0.6,
    "generated_at": "ISO8601 (UTC)",
    "source": "momentum:..."
  },
  "intent": { "venue": "...", "symbol": "...", "side": "BUY",
              "order_type": "LIMIT", "quantity": "0.002",
              "limit_price": "21000.00", "reduce_only": false,
              "time_in_force": "GTC", "source_signal_id": "...",
              "created_at": "ISO8601 (UTC)" },
  "approval": { "record_id": "16-hex",
                "status": "confirmed",
                "mode": "manual",
                "go": true, "awaiting": false },
  "live_summary": { "run_id": "...", "order": {"accepted": true,
                                               "canceled": true,
                                               "rejected": false} },
  "passed": true,
  "note": "",
  "config": { "...": "snapshot" }
}
```

`live_summary` 仅在 `passed=True` 时为 dict，dry_run / 失败路径下为 `null`。

### 3.3 Replay-parity 结果

`tests/test_signal_replay_parity.py::test_paper_and_direct_paths_produce_identical_fills`：

- 同 sine-wave 200-bar catalog + 同 3 条 signals。
- paper variant fills 数：**3**。
- direct variant fills 数：**3**。
- `(ts_event, symbol, side, qty, price)` 逐位严格相等：**True**。
- 全部 9 项 summary 计数器/Decimal 字段也全等。
- 单条测试用时：~16s（两次 subprocess 启动 + 两次 BacktestEngine 启动）。

### 3.4 一次完整 `xtrade paper run`（CLI 样本）

```
$ xtrade paper run \
    --strategy momentum_follow \
    --catalog data/catalog \
    --instrument BTCUSDT-PERP.BINANCE \
    --bar 1m \
    --signals-from data/signals \
    --approval-mode auto \
    --run-id phase3-demo
run_id:          phase3-demo
strategy:        momentum_follow
instrument:      BTCUSDT-PERP.BINANCE  bar=1m
signals_consumed: 27   intents_generated: 27   risk_rejected: 3
approvals:       confirmed=24  pending=0  rejected=0  dry_run=0
fills:           24
final_nav_usd:   1000180.55     peak_nav_usd: 1000245.12
max_drawdown_pct: 0.026
elapsed_s:       7.84
summary:         logs/phase3-demo/paper_summary.json
```

---

## 4. 已知限制 / Phase 4 衔接

### 已知限制（继承 Phase 1 / 2，本阶段不解）

- **Nautilus `BacktestEngine` 单进程二次实例化 abort**：Phase 1 已识别；Phase 3 中 `tests/test_paper_runner.py`、`tests/test_signal_replay_parity.py`、`tests/test_strategy_summary_schema.py` 全部走 subprocess hop（沿用 `tests/_parity_nautilus_runner.py` 范式）。
- **Binance Futures testnet venue 侧抖动**：Phase 3 默认走 Binance Spot testnet + Hyperliquid testnet（按 brief §2 决策矩阵第 4 行）。
- **vectorbt 全局 `vbt.settings` 缓存**：Phase 2 已记录；Phase 3 未触发新 flake。

### Phase 3 自身的开放项（不影响进入 Phase 4）

- **manual 模式仍是文件轮询**：`run_live_signal` 在 `manual` 下用 `time.sleep(poll_interval)` 轮询 jsonl —— Phase 4 引入 Telegram/Web 推送后可替换为事件通知，但文件队列仍是落地审计真相。
- **`MaxDrawdownPct` 用 peak_nav 静态比较**：未考虑跨日 peak 滚动窗口；任何强反弹后回落都会一直在熔断。Phase 4 / Phase 5 评估是否引入"trailing peak"或"daily reset"语义。
- **`MomentumFollow` 是最简翻转**：未做 sizing / slippage / 部分撤单逻辑 —— brief §1 显式说"动态 sizing 留 Phase 5"，符合预期。
- ~~**`SignalConsumer` cursor 仅维护内存**~~：Phase 3.5（`c71ea93`）已落地 `xtrade.strategy.cursor`，`SignalConsumer(cursor_path=...)` 支持原子持久化 + `commit()` 显式 flush。Phase 4 可直接接 Telegram/Web 触发 commit。
- **`live signal-run` 单次跑完即退**：不留长进程 —— 与 brief §5 Task 6 一致；Phase 4 的"常驻 supervisor"再做。

### Phase 4 衔接建议

1. **审批通道扩展**：Phase 4 接 Telegram 时，`ApprovalQueue` 的 jsonl 仍是单一真相源；Telegram bot 只是 `xtrade approve confirm/reject` 的远程触发面（写同一份 jsonl）。Phase 3 的 atomic write 模板可不变。
2. **容器化路径**：`data/approvals/`、`data/signals/`、`data/catalog/`、`logs/` 四个目录全部要 mount 进容器；环境变量保持 `BINANCE_TESTNET_API_KEY` / `BINANCE_TESTNET_API_SECRET` 形态，不要硬编码 venues yaml 中。
3. **观测落地**：Phase 4 的 Grafana / Loki 接入只要"汇总 `logs/<run-id>/*_summary.json`"即可 —— 全部走 atomic write，可被 file-tailing log shipper 安全读取。建议把 `paper_summary.json` / `live_signal_summary.json` 的 schema 固化为 JSON Schema 文件供 dashboards 校验（Phase 3 这里以 Python dict spec 落在测试里，Phase 4 把它转 `*.schema.json`）。
4. **故障恢复演练**：Phase 4 应给 `run_live_signal` 加 `--resume-from <record_id>` 路径，从已有 `pending` 行继续轮询而不重新创建一行 —— 当前每次启动都走 `intent.fingerprint()`，同 intent 多次 retry 是幂等的，但 manual 操作员看到的会是同一 record_id 的不同 `created_at` —— 不破坏审计，但 UX 上需要明确语义。
5. **mainnet 解锁**：Phase 5 才解；当前 `run_live` 在 venues.yaml `testnet: false` 时即拒，无需在 Phase 3 提供任何 override 路径。Phase 4 加 mainnet 时建议沿用同一硬闸 + `--mainnet --i-understand` 双确认。

---

## 5. 提交历史（Phase 3）

```
# Phase 3 主干
c428522 Phase 3 Task 8: strategy_summary schema tests + fix BUY/SELL sign bug (T8)
d058df6 Phase 3 Task 7: replay-parity test + fill_events in paper summary (T7)
5edad2c Phase 3 Task 6: testnet end-to-end signal runner + runbook (T6)
7a2ba95 Phase 3 Task 5: paper runner + strategy/paper/approve CLI groups (T5)
7dbd432 Phase 3 Task 4: SignalConsumer + MomentumFollow plugin (T1, T5)
f9b99c3 Phase 3 Task 3: ApprovalGate + ApprovalQueue (T4)
1c7f471 Phase 3 Task 2: risk module + import-graph lint (T3)
05e95a2 Phase 3 Task 1: OrderIntent + SignalDrivenStrategy contract (T1, T2)
a02e250 Phase 3 brief: strategy framework + paper trading
8c60af5 Phase 3 results report

# Phase 3.5 实测 + 硬化（runbook 实跑触发，2026-05-22 至 2026-05-23）
c71ea93 Phase 3.5 hardening: cursor persistence + risk dry-run calibration
aec62d5 Fix Binance spot+futures venue collision in live commands
70fb0f6 Narrow VenuesConfig in `live health` to dodge spot+futures guard
184a360 Build TradingNode inside the asyncio loop to fix silent data client
8b0d0ee Construct TradingNode inside asyncio.run so engines bind to the live loop
2ca3838 Split testnet venues into per-venue yamls; chain `live health` probes
e650c10 Scope approval-queue idempotency by `(fingerprint, mode)`
90bcc34 Auto-resolve per-venue yaml from `--instrument` for live single-venue commands
```

---

## 6. 决策

按 `docs/phase3_brief.md §7` 的决策矩阵：

- T1–T7 全 PASS → **进入 Phase 4**：容器化 + 云端部署 + Grafana / Loki / Telegram 接入 + 故障恢复演练。
- T8 为收尾要求，亦 PASS（含意外修复的 latent BUY/SELL 反向 bug）。
- T6 走的是手工 runbook 路径（brief §5 Task 6 明确"不写自动化测试"），自动化覆盖到 venue hop 之前；操作员按 `docs/phase3_runbook_testnet.md` §4 的 6 项 checklist 验证后即可签收。**2026-05-23 已由操作员实跑通过（Binance Futures testnet），证据 §1.9。**
- Phase 3.5（runbook 实测 + 8 commit 硬化，`c71ea93..90bcc34`）于实测后落地：venue collision、event-loop 绑定、approval 跨 mode 幂等、`--venues-yaml` 自动解析全部从代码 + 测试 + 文档三层堵死；测试矩阵 400 → 446，仍全 offline。

无需在结果报告中记录的偏差 / 降级条目。
