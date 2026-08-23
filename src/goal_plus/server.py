from __future__ import annotations

import argparse
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field

from goal_plus.goal_plus import FileGoalPlusRuntime
from goal_plus.models import (
    AgentHostKind,
    GoalPlusSpecDraftInput,
    SearchSpec,
    ToolizationDecision,
)
from goal_plus.paths import DEFAULT_RUNTIME_ROOT
from goal_plus.runtime import FileSearchRuntime
from goal_plus.tools import GoalPlusTools, SearchTools


def create_mcp(
    root_dir: str | Path = DEFAULT_RUNTIME_ROOT,
) -> FastMCP:
    from fastmcp import FastMCP

    runtime = FileSearchRuntime(root_dir)
    goal_runtime = FileGoalPlusRuntime(root_dir)
    tools = SearchTools(runtime)
    goal_tools = GoalPlusTools(goal_runtime)
    mcp = FastMCP("goal-plus")

    @mcp.tool()
    def search_freeze_spec(
        spec: SearchSpec,
        verifier_artifact_paths: list[str],
    ) -> dict[str, Any]:
        """将 SearchSpec 及其 verifier 文件冻结为不可变 bundle。

        返回 `frozen_spec_id`，应在 `search_create` 前调用。verifier 文件按 hash 固定；
        候选执行期间修改它们会强制分数为 0.0。冻结会预检每个 `ranking_signal`：
        它必须以 0 退出，并输出一个最终 JSON 对象，其中包含有限数值类型的
        `spec.metric_name`，例如 `{"score": 123.0}`。verifier 命令在一次性源码副本中
        运行，不能改变该工作区。编译器产物和临时输出应放入每次调用唯一的
        `GOAL_PLUS_VERIFIER_TMPDIR`、`TMPDIR`、`TMP` 或 `TEMP`；并发验证候选时固定
        `/tmp` 路径不安全。可选自定义 verifier 必须在 Spec Discovery 期间写入源码拥有
        的路径，绝不能放在 `.gp` 或 `.search`。`expected_outputs` 只包含产物路径/glob，
        不是 stdout parser 配置。`spec.budget.max_parallel` 唯一决定初始候选 Agent 数。
        """
        return tools.search_freeze_spec(spec, verifier_artifact_paths)

    @mcp.tool()
    def goal_plus_list_models(
        host: AgentHostKind,
        query: str | None = None,
    ) -> dict[str, Any]:
        """列出指定 Codex/Pi host 当前可用的模型。

        `/goal-plus models=...` 在冻结 spec 前调用它。返回的 `model` 是可写入
        `strategy.models[].model` 的精确 host 引用；不存在或有歧义的请求会在冻结时
        直接失败。省略 `query` 返回完整清单。
        """
        return tools.goal_plus_list_models(host, query)

    @mcp.tool()
    def search_create(
        frozen_spec_id: str,
        source_run_id: str | None = None,
    ) -> dict[str, str]:
        """从冻结 spec 启动 Search run，并返回 `run_id`。

        只有无法避免新的不可变 run 时才传入 `source_run_id`。新 run 会收到来源 frontier、
        限定范围的问题和特性账本的有界 snapshot。来源分数只是历史，必须在新契约下重新验证。
        """
        return tools.search_create(frozen_spec_id, source_run_id)

    @mcp.tool()
    def search_status(run_id: str) -> dict[str, Any]:
        """返回 run 状态、预算使用和最佳分数的只读 snapshot。"""
        return tools.search_status(run_id)

    @mcp.tool()
    def search_invalidate_run(
        run_id: str,
        reason: Literal[
            "verifier_contract_invalid",
            "verifier_coverage_inadequate",
            "verifier_nondeterministic",
            "verifier_target_mismatch",
            "verifier_infrastructure_failure",
        ],
        summary: str,
        evidence: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """主 agent 确认 verifier 不充分后隔离 run。

        该操作会原子地阻止新规划、session、verifier 记录、选择和提升。它不控制 host
        worker：调用后，主 agent 必须中断 host pool，等待每个 worker 达到终态，
        再修复/冻结 verifier，并使用 `source_run_id` 创建新 run。
        """
        return tools.search_invalidate_run(run_id, reason, summary, evidence)

    @mcp.tool()
    def goal_plus_monitor_snapshot(
        goal_plus_id: str | None = None,
        run_id: str | None = None,
        stale_after_seconds: int = 600,
    ) -> dict[str, Any]:
        """返回供轮询 agent 使用的只读 Goal Plus/Search 监控 snapshot。

        从持久化 `.gp` 状态返回 run、candidate、agent-session、verifier、host-log 和 Pi
        usage 证据。它不会启动、等待或中断 worker。
        """
        return tools.goal_plus_monitor_snapshot(
            goal_plus_id=goal_plus_id,
            run_id=run_id,
            stale_after_seconds=stale_after_seconds,
        )

    @mcp.tool()
    def search_list_history(
        run_id: str,
        top_n: int = 5,
        sort_by: str = "score",
    ) -> dict[str, Any]:
        """返回已评估候选及其分数的只读排名列表。"""
        return tools.search_list_history(run_id, top_n, sort_by)

    @mcp.tool()
    def search_plan_next(
        run_id: str,
        requested_k: Annotated[
            int,
            Field(
                gt=0,
                description=(
                    "请求初始候选数。运行时按 min(requested_k, budget.max_parallel 的"
                    "剩余可用数量) 进行规划。标准流程必须传入 budget.max_parallel。"
                ),
            ),
        ] = 4,
    ) -> dict[str, Any]:
        """从冻结的整个 run 预算中规划一个候选 batch/轮次。

        `requested_k` 只适用于这一次初始规划。实际 `planned_k` 是 `requested_k` 与
        `budget.max_parallel` 剩余可用数量的最小值。标准流程必须令两者相等。返回
        `plan_id` 和候选任务。
        """
        return tools.search_plan_next(run_id, requested_k)

    @mcp.tool()
    def search_start_batch(
        run_id: str,
        plan_id: str,
        proposals: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """materialize 已规划的候选工作区（`source_path` 的副本）。

        每个返回的 `CandidateTask` 拥有隔离工作区；候选编辑必须留在其中。
        不要在 `search_plan_next` 前调用。
        """
        return tools.search_start_batch(run_id, plan_id, proposals)

    @mcp.tool()
    def search_start_agent_session(
        run_id: str,
        candidate_id: str,
        directive: dict[str, Any] | str | None = None,
        worker_budget: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """创建 context/provenance handle 和 host 原生 launch payload。

        它不会启动 worker 或跟踪 lifecycle。可选 `worker_budget` 只覆盖本次派发，
        不改变冻结 spec。对所选 host 使用返回的 `launch` payload。prompt 提供的
        `candidate_id` 只是标签；worker 必须从 `search_get_agent_context` 获取权威上下文。
        """
        return tools.search_start_agent_session(
            run_id, candidate_id, directive, worker_budget
        )

    @mcp.tool()
    def search_redispatch_candidate(
        run_id: str,
        candidate_id: str,
        worker_agent_type: str | None = None,
        worker_budget: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """为现有候选工作区创建新的 worker launch。

        这是状态级恢复，不是同 worker continuation。它为相同 candidate/workspace 返回新的
        `agent_session_id` 和 host launch payload。可选 `worker_agent_type` 和
        `worker_budget` 只覆盖本次派发；候选任务 policy 不变。
        """
        return tools.search_redispatch_candidate(
            run_id,
            candidate_id,
            worker_agent_type,
            worker_budget,
        )

    @mcp.tool()
    def search_bind_agent_handle(
        agent_session_id: str,
        handle: dict[str, Any],
    ) -> dict[str, Any]:
        """把运行时 agent session 绑定到 host worker handle。"""
        return tools.search_bind_agent_handle(agent_session_id, handle)

    @mcp.tool()
    def search_get_agent_observability(
        agent_session_id: str,
    ) -> dict[str, Any]:
        """读取一个 agent session 的规范化 host metric 和产物。

        schema 跨 host 共享。可用时 Codex 解析原生 session JSONL；Pi 规范化已绑定的
        `pi_metrics`。此调用只读，不会等待、继续或中断 worker。绝不返回 prompt、
        reasoning 或工具 payload 正文。
        """
        return tools.search_get_agent_observability(agent_session_id)

    @mcp.tool()
    def search_continue_agent_session(
        agent_session_id: str,
        worker_budget: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """返回继续已绑定 worker session 所需的 host launch 字段。

        支持原生 continuation 的 host 会复用相同 worker handle。可选 worker budget
        只覆盖本次 continuation 派发。continuation prompt 保持中性，由 worker 选择下一步。
        """
        return tools.search_continue_agent_session(
            agent_session_id,
            worker_budget,
        )

    @mcp.tool()
    def search_get_agent_context(agent_session_id: str) -> dict[str, Any]:
        """Subagent 的首次调用，返回权威 id 和工作区。

        返回 run_id、candidate_id、workspace、candidate_task 和 subagent 自己的
        iterations/results。共享 Evidence 的 adaptive 派生 candidate 还可能包含从已结算
        transition 动态投影的 expansion_action_context。由 subagent 调用，不由主
        agent 调用。subagent 必须把 prompt 提供的 id 只当作标签，并将此响应作为事实来源。
        """
        return tools.search_get_agent_context(agent_session_id)

    @mcp.tool()
    def search_get_global_evidence(
        agent_session_id: str,
    ) -> list[dict[str, Any]]:
        """返回当前 run 的窄 Global Evidence 视图。

        每项只包含 candidate_id、iteration、score、keep/retain/discard/failure disposition、
        verifier attempt commit、可能延迟的客观 View，以及可选 supplemental evaluation
        的可用标记；启用 shared_dir 时还可包含已绑定 Tool View 的共享工具。任何 View 为 null
        都不影响 verifier Evidence；worker 不需要等待，可先依据 Evidence 独立探索。
        """
        return tools.search_get_global_evidence(agent_session_id)

    @mcp.tool()
    def search_copy_shared_tool(
        agent_session_id: str,
        tool_id: str,
        snapshot_hash: str,
    ) -> dict[str, Any]:
        """复制一个已绑定 Tool View 的共享工具到候选临时 inbox。"""
        return tools.search_copy_shared_tool(agent_session_id, tool_id, snapshot_hash)

    @mcp.tool()
    def search_stage_shared_tool(
        agent_session_id: str,
        name: str,
        summary: str,
        entrypoint: str,
        candidate_relative_source_paths: list[str],
    ) -> dict[str, Any]:
        """将显式的 candidate tool drafts 安全复制到下一次 verifier staging。

        source paths 必须位于当前 candidate 的 `.tmp/tool-drafts/` 下。该工具只执行
        staging；只有归属于当前 worker 且通过的 process verifier 才能发布快照。
        """
        return tools.search_stage_shared_tool(
            agent_session_id,
            name,
            summary,
            entrypoint,
            candidate_relative_source_paths,
        )

    @mcp.tool()
    def search_get_evidence_detail(
        agent_session_id: str,
        candidate_id: str,
        iteration: Annotated[int, Field(ge=1)],
    ) -> dict[str, Any]:
        """按需展开一条已结算 Evidence 的 supplemental evaluation。

        仅当 agent context 声明该能力已开启，且 Global Evidence 对目标行返回
        `supplemental_available=true` 时调用。目标必须属于同一 run；`independent` 模式只
        允许读取调用方自己的 candidate。返回内容不含 peer workspace、transcript 或推理。
        """
        return tools.search_get_evidence_detail(
            agent_session_id,
            candidate_id,
            iteration,
        )

    @mcp.tool()
    def search_run_verifier(
        run_id: str,
        candidate_id: str,
        scope: Literal["process", "promotion"] = "process",
        agent_session_id: str | None = None,
        hypothesis: str | None = None,
        toolization_decision: ToolizationDecision | None = None,
    ) -> dict[str, Any]:
        """Subagent 带 `agent_session_id` 自评分；主流程最终验证不带它。

        subagent 传入准确 `run_id`、`candidate_id`、自己的 `agent_session_id`、一句话
        `hypothesis` 并省略 `scope`；启用 shared-dir 时还可提交结构化
        `toolization_decision`。缺失或与实际 staging 不匹配只记录 advisory，不改变评分、
        结算、选择或 promotion；决策保存在 iteration 中但不进入 Global Evidence。
        hypothesis 应客观概括本轮实际尝试。运行时随后在继承的 `workspace/results.tsv`
        中追加且只追加一条已验证记录并提交账本。主 agent 不带 `agent_session_id` 的
        内部复验不要求 hypothesis；`promotion` 只属于主流程。带
        `candidate_action="stop_and_report"` 的 `VerifierWorkspaceSideEffect` 是冻结
        verifier 的基础设施失败：worker 不能清理 verifier 输出或重试。
        """
        return tools.search_run_verifier(
            run_id,
            candidate_id,
            scope,
            agent_session_id,
            hypothesis,
            toolization_decision,
        )

    @mcp.tool()
    def search_list_iterations(
        run_id: str,
        candidate_id: str,
    ) -> list[dict[str, Any]]:
        """返回一个候选的只读 iteration 记录列表。"""
        return tools.search_list_iterations(run_id, candidate_id)

    @mcp.tool()
    def search_list_allocation_decisions(
        run_id: str,
        status: Literal["pending", "applied"] | None = None,
    ) -> list[dict[str, Any]]:
        """仅当 FrozenSpec 显式启用 adaptive_search 时读取持久化资源分配决策。

        该工具只读，不启动、继续或中断任何 host worker。未传 status 时返回全部决策。
        """
        return tools.search_list_allocation_decisions(run_id, status)

    @mcp.tool()
    def search_apply_allocation_decision(
        run_id: str,
        decision_id: str,
    ) -> dict[str, Any]:
        """仅当 FrozenSpec 显式启用 adaptive_search 时幂等应用准确决策。

        runtime 先预检 decision 中全部 replacement，再保留被剪枝候选的 Evidence、物化
        所有派生 workspace、创建 agent-session provenance，并返回每个 replacement 的 host
        launch payload。调用方仍须通过 Codex/Pi host 启动全部 worker 并绑定原生 handle；
        该工具不监督 worker 生命周期。
        """
        return tools.search_apply_allocation_decision(run_id, decision_id)

    @mcp.tool()
    def search_select(run_id: str) -> dict[str, Any]:
        """按分数选择最佳已评估候选。在验证候选后调用。"""
        return tools.search_select(run_id)

    @mcp.tool()
    def search_report(run_id: str) -> dict[str, str]:
        """生成最终 Markdown/HTML 报告；已链接的 Goal Plus 必须处于终态。"""
        return tools.search_report(run_id)

    @mcp.tool()
    def search_promote(run_id: str, candidate_id: str) -> dict[str, str]:
        """将所选候选导出为 patch，不改变主源码工作区。"""
        return tools.search_promote(run_id, candidate_id)

    @mcp.tool()
    def goal_plus_create(
        raw_goal: str,
        source_path: str | None = None,
        policy: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """triage 前根据原始用户目标创建 goal-plus 记录。"""
        return goal_tools.goal_plus_create(raw_goal, source_path, policy)

    @mcp.tool()
    def goal_plus_status(goal_plus_id: str) -> dict[str, Any]:
        """读取 goal-plus phase、status、已链接 Search 状态和证据日志。"""
        return goal_tools.goal_plus_status(goal_plus_id)

    @mcp.tool()
    def goal_plus_update_goal(
        goal_plus_id: str,
        raw_goal: str,
        expected_revision: int,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """原地替换实际目标，并开始新的可审计修订版。"""
        return goal_tools.goal_plus_update_goal(
            goal_plus_id,
            raw_goal,
            expected_revision,
            reason,
        )

    @mcp.tool()
    def goal_plus_record_triage(
        goal_plus_id: str,
        triage: dict[str, Any],
    ) -> dict[str, Any]:
        """记录目标应保留 goal 形态，还是向 Search 升级。"""
        return goal_tools.goal_plus_record_triage(goal_plus_id, triage)

    @mcp.tool()
    def goal_plus_save_spec_draft(
        goal_plus_id: str,
        spec_draft: GoalPlusSpecDraftInput,
    ) -> dict[str, Any]:
        """在 search_freeze_spec 前保存发现的冻结 spec 候选。"""
        return goal_tools.goal_plus_save_spec_draft(goal_plus_id, spec_draft)

    @mcp.tool()
    def goal_plus_link_search_run(
        goal_plus_id: str,
        frozen_spec_id: str,
        run_id: str,
    ) -> dict[str, Any]:
        """把现有 Search MCP run 链接到 goal-plus 记录。"""
        return goal_tools.goal_plus_link_search_run(goal_plus_id, frozen_spec_id, run_id)

    @mcp.tool()
    def goal_plus_record_search_result(
        goal_plus_id: str,
        run_id: str,
        selected_candidate_id: str | None = None,
        report_path: str | None = None,
        promotion_artifact_path: str | None = None,
        summary: str | None = None,
    ) -> dict[str, Any]:
        """最终原始目标审计前，记录已选择/提升的 Search 证据。"""
        return goal_tools.goal_plus_record_search_result(
            goal_plus_id,
            run_id,
            selected_candidate_id,
            report_path,
            promotion_artifact_path,
            summary,
        )

    @mcp.tool()
    def goal_plus_prepare_final_check(
        goal_plus_id: str,
        checker_host: Literal["codex", "pi"],
    ) -> dict[str, Any]:
        """创建或恢复必需的最终检查请求，并返回 host launch payload。"""
        return goal_tools.goal_plus_prepare_final_check(goal_plus_id, checker_host)

    @mcp.tool()
    def goal_plus_submit_final_check(
        goal_plus_id: str,
        check_id: str,
        goal_revision: int,
        verdict: Literal["pass", "fail", "interrupted"],
        summary: str,
        findings: list[dict[str, Any]] | None = None,
        evidence: list[dict[str, Any]] | None = None,
        checker_metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """为准确目标修订版记录独立最终检查结论。"""
        return goal_tools.goal_plus_submit_final_check(
            goal_plus_id,
            check_id,
            goal_revision,
            verdict,
            summary,
            findings,
            evidence,
            checker_metadata,
        )

    @mcp.tool()
    def goal_plus_set_status(
        goal_plus_id: str,
        status: str,
        reason: str | None = None,
        evidence: list[dict[str, Any]] | None = None,
        next_action: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """根据证据完成、阻塞或暂停后，设置 goal-plus 状态。"""
        return goal_tools.goal_plus_set_status(
            goal_plus_id,
            status,
            reason,
            evidence,
            next_action,
        )

    @mcp.tool()
    def goal_plus_gate(
        goal_plus_id: str,
        event: str,
        context: dict[str, Any],
    ) -> dict[str, Any]:
        """为 goal-plus 流程控制返回适合 hook 使用的 allow/block 决策。"""
        return goal_tools.goal_plus_gate(goal_plus_id, event, context)

    return mcp


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--root",
        default=DEFAULT_RUNTIME_ROOT,
        help="Search runtime storage directory",
    )
    parser.add_argument(
        "--goal-plus-stop-hook",
        action="store_true",
        help="Run the Goal Plus Stop hook instead of starting the MCP server",
    )
    parser.add_argument(
        "--goal-plus-host-hook",
        action="store_true",
        help="Run the Goal Plus host hook instead of starting the MCP server",
    )
    args = parser.parse_args()
    if args.goal_plus_stop_hook or args.goal_plus_host_hook:
        from goal_plus.goal_plus_stop_hook import main as hook_main

        raise SystemExit(hook_main())
    create_mcp(args.root).run(transport="stdio")


if __name__ == "__main__":
    main()
