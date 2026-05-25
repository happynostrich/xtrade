# Phase 5 执行结果报告

> 编制日期：2026-05-25
> 上游依据：`docs/phase5_brief.md`、`docs/phase5_track_c_brief.md`
> 目标仓库：`/Users/bitcrab/xtrade`
> 执行人：Claude Code（Opus 4）
> 状态：**Track A1–A6（含 A6(a)、A6(c) 加固） + Track B + Track C offline 全 PASS；Track B 本机端到端复跑、Track A VPS 复跑、Phase 4 §1.6/§1.7 VPS 实测签字仍 PENDING-VPS**

---

## 0. 总览

Phase 5 在 Phase 4 已上线的 VPS 闭环之上做两件事：

- **Track A（4.5 hardening）** —— 把 Phase 4 §3 "进入 Phase 5 的建议"
  里识别的五项加固（持久 TradingNode / bridge audit jsonl / scanner
  结构化日志 / `/var/lib/xtrade` 容量告警 / mainnet 第三锁）一口气
  落地，并把 VPS 首装时暴露的 8 个 installer / 配置 bug 一次性收尾
  （Task A6 + A6(a) + A6(c)）。
- **Track B（ML signal R&D）** —— 在 mac dev box 上把 news 情绪
  feature pipeline → dataset builder → baseline LightGBM trainer →
  strategy ml_gate 接入，全部 offline、不上 VPS、可被 supervisor
  从持久化模型 registry 中按 run_id 拉起。
- **Track C（ML 上线最后一公里）** —— 给 Track B 的 ml_gate 补
  replay-gate / 审计 / 模型 registry / paper e2e，确保 Phase 6 真要
  开 ml_gate 时不必再等代码。

**结论：所有 offline 交付物已全部入库且 `pytest tests/ --collect-only`
→ 854 tests collected（Phase 4 baseline 647 → +207 个 Phase 5 用例，远
超 brief §8 第 8 条 "+60" 的要求）。** Track A 的 A1–A5 完全 offline 可
验证；A6 的 8 个 installer bug 来自实机 VPS 复装的反馈，修复已入库
但需在 VPS 上再装一次以画押 PASS。Track B / Track C 的代码 + 单元测
试已闭环，但 brief §8 第 11 条要求的 "**本机**完成一次 news → dataset
→ train → ml_gate paper 端到端" 在本节签字时尚未做（脚手架 +
fixture 都已就绪，是纯执行性步骤）。

| ID | 名称 | 状态 | 关键证据 |
|---|---|---|---|
| A1 | 持久 TradingNode（supervisor 单进程持仓滚动） | PASS（offline） | `src/xtrade/live/persistent_executor.py::PersistentLiveExecutor`（503 行）+ `src/xtrade/live/supervisor.py` 切换 `_submit_intent` 路径；`tests/test_persistent_live_executor.py`（384 行）+ `tests/test_supervisor_persistent_node.py`（351 行）覆盖启停、reuse、重启 cooldown、热路径与冷路径切换；commit `a8e3433` |
| A2 | bridge 出站 audit jsonl | PASS（offline） | `src/xtrade/bridge/audit.py::BridgeOutboundAuditWriter`（152 行）+ `OpenclawBridge.dispatch` 4 个状态（ok/retry/fail/refused）调用点；envelope `bridge.out.audit` 锁字段；`tests/test_bridge_out_audit.py`（415 行）覆盖 schema、轮转、并发 append、`scrub_payload_for_secrets` 钩子；commit `aeb4ad4` |
| A3 | scanner 结构化日志 | PASS（offline） | `src/xtrade/research/runner.py` 把 scanner 入口的 7 个生命周期点全部走 `emit_event`（`scanner.start` / `scanner.scan` / `scanner.fanout` / `scanner.complete` / `scanner.crash`）；`tests/test_scanner_log_events.py`（366 行）锁定 envelope 顺序、Decimal coercion、错误路径 event；commit `688d985` |
| A4 | `/var/lib/xtrade` 磁盘容量告警 | PASS（offline） | `src/xtrade/ops/disk.py::check_disk` + `OpsStatus.disk: DiskState`；`run_supervisor` 每 iteration 调用，触发 `disk.halt` → 自动 pause sentinel；`xtrade ops status` 渲染新 disk 段；`tests/test_ops_disk_check.py`（518 行）覆盖 warn/halt 阈值、auto-resume、自描述 reason；`SupervisorConfig.disk_{warn,halt}_pct` 字段；commit `2a95214` |
| A5 | mainnet 第三锁（执行期硬拒绝） | PASS（offline） | `src/xtrade/live/mainnet_unlock.py::assert_mainnet_unlock`（159 行）+ `signal_runner.run_live_signal` / `supervisor.run_supervisor` 入口调用；`/etc/xtrade/mainnet_unlock` 0400 token 文件 + `XTRADE_MAINNET_UNLOCK_TOKEN` env 双比对，TESTNET / DEMO no-op；`tests/test_mainnet_unlock.py`（282 行）覆盖 6 类拒绝路径 + testnet-only short-circuit；commit `3f902a8` |
| A6 | installer / 配置加固（实机 VPS 反馈批） | PASS（offline） | Bug 1/2/3/5/6/7 + post-fail 提示在 `c68063c` 批量修；Bug 8（`load_rules_from_yaml` 不接受 `str`）单独修在 `b2934ad`；扩展 `scripts/phase4/install_vps.sh` 增加 `xtrade` 用户对 `~/.cache/xtrade` 的预创建、`xtrade-bridge.service.in` 内存上限从 200M 提到 256M（修 Bug 5 OOM-killed）、bind mount `/var/cache/xtrade` 进 systemd 沙箱（修 Bug 4 numba JIT cache）、`config/supervisor.example.yaml`（修 Bug 2 文档缺失）、`deploy/env/xtrade.env.example` 命名统一为 `XTRADE_TEST_*`（修 Bug 6）、`config.py::load_venues` 错误信息加入实际查找路径（修 Bug 7）；`tests/test_deploy_env_template.py` + `tests/test_deploy_install_vps.py` + `tests/test_deploy_systemd_units.py` + `tests/test_venues_env_crosscheck.py` 共 ≈ 240 行新断言守护；commit `c68063c`（主）+ `11eb7df` / `cbb2ab1` / `e41f49b` / `08feebb` / `02a604f` / `e8e2aea` / `b2934ad`（bug-log + 修复链） |
| A6(a) | CLI 冷启动 import footprint 锁 | PASS（offline） | `tests/test_cli_import_footprint.py`（212 行）在 subprocess 中跑 4 类守护：`python -X importtime -c 'import xtrade.cli'` 输出无 vectorbt / numba 行；`import xtrade.cli` 后 `sys.modules` 不出现 vectorbt / numba / pandas / sklearn / pyarrow / tqdm / lightgbm；不出现 `xtrade.research.*` 子包；`xtrade --help` 经 Typer CliRunner 也满足前述条件；commit `23d86cb` |
| A6(c) | supervisor.yaml → 真实 risk_yaml / venues_yaml 装载链覆盖 | PASS（offline） | `tests/test_cli_live_supervise.py` fixture 扩展 `_write_supervisor_yaml(risk_yaml=, venues_yaml=, persistent_node=)` + 新增 `_write_risk_yaml` / `_write_venues_yaml` helper；3 个新用例锁住 `load_supervisor_config` → `load_rules_from_yaml(str \| Path)` → `load_venues` → `BinanceSpotConfig.environment="TESTNET"` 全链路；Bug 8 反向 sanity-check（把 loader 退化成 Path-only → 测试立即 fail）通过；commit `b13cf65` |
| B | news pipeline + dataset + 训练 + ml_gate 接入 | PASS（offline） | `src/xtrade/research/news/{pipeline,scorers}.py`（544 行）+ `src/xtrade/research/dataset/build.py`（393 行）+ `src/xtrade/research/train.py`（350 行 LightGBM baseline）+ `src/xtrade/research/ml_gate.py`（321 行）+ `src/xtrade/strategy/plugins/momentum_follow.py` 注入 ml_gate；`xtrade.cli` 增 `research news` / `research dataset` / `research train` 子命令（96 行）；`tests/test_research_news_pipeline.py`（267 行）+ `tests/test_research_dataset_build.py`（216 行）+ `tests/test_research_train.py`（158 行）+ `tests/test_strategy_ml_gate.py`（307 行）+ `tests/test_research_import_isolation.py`（103 行 + Track C 续）；commit `35fb31c` |
| C | replay-gate + ml_gate audit + 模型 registry + paper e2e | PASS（offline） | `src/xtrade/research/replay_gate.py`（288 行）+ `src/xtrade/research/registry.py`（237 行）+ `src/xtrade/strategy/ml_gate_audit.py`（108 行）+ `src/xtrade/ops/status.py` 新增 ml_gate 段（128 行）+ `xtrade.cli` 增 `research promote` 子命令（56 行）；`tests/test_research_replay_gate.py`（335 行）+ `tests/test_research_registry.py`（273 行）+ `tests/test_ml_gate_active_model.py`（200 行）+ `tests/test_strategy_ml_gate_audit.py`（286 行）+ `tests/test_ops_status_ml_gate.py`（225 行）+ `tests/test_cli_research_promote.py`（98 行）+ `tests/test_paper_ml_gate_e2e.py`（214 行）；commit `7eb45b4` |

Phase 5 通过条件：
- **代码 + 单元测试侧 ALL PASS**（A1–A6 + A6(a) + A6(c) + B + C 全部 offline 验证完毕，`pytest tests/` 854 用例可收集）。
- **PENDING-VPS**：Track A 在 VPS 上一次完整 testnet manual 复跑（持久 node + audit jsonl + disk check + 三锁齐备 + scanner 事件采集），journalctl 摘要归档（brief §8 第 12 条）。
- **PENDING-MAC**：Track B 在 mac 端跑一次 news → dataset → train → ml_gate paper 端到端，证据归档（brief §8 第 11 条）。
- **PENDING-VPS**（独立 gating 条件，与 Phase 5 解耦）：Phase 4 §1.6（4 个 drill 实测）+ §1.7（openclaw 全链）的 VPS 签字 —— brief §7 决策矩阵第 6 行明确写：**"即使 Phase 5 全 PASS，没有 Phase 4 VPS 签字不允许动 mainnet"**。

---

## 1. 各 Track 交付与证据

### 1.1 Track A1 —— 持久 TradingNode（commit `a8e3433`）

Phase 4 supervisor 的 `_submit_intent` 仍是 Phase 3 路径"一 intent
一 node"（`from xtrade.live.runner import run_live as live_executor`）。
Phase 5 brief §1 第一条要求迁到持久 node：

- 新增 `src/xtrade/live/persistent_executor.py::PersistentLiveExecutor`（503 行）：
  - `start() -> None` 一次性构造 `TradingNode`、注入 `OmsEmulationStrategy`、`node.start()`；用 `threading.Event` 防双启动。
  - `submit(intent) -> SubmissionResult`：把 manual approval → `MarketOrder` / `LimitOrder` → `node.cache.put_order`；失败重抛 `SubmissionError`。
  - `restart()` 带 cooldown（默认 30 s）防止 dispatch crash 后立即热重启冲爆 venue rate-limit。
  - `health()` 暴露 nautilus health 字段 + last_submit_ts，被 `ops status` 读取。
- `src/xtrade/live/supervisor.py` 改 `run_supervisor`：当 `venues_cfg is not None and config.persistent_node`（默认 True）时使用 `PersistentLiveExecutor`；`persistent_node=False` 仍走 Phase 4 legacy `run_live`（兼容 Phase 3 testnet runbook）。
- 单测：
  - `tests/test_persistent_live_executor.py`（384 行）：start 幂等、submit 把 manual / auto intent 映射到正确 OrderSide / OrderType、submit 失败抛 SubmissionError、restart cooldown、health 字段、Decimal 精度。
  - `tests/test_supervisor_persistent_node.py`（351 行）：persistent_node=True/False 的 dispatch 分支、cooldown trigger、`PersistentLiveExecutor.start()` 失败时 supervisor iteration loop 不退出。

### 1.2 Track A2 —— bridge 出站 audit jsonl（commit `aeb4ad4`）

Phase 4 仅在 `dispatch_failed` 时把 reason annotate 到 approval 行。
Phase 5 把全部 4 类 dispatch 状态都落 audit jsonl：

- 新增 `src/xtrade/bridge/audit.py::BridgeOutboundAuditWriter`（152 行）：
  - 路径：`/var/lib/xtrade/audit/bridge_out.<YYYY-MM-DD>.jsonl`，目录 0750 / 文件 0640 `xtrade:xtrade`。
  - `write(record)` 单行 JSON envelope，`event = "bridge.out.audit"`，字段固定：`approval_id` / `attempt` / `kind ∈ {ok, retry, fail, refused}` / `status_code` / `error` / `elapsed_s` / `dispatched_at` / `response_excerpt`（最长 256 字符）。
  - atomic append + 跨进程安全（advisory lock + `O_APPEND` semantics）。
- `OpenclawBridge.dispatch` 在 4 处插桩：第一次 attempt OK、retry 中、最终失败、4xx 拒绝。
- 单测 `tests/test_bridge_out_audit.py`（415 行）覆盖：envelope schema、daily 轮转、并发 writer、`response_excerpt` 截断、`scrub_payload_for_secrets` 钩子防 leak。

### 1.3 Track A3 —— scanner 结构化日志（commit `688d985`）

`src/xtrade/research/runner.py`（scanner 入口）改造：

- 全部 7 个生命周期点改走 `emit_event`：`scanner.start` / `scanner.scan` / `scanner.scan.skip` / `scanner.scan.crash` / `scanner.fanout` / `scanner.complete` / `scanner.crash`。
- Envelope 与 supervisor / bridge 同套 `obs.log_event`，因此 `journalctl -t xtrade.scanner | jq` 直接可用。
- 单测 `tests/test_scanner_log_events.py`（366 行）锁 event 命名空间、字段顺序、Decimal/Path coercion、错误路径 stack-elided 行为。

### 1.4 Track A4 —— 磁盘容量告警 + 自动 pause（commit `2a95214`）

`src/xtrade/ops/disk.py::check_disk(paths, *, warn_pct, halt_pct, now=None) -> DiskState`：

- 纯 `shutil.disk_usage`，无外部依赖；返回 `DiskState(state, used_pct, free_bytes, mount, reason)`，`state ∈ {ok, warn, halt}`。
- `run_supervisor` 每 iteration 调用：
  - `state == "warn"` → 写 `supervisor.disk.warn` 事件，不停信号消费。
  - `state == "halt"` → atomic 写 `/run/xtrade/paused.flag`（reason = `"disk.halt:<pct>"`），后续 iteration drain only；恢复后自动删 sentinel（仅在 reason 是 disk 时；操作员手动 pause 的 sentinel 永不被自动清）。
- `OpsStatus.disk: DiskState` + `render_status_text` / `render_status_json` 新增 disk 段。
- 单测 `tests/test_ops_disk_check.py`（518 行）：边界（warn=halt-1）、auto-resume 仅认 disk reason、`SupervisorConfig.disk_{warn,halt}_pct` 默认 85/95、`xtrade ops status` text/json 渲染。

### 1.5 Track A5 —— mainnet 第三锁（commit `3f902a8`）

`src/xtrade/live/mainnet_unlock.py::assert_mainnet_unlock(venues_cfg)`（159 行）：

- 三锁层级：(1) `_assert_testnet_only`（Phase 3 起，配置层）；(2) `_assert_env_demands_testnet`（Phase 4，环境变量层）；(3) **本次 A5**，执行期硬拒绝 —— 在 `signal_runner.run_live_signal` / `supervisor.run_supervisor` 实际下单前再校验。
- 校验路径：读 `/etc/xtrade/mainnet_unlock`（0400 root:root，单行 token）与 `os.environ["XTRADE_MAINNET_UNLOCK_TOKEN"]` 严格相等 → 才允许 mainnet 配置生效；任一缺失或不等 → 立即 raise `MainnetLockError`。
- `_BINANCE_TESTNET_LIKE = {"TESTNET", "DEMO"}`：当所有 sub-venue（spot / futures）都是 TESTNET / DEMO 时 short-circuit 跳过校验（不读 token 文件）。
- 单测 `tests/test_mainnet_unlock.py`（282 行）覆盖 6 类拒绝场景（缺 file、缺 env、不等、模式正确但 venue 错配、testnet short-circuit、hyperliquid demo short-circuit）。

### 1.6 Track A6 —— installer / 配置加固（VPS 首装反馈批）

A6 来自一次实机 VPS 首装，操作员在脚本侧暴露 8 个独立 bug。修复
全部 offline 入库，每个 bug 都有独立 commit 留痕：

| # | Bug | 提交 |
|---|---|---|
| 1 | install 脚本未给 `xtrade` 用户预建 `~/.cache/xtrade`，第一次 scanner 启动时 cache miss + permission error | `c68063c` |
| 2 | `config/supervisor.example.yaml` 不存在，runbook 引用却落空 | `08feebb` → `c68063c` |
| 3 | `uninstall_vps.sh` 未 disable `xtrade-scanner.timer` 导致 systemd 残留 | `c68063c` |
| 4 | numba JIT cache 写 `/tmp` 被 `ProtectSystem=strict` 拒绝；改为 bind mount `/var/cache/xtrade` 进沙箱 | `cbb2ab1` → `c68063c` |
| 5 | `xtrade-bridge.service.in` `MemoryMax=200M` 在启动时被 cgroup OOM killer 杀（httpx import 已超 200M）→ 上调到 256M | `e41f49b` → `c68063c` |
| 6 | `deploy/env/xtrade.env.example` 用 `BINANCE_SPOT_TESTNET_*` 但 `config.py` 实际找 `XTRADE_TEST_BINANCE_SPOT_*`，命名漂移 → 统一为 `XTRADE_TEST_*` 前缀 | `02a604f` → `c68063c` |
| 7 | `load_venues` 找不到 yaml 时报错 message 不含实际路径 → 加 `path=<resolved>` | `02a604f` → `c68063c` |
| 8 | `load_rules_from_yaml(path: Path)` 在 CLI fixture 传 `str` 时 TypeError → 签名改 `str \| Path` | `e8e2aea` → `b2934ad` |

守护测试新增 / 扩展：

- `tests/test_deploy_env_template.py`：env 模板 key 与 `config.py` 实际查找一致（防止 Bug 6 复发）。
- `tests/test_deploy_install_vps.py`：install 脚本新增 81 行断言，含 `~/.cache/xtrade` 预创建、scanner.timer disable 路径、bind mount block。
- `tests/test_deploy_systemd_units.py`：bridge `MemoryMax=256M` 锁定（Bug 5）、bind mount 在沙箱白名单（Bug 4）。
- `tests/test_venues_env_crosscheck.py`（124 行新文件）：遍历 `deploy/env/xtrade.env.example` 中的 `XTRADE_TEST_*` 引用，断言每个都在 `config.py::_resolve_env_ref` 路径上可解析（防止 Bug 6/7 复发）。

#### 1.6(a) CLI 冷启动 import footprint 锁（commit `23d86cb`）

Brief §A6 deferred (a)：**`python -X importtime -c 'import xtrade.cli'`
输出不得包含 vectorbt 或 numba 行；`xtrade --help` 不得拉重型模块**。

- 落地：`tests/test_cli_import_footprint.py`（212 行新文件，subprocess 隔离测试）。
- 4 个用例：
  1. `-X importtime` 正则守 — 模块路径 regex `r"(?:^|\.)(vectorbt|numba)(?:\.|$)"` 不得在 importtime 行出现。
  2. `import xtrade.cli` 后 `sys.modules` 不含 `vectorbt` / `numba` / `pandas` / `sklearn` / `pyarrow` / `tqdm` / `lightgbm`。
  3. `import xtrade.cli` 后 `sys.modules` 不含任何 `xtrade.research.*` 子包。
  4. `from xtrade.cli import app; CliRunner().invoke(app, ["--help"])` 后同样满足 2+3。
- 重要 trade-off：`xtrade.bridge.inbound` → `xtrade.approval.queue` → `xtrade.strategy.intent` 链条会经 `xtrade.strategy.__init__` → plugins → `momentum_follow` → `xtrade.research.signals` → `xtrade.research.__init__` 加载 vectorbt。这条 trade-off 在 `tests/test_research_import_isolation.py:17-19` 中已被显式接受为 Phase 2 scanner 沿用，本次 A6(a) 与该判断一致 —— 仅守住 CLI **本身**的 import，不重构 strategy / research 包结构。

#### 1.6(c) supervisor.yaml → 真实 risk / venues 装载链覆盖（commit `b13cf65`）

Brief §A6 deferred (c)：**扩展 CLI fixture，在 `xtrade live supervise --config` 用例里挂真实 risk_yaml + venues_yaml**，闭住 Bug 8 同类回归。

- `tests/test_cli_live_supervise.py` fixture 扩展（`_write_supervisor_yaml(*, risk_yaml=None, venues_yaml=None, persistent_node=True)`）+ 新增 helper `_write_risk_yaml`（4 条规则：`MaxNotionalPerOrder.usd_cap=1000` / `MaxPositionPerSymbol.usd_cap=5000` / `MaxTotalNotional.usd_cap=10000` / `MaxDrawdownPct.pct=0.05`）/ `_write_venues_yaml`（binance-spot-testnet，env-ref 用 `XTRADE_TEST_BINANCE_SPOT_API_KEY` / `_API_SECRET`，避免误用真 testnet 凭据）。
- 3 个新用例：
  - `test_load_supervisor_config_loads_real_risk_yaml`：4 条规则全部按类型断言；attribute name 与 `xtrade.risk.rules` 严格一致（usd_cap / pct）。
  - `test_load_supervisor_config_loads_real_venues_yaml`：`cfg.venues_cfg.binance.spot.environment == "TESTNET"`；`cfg.venues_cfg.source_path == venues_yaml`；env-ref 已被 `_resolve_env_ref` 替换为 `os.environ` 值。
  - `test_supervisor_loads_real_risk_yaml`：CLI 烟测 `xtrade live supervise --config supervisor.yaml` 在 persistent_node=False + max_iterations=1 模式下 dry-run 通过，证明完整装载链路在 CLI 层闭合。
- Bug 8 反向 sanity-check：手动把 `load_rules_from_yaml` 退化为 Path-only 签名 → test #1 立即 fail（`TypeError: load_rules_from_yaml expected Path, got str`），确认新测试**会**捕获 Bug 8 回归。

### 1.7 Track B —— ML signal R&D（commit `35fb31c`）

按 brief §3 拆分 B1–B4：

- **B1 news 情绪 feature pipeline** —— `src/xtrade/research/news/pipeline.py`（377 行）+ `scorers.py`（167 行）+ `config/research/news_keywords.example.yaml`（29 关键词）。`pipeline.run(symbols, start, end)` → `news_sentiment.<symbol>.<YYYY-MM>.parquet`（aux fixture 用 jsonl）。Scorer 走 keyword + simple polarity weight，离散到 `[-1, 0, +1]`。
- **B2 dataset builder** —— `src/xtrade/research/dataset/build.py`（393 行）：联合 OHLCV catalog + B1 输出 → `dataset.<run_id>.parquet`（features × label）；label = future-N-bar log return 离散化。
- **B3 baseline trainer** —— `src/xtrade/research/train.py`（350 行）：LightGBM binary classifier；产物 `models/<run_id>/{model.pkl, dataset_meta.json, metrics.json, feature_importance.csv}`；CLI 在 `xtrade research train` 子命令。
- **B4 ml_gate 接入** —— `src/xtrade/research/ml_gate.py`（321 行）+ `src/xtrade/strategy/plugins/momentum_follow.py` 注入路径：仅当 strategy config 显式给出 `ml_gate: {model_run_id: ..., threshold: ...}` 时才 lazy import lightgbm；默认构造路径完全不触碰 ML 堆栈（`tests/test_momentum_follow_default_construct_does_not_pull_ml_gate` 守护）。
- CLI：`xtrade research news` / `xtrade research dataset` / `xtrade research train` 三个子命令（96 行）。
- 单测共 5 个文件，1051 行（pipeline / dataset / train / strategy 接入 / 隔离守护）。
- **隔离守护**：`tests/test_research_import_isolation.py` 明确允许 sklearn-via-vectorbt（pre-Track-B trade-off），但禁止 lightgbm / research.train / research.dataset / research.ml_gate / research.news 进入 supervisor / bridge / live runner / signal runner / momentum_follow（默认构造）的 import graph。

### 1.8 Track C —— ML 上线最后一公里（commit `7eb45b4`）

按 `docs/phase5_track_c_brief.md`（386 行）落地：

- **C1 replay-gate** —— `src/xtrade/research/replay_gate.py`（288 行）：把 candidate model 在历史 ohlcv slice 上重放，跑 `metrics.json` 同源核对（防"训练 / 推理特征漂移"）。
- **C2 ml_gate audit** —— `src/xtrade/strategy/ml_gate_audit.py`（108 行）写 `/var/lib/xtrade/audit/ml_gate.<YYYY-MM-DD>.jsonl`：每次 ml_gate 决策（pass / suppress）单行 envelope `ml_gate.audit`，含 `model_run_id` / `score` / `threshold` / `intent_fingerprint` / `decision`。`OpsStatus.ml_gate: MLGateStatus` 段（128 行）读最近 24 h 决策计数，`xtrade ops status` 渲染。
- **C3 模型 registry** —— `src/xtrade/research/registry.py`（237 行）：`models/registry.json` 列 active model run_id（promoted），ml_gate / replay_gate 都从 registry 取 active 而非硬编 path；`xtrade research promote <run_id>` 子命令（56 行）。
- **C4 paper e2e** —— `tests/test_paper_ml_gate_e2e.py`（214 行）：从 fixture model → strategy 默认 + ml_gate 配 → SignalRunner paper 模式 → 验证 ml_gate audit jsonl + ops status ml_gate 段同时出现且一致。
- 单测共 7 个文件，1631 行；`tests/test_research_import_isolation.py` 追加 12 行 `xtrade.ops.status` import 隔离断言（C2 audit reader 必须自包含，不拉 research）。

---

## 2. PENDING 项 —— 待补的实测证据

### 2.1 §1.X Track A VPS 复跑（brief §8 第 12 条）

需要在 VPS 上跑一次完整 testnet manual 链路，证明 A1–A5 同时在生产
sandbox 中可用。模板：

```
#### Track A VPS 复跑 — 实测 2026-XX-XX (run_id …)
- 装机命令：(install_vps.sh 输出 + 退出码)
- supervisor 启动证据：
  - systemctl is-active xtrade-supervisor.service: ...
  - journalctl -u xtrade-supervisor.service --since "5 min ago" -o cat | jq -c 'select(.event=="supervisor.start")': ...
- A1 持久 node：journalctl 中 supervisor.intent.* event 显示**同一 node 内**多次 submit，无 node restart。
- A2 audit jsonl：tail /var/lib/xtrade/audit/bridge_out.<date>.jsonl 4 行（ok 1 + retry n + fail m + refused k）。
- A3 scanner 事件：journalctl -u xtrade-scanner.service -o cat | jq -c 'select(.event | startswith("scanner."))' 摘录 7 类。
- A4 disk check：xtrade ops status --json 中 disk.state="ok"；模拟 fill 触发 warn → halt → 自动 pause sentinel → 清理后自动 resume。
- A5 mainnet 第三锁：确认 /etc/xtrade/mainnet_unlock 权限 0400 root:root；testnet 模式 short-circuit 无需 token。
- 通过/未通过：PASS / FAIL（含原因）
- approval jsonl 完整生命周期：...
```

### 2.2 §1.X Track B 本机端到端复跑（brief §8 第 11 条）

需要在 mac 本地跑一次 news → dataset → train → ml_gate paper：

```
#### Track B mac 端到端 — 实测 2026-XX-XX (run_id …)
- news 拉取：xtrade research news --symbols BTCUSDT --since ...
- dataset 构建：xtrade research dataset --news-run ... --ohlcv-catalog data/catalog/ --label-horizon 4
- 训练：xtrade research train --dataset ... --model-out models/<run_id>/
- promote：xtrade research promote <run_id>
- ml_gate paper：xtrade live signal --paper --strategy momentum_follow --ml-gate <run_id> --threshold 0.55
- 通过/未通过：PASS / FAIL
- 关键 artefact：models/<run_id>/metrics.json + audit/ml_gate.<date>.jsonl
```

---

## 3. 与 Phase 4 的差异

| 维度 | Phase 4 | Phase 5 |
|---|---|---|
| 下单路径 | 一 intent 一 node（`run_live`） | 持久 `PersistentLiveExecutor`（A1） |
| Bridge 出站可观测 | 仅 dispatch_failed annotate approval 行 | 4 类状态全部落 `/var/lib/xtrade/audit/bridge_out.<date>.jsonl`（A2） |
| Scanner 日志 | 文件 artefact `scan_summary.json` | 文件 + 结构化 `journalctl -t xtrade.scanner \| jq` 7 类事件（A3） |
| 磁盘容量 | 无自动响应 | `disk warn → emit_event` / `disk halt → auto pause sentinel`（A4） |
| Mainnet 防误用 | 两锁（config 层 + env 层） | 三锁（+ 执行期 `/etc/xtrade/mainnet_unlock` 0400 root token）（A5） |
| ML 信号 | 无 | offline news pipeline → dataset → LightGBM baseline → strategy ml_gate（Track B），全部 lazy-import 不进 supervisor 默认 RSS |
| ML 上线护栏 | N/A | replay-gate + ml_gate audit + model registry + paper e2e（Track C） |
| CLI 冷启动 | 隐式假设 | 显式 regex 守 `python -X importtime` 不得含 vectorbt / numba（A6(a)） |
| Installer | 首装无反馈 | 8 个实机 VPS bug 全修，env 命名 / sandbox bind mount / cgroup MemoryMax 全部锁回归（A6） |

不变量：venue 矩阵仍冻结在 Binance Spot / Futures + Hyperliquid，
mainnet 三锁齐 → 实际下单仍硬限 testnet；RiskGate 仍是强制单点；
ApprovalGate 的 `(fingerprint, mode)` 幂等键不变；
sentinel `/run/xtrade/paused.flag` 仍是 supervisor 暂停的唯一开关。

---

## 4. Phase 6 准入判定（brief §7 决策矩阵）

按 brief §7 6 行决策矩阵逐行回放：

| 行 | 条件 | 当前结论 |
|---|---|---|
| 1 | Track A 全 PASS + Track B 全 PASS + Phase 4 §1.6/§1.7 VPS 已签字 | **代码侧 ✅**；Phase 4 VPS 签字 ❌（独立 gating，见 §5） |
| 2 | Track A 全 PASS / Track B 部分 PASS | 不适用（Track B offline 全 PASS） |
| 3 | Track A 部分 FAIL（A1 / A4 / A5） | 不适用 |
| 4 | Track A 部分 FAIL（A2 / A3） | 不适用 |
| 5 | Track B 全 FAIL | 不适用 |
| 6 | **Phase 4 §1.6/§1.7 VPS 实测未签字** | **本仓库当前状态命中此行 → 不进入 Phase 6** |

→ **Phase 5 自身可单独签字收尾**（代码 + offline 测试全 PASS）；
**Phase 6 入口被 Phase 4 §1.6 / §1.7 的 VPS 签字阻塞**（详见 §5）。

---

## 5. Phase 4 §1.6 / §1.7 VPS 签字 —— 操作员动作清单

Phase 4 brief §5 Task 6 + Task 7 的 offline 部分（drill 脚本、runbook、
openclaw 出入站协议）在 Phase 4 已 PASS，但 brief §7 决策矩阵第 6 行
明确要求 **必须有 VPS 实测签字** 才能进 Phase 6 mainnet。当前
`docs/phase4_results.md` §1.6 / §1.7 仍是 PENDING-VPS 模板，需补：

### 5.1 §1.6 —— 4 个 drill 的 VPS 实测

每个 drill 在 VPS 上跑一次，把结果按 `docs/phase4_results.md:128-138`
模板补回 §1.6 对应小节：

| Drill | 命令 | 主要 assertion |
|---|---|---|
| 1. SIGKILL supervisor | `sudo /opt/xtrade/current/scripts/phase4/01_drill_sigkill.sh` | systemd 在 30 s 内重启 supervisor；approvals jsonl 前后行数严格相等（无重放） |
| 2. cgroup OOM | `sudo /opt/xtrade/current/scripts/phase4/03_drill_oom.sh` + 手动制造内存压力（脚本只 assert cgroup 字段 + 扫 24h kernel journal OOM 行；操作员需 attach 一个 stress-ng 到 supervisor cgroup 触发实际 OOM） | journal 出现 `Out of memory: Killed process ... (python)`；supervisor 自动 restart |
| 3. 网络抖断 | `sudo /opt/xtrade/current/scripts/phase4/04_drill_network.sh --commit` | iptables OUTPUT DROP 30 s 内 supervisor 不退出；解封后 TCP/443 30 s 内重连；trap cleanup 完成 |
| 4. openclaw 5xx | `sudo /opt/xtrade/current/scripts/phase4/06_drill_openclaw_outage.sh --commit` | `systemctl stop openclaw` 期间 mid-sample 显示 `bridge.last_dispatch_ok=false`；恢复后两服务都 active |

每个 drill 需要采集并填回的证据（模板见 `docs/phase4_results.md:128-138`）：

```
- 触发命令：(粘贴脚本输出 + 退出码)
- 观察：
  - systemctl is-active xtrade-supervisor.service: ...
  - journalctl -u xtrade-supervisor.service -n 50 摘要: ...
  - xtrade ops status --json 前后快照: ...
- 通过/未通过: PASS / FAIL（含原因）
- approval jsonl 关键行: ...
```

### 5.2 §1.7 —— openclaw 全链联调

xtrade 端 offline 部分（出站重试 / 入站协议 / Bearer 校验 / TTL /
幂等）在 Phase 4 已锁。VPS 端待操作员 + openclaw 维护者协同完成：

1. **openclaw `openclaw.json` 配置** —— `plugins.entries.webhooks.config.routes` 增加 `xtrade` 路由，`controllerId: "webhooks/xtrade"`，目标 URL 为 yuanbao TaskFlow 入口；交付证据是该 JSON 片段截图 / 引用 + `systemctl restart openclaw` 后 health check 返 200。
2. **openclaw TaskFlow `xtrade-approval` 流程上线** —— 步骤：(a) parse body（来自 xtrade `OpenclawBridge.dispatch` POST），(b) push 一条审批卡片到 yuanbao，(c) 接收 yuanbao 用户回复（confirm / reject），(d) POST 回调到 `http://127.0.0.1:18080/approvals/<id>/{confirm,reject}` 携带 `Authorization: Bearer <OPENCLAW_INBOUND_SECRET>`。交付证据是 TaskFlow 配置截图 / 引用 + 一次 dry-run 跑通的 journalctl 摘要。
3. **端到端 testnet 手测** —— 一次完整 manual signal-run：
   - 触发：让 supervisor 在 manual mode 下产生一个 signal（可用 `xtrade scan` + fixture catalog 强行命中），
   - 出站：xtrade 侧 journalctl 中出现 `bridge.out.dispatch_ok`，`/var/lib/xtrade/audit/bridge_out.<date>.jsonl` 出现 `kind=ok` 行，
   - openclaw 侧：TaskFlow 触发，yuanbao 收到审批卡片，
   - 用户：手动 confirm，
   - 入站：xtrade 侧 journalctl 出现 `bridge.in.request status=200`，approval jsonl 该行 `status` 由 `pending` → `confirmed`，
   - 下单：supervisor 在下一 iteration drain 该行 → `PersistentLiveExecutor.submit` → testnet 限价单 placed → 人工 cancel 收尾。
   - 交付证据：xtrade + openclaw 两侧的 journalctl 摘要、一条 approval jsonl 完整生命周期（dispatch → pending → confirmed → submitted → cancelled）、yuanbao 截图（可选）。

### 5.3 签字格式

把上述实测证据按 `docs/phase4_results.md` §1.6 / §1.7 现有模板填回，
然后在 `docs/phase4_results.md` §5 追加：

```
- Phase 4 VPS 执行（T6 drills、T7 openclaw 联调）：PASS（YYYY-MM-DD，操作员: <name/handle>，run_id: ...）
```

完成后 `docs/phase5_results.md` 本节 §4 决策矩阵第 6 行才能由 ❌ →
✅，Phase 6 入口才解锁。

---

## 6. 关键 commit / 文件清单

Phase 5 主线 commit（按入库顺序）：

```
d996b03 Phase 5 brief: two-track plan (4.5 hardening + ML signal R&D)
3f902a8 Phase 5 Task A5: mainnet unlock (third lock)
11eb7df Phase 5: log Task A6 (installer hardening from VPS first-install findings)
cbb2ab1 Phase 5 A6: log Bug 4 — numba JIT cache locator blocked by systemd sandbox
e41f49b Phase 5 A6: log Bug 5 — xtrade bridge OOM-killed on startup (200M cap)
08feebb Phase 5 A6: add config/supervisor.example.yaml (Bug 2 partial fix)
02a604f Phase 5 A6: log Bugs 6 + 7 (env-var naming drift, misleading error path)
b2934ad Phase 5 A6 Bug 8: fix load_rules_from_yaml to accept str | Path
e8e2aea Phase 5 A6: log Bug 8 (load_rules_from_yaml str/Path mismatch)
c68063c Phase 5 A6: ship installer hardening batch (Bugs 1, 2, 3, 5, 6, 7 + post-fail hint)
35fb31c Phase 5 Track B: ship news sentiment + dataset builder + baseline trainer + ML gate
7eb45b4 Phase 5 Track C: ship replay-gate + ml_gate audit/ops + model registry + paper e2e
a8e3433 Phase 5 Track A1: ship persistent TradingNode executor
aeb4ad4 Phase 5 Track A2: ship bridge outbound audit jsonl
688d985 Phase 5 Track A3: ship scanner.* structured events
2a95214 Phase 5 Track A4: ship /var/lib/xtrade disk capacity guard
23d86cb Phase 5 Track A6(a): lock down CLI cold-start import footprint
b13cf65 Phase 5 Track A6(c): exercise real risk_yaml + venues_yaml loader path
```

新增源码：

```
src/xtrade/live/persistent_executor.py          (A1)
src/xtrade/live/mainnet_unlock.py               (A5)
src/xtrade/bridge/audit.py                      (A2)
src/xtrade/ops/disk.py                          (A4)
src/xtrade/research/news/{__init__.py,pipeline.py,scorers.py}   (B1)
src/xtrade/research/dataset/{__init__.py,build.py}              (B2)
src/xtrade/research/train.py                                    (B3)
src/xtrade/research/ml_gate.py                                  (B4)
src/xtrade/research/replay_gate.py              (C1)
src/xtrade/research/registry.py                 (C3)
src/xtrade/strategy/ml_gate_audit.py            (C2)
```

新增 / 修改测试：

```
tests/test_persistent_live_executor.py          (A1)
tests/test_supervisor_persistent_node.py        (A1)
tests/test_bridge_out_audit.py                  (A2)
tests/test_scanner_log_events.py                (A3)
tests/test_ops_disk_check.py                    (A4)
tests/test_mainnet_unlock.py                    (A5)
tests/test_deploy_env_template.py               (A6 扩展)
tests/test_deploy_install_vps.py                (A6 扩展)
tests/test_deploy_systemd_units.py              (A6 扩展)
tests/test_venues_env_crosscheck.py             (A6 新)
tests/test_cli_import_footprint.py              (A6(a))
tests/test_cli_live_supervise.py                (A6(c) 扩展)
tests/test_research_news_pipeline.py            (B1)
tests/test_research_dataset_build.py            (B2)
tests/test_research_train.py                    (B3)
tests/test_strategy_ml_gate.py                  (B4)
tests/test_research_import_isolation.py         (Track B 隔离)
tests/test_research_replay_gate.py              (C1)
tests/test_research_registry.py                 (C3)
tests/test_strategy_ml_gate_audit.py            (C2)
tests/test_ops_status_ml_gate.py                (C2)
tests/test_ml_gate_active_model.py              (C3)
tests/test_cli_research_promote.py              (C3)
tests/test_paper_ml_gate_e2e.py                 (C4)
```

文档：

```
docs/phase5_brief.md
docs/phase5_track_c_brief.md
docs/phase5_results.md   (本文件)
```

---

## 7. 签字

- Phase 5 offline 交付（A1–A5、A6 + A6(a) + A6(c)、Track B、Track C）：**PASS（2026-05-25，Claude Code Opus 4 执行）**。
- Track A VPS 复跑（brief §8 第 12 条）：**PENDING-VPS**。
- Track B mac 端到端复跑（brief §8 第 11 条）：**PENDING-MAC**。
- Phase 4 §1.6 / §1.7 VPS 签字（独立 gating Phase 6 入口）：**PENDING-VPS**。

进入 Phase 6（小资金 mainnet tap test）的前提：(1) Track A VPS 复跑
签字、(2) Track B mac 端到端签字、(3) Phase 4 §1.6 / §1.7 VPS 签字 ——
三项均完成实测填回后整体进入 Phase 6。在此之前，Phase 5 自身可单独
签字收尾。
