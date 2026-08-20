---
name: goal-plus
description: 运行、恢复或编辑 Codex Goal Plus 任务，包括需要独立最终审查员的 /goal-plus-with-final-check 任务，并可通过 goal-plus MCP server 升级为 Agentic Search。
---

# Codex 的 Goal Plus

对 `/goal-plus` 使用此 skill，这是标准目标工作流。当成功标准可度量且已冻结时，
它可以升级为多候选 Agentic Search。

使用 `goal-plus` MCP server 暴露的逻辑 `goal_plus_*` 和 `search_*` 工具。
Codex 可能显示带客户端特定前缀的 MCP 工具；按最后的逻辑工具名匹配。

## 工作流

1. 首先读取隐藏的 Codex hook 上下文。如果其中包含 active 的 `goal_plus_id`，
   则 `UserPromptSubmit` hook 已在本模型轮次前创建并绑定记录；使用该 id，
   不要再次调用 `goal_plus_create`。如果没有 hook 上下文，调用
   `goal_plus_create(raw_goal=...)` 作为兼容后备。
   `/goal-plus-with-final-check` 会预先创建，并设置
   `policy.final_check.mode="required"`。`/goal-plus edit <完整的修订目标>`
   会在模型轮次前更新同一记录；使用新的 `goal_revision`，不要继续旧修订版。
   host 中断后，`/goal-plus resume` 会恢复同一个 active 修订版。
   `/goal-plus mode=autonomous <目标>`（默认）要求为初始候选分配充足 lease，
   并在没有全局停止条件时继续同一候选；`/goal-plus mode=probe <目标>`
   要求进行短期可行性、潜力和阻塞因素探查。未指定 mode 的编辑保留当前选择。
   运行时只把它存为 `raw_goal` 的规范末行，不把它作为 phase、Search strategy
   或运行时字段。
   恢复 active 记录前，把最新用户消息视为本轮的权威依据：
   - 如果它继续或引导现有目标，但没有改变范围、交付物或成功标准，保留当前修订版。
   - 如果它改变了实际范围、交付物或成功标准，使用完整修订目标和当前
     `expected_revision` 调用 `goal_plus_update_goal`，然后在继续工作前重新 triage。
   - 如果消息与目标无关，直接回复而不改变目标。如果它与目标的关系不明确，
     在修订或恢复前先澄清。不要仅因 Goal Plus 记录处于 active 就恢复工作。
2. 检查足够的上下文以分类任务。
3. 调用 `goal_plus_record_triage`。
4. 如果 triage 选择 Goal Mode，在当前工作区正常工作。Goal Mode 下不要创建 SearchSpec。
5. 如果 triage 选择 Spec Discovery Mode，确定 baseline、metric、正确性门禁、编辑范围、
   verifier 产物、预算和提升规则。ranking verifier 必须输出一个最终 JSON 对象，
   其中包含有限数值类型的 `spec.metric_name`；其文件应放在源码拥有的路径，
   例如 `.goal-plus-verifiers/`，绝不能放在 `.gp/` 或 `.search/`。
   `expected_outputs` 只列出产物路径或 glob，不解析 stdout。要求 verifier 保持候选
   工作区只读，并使用唯一的 `GOAL_PLUS_VERIFIER_TMPDIR`/`TMPDIR`（或 Python
   `tempfile`）存放编译器和临时输出；并行 Search 下固定 `/tmp` 路径不安全。
   冻结操作会在消耗候选预算前拒绝工作区副作用。新 SearchSpec 必须显式设置
   `workspace.backend="git_worktree"`，以便候选共享 Git object database 并解析彼此的
   Evidence commit；只有用户明确要求兼容隔离时才能设置 `copy`。使用
   `goal_plus_save_spec_draft` 保存完整契约。
   不要在 SearchSpec 中生成软 rubric 或预设评价维度。Spec Discovery 只能冻结硬 metric、
   verifier、编辑范围、预算和 promotion 合同。开放式补充评价发生在每次 Evidence 结算之后：
   独立 annotator 根据当前候选累计 diff 和当时其他已结算候选的快照，自行提出与任务实际
   相关的观察维度并动态比较。它不读取 hidden 数据，不产生总分或最终推荐，也不改变硬
   PASS/FAIL、数值排名、candidate-local 结算、selection 或 promotion。MainAgent
   不负责定义这些维度，也不要根据 benchmark 类型向 annotator 预埋固定清单。
   如果原始命令包含 `models=...`，先调用 `goal_plus_list_models(host="codex")`，
   将用户填写的名称解析为唯一可用模型并冻结到 `strategy.models`；不存在或不唯一时，
   在创建 run 前直接返回错误。`models=A,B max_parallel=4` 表示按 A、B、A、B 轮转；
   `models=A,B A1B3 max_parallel=4`（等价写法 `models=A*1,B*3`）表示显式
   A1B3，所有显式数量之和必须等于
   `max_parallel`。`models=A,B 每个一个` 表示 `max_parallel=2`。用户没有填写
   `models` 时不要探测目录，保持 host 默认模型。
   对于用语义、大致 shape/dtype 和参考提示描述的 AscendC Direct Invoke 算子目标，
   记录 `scenario="ascendc_direct_invoke"`，并完整读取
   `examples/ascendc-direct-search/SPEC_DISCOVERY.md`。遵循其中的 request schema
   和源码模板。针对准确固定的 Git commit，使用 `knowledge.sources.json` 运行
   `materialize_knowledge.py` 生成任务局部 `_skills/`；绝不能复制 live Skill 目录。
   将精编的 AKG AscendC tree 作为主要知识，只对未覆盖的算子类别使用声明的 CANNBot
   补充。主 agent 必须在 Spec Discovery 期间生成 Golden、cases、verifier、baseline
   和 SearchSpec。调用 `search_freeze_spec` 前，使用 JSON Schema validator 按照
   `examples/ascendc-direct-search/request.schema.json` 校验生成的
   `_task/operator_request.json`；仅做 JSON 解析或手动字段清单检查不够，校验失败会
   阻止冻结。
   绝不能要求用户运行任务准备器、提供任务目录或编写 verifier。仅支持 Direct Invoke；
   生成的知识是只读的，不能启动源码 Agent 或 Plugin 工作流。该场景自包含；
   不要调用外部 AscendC Agent、plugin 或编排工作流。
6. 只有已保存 draft 的 `confidence="high"` 且没有 open question 时，才进入 Search Mode。
7. Search 是自主升级。draft 达到高置信度且无 open question 后，直接进入 Search Mode
   gate，不要要求用户批准 verifier、metric、编辑范围、提升规则或 mode 变化。
   用户提示是有用证据，但不是必要条件。
8. 将 `origin="initial"` 或 `origin="in_progress"` 仅作为 provenance。
   它不能改变已准备好的 search draft 是否可以继续。
9. 调用 `search_freeze_spec` 等 Search Mode 工具前，调用
   `goal_plus_gate(event="pre_tool_use", context={"tool_name": "search_freeze_spec"})`。
10. 在 Search Mode 中使用内部 `search` skill：默认以
    `strategy.orchestration_mode="parallel_loops"` 冻结新的 Codex spec；只有目标或 benchmark
    明确要求 runtime reward/allocation 时才使用内部 skill 定义的 `adaptive_search` 配置。
    两种模式都只做一次初始规划；`adaptive_search` 后续只执行 runtime 持久化的派生决策。依次使用
    `search_freeze_spec`、`search_create`、`search_plan_next`、
    `search_start_batch`、`search_start_agent_session`、最终
    `search_run_verifier`、`search_select` 和 `search_promote`。
11. `search_create` 后调用 `goal_plus_link_search_run`。
12. 选择并提升后调用 `goal_plus_record_search_result`。此时不要调用
    `search_report`。结果记录只预留规范 Markdown 和 HTML 路径，不创建报告。
13. 执行原始目标审计。如果还需要其他有 verifier 支持的 Search，使用相同
    `goal_plus_id` 冻结/创建新 run，并重复步骤 9-12。每个不同的 `run_id`
    都作为另一项 Search 任务追加；不要为新的冻结 spec 复用旧 `run_id`。
14. 最后执行一次原始目标审计。对于普通 Goal Plus 记录，只有当前目标已满足时才调用
    `goal_plus_set_status(status="complete", evidence=[...])`。当
    `policy.final_check.mode="required"` 时：
    - 调用 `goal_plus_prepare_final_check(checker_host="codex")`
    - 把 `launch.task_name`、`launch.message` 和 `launch.fork_turns` 映射到当前
      `spawn_agent` schema，并在前台启动
    - 使用 `fork_turns="none"`；审查员必须从工作区和运行时证据重建结果，
      不能继承父级 transcript
    - 等待审查员返回；它必须自行调用 `goal_plus_submit_final_check`
    - 失败时处理其所有发现并准备新检查；绝不能代表审查员提交结论
    通过的必需检查会原子地把 Goal Plus 记录标记为 complete。
15. 只有 Goal Plus 记录达到终态（`complete`、`blocked` 或 `abandoned`）后，
    才对每个成功记录的 `run_id` 调用且只调用一次 `search_report`。
    绝不能生成中间 Goal Plus 报告。向用户返回最终 Markdown 和 HTML 路径。
    通过的必需最终检查属于终态，因为它会原子地把记录标记为 complete。
16. 停止前调用 `goal_plus_gate(event="stop", context={})`；如果它返回继续 prompt，
    则继续工作。

顶层 Stop gate 会阻止每条仍处于 active 的记录，并返回完整的当前原始目标、创建/检查
时间戳、已用时间、phase、next action 和最终检查 policy。根据该 prompt 审计全部目标要求
以及任何时间条件。未完成时继续；否则先用真实的终态调用 `goal_plus_set_status`。
候选 lease 结束绝不会完成父级 Goal Plus 任务，Goal Plus 也不单独存储任务 deadline。

一条 Goal Plus 记录就是完整任务。`search_tasks` 是其仅追加的 Search Mode 任务历史，
每个 `run_id` 对应一个冻结 spec；`linked_search` 只是当前任务的兼容视图。在一项 Search
任务中，规划轮次和已启动搜索轮次由 `goal_plus_monitor_snapshot` 分别报告。

目标编辑同样仅追加：`goal_revisions` 保留每个实际目标。更新目标会为新修订版重置
intake/triage，并在不删除旧 Search 任务和最终检查的情况下将其变为历史。如果轮次中断，
调用 `goal_plus_status` 并恢复持久化修订版。如果审查员中断，其尝试会记录为
`interrupted`；调用 `goal_plus_prepare_final_check` 创建并启动新尝试。

## Triage Schema

`goal_plus_record_triage` 要求以下运行时 schema：

```json
{
  "is_optimization": false,
  "confidence": "high",
  "recommended_phase": "goal",
  "identified_at": "initial",
  "scenario": null,
  "reasons": ["此分类正确的原因"],
  "missing": []
}
```

`recommended_phase` 只能使用 `"goal"`、`"spec_discovery"` 或 `"search"`。
不要发送名为 `mode` 或 `reason` 的字段，也不要使用 `"goal_mode"` 等值。

建议映射：

- Goal Mode：`is_optimization=false`、`recommended_phase="goal"`、
  `confidence="high"`。
- Spec Discovery Mode：`is_optimization=true`、
  `recommended_phase="spec_discovery"`，并列出缺失的 baseline、metric、正确性门禁、
  编辑范围、verifier、预算或提升细节。
- Search Mode：`is_optimization=true`、`recommended_phase="search"`、
  `confidence="high"`。

## 模式

Goal Mode 用于普通编码、文档、审查和调查任务。它使用普通 Codex 验证证据，
不使用 SearchSpec。

Spec Discovery Mode 用于具有优化形态、但 metric、baseline、正确性门禁或编辑范围仍不明确
的目标。

Search Mode 用于已冻结、可度量的优化。它把候选工作区创建、verifier 执行、选择、报告和
提升委托给现有 Search MCP 流程。

## Hook 兼容性

此仓库在 `.codex/hooks.json` 提供 Codex 0.144.1 Goal Plus host hook。
它们会为 `UserPromptSubmit`、`SessionStart`、`PreToolUse`、`PostToolUse`、
`Stop` 和 `SubagentStop` 运行 `goal-plus --goal-plus-host-hook`。
`UserPromptSubmit` 会在模型轮次前预先创建并绑定 `/goal-plus` 或 `$goal-plus`，
也识别 `/goal-plus-with-final-check` 和显式 `/goal-plus edit` 更新。
`SessionStart` 恢复与 session 绑定的 active id。`PreToolUse` 强制执行 Search 和变更
gate。`PostToolUse(goal_plus_create)` 仍作为兼容绑定后备。Search 候选 subagent 的
PostTool 事件还会执行一次只读 verifier 时间提示检查；它们绝不会绑定 Goal Plus 所有权，
主 agent、最终检查员和普通 subagent 的事件会被忽略。顶层 `Stop` 会强制执行完整原始目标
和耗时审计，直到主 agent 记录终态。
`SubagentStop` 感知所有权：Search 候选只会被阻止到其自己的
`search_run_verifier(..., agent_session_id=...)` 调用被持久记录为止。它返回后，
父 agent 执行完成验证，观察是否出现新的 verifier 支持的全局最佳。普通
`parallel_loops` 在全局停止 policy 为 false 时恢复同一个候选 worker，父 agent 不选择其
下一个技术方向，也不创建基于质量的替代项。显式 `adaptive_search` 只执行 runtime 已持久化的
allocation decision。普通 subagent 不继承父级 action；最终检查审查员保留其独立审查 gate。

即使 hook 是强制后备，也应保留上述显式工作流调用作为可审计状态转换。subagent 工具事件
不会绑定 Goal Plus 所有权。`goal_plus_gate` 不监管 worker 生命周期；Codex worker 预算和
前台 subagent 行为仍由内部 `search` skill 负责。
