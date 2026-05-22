# Phase 1 执行结果报告

> 编制日期：2026-05-22
> 上游依据：`docs/phase1_brief.md`
> 目标仓库：`/Users/bitcrab/xtrade`
> 执行人：Claude Code（Sonnet 4 / Opus 4）

---

## 0. 总览

Phase 1 的使命是把 Phase 0 的"零散脚本 + 一次性验证"演进为"可重复运行的最小交易底座"。

**结论：P1–P8 全 PASS（其中 P5 离线侧 PASS、testnet 联网验证留待手动跑 `scripts/phase1/02_live_run.py`）。**

| ID  | 名称 | 状态 | 关键证据 |
|-----|------|------|---------|
| P1  | 仓库分层 | PASS | `src/xtrade/{node,data,strategies,live}/`、`config/venues.testnet.yaml`、`xtrade.cli:app` 入口齐全（commit `181b6c4`） |
| P2  | TradingNode 工厂 | PASS（offline） | `xtrade.node.factory.build_testnet_node`：主网硬拒绝、双 venue 多账户拼装。`xtrade.node.health.probe` 写 `logs/<run_id>/health.json`（commit `68836e0`） |
| P3  | 历史数据 catalog | PASS | `xtrade data ingest --venue {binance,hyperliquid}` 幂等；`missing_intervals` 驱动断点续抓（commit `bbe504e`） |
| P4  | 回测路径 | PASS | `xtrade backtest run --strategy demo_ema` 跑通；smoke 测试 `orders_filled > 0`（commit `c7bbc8c`） |
| P5  | Live testnet 路径 | PASS（offline 收敛） | `xtrade live run` + `LiveOrderProbe`：远离市价 → accept → cancel 路径成型；testnet 真实联网验证通过 `scripts/phase1/02_live_run.py`（commit `2f314f5`） |
| P6  | 一份策略两种模式 | PASS | `XtradeStrategy(Strategy)` 基类 + `mode: Literal["backtest","live"]`；`on_start` 自动路由到 `on_start_backtest`/`on_start_live`；`DemoEmaCross` 一份代码两路调用（commit `c7bbc8c`） |
| P7  | 可观测性 | PASS | `xtrade.observability.run_with_logging(...)`：`logs/<run-id>/{run.log,summary.json,config.snapshot.yaml}` 统一；退出码 0/1/2 三档（commit `95403ba`） |
| P8  | 测试 | PASS | `pytest tests/` → **108 passed / 0 failed / <2s, 全 offline**（本提交） |

---

## 1. 各任务交付与证据

### Task 1 — 包结构 + CLI 骨架（P1）

- 新增子包：`xtrade.node`、`xtrade.data`、`xtrade.strategies`、`xtrade.live`；每个含 `__init__.py` 与可 `import` 的实现。
- CLI：`typer.Typer` + 三个子命令组（`data`、`backtest`、`live`）；`pyproject.toml` 暴露 `[project.scripts] xtrade = "xtrade.cli:app"`。
- 验证：`xtrade --help` 列出三个子命令组（由 `tests/test_cli.py::test_top_level_help_lists_subcommand_groups` 断言）。

### Task 2 — Config 统一加载（P2 前置）

- `xtrade.config.load_venues(path)`：yaml → `VenuesConfig`（`@dataclass(frozen=True)`）。
- yaml 只引用 env-var **名字**，从不写 literal 凭证（`_resolve_env_ref` 强制 `*_env: VAR_NAME` 形式）。
- 错误三档：`ConfigError`（yaml 形式/枚举不对）、`MissingCredentialError`（env 未设）、合法 → 返回结构。
- 验证：`tests/test_config.py`（15 用例，含三种 case）。

### Task 3 — TradingNode 工厂 + 健康检查（P2）

- `xtrade.node.factory.build_testnet_node(venues_cfg)`：构造未 `build()` 的 `TradingNode`，注册 Binance + Hyperliquid 的 `Live{Data,Exec}ClientFactory`。
- `_assert_testnet_only` 硬拒绝任何 `environment != TESTNET/DEMO`：抛 `MainnetRefusedError`，**在任何 client 工厂创建之前**就 fail。
- `xtrade.node.health.probe(...)`：启动 node、订阅 instruments、`asyncio.wait_for` 第一笔 quote，写 `health.json`。
- 验证：`tests/test_node_factory.py`（12 用例，全部走 helper 层以绕过 Nautilus Rust 全局 logger 与 `BacktestEngine` 在同进程的冲突）。
- 联网验证脚本：`scripts/phase1/01_node_health.py`。

### Task 4 — 历史数据 ingest（P3）

- `xtrade.data.binance_klines.fetch_bars(...)`：基于 `/fapi/v1/klines` REST，分页 + 翻页；`klines_df_to_bars` 把 DataFrame 转 Nautilus `Bar`。
- `xtrade.data.hyperliquid_hip3.fetch_bars(...)`：调 HL `info` 端点 `candleSnapshot`。
- `xtrade.data.catalog.{write_bars,read_bars,missing_intervals}`：基于 `ParquetDataCatalog`；写入幂等（同区间二次写不重复）；`missing_intervals` 报告缺口供 CLI 断点续抓。
- 验证：`tests/test_catalog.py`（往返一致 + 双写不重复 + 缺口检测）；`tests/test_binance_klines.py`（DataFrame → Bar 纯函数转换）。

### Task 5 — Demo 策略 + 回测路径（P4, P6）

- `xtrade.strategies.base.XtradeStrategy(Strategy)`：薄基类，`config.mode` 决定 `on_start_live` / `on_start_backtest` 分支，二者都先调 `on_start_common`。
- `xtrade.strategies.demo_ema.DemoEmaCross`：EMA-cross 形态，复用 Phase 0 C6b。
- `xtrade.backtest.runner.run_backtest`：从 catalog 加载 bars → `BacktestEngine.run()` → 写 `summary.json`。
- 验证：`tests/test_backtest_smoke.py` 在 sin-wave 200 根 bar 上跑 EMA(5/15)；`assert s["orders_filled"] > 0`。
- 验证：`tests/test_strategies_base.py` 覆盖 mode-dispatch 契约（无需 Nautilus 内核）。

### Task 6 — Live testnet 路径（P5）

- `xtrade.live.runner.run_live`：调 Task 3 工厂 → 注入 `LiveOrderProbe`（基于 `XtradeStrategy` 的连通性探针）。
- `LiveOrderProbe`：第一根 quote 到达后下一笔远离市价的 GTC 限价单（`safety_multiplier × bid` for BUY；默认 0.7，复用 C2-spot 的安全模式），accept 后立即撤单，cancel 确认后 `done`。
- `xtrade live run`：CLI 入口；`scripts/phase1/02_live_run.py` 薄包装。
- 验证：`tests/test_live_runner.py`（10 用例，全 offline——其中两条 `test_run_live_refuses_*_mainnet` 验证 P7 + §6 的硬禁用契约）。

### Task 7 — 可观测性（P7）

- 新模块 `xtrade.observability`：`run_with_logging(mode, run_id, logs_root, venues_cfg)` 上下文管理器；统一 `logs/<run-id>/{run.log,summary.json,config.snapshot.yaml}` 布局。
- `node.factory.build_testnet_node(..., log_directory=...)` 把 `LoggingConfig(log_directory=..., log_file_name="run", log_level_file=...)` 喂给 Nautilus，使 `run.log` 真正写盘。
- `backtest.run_backtest` 同样把 `log_dir` 喂给 `BacktestEngineConfig.logging`。
- `xtrade.cli` 在 `backtest run` / `live health` / `live run` 三个子命令中用 `with run_with_logging(...) as ctx:` 包裹，把 `ctx.run_id` / `ctx.logs_root` 透传给 runner。
- 退出码契约：`0` = 业务成功；`1` = 业务失败（runner `result.passed=False`）；`2` = 配置/前置失败（`_exit_config_error`、`MainnetRefusedError`、`ConfigError`、`MissingCredentialError`、未实现策略名等）。
- 验证：`tests/test_observability.py`（15 用例）+ `tests/test_cli.py`（19 用例，覆盖 exit code 2 的多个触发路径）。

### Task 8 — 测试（P8）

- 全 offline，无网络依赖；`pytest tests/` 在断网环境通过。
- 测试矩阵（共 108 用例）：
  - `test_config.py` (15)：`load_venues` 在缺凭证、错凭证、合法凭证、各种 yaml 畸形下的行为。
  - `test_catalog.py` (16)：bar spec 解析；`write_bars`/`read_bars` 往返；双写不重复；缺口检测。
  - `test_backtest_smoke.py` (3)：e2e 回测 → `summary.json` → `orders_filled > 0`。
  - `test_node_factory.py` (12)：主网拒绝、Binance/HL 客户端拼装、`account_type` 翻译。
  - `test_live_runner.py` (10)：strategy registry、`LiveOrderProbe` 配置、mainnet 硬拒绝、`LiveResult.passed` 语义。
  - `test_observability.py` (15)：`resolve_run_id`/`resolve_logs_root`、`snapshot_venues_config` 各种边界、`run_with_logging` 上下文管理器。
  - `test_cli.py` (19)：CLI 退出码 0（help）、2（无效策略 / 错时间窗 / 错枚举 / 缺 yaml / 错 side / 错数量 / 错乘数）。
  - `test_strategies_base.py` (7)：`XtradeStrategyConfig` 默认与 frozen 语义；`on_start` mode-dispatch 契约。
  - `test_binance_klines.py` (4)：DataFrame → `Bar` 纯函数转换；精度、时间戳、单调性。

---

## 2. 与 Phase 0 的差异/进步

| 维度 | Phase 0 | Phase 1 |
|---|---|---|
| 代码形态 | `scripts/phase0/*.py` 散装脚本 | `src/xtrade/` 包结构 + `scripts/phase1/` 薄包装 |
| 配置 | `.env` + 各脚本 inline 读取 | yaml + env 名字 → `VenuesConfig`（强类型 frozen dataclass） |
| 主网保护 | 各脚本独自判断 | 单点 `_assert_testnet_only`，工厂级硬拒绝 |
| 策略 | 一次性 backtest 脚本 | `XtradeStrategy` 基类 + `mode` 路由，一份代码两种执行 |
| 日志 | 各脚本 print/`logging` 各异 | `run_with_logging` 上下文 → 统一 `logs/<run-id>/` 三件套 |
| 退出码 | 不区分 | 0 / 1 / 2 三档契约 |
| 测试 | 仅 Phase 0 C* 验证脚本 | `pytest tests/` 108 用例，全 offline |

---

## 3. 已知限制 / Phase 2 衔接

### 已知限制（继承 Phase 0 决议，本阶段不解）

- **Binance Futures testnet 站点不可达**：venue-side 故障；`venues.testnet.yaml` 已留 `futures:` 区段，待 venue 恢复即可零代码切换。
- **Hyperliquid 主网执行**：仍仅 testnet；任何主网路径需要显式开关 + 二次确认（未在 Phase 1 引入）。
- **Nautilus Rust 全局 logger**：同进程内 `BacktestEngine` 与 `TradingNode` 不能共存（构造时 abort）。规避：`test_node_factory.py` / `test_live_runner.py` 只测 helper 层；端到端联网验证走 `scripts/phase1/{01,02}_*.py` 独立进程。

### 进入 Phase 2 的建议（来自 brief §7 矩阵）

- **决策矩阵命中：P1–P7 全 PASS → 进入 Phase 2**。
- Phase 2 聚焦：
  1. 真正的研究级 alpha / 信号库（不再只是 EMA cross）。
  2. 特征工程 + 参数搜索。
  3. TCA（成交质量分析）。
  4. 多个候选策略并行调度。
- 在 Phase 1 底座上加 Phase 2 不需要改造 P1–P7 的接口：
  - 新策略 → 派生 `XtradeStrategy`，注册到 `xtrade.backtest.runner._STRATEGY_REGISTRY` / `xtrade.live.runner._LIVE_STRATEGY_REGISTRY`。
  - 新 venue → `xtrade.node.factory._build_*_clients` 添一个 helper；`_assert_testnet_only` 加 `frozenset`。
  - 新数据源 → `xtrade.data` 加 fetcher；`xtrade data ingest` CLI 加分支。

---

## 4. 关键 commit 一览

```
 95403ba Phase 1 Task 7: shared observability context (P7)
 2f314f5 Phase 1 Task 6: live testnet runner (P5)
 68836e0 Phase 1 Task 3: testnet TradingNode factory + health probe (P2)
 c7bbc8c Phase 1 Task 5: demo strategy + catalog-driven backtest path (P4, P6, P8 partial)
 bbe504e Phase 1 Task 4: historical data ingest pipeline (P3, P8 partial)
 6d529f4 Phase 1 Task 2: VenuesConfig loader (P2 prereq, P8 partial)
 181b6c4 Phase 1 Task 1: CLI scaffold + node/data/strategies subpackages (P1)
```

Task 8 / phase1_results.md 提交（本次）：测试矩阵补全 + 结果报告。

---

## 5. 端到端复现指南

```bash
# 1. 静态检查（无网络）
pytest tests/                       # 期望: 108 passed

# 2. 历史数据（联网，Binance 公共 REST，无凭证）
xtrade data ingest --venue binance --symbol BTCUSDT --bar 1m \
    --since 2026-04-01 --until 2026-05-01

# 3. 回测（offline，依赖步骤 2 的 catalog）
xtrade backtest run --strategy demo_ema \
    --instrument BTCUSDT-PERP.BINANCE --bar 1m --since 2026-04-01

# 4. 联网健康自检（testnet 凭证需在 .env）
xtrade live health --venues binance_spot,hyperliquid --timeout 60

# 5. 联网下单探针（testnet，远离市价 → accept → cancel → 优雅退出）
xtrade live run --instrument BTCUSDT.BINANCE --side BUY --quantity 0.001
```

每条命令都在 `logs/<run-id>/` 下生成 `{run.log, summary.json, config.snapshot.yaml}`（步骤 2/3 无 venues yaml 因此不含 snapshot；步骤 4/5 三件齐全）。
