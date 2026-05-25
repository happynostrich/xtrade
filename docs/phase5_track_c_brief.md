# Phase 5 Track C 实施简报 —— Paper 验证 + ML-gate Ops 表面

> 编制日期：2026-05-25
> 目标仓库：`/Users/bitcrab/xtrade`
> 上游依据：
> - `docs/phase5_brief.md`（Phase 5 总简报，Track A / Track B）
> - Track A6 SHIPPED 2026-05-25（commit `c68063c`）
> - Track B SHIPPED 2026-05-25（commit `35fb31c`；VPS 验证 release `77616ddaac42`，bridge + supervisor active，lightgbm 未泄漏到 live 路径）
> - 与用户对齐（2026-05-25）：Phase 5 Track C 由 Claude Code 自主拟定范围；用户授权 "go"
>
> 执行方式：本简报交给 **Claude Code** 在 `/Users/bitcrab/xtrade` 中执行；单一 commit 序列；不动 VPS、不动 mainnet。

---

## 0. 进入 Track C 的前提

| 前提项 | 状态 |
|---|---|
| Phase 5 Track A6 全部 source-tree 修复 + 676 passed | ✅（commit `c68063c`） |
| Phase 5 Track B B1–B4 + import isolation 守护 + 720 passed | ✅（commit `35fb31c`） |
| Track B VPS smoke：release `77616ddaac42` active；`lightgbm: absent`、`sklearn: present (via vectorbt)`、`pyarrow: present (via nautilus-trader)`；bridge `:18080` 405/405 | ✅ |
| `docs/phase4_runbook_vps.md` tarball 流程修复 | ✅（commit `fbff64a`） |

---

## 1. Track C 的使命与非使命

### 使命

Track B 把 ML / news pipeline 建到了 "可训练 + 可作为 strategy gate" 的程度，但 **离 paper 链路真的能跑 + ops 端能看到 gate 在做什么 + 模型升级不靠改 yaml** 还差三个洞。Track C 把这三个洞补齐：

- **C1. `xtrade research replay-gate`** —— 离线回放工具
  把已落盘的 signal jsonl（`data/signals/<date>.jsonl` 或 Phase 4 VPS 同步下来的 `/var/lib/xtrade/signals/<date>.jsonl`）按指定时间窗喂给 `MLGate.decide`，输出 `models/<run_id>/replay_<since>_<until>.json` 汇总：
  - `n_signals` / `n_allowed` / `n_suppressed`
  - `by_side`（LONG / SHORT / FLAT 各自的 allow/suppress）
  - `by_symbol`（按 instrument 分桶）
  - `p_long_quantiles`（[p10, p50, p90]，让人看到分数分布而不是单一阈值）
  - `score_threshold` / `direction_check`（参数 echo，便于复审）
  纯离线，不动 strategy 实例，不构造 venue 连接。

- **C2. `xtrade ops status` ML-gate 摘要块**
  现在 `xtrade ops status` 看不到 gate 在线上的工作量。补一个 `ml_gate.*` 摘要块（按 24h 滚动窗口）：
  - `ml_gate.enabled_strategies`（从 supervisor.yaml 解析的策略数）
  - `ml_gate.suppressed_24h`
  - `ml_gate.allowed_24h`
  - `ml_gate.suppression_rate_24h`（百分比，`None` 当样本 < 10）
  - `ml_gate.last_event_age_s`（最近一条 `strategy.ml_gate.*` 事件的年龄）

  **架构调整**（基于探勘）：当前 `xtrade.obs.emit_event` 只写 logger → journalctl，而 `xtrade ops status` 必须保持 "pure-filesystem" 不可调 journalctl。因此 C2 同时引入小型 jsonl audit writer（模式照搬 Track A2 `BridgeAuditWriter`）：
  - 新模块 `xtrade/strategy/ml_gate_audit.py`，提供 `MLGateAuditWriter.write(kind="allowed"|"suppressed", symbol, side, score, threshold, reason, source_signal_id, ts)`
  - 落盘 `/var/lib/xtrade/audit/ml_gate.<YYYY-MM-DD>.jsonl`，0640 xtrade:xtrade，atomic O_APPEND
  - `MomentumFollow._apply_ml_gate` 在 allowed / suppressed 两个分支各调一次 audit.write（与 emit_event 并列；emit_event 路径不变，保留 journalctl 可观测）
  - audit writer 可选注入（默认 `None` → no-op），strategy 构造期 `MLGateAuditWriter.if_enabled(audit_root)` 创建；缺 audit_root 直接 no-op，不阻塞策略
  - `xtrade ops status` 扫 `audit/ml_gate.*.jsonl`（按 mtime 倒序，过滤 24h 内），统计上面 5 字段

- **C3. 模型注册表 + `xtrade research promote`**
  现在 strategy yaml 写死 `ml_gate.model_path: /opt/xtrade/models/<run_id>/model.pkl`。换模型要改 yaml、重启 supervisor、有人工操作误差。Track C 引入：
  - `models/active.json` schema：`{"run_id": "...", "model_path": "models/<run_id>/model.pkl", "active_since": "<ISO UTC>", "promoted_by": "<user>", "previous": {"run_id": ..., "active_since": ...} | null}`
  - `xtrade research promote <run_id>` CLI 命令：
    - 检查 `models/<run_id>/{model.pkl, metrics.json, dataset_meta.json}` 三件套齐全
    - 检查 `metrics.json.auc >= 0.5`（防止误推一个明显坏模型）
    - 原子写 `models/active.json`（write to `.tmp` then `os.replace`）
    - 历史保留在 `models/active.history.jsonl`（append-only）
  - `MLGateConfig` 新增 `use_active_model: bool = False` 开关；为 True 时 strategy 启动期从 `models/active.json` 解析 `model_path`，忽略 yaml 中的硬编码路径；为 False 时保持 Track B 行为（yaml 路径优先）。
  - Promote 不动 supervisor 状态；要换模型仍然要 restart strategy（这是预期，因为 model 是构造期加载的）；但 yaml 不再需要改。

- **C4. Paper 端到端 smoke（opt-in）**
  把 B1→B2→B3→C3→paper 链跑一遍，证明 ml_gate 真的在 paper run 中影响 intent 数量。**opt-in via `XTRADE_RUN_PAPER_E2E=1`**（默认 skip），因为完整链路要 ~30s + 内置 OHLCV CSV，不该拖 PR pipeline；CI 矩阵不开 paper-e2e。
  断言：
  - gate-off run 与 gate-on run 都成功完赛
  - gate-on run 的 `strategy_summary.json` `intents_emitted` ≤ gate-off run 的 `intents_emitted`
  - gate-on run 的 `strategy_events.jsonl` 至少含一条 `strategy.ml_gate.allowed` 或 `strategy.ml_gate.suppressed`
  - 两次 run 都没把 `lightgbm` 拉进 live 路径（重申 Track B import 守护，但走真实链路）

### 非使命

- **不接 mainnet**：Phase 5 三锁仍生效；Track C 任何 CLI 都不创建任何带 mainnet endpoint 的 venue。
- **不动 supervisor**：Track C 不改 `run_supervisor` / `node` 生命周期；C2 是只读 ops；C3 的 active.json 在 strategy 构造期读取，不是运行时热加载。
- **不接 Telegram / Grafana / Prometheus**：C2 出口仍是 `xtrade ops status --json` + journalctl，沿用 Phase 4 决策。
- **不做 hyperparam search / autoML**：B3 baseline 阈值就是当前阈值；Track C 只做"换模型"基础设施，不做"找更好模型"。
- **不做 multi-model ensemble**：active.json 只指一个 run_id，不做加权平均。
- **不做 drift sentinel**（曾经的 C5 提议）：保留到 Phase 6 / 后续 brief；Track C scope 收紧到上面 4 项即可。
- **不动 news fetcher**：B1 仍依赖外部已抓好的 RSS jsonl；Track C 不引入网络拉取。

---

## 2. 验收标准（Go / No-Go 清单）

每项明确 PASS / FAIL，记入 `docs/phase5_results.md`（Track C 章节追加）。

| ID  | 名称 | 描述 |
|-----|------|------|
| C1  | replay-gate CLI | `xtrade research replay-gate --run-id <id> --since <iso> --until <iso> [--signals-root <path>]` 命令存在；从 `data/signals/<date>.jsonl`（或自定义 root）读所有时间窗内 row，调 `MLGate.decide` 各一次，落盘 `models/<run_id>/replay_<since>_<until>.json` 含 `n_signals`/`n_allowed`/`n_suppressed`/`by_side`/`by_symbol`/`p_long_quantiles`/`score_threshold`/`direction_check`；offline test 用合成 signals jsonl + FakeModel 锁字段 + 计数；空时间窗 → 写 `n_signals=0` 文件并 exit code 0；输出 byte-stable（同输入两次跑 byte equal）。 |
| C2  | ops status ml_gate 摘要 | `OpsStatus` dataclass 增 `ml_gate` sub-block；`render_status_text` / `render_status_json` 显示 5 字段；新增 `MLGateAuditWriter`（落 `/var/lib/xtrade/audit/ml_gate.<date>.jsonl`，O_APPEND 原子写）；`MomentumFollow._apply_ml_gate` 在 allowed / suppressed 两分支各调一次 audit.write（emit_event 路径并行保留）；24h 窗由 `audit_root/ml_gate.*.jsonl` 扫得；offline test 锁 4 路径（无事件 / 全 allow / 全 suppress / 混合）；样本 < 10 时 `suppression_rate_24h` 为 `None`；ops 模块仍不 import `xtrade.research.*`（import isolation 守护扩展）。 |
| C3  | 模型注册表 + promote | `models/active.json` schema 锁；`xtrade research promote <run_id>` 命令存在、检查三件套 + auc 阈值、原子写 + history append；`MLGateConfig.use_active_model=True` 时 strategy 从 active.json 解析 model_path，硬编码 yaml 路径被忽略；offline test 覆盖：(a) promote 缺 metrics.json → 拒；(b) auc < 0.5 → 拒；(c) 正常 promote → active.json 写入 + history append；(d) strategy `use_active_model=True` 且 active.json 缺失 → 启动期 raise；(e) `use_active_model=False` 行为不变；promote 操作不导入 sklearn / lightgbm（纯文件操作）。 |
| C4  | paper 端到端 smoke | `tests/test_paper_ml_gate_e2e.py`，跳过条件 `pytest.skip` unless `XTRADE_RUN_PAPER_E2E=1`；测试体走 B2 build_dataset → B3 run_training → C3 promote → C4 paper run × 2（gate-off + gate-on），断言 (i) 两次都成功 (ii) gate-on intents ≤ gate-off intents (iii) gate-on 至少发 1 条 `strategy.ml_gate.*` 事件 (iv) 两次 run 的 venv `sys.modules` 中没有 `lightgbm`。 |

---

## 3. 不在本阶段处理的事项（显式延后）

- **不上 mainnet**：Phase 5 三锁仍生效。
- **不做 drift sentinel**：KS-statistic / feature distribution 监控推到 Phase 6 或之后。
- **不做 model hot-reload**：换模型仍要 restart strategy 进程。
- **不做 multi-model ensemble** / **stacking** / **online learning**。
- **不动 supervisor / scanner / bridge** 的运行时路径；C2 是纯只读扩展。
- **不引入新依赖**：复用 Track B 已声明的 `[research]` extra（sklearn / lightgbm / pyarrow / numpy）。
- **不做新闻流实时抓取**：B1 仍 batch。

---

## 4. 仓库结构变更

### 新增

```
src/xtrade/research/replay_gate.py          # C1：replay-gate 实现
src/xtrade/research/registry.py             # C3：active.json + history 读写
src/xtrade/strategy/ml_gate_audit.py        # C2：ML gate jsonl audit writer
tests/test_research_replay_gate.py          # C1 offline tests
tests/test_strategy_ml_gate_audit.py        # C2 audit writer offline tests
tests/test_ops_status_ml_gate.py            # C2 ops status ml_gate 摘要 offline tests
tests/test_research_registry.py             # C3 offline tests
tests/test_paper_ml_gate_e2e.py             # C4 opt-in e2e
docs/phase5_track_c_brief.md                # 本文件
```

注：CLI 命令通过现有 `cli.py` 中的 `research_app = typer.Typer(...)` 挂载，沿用
`@research_app.command("replay-gate")` / `@research_app.command("promote")` 模式
（与 Track B `research news` / `research train` 同级）。

### 修改

```
src/xtrade/ops/status.py                    # C2：OpsStatus + collect_status 扩展 ml_gate 子块
                                            # 新增 DEFAULT_AUDIT_ROOT 常量 + OpsPaths.audit_root
src/xtrade/strategy/plugins/momentum_follow.py
                                            # C2：emit strategy.ml_gate.allowed + audit.write
                                            # C3：MLGateConfig.use_active_model 支持
src/xtrade/research/ml_gate.py              # C3：MLGateConfig 扩展 + 校验
tests/test_research_import_isolation.py     # 守护 src/xtrade/ops/status.py 不拉 research stack
src/xtrade/cli.py                           # 挂 research replay-gate / research promote 命令
.gitignore                                  # 加 models/ + data/research/ 规则
```

### 数据布局新增

```
models/active.json                          # C3 注册表当前指针（git-ignored 通过 .gitignore 规则）
models/active.history.jsonl                 # C3 promote 历史 append-only
models/<run_id>/replay_<since>_<until>.json # C1 输出
/var/lib/xtrade/audit/ml_gate.<date>.jsonl  # C2 ML gate audit jsonl（VPS 0640 xtrade:xtrade）
```

---

## 5. 任务分解

### Task C1 —— `xtrade research replay-gate`

- 新模块 `xtrade.research.replay_gate`：
  ```python
  @dataclasses.dataclass(frozen=True)
  class ReplaySummary:
      run_id: str
      since: dt.datetime          # tz-aware UTC
      until: dt.datetime
      n_signals: int
      n_allowed: int
      n_suppressed: int
      by_side: dict[str, dict[str, int]]      # side → {"allowed":..,"suppressed":..}
      by_symbol: dict[str, dict[str, int]]
      p_long_quantiles: dict[str, float]      # {"p10","p50","p90"}
      score_threshold: float
      direction_check: bool

  def replay_gate(
      *, run_id: str,
      since: dt.datetime, until: dt.datetime,
      signals_root: Path,
      models_root: Path = Path("models"),
      score_threshold: float | None = None,   # default: from model meta
      direction_check: bool = True,
  ) -> Path: ...
  ```
- 读 jsonl：`signals_root/<date>.jsonl` UTC 日切；只取 `since <= ts < until` 行；按 `(symbol, side, features)` 调 `MLGate.decide`。
- features 字段：从 signal payload 中取 (Track B 已规定 `features` 子字段)；缺字段时 fallback 0.0 + 记 stat。
- 量化分布：`numpy.quantile(p_long_list, [0.1, 0.5, 0.9])`。
- 输出 byte-stable：JSON `sort_keys=True, ensure_ascii=False, indent=2`；timestamps 转 ISO `YYYY-MM-DDTHH:MM:SSZ` 无微秒。
- CLI 入口（cli.py 挂 `research` sub-parser，已为 C3 准备）：
  ```
  xtrade research replay-gate \
      --run-id <id> \
      --since 2026-05-22T00:00:00Z \
      --until 2026-05-23T00:00:00Z \
      [--signals-root data/signals] \
      [--models-root models] \
      [--score-threshold 0.55] \
      [--no-direction-check]
  ```
- 验收测试 `tests/test_research_replay_gate.py`：
  - 用 `_FakeModel`（同 B4 测试 fixture pattern）+ monkeypatch loader
  - 合成 3 天 signals jsonl（BTC LONG / ETH SHORT 各 5 条）
  - 跑 since=Day1 until=Day2 → 锁 n_signals 与 by_side 计数
  - 空时间窗 → `n_signals=0` 文件被写、exit code 0
  - 两次 run → byte-equal
  - 缺 features 字段 → fallback + warning，不 raise

### Task C2 —— `xtrade ops status` ML-gate 摘要

- 新 dataclass field on `OpsStatus`:
  ```python
  @dataclasses.dataclass(frozen=True)
  class MLGateStatus:
      enabled_strategies: int
      suppressed_24h: int
      allowed_24h: int
      suppression_rate_24h: float | None
      last_event_age_s: float | None
  ```
- **新增 audit writer** `src/xtrade/strategy/ml_gate_audit.py`:
  ```python
  class MLGateAuditWriter:
      def __init__(self, audit_root: Path) -> None: ...
      @classmethod
      def if_enabled(cls, audit_root: Path | None) -> "MLGateAuditWriter | None":
          return cls(audit_root) if audit_root else None
      def write(self, *, kind: Literal["allowed","suppressed"],
                symbol: str, side: str, score: float, threshold: float,
                reason: str, source_signal_id: str | None,
                ts: dt.datetime | None = None) -> None: ...
  ```
  落 `audit_root/ml_gate.<YYYY-MM-DD>.jsonl`（UTC 日切）；写入用 `os.open(O_WRONLY|O_APPEND|O_CREAT, 0o640)` + 单次 `os.write(line + "\n")`（同 A2 bridge audit）。
- 数据源：`audit_root/ml_gate.*.jsonl`（默认 `/var/lib/xtrade/audit`，与 A2 共用目录）；扫文件名取近 2 日（昨天 + 今天足够 24h 窗），过滤 `ts >= now - 24h`。
- `enabled_strategies` 来自 supervisor.yaml 解析（best-effort，软读；yaml 缺失则为 0，不 raise）。
- `MomentumFollow._apply_ml_gate`（Track B 现有）：
  - 在 `decision.allow=True` 路径：调 audit.write(kind="allowed", ...) + 发 `strategy.ml_gate.allowed` event（envelope 与 suppressed 对称）。
  - 在 `decision.allow=False` 路径：保留 Track B 的 emit_event suppressed + 调 audit.write(kind="suppressed", ...)。
  - 当 strategy 构造时未传入 audit_root → writer 为 None → 两分支跳过 audit.write（journalctl 仍能看到 emit_event）。
- render：
  ```
  ml_gate:
    enabled_strategies: 1
    suppressed_24h:     7
    allowed_24h:        42
    suppression_rate:   14.3%
    last_event_age_s:   183.5
  ```
  样本 < 10 时 `suppression_rate` 显示 `n/a`。
- 验收测试 `tests/test_ops_status_ml_gate.py`：
  - tmp_path 模拟 logs_root + 3 个 run-id 子目录
  - 4 路径：(a) 无 strategy_events.jsonl → 全 0；(b) 全 allowed × 30 条 → suppression_rate=0.0；(c) 全 suppressed × 30 条 → suppression_rate=100.0；(d) 12 allowed + 8 suppressed → 40.0
  - 样本 < 10：5 allowed + 2 suppressed → `suppression_rate_24h is None`
  - `last_event_age_s` 锁单调
- import isolation：在 `tests/test_research_import_isolation.py` 增 `test_ops_status_import_does_not_pull_research_stack`：
  ```python
  def test_ops_status_import_does_not_pull_research_stack():
      rc, out, err = _run_isolation_check("import xtrade.ops.status")
      assert rc == 0
  ```

### Task C3 —— 模型注册表 + `xtrade research promote`

- 新模块 `xtrade.research.registry`：
  ```python
  @dataclasses.dataclass(frozen=True)
  class ActiveModelRef:
      run_id: str
      model_path: Path        # 解析后的绝对路径
      active_since: dt.datetime
      promoted_by: str

  def load_active(models_root: Path) -> ActiveModelRef | None: ...
  def promote(
      run_id: str, *, models_root: Path,
      promoted_by: str | None = None,
      min_auc: float = 0.5,
  ) -> ActiveModelRef: ...
  ```
- promote 流程：
  1. 校验 `models/<run_id>/{model.pkl, metrics.json, dataset_meta.json}` 三件套
  2. 读 `metrics.json["auc"]`；< `min_auc` raise `PromoteRejected`
  3. 旧 active 备份到 history（append `{"action":"deactivate", "ref": <old>}` + `{"action":"activate", "ref": <new>}`）
  4. write `models/active.json.tmp` 然后 `os.replace`（原子）
- `MLGateConfig` 扩展：
  ```python
  use_active_model: bool = False
  ```
  - 与 `model_path` 互斥：`use_active_model=True` 时 `model_path` 在 `from_mapping` 中可为 None；`MomentumFollow` 构造期：若 `use_active_model` → 解析 active.json，缺失 raise；否则保持 Track B 行为。
- CLI：`xtrade research promote <run_id> [--min-auc 0.5] [--promoted-by <handle>]`。
- 验收测试 `tests/test_research_registry.py`：
  - tmp_path 模拟 models/<run_id>/ 三件套；缺一项 → raise
  - auc < 0.5 → raise
  - 正常 promote → active.json + history 正确
  - 第二次 promote 不同 run_id → history 累积、active.json 覆盖
  - `MLGateConfig(use_active_model=True, model_path=None)` 配合 active.json 存在 → ok
  - `MLGateConfig(use_active_model=True)` 且 active.json 缺失 → 构造期 raise
- promote 必须不拉 sklearn / lightgbm（纯 json + path 操作）；import isolation 守护扩展：`test_research_registry_import_does_not_pull_ml`。

### Task C4 —— Paper 端到端 smoke

- 新测试 `tests/test_paper_ml_gate_e2e.py`：
  ```python
  import os
  import pytest

  if not os.environ.get("XTRADE_RUN_PAPER_E2E"):
      pytest.skip("set XTRADE_RUN_PAPER_E2E=1 to opt in", allow_module_level=True)
  ```
- 链路（全部 in-process，无 subprocess）：
  1. 合成 OHLCV bundle（同 `tests/test_research_train.py::_make_bundle`）
  2. `run_training(bundle, model_name="logistic", seed=7)` → `run_id`
  3. `promote(run_id)` → active.json
  4. paper run × 2：
     - gate-off：`MomentumFollow({"notional_usd":"500"})`
     - gate-on：`MomentumFollow({"notional_usd":"500", "ml_gate":{"enabled":True, "use_active_model":True, "score_threshold":0.55}})`
     - 各喂 200 条合成 signal，统计 emit 的 intent 数
  5. 断言 gate-on `intents_emitted` ≤ gate-off `intents_emitted`
  6. 断言 gate-on 路径至少发 1 条 `strategy.ml_gate.allowed` 或 `strategy.ml_gate.suppressed`（caplog）
  7. **不**主动 import lightgbm；测试结束后断言 `"lightgbm" not in sys.modules`（如果环境本来就装了 lightgbm 这一条会失败 → 测试体内用 subprocess 隔离断言；或在 in-process 路径直接 logistic-only）
- 默认 CI / 本地 `pytest` 跳过；操作员手动 `XTRADE_RUN_PAPER_E2E=1 pytest tests/test_paper_ml_gate_e2e.py -v`。

---

## 6. 安全边界

沿用 Phase 5 brief §6 全部规则。Track C 增量：

- **active.json 信任边界**：`load_active()` 读到的 `model_path` 必须落在 `models_root` 之内（防 path traversal）；绝对路径必须以 `models_root.resolve()` 为前缀，否则 raise `ActiveModelInvalid`。
- **promote 命令鉴权**：promote 不做 token 校验（开发者本地工具），但要求 `--promoted-by` 显式给定，落到 history；future Phase 6 可扩展为 OTP。
- **replay-gate 不动 venue / 不发单**：纯只读 + 只写 `models/<run_id>/replay_*.json`。
- **C2 ops 模块继续不导入 research 子树**：守护测试 `test_ops_status_import_does_not_pull_research_stack` 显式断言。
- **C4 e2e 仍走 paper**，不连任何 venue endpoint；mainnet 三锁不受影响。

---

## 7. 决策矩阵（Track C 收尾时）

| 结果 | 判断 | 下一步 |
|---|---|---|
| C1–C4 全 PASS | **Track C 收尾**；写 `docs/phase5_results.md` Track C 章节，进入 Phase 6 brief 起草 | Phase 6：mainnet 小资金 tap test brief。 |
| C1+C2+C3 PASS，C4 因 opt-in 未跑 | **Track C 部分收尾**；e2e 标 deferred，但 C1/C2/C3 单测全绿 | 操作员在本地手跑一次 e2e 后补签 |
| C2 / C3 任一 FAIL | **暂缓 C4** | C3 是 C4 的依赖（active.json）；先修 |
| C1 FAIL | **不阻 C2/C3** | replay-gate 是独立诊断工具，可单独修 |

---

## 8. 交付物

1. `src/xtrade/research/replay_gate.py`（C1）
2. `src/xtrade/research/registry.py`（C3）
3. `src/xtrade/cli_research.py` 或 `src/xtrade/cli.py` 内挂 sub-parser（C1 + C3）
4. `src/xtrade/ops/status.py` 扩展（C2）
5. `src/xtrade/strategy/plugins/momentum_follow.py` 扩展（emit allowed + `use_active_model`）
6. `src/xtrade/research/ml_gate.py` `MLGateConfig.use_active_model`
7. 4 个新测试文件 + 1 个测试扩展（import isolation 守护 ops）
8. `src/xtrade/strategy/ml_gate_audit.py`（C2 audit writer）
9. `docs/phase5_track_c_brief.md`（本文件）
10. `docs/phase5_results.md` Track C 章节（收尾时写）

---

## 9. 建议执行顺序

按依赖最小化排：

1. **C1 replay-gate** —— 独立模块，零依赖于 C2/C3；先落给后续提供"模型 + signal 历史能产出什么"的可视化。
2. **C2 ops status ml_gate 摘要** —— 改 ops + emit allowed；不依赖 C3。
3. **C3 registry + promote** —— 引入 active.json + use_active_model；C4 的前置依赖。
4. **C4 paper e2e** —— 串 B2/B3/C3 + strategy；最后做，因为依赖前 3 项稳定。

每个 task 完成后跑一次 `pytest tests/` 全套（包含 import isolation 守护），不退化。

---

## 参考链接

- `docs/phase5_brief.md`（Phase 5 总简报）
- Track A6 SHIPPED：commit `c68063c`
- Track B SHIPPED：commit `35fb31c`（VPS release `77616ddaac42`）
- Runbook 修复：commit `fbff64a`
- `src/xtrade/research/ml_gate.py`（MLGate 实现，Track C 直接复用 `.decide()`）
- `src/xtrade/research/train.py`（METRICS_KEYS / dataset_meta.json schema，Track C 复用）
- `src/xtrade/ops/status.py`（C2 扩展点）
- `src/xtrade/strategy/plugins/momentum_follow.py`（C2 emit / C3 use_active_model 扩展点）
- `tests/test_research_import_isolation.py`（Track C 守护测试扩展点）
