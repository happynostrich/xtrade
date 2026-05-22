# Phase 2 实施简报 —— 机会发现 / 扫描层

> 编制日期：2026-05-22
> 目标仓库：`/Users/bitcrab/xtrade`
> 上游依据：
> - 主路线图：`xtrade_plan.md` §七 "Phase 2 — 机会发现/扫描层"
> - Phase 1 收尾：`docs/phase1_results.md`（结论：P1–P8 全 PASS → 进入 Phase 2）
> 执行方式：本简报交给 **Claude Code** 在 `/Users/bitcrab/xtrade` 中执行。

---

## 0. 进入 Phase 2 的前提（来自 Phase 1 结论）

Phase 1 已交付：

- **包结构**：`src/xtrade/{node,data,strategies,live,backtest}/` + `xtrade.cli:app`。
- **数据底座**：`ParquetDataCatalog` + `xtrade data ingest --venue {binance,hyperliquid}` 幂等抓取；`xtrade.data.catalog.{read_bars,write_bars,missing_intervals}` 稳定接口。
- **执行路径**：`XtradeStrategy` 基类 + `mode: backtest|live` 路由；`xtrade backtest run` 与 `xtrade live run` 跑同一份策略代码。
- **可观测性**：`run_with_logging` 统一 `logs/<run-id>/{run.log, summary.json, config.snapshot.yaml}`；退出码 0/1/2 三档契约。
- **测试矩阵**：`pytest tests/` 108 用例全 offline。

主路线图 §七 Phase 2 定位：

> **Phase 2 — 机会发现/扫描层（2–3 周）**
> 用 vectorbt 搭扫描器：先做技术面（动量、均值回归、突破、跨品种价差）；
> 再接入基本面/链上/宏观流动性数据做过滤；产出"候选清单 + 信号"
> 写入信号队列。ML 与新闻情绪作为本阶段之后的增量。

Phase 2 不替换 Phase 1 的 Nautilus 执行内核 —— 而是在它**之上**架一层独立的研究/筛选环路：扫描器周期性消费 catalog，输出信号到队列；下游（Phase 3 才接的策略框架）按需消费这些信号。

---

## 1. Phase 2 的使命与非使命

**使命（Phase 2 要做的）**

1. 建立**符号宇宙（symbol universe）**：从 Binance Perp + Hyperliquid HIP-3 中按显式规则圈出可扫描的标的池（流动性、行情质量、地理可达）。
2. 把 Phase 1 的 `ParquetDataCatalog` bars 桥接成 vectorbt 友好的 `pd.DataFrame`（time-indexed OHLCV，多标的对齐）。
3. 至少实现 **4 个技术面扫描器**：动量、均值回归、突破、跨品种价差（基础形态即可，不追求 alpha 厚度）。
4. 用 vectorbt 的向量化能力做**参数网格搜索**：每个扫描器在符号宇宙 × 参数网格上一次性算出全部组合，按 Sharpe/收益/胜率排序保留 top-k。
5. 定义并实现**信号队列契约**：扫描器产出 `Signal`（标的、方向、强度、生成时间、策略来源、有效期），写入磁盘队列（jsonl）；下游可纯读消费。
6. 暴露 CLI：`xtrade scan run`（跑一次扫描）/ `xtrade scan inspect`（查最近 N 个信号）。
7. 与 Phase 1 已有的事件驱动回测做**一次性 parity 抽查**：同一根 bar 序列、同一策略形态，vectorbt 与 Nautilus `BacktestEngine` 的方向性结果一致（不要求逐笔成交价完全相同，但 long/flat 翻转序列应一致）。
8. 把扫描运行接入 `logs/<run-id>/`，写 `scan_summary.json`（覆盖了多少标的、每个扫描器跑了多少参数组合、产出了多少信号、耗时）。

**非使命（Phase 2 不做的）**

- 不写真正的研究级 alpha（扫描器是**形态发现**，不是赚钱保证）。
- 不接基本面 / 链上 / 宏观 / 新闻 / ML 数据源（这些在路线图里属"本阶段之后的增量"）。
- 不做策略插件框架与审批网关（属 Phase 3）。
- 不做实盘信号消费（Phase 3 / Phase 4）。
- 不做云端定时调度 / Grafana / Telegram 告警（Phase 4）。
- 不引入 PostgreSQL / TimescaleDB / Redis（依旧 Parquet + jsonl）。

> 边界原则：Phase 2 的输出是"研究环路的雏形"，可以稳定喂出"今天哪些标的的哪些形态最值得看"；它**不是**自动交易决策器。

---

## 2. 验收标准（Go / No-Go 清单）

每项明确 PASS / FAIL，记入 `docs/phase2_results.md`。

| ID  | 名称 | 描述 |
|-----|------|------|
| S1  | 符号宇宙 | `config/universe.example.yaml` 列出 Binance Perp + Hyperliquid HIP-3 的可扫标的；`xtrade.research.universe.load_universe(path)` 返回强类型 `UniverseConfig`；CLI `xtrade scan universe` 打印解析结果。 |
| S2  | Catalog → DataFrame 桥 | `xtrade.research.frames.bars_to_dataframe(catalog, instrument, bar_spec, since, until)` 返回带 `DatetimeIndex` 的 OHLCV DataFrame；`bars_to_panel(...)` 把多标的拼成多列 close panel（标的为列，UTC 索引对齐）。 |
| S3  | 扫描器接口 | `xtrade.research.scanners.base.Scanner` 抽象基类定义 `run(panel, params) -> pd.DataFrame[Signal]`；至少 4 个实现：`MomentumScanner`、`MeanReversionScanner`、`BreakoutScanner`、`SpreadScanner`。每个内部使用 vectorbt 向量化算子。 |
| S4  | 参数搜索 | `xtrade.research.gridsearch.run_grid(scanner, panel, param_grid, scoring)` 用 vectorbt 一次跑完笛卡尔积，输出 `pd.DataFrame` 含 (scanner, params, sharpe, total_return, win_rate, n_trades)；top-k 截断可配置。 |
| S5  | 信号队列 | `xtrade.research.signals.{Signal, SignalQueue}`：`Signal` 是 frozen dataclass（`symbol`、`venue`、`direction`、`strength`、`generated_at`、`source`、`valid_until`）；`SignalQueue.append(signals)` 写 `data/signals/<YYYY-MM-DD>.jsonl`，`SignalQueue.tail(n)` 读末尾 n 条。重复运行幂等（同 generated_at + symbol + source 去重）。 |
| S6  | CLI | `xtrade scan run --universe config/universe.example.yaml --scanner momentum --since ... --until ...` 跑一次扫描，将信号写入队列并打印 top-k；`xtrade scan inspect --since ... --source ...` 列出近期信号。 |
| S7  | Nautilus parity 抽查 | 至少一个扫描器（建议 `MomentumScanner`）在同一段 bars 上：vectorbt 产出的 long/flat 翻转点位与 Phase 1 demo_ema 风格的 Nautilus 事件驱动回测翻转点位一致（容差：±1 bar）。`tests/test_parity_vectorbt_nautilus.py` 自动化。 |
| S8  | 可观测性与测试 | 每次 `xtrade scan run` 写 `logs/<run-id>/scan_summary.json`（universe size、scanner、参数组合数、信号数、耗时）；`pytest tests/` 全绿，新增至少 5 个 offline 测试覆盖 S2–S5。 |

**Phase 2 通过条件**：S1–S7 全 PASS；S8 为收尾要求。任何一项 FAIL 在结果报告中明确记录原因与解决路径。

---

## 3. 不在本阶段处理的事项（显式延后）

- **基本面 / 链上 / 宏观数据接入**：留接口（`Scanner.run` 接受 `extra_context: dict` 占位），不实装数据源。
- **新闻情绪与 ML 信号**：路线图 §七 明示"作为本阶段之后的增量"。
- **策略插件框架 / 审批网关 / 风控单点**：属 Phase 3。
- **信号消费侧**：Phase 2 只写队列；Phase 3 才把信号接进 `XtradeStrategy`。
- **实时增量扫描**：Phase 2 的扫描器是**离线批扫**（按指定时间窗），不做 streaming。
- **跨语言/分布式参数搜索**：vectorbt 的单机向量化已经能跑万级组合，不引入 Dask/Ray。
- **未上线 venue 的扫描**：Phase 1 已确认 Binance Futures testnet 不可达；Phase 2 的扫描使用 **Binance USDT-M Perp 主网行情**（已在 Phase 0/1 验证只读可达）+ **Hyperliquid HIP-3 主网行情**。所有数据都是 catalog 中已有的历史 bars，不依赖任何 testnet 通路。

---

## 4. 目标仓库结构（增量于 Phase 1）

```
xtrade/
├── config/
│   ├── venues.example.yaml         (已有)
│   ├── venues.testnet.yaml         (已有)
│   └── universe.example.yaml       (新增；显式列出符号宇宙)
├── src/xtrade/
│   ├── cli.py                      (已有；新增 `scan` 子命令组)
│   ├── research/                   (新包，纯研究层)
│   │   ├── __init__.py
│   │   ├── universe.py             (UniverseConfig + load_universe)
│   │   ├── frames.py               (bars_to_dataframe / bars_to_panel)
│   │   ├── signals.py              (Signal + SignalQueue)
│   │   ├── gridsearch.py           (run_grid，封装 vectorbt 参数网格)
│   │   └── scanners/
│   │       ├── __init__.py
│   │       ├── base.py             (Scanner 抽象基类 + 注册表)
│   │       ├── momentum.py
│   │       ├── mean_reversion.py
│   │       ├── breakout.py
│   │       └── spread.py
│   └── (其他 Phase 1 包不动)
├── scripts/phase2/                 (新增；薄包装)
│   ├── 01_inspect_universe.py
│   ├── 02_scan_momentum.py
│   ├── 03_scan_all.py
│   └── 04_signal_queue_demo.py
├── data/
│   ├── catalog/                    (已有，Phase 1 ingest 产物)
│   └── signals/                    (新增；jsonl 信号队列根，gitignored)
├── logs/                           (已有；新增 scan_summary.json)
├── tests/
│   ├── test_universe.py            (新增)
│   ├── test_frames.py              (新增)
│   ├── test_scanners.py            (新增)
│   ├── test_gridsearch.py          (新增)
│   ├── test_signals.py             (新增)
│   ├── test_parity_vectorbt_nautilus.py (新增；S7)
│   └── test_cli_scan.py            (新增；CLI exit-code 契约)
└── docs/
    ├── phase1_brief.md             (已有)
    ├── phase1_results.md           (已有)
    ├── phase2_brief.md             (本文件)
    └── phase2_results.md           (新增；执行中追加)
```

`scripts/phase2/*.py` 仅做参数解析与 `xtrade.research.*` 调用，与 Phase 1 风格一致。

---

## 5. 任务分解

### Task 1 —— 符号宇宙 + research 包骨架 (S1)
- 新增 `xtrade.research` 包；定义 `UniverseConfig`、`SymbolSpec`（venue、symbol、quote、min_volume 等可选过滤项）。
- `config/universe.example.yaml` 列出至少 10 个 Binance Perp 与 5 个 Hyperliquid HIP-3 标的，覆盖 BTC/ETH/几个主流 alt + xyz:TSLA/xyz:NVDA 等。
- CLI 新增 `xtrade scan` 子命令组（与 `data` / `backtest` / `live` 同级），首个子命令 `xtrade scan universe --config ...` 解析并打印宇宙。
- 验收：`xtrade scan universe --help` 与 `xtrade scan universe --config config/universe.example.yaml` 全部 exit 0；测试覆盖 yaml 缺失字段 / 重复符号 / 未知 venue 的拒绝（exit 2）。

### Task 2 —— Catalog → DataFrame 桥 (S2)
- `xtrade.research.frames.bars_to_dataframe(catalog_path, instrument, bar_spec, since, until)`：调 `xtrade.data.catalog.read_bars` → 转 `pd.DataFrame(index=DatetimeIndex(utc), columns=["open","high","low","close","volume"])`。
- `bars_to_panel(catalog_path, symbols: list[SymbolSpec], bar_spec, since, until)`：多标的拼成 panel（外层 join，统一索引）；缺失值显式 `NaN`，不填充。
- 单元测试覆盖：单标的 round-trip、多标的对齐、索引时区一致性（必须 `UTC`）、空 catalog 返回空 DataFrame 不报错。
- **不引入新依赖**：靠 pandas + Phase 1 已有的 `read_bars`。

### Task 3 —— 扫描器接口 + 4 个实现 (S3)
- `xtrade.research.scanners.base.Scanner` 抽象：`name: str`、`run(panel: pd.DataFrame, params: dict) -> pd.DataFrame[Signal]`。
- 扫描器注册表 `_SCANNER_REGISTRY: dict[str, type[Scanner]]`，由 CLI 按名字查找。
- **MomentumScanner**：N-period return rank → top quantile 给 +1，bottom quantile 给 -1。
- **MeanReversionScanner**：z-score = (close − rolling_mean) / rolling_std；阈值反向交易。
- **BreakoutScanner**：Donchian-N 通道突破。
- **SpreadScanner**：两资产 cointegration 残差 z-score（默认 BTCUSDT vs ETHUSDT）。
- 每个扫描器内部用 vectorbt 的 `IndicatorFactory` / `Portfolio.from_signals` 计算指标与回测指标，但 `run()` 返回的是**信号** DataFrame，不是 portfolio。
- 新依赖：`vectorbt>=0.26`（加入 `pyproject.toml` `[project.dependencies]`）。

### Task 4 —— 参数搜索 (S4)
- `xtrade.research.gridsearch.run_grid(scanner: Scanner, panel, param_grid: dict[str, list], scoring: str = "sharpe", top_k: int = 20)`：
  - 用 `itertools.product` 展开网格；对每个组合调 `scanner.run(panel, params)` → vectorbt `Portfolio.from_signals` 评估 → 汇总指标到一行。
  - 输出 `pd.DataFrame` 列：`scanner`, `params` (json str), `sharpe`, `total_return`, `win_rate`, `n_trades`，按 `scoring` 降序取 top-k。
- 默认 scoring：`sharpe`；可选 `total_return`、`win_rate × n_trades^0.5`（防止过少样本霸榜）。
- 单元测试在合成 sin-wave 数据上跑 `MomentumScanner` 网格，断言 top 1 的 `n_trades > 0` 且 `sharpe` 有限。

### Task 5 —— 信号队列 (S5)
- `Signal`（`@dataclass(frozen=True)`）：`symbol`、`venue`、`direction: Literal["LONG","SHORT","FLAT"]`、`strength: float ∈ [-1,1]`、`generated_at: datetime`、`source: str`（扫描器名 + 参数 hash）、`valid_until: datetime | None`、`metadata: dict`。
- `SignalQueue`（持久化在 `data/signals/<YYYY-MM-DD>.jsonl`）：
  - `append(signals: Iterable[Signal])`：原子写入（先写 `.tmp` 再 rename）；重复 (generated_at, symbol, source) 去重。
  - `tail(n)`、`since(dt)`、`filter(symbol=..., source=...)` 读侧便利方法。
  - 文件格式：每行一个 Signal 的 JSON（UTC ISO8601 时间戳）。
- 单元测试覆盖：写 → 读 round-trip、跨日写入分片、去重幂等、损坏 jsonl 行跳过并告警（不崩）。

### Task 6 —— CLI: scan run / scan inspect (S6)
- `xtrade scan run --universe ... --scanner momentum --bar 1m --since ... --until ... [--param-grid k=v,k=v]`：
  - 加载 universe → bars_to_panel → 实例化 scanner → `run_grid` → top-k 信号写入队列 → 打印简表。
  - 包在 `run_with_logging(mode="scan", ...)` 中；输出 `logs/<run-id>/scan_summary.json`。
- `xtrade scan inspect --since ... [--source ...] [--symbol ...] [--limit N]`：
  - 读队列，按过滤器输出最近 N 条；默认 limit=20。
- 退出码契约沿用 Phase 1：0 成功 / 1 业务失败（如 universe 空、无产生任何信号且 `--strict`）/ 2 配置失败。

### Task 7 —— vectorbt ↔ Nautilus parity 抽查 (S7)
- 选择 `MomentumScanner` 一个固定参数下的信号序列。
- 同样的策略形态（"momentum z-score 翻正做多 / 翻负平仓"）在 Phase 1 的 `XtradeStrategy` 框架下实现一个 `MomentumDemo`（不进入主策略库，只作为 parity 测试夹具）。
- `tests/test_parity_vectorbt_nautilus.py` 在合成 bars 上跑两侧，断言信号翻转 bar 索引序列一致（容差 ±1 bar，因为 Nautilus 是 bar-close 触发）。
- 该测试可以与 `test_backtest_smoke.py` 同进程运行（同样仅构造 `BacktestEngine`，不与 `TradingNode` 并存）。

### Task 8 —— 测试与可观测性 (S8)
- 所有新测试 offline；总数预计 +30 用例。
- `pytest tests/` 在断网环境通过。
- `scan_summary.json` 字段固定：`run_id`、`started_at`、`completed_at`、`universe_size`、`scanner`、`param_combos`、`signals_emitted`、`top_k`、`elapsed_s`、`errors`。
- `docs/phase2_results.md`：逐项 S1–S8 PASS/FAIL、关键数据样本、与 Phase 3 衔接建议。

---

## 6. 安全边界（沿用 Phase 0/1，新增几条研究层规则）

- **扫描器只读**：`xtrade.research.*` 任何函数不得调用 `xtrade.live.*` 或构造 `TradingNode` —— 由测试与 import-graph lint 守护。
- **catalog 只读**：扫描器只能 `read_bars`，不能 `write_bars`（防止把扫描算出来的合成序列污染 catalog）。
- **信号队列防误用**：`Signal.metadata` 严禁包含 API key / 私钥；写入时如检测到形如 `sk-` / `0x[0-9a-f]{64}` 字串，拒绝写入并报警。
- **vectorbt 内部缓存**：`vbt.settings` 全局缓存可能导致 pytest 间状态泄漏；测试 fixture 必须 reset 相关设置。
- **测试隔离**：`BacktestEngine` 与 `TradingNode` 同进程 abort 的 Phase 0 历史问题仍然有效——扫描器测试与 parity 测试都不构造 `TradingNode`，可与 `test_backtest_smoke.py` 共存。

---

## 7. 决策矩阵（Phase 2 收尾时）

| 结果 | 判断 | 下一步 |
|---|---|---|
| S1–S7 全 PASS | **进入 Phase 3** | Phase 3 把扫描器产出的信号接入策略框架：审批网关、风控单点、半自动确认。 |
| S7 fail，其余 PASS | **有条件 GO** | parity 偏差通常是 bar-close 时机问题；可以继续进 Phase 3，但需在 results 报告中记录已知偏差。 |
| S3 / S4 fail（扫描器或网格） | **暂停** | 这是 Phase 2 的核心交付，必须修。 |
| S2 fail（DataFrame 桥） | **暂停** | 整条研究环路的数据入口断了，下游都不能继续。 |
| Binance Futures testnet 在此期间恢复 | **不影响 Phase 2** | Phase 2 不依赖 testnet 执行；Phase 3 再决定是否切回。 |

---

## 8. 交付物

1. `src/xtrade/research/` 包：`universe.py`、`frames.py`、`signals.py`、`gridsearch.py`、`scanners/{base,momentum,mean_reversion,breakout,spread}.py`。
2. `config/universe.example.yaml`。
3. `xtrade.cli` 新增 `scan` 子命令组（`universe` / `run` / `inspect`）。
4. `scripts/phase2/` 下 4 个薄包装脚本。
5. `data/signals/` 目录（gitignored）+ 至少一次完整 scan run 在 `data/signals/<date>.jsonl` 写入。
6. `tests/` 下 7 个新测试文件（universe、frames、scanners、gridsearch、signals、parity、cli_scan），`pytest tests/` 全绿。
7. `pyproject.toml` 增加 `vectorbt` 依赖。
8. `docs/phase2_results.md`：S1–S8 PASS/FAIL、关键证据、与 Phase 3 衔接建议。

---

## 9. 建议执行顺序

1. Task 1（research 包骨架 + universe）→ Task 5 部分（`Signal` 数据类先行）。
2. Task 2（DataFrame 桥）→ 单测先行驱动接口设计。
3. Task 3（4 个扫描器）：先 Momentum，剩下 3 个套同样模板。
4. Task 4（参数搜索）依赖 Task 3 接口稳定。
5. Task 5 剩余（`SignalQueue` 持久化 + CLI 一并落地）。
6. Task 6（CLI：`scan run`/`scan inspect`）。
7. Task 7（parity 抽查）放最后，因为它需要 Task 3/4/5 都完成。
8. Task 8 的可观测性与测试贯穿前述任务，每个 task 落地立即补对应 test。
9. 全部完成后写 `docs/phase2_results.md`，给出进入 Phase 3 的建议。

---

## 参考链接

- vectorbt 官方文档：https://vectorbt.dev/
- vectorbt `IndicatorFactory`：https://vectorbt.dev/api/indicators/factory/
- vectorbt `Portfolio.from_signals`：https://vectorbt.dev/api/portfolio/base/#vectorbt.portfolio.base.Portfolio.from_signals
- NautilusTrader ParquetDataCatalog（Phase 1 已使用）：https://nautilustrader.io/docs/latest/concepts/data/#data-catalog
- 主路线图 Phase 2 段：`xtrade_plan.md` §七
- Phase 1 结果与限制：`docs/phase1_results.md`
