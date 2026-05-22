# Phase 1 实施简报 —— 最小可运行的多场所交易底座

> 编制日期：2026-05-22
> 目标仓库：`/Users/bitcrab/xtrade`
> 上游依据：`docs/phase0_results.md`（Phase 0 决议：**GO**）
> 执行方式：本简报交给 **Claude Code** 在 `/Users/bitcrab/xtrade` 中执行。

---

## 0. 进入 Phase 1 的前提（来自 Phase 0 结论）

Phase 0 已确认：

- **执行内核**：NautilusTrader 1.227.0（Python 3.12+，Rust 内核）。
- **加密永续**：Binance USDT-M Futures 适配器在主网行情可用；执行路径通过 **Binance Spot Testnet**（Ed25519 + WS Trading API）端到端验证（C2-spot PASS）。Binance Futures Testnet 站点当前不可达（C2 SKIP，venue-side）。
- **美股永续**：trade.xyz（HIP-3 on Hyperliquid，`dex='xyz'`，78 个 symbol）主网行情可读；Hyperliquid 执行路径在 testnet 端到端验证通过（C3 PASS，Unified Account 模式）。
- **回测**：Nautilus `BacktestEngine` 在 4321 根 BTCUSDT 1m bar 上跑通 EMA-cross（C6b PASS）。
- **历史数据**：Binance USDT-M klines REST 公共端点可用（C6a PASS）。

Phase 1 在此基础上把"零散脚本+一次性验证"演进为"可重复运行的最小交易底座"。

---

## 1. Phase 1 的使命与非使命

**使命（Phase 1 要做的）**

构建一个**最小但端到端可运行**的交易底座，能够：

1. 用统一配置文件启动/停止一个 testnet `TradingNode`，同时连接 Binance Spot + Hyperliquid。
2. 持久化拉取 Binance 与 Hyperliquid HIP-3 的历史 K 线，写入 Nautilus `ParquetDataCatalog`。
3. 在 catalog 上跑回测、在 testnet 上跑同一份策略代码（"一份策略，两种执行模式"）。
4. 提供一个**真正可运行**的样例策略（不是研究级 alpha，仅作为底座连通性 demo），覆盖一个加密永续 + 一个股票永续。
5. 暴露最低限度的运行可观测性：结构化日志、健康检查端点（或周期性自检写盘）、错误告警钩子。

**非使命（Phase 1 不做的）**

- 不写真正的研究级 alpha / 信号挖掘。
- 不接入主网真实资金下单。
- 不做撮合优化、TCA、订单簿做市。
- 不做云端部署/CI；只确保本地可重复运行。
- 不做 GUI / 监控面板（最多 CLI + 日志文件）。

> 边界原则：Phase 1 的输出是"地基的第一层楼板"，不是"装修好的房间"。

---

## 2. 验收标准（Go / No-Go 清单）

每项要有明确 PASS / FAIL，记入 `docs/phase1_results.md`。

| ID  | 名称 | 描述 |
|-----|------|------|
| P1  | 仓库分层 | `src/xtrade/` 下出现 `node/`、`data/`、`strategies/`、`cli.py`；`config/` 下出现真实可加载的 `venues.testnet.yaml`。所有新代码可被 `import xtrade` 链路引用。 |
| P2  | TradingNode 工厂 | 单一函数 `build_testnet_node(cfg_path)` 同时构造 Binance Spot + Hyperliquid testnet 客户端，复用 Phase 0 已验证的配置形态（Ed25519 / Unified Account）。健康检查可在 N 秒内确认两条数据通道都收到行情。 |
| P3  | 历史数据 catalog | 一条 CLI 命令 `xtrade data ingest --venue binance --symbol BTCUSDT --bar 1m --since ...` 把 Binance klines 落入 `data/catalog/`（Nautilus `ParquetDataCatalog` 格式）；同一 CLI 支持 `--venue hyperliquid --symbol xyz:TSLA`（主网只读，HIP-3）。重复运行幂等（已存在的区间不重复抓）。 |
| P4  | 回测路径 | `xtrade backtest run --strategy demo_ema --catalog data/catalog --instrument BTCUSDT-PERP.BINANCE --since ...` 能从 catalog 加载 bars，跑 Phase 0 已通过的 EMA-cross 策略并打印账户/持仓报告。 |
| P5  | Live testnet 路径 | `xtrade live run --strategy demo_ema --venues binance_spot,hyperliquid` 启动 testnet TradingNode，在两边各订阅一个 instrument，限价单远离市价、提交、撤单各一次后优雅退出。复用 P2 工厂。 |
| P6  | 一份策略两种模式 | P4 和 P5 引用**同一个** `Strategy` 子类，仅通过 config 切换 instrument 与是否为 backtest 模式。无重复策略代码。 |
| P7  | 可观测性 | 结构化日志输出到 `logs/<run-id>/`；CLI 退出码 0/1/2 区分成功/业务失败/配置失败；每个 run 末尾写一份 `summary.json`（账户、订单、fills、错误计数）。 |
| P8  | 测试 | `tests/` 下至少：单元测试覆盖 catalog 路径解析与 config 加载；一个 e2e 烟雾测试运行 P4 的回测（不依赖网络）。 |

**Phase 1 通过条件**：P1–P7 全 PASS（P8 可作为收尾要求）。任何一项 FAIL 即在结果报告中明确记录原因与解决路径。

---

## 3. 不在本阶段处理的事项（显式延后）

- **Binance Futures testnet**：等待 venue 可用，届时把 P5 的 `binance_spot` 替换/扩充为 `binance_futures`。
- **Hyperliquid 主网执行**：仅在 testnet 验证；任何主网代码必须显式 `--mainnet` 开关 + 二次确认，且 Phase 1 不开。
- **多账户 / 子账户**：单账户对接。
- **数据库 / 时序库**：只用 Nautilus `ParquetDataCatalog`，不引入 PostgreSQL/InfluxDB。
- **撮合模拟模型**：用 Nautilus 默认 simulated venue 配置。

---

## 4. 目标仓库结构（增量于 Phase 0）

```
xtrade/
├── config/
│   ├── venues.example.yaml      (已有)
│   └── venues.testnet.yaml      (新增；从 .env 读凭证，不入库实值)
├── src/xtrade/
│   ├── __init__.py              (已有)
│   ├── config.py                (已有，扩展支持 venues yaml 合并)
│   ├── cli.py                   (新增；click 或 typer)
│   ├── node/
│   │   ├── __init__.py
│   │   ├── factory.py           (build_testnet_node)
│   │   └── health.py            (订阅自检)
│   ├── data/
│   │   ├── __init__.py
│   │   ├── catalog.py           (ParquetDataCatalog 路径与读写)
│   │   ├── binance_klines.py    (基于 phase0/07_fetch_binance_history.py 复用)
│   │   └── hyperliquid_hip3.py  (HIP-3 历史抓取)
│   └── strategies/
│       ├── __init__.py
│       ├── base.py              (薄基类；带 backtest/live 模式钩子)
│       └── demo_ema.py          (Phase 0 EMA-cross 的可复用形态)
├── scripts/phase1/              (新增；薄包装，只调用 src/xtrade)
│   ├── 01_node_health.py
│   ├── 02_ingest_binance.py
│   ├── 03_ingest_hyperliquid.py
│   ├── 04_backtest_demo.py
│   └── 05_live_demo.py
├── data/
│   ├── binance_BTCUSDT_1m.csv   (已有)
│   └── catalog/                 (新增；ParquetDataCatalog 根)
├── logs/                        (新增；gitignored)
├── tests/
│   ├── __init__.py              (已有)
│   ├── test_config.py
│   ├── test_catalog.py
│   └── test_backtest_smoke.py
└── docs/
    ├── phase0_results.md        (已有)
    ├── phase1_brief.md          (本文件)
    └── phase1_results.md        (新增；执行中追加)
```

`scripts/phase1/*.py` 只做参数解析 + 调用 `xtrade.*` 中真正的实现。Phase 1 的代码主体在 `src/xtrade/` 下，与 Phase 0 的"散装脚本"形成对照。

---

## 5. 任务分解

### Task 1 —— 包结构 + CLI 骨架 (P1)
- 新增子包 `xtrade.node` / `xtrade.data` / `xtrade.strategies`，每个都带 `__init__.py` 和最小占位实现。
- 引入 `typer` 或 `click`（优先 `typer`，更现代且类型友好）作为 `xtrade` 命令的入口。
- `pyproject.toml` 新增 `[project.scripts] xtrade = "xtrade.cli:app"`。
- 验收：`xtrade --help` 列出 `data`、`backtest`、`live` 三个子命令组。

### Task 2 —— Config 统一加载 (P2 前置)
- 扩展 `src/xtrade/config.py`：增加 `load_venues(path)`，把 yaml + .env 凭证合并成强类型对象（`@dataclass(frozen=True)`），供 `node/factory.py` 使用。
- 显式区分 `BinanceVenueConfig`（含 spot/futures + key_type）与 `HyperliquidVenueConfig`（含 unified_account 标记）。
- 错误时给出 Phase 0 风格的可执行提示（"在 .env 中加入 X"）。

### Task 3 —— TradingNode 工厂 + 健康检查 (P2)
- `xtrade.node.factory.build_testnet_node(venues_cfg)` 返回未 `build()` 的 `TradingNode`。
- `xtrade.node.health.probe(node, timeout_s, instruments)`：启动 node、订阅给定 instruments、确认 N 秒内每条通道都有 quote/trade，返回结构化结果。
- `scripts/phase1/01_node_health.py` 调用上述函数，写 `logs/<run-id>/health.json`。

### Task 4 —— 历史数据 ingest (P3)
- `xtrade.data.binance_klines.fetch(symbol, interval, start, end)` 抽自 Phase 0 的 07 脚本，支持分页、断点续抓、跳过已下载区间。
- `xtrade.data.hyperliquid_hip3.fetch(dex, symbol, interval, start, end)` 调用 Hyperliquid `info` 端点的 `candleSnapshot`。
- `xtrade.data.catalog.write_bars(catalog_path, bars, instrument)`：写入 `ParquetDataCatalog`。
- `xtrade data ingest ...` CLI；重复运行幂等。

### Task 5 —— Demo 策略 + 回测路径 (P4, P6)
- `xtrade.strategies.base.XtradeStrategy(Strategy)`：薄基类，约定 `on_start_live(self)` / `on_start_backtest(self)` 钩子（默认都委派到 `on_start`，只是把模式注入 `self.mode`）。
- `xtrade.strategies.demo_ema.DemoEmaCross`：复用 Phase 0 backtest 的 EMA-cross 形态，但 instrument/bar_type 走 config。
- `xtrade backtest run ...`：从 catalog 加载 bars，构造 `BacktestEngine`，注入策略，run，写 `summary.json`。
- `tests/test_backtest_smoke.py`：用一小段离线 bars 跑通整条路径。

### Task 6 —— Live testnet 路径 (P5)
- `xtrade live run ...`：用 Task 3 的 node 工厂 + Task 5 的策略；强制 testnet（mainnet 触发硬失败）。
- 策略在第一根 bar 收到后下一个远离市价的限价单（复用 C2-spot/C3 的安全模式），accepted 后立即撤单，撤单确认后优雅关闭。
- 写 `summary.json`。

### Task 7 —— 可观测性 (P7)
- `logs/<run-id>/` 目录结构：`run.log`（Nautilus 输出）、`summary.json`、`config.snapshot.yaml`。
- 退出码：0 = 业务成功；1 = 业务失败（如订单未在超时内确认）；2 = 配置/前置失败（缺凭证、catalog 缺数据）。
- 所有 CLI 入口共用一个 `run_with_logging(...)` 上下文。

### Task 8 —— 测试 (P8)
- `test_config.py`：`load_venues` 在缺凭证、错凭证、合法凭证三种 case 下的行为。
- `test_catalog.py`：`write_bars` / `read_bars` 往返一致；幂等覆盖正确。
- `test_backtest_smoke.py`：在 tmpdir 里跑回测，断言 `summary.json` 中 `orders_filled > 0`。
- 全部 offline，`pytest tests/` 在无网络环境通过。

---

## 6. 安全边界（沿用 Phase 0，强化部分）

- **主网执行硬禁用**：所有 live 入口检测到 `environment != TESTNET` 直接抛 `RuntimeError`。
- **凭证**：`.env` 不入库；`config/venues.testnet.yaml` 只能引用环境变量名，不能写 literal 凭证。
- **日志脱敏**：`run.log` 中不得出现 API key/secret/private key。`xtrade.config` 提供 `redact()` 工具，所有 `__repr__` 都用它。
- **订单安全模式**：Phase 1 demo 策略下的所有 testnet 订单都必须满足"远离市价 + 通过 venue 过滤器"（Spot BTCUSDT 用 0.7×bid，沿用 C2-spot 的修复）。

---

## 7. 决策矩阵（Phase 1 收尾时）

| 结果 | 判断 | 下一步 |
|---|---|---|
| P1–P7 全 PASS | **进入 Phase 2** | Phase 2 聚焦真实研究：信号库、特征工程、若干候选策略、参数搜索、TCA。 |
| P5 fail，其余全 PASS | **有条件 GO** | Phase 2 可以在回测路径上推进；live testnet 故障单独排查（多为 venue 凭证 / 网络）。 |
| P3 或 P4 fail | **暂停** | 历史 + 回测路径是 Phase 2 研究的输入，必须先修。 |
| P2 fail | **暂停** | Live 通路是后续真实运行的必要条件。 |
| Binance Futures testnet 在此期间恢复可达 | **同步小补** | 在 venues yaml 中加 `binance_futures`，但不阻塞 Phase 1 收尾。 |

---

## 8. 交付物

1. `src/xtrade/{node,data,strategies}/` 包结构与最小可运行实现。
2. `src/xtrade/cli.py` + `pyproject.toml` 的 `[project.scripts]` 入口。
3. `config/venues.testnet.yaml`（从环境变量读取凭证）。
4. `scripts/phase1/` 下 5 个薄包装脚本。
5. `data/catalog/` 中至少一份 Binance BTCUSDT 1m 与一份 Hyperliquid HIP-3 bars。
6. `tests/` 下 3 个测试文件，`pytest tests/` 全绿。
7. `docs/phase1_results.md`：逐项 P1–P8 的 PASS/FAIL、关键日志、问题记录、与 Phase 2 衔接建议。

---

## 9. 建议执行顺序

1. Task 1（CLI/包骨架）→ Task 2（config 合并）→ Task 8 的 `test_config.py` 先行（驱动设计）。
2. Task 4（数据 ingest）→ Task 8 的 `test_catalog.py`。
3. Task 5（策略基类 + 回测）→ Task 8 的 `test_backtest_smoke.py`。回测路径优先，因为它完全 offline，最稳。
4. Task 3（node 工厂）→ Task 6（live testnet）。需要联网且依赖凭证，放在最后。
5. Task 7 的可观测性贯穿前述任务实现（每个 CLI 加入即接入）。
6. 全部完成后写 `docs/phase1_results.md`，给出进入 Phase 2 的建议。

---

## 参考链接

- NautilusTrader ParquetDataCatalog：https://nautilustrader.io/docs/latest/concepts/data/#data-catalog
- NautilusTrader Strategy 接口：https://nautilustrader.io/docs/latest/api_reference/trading/#strategy
- Hyperliquid info `candleSnapshot` 端点：https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint
- Binance USDT-M Futures klines REST：https://binance-docs.github.io/apidocs/futures/en/#kline-candlestick-data
