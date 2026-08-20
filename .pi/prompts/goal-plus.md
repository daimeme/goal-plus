首先调用 `goal_plus_create(raw_goal="$ARGUMENTS")`，必须在 triage、规划、编辑或 Search 之前调用。
除了加载 goal-plus skill 之外，在调用 `goal_plus_record_triage` 之前不要读取或审计目标文件。

原生入口若注入了已解析的显式角色模型，严格使用该路由：`main=` 已由入口切换；
`annotator=` 写入 `strategy.evidence_annotator.model`；`workers=` 或兼容别名
`models=` 按现有分配规则写入 `strategy.models`。未显式指定的角色保持现有默认或继承语义。

# Goal Plus

对以下原始用户目标使用 `/skill:goal-plus`：

$ARGUMENTS

当此 Pi prompt 开启 Search Mode 时，SearchSpec strategy 必须设置
`worker_host: "pi-rpc"`；默认设置 `orchestration_mode: "parallel_loops"`。只有原始目标或
benchmark 明确要求 runtime reward/allocation 时，才按 skill 中的隔离配置使用
`orchestration_mode: "adaptive_search"` 和 `budget.max_candidates`。SearchSpec 必须显式
设置 `workspace.backend="git_worktree"`，使 worker 通过持久化 Pi pool 运行。
只有用户明确要求兼容隔离时才能设置 `workspace.backend="copy"`。

冻结前，要求每个 `ranking_signal` 输出一个最终 JSON 对象，其中包含有限数值类型的
`spec.metric_name`。命令可以内联，也可以调用现有工具。只在必要时创建自定义 verifier 文件，
并在冻结前的 Spec Discovery 阶段将其写入源码拥有的路径，例如 `.goal-plus-verifiers/`，
绝不能放在 `.gp/` 或 `.search/` 中。Spec Discovery 可以为此使用 `bash`、`write` 和
`edit`。freeze 工具会暴露完整的嵌套 `SearchSpec` schema；不要根据校验错误猜测字段。
`expected_outputs` 只列出产物路径或 glob。
verifier 必须保持候选工作区只读，并使用唯一的 `GOAL_PLUS_VERIFIER_TMPDIR`/`TMPDIR`
或 Python `tempfile` 存放编译器和临时输出。绝不能使用固定的 `/tmp` 路径，因为 Pi pool
可能并发验证多个候选。冻结操作会在消耗候选预算前拒绝任何工作区副作用。

首次获得有意义的优化结果后，执行 skill 中已有的原始目标审计，但不要把该结果视为运行完成。
相对 baseline 的巨大改进并不能证明结果已接近有意义的成功，尤其是在没有绝对目标或验收条件时。
默认继续使用同一个冻结 spec 和 run。只有存在具体的评估/编辑契约缺陷时才使用
`upgrade_spec`；只有实际目标发生变化时才使用 `revise_goal`。新搜索方向或特性迁移应留在
当前 run 内。这是现有流程中的推理，不是新的运行时阶段或审批步骤。

如果 worker 报告 verifier 问题，在核查其证据期间暂停继续执行。普通 `parallel_loops` 模式下
不存在常规 slot 补充。不要因为诊断稀疏、分数低或进展缓慢而重建。
如果主 agent 确认确有契约、覆盖范围、确定性、目标或基础设施缺陷，先调用
`search_invalidate_run`，中断整个 Pi pool 并等待 `active_count=0`，然后修复并重新冻结，
再使用 `source_run_id` 创建后继 run。绝不能选择或提升已经失效的 run。

在 Search 执行、提升、结果记录或最终原始目标审计期间，绝不能调用 `search_report`。
Goal Plus 记录达到终态后，对每个成功记录的 `run_id` 调用且只调用一次 `search_report`，
并返回最终 Markdown 和 HTML 路径。
