# Lazy Value Agent 与 Near-Prune Barrier

> 状态：第一版已实现。runtime 的权威边界仍以
> [共享平面](shared-plane.md) 和 [Evidence Annotator](evidence-annotator.md)
> 文档为准；本文同时记录实现约束与后续实验设计。

本文定义在 Goal Plus Adaptive Search 中引入 LLM-based process value 的第一版架构。
目标场景是 SWE-bench 一类最终结果接近二元、公开 process verifier 无法提供稠密质量信号的
软件工程任务。设计优先复用现有 verifier settlement、value backup、allocation 和派生流程，
同时避免每次 `search_run_verifier` 都等待一次完整模型推理。

## 目标与非目标

第一版目标：

- 从 Annotator 抽取可复用的 host-neutral 结构化模型执行器，不改变 Annotator 的角色、
  task、View 或 Global Evidence 合同。
- 新增独立的 Lazy Value Agent。健康路径异步评估；只有接近剪枝或积压失控时，
  verifier 才建立有界 Value barrier。
- Value Agent 输出绝对过程价值、解释和 hindsight feedback；runtime 将其转换为现有
  `RewardEvaluation`，通过扩展后的 `ValueBackupOperator` 回传到父节点。
- 保持 hard verifier 对 keep/retain/discard/failure、candidate-local rollback、selection 和
  promotion 的唯一权威地位。
- 为未来一次模型调用同时生成 View 和 Value 保留执行器、provenance 和结果消费者接口。

第一版非目标：

- 不修改现有 Evidence Annotator prompt、输出 schema、异步 drainer 或 Global Evidence 投影。
- 不让 Value Agent 创建 `AgentSessionRecord`、占用 candidate lane 或进入 host worker pool。
- 不实现模型参与的 allocation decision、原子 action proposal、主动探索份额或新的
  frontier-width 策略。
- 不让 hidden fail-to-pass 测试、SWE-bench 最终标签或由其派生的信号进入 Value Agent 输入。
- 不等待全部 Value backlog 后再执行 hard selection 或 promotion。

## 核心决定

整体结构分为执行、领域输出和消费三个层次：

```text
HostStructuredModelExecutor
  |-- CodexStructuredExecutor
  `-- PiStructuredExecutor

EvidenceAnnotatorFacade
  -> existing EvidenceAnnotationTask
  -> existing EvidenceViewRecord
  -> Global Evidence

EvidenceValueAgentFacade
  -> EvidenceValueTask
  -> ValueAssessment
  -> RewardEvaluation
  -> ValueBackupEvent / ValueProjection
  -> AllocationDecision
```

共享的是模型执行能力，而不是 Annotator 的角色或发布合同。View 始终是客观、可选、
worker-visible 的 Evidence 辅助描述；Value 始终是 runtime-private 的调度输入。

## 结构化模型执行器

### 职责

从现有 Codex/Pi Annotator adapter 抽取一个通用执行器，负责：

- 冻结的 host、model、provider 和 reasoning 配置；
- Codex ephemeral/read-only 调用与 Pi one-shot/no-session 调用；
- system instructions、user prompt 和严格 JSON Schema；
- timeout、outer deadline、进程终止和瞬时/永久错误分类；
- prompt-injection 边界、输入大小限制和临时目录隔离；
- token、cost、monitor 和 invocation provenance 采集。

建议的最小接口：

```python
class StructuredModelRequest:
    task_kind: str
    profile: ResolvedStructuredModelProfile
    instructions: str
    prompt: str
    output_schema: dict[str, object]
    identity: dict[str, object]
    timeout_seconds: int
    outer_deadline_at: str | None


class StructuredModelResult:
    payload: dict[str, object]
    usage: dict[str, int | float]
    invocation: ModelInvocationRecord
```

执行器不读取或写入 Search run state，不发布 View，不计算 reward，也不生成 allocation。
task owner 在调用前执行 run/deadline fence，在调用后校验 identity 并持久化领域结果。

### Annotator 兼容性

`CodexEvidenceAnnotator`、`PiEvidenceAnnotator` 和 host router 改为执行器的薄 facade。
它们继续使用当前 `EvidenceAnnotationOutput`、task 状态、重试、drainer、View publication
和目录布局。执行器抽取必须先通过现有 Annotator 单元测试，证明命令参数、schema、usage、
错误分类和 publication fence 没有行为变化。

## EvidenceValueTask

Value task 绑定一次已经完成 hard settlement 的 worker iteration：

```text
task_id
run_id / candidate_id / iteration
attempt_base_commit / attempt_commit
parent_node_id
task_context_ref / task_context_sha256
evaluator_name / evaluator_version / config_hash
resolved model profile / prompt version / schema version
state / attempts / next_attempt_at / error
assessment / usage / invocation provenance
reward_id / value_backup_event_id
barrier recommendation and snapshot
```

task identity 至少包含 candidate、iteration、准确 attempt commit 和 evaluator config hash。
相同 identity 的注册、执行和结算必须幂等；identity 冲突必须拒绝。

Value task 采用与 Annotator 兼容的 durable 状态语义：

```text
pending -> retry_wait -> completed
                     `-> terminal_error
```

`completed` 表示结构化模型输出已经持久化。`reward_id` 和
`value_backup_event_id` 表示下游消费进度；进程在模型输出后、reward 绑定前崩溃时，恢复逻辑
扫描 completed task 并幂等补齐消费。

### 输入

Value context 只从准确 Git commits 和已持久化 iteration 重建，不读取 candidate 当前
workspace 内容。worker 可以在异步推理期间继续下一轮修改，而不会改变 task 输入。

第一版输入包括：

- 原始任务或冻结 objective；
- 本轮 base 到 attempt commit 的实际 diff；
- candidate 初始基线到 attempt commit 的累计 diff；
- 当前 lineage 中已经结算的 hypothesis、公开测试和失败摘要；
- changed files、公开 verifier metrics 和可见代码上下文；
- 当前输入的限制和省略信息。

第一版不提供 hidden grader、最终 SWE-bench 标签、其他完成 run 的结果、prior Value score
或 peer Value。`parent_node_id` 用于 runtime 映射 reward，不进入模型 prompt，避免评分锚定。

### 输出

```json
{
  "value": 34,
  "explanation": "修改覆盖主要调用路径，但空输入分支仍缺少处理。",
  "hindsight_feedback": "从父节点重新扩展时检查空输入和缓存失效语义。",
  "limitations": ["未观察到隐藏 fail-to-pass 测试"]
}
```

`value` 是 `[-100, 100]` 内的整数，表示当前 attempt 作为剩余固定预算内继续解决问题的
起点所具有的绝对过程价值。它不是 hidden tests 通过概率，也不是最终 selection score。

## Lazy Settlement 流程

LLM reward 只改变 verifier settlement 的派生分支。hard settlement 仍先完成并持久化：

```text
candidate 调用 search_run_verifier
  -> runtime 固化并验证 exact attempt commit
  -> 执行公开 process verifier
  -> hard settlement transaction
       disposition / rollback / results ledger
       append IterationRecord
       register EvidenceValueTask
       persist ValueBarrierRecommendation
  -> release run transaction
  -> async path 或 barrier path
```

普通 `metric_progress` evaluator 保持当前同步顺序，不创建 Value task。

### 健康路径

```text
hard settlement
  -> non-blocking kick Value drainer
  -> search_run_verifier 返回 value_status=pending
  -> worker 可以立即开始下一轮

Value Agent 完成
  -> 短 run transaction 绑定 RewardEvaluation
  -> 创建并重放 ValueBackupEvent
  -> 更新 graph/value projection
  -> 不生成 allocation decision
```

异步完成只更新 value layer，不退休 candidate。否则低分结果可能在 worker 已经开始下一轮修改时
突然 fence candidate，造成在途工作与 runtime 状态冲突。

### Near-Prune Barrier 路径

```text
hard settlement
  -> foreground resolve candidate tasks through current iteration
  -> 按 iteration 顺序绑定 reward 和 backup
  -> 调用现有 allocation decision 流程
  -> search_run_verifier 返回 reward 和可选 allocation decision
```

模型等待发生在 run transaction 之外，但仍位于当前 candidate 的 verifier 边界内。
barrier 只等待准确 Value tasks，不等待 candidate worker、Annotator 或其他 host lifecycle。

## Barrier 决策

barrier 必须与 allocation 使用相同的统计量和阈值，不能在 runtime 复制一套近似低分规则。
建议让支持 deferred reward 的 allocation policy 实现：

```python
class DeferredRewardAllocationPolicy(Protocol):
    def barrier(
        self,
        context: AllocationContext,
        pending: PendingValueState,
    ) -> ValueBarrierRecommendation: ...

    def decide(
        self,
        context: AllocationContext,
        params: dict[str, object],
    ) -> AllocationRecommendation | None: ...
```

`value_guided_replace/v2` 在以下任一条件成立时返回 `await_through_current`：

- 再出现一次低 reward 就可能满足连续低分或近期低分比例条件；
- lane UCB 已进入 retirement threshold 的 guard band；
- 当前 policy 已有足够历史样本，但需要一个 fresh value 决定是否 retirement；
- pending task 数达到 `max_pending_per_candidate`；
- 最早 pending task 超过 `max_reward_staleness_seconds`。

其余情况返回 `async`。pending 或 error reward 不能按低分计数。

建议初始配置：

```yaml
strategy:
  orchestration_mode: adaptive_search
  adaptive_search:
    reward:
      name: evidence_llm_value
      version: 1
      params:
        model: null
        reasoning_effort: low
        timeout_seconds: 180
        scheduling:
          mode: lazy_near_prune
          max_pending_per_candidate: 1
          max_reward_staleness_seconds: 180
    value_backup:
      name: discounted_mean_best
      version: 2
      params:
        discount: 0.9
        best_weight: 0.25
    allocation:
      name: value_guided_replace
      version: 2
      params:
        min_attempts: 3
        window_size: 3
        retire_threshold: 0.1
        barrier_guard: 0.1
```

freeze validation 必须拒绝 lazy LLM reward 与不支持 barrier 的 allocation policy 组合。

## 并发、顺序与锁

### 同一 candidate

模型执行可以与下一轮 worker 工作重叠，但 reward 必须按 iteration 顺序消费。每个 candidate
保存 `value_settlement_watermark`；更晚 task 即使先完成，也只能等待此前 task 进入
`completed` 或 `terminal_error`。

第一版使用 candidate-scoped single-flight Value drainer。同一 candidate 至多一个模型调用正在
执行，不同 live candidate 可以并行执行 Value Agent。active candidate 数仍由
`budget.max_parallel` 限制；Value Agent 不占 Search slot。

### Run transaction

以下操作必须短暂持有 run transaction：

- task 注册和 iteration identity 绑定；
- completed assessment 到 RewardEvaluation 的绑定；
- backup event、graph/value projection 和 allocation decision 的原子持久化。

模型启动、推理、timeout 等待和重试 backoff 不能持有 run transaction。

### 异步完成顺序

跨 candidate 的模型完成顺序可以不同。backup event 使用 source iteration 的持久化时间和稳定
event id 重放，不使用模型返回先后作为价值语义。allocation 始终从 decision transaction 当时的
durable graph/value/resource snapshot 读取，并把 snapshot 固定到 decision。

## Reward 与 Value Backup

模型输出先归一化：

```text
attempt_value = model.value / 100
parent_value  = parent node settled_value，virtual root 默认 0
attempt_reward = attempt_value - parent_value

settled_value = attempt_value   when disposition is keep/retain
settled_value = parent_value    when disposition is discard/failure
```

`RewardEvaluation.components` 第一版保存 `attempt_value`、`parent_value`、Value 调用耗时和
usage 派生的有限数值观测；`assessment_ref` 指向完整解释，不把文本放入 numeric components。

`discounted_mean_best/v1` 的 observed return 由 metric-progress 语义推导，直接用于 absolute
LLM value 会重复计算 accepted attempt 的增量。因此新增 v2：

```text
observed_return = attempt_value
ancestor_return = ancestor.settled_value
                  + discount^(distance-1)
                    * (observed_return - ancestor.settled_value)
```

v2 保留当前 ancestor traversal、mean return、best return、uncertainty 和 backed value 算法，
但 `ValueBackupContext` 必须显式携带 `trigger_iteration`，不能在异步回填时假定目标总是
`record.iterations[-1]`。

Value Agent 失败时生成 `RewardEvaluation(status="error")`，并把该节点的
`settled_value` 回退到 parent value，以保持图上的 value 连续；不生成 attempt reward、backup
或 allocation，且不把错误计为低分。

## 解释与父节点再扩展

`ValueBackupEvent` 保存 `assessment_ref`。数值 return 按现有算法传播到全部祖先；第一版只把
`distance=1` 的 qualitative feedback 绑定给准确直接父节点，避免把局部修改建议传播到失去上下文
的更远祖先。

allocation 选择 source 时，在不可变 `ExpansionSource` 中冻结：

- source 节点自己的 direct assessment ref；
- 从该 source 出发的一个或多个 direct hindsight feedback refs；
- 已有 verifier-backed commit、artifact、model provenance 和 backed value。

派生 worker 可以在 `ExpansionActionContext` 中读取明确标记为 Value Agent 判断的 feedback，
但不读取数值 value、peer 排名或 Value Agent 的内部选择。feedback 不进入 Global Evidence，
主 agent 不编辑、选择或补写它。

## 失败与恢复

- task 注册、模型执行、reward 绑定、backup 和 allocation 均按稳定 identity 幂等。
- 模型 timeout、provider 错误或无效输出 fail-open，不影响 hard settlement、Evidence、selection
  或 promotion。
- hard barrier 受 task timeout 和 outer deadline 限制；超时后返回无 allocation 的结果，
  不能无限阻塞 worker。
- terminal error 推进 candidate 的 settlement watermark，但不形成低 reward；后续 task 可以
  使用该节点继承的 parent value 继续结算。
- completed assessment 在 reward 绑定前崩溃时，由下次 drainer kick、verifier settlement 或
  monitor recovery 幂等补齐。
- run 进入不允许 value publication 的终态后，迟到 task 不再改变 graph、backup 或 allocation；
  已观察 usage/error 可以保留用于报告。
- selection 和 promotion 不等待 Value backlog；已经没有 live worker 的低 Value 结果不触发
  replacement。

## 持久化布局

第一版不移动现有 Annotator 文件：

```text
.gp/runs/<run_id>/
  evidence-annotator/                         # 保持现状
  candidates/<candidate_id>/
    evidence-annotations/iteration-<n>.json   # 保持现状
    value-evaluations/iteration-<n>.json      # task + assessment + consumption refs
  value-agent/
    workers/<candidate_id>.json
    locks/
    attempts/
    value-agent.log
```

`IterationRecord` 增加可选 `value_task_id` 和 value status/reference。旧 run 没有这些字段时仍可读；
普通 `parallel_loops` 和 metric reward run 不创建上述文件。

## Worker 与 Main Agent 合同

soft path 的 verifier report 可以返回：

```json
{
  "value_status": "pending",
  "value_task_id": "value_c001_0003_...",
  "allocation_decision": null
}
```

candidate 不等待、不轮询 Value task；没有 allocation decision 时按现有 parallel continuation
规则继续。hard barrier 返回 decision 时，main agent仍只应用 runtime 持久化的 exact actions。
main agent 不计算 reward，不判断 barrier，不主动读取 Value 文件，也不根据 Value 自行终止或派生
worker。

异步 Value completion 不增加 host notification、runtime observation bus 或 worker lifecycle API。
只有后续 verifier 安全边界可以把 late Value 转换成 retirement/expansion decision。

## 未来统一 View 与 Value

独立 `EvidenceViewRecord` 文件和统一分析任务不属于第一版。当前保留：

```text
AnnotatorFacade -> shared executor -> legacy annotation consumer
ValueAgentFacade -> shared executor -> value consumer
```

未来 View 可独立持久化后，增加：

```text
EvidenceAnalysisFacade
  -> shared executor, one model invocation
  -> AnnotationResultConsumer -> EvidenceViewRecord
  -> ValueResultConsumer      -> ValueAssessment / reward / backup
```

为保证该升级不改写下游，第一版必须遵守：

- 执行器返回通用 `StructuredModelResult` 和 `ModelInvocationRecord`；
- Annotation 与 Value prompt/schema/validator 仍是独立领域组件；
- View 和 Value consumer 可以独立成功或失败；
- Global Evidence 只读取 View consumer 的产物；adaptive engine 只读取 Value consumer 的产物；
- task/output 都携带共同 invocation provenance，但不复制另一侧领域内容。

初始统一实验只应合并 basic objective View 与 Value。启用 supplemental peer comparison 时仍使用
独立调用，因为 peer context 会使 Value 随异步完成顺序变化；shared Tool View 也应在单独验证后
再纳入统一输出。

## 实施状态

阶段 1-5 已在第一版落地：Annotator 与 Value Agent 共用结构化模型执行器，Value task durable
注册、lazy settlement、有序 reward 回填、v2 backup、near-prune barrier 和 hindsight refs 已接入
现有 runtime。阶段 6 仍是后续实验工作，不属于本次实现。

## 实施阶段

### 阶段 1：执行器抽取

- 抽取 Codex/Pi structured executor。
- 让现有 Annotator facade 使用执行器。
- 通过所有 Annotator、Codex asset 和 Pi asset 回归测试。

### 阶段 2：Shadow Value

- 增加 EvidenceValueTask、Value Agent prompt/schema 和 candidate-scoped drainer。
- 每次 settlement 异步评分，但不生成 RewardEvaluation、backup 或 allocation。
- 在外部 hidden grader 完成后评估区分度、校准、延迟、成本和潜在错误剪枝率。

### 阶段 3：Late Reward 与 Backup

- 增加有序 task consumption 和 settlement watermark。
- 实现 `evidence_llm_value/v1` 与 `discounted_mean_best/v2`。
- 异步完成只更新 reward、backup 和 projection。

### 阶段 4：Near-Prune Barrier

- 实现 `value_guided_replace/v2.barrier()`。
- hard path foreground resolve through current iteration。
- 只在 verifier 安全边界生成 allocation decision。

### 阶段 5：Hindsight Feedback

- backup event 和 expansion source 增加 assessment refs。
- derived worker context 注入有界的 direct-parent feedback。
- 数值 Value 继续保持 runtime-private。

### 阶段 6：统一分析实验

- 先实现独立 Evidence View artifact 和消费者。
- 增加一次调用、多输出、独立 validation/settlement 的 EvidenceAnalysisFacade。
- 与双调用方案比较质量、延迟、成本和 View 客观性。

## 验证要求

单元和集成测试至少覆盖：

- 执行器抽取前后 Annotator 命令、输出、usage、重试和 View publication 行为相同；
- healthy path 不等待模型，report 返回 pending，worker 可继续；
- near-prune、pending 上限和 staleness 分别触发 barrier；
- 模型推理期间不持有 run transaction，其他 candidate 可以结算；
- workspace 继续修改后，Value 输入仍绑定原 exact commit；
- 同 candidate task 乱序完成但 reward/backup 按 iteration 顺序消费；
- async completion 不生成 allocation；hard barrier 可以生成并返回 decision；
- late reward backup 使用准确 `trigger_iteration`；
- timeout、无效输出、provider 错误和中途崩溃全部 fail-open 且可恢复；
- Value、explanation 和 assessment refs 不出现在 Global Evidence；
- hidden grader 信息不进入 task、prompt、candidate workspace 或 worker context；
- selection/promotion 与 Value backlog 无关；
- metric reward 和普通 `parallel_loops` 行为不变；Value 结构只按当前代码写入和读取，不提供旧
  Value schema 的迁移或双读。

在线实验使用固定 token、时间、candidate 和 verifier 预算，至少记录：

- externally-scored resolved rate、Pass@k 和首次正确 patch 的成本；
- Value 调用延迟、pending 深度、barrier 命中率和 verifier 被阻塞时间；
- low-value lane 的额外 speculative iteration 数；
- source selection 后代成功率和错误剪枝率；
- Value Agent token/cost 占总搜索成本的比例。

## 必须保持的不变量

- hard verifier settlement 先于任何 reward、backup 和 allocation。
- LLM 推理永不持有 run transaction。
- Value 缺失、迟到或失败不能改变硬 Evidence、rollback、selection 或 promotion。
- async Value completion 不直接退休 candidate。
- allocation 只发生在 verifier 安全边界，并固定完整 decision snapshot。
- task、reward、backup、View 和 allocation 都绑定准确 candidate、iteration 和 commit。
- runtime 拥有 reward、backup、allocation 和持久化；host 继续拥有 worker lifecycle。
- Global Evidence 不包含过程 Value、排名或隐藏答案派生信号。
