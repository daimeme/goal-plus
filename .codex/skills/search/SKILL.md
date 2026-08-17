---
name: search
description: Codex 使用 goal-plus MCP server 的 /goal-plus 内部 Search Mode 引擎。
---

# Codex 的 Search Mode 运行时

在 `/goal-plus` 已把目标升级到 Search Mode 后使用此 skill，或用它对已经可度量的
SearchSpec 进行显式底层调试。普通用户入口是 `/goal-plus`。

使用 `goal-plus` MCP server 暴露的逻辑 `search_*` 工具。Codex 可能显示带客户端特定
前缀的 MCP 工具；按最后的逻辑工具名匹配。

## Verifier 冻结契约

不要在 `source_path` 手工运行拟定的 `ranking_signal`。将命令和 verifier 产物直接传给
`search_freeze_spec`；它会在一次性源码副本中运行预检，并确认最后一个非空 stdout 行是
JSON，其中包含有限数值类型的 `spec.metric_name`，例如
`{"combined_score": 123.0}`。只在必要时创建自定义 verifier 文件，并在冻结前的 Spec
Discovery 阶段将其写入源码拥有的路径，例如 `.goal-plus-verifiers/`，绝不能放在 `.gp/`
或 `.search/` 中。freeze 工具会暴露完整的嵌套 `SearchSpec` schema。
`expected_outputs` 只接受产物路径或 glob，不解析 stdout。

冻结预检在一次性源码副本中运行，并将候选工作区视为只读。verifier 必须把编译器产物和
临时输出放入 `GOAL_PLUS_VERIFIER_TMPDIR`/`TMPDIR` 或
`tempfile.TemporaryDirectory()`。绝不能使用固定 `/tmp` 路径：并行候选验证会发生冲突。
任何工作区变更都会触发 `VerifierWorkspaceSideEffect`；启动候选前应修复 verifier
并冻结新 spec。

如果运行时验证返回 `VerifierWorkspaceSideEffect`、
`metrics.infrastructure_failure=true` 或 `metrics.candidate_action=stop_and_report`，
受影响的 worker 必须立即停止。它不能删除生成文件、修改冻结的 verifier 产物、绕过失败
进行 reset 或重试。父 agent 使 run 失效，停止每个 live worker，修复源码拥有的 verifier，
冻结新 spec，并创建后继 run。绝不能选择或提升已失效的 run。
在 `interrupt_agent` 前使用 `search_invalidate_run`，防止执行中的 verifier 结果进入旧 run。
使用 `source_run_id` 创建后继项，在重新验证每个继承产物的同时保留持久研究 lineage。

## 并行循环契约

新的 Codex Search run 使用：

```yaml
strategy:
  orchestration_mode: parallel_loops
  worker_host: codex
```

`parallel_loops` 表示：

- 只创建一次初始候选集合；
- 一个候选工作区就是一条自主搜索循环；
- 候选 subagent 负责该工作区中后续所有假设、转向、rebase 和 AutoResearch 决策；
- worker 终态事件触发验证并继续同一 worker，而不是开始另一轮规划；
- 正常执行绝不创建替代候选或补充 slot；
- 运行时拒绝对该 run 的第二次 `search_plan_next` 调用；
- `search_select` 和 `search_promote` 仍是父级拥有的最终 Search action；
  对 Goal Plus，`search_report` 要延迟到父级 Goal Plus 记录达到终态。

父 agent 是完成验证器和继续触发器，不是搜索指挥者。低分、一次没有改进的迭代或其他候选
领先，都不是停止或替换 worker 的理由。

## Search Run 预算规划

在 `search_freeze_spec` 前选择整个 run 的预算；预算一旦冻结，不能在该 run 内增长。

1. 为最终 drain、选择、父级验证、报告和提升预留时间。
2. 使用 `max_parallel` 选择初始自主循环数量。如果没有更好的资源信号，建议使用 4。
3. 为每个初始 worker 提供足够的不间断运行时间，以创建真实产物和 verifier 证据。
5. 只能依据全局停止事实决定是否恢复：达到显式目标、用户停止、run 失效，或外层剩余时间
   不足以容纳另一个 worker 轮次和最终收尾。

不同 candidate id 本身不能提供搜索多样性。适用时使用不同初始 proposal 或 seed，
然后让每个 subagent 自行选择下一个有证据支持的假设。大量相近尝试仍无进展后，
由 subagent 而不是父 agent 重新评估理论或结构限制，并在其候选循环内转向。

## 主工作流

1. 为 Goal Plus spec draft 调用 `search_freeze_spec`；如果已有合适的冻结 spec，
   则调用 `search_create`。新 spec 必须设置
   `strategy.orchestration_mode="parallel_loops"`、`worker_host="codex"` 和唯一的
   `budget.max_parallel`；同时必须显式设置 `workspace.backend="git_worktree"`。
   只有用户明确要求兼容隔离时才能设置 `copy`。
   用户指定多模型时，先调用 `goal_plus_list_models(host="codex")` 并将唯一匹配冻结到
   `strategy.models`。`models=A,B` 按 `max_parallel` 轮转；用户写
   `models=A,B A1B3`（或 `models=A*1,B*3`）时使用显式数量，数量之和必须等于
   `max_parallel`；`models=A,B 每个一个` 表示 `max_parallel=2`。运行时据此生成有序
   `selected_models`，每个候选的 selected model 在原生 session 继续期间不可变；
   所有模型仍读取同一个 run 的 `search_get_global_evidence`，模型身份只作为
   candidate/session provenance，不改变候选准入或选择逻辑。`search_create` 不接收
   第二套模型分配参数。用户没有指定 `models` 时保持 host 默认模型。
2. 调用且只调用一次 `search_plan_next(requested_k=budget.max_parallel)`，然后调用且只调用
   一次 `search_start_batch`，创建初始候选。
3. 对每个候选调用且只调用一次 `search_start_agent_session`，直接解析同一次响应中的
   launch payload，不要为了重新读取响应而再次调用。可选的 `worker_budget` 是一次派发的
   host 限制；它不会改变候选的技术方向。真正重派使用 `search_redispatch_candidate`。
4. 使用返回的 launch payload 启动 Codex subagent：
   - 将其映射到当前 `spawn_agent` 工具 schema；
   - 字段可用时始终传入 `task_name`、`message` 和 `fork_turns`；
   - 只有 launch payload 与当前工具 schema 都暴露相应字段时，才传入 `agent_type`、
     `model`、`reasoning_effort` 或 `service_tier`；
   - 绝不能合成可选 launch metadata；
   - 无法传递 model override 时，worker 继承父 Codex model。
5. 使用 `host="codex"` 调用 `search_bind_agent_handle`，绑定返回的 task name/nickname。
6. 跟踪每个 live worker 及其自身 watchdog deadline。使用无 target 的 `wait_agent`，
   然后使用 `list_agents`，处理每个已达终态的 worker。仅有进度的唤醒不是完成事件。
7. 对每个终态 worker：
   - 从 `search_list_history` 或 `goal_plus_monitor_snapshot` 读取当前 run 的最佳结果；
   - 使用 `search_bind_agent_handle` 绑定终态 handle 和摘要；终态绑定会自动采集有界的
     `.tmp/handoff.json` 数据；
   - 需要原生 model、token、时长、上下文或终态证据时，使用
     `search_get_agent_observability(agent_session_id)`；它只读，不能替代
     `list_agents` 判断存活状态；
   - 当前产物已有准确 worker Evidence 时直接复用；仅在没有匹配 Evidence 时，才不传
     `agent_session_id`，使用准确 `run_id` 和 `candidate_id` 补一次父级
     `search_run_verifier`；
   - 刷新 history/monitor，记录 verifier 支持的全局最佳候选/分数是否变化；
   - 只有出现具体评估契约或基础设施失败时才检查 `verifier_assessment`。诊断稀疏、
     分数低或没有改进不代表 verifier 不充分，也不会阻止继续。
8. 验证后执行全局停止 policy：
   - 满足显式成功标准时停止恢复；
   - run 失效或用户停止时停止；
   - 外层剩余时间不足以容纳另一个 worker 轮次和最终收尾时停止；
   - 否则在同一候选上恢复同一个原生 Codex subagent。
9. 要恢复时，先调用不含新技术指令的
   `search_continue_agent_session(agent_session_id)`，再把返回的 continuation payload
   映射到现有 task 的 `followup_task`。严格使用以下中性消息：

   ```text
   根据最新提交的证据继续同一条自主搜索循环。
   刷新运行时上下文，自行选择下一个有证据支持的假设，
   验证每项实质变更，并在仍有分配预算时继续工作。
   ```

   不要提及父级偏好的方向、特性迁移、宏观重启或基于排名的判断。恢复后的 worker
   自行负责该决策。
10. 初始候选创建后，不要再调用 `search_plan_next` 或 `search_start_batch`。
    不要因为候选较弱而让 slot 闲置；要么依据全局停止 policy 恢复同一 worker，
    要么出于全局原因停止整个 Search。
11. 每次等待前，比较最近的 worker deadline 与当前时间。soft closeout 时，使用配置的
    closeout message 调用且只调用一次 `send_message`；到达 hard deadline 时调用
    `interrupt_agent`，观察终态，并像其他完成事件一样验证。一个 worker 超时不会停止
    其他 worker。
12. 全局停止 policy 为 true 时，drain 或中断每个 live worker，然后按要求调用
    `search_select` 和 `search_promote`。选择使用 verifier 支持的 Git iteration；
    不要只为记录临时最佳而提升。该 Search 属于 Goal Plus 时，不调用 `search_report`
    就交还控制；Goal Plus skill 会在父记录达到终态后生成且只生成一次报告。
    对独立 Search，只在提升后调用 `search_report`。

## 当前最佳契约

`search_run_verifier` 感知 metric 方向，并在通过的结果更好时更新持久化的
`run.best_score` 和 `run.best_candidate_id`。父 agent 必须在每次完成验证后观察它，
但不能把比较结果变为是否继续的决策。

每个 process verifier 结果还包含 candidate-local `disposition`。严格改善为
`keep`；同分为 `retain` 并成为最新工作基线；退化为 `discard`，无有效排名证据为
`failure`。runtime 保留本轮实际被测 commit，并只在 `discard`/`failure` 后自动恢复
该 candidate 的 best 代码。worker
不得自行 reset verifier-backed 状态；下一轮直接使用返回后的 settled workspace。

- 更好结果：将其保留为最新 verifier 支持的答案；全局停止 policy 为 false 时，
  恢复同一 worker。
- 更差或相同结果：保留早期最佳；全局停止 policy 为 false 时，仍恢复同一 worker。
- 最终选择：只能在所有 worker drain 后运行。

## Worker 预算控制

`budget_control.mode == "parent_watchdog"` 表示父 Codex agent 强制执行 worker 已用时间。
`spawn_agent` 没有 timeout 参数，因此应结合 `wait_agent`、closeout 和
`interrupt_agent`。

`worker_budget.max_runtime_seconds` 是一次派发可强制执行的上限。continuation 可能收到
根据外层剩余时间推导的另一份单次派发 host 限制。

只把 `budget_control.max_turns_hint` 当作 prompt 指引。hard limit 是
`initial_wait_timeout_ms` 与 `final_wait_timeout_ms` 之和，随后执行中断。
`soft_closeout_seconds` 是收尾窗口。对该次派发发送且只发送一次配置的
`closeout_message`。continuation 只重置同一个 worker 的派发 deadline。

存在 `worker_budget.min_runtime_seconds` 或 `min_verifier_runs` 时，
`budget_control.autoresearch_lease.mode == "subagent_stop"` 是由候选
`SubagentStop` hook 强制执行的下限 lease。过早的最终回复会被阻止，并在不把控制权交还
主流程的情况下继续同一个 Codex worker。该 lease active 时，绝不能发送父级 closeout
消息。不要轮询或休眠；继续“假设 -> 产物 -> verifier”循环。基础设施
`stop_and_report` 证据会绕过 lease。

项目 `PostToolUse` hook 也可能向绑定的候选 worker 提供仅供参考的时间提示。
可用时它可能使用 `GOAL_PLUS_OUTER_DEADLINE_AT`。它绝不会停止 worker，也不能为主 agent、
普通 subagent 或最终检查员触发。

主 agent、普通 subagent 和最终检查员的 PostTool 事件绝不能触发此候选提示。

continuation 预算根据外层剩余时间和最终收尾预留推导，而不是根据父级是否喜欢该候选。
不要故意给弱候选不可用的预算，也不要给强候选新的技术指令。

## 运行时历史与状态级恢复

candidate-local history 由运行时拥有，不是 `plan.md` 文件。worker 通过
`search_get_agent_context` 恢复自己的 `context.iterations`、`context.results`、
`context.results_tsv`、工作区 Git 状态和有界 handoff metadata。其他 candidate 的尝试
只通过窄 `search_get_global_evidence` 视图披露。每轮修改前读取一次；`view=null` 只表示
annotator 尚未更新，worker 可先依据 commit、score、disposition 和自己的推理独立探索，
不等待或轮询。`context.supplemental_evaluation_enabled=false` 时不要等待或
尝试读取补充评价；启用时仅以 `supplemental_available` 标记可展开的行。worker 仅在
路线停滞、结构性分数跃升、hidden 泛化风险或
官方/本地结果背离时，通过 `search_get_evidence_detail` 按需读取完整评价，且不重复读取同一
不可变行。完整内容由 annotator 根据当前累计 diff 和 annotation task 创建时其他已结算候选
快照生成。其中维度由 annotator
根据实际 Evidence 后验提出，不来自 FrozenSpec；动态比较、置信度与 limitations 只是第三方
观察，不是分数、推荐或 promotion gate；它不改变硬分结算规则。worker 可以据此
形成自己的下一轮假设，但应独立核对。只有代码级证据确有必要时，才在当前 workspace 使用
`git diff HEAD <commit> -- <allowed-file>` 做只读比较，不访问其他 candidate workspace，
也不 checkout/reset peer commit。修改完成后，worker 在 `search_run_verifier` 中用一句话
`hypothesis` 客观概括实际尝试。

当 FrozenSpec 显式启用 `shared_dir` 时，candidate 只能从 Global Evidence 的
`shared_tools[*].tool_view` 发现工具；Tool View 前不向 candidate 暴露，生成后由 runtime 绑定。
Tool View 只用于发现和初筛，不规定复用方式，也不是采用建议；candidate 不得仅凭它推断源码行为。
producer 每次 verifier 前必须回顾命令序列、临时代码片段、测试代码和 scratch scripts。可在 peer
workspace 运行且不依赖 candidate 私有临时状态的能力，只要命中当前六类信号之一，就默认提炼到
`.tmp/tool-drafts/` 并用
`search_stage_shared_tool` stage。这里的复用范围是同一 run，不要求跨项目；短小、任务专属、
来自临时代码片段或只输出退出码都不是排除理由。六类信号是 `repeated_workflow`（重复多步流程）、
`domain_construction_or_probe`（领域构造、边界、配置或兼容性探测）、
`behavior_or_invariant_checker`（功能验证、断言或行为/性能/资源/状态不变量）、
`reproducer_fixture_or_case_generator`（最小复现、fixture、输入/corpus/case 生成）、
`parser_trace_or_comparator`（解析/转换、trace/日志归一化、差分/状态比较、mutation 或失败分析）和
`peer_setup_or_feedback_reduction`（setup/cleanup、环境 harness 或更短的本地反馈循环）。旧 signal
仅用于读取历史 iteration，新决策不要使用。测试代码按用途而不是文件名分类：搜索期间临时创建的
测试文件、功能验证函数、最小复现、fixture/case 生成器、差分或不变量检查可以工具化；使用测试框架、
`test_` 文件名或验证同一目标行为都不是 `restricted_artifact` 理由。`restricted_artifact` 只包括
candidate 最终交付或主补丁中的正式测试、冻结 verifier/runner/grader、隐藏答案或评分逻辑，以及日志、
原始数据、凭据和构建输出；诊断工具不得复制、代理或近似重建隐藏反馈。正式测试中的可复用诊断逻辑
应提取为最小 checker/harness，而不是发布最终测试文件本身。首次发布使用 `publication_intent=new` 并保持
低门槛。更新已有 family 前读取 `tool_family_catalog`；仅当 `revision_allowed=true` 时引用
`revision_head.tool_id`。`capability_extension` 必须新增稳定 capability/coverage key；
`adoption_fix` 必须有同 family 的真实 copy/adoption 事实及具体缺陷；`contract_change` 必须为有价值
的入口、输入、输出或依赖变化新增稳定的契约键。键是可机器比较的语义标识，不能靠改名或改写同义键
制造增量。纯改名、重排或同义断言不得发布。只有 `single_common_command`、`logic_free_wrapper`、
`restricted_artifact`、`candidate_private_state`、`duplicate_snapshot`、
`existing_family_sufficient`、`no_material_tool_delta`、`draft_not_ready`、`max_published_versions_reached`
这些具体排除项才支持 `not_applicable`。`revision_allowed=false` 时使用 `revision_blocker` 的准确值；
`existing_family_sufficient` 仅表示 `revision_head` 已经覆盖需求；`no_material_tool_delta` 表示没有上述
实质增量；`draft_not_ready` 仅表示具体的安全性、
完整性、可移植性或 peer 可运行性阻塞，不能用短小、任务专属、inline 来源或未润色作为理由。
每次归属于 worker 的 process verifier 都提交
`toolization_decision`；缺失或与实际 staging 不匹配只记录 monitor/report advisory，不改变硬分、
结算、选择或 promotion，staging inventory 始终是权威事实。
若工具可能相关，调用 `search_copy_shared_tool(agent_session_id, tool_id, snapshot_hash)` 将精确快照
复制到本地临时 inbox。复制本身不要求调用原工具；先阅读 manifest、入口和源码，再由 candidate 自行决定直接执行或
导入、提取并改写局部逻辑、复用诊断方法、作为实现对照，或不采用；这些示例不限制其他合理方式。
只有直接执行、导入或把原快照作为运行时依赖时，才在使用前验证其入口、依赖、路径假设和输出
语义；只读分析或改写源码不要求先运行原工具，最终 candidate 修改仍由正常 process verifier
验证。下一次 worker verifier 原子消费复制 receipt；当前 `adopted_tools` 只证明该快照曾复制进
本轮上下文，不证明原工具被执行或其代码被保留。Tool View 与复制结算事实可以帮助形成后续假设，
但不改变 hard score、`keep`/`retain`/`discard`/`failure` 结算、选择或 promotion。
host transcript 是有用上下文，但不是权威 Search 状态。

Codex 的同 worker continuation 使用 `search_continue_agent_session`，随后对现有 task
调用 `followup_task`。worker 必须在每个恢复轮次开始时刷新上下文。

只有原始 native worker 无法继续时才使用 `search_redispatch_candidate`。
重新派发必须保留相同 candidate/workspace。它是恢复手段，不是引入另一搜索方向的常规方式。

## 污染恢复

普通低性能绝不会创建新候选。只有存在客观恢复证据时才允许替代/fork，例如无法恢复的
Git 或账本损坏、原生 session 不可用且同候选重新派发失败，或 verifier/spec 失效而需要
后继 run。

恢复顺序：

1. 继续同一个 native worker；
2. 同候选状态级重新派发；
3. 从最新 verifier 支持的 Git revision 恢复同一候选；
4. 只能通过未来显式恢复契约，fork 带持久 lineage 的替代项。

在 `parallel_loops` 模式下，不要把普通 `search_plan_next` 用作该恢复契约。
