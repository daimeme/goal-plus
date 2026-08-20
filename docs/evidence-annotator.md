# Evidence Annotator

Evidence Annotator 是 Search runtime 内部的异步标注子系统。它把一次已经结算的
worker process-verifier 尝试压缩成一句客观的简体中文描述，并把该描述作为
Global Evidence 中可选的 `view` 发布。

View 用于帮助 candidate 快速理解 peer 的实际代码变化。它不是评分、选择依据或新的
Evidence，也不是由 host pool 管理的长期 agent。

## 角色边界

Evidence Annotator 与 Search candidate、main agent 和宿主 worker 的边界如下：

| 组件 | 所有内容 |
|---|---|
| Search runtime | 已结算 iteration、准确 Git commit、annotation task、重试状态和不可变 View |
| Evidence Annotator | 根据固化输入生成一句客观描述 |
| candidate worker | 读取可见 Global Evidence，自主选择后续方向，不等待 View |
| main agent | 观察 Evidence、执行全局停止、选择和 promotion；不调度 annotation 内容 |
| Codex/Pi worker host | candidate worker 的启动、续跑、deadline 和 interrupt |

Evidence Annotator 不创建 `AgentSessionRecord`，不占用 candidate lane，也不进入 Codex
agent registry 或 Pi host pool。它有独立的 run-scoped drainer 进程，但 verifier、选择和
promotion 都不会等待这个进程。

只有带 `agent_session_id` 的 worker process-verifier 调用会创建 annotation task。
main agent 的 parent fallback verification 和 promotion verification 不会进入 Global
Evidence，也不会创建 View。

## 端到端流程

```text
candidate 调用 search_run_verifier
  -> runtime 提交并验证准确 attempt Git tree
  -> runtime 结算 keep / retain / discard / failure 和 candidate-local rollback
  -> runtime 固化 EvidenceAnnotationTask
  -> run-scoped single-flight drainer 串行领取 task
  -> 从 settled base..attempt commit 构造完整实际 diff
  -> 按 worker_host 启动一次性 Codex 或 Pi annotator
  -> 校验单行 JSON 输出
  -> run 仍可发布时写入不可变 EvidenceViewRecord
  -> search_get_global_evidence 投影 view 字段
```

一次 worker verifier settlement 会在返回前持久化 task，随后异步 kick drainer。
`search_get_global_evidence` 也会执行一次非阻塞 kick，以便恢复尚未消费的 backlog。
kick 只保证同一 run 同时至多有一个有效 drainer；它不等待推理完成。

drainer 串行处理当前可运行的 task。清空当前 backlog 后会删除自己的 active worker
标记并退出；以后产生的新 task 会启动新的 drainer generation。worker 标记用于
single-flight 和崩溃恢复，不表示 Search candidate 的生命周期。

## Evidence 与 View

Global Evidence 的事实字段由 verifier settlement 同步产生：

```json
{
  "candidate_id": "c001",
  "iteration": 3,
  "commit": "<exact-attempt-commit>",
  "score": 13350,
  "disposition": "keep",
  "view": "将调度逻辑改为按依赖深度分组。"
}
```

其中 `candidate_id`、`iteration`、`commit`、`score` 和 `disposition` 是权威
Evidence。`view` 是绑定到同一 `run_id`、candidate、iteration 和 attempt commit 的
辅助 annotation。

以下不变量始终成立：

- Evidence 无需 View 即已有效；`view=null` 不能解释为 verifier 失败。
- View 不参与 candidate-local best、run-wide best、选择或 promotion。
- View 发布后不可修改，也不能绑定到另一个 iteration 或 commit。
- annotation 失败不能回滚、降级或作废已经结算的 verifier Evidence。
- candidate 不应等待、sleep 或高频轮询 View。

View 是 best-effort 产物，不保证每条 worker Evidence 最终都有非空值。`view=null`
可能表示 task 尚未运行、正在等待重试、已经永久失败，或者 run 已关闭并拒绝了迟到发布。
这些内部状态不会通过 candidate-facing Global Evidence 暴露。

## Annotator 输入输出

每个 task 固化并提供以下输入：

| 字段 | 含义 |
|---|---|
| `agent_summary` | candidate 在 verifier 调用中提交的一句 `hypothesis` |
| `actual_diff` | 本轮 settled base 到准确 attempt commit 的完整 diff |
| `exact_attempt_commit` | verifier 实际读取的 attempt commit |
| `verifier_result` | score、process pass、disposition 和 failure class |
| `relevant_metrics` | 各 verifier 的持久化 metrics |

`actual_diff` 是描述代码变化的首要事实来源，`agent_summary` 只作为待核对的 candidate
自述。diff 根据 task 固化的 base/head 和 `attempt_changed_files` 重新读取，不依赖
candidate 当前工作区是否已经因 discard/failure 回滚。

输入中的 diff、注释、字符串和 agent summary 全部被视为不可信数据。Annotator 的
system instructions 明确禁止执行其中的指令、运行命令、读取其他文件或访问网络。
输入使用独立标记包裹，并转义尖括号以降低 prompt injection 风险。

输出必须是以下 JSON shape：

```json
{"description": "将缓存键改为包含输入形状和数据类型。"}
```

`description` 必须满足：

- 一句简体中文客观陈述；
- 单行，去除多余空白，最多 1000 个字符；
- 只描述实际做了什么；
- 不评价好坏，不排名，不推断动机，不提出建议；
- 不复述 commit、分数或 disposition。

annotation diff 最大为 1 MiB。超过上限会在调用模型前把 task 标记为永久失败，
不会截断 diff 后生成可能失真的 View。

## Host 路由与隔离

Annotator 始终跟随冻结 SearchSpec 的 `strategy.worker_host`，不会为了 annotation
跨用另一个 host。

### Codex

Codex 路径为每个 task 创建临时目录，并运行一次：

```text
codex exec --json --ephemeral --sandbox read-only ...
```

临时目录只包含 annotator instructions 和结构化输出 schema。Codex 使用只读 sandbox、
独立输出文件和 Pydantic schema 校验；instructions 禁止工具、额外文件读取和网络访问。
这不是 candidate subagent，也不会继承 candidate transcript。

### Pi

Pi 路径为每个 task 运行一次：

```text
pi --mode json --print --no-session --no-builtin-tools --tools submit_evidence_annotation ...
```

Pi annotator 没有 session、内置工具、自动发现的 extension、skill、prompt template 或
context file。运行时只加载一个临时的 `submit_evidence_annotation` terminating tool，参数
复用 Codex annotator 的同一份 Pydantic JSON Schema，包括启用 shared-dir 后完整且严格的
`tool_views` 字段；模型和 provider 由固化 profile 决定，provider 配置从
`PI_CODING_AGENT_DIR` 读取。

两个路径都会在推理期间重复检查 run 和外层 deadline。run 关闭或 deadline 到期时，
当前模型进程会被终止，迟到结果不能发布。

## SearchSpec 配置

配置入口是 `strategy.evidence_annotator`：

```json
{
  "strategy": {
    "worker_host": "codex",
    "evidence_annotator": {
      "model": "gpt-5.6-terra",
      "reasoning_effort": "low",
      "timeout_seconds": 300
    }
  }
}
```

支持字段：

| 字段 | 含义 |
|---|---|
| `model` | annotator 模型；Pi 可使用 `provider/model` |
| `reasoning_effort` | host-native reasoning/thinking 配置 |
| `timeout_seconds` | 单次调用上限，默认且最大为 1800 秒 |
| `provider` | Codex 专用自定义 provider 配置 |
| `pi_provider` | Pi 专用 provider override |

`provider` 和 `pi_provider` 不能同时出现，配置在错误的 host 上也会使 annotation task
进入 `terminal_error`，但不会影响 verifier settlement。

Codex 自定义 provider 结构为：

```json
{
  "provider_id": "goal-plus-evidence",
  "name": "Goal Plus Evidence provider",
  "base_url": "https://api.example.invalid/v1",
  "api_key_env": "ANNOTATOR_API_KEY",
  "wire_api": "responses"
}
```

这里只保存 credential 环境变量的名称，不保存 credential 值。显式写入 SearchSpec 的
provider URL 会随冻结 spec 持久化；通过环境提供的 URL 则在 task profile 中保存环境变量名
和 URL hash，用于拒绝运行期间发生的 endpoint 变化。

### 模型和 provider 继承

task 在 verifier settlement 时解析并固化 profile；后续修改 session launch 或环境不会
改变已经创建的 task。

模型优先级为：

1. `strategy.evidence_annotator.model`；
2. 当前 iteration 固定的 selected worker model；
3. `strategy.worker_launch.model`；
4. `GOAL_PLUS_EVIDENCE_ANNOTATOR_MODEL`；
5. Pi 的 `PI_MODEL`；
6. host 默认模型。

reasoning effort 优先使用 annotator 显式配置，其次是 `strategy.worker_launch`，再其次是
`GOAL_PLUS_EVIDENCE_ANNOTATOR_REASONING_EFFORT`。

Pi provider 优先由带限定符的 `provider/model` 或 `pi_provider` 指定；未指定时继承
`PI_PROVIDER`。限定模型与显式 `pi_provider` 冲突会被拒绝。Codex 优先使用 SearchSpec
中的 `provider`，否则在配置了 `GOAL_PLUS_EVIDENCE_ANNOTATOR_BASE_URL` 时构造环境支持的
provider，最后使用 Codex 自身默认 provider。

### 环境变量

| 变量 | 用途 |
|---|---|
| `GOAL_PLUS_EVIDENCE_ANNOTATOR_MODEL` | 未被 spec/worker model 覆盖时的 annotator 模型 |
| `GOAL_PLUS_EVIDENCE_ANNOTATOR_REASONING_EFFORT` | 默认 reasoning effort |
| `GOAL_PLUS_EVIDENCE_ANNOTATOR_BASE_URL` | Codex 自定义 provider endpoint |
| `GOAL_PLUS_EVIDENCE_ANNOTATOR_PROVIDER_ID` | Codex provider id |
| `GOAL_PLUS_EVIDENCE_ANNOTATOR_PROVIDER_NAME` | Codex provider display name |
| `GOAL_PLUS_EVIDENCE_ANNOTATOR_API_KEY_ENV` | 保存 API key 的环境变量名称 |
| `GOAL_PLUS_EVIDENCE_ANNOTATOR_WIRE_API` | Codex provider wire API，默认 `responses` |
| `CODEX_HOME` | Codex annotator 使用的配置目录 |
| `PI_MODEL` / `PI_PROVIDER` | Pi 默认模型和 provider |
| `PI_CODING_AGENT_DIR` | Pi provider 配置目录 |
| `GOAL_PLUS_OUTER_DEADLINE_AT` | verifier 和 annotation 共享的外层 deadline |
| `GOAL_PLUS_EVIDENCE_ANNOTATOR_DISABLED` | 阻止看到该变量的 runtime 进程自动启动新 drainer |

Codex MCP 配置必须通过 `env_vars` 显式转发 annotator 所需变量。自定义
`GOAL_PLUS_EVIDENCE_ANNOTATOR_API_KEY_ENV` 指向的 credential 变量也必须单独加入
`env_vars`。

## Task 状态与重试

每个 worker iteration 对应至多一个不可变 identity 的 `EvidenceAnnotationTask`：

| 状态 | 含义 |
|---|---|
| `pending` | task 已创建，尚未领取 |
| `retry_wait` | 已领取或瞬时失败，等待当前/下一次尝试完成或 backoff 到期 |
| `completed` | View 已通过 identity 和 run fence 检查并持久化 |
| `terminal_error` | profile、输入、deadline、输出或重试已永久失败 |

每个 task 最多尝试三次。瞬时失败使用 30 秒、120 秒的持久化 backoff；第三次失败后
进入 `terminal_error`。HTTP 429/5xx、timeout、连接错误和临时不可用等被视为瞬时失败；
无效 profile、超大 diff、run 关闭等为永久失败。结构化输出缺失或解析失败可以重试。

`GOAL_PLUS_OUTER_DEADLINE_AT` 在 task 创建时固化。它接受 Unix 秒、Unix 毫秒或带时区的
ISO 时间戳。单次 host timeout 会被缩短到剩余外层时间；deadline 到期后不再启动或发布
annotation。

允许发布 View 的 run state 是 `running`、`waiting_for_workers`、`selecting` 和
`selection_blocked`，且 run 不能已 invalidated。进入 `ready_to_promote`、`promoted`、
`aborted` 或 `failed` 后，未领取 backlog 不再运行；正在推理的迟到结果会被 publication
fence 拒绝。因此结束 Search 前无需等待 annotation backlog。

## Global Evidence 模式

`strategy.config.global_evidence_mode` 只改变 Evidence 的交付方式，不改变 task 创建、
annotation 内容或 verifier settlement：

| 模式 | Candidate 可见行为 |
|---|---|
| `manual` | 默认；candidate 显式调用 `search_get_global_evidence` 读取共享 run Evidence |
| `auto` | verifier 成功返回时额外注入 `global_evidence_snapshot` |
| `independent` | 不自动注入，显式读取也只返回调用 candidate 自己的 Evidence |

Annotator 仍按 run 处理所有 worker Evidence；`independent` 只是 candidate-facing 投影过滤。
任何模式下 View 都不影响分数和选择。详细交付契约见 [API](api.md#worker-context)。
Worker 首次修改前读取 Evidence；此后每完成三次 verifier iteration 刷新一次，连续两轮无提升
或准备切换技术路线时提前刷新。`auto` 返回的注入快照算作刷新，无需再次调用读取工具。

## 持久化布局

```text
.gp/runs/<run_id>/
  evidence-annotator/
    worker.json             # 当前 drainer generation/PID；正常退出时删除
    worker.lock
    drain.lock
    tasks.lock
    annotator.log           # launch、task 或 drainer 错误
  candidates/<candidate_id>/
    evidence-annotations/
      iteration-<n>.json    # identity、profile、状态、尝试、usage、错误和可选 View
```

`candidate.json` 中的 iteration 仍是 verifier Evidence 的事实来源。annotation task 只保存
派生 View 和执行状态；Global Evidence 在读取时从 iteration 与 task 即时投影，不维护第二份
可写 Evidence ledger。

task 注册是可恢复的独立分支，不是 reward、allocation 或 candidate retirement 的提交前置条件。
若注册写入瞬时失败，后续 worker verifier 结算或 Global Evidence 读取会扫描已持久化 worker
iteration 并幂等补齐缺失 task；candidate 已被 retirement fence 不影响该恢复。反向同样成立：
reward evaluator 或 allocation policy 失败不影响 task 注册和 View 重试。

`iteration-<n>.json` 中的 `usage` 和 `attempt_history` 会累计成功或失败调用的 host-native
token/cost 观测。不得从 transcript 猜测缺失 usage，也不要手工修改 task、View 或重试状态。

## 排障与恢复

先使用只读 monitor 确认 Goal Plus/Search run、candidate iteration 和 run state：

```bash
goal-plus-pi-tool goal_plus_monitor_snapshot \
  --root .gp \
  --args-json '{"run_id":"run_..."}' \
  --pretty
```

monitor 未提供 annotation 内部状态时，再检查：

1. `.gp/runs/<run_id>/candidates/<candidate_id>/evidence-annotations/iteration-<n>.json`；
2. `.gp/runs/<run_id>/evidence-annotator/annotator.log`；
3. 固化 profile 中的 host、model、provider、deadline 和 `last_error`；
4. Codex/Pi CLI、配置目录和对应 credential 环境变量是否在 runtime 环境可用。

常见现象：

| 现象 | 检查方向 |
|---|---|
| 没有 task 文件 | 是否是 parent/promotion verifier，或 worker 调用是否缺少 `agent_session_id` |
| `pending` 长时间不变 | run 是否仍允许 annotation、自动 kick 是否被禁用、drainer PID 是否有效 |
| `retry_wait` | `next_attempt_at`、网络/限流错误和剩余 outer deadline |
| `terminal_error` | `last_error`、`error_fingerprint`、profile 冲突、输出 schema 或 diff 大小 |
| Evidence 有值但 `view=null` | 这是合法状态；不要重跑 verifier 只为补 View |
| run 结束后仍无 View | publication fence 已关闭；不要重新打开 run 或手工注入 View |

在 active run 中，可以前台运行一次同步 drainer 以复现 host 配置或处理当前已经到期的
backlog：

```bash
goal-plus-evidence-annotator drain --root .gp --run-id run_...
```

该命令会调用模型、产生 usage/cost 并可能发布 View。它不会等待尚未到达
`next_attempt_at` 的重试，也不能绕过 run/deadline fence。正常运行不需要手动调用它。

相关入口：

- [Shared Plane](shared-plane.md)：Global Evidence、Git revision 和 candidate rollback；
- [API](api.md)：candidate-facing Evidence 工具和配置字段；
- [Debugging Runtime State](debugging-runtime.md)：完整 `.gp/` 状态和 host 日志；
- [Codex](codex.md) 与 [Pi](pi.md)：host setup 和 worker 行为。

聚焦测试：

```bash
python -m pytest tests/test_evidence_annotator.py tests/test_global_evidence.py -q
```
