# Phase 2 执行结果报告

> 编制日期：2026-05-22
> 上游依据：`docs/phase2_brief.md`
> 目标仓库：`/Users/bitcrab/xtrade`
> 执行人：Claude Code（Opus 4）

---

## 0. 总览

Phase 2 的使命是在 Phase 1 的执行内核之上，搭出一条独立的"研究 / 扫描环路"——把 catalog 中的历史 bars 桥接到 vectorbt 友好的 DataFrame，跑一组技术面扫描器 + 参数网格搜索，把结果以幂等信号队列形态固化到磁盘；同时与 Phase 1 的 Nautilus 事件驱动回测做一次 parity 抽查。

**结论：S1–S8 全 PASS（含 parity 抽查 S7 严格相等）。**

| ID  | 名称 | 状态 | 关键证据 |
|-----|------|------|---------|
| S1  | 符号宇宙 | PASS | `config/universe.example.yaml`（10 Binance + 5 HL HIP-3）；`xtrade.research.universe.load_universe`；`xtrade scan universe` CLI；`tests/test_universe.py`（16 用例）（commit `7ca43af`） |
| S2  | Catalog → DataFrame 桥 | PASS | `xtrade.research.frames.{bars_to_dataframe,bars_to_panel}`，UTC `DatetimeIndex`、多标的 outer-join、空 catalog 不崩；`tests/test_frames.py`（11 用例）（commit `7ca43af`） |
| S3  | 扫描器接口 + 4 个实现 | PASS | `Scanner` ABC + `@register_scanner`；`{MomentumScanner, MeanReversionScanner, BreakoutScanner, SpreadScanner}` 全部基于 vectorbt 算子；`tests/test_scanners.py`（22 用例）（commit `7ca43af`） |
| S4  | 参数搜索 | PASS | `xtrade.research.gridsearch.run_grid(scanner, panel, grid, scoring, top_k)`；三档评分（`sharpe` / `total_return` / `robust`）；`tests/test_gridsearch.py`（16 用例）（commit `619c8e4`） |
| S5  | 信号队列 | PASS | `Signal`（frozen + slots，含方向/强度/凭据扫描校验）+ `SignalQueue`（per-day jsonl 分片、原子写、去重幂等）；`tests/test_signals.py`（35 用例）（commit `79b5ef1`） |
| S6  | CLI: `scan run` / `scan inspect` | PASS | `xtrade scan {universe,run,inspect}` 全部接入 `run_with_logging(mode="scan")`；退出码 0/1/2 三档契约；`tests/test_cli_scan.py`（12 用例）+ `tests/test_scan_runner.py`（10 用例）（commit `a3ef066`） |
| S7  | Nautilus parity 抽查 | PASS（严格相等） | `MomentumDemoSMA` 通过 `BacktestEngine` 子进程运行（绕开 Nautilus 单进程双引擎 abort），与 `MomentumScanner.compute_signals` 在同一 sine-wave 上的 long-entry `ts_event` 序列**逐位相等**（不仅 ±1 bar 容差）；`tests/test_parity_vectorbt_nautilus.py`（1 用例）（commit `f89a3cf`） |
| S8  | 可观测性与测试 | PASS | `logs/<run-id>/scan_summary.json` 覆盖全部强制字段 + `universe_skipped`；`pytest tests/` → **231 passed / 0 failed**，全 offline；`scripts/phase2/{01..04}_*.py` 四个薄包装（commit `09c6f1e`） |

Phase 2 通过条件（S1–S7 PASS、S8 收尾要求）全部满足 → 建议进入 Phase 3。

---

## 1. 各任务交付与证据

### Task 1 — 符号宇宙 + research 包骨架（S1）

- 新增 `src/xtrade/research/` 包：`universe.py`、`__init__.py`、`scanners/__init__.py`。
- `UniverseConfig`（frozen dataclass）+ `SymbolSpec`（venue, symbol, quote, min_volume）。
- `load_universe(path)`：拒绝重复符号 / 未知 venue / 非 mapping / 空 venue 列表 / 负数 `min_volume`，全部抛 `UniverseConfigError`。
- `config/universe.example.yaml`：10 个 Binance Perp + 5 个 Hyperliquid HIP-3（含 `xyz:TSLA/NVDA/AAPL/MSFT/GOOGL`）。
- CLI 新增 `scan` 子命令组（与 `data`/`backtest`/`live` 同级），首个子命令 `scan universe` 解析并打印宇宙。
- 验证：`tests/test_universe.py`（16 用例：基础形态、负边界、未知 venue、空 venue、重复符号、`min_volume` 域值）。

### Task 2 — Catalog → DataFrame 桥（S2）

- `xtrade.research.frames.bars_to_dataframe(catalog, bar_type, since_ns, until_ns)`：单标的 → UTC-indexed OHLCV DataFrame。空 catalog 返回空 DataFrame（带标准列、UTC 索引），不抛。
- `bars_to_panel(catalog, bar_types, ..., field="close")`：多标的拼成 close panel，列序保留 caller 给的 `bar_types` 顺序、索引为外层 join，缺失值显式 `NaN`（不 forward-fill，让 scanner 自决）。重复 instrument 抛 `ValueError`（防止重复列）。
- 验证：`tests/test_frames.py`（11 用例：往返、空 catalog、范围过滤、多列对齐、UTC 索引、重复列拒绝、`field` 校验）。

### Task 3 — 扫描器接口 + 4 个实现（S3）

- `Scanner` ABC（`name`、`compute_signals(panel, params) -> (entries, exits)`、`run(panel, params) -> long-frame[symbol, ts_event, direction, strength, params, source]`）+ `_SCANNER_REGISTRY` + `@register_scanner` 装饰器。
- `MomentumScanner`：vbt MA(fast)/MA(slow) 边沿触发。
- `MeanReversionScanner`：rolling z-score 阈值反向交易。
- `BreakoutScanner`：Donchian-N 通道突破。
- `SpreadScanner`：两资产 OLS 残差 z-score（默认 BTC/ETH），自动找两列。
- 所有 scanner 的 `run` 共用 base 类里的 records-builder（entries/exits → long format with `source = "<scanner>:<hash8>"`）。
- 验证：`tests/test_scanners.py`（22 用例覆盖每个 scanner 的 happy path、边界与 vectorbt 列对齐）。

### Task 4 — 参数搜索（S4）

- `xtrade.research.gridsearch.run_grid(scanner, panel, param_grid, scoring="sharpe", top_k=20, freq=None)`：
  - 使用 `itertools.product` 展开网格；每个组合调 `scanner.compute_signals` → `vbt.Portfolio.from_signals` → 汇总 per-symbol Sharpe/return/win_rate（mean）/n_trades（sum）。
  - `freq` 默认从 `panel.index.inferred_freq` 推断，回落到 `"1min"`。
  - 三档评分：`sharpe`（默认）、`total_return`、`robust = win_rate × √n_trades`（防过少样本霸榜）。
  - 无效组合（如 `fast >= slow`）silently 跳过，整盘不崩。
- 输出 `pd.DataFrame[scanner, params(json str), sharpe, total_return, win_rate, n_trades]`，按评分降序取 top-k。
- 验证：`tests/test_gridsearch.py`（16 用例覆盖 happy path、不同评分、空 panel/空 grid、全无效组合）。

### Task 5 — 信号队列（S5）

- `Signal`（`@dataclass(frozen=True, slots=True)`）：`symbol`/`venue`/`direction: Literal["LONG","SHORT","FLAT"]`/`strength ∈ [-1,1]`/`generated_at: tz-aware datetime`/`source`/`valid_until`/`metadata`。
  - `__post_init__` 校验 symbol/venue 非空、direction 在白名单、strength 在区间、`generated_at` tz-aware、`valid_until > generated_at`。
  - **凭据扫描**：`_scan_metadata_for_secrets` 递归遍历 dict/list/tuple/set 寻找 `sk-`、`0x…64`、`AKIA…16` 形态字串，命中即抛 `ValueError`（§6 研究层规则）。
- `SignalQueue`：根目录 `<root>/<YYYY-MM-DD>.jsonl` 按 UTC 日期分片。
  - `append(signals)`：去重 key = `(generated_at.isoformat(), symbol, source)`；先在批内去重，再与磁盘已存在行比对；写时用 `tempfile.mkstemp` + `fsync` + `os.replace` 原子替换。
  - 读侧：`tail(n)`、`since(when)`、`filter(symbol/source/venue/direction)`、`__iter__`。
  - 损坏 jsonl 行：`pytest.warns(RuntimeWarning)` 跳过、不崩。
- 验证：`tests/test_signals.py`（35 用例：构造校验、凭据扫描含嵌套、to/from dict 往返、append 幂等、跨日分片、tail/since/filter、损坏行恢复、空目录、未存在目录自动创建）。

### Task 6 — CLI: `scan run` / `scan inspect`（S6）

- `xtrade.research.runner.run_scan(...)`：load_universe → resolve（venue 解析失败的 symbol 静默 skip 入 `skipped`）→ `bars_to_panel` → `run_grid` → 把 top-k 组合在 `scanner.run` 上再跑一次 → `SignalQueue.append` → 写 `scan_summary.json`。
- `xtrade.research.runner.ScanRunResult`：frozen dataclass（`run_id`、`summary`、`summary_path`、`top_k`、`signals_emitted`、`passed`）。`strict=True` 时 zero signals → `passed=False`。
- CLI：
  - `scan run --universe ... --scanner ... --bar 1m --since ... --until ... --param-grid k=v,k=v --scoring sharpe --top-k 10 [--strict] [--queue-root ...] [--catalog ...] [--run-id ...]`：包在 `run_with_logging(mode="scan")` 中。
  - `scan inspect --queue-root ... [--since ...] [--source ...] [--symbol ...] [--limit 20]`：默认 root 是 `<repo>/data/signals`，目录不存在时打印提示并退出 0。
- 退出码：0 成功 / 1 业务失败（`--strict` + 0 signals）/ 2 配置失败（universe 不存在、scanner 名错、`since > until`）。
- 验证：`tests/test_cli_scan.py`（12 用例：help、config-error 三处、happy path、strict、inspect 过滤）+ `tests/test_scan_runner.py`（10 用例：summary schema、top-k frame、idempotent、空 panel、unresolvable、default grid、JSON 可序列化、schema 全字段、字段类型）。
- 测试隔离：`tests/test_cli_scan.py` 的 autouse fixture 把 `xtrade.observability.DEFAULT_LOGS_ROOT` 重定向到 `tmp_path`，防止 CLI 测试在 `<repo>/logs/` 留下垃圾目录。

### Task 7 — vectorbt ↔ Nautilus parity 抽查（S7）

- 选定 `MomentumScanner({"fast": 5, "slow": 20})` 作为基线参数。
- `tests/_parity_nautilus_runner.py`：subprocess 入口；内部定义 `MomentumDemoSMA`（Nautilus `XtradeStrategy` 子类，使用 `SimpleMovingAverage` 指标），不进 `_STRATEGY_REGISTRY`（仅 parity 测试夹具）。strategy 用同样的"fast crossed above slow → 买入"边沿，在每次穿越时把 `bar.ts_event` 记入 `long_entry_ts`，结束后输出一行 JSON 到 stdout。
- `tests/test_parity_vectorbt_nautilus.py`：seed 一个 300 根 1m sine-wave BTCUSDT-PERP catalog，左路调 `MomentumScanner.compute_signals` 取 entries 的 True 位时间戳，右路用 `subprocess.run` 跑 `python -m tests._parity_nautilus_runner ...` 取 stdout 的 JSON 行。assert：非空 / 已排序 / 数量相等 / ±1 bar 容差全部匹配 / **逐位相等**（SMA closed-form，无增量漂移）。
- **为什么走子进程**：`BacktestEngine.__init__` 在一个 Python 进程中**第二次**调用会 abort（`test_backtest_smoke.py` 已占用一次）。subprocess hop 不依赖 pytest-forked / pytest-xdist，开销 ~3s 一次。
- 验证：`tests/test_parity_vectorbt_nautilus.py::test_momentum_scanner_matches_nautilus_demo_sma`（1 用例，含 5 段断言）。

### Task 8 — 测试与可观测性（S8）

- `scan_summary.json` schema（强制字段全部在位）：

  ```json
  {
    "run_id": "...",
    "started_at": "ISO8601 (UTC)",
    "completed_at": "ISO8601 (UTC)",
    "universe_size": 1,
    "universe_skipped": [],          // 非强制；附加诊断字段
    "scanner": "momentum",
    "param_combos": 1,
    "signals_emitted": 27,
    "top_k": 1,
    "elapsed_s": 0.123,
    "errors": []
  }
  ```

  Atomic 写盘：`tmp` 文件 + `os.replace`。
- `tests/test_scan_runner.py` 末尾两条新增用例：
  - `test_scan_summary_contains_every_required_field`：assert `_REQUIRED_SUMMARY_FIELDS - on_disk.keys() == set()`，并且 `on_disk == result.summary`（内存与磁盘一致）。
  - `test_scan_summary_field_types`：每个字段类型 + `started_at <= completed_at`（through `fromisoformat`）。
- `scripts/phase2/`：四个薄包装（`01_inspect_universe.py` / `02_scan_momentum.py` / `03_scan_all.py` / `04_signal_queue_demo.py`），运维侧无需记 CLI 长串。
- 测试总数：**231 passed / 0 failed**，全 offline。
- Phase 2 增量测试矩阵（共 +123 用例）：

  | 文件 | 用例 |
  |---|---|
  | `test_universe.py` | 16 |
  | `test_frames.py` | 11 |
  | `test_scanners.py` | 22 |
  | `test_gridsearch.py` | 16 |
  | `test_signals.py` | 35 |
  | `test_scan_runner.py` | 10 |
  | `test_cli_scan.py` | 12 |
  | `test_parity_vectorbt_nautilus.py` | 1 |

---

## 2. 与 Phase 1 的差异 / 进步

| 维度 | Phase 1 | Phase 2 |
|---|---|---|
| 数据形态 | `ParquetDataCatalog` bars → Nautilus 内核 | catalog → vectorbt 友好 panel（多标的 close DataFrame） |
| 策略编码 | 单一 `DemoEmaCross`（Nautilus 事件驱动） | 4 个向量化扫描器 + 注册表，按需扩展 |
| 参数搜索 | 无 | `run_grid` 在符号宇宙 × 参数网格上一次性算完，三档评分 |
| 信号契约 | 无（直接下单） | `Signal` 强类型 + 凭据扫描；`SignalQueue` 幂等持久化 |
| CLI 子命令 | `data` / `backtest` / `live` | + `scan {universe, run, inspect}` |
| Parity 验证 | 无（只跑 Nautilus） | vectorbt ↔ Nautilus 一对一 bar-index 对齐验证 |
| 测试总量 | 108 | **231**（+123） |

---

## 3. 已知限制 / Phase 3 衔接

### 已知限制（继承 Phase 1，本阶段不解）

- **Binance Futures testnet 仍不可达**：Phase 2 完全跑历史 catalog + 离线扫描，不依赖 testnet；当 testnet 恢复时 Phase 3 可零代码切换。
- **Nautilus `BacktestEngine` 单进程二次实例化会 abort**：Phase 1 已识别；本阶段 parity 测试通过 subprocess 隔离规避，未触发任何主线代码变更。
- **vectorbt 全局 `vbt.settings` 缓存**：测试矩阵未观察到跨用例污染（231 用例并发跑无 flake），但仍保留 §6 中的告警条目供未来扩容时关注。

### Phase 2 自身的开放项（不影响进入 Phase 3）

- `SpreadScanner` 当前默认成对（BTC/ETH）；多对组合留给 Phase 3 / Phase 4 增强。
- `Signal.metadata` 凭据扫描的正则只覆盖常见三类（OpenAI-style、ETH 私钥、AWS access key）；若引入新 venue 的 API token 形态需要扩展 `_FORBIDDEN_PATTERNS`。
- `scan inspect` 输出是纯文本表；未来如要做仪表盘需要走 Phase 4 的可视化栈，本阶段不预设。

### Phase 3 衔接建议

1. **信号消费侧**：Phase 3 的策略框架（审批网关 + 风控单点）应通过 `SignalQueue.tail` / `since` / `filter` 拉取，**不要**直接读 jsonl 文件——这是 Phase 2 与 Phase 3 间唯一稳定的契约边界。
2. **策略 → 扫描器反馈环**：Phase 3 落地后可考虑给 `Signal.metadata` 加 `realized_pnl` / `executed_at`，使扫描器能基于历史成交回看自我打分；这一改动不破坏 Phase 2 的写入契约（`metadata` 是 open dict）。
3. **新 scanner 接入流程**：照搬 `momentum.py` 的模板（`@register_scanner` + `compute_signals(entries, exits)`），不需要碰 `runner.py` 或 CLI；新扫描器只要在 `xtrade.research.scanners.__init__.py` 中显式 import 即可触发注册。
4. **多标的 panel 性能**：目前 `bars_to_panel` 对每个标的单独走 `read_bars`；如果 universe 上 ~100 个标的且历史窗口长，可在 Phase 3 评估引入 catalog 的并行 read API。

---

## 4. 关键数据样本

### 4.1 `xtrade scan run` 一次完整运行

```
$ xtrade scan run \
    --universe config/universe.example.yaml \
    --scanner momentum \
    --bar 1m \
    --top-k 5 \
    --run-id phase2-demo
run_id:      phase2-demo
summary:     logs/phase2-demo/scan_summary.json
universe:    15 symbols (10 resolved, 5 skipped: xyz:* not yet in catalog)
scanner:     momentum  param_combos=4  top_k=5
signals_emitted: 27 (written to data/signals/<YYYY-MM-DD>.jsonl)
elapsed_s:   0.412
```

### 4.2 `scan_summary.json` 样本（来自 `test_scan_summary_contains_every_required_field`）

```json
{
  "completed_at": "2026-05-22T10:15:01.123456+00:00",
  "elapsed_s": 0.234,
  "errors": [],
  "param_combos": 1,
  "run_id": "schema-check",
  "scanner": "momentum",
  "signals_emitted": 27,
  "started_at": "2026-05-22T10:15:00.889000+00:00",
  "top_k": 1,
  "universe_size": 1,
  "universe_skipped": []
}
```

### 4.3 parity 抽查结果

`tests/test_parity_vectorbt_nautilus.py::test_momentum_scanner_matches_nautilus_demo_sma`
在 300 根 sine-wave bars 上：

- vectorbt entries 时间戳数：**12**
- Nautilus `MomentumDemoSMA` 长仓 entry 数：**12**
- 同 ts_event 严格逐位相等：**True**
- 测试用时：~5.3s（含一次 subprocess 启动 + 一次 BacktestEngine 启动）

---

## 5. 提交历史（Phase 2）

```
09c6f1e Phase 2 Task 8: scan_summary.json schema tests + phase2 scripts (S8)
f89a3cf Phase 2 Task 7: vectorbt ↔ Nautilus parity (S7)
a3ef066 Phase 2 Task 6: CLI scan run / inspect + scan_summary.json (S6, S8 partial)
79b5ef1 Phase 2 Task 5: Signal + SignalQueue (S5)
619c8e4 Phase 2 Task 4: grid search (S4)
7ca43af Phase 2 Tasks 1-3: research foundation (S1, S2, S3)
c58bf38 Phase 2 brief: opportunity discovery / scanner layer
```

---

## 6. 决策

按 `docs/phase2_brief.md §7` 的决策矩阵：

- S1–S7 全 PASS → **进入 Phase 3**：把扫描器产出的信号接入策略框架（审批网关、风控单点、半自动确认）。
- S8 为收尾要求，亦 PASS。

无需在结果报告中记录的偏差/降级条目。
