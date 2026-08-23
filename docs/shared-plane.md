# 共享平面

Goal Plus 是面向长时间 agent 任务的宿主中立运行时。普通任务停留在 Goal
Mode；具有明确可度量目标的优化任务进入 Search Mode。Search Mode 会先冻结评价
合同，再让多个隔离、长期运行的 candidate 并行探索。

Search Mode 的核心是持久化的共享平面。共享平面不共享 agent 的私有推理，也不共享
可写工作区。它只共享冻结合同、精确 Git revision、verifier 支持的 Evidence、异步
生成的客观 View 与可选开放式补充评价，以及选择和提升结果所需的持久化状态。

## 总体架构

```text
main agent
  |  发现并冻结 SearchSpec，分配初始 candidate
  |  观察持久化结果，执行全局停止策略，选择并提升结果
  v
+------------------------- 共享运行时平面 -------------------------+
| Goal 记录与冻结 SearchSpec                                      |
| candidate 工作区与 Git revision                                 |
| verifier-backed Global Evidence 与异步 View                     |
| candidate-local best、全局 best、报告与 promotion               |
+------------------------------------------------------------------+
       ^                         ^                         ^
       |                         |                         |
 candidate c001             candidate c002             candidate c003
 隔离工作区                 隔离工作区                 隔离工作区
 自主长期循环               自主长期循环               自主长期循环

宿主平面：启动、wait-any、continuation、deadline、interrupt、原生日志
```

各组件的职责边界如下：

| 所有者 | 职责 |
|---|---|
| Search runtime | 冻结合同、物化工作区、提交 Git 和 verifier Evidence、回滚、选择、报告与 promotion |
| candidate worker | 选择自己的技术方向、修改自己的工作区、调用 verifier、提交 handoff |
| main agent | triage、spec discovery、初始 candidate 分配、全局停止、最终收尾、确认 verifier 失效 |
| Codex 或 Pi 宿主 | 实际 worker 启动、等待、续跑、deadline、interrupt 和原生 transcript |
| Evidence annotator | 对每个已结算尝试生成客观描述，并基于动态 Evidence 快照生成开放式补充评价与 peer 比较 |

Search runtime 不是 worker supervisor。`AgentSessionRecord` 只记录上下文、来源和
宿主 launch payload，不表示进程是否存活。Pi 的 pool 状态因此单独保存在
`.gp/host-pools/pi/`；Codex 生命周期由原生 agent registry 管理。

## SearchSpec 与评价合同

一个 `run_id` 绑定一份不可变的 `SearchSpec`。它至少包含：

- 可度量的目标、`metric_name` 和方向；
- `source_path` 与允许修改的文件范围；
- process verifier 与 promotion verifier；
- 初始并发宽度、工作区后端和 worker 执行策略；
- 可选的用户模型选择 `strategy.models`；
- verifier 所依赖的源码内 artifact。

当前逻辑由 main agent 在 Spec Discovery 中读取任务说明、公开数据和评分工具，然后
构造或选择 verifier，并调用 `search_freeze_spec`。Runtime 会：

- 使用 Pydantic 校验 SearchSpec 结构；
- hash-pin verifier artifact；
- 在一次性源码副本中预执行 ranking verifier；
- 检查退出码、数值 metric、超时和工作区副作用。

Runtime 不理解具体 benchmark 的语义，也不会自动证明本地 verifier 与官方 Judge
等价。公开评分公式、有效性条件、每例资源限制和聚合方式是否完整进入 verifier，仍由
SearchSpec 的创建者负责。隐藏数据只能作为明确声明的近似项，不能被无提示地替换为
另一套权威指标。

SearchSpec 不包含预设软 rubric。这样 Spec Discovery 阶段不会因为 MainAgent 过早猜测
任务维度而固定后续注意力、假设和代码基线。

Benchmark 机制消融由 controller 设置
`GOAL_PLUS_SUPPLEMENTAL_EVALUATION_ENABLED` 和
`GOAL_PLUS_SUPPLEMENTAL_EVALUATION_REQUIRED`。它们只决定 annotation task 是否要求开放式
补充评价，不向 FrozenSpec 注入 criterion。ON/OFF 都不改变硬分结算、selection、
promotion gate 或最终验收。

Evidence annotator 默认继承 Search 的 `worker_host`。需要把 annotator 作为独立机制控制
变量时，可以冻结 `strategy.evidence_annotator.host=codex` 或 `pi-rpc`；模型、provider、
home 和调用进程都按该 host 解析，不改变候选 worker 的 host。未设置该字段时继续使用
host-native 默认路径。

每条 Global Evidence View 同时保留本轮 `actual_diff` 与从候选初始基线到当前提交的
累计 `candidate_diff`。description 只描述本轮增量；开放式补充评价使用累计候选证据。
annotation task 创建时还会快照最多 8 个其他 candidate 的当前硬分最佳结算版本，每个
candidate 最多一个，固定其 candidate、iteration 和 commit，后续推理基于这份可审计
比较基线。

为判断代码变化与原始请求是否相关，annotation task 记录与当前 Search run 绑定的准确
Goal revision 引用和 SHA-256，而不复制 `raw_goal`。annotator 启动时临时解析该 revision
并校验 hash；没有绑定 Goal 时才退回 FrozenSpec objective。原始上下文只进入隔离的
annotation prompt，不进入 Global Evidence、candidate task 或报告正文；报告只可保留来源、
引用和 hash。benchmark adapter 只能提供公开任务字段，不能把 hidden judge、答案 patch
或私有评测数据写入 Goal。

冻结后的合同不能原地修改。确认 verifier 合同有误时，应先使当前 run 失效并停止其
worker，再冻结修正后的 spec，创建 successor run。旧分数不能跨合同复用。

## 一次 Search Run

一次正常的 Search run 按以下顺序执行：

1. main agent 冻结 SearchSpec 和 verifier artifact。
2. 一次初始 `SearchPlan` 分配固定 candidate lane。
3. 每个 candidate 获得独立工作区并开始自主循环。
4. 宿主以 wait-any 方式返回完成事件；较慢 lane 不阻塞其他 lane 发布 Evidence。
5. 只要全局停止条件未满足，宿主续跑同一个 candidate、原生 session 和工作区。
6. 收尾时先让所有 live worker 静止，再选择精确 commit，执行 promotion gate 并生成报告。

`budget.max_parallel` 是 spec 的初始 candidate 和 live-worker 数量。普通
`parallel_loops` run 只做一次初始分配，不按分数创建替代 candidate，也没有后续
规划轮次。用户提供 `strategy.models` 时，runtime 在冻结前通过 host adapter 验证
名称，并在初始 plan 中生成与 lane 一一对应的 `selected_models`：未写数量时轮转，
全部写数量时按数量展开。每个 selected model 随 candidate 和原生 session 的续跑保持
不变，但所有 lane 仍共享同一份 Global Evidence。

### Adaptive Search value-guided allocation

`strategy.orchestration_mode="adaptive_search"` 保留同一条主流程，只在 verifier settlement
之后由 `AdaptiveSearchEngine` 增加四个隔离步骤：注册的 `RewardEvaluator` 计算
`RewardEvaluation`，`SearchGraphProjection` 从已结算 iteration 投影搜索图，注册的
`ValueBackupOperator` 生成并重放 `ValueBackupEvent` 得到 `ValueProjection`，注册的
`AllocationPolicy` 根据 graph/value projection 决定是否产生 `AllocationDecision`。
兼容组件为 `metric_progress/v1`、`identity/v1` 和 `low_reward_replace/v1`；value-guided
组件为 `metric_progress/v2`、`discounted_mean_best/v1` 和
`value_guided_replace/v1`。未知 name/version 在 freeze 时拒绝。

Value layer 把三个概念分开持久化：`RewardEvaluation.attempt_reward` 描述本次被测尝试产生的
方向归一化即时回报；`metric_progress/v1` 保持原有结算语义，退化尝试回滚到 incumbent 后为零；
`RewardEvaluation.settled_value` 描述 keep/retain/restore 完成后的 candidate-local 绝对效用。
`SearchGraphProjection` 把每次 verifier settlement 表示为 transition：keep/retain 创建
`settled_iteration` 节点，discard/failure 结算回原节点，派生 candidate 的首条 transition 从
decision 固定的 source node 接入。`ValueProjection.node_values[].backed_up_value` 是 allocation
读取的节点值。`identity/v1` 令 `backed_up_value == settled_value`；
`discounted_mean_best/v1` 把 settled value 与 V2 负/正 attempt reward 沿实际 transition 祖先链
折扣回传，并以 own value、mean return 和 best return 的稳定混合更新节点。旧状态中的
`reward/state_value` 在读取时迁移为前两个
字段，新写入不再产生旧名称。

该模式要求 `workspace.backend="git_worktree"`、`budget.max_parallel` 和显式
`budget.max_candidates`。前者仍是 live-worker 上限，后者是 run 内可物化的唯一 candidate
上限。reward 错误和 policy 错误 fail-open：错误随 iteration 保存，但不改变硬 score、
keep/retain/discard/failure、`best.json`、selection 或 promotion。

Reward/allocation 与 Evidence annotation 是两个独立的结算后分支，互不作为前置条件。
annotation task 写入或启动失败不会阻止 reward、retirement 或 expansion decision；reward evaluator
或 allocation policy 失败也不会阻止 annotation task 注册。Runtime 会在后续 worker verifier
结算和 Global Evidence 读取时，根据已持久化 iteration 幂等补齐缺失 task。

兼容 low-reward policy 只检查触发 candidate 的连续低 reward。value-guided policy 在最小样本数
后计算近期 reward mean、standard error、探索 bonus 和 lane UCB；只有近期非正收益比例与 UCB
同时满足退休条件才产生替换。命中后，runtime 在一个不可变 decision 中持久化配对的
`retire_candidate + expand_candidate` actions，立即 fence 被退休 candidate，并把派生来源固定到
最高 `backed_up_value + exploration_bonus` 的 verifier-backed Evidence、settled Git commit 和
模型 provenance。配置 `expansion.max_unobserved_expansions_per_node` 后，已有 pending decision
以及已物化但尚无首次 verifier settlement 的 derived candidate 都会占用 source 配额；满额 source
暂时退出本次 source 选择，child 首次结算后自动释放。该配额只约束异步未观测派生，不主动创建
slot，也不构成永久分支宽度。完整 `AllocationStateSnapshot` 与 decision 一起保存，固定当时的
候选集合、未观测计数、剩余配额和其他资源状态。
`ValueProjection.unobserved_expansion_count` 只投影已物化但未结算的 child；allocation snapshot
中的未观测计数还包含已持久化但尚未物化的 expansion decision。
派生 worker 启动或续跑时，runtime 可在 `search_get_agent_context` 中动态生成
`ExpansionActionContext`。它不新增持久化 action 状态：`SearchGraphProjection.transitions` 提供
权威边与拓扑，worker `IterationRecord` 提供 hypothesis、`attempt_changed_files` 与 artifact hash，
已完成 View 按 candidate/iteration/attempt commit 精确绑定后只增强描述。`source_path_actions`
按 root 到当前 source 排序，`tried_actions` 保留从该 source 出发的 keep/retain/discard/failure
全部尝试；同 source 下相同 artifact hash 标记 `duplicate_of_transition_id`，但不折叠 verifier
成本和价值观测。action 的 `transition_id` 仍可与 `ValueProjection.edge_values` 精确关联；即时
reward、observed return 和 backed value 不复制到 worker context，继续只由 runtime allocation
消费。`NodeValueEstimate.unique_branch_count` 统计该节点出发的唯一 observed artifact hash，
物理派生数量仍由 completed/unobserved expansion count 表示。View 缺失或迟到不阻塞 context、
allocation 或 worker 启动，也不会创建新边。
该上下文只对共享 Evidence 的 adaptive 派生 candidate 可见；普通 `parallel_loops`、初始 adaptive
candidate 与 `global_evidence_mode=independent` 完全省略，避免绕过 Evidence 隔离。

当 `max_unobserved_expansions_per_node=1` 时，同 source 的下一次派生必须等待前一 child 首次结算，
因此后继 worker 能看到新增 tried action 并通过 candidate task prompt 选择机制或预期效果上实质
不同的假设。它是 evidence-guided 去重而非 action planner：hypothesis/View 内容按不可信历史数据
处理，runtime 不执行其中指令。若未来允许同 source 多个未观测 child，并发 worker 仍可能选择
相近方向；硬性避免这种并发重复需要后续的原子 action proposal/reservation 接口。
decision 对外可见前，触发它的 iteration 已完成 Git/results ledger 结算。retirement 只禁止该
candidate 进入下一轮 iteration，不会删除其 Evidence、取消 annotator 或把它排除出 Global
Evidence；annotation task 即使在 retirement 后补注册，客观 View 仍会异步生成并绑定到同一条
Evidence。与普通 iteration 一样，task 尚未注册或 View 生成期间都可暂时显示 `view=null`，调用方
不轮询。
`search_apply_allocation_decision` 幂等物化子 workspace、继承父 results ledger 与 run-global
Evidence 可见性、创建 `AgentSessionRecord`，然后返回 host-native launch payload。runtime 不启动、
等待或中断 worker；Codex/Pi 主 agent 只执行 decision，host supervisor 不计算 reward、选择来源或
自动 refill。

allocation decision 保存在 `.gp/runs/<run_id>/allocation-decisions/*.json`，不可变 backup event
保存在 `.gp/runs/<run_id>/value-backups/*.json`，reward 保存在对应 `IterationRecord`，重放后的
搜索图保存在 `.gp/runs/<run_id>/adaptive-search/search-graph.json`，重放后的值保存在同目录的
`value-projection.json`。两者都可从 candidate iteration 与 backup event 重建，candidate record
不保存派生节点缓存，也不存在单独的 action projection 文件。`best.json`、selection 与
promotion 仍只认硬 verifier score；value layer
只影响 runtime allocation。当前 engine 已预留 allocation constraint 与 reservation planner 接口，
但尚未实现原子多候选 reservation、主动最低探索份额、永久节点分支宽度或 frontier width 限制。

该模式只适用于可以合法向 worker 暴露硬 metric 的优化任务。隐藏答案 benchmark 不能把
gold correctness、score、reward 或由它们派生的 allocation decision 暴露给 worker；这类任务
继续使用公开格式 verifier、预声明的 gold-independent 聚合规则和所有答案固定后的外部评分，
不能启用 verifier-guided `adaptive_search`。

## Candidate 循环

每个 candidate iteration 只需要以下协议：

1. 调用 `search_get_agent_context` 读取自己的权威状态和历史。
2. 调用 `search_get_global_evidence` 读取当前 run 的共享视图。
3. 独立选择方向，只修改自己的工作区。
4. 启用 `shared_dir` 时回顾本轮及此前 iteration 的命令序列、临时代码片段和 scratch
   scripts。命中正向工具化信号时，从 `.tmp/tool-drafts/` 显式选择源文件并调用
   `search_stage_shared_tool`；否则记录具体排除项。
5. 调用 `search_run_verifier`，用一句 `hypothesis` 描述实际完成的尝试，并在启用
   `shared_dir` 时传入 `toolization_decision`。
6. 从 verifier settlement 返回后的工作区继续。

candidate 不需要在修改前提交 iteration plan。`hypothesis` 是完成尝试后的事实性
自述，与 verifier 结果一起保存；它不是 pending plan，也不形成协调锁。多个
candidate 可以同时读取同一版 Evidence 并并发工作。

工具化的正向信号是：重复或等价的多步流程、非显然领域对象/边界/断言检查、解析/trace/
复现/转换/mutation 检查，以及明显降低 peer 重建成本的流程。复用范围只要求同一 run 内的
其他 candidate 有用，不要求跨项目通用；短小、任务专属、来自临时代码片段或只输出退出码
都不能单独作为低价值理由。只有单条普通命令、无逻辑 wrapper、受限产物、依赖 candidate
私有状态或与已发布快照完全相同才是具体排除项。

`ToolizationDecision` 只是 `IterationRecord` 的普通事实。缺失、声明 staged 但 staging 为空，
或声明 not_applicable 但 staging 非空会分别产生 advisory；这些字段只供 monitor/report，
不改变 score、verifier disposition、selection 或 promotion。实际 staging inventory 与现有
publication settlement 始终是权威。

main agent 不向 worker 提供后续技术方向。普通 `parallel_loops` 正常路径优先续跑同一个原生 session；
redispatch 只用于恢复，并继续使用同一个 candidate 工作区、Git 历史、verifier 历史
和有界 handoff。`adaptive_search` 只有在 verifier 返回 runtime 已持久化的 allocation decision
时才停止该 candidate；worker 和 host 都不能自行推断质量剪枝。

## Global Evidence

`search_get_global_evidence` 将当前 run 中已结算的 worker iteration 投影为窄表：

```json
{
  "candidate_id": "c001",
  "iteration": 3,
  "commit": "<exact-attempt-commit>",
  "score": 13350,
  "disposition": "keep",
  "view": "将调度逻辑改为按依赖深度分组。",
  "view_created_at": "2026-08-06T12:00:00Z",
  "supplemental_available": true
}
```

冻结 spec 显式启用 `shared_dir` 时，已发布工具还会以 `shared_tools` 出现在同一条
Global Evidence 中。每项包含 runtime 绑定的 `tool_view`、`tool_id`、`snapshot_hash` 与
`source_commit`。候选不会看到共享目录路径，也不能在 Tool View 生成前发现工具；它只能通过
`search_copy_shared_tool` 按精确 id/hash 复制到本地临时 inbox，再在自己的 iteration 中重新验证。
下一次 worker verifier 原子消费 copy receipt，并将采用作为该 iteration 的事实记录。工具描述与
采用结果仅是后续搜索的参考，不单独聚合收益，也不改变硬分、retain/discard、选择或 promotion。
工具化决策本身不进入 Global Evidence。完整状态流保持为：
`ToolizationDecision -> IterationRecord`，以及独立的
`staging -> passing verifier -> SharedToolRecord -> Tool View -> Global Evidence -> copy receipt -> adopted_tools`。

完整评价不在窄视图中重复返回。worker 仅在 `search_get_agent_context` 的
`supplemental_evaluation_enabled=true` 且目标行
`supplemental_available=true` 时按需调用：

```text
search_get_evidence_detail(
  agent_session_id="<current-session>",
  candidate_id="c001",
  iteration=3
)
```

工具只解析调用方当前 run 的已结算 Evidence identity；`independent` 模式只允许读取自己
candidate。返回完整 `supplemental_evaluation`，但不暴露 annotation 文件路径、peer
workspace、transcript 或推理。

verifier 会同步发布 `candidate_id`、`iteration`、`commit`、`score` 和
`disposition`。`disposition` 取值为：

- `keep`：尝试有效，并且严格改善该 candidate 的历史最佳；
- `retain`：尝试有效、硬分同分，并成为该 candidate 的最新工作基线；
- `discard`：尝试有效，但分数更差；
- `failure`：尝试未产生可用于排名的 verifier Evidence。

Global Evidence 只包含 worker 的 process-verifier 尝试。parent fallback verification
与 promotion verification 不会成为 peer Evidence。视图也不会暴露 peer transcript、
私有推理、annotation 内部任务状态或 peer 工作区路径。

EdgeBench 的 Goal Plus 模式默认通过 `GOAL_PLUS_EXTERNAL_EVIDENCE_DIR` 提供按轮保存
的外部评测附件；将该变量显式设为空字符串可关闭。Runtime 仅在其中的 `run_id`、
`candidate_id`、`iteration` 和 `commit` 与既有
process-verifier Evidence 完全一致时，将记录附加到该条目的 `external_evaluations`；
它不创建 iteration、不参与本地排名，也不成为第二份历史账本。未配置、记录不完整或
身份不匹配时，Global Evidence 行为不变。附件仍服从 `manual`、`auto` 和 `independent`
三种可见性模式。

## 客观 View 与开放式补充评价

`view` 是绑定到准确 Evidence identity 的异步、不可变、best-effort annotation。后台
drainer 按 SearchSpec 的 `worker_host` 选择 Codex 的一次性只读执行或 Pi 的一次性无工具
执行；不会为了 annotation 跨用另一个 host。Annotator 可读取：

完整的角色边界、host 路由、配置继承、重试状态和排障流程见
[Evidence Annotator](evidence-annotator.md)。

- candidate 的一句 `hypothesis`；
- 从本轮 settled base 到 attempt commit 的完整 diff；
- 精确 attempt commit；
- 本轮 changed files；
- 冻结的 process verifier 合同；
- verifier 结果与相关 metrics。

View 只用一句中文客观描述实际做了什么，不评价好坏、不推断动机、不排名，也不推荐下一
步。事实来源是 actual diff，而不是 candidate 的自述。Changed files、verifier command
和 metrics 只提供验证上下文；命令名称本身或失败的测试不能证明目标行为已经实现。

`supplemental_evaluation` 不读取 FrozenSpec 软标准。annotator 只依据当前候选累计 diff、
公开 verifier Evidence 和 annotation task 创建时固定的 peer 快照，自行提出 1–8 个与当前
任务实际相关的观察维度。它逐项给出 finding、证据与置信度，并对比较基线中的每个 peer
返回非定向的 `similar`、`different`、`tradeoff`、`complementary` 或 `unknown`，不能借此
选择赢家。没有 peer 时 comparisons 为空。limitations 明确列出公开 Evidence 无法判断的
事项。

这种评价发生在提交结算之后，因此不会在搜索开始前固定注意力方向。worker 可以把第三方
观察作为下一轮假设来源，但必须独立核对；评价不产生总分或最终推荐，不能改变硬 score、
PASS/FAIL、candidate-local 基线、run-wide 排名或 promotion gate。

annotator 收到的累计 diff 使用 Git 函数级上下文和至少 10 行普通上下文，并继续受字节
上限约束。上下文中未出现某个定义，不代表该定义不存在；这类判断必须降低置信度并写入
`limitations`。每次 worker 调用 `search_get_global_evidence`，runtime 都会在对应
`agent_sessions/*.json` 的 `global_evidence_reads` 中记录读取时间、当时 Evidence 数量、
已完成 View 的 candidate/iteration/commit 引用以及其中是否含 supplemental evaluation。
该读取记录只用于审计 View 是否在后续 verifier 之前可见，不参与候选结算或最终验收。

`view=null` 只表示 annotation 尚未发布，Evidence 本身已经有效。candidate 可以先按
自己的方向继续，不应等待、sleep 或轮询 View。

verifier settlement 和 Evidence 读取都可以触发 run-scoped annotator。一个 drainer
串行处理 backlog，但 verifier、selection 和 promotion 都不等待它。重试状态、解析后的
模型/provider、deadline、usage 和 View 都持久化保存。`ready_to_promote` 和 `promoted`
run 的 Evidence 已经不可变，因此 annotator 仍可在 outer deadline 内发布迟到 View；
invalidated、aborted 或 failed run 的发布 fence 继续拒绝修改。需要在销毁宿主环境前得到
完整 View 的 controller，可以显式同步 drain 已登记的有界重试，而不改变 SearchTools 的
选择或 promotion 语义。

## Git 与 Candidate-Local Best

Evidence 中的 commit 表示 verifier 实际读取的完整 Git tree，不是相对于初始源码的
patch。Runtime 分别记录：

- `attempt_base_git_head`：尝试前的 settled HEAD；
- `git_head`：verifier 实际验证的 attempt commit；
- `attempt_changed_files`：用于 annotation 的 `base..attempt` 净变化；
- `changed_files`：相对于原始 source 的最终 artifact 变化，用于策略检查和 promotion。

Runtime 会在验证前提交所有 candidate-controlled 修改，并要求 artifact worktree
干净。candidate 可以包含多个手工 commit；annotation 使用完整
`settled-base..attempt` 范围，而不是只查看最后一个 commit。

每次 process-verifier 尝试都会永久保留。严格改善和同分分别以 `keep`、`retain` 更新
candidate-local 最新最佳。只有 `discard` 或 `failure` 时，Runtime 才把代码恢复到此前
最佳版本，再追加不可变的
`results.tsv` ledger：

```text
keep:     settled -> attempt -> ledger
retain:   settled -> equal-score attempt -> ledger
discard:  settled -> attempt -> restore-best -> ledger
```

attempt、恢复和 ledger commit 都保持可达。因此下一轮始终从 candidate-local best
规划，而 Global Evidence 仍保留好、差和失败的全部尝试。一个 candidate 的回滚不会
改变 peer 工作区，也不会修改 peer 的判断。

`git_worktree` 后端下，各 candidate 共享一个 Git common directory。只有在代码级
证据确有必要时，candidate 才从自己的工作区渐进式查看 peer Evidence commit：

```bash
git show <commit>:<allowed-file>
git diff HEAD <commit> -- <allowed-file>
```

candidate 不应 checkout、reset 或修改 peer revision。最终 promotion artifact 虽然是
相对于 source 生成的 patch，但这不会改变 Evidence commit 代表完整代码树的语义。

## 选择、Promotion 与失效

run-wide best 从有效 iteration record 的硬 score 中计算，与各 candidate-local best 分开。
`search_select` 不读取补充评价或 peer 比较来改变排名；硬分并列按原有稳定顺序选择。它将
结果绑定到一个精确、通过验证的 worker Evidence commit。只有旧状态
或当前产物没有对应 durable Evidence 时，parent 才补做 process verifier。

promotion 是独立的验收 gate。Runtime 会检出选中的不可变 revision，在
`GOAL_PLUS_VERIFIER_PHASE=promotion` 下重跑配置的 promotion verifier，并把结果绑定
到 Git head 和 artifact hash。只有 promotion 成功，才生成可被 Git 应用的 patch；
source workspace 不会被静默修改。

worker 报告的 verifier concern 只是建议。main agent 确认缺陷后，Runtime 先原子
invalidate 并 fence 当前 run，宿主再停止所有 worker。修复后的合同必须进入 successor
run。可以继承有界研究上下文，但不能继承旧分数或把旧 Evidence 当作新合同下的结果。

## 持久化目录

```text
.gp/
  goal-plus/<goal_plus_id>/
  specs/<frozen_spec_id>/
  runs/<run_id>/
    run.json
    best.json
    plans/<initial-plan-id>.json
    candidates/<candidate_id>/
      candidate.json
      evidence-annotations/iteration-<n>.json
    agent_sessions/<agent_session_id>.json
    workspace/<candidate_id>/
    report.md
    report.html
    promotion/<candidate_id>.patch
  host-pools/pi/
```

`candidate.json` 中的 iteration record 是 Evidence 的事实来源。Annotation task 保存
可选 View 及其执行状态。Global Evidence 在读取时即时投影，不是第二份可写共享账本。
`best.json` 是当前 run 最优 verifier commit 的权威原子指针；严格改善或同分时更新，
退化和失败不会覆盖它。同分 candidate 按最后完成结算的有效 commit 决定。

## 核心不变量

- 并行工作开始前，先冻结 verifier 与编辑策略。
- 隔离可写 candidate 工作区，只共享持久化事实和 Git object。
- verifier-backed 严格改善或同分最新版本才能成为 candidate-local best。
- 回滚、选择和 promotion 后，所有精确 attempt 仍可审计。
- 客观 View 和开放式补充评价都可以延迟，但不能阻塞优化或改变最终硬验收。
- worker 生命周期和原生 transcript 不进入 Search runtime 状态。
- 一份有效合同对应一个 run，successor run 不复用旧分数。
- 全局停止依据持久化状态，而不是单个 worker 的意见或 lease 到期。

当前框架没有逐 iteration Global Plan、AtomicPlan admission、Search Space schema、
共享 chain of thought、peer-history feed，也没有 runtime-owned agent scheduler。
benchmark 专用任务、evaluator、campaign 和对比结果属于 `bench-goal-plus`；
本仓只维护可复用运行时。

具体工具见 [API](api.md)。宿主机制见
[Agent Host Adapters](agent-host-adapters.md)、[Codex](codex.md) 和 [Pi](pi.md)。
持久化状态排查见 [Debugging Runtime State](debugging-runtime.md)。
