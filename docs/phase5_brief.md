# Phase 5 实施简报 —— 长跑加固 + ML/新闻情绪信号探索

> 编制日期：2026-05-24
> 目标仓库：`/Users/bitcrab/xtrade`
> 上游依据：
> - 主路线图：`xtrade_plan.md` §七 "Phase 5 — 小资金上线与迭代"
> - Phase 4 收尾：`docs/phase4_results.md`（offline T1–T5、T7 CLI、T8 全 PASS；VPS 实测 §1.6 / §1.7 由操作员 2026-05-24 起填回）
> - 与用户对齐（2026-05-24）：Phase 5 拆为**两条并行轨道**
>   - **Track A — Phase 4.5 加固**：在 Phase 4 基础上把 5 项 "Phase 5 入口加固" 落地（`phase4_results.md` §3 的 5 条）。
>   - **Track B — ML / news 情绪信号探索**：纯研究分支，本地 mac 执行，不动 VPS。
> 执行方式：本简报交给 **Claude Code** 在 `/Users/bitcrab/xtrade` 中执行；两轨独立 PR / commit 序列，互不阻塞。

---

## 0. 进入 Phase 5 的前提（Phase 4 收尾结论）

Phase 4 offline 交付已 PASS（`docs/phase4_results.md` §5 第一行签字）。**VPS 实测填回与本简报并行**：

| 前提项 | 状态 | 责任方 |
|---|---|---|
| Phase 4 §1.6 — 4 个 drill 在 VPS PASS（SIGKILL / OOM / 网络抖断 / openclaw 5xx） | 进行中（2026-05-24 操作员上线起跑） | 操作员（用户） |
| Phase 4 §1.7 — openclaw 端 `xtrade-approval` TaskFlow 上线 + testnet 端到端 manual 一次 PASS | 进行中 | openclaw 操作员 + 用户 |
| Phase 4 §5 — 第二段 VPS 签字（含日期、run_id、操作员 handle） | 待填 | 操作员 |
| 主路线图 §七 重读、Phase 5 范围与用户对齐 | ✅ 2026-05-24 | Claude Code + 用户 |

> Track A 的实现**不依赖** VPS 签字（绝大部分可 offline 完成 + offline 测试覆盖），但其中"持久 TradingNode"与"df 容量告警"两项的 VPS 实测必须在 §1.6/§1.7 填回完成之后才能签 PASS。
>
> Track B（ML / news）**完全不依赖** VPS。

---

## 1. Phase 5 的使命与非使命

### Track A —— Phase 4.5 加固（VPS 长跑实战准备）

**使命**

1. **持久 TradingNode**：Phase 4 仍是"一 intent 一 node"（继承 Phase 3 testnet runbook 的最稳定路径，每次都 `node.start()` → submit → `node.stop()`）。Phase 5 迁到单进程长持 node，节省启动开销并支持持仓滚动。要求 supervisor 进程内 node 长跑、SIGTERM 时优雅 stop、崩溃重启走 systemd `Restart=on-failure`。
2. **bridge 出站事件 → audit JSONL**：当前 `dispatch_failed` 仅作为 approval 行的注解，无法独立审计 bridge 路径。Phase 5 增加 `/var/lib/xtrade/audit/bridge_out.<date>.jsonl`，每条 dispatch attempt（成功 / 重试 / 终态失败 / 凭据 scrub 拒绝）都写一行。
3. **scanner 结构化日志**：scanner 当前只产 `scan_summary.json` artefact，运行时事件未走 `emit_event`。Phase 5 在 scanner 内引入 `xtrade.obs.emit_event`，让 `journalctl -t xtrade.scanner -o cat | jq` 可用；event 命名空间约定 `scanner.*`（`scanner.run.start` / `scanner.run.complete` / `scanner.signal.emitted` / `scanner.run.error`）。
4. **`/var/lib/xtrade` 容量告警**：state GC 已就位（`02_state_gc.py`），但 VPS 上还需要一个 `df` 守望，超阈值（默认 80% 警告 / 90% 拒新单）即在 `xtrade ops status --json` 中暴露 `disk_warning` 字段；supervisor 在 ≥ 90% 时自动进入 paused 状态并写明原因。
5. **mainnet 第三锁**：Phase 4 已在 `_assert_testnet_only` + `/etc/xtrade/env` 中布两锁；Phase 5 补第三锁：CLI 入口（`xtrade live supervise` / `xtrade live run` / `xtrade ops resume`）在检测到 venue yaml 解析出 mainnet endpoint 时，**额外**要求 `XTRADE_MAINNET_UNLOCK_TOKEN` 环境变量与 `/etc/xtrade/mainnet_unlock` 文件内容一致才放行；该锁与 Phase 3 双锁串行。

**非使命**

- **不接 Telegram / Grafana / Loki / Prometheus / OpenTelemetry collector**：Phase 4 决策仍生效。
- **不引入数据库**：Parquet + jsonl + journalctl 不变。
- **不容器化**。
- **不动 venue 矩阵**：仍 Binance Spot + Futures + Hyperliquid testnet。

### Track B —— ML / news 情绪信号探索（研究分支）

**使命**

B1. **新闻 / 社媒情绪 feature pipeline（离线）**：新增 `xtrade.research.signals.news` 子包，提供从指定新闻源（NewsAPI / RSS / 已抓取本地 corpus）抽取每 instrument × 时间窗口的情绪 score 的 batch 管线；输出 Parquet 到 `data/research/news_sentiment/<instrument>/<date>.parquet`。情绪打分至少给两种可比 baseline（VADER 或同级 lexicon、HuggingFace pretrained transformer 二选一），不引入外部 API key 到代码。
B2. **ML feature/label 数据集**：编写 `xtrade.research.dataset.build` 把 Phase 0/1 的 OHLCV catalog（已就位）+ B1 的 sentiment Parquet 对齐成 `(features, label)` 对；label 选择短期收益（5 分 / 15 分 / 1 h）；保留 `train / val / test` 时间窗口切分逻辑，避免泄漏。
B3. **ML baseline 模型**：训练 1–2 个 baseline（logistic / GBM via lightgbm），输出 `models/<run_id>/{model.pkl, metrics.json, feature_importance.csv}`；评估只看分类 / 回归指标（AUC / IC / RMSE），**不**接入交易决策。
B4. **信号融合 prototype**：把 ML 模型的预测分数作为 `xtrade.strategies` 现有规则策略的一个额外 gate（"ML 同向且分数 ≥ 阈值才发 intent"）；先用 Phase 3 paper run 链路（**不**进 testnet runbook）验证全链路 wiring；ML gate 默认**关闭**（`config.ml_gate.enabled=false`），需在 strategy yaml 显式打开。

**非使命**

- **不调用付费 API**：所有数据源走免费 / 已下载 / 公开 RSS；任何凭据走 `.env`，不入 git。
- **不动 testnet 真单**：Track B 在 paper 链路验证；如要进 testnet 需在 Phase 6 brief 单独决策。
- **不引入 PyTorch / TensorFlow / GPU 依赖**：lightgbm + scikit-learn + numpy 已足；只在工作站本地跑训练。
- **不做实时推理服务**：模型推理在 strategy `on_signal()` 中同步加载 + 内联预测；不起 model server。
- **不做超参搜索 / autoML**：单一固定超参 + 简单 grid（≤ 8 组），目标是建链不调参。

> Phase 5 仍**不接主网真钱**。`xtrade_plan.md` §七 原文"小资金上线"被推迟到 Phase 6；Phase 5 的全部交付物均在 testnet / paper / 离线管线内。Track A 是把 Phase 6 mainnet 上线的稳定性 / 安全 / 可观测前置条件做齐；Track B 是把策略增量先在离线 / paper 验证完。

---

## 2. 验收标准（Go / No-Go 清单）

每项明确 PASS / FAIL，记入 `docs/phase5_results.md`。Track A 与 Track B 各自独立评判，相互不阻塞。

### Track A — Phase 4.5 加固

| ID  | 名称 | 描述 |
|-----|------|------|
| A1  | 持久 TradingNode | `run_supervisor` 启动时一次性 `node.start()`，所有 intent 复用同一 node 直到 SIGTERM；崩溃重启场景下 cursor 与 approval 状态不丢失；testnet runbook 重跑一次 manual 链路（信号 → bridge → confirm → fill → cancel），journalctl 中 node 仅 start/stop 一次。 |
| A2  | bridge 出站 audit jsonl | `dispatch()` 每条 attempt（含 retry、HTTP 4xx、secret-scrub refused）写 `/var/lib/xtrade/audit/bridge_out.<YYYY-MM-DD>.jsonl` 一行；行内含 `approval_id` / `attempt` / `status_code` / `error` / `dispatched_at`；offline test 覆盖 5 种路径（200 / 5xx after retry / 4xx / network exception / secret refusal）；audit 文件采用 atomic append（`os.open(O_APPEND)`），多写并发不撕裂。 |
| A3  | scanner 结构化日志 | scanner 入口 `xtrade scan run` 在 start / per-instrument / summary / error 处发 `scanner.*` 事件；`journalctl -t xtrade.scanner -o cat \| jq -c '.event'` 至少能看到 `scanner.run.start` + `scanner.signal.emitted`*N + `scanner.run.complete`；事件 schema 由 `tests/test_log_event.py` 风格的正则审计加锁。 |
| A4  | 磁盘容量告警 | `xtrade.ops.collect_status` 新增 `disk` 字段：`{"path":"/var/lib/xtrade","used_pct":63,"free_bytes":...,"warning":false}`；supervisor 每 iteration 启动前调 `_check_disk_room()`，≥ 90% 时主动写 sentinel 并发 `supervisor.disk.exhausted` event 进入 paused；阈值在 supervisor.yaml 中可配置（默认 80 / 90）。 |
| A5  | mainnet 第三锁 | `xtrade.live.config` 在解析任意 `venue_endpoint` 时若识别为 mainnet（非 testnet domain 列表外）即调 `_assert_mainnet_unlock(env)`：要求 `XTRADE_MAINNET_UNLOCK_TOKEN` 与 `/etc/xtrade/mainnet_unlock` 文件首行 strip 后**完全相等**且文件 mode ≤ `0400 root:root`，否则 raise；offline test 覆盖 4 路径（无 env / 无文件 / 内容不等 / 文件权限松）。 |
| A6  | installer hardening | `scripts/phase4/install_vps.sh` 在 2026-05-24 VPS 首装实测中暴露 3 个阻断 bug + 1 个噪音 bug：(a) `uv python install 3.12` 默认把工具链放到 `~/.local/share/uv/python`，root 运行落到 `/root/.local/...`（mode `0550 root:root`），`xtrade` 用户 traverse 失败 → systemd `status=203/EXEC Permission denied`；(b) installer 从未 seed `/etc/xtrade/supervisor.yaml`（systemd unit 指向该文件），supervisor 启动即 `FileNotFoundError`；(c) Bug a 修好后进入 import 阶段，vectorbt → numba `caching.py:423` 报 `cannot cache function 'set_seed_nb': no locator available`，因 systemd `ProtectSystem=strict` + `ProtectHome=yes` 把 in-tree / user-wide / `NUMBA_CACHE_DIR` 三条 cache 路径全堵死；(d) restart 风暴刷 journal。Phase 5 A6 任务：(1) installer 显式 `UV_PYTHON_INSTALL_DIR=/opt/uv/python` 并 `chmod -R a+rX /opt/uv`；(2) 新增 `config/supervisor.example.yaml`，installer seed 到 `/etc/xtrade/supervisor.yaml`（`0640 root:xtrade`）；(3) installer 创建 `/var/lib/xtrade/numba_cache`（`0750 xtrade:xtrade`）并把 `NUMBA_CACHE_DIR=/var/lib/xtrade/numba_cache` + `HOME=/var/lib/xtrade` 追加到 `/etc/xtrade/env`；(4) installer 末尾 post-start health check 失败时打印明确"下一步操作"提示；(5) systemd unit 加 `StartLimitBurst=5` + `StartLimitIntervalSec=120` 抑制 restart 风暴；(6) `scripts/phase5/01_check_phase5_prereqs.sh` 增加 venv interpreter 可执行性 + vectorbt import 两个 self-test。 |

### Track B — ML / news 信号探索

| ID  | 名称 | 描述 |
|-----|------|------|
| B1  | news 情绪 feature pipeline | `xtrade.research.signals.news.build_sentiment_features(instrument, since, until)` 输出 Parquet schema `{ts, instrument, source, raw_score, normalized_score, n_articles}`；至少接入 2 个数据源（如 1 RSS + 1 local NewsAPI export）；情绪打分 baseline 至少 1 个（VADER 或类似 lexicon）；offline test 用 fixture corpus 锁 schema + 计数。 |
| B2  | feature/label 数据集 builder | `xtrade.research.dataset.build_dataset(instrument, horizons_min, train_window, val_window, test_window)` 产 `(X_train, y_train, X_val, y_val, X_test, y_test)` 及对应 instrument/timestamp 索引；label 用 forward return（5 / 15 / 60 分）；时序切分不泄漏（test 严格在 train 之后，val 在中间）；offline test 用合成数据锁切分行为。 |
| B3  | baseline 模型训练 | `xtrade.research.train.run_training(dataset_id, model="lightgbm" \| "logistic")` 训练并保存 `models/<run_id>/{model.pkl, metrics.json, feature_importance.csv}`；`metrics.json` 至少含 `auc`/`accuracy`/`ic`/`sample_count`/`features_used`/`train_window`/`val_window`/`test_window`；offline test 用合成数据锁路径产出 + reproducibility（同 seed 同输入 → 同 metrics）。 |
| B4  | ML gate 接入 strategy（paper） | 现有 `xtrade.strategies.momentum_follow` 接受可选 `ml_gate: MLGateConfig{enabled, model_path, score_threshold, direction_check}`；启用后 `on_signal()` 在规则放行后再走 ML 推理，得分低于阈值或方向相反则抑制 intent；Phase 3 paper runbook 复跑（**不**碰 testnet）一次：ml_gate 关闭 vs 启用各跑一次，`logs/<run-id>/strategy_summary.json` 中可区分两次的 intent 数。 |

---

## 3. 不在本阶段处理的事项（显式延后）

- **不上 mainnet 真钱**：`_assert_testnet_only` + Phase 5 三锁 + 操作员 OTP 仍是软硬两道墙；mainnet tap test 推到 **Phase 6**。
- **不容器化**：Phase 4 决策延续。
- **不接 Telegram / Grafana / Loki / Prometheus**：journalctl + jsonl summaries 仍是观测主面。
- **不引入数据库**。
- **不引入 PyTorch / TensorFlow / GPU / 模型 server**：Track B 仅 lightgbm + sklearn。
- **不引入新 venue / 新 instrument**。
- **不做实时新闻流处理**：news sentiment 仍是 batch（定时拉取 + 离线打分），不接 streaming。
- **不做策略组合 / 多策略并行调度**：单策略 + 单 ML gate；多策略推到 Phase 6 之后。
- **不做超参搜索 / autoML / hyperopt**：Track B 仅 baseline。

---

## 4. 仓库结构变更

### 新增（Track A）

```
src/xtrade/live/supervisor.py                # 改造为持久 TradingNode（A1）
src/xtrade/bridge/audit.py                   # bridge 出站 audit jsonl 写入器（A2）
src/xtrade/obs/scanner_events.py             # scanner.* 事件常量（A3）
src/xtrade/ops/disk.py                       # 容量探测 + 阈值判断（A4）
src/xtrade/live/mainnet_unlock.py            # 第三锁实现（A5）
tests/test_supervisor_persistent_node.py
tests/test_bridge_out_audit.py
tests/test_scanner_log_events.py
tests/test_ops_disk_check.py
tests/test_mainnet_unlock.py
```

### 新增（Track B）

```
src/xtrade/research/__init__.py
src/xtrade/research/signals/news/__init__.py
src/xtrade/research/signals/news/pipeline.py
src/xtrade/research/signals/news/scorers.py
src/xtrade/research/dataset/__init__.py
src/xtrade/research/dataset/build.py
src/xtrade/research/train.py
src/xtrade/strategies/momentum_follow.py     # 修改：增加 ml_gate hook
tests/test_research_news_pipeline.py
tests/test_research_dataset_build.py
tests/test_research_train.py
tests/test_strategy_ml_gate.py
docs/phase5_ml_research_notes.md             # Track B 实验笔记（自由格式）
```

### 配置与部署

```
deploy/env/xtrade.env.example                # 增 XTRADE_MAINNET_UNLOCK_TOKEN 占位
deploy/systemd/xtrade-supervisor.service.in  # ReadWritePaths 增 /var/lib/xtrade/audit
scripts/phase5/01_check_phase5_prereqs.sh    # 上线前自检（mainnet 三锁齐备、audit 目录权限）
scripts/phase5/02_disk_watch_smoke.sh        # A4 烟测脚本（filldisk + 解除）
docs/phase5_brief.md                         # 本文件
docs/phase5_results.md                       # 收尾报告（Phase 5 末写）
```

VPS 文件布局新增：

```
/var/lib/xtrade/audit/                     # 0750 xtrade:xtrade，bridge_out.*.jsonl
/etc/xtrade/mainnet_unlock                 # 0400 root:root（第三锁文件，默认不存在）
```

---

## 5. 任务分解

### Track A

#### Task A1 —— 持久 TradingNode

- `run_supervisor` 启动阶段一次性 `node = build_testnet_trading_node(...); node.start()`；存到 `SupervisorContext` 中。
- `_submit_intent(node, intent)` 直接复用 ctx node，不再 per-intent start/stop。
- SIGTERM / stop_event 触发 → `node.stop()` 在 `finally` 块（已存在）。
- 崩溃路径：strategy 异常仍 catch，**不**触发 node restart；node 自身崩溃（`OSError` / venue 断流 ≥ N 秒）→ supervisor 重启整个进程（systemd `Restart=on-failure`）。
- 持仓滚动：node 长跑后 paper account snapshot 在 iteration 之间应保持单调一致；RiskGate 的 `account.snapshot()` 每 iteration 取最新。
- 验收：`tests/test_supervisor_persistent_node.py` 用 fake node 锁定 `start/stop` 各调用 1 次；testnet runbook 复跑（人工，VPS 上 §1.7 链路通后操作员补一次）。

#### Task A2 —— bridge 出站 audit jsonl

- `src/xtrade/bridge/audit.py`：
  ```python
  class BridgeAuditWriter:
      def __init__(self, audit_root: Path): ...
      def write(self, *, approval_id: str, attempt: int, kind: Literal["ok","retry","fail","refused"],
                status_code: int|None, error: str|None, dispatched_at: dt.datetime,
                elapsed_s: float, response_excerpt: str|None) -> None: ...
  ```
  - `audit_root/bridge_out.<YYYY-MM-DD>.jsonl`，UTC 日切。
  - 写入用 `os.open(path, O_WRONLY|O_APPEND|O_CREAT, 0o640)` + 单次 `write(line + "\n")` 原子（POSIX 保证 PIPE_BUF 内的 append 不撕裂；4 KiB 内 envelope 足够）。
- `OpenclawBridge.dispatch` 在每个分支点（200 / 5xx after retry / 4xx / `TimeoutException` / `TransportError` / `SecretLeakError`）调一次 audit；secret-scrub refused 路径 audit 中 `error` 字段已脱敏（仅写 `"secret-scrub: <exception class>"`，不写原 secret）。
- audit_root 来源：`SupervisorConfig.audit_root` 默认 `/var/lib/xtrade/audit`，本地测试用 tmp_path。
- 验收：`tests/test_bridge_out_audit.py` 用 httpx MockTransport 跑 5 条路径，断言对应 jsonl 行的 kind + status_code + approval_id；额外 1 个 fuzz 测试：100 条 attempt 并发写，最终行数严格 100。

#### Task A3 —— scanner 结构化日志

- 在 `xtrade.scan.run` 入口（CLI `xtrade scan run`）顶部 `from xtrade.obs import emit_event` + `log = logging.getLogger("xtrade.scanner")`。
- 事件清单（命名空间 `scanner.*`）：
  - `scanner.run.start{run_id, universe_path, instruments_count}`
  - `scanner.signal.emitted{run_id, instrument, signal_id, decision}`
  - `scanner.signal.skipped{run_id, instrument, reason}`
  - `scanner.run.complete{run_id, instruments_count, signals_emitted, duration_s}`
  - `scanner.run.error{run_id, error}`
- 现有 `scan_summary.json` artefact 保持不变（仍是 Phase 3 契约）。
- 验收：`tests/test_scanner_log_events.py` 用 `caplog` 捕获 `xtrade.scanner` logger 的 records，断言 envelope 形状 + 至少出现 `scanner.run.start` 与 `scanner.run.complete`；regex 审计 `xtrade.scan.run` 模块源码内所有 `emit_event(log, "...")` 事件名均以 `scanner.` 起头。

#### Task A4 —— `/var/lib/xtrade` 容量告警

- `src/xtrade/ops/disk.py::check_disk(path: Path, warn_pct: int=80, halt_pct: int=90) -> DiskState`
  - `DiskState{path, used_pct, free_bytes, warning, halt}` dataclass。
  - 实现：`shutil.disk_usage(path)`。
- `collect_status()` 增 `disk` 字段；`render_status_text` / `_json` 显示一行 `xtrade.ops disk used_pct=63 warning=false halt=false`。
- `run_supervisor`：每 iteration 顶部 `disk = check_disk(config.var_root, ...); if disk.halt: sentinel.pause(reason="disk-exhausted"); emit_event("supervisor.disk.exhausted", ...)`。
- 阈值在 `SupervisorConfig.disk_warn_pct` / `disk_halt_pct` 中（默认 80 / 90），允许 supervisor.yaml 覆写。
- 验收：`tests/test_ops_disk_check.py` 用 monkeypatch `shutil.disk_usage` 锁 4 路径（正常 / warn / halt / 越界回退）；supervisor 集成测试用 fake `check_disk` 注入 halt → 断言 sentinel 文件被写 + event 被发。

#### Task A5 —— mainnet 第三锁

- `src/xtrade/live/mainnet_unlock.py::assert_mainnet_unlock(env: Mapping, unlock_path: Path) -> None`
  - 若 venue 配置中无任何 mainnet endpoint → return（no-op）。
  - 若有 mainnet：
    1. `env["XTRADE_MAINNET_UNLOCK_TOKEN"]` 必须存在且非空。
    2. `unlock_path` 必须存在 + `st_mode & 0o777 == 0o400` + `st_uid == 0`。
    3. `unlock_path` 首行 strip 等于 env token。
  - 任何不满足 → raise `MainnetUnlockError`，message 不泄漏 token。
- venue 是否 mainnet 的判定：复用 `xtrade.config._VALID_BINANCE_ENVIRONMENTS` + 增加 hyperliquid mainnet 域名列表（明确常量，便于 review）。
- `xtrade.live.run_supervisor` / `xtrade.live.run_live_signal` 在 `node` 构造之前调用 unlock check；保留 Phase 3 双锁优先于 Phase 5 第三锁的执行顺序（保留原 error message 风格）。
- 验收：`tests/test_mainnet_unlock.py` 覆盖 4 路径（无 env / 无文件 / 内容不等 / 文件 mode 0644）+ no-op 路径（纯 testnet）；不在 git 中写真实 unlock token。

#### Task A6 —— installer hardening（2026-05-24 VPS 首装实测发现）

**背景**：2026-05-24 / 25 在 OpenCloudOS 9.x VPS 上首次跑 `scripts/phase4/install_vps.sh` 共暴露 7 个 bug（其中 5 个是阻断性的），使 `xtrade-supervisor.service` / `xtrade-bridge.service` 在 `systemctl enable --now` 后无法启动；手动绕过逾 2 小时才上线。这些问题在 Phase 4 brief / runbook 都未涵盖，须在 Phase 5 收口。

**Bug 1 — uv Python 工具链落在 `/root` 下，xtrade 用户无法 traverse**

- 现象：`/opt/xtrade/.venv/bin/python3` → `python` → `/root/.local/share/uv/python/cpython-3.12-linux-x86_64-gnu/bin/python3.12`；`/root` 默认 mode `0550 root:root`，`xtrade` 用户 traverse 失败；systemd 启动报 `status=203/EXEC Failed to execute /opt/xtrade/.venv/bin/xtrade: Permission denied`。
- 根因：`install_vps.sh` line 117 调用 `uv python install 3.12` 时未设置 `UV_PYTHON_INSTALL_DIR`，uv 默认写到调用者的 `~/.local/share/uv/python`（root 即 `/root/...`）。
- 修复：
  ```bash
  # install_vps.sh 中：
  export UV_PYTHON_INSTALL_DIR=/opt/uv/python
  install -d -m 0755 -o root -g root "$UV_PYTHON_INSTALL_DIR"
  "$UV_BIN" python install 3.12 >/dev/null
  "$UV_BIN" venv --python 3.12 "$OPT_XTRADE/.venv"
  ...
  chmod -R a+rX /opt/uv "$OPT_XTRADE/.venv"
  ```
- 兜底：installer 最后增加 self-test：`sudo -u "$XTRADE_USER" "$OPT_XTRADE/.venv/bin/python3" -c 'print("ok")'`，失败立即报错指向修复 PATH。

**Bug 2 — `/etc/xtrade/supervisor.yaml` 从未被 seed**

- 现象：systemd unit `ExecStart=...xtrade live supervise --config /etc/xtrade/supervisor.yaml`；该文件不存在，supervisor 启动即报 `FileNotFoundError`。
- 根因：`install_vps.sh` line 133–142 的 config seed 循环只覆盖 `venues.*.yaml`、`universe.yaml`、`risk.yaml`，没有 supervisor.yaml；仓库 `config/` 下也没有 `supervisor.example.yaml`。
- 修复：
  1. 新增 `config/supervisor.example.yaml`，包含完整 schema 注释（`instrument_id` / `strategy_name` / `approval_mode` / 各路径 / `venues_yaml` / `risk_yaml` / `poll_interval_s` / `venue_timeout_s` / `safety_multiplier`）。
  2. installer seed 列表加入 `supervisor.example.yaml`，目标路径 `/etc/xtrade/supervisor.yaml`（mode `0640 root:xtrade`，已存在则跳过）。
  3. installer 末尾若 supervisor 启动失败，打印明确提示：`"EDIT /etc/xtrade/env (set BINANCE_TESTNET_API_KEY etc.) AND /etc/xtrade/supervisor.yaml (set instrument_id + venues_yaml), then run: sudo systemctl restart xtrade-supervisor"`。

**Bug 3（轻量）— Restart 风暴噪音**

- 现象：`xtrade-supervisor.service` 在 EACCES 阶段每 10 秒重启，60 次后被 systemd 限流；journal 被刷满。
- 修复：unit 增加 `StartLimitBurst=5` + `StartLimitIntervalSec=120`，超过即 `failed` 不再 restart，避免 5 分钟内刷 30 条 EACCES。

**Bug 5 — `xtrade bridge serve` 启动即 OOM-killed（cgroup MemoryMax=200M）**

- 现象：`xtrade-bridge.service` 在新装 VPS 上启动 ~10s 后被 systemd OOM killer 终结：`Memory: 198.2M (max: 200.0M ... peak: 199.6M)` → `status=9/KILL Failed with result 'oom-kill'`；同一容器内的 supervisor 用 `MemoryMax=1500M` 反而正常。bridge 是个 stdlib HTTP 监听器，理论上几十 MB 就够。
- 根因：CLI 顶层 `import xtrade.cli` 强制导入 vectorbt/numba（research stack），而 `xtrade bridge serve` 是 webhook receiver 完全不需要这些重量级依赖。冷启动 vectorbt + numba JIT 初始化轻松吃满 ~200 MB。
- 修复方向（择一或同时）：
  1. **CLI 懒加载**：把 `xtrade.cli` 顶层的 `import xtrade.research.*` / `import vectorbt` 全部下推到具体子命令 `scan` / `backtest` 函数体内，使 `xtrade bridge serve` / `xtrade ops *` / `xtrade live supervise` 启动只载入 stdlib + nautilus 核心（目标 RSS < 80 MB）。
  2. **unit 短期兜底**：把 bridge unit `MemoryMax` 从 200M 调到 512M（或安装脚本计算 nautilus 基线 + 100 MB headroom）。
  3. 在 `tests/test_cli_import_footprint.py`（新增）锁住：`python -X importtime -c 'import xtrade.cli'` 输出不得包含 `vectorbt` 或 `numba` 行（regex 守护）。
- 验收：fresh VPS 上 `systemctl start xtrade-bridge` 后 30s 仍然 active，`Memory: <100M`；同时 `xtrade bridge serve --help` 在 Mac 本地 `/usr/bin/time -l` 测得 RSS < 80 MB。

**Bug 4 — numba JIT cache locator 在 systemd 沙箱下无落点**

- 现象：Bug 1 修好后，supervisor 进入 Python 模块加载阶段崩在 vectorbt → numba `caching.py:423`：`RuntimeError: cannot cache function 'set_seed_nb': no locator available for file '/opt/xtrade/.venv/lib/python3.12/site-packages/vectorbt/utils/random_.py'`。
- 根因：numba 的 locator 链依次尝试 in-tree（venv 站点目录，`ProtectSystem=strict` 只读 → fail）→ user-wide `~/.numba`（`ProtectHome=yes` + `HOME` 不可写 → fail）→ `NUMBA_CACHE_DIR`（未 set → fail）。三条路径全堵死即 raise。
- 修复：
  1. installer 在 `/var/lib/xtrade` 下创建 `numba_cache/`（owner `xtrade:xtrade`，mode `0750`）；
  2. installer 在 seed 后的 `/etc/xtrade/env` 追加默认行（已存在则跳过）：
     ```
     NUMBA_CACHE_DIR=/var/lib/xtrade/numba_cache
     HOME=/var/lib/xtrade
     ```
     `HOME` 兜底其他库（如 polars / matplotlib）的 `~/.cache` 探测；`/var/lib/xtrade` 本来就在 `ReadWritePaths=`。
  3. `scripts/phase5/01_check_phase5_prereqs.sh` 增加 import-side-effect self-test：`sudo -u xtrade NUMBA_CACHE_DIR=/var/lib/xtrade/numba_cache HOME=/var/lib/xtrade /opt/xtrade/.venv/bin/python3 -c 'import vectorbt; print("vbt ok")'`。

**Bug 6 — env 变量命名不一致：env 模板 vs venues YAML**

- 现象：populate `/etc/xtrade/env` 时 operator 按 `deploy/env/xtrade.env.example` 写入 `BINANCE_TESTNET_API_KEY=...`，但 supervisor 启动报 `MissingCredentialError: BINANCE_FUTURES_TESTNET_API_KEY ...`，因为 `config/venues.binance_futures.testnet.yaml` 中 `api_key_env:` 引用的是**带 FUTURES 前缀**的名字。
- 根因：env 模板（`deploy/env/xtrade.env.example`）与 venues YAML（`config/venues.binance_futures.testnet.yaml`）由不同任务在不同 phase 写就，命名约定从未统一；同理可能存在 `BINANCE_SPOT_TESTNET_*` / `HYPERLIQUID_TESTNET_*` 等变体。
- 修复：
  1. 选定**唯一权威命名**：以 venues YAML 的 `api_key_env` 值为准（更精细化、按 venue product 拆分），即 `BINANCE_FUTURES_TESTNET_API_KEY` / `BINANCE_FUTURES_TESTNET_API_SECRET` / `BINANCE_SPOT_TESTNET_API_KEY` / `BINANCE_SPOT_TESTNET_API_SECRET` / `HYPERLIQUID_TESTNET_PRIVATE_KEY` / `HYPERLIQUID_TESTNET_WALLET_ADDRESS`。
  2. 改 `deploy/env/xtrade.env.example` 用上述权威命名；`tests/test_deploy_env_template.py` 的 `REQUIRED_KEYS` 同步更新。
  3. 新增 `tests/test_env_yaml_consistency.py`：扫 `config/venues.*.yaml` 抽出 `*_env:` 引用，与 env 模板 keys 做集合差，缺一即 fail。

**Bug 7 — `MissingCredentialError` 报错路径写死，误导 operator**

- 现象：错误信息显示 `Please add ... to your .env file at /opt/xtrade/.venv/lib/python3.12/.env`，但 VPS 实际加载的是 `/etc/xtrade/env`（由 systemd `EnvironmentFile=` 注入，不是 python-dotenv 读盘）。operator 按提示去 venv 内创建 `.env` 文件毫无作用。
- 根因：`xtrade/utils/env.py`（约 `_ENV_PATH` 常量附近）硬编码相对 `__file__` 的 `.env` 路径，没有感知 systemd 部署模式。
- 修复：
  1. 让 `MissingCredentialError` 的 hint 文本根据 `os.environ.get("XTRADE_ENV_FILE")` 切换（systemd unit 注入 `Environment=XTRADE_ENV_FILE=/etc/xtrade/env`）；
  2. 没有该 hint 时 fall back 到当前 cwd 的 `.env`（dev 模式行为不变）；
  3. 加 unit test 覆盖两种模式。

**验收**

- `tests/test_install_vps_script.py`（新增）：lint installer 脚本中存在 `UV_PYTHON_INSTALL_DIR` export 与 `supervisor.example.yaml` seed 行；regex 锁住路径不再为 `~/.local`。
- `tests/test_env_yaml_consistency.py`（新增）：env 模板与 venues YAML 引用集合完全一致。
- VPS 复现：在干净 OpenCloudOS 9.x VPS 上 `bash install_vps.sh --release-tarball ...` 一次跑通到 `xtrade-supervisor.service` `active` 状态（除 `/etc/xtrade/env` 凭据外无需任何手动步骤）；命令日志归档到 `docs/phase5_results.md` §A6。
- 旧 VPS（已踩到 bug 的本机）：可选回归测试，`uninstall_vps.sh` 完全清理后再走一遍 installer。

### Track B

#### Task B1 —— news 情绪 feature pipeline

- `xtrade.research.signals.news.pipeline.build_sentiment_features(instrument, since, until, sources, scorer="vader") -> Path`
  - 输入数据：从 `data/research/news_raw/<source>/<date>.{jsonl|parquet}` 读（offline 假定数据已被 phase1 / phase5 拉取脚本拉好，本任务**不**实现 HTTP 拉取以避免依赖外部服务）。
  - 输出：`data/research/news_sentiment/<instrument>/<YYYY-MM-DD>.parquet`，schema `{ts: int64 ns, instrument: str, source: str, raw_score: float64, normalized_score: float64, n_articles: int32}`。
- `xtrade.research.signals.news.scorers`：
  - `VaderScorer`（首选，纯 Python 词典）
  - 可选 `TransformerScorer`（lazy import HuggingFace；不强制依赖；optional dep marker `[research-ml]`）
- 实例 / 行业 mapping：用 `xtrade.data.instruments` 现有 venue → symbol → underlying string；映射文件 `config/research/news_keywords.yaml` 用 `<instrument>: [<keyword>, ...]` 给查询关键词。
- 验收：`tests/test_research_news_pipeline.py` 用 fixture corpus（仓库内置 3 篇 mini article jsonl）锁 schema、行数、normalized_score ∈ [-1, 1]、空 corpus 输出空 Parquet。

#### Task B2 —— feature/label 数据集

- `xtrade.research.dataset.build.build_dataset(instrument, ohlcv_root, sentiment_root, horizons_min, train_window, val_window, test_window) -> DatasetBundle`
  - `DatasetBundle{X_train, y_train, X_val, y_val, X_test, y_test, index_train/val/test, feature_names, target_horizon_min}`。
  - Features：rolling returns (5/15/60m)、rolling vol、最新 sentiment normalized_score、sentiment 滞后 1h。
  - Label：`forward_return_{horizon}m` 二分类（涨 / 跌）或回归（默认二分类）。
  - 时序切分：3 段连续时间窗口 train / val / test；test_start > val_end > train_end；空集报清晰错误。
- 验收：`tests/test_research_dataset_build.py` 用合成 OHLCV + 合成 sentiment Parquet 跑：
  - 锁切分边界（test_start > val_end > train_end）
  - 锁 feature 列固定顺序
  - 锁同 seed 二次调用结果 bit-identical
  - 锁空 sentiment 时 fallback：sentiment 列填 0.0 + 显式 warning 行（不抛）。

#### Task B3 —— baseline 模型训练

- `xtrade.research.train.run_training(dataset_bundle, model_name: Literal["lightgbm","logistic"], seed=42, params=None) -> TrainResult`
  - `TrainResult{run_id, model_path, metrics, feature_importance_df}`
  - `models/<run_id>/{model.pkl, metrics.json, feature_importance.csv, dataset_meta.json}`
  - `metrics.json` 字段：`auc, accuracy, ic, n_train, n_val, n_test, model_name, seed, params, feature_names, train_window, val_window, test_window`
- CLI 入口：`xtrade research train --instrument <id> --model lightgbm` （新增 `xtrade.cli.research`）
- 验收：`tests/test_research_train.py`：
  - 用合成 dataset 跑 lightgbm + logistic 各一次
  - 锁 metrics.json 字段完整性
  - 锁 reproducibility（同 seed → 同 auc）
  - 锁 feature_importance.csv 列名

#### Task B4 —— ML gate 接入 strategy（paper）

- 修改 `xtrade.strategies.momentum_follow`：
  ```python
  @dataclasses.dataclass(frozen=True, slots=True)
  class MLGateConfig:
      enabled: bool = False
      model_path: Path | None = None
      score_threshold: float = 0.55
      direction_check: bool = True

  class MomentumFollow:
      def __init__(self, ..., ml_gate: MLGateConfig | None = None): ...
  ```
- `on_signal()` 在规则放行之后、emit intent 之前调 `_ml_gate_score(intent, snapshot)`；得分 < threshold → drop（emit `strategy.ml_gate.suppressed` event 经 emit_event）；direction_check 启用时模型预测方向与 intent.side 不一致也 drop。
- 模型加载：strategy 构造时 lazy 加载（`joblib.load`）；预测特征向量从当前 OHLCV + 最近 sentiment Parquet（如有）现算；缺 sentiment 时回退到 0.0 + 一次 warning。
- Paper runbook：Phase 3 paper 链路重跑一次，对比 ml_gate 关闭 / 启用两次 `strategy_summary.json` 的 intent 数量与 metrics；**不**碰 testnet。
- 验收：`tests/test_strategy_ml_gate.py`：
  - 关闭时 strategy 行为与 Phase 3 完全一致（已有 strategy 测试不退化）
  - 启用 + 模型 mock 返回低分 → intent 被压制 + event 被发
  - 启用 + 模型方向相反 → intent 被压制
  - 缺 sentiment → warning + score 计算不报错

---

## 6. 安全边界（沿用 Phase 0–4，新增 Track A / B 层规则）

### 沿用

- xtrade 仍**仅与 openclaw 通信，不直连 yuanbao**。
- bridge 入口仍仅 localhost；systemd `IPAddressAllow=127.0.0.0/8`。
- 凭据仍走 `/etc/xtrade/env`，不入 git。
- payload 凭据扫描仍是 bridge 出站默认；Phase 5 audit jsonl 写之前同样过 scrub（不允许 secret 漏到 audit 文件）。
- RiskGate 仍是强制单点；Track B 的 ML gate 在 RiskGate **之后**而非之前。
- systemd hardening 全部字段保留。

### 新增

- **mainnet 第三锁**（A5）：env token + 0400 root-only 文件 + 内容相等三重；任何一项缺失即 supervisor 启动失败。
- **audit jsonl 权限**（A2）：`/var/lib/xtrade/audit/` 目录 `0750 xtrade:xtrade`；文件 `0640`；logrotate 配置同 logs。
- **ML 模型文件信任边界**（B3/B4）：`joblib.load` 的 pickle 是任意代码执行向量；约束 1）只加载 `models/` 目录下 + 2）路径在 strategy yaml 中**写绝对路径**且 supervisor 启动时 check `model.pkl` 权限 ≤ 0640 + 3）model.pkl 旁边必须有 `dataset_meta.json` 由本仓库 `run_training` 生成的（meta 不存在直接拒载）。
- **research 数据落盘隔离**：所有 Track B 输出在 `data/research/` 与 `models/` 下；`run_supervisor` / `run_live_signal` 的代码路径中**不**导入 `xtrade.research.*`（import-graph lint 加守护）。
- **不引入 ML 训练到 supervisor 进程**：训练只在本地 mac CLI；VPS 上 supervisor 只**加载**已训练好的 model.pkl 推理，不训练。

---

## 7. 决策矩阵（Phase 5 收尾时）

| 结果 | 判断 | 下一步 |
|---|---|---|
| Track A 全 PASS + Track B 全 PASS + Phase 4 §1.6/§1.7 VPS 已签字 | **进入 Phase 6（小资金 mainnet tap test）** | Phase 6 brief：小资金 mainnet 双账户（real + paper 镜像）、回撤熔断、SLA 监控、24h 守夜样本。 |
| Track A 全 PASS / Track B 部分 PASS | **有条件 进入 Phase 6** | Phase 6 mainnet 上线仅启用规则策略（ML gate 关闭），Track B 失败任务移到 Phase 6 子任务。 |
| Track A 部分 FAIL（A1 / A4 / A5 任一） | **暂停 Phase 6** | 持久 node / 容量告警 / 第三锁是 mainnet 上线前提；必须先修。 |
| Track A 部分 FAIL（A2 / A3） | **有条件 进入 Phase 6** | audit jsonl 与 scanner 日志缺失可作为 Phase 6 监控强化项，但 §1 通过/未通过格在 phase5_results.md 显式标 FAIL。 |
| Track B 全 FAIL | **进入 Phase 6 但延后 ML** | Phase 6 只上线 Phase 3 规则策略；Track B 移到独立 research 分支推进。 |
| Phase 4 §1.6/§1.7 VPS 实测**未**签字 | **不进入 Phase 6** | 即使 Phase 5 全 PASS，没有 Phase 4 VPS 签字不允许动 mainnet。Phase 5 可单独进入收尾。 |

---

## 8. 交付物

1. `src/xtrade/live/supervisor.py`（持久 node 改造）。
2. `src/xtrade/bridge/audit.py` + bridge 调用点改造。
3. `src/xtrade/obs/scanner_events.py` 与 scanner 入口改造。
4. `src/xtrade/ops/disk.py` + `collect_status` 扩展 + supervisor 集成。
5. `src/xtrade/live/mainnet_unlock.py` + supervisor / run_live_signal 调用点。
6. `src/xtrade/research/` 子包（B1/B2/B3）+ `xtrade.cli.research`。
7. `src/xtrade/strategies/momentum_follow.py` ml_gate 改造（B4）。
8. `tests/` 下 9 个新测试文件（`pytest tests/` 全绿，新增用例 ≥ 60）。
9. `scripts/phase5/01_check_phase5_prereqs.sh`、`scripts/phase5/02_disk_watch_smoke.sh`。
10. `docs/phase5_brief.md`（本文件）、`docs/phase5_results.md`（收尾）、`docs/phase5_ml_research_notes.md`（Track B 实验笔记）。
11. 在本地 mac 完成 Track B 端到端一次：news pipeline → dataset → train → ml_gate paper run，证据归档进 `phase5_results.md` §X。
12. Track A 在 VPS 上一次完整 testnet manual 复跑（持久 node + audit jsonl + disk check + 三锁齐备），journalctl 摘要归档。

---

## 9. 建议执行顺序

并行 A / B 两轨；A 内部按先稳定再观察的顺序，B 内部按数据 → 模型 → 接入的顺序。

**Track A 顺序**

1. **A5 mainnet 第三锁** —— 纯 offline 实现，无下游依赖；先落以收紧"哪天误把 venue 配成 mainnet" 的窗口。
2. **A2 bridge 出站 audit jsonl** —— 改 bridge + 新增 audit 模块；独立模块好测试。
3. **A1 持久 TradingNode** —— supervisor 内部改造；A2 已经覆盖 bridge 路径，A1 改造期间 audit jsonl 自动捕获回归。
4. **A3 scanner 结构化日志** —— scanner 是独立 CLI 入口，与 supervisor 解耦，最后做以避免分散注意力。
5. **A4 磁盘容量告警** —— supervisor + ops 同时改；放最后是因为它依赖 A1 的持久 node iteration 循环改造完成。

**Track B 顺序**

1. **B1 news 情绪 feature pipeline** —— 数据底座，独立子包，offline。
2. **B2 dataset builder** —— 依赖 B1 + 现有 OHLCV catalog。
3. **B3 baseline 模型训练** —— 依赖 B2。
4. **B4 ML gate 接入** —— 依赖 B3 产出的 model.pkl；同时是 Track B 与 Track A 的唯一接触点（strategy 在 VPS 上跑，model 在 mac 训练）。

**两轨同步点**

- A5 落地前不允许把 Track B 输出的 model 拷到 VPS（理论上 testnet 不会触发 mainnet 锁，但作为流程纪律）。
- A2 audit jsonl 落地后才把 B4 ml_gate 在 paper 链路跑（确保 strategy.ml_gate.suppressed 事件被审计到）。

---

## 10. 与 openclaw 操作员的接口约定（Phase 4 沿用 + Phase 5 增量）

Phase 4 §10 全部保留。Phase 5 增量：

```
xtrade 出站 audit jsonl（仅本地，不传 openclaw）
  路径:    /var/lib/xtrade/audit/bridge_out.<YYYY-MM-DD>.jsonl
  权限:    0640 xtrade:xtrade（目录 0750 xtrade:xtrade）
  行 schema:
    {"ts": "...Z", "level": "INFO"|"WARNING"|"ERROR",
     "event": "bridge.out.audit",
     "approval_id": "...",
     "attempt": 1..N,
     "kind": "ok"|"retry"|"fail"|"refused",
     "status_code": 200|null,
     "error": null|"...",
     "elapsed_s": 0.123,
     "dispatched_at": "...Z",
     "response_excerpt": null|"..."}

mainnet 第三锁文件（仅 root，xtrade 用户不可读）
  /etc/xtrade/mainnet_unlock        # 0400 root:root，单行 token，与 env 一致
  /etc/xtrade/env                   # 增 XTRADE_MAINNET_UNLOCK_TOKEN=<token>

ML model 文件（仅在 Phase 6 才会推到 VPS；Phase 5 仅本地）
  models/<run_id>/model.pkl                  # 0640 xtrade:xtrade
  models/<run_id>/dataset_meta.json          # 必须存在；缺则拒载
  models/<run_id>/metrics.json
  models/<run_id>/feature_importance.csv
```

---

## 参考链接

- `xtrade_plan.md`（主路线图）
- `docs/phase3_results.md`（Phase 3.5 收尾签字、testnet runbook 实测）
- `docs/phase4_brief.md`（Phase 4 范围、systemd / bridge / supervisor 约束）
- `docs/phase4_results.md`（Phase 4 offline 签字、§3 Phase 5 入口加固建议）
- `docs/phase4_runbook_vps.md`（VPS 4 个 drill 模板）
- `xtrade.live.config._assert_testnet_only`（Phase 3 双锁源码位置）
- `xtrade.bridge.openclaw_webhook.OpenclawBridge`（Phase 5 A2 audit 接入点）
- `xtrade.strategies.momentum_follow`（Phase 5 B4 ml_gate 接入点）
