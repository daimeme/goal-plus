from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.codex


def test_codex_goal_plus_skill_records_modes_and_mcp_tools() -> None:
    text = (ROOT / ".codex" / "skills" / "goal-plus" / "SKILL.md").read_text(
        encoding="utf-8"
    )

    for expected in (
        "name: goal-plus",
        "goal_plus_create",
        "goal_plus_record_triage",
        "goal_plus_save_spec_draft",
        "goal_plus_gate",
        "Goal Mode",
        "Spec Discovery Mode",
        "Search Mode",
        '"recommended_phase": "goal"',
        "goal_mode",
        "不要发送",
        "`mode`",
        "`reason`",
        "Search 是自主升级",
        "不要要求用户",
        "search_freeze_spec",
        "原始目标审计",
        "mode=autonomous",
        "mode=probe",
        ".goal-plus-verifiers/",
        "`expected_outputs`",
        '`workspace.backend="git_worktree"`',
        "/goal-plus-with-final-check",
        "/goal-plus edit",
        "/goal-plus mode=autonomous",
        "/goal-plus mode=probe",
        "`raw_goal` 的规范末行",
        "候选 lease 结束绝不会完成",
        "不单独存储任务 deadline",
        "把最新用户消息视为",
        "范围、交付物或成功标准",
        "goal_plus_update_goal",
        "在修订或恢复前先澄清",
        "不要仅因 Goal Plus 记录处于 active",
        "goal_plus_prepare_final_check",
        "goal_plus_submit_final_check",
        "spawn_agent",
        'fork_turns="none"',
        "绝不能代表审查员提交结论",
        "软 rubric 或预设评价维度",
        "开放式补充评价发生在每次 Evidence 结算之后",
        "也不改变硬",
        "candidate-local 结算",
        "明确要求 runtime reward/allocation 时才使用",
    ):
        assert expected in text
    assert "mode_hint" not in text
    assert (
        "Goal Mode 下不要创建 SearchSpec" in text
        or "不要在 Goal Mode 创建 SearchSpec" in text
    )


def test_codex_mcp_config_registers_search_runtime() -> None:
    text = (ROOT / ".codex" / "config.example.toml").read_text(encoding="utf-8")
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")

    assert "[mcp_servers.goal-plus]" in text
    assert 'command = "goal-plus"' in text
    assert 'args = ["--root", ".gp"]' in text
    for variable in (
        "CODEX_HOME",
        "OPENAI_API_KEY",
        "SFORGE_AGENT_API_KEY",
        "GOAL_PLUS_OUTER_DEADLINE_AT",
        "GOAL_PLUS_SUPPLEMENTAL_EVALUATION_ENABLED",
        "GOAL_PLUS_SUPPLEMENTAL_EVALUATION_REQUIRED",
        "GOAL_PLUS_EVIDENCE_ANNOTATOR_MODEL",
        "GOAL_PLUS_EVIDENCE_ANNOTATOR_REASONING_EFFORT",
        "GOAL_PLUS_EVIDENCE_ANNOTATOR_BASE_URL",
        "GOAL_PLUS_EVIDENCE_ANNOTATOR_PROVIDER_ID",
        "GOAL_PLUS_EVIDENCE_ANNOTATOR_PROVIDER_NAME",
        "GOAL_PLUS_EVIDENCE_ANNOTATOR_API_KEY_ENV",
        "GOAL_PLUS_EVIDENCE_ANNOTATOR_WIRE_API",
    ):
        assert f'"{variable}"' in text
    assert 'cwd = "."' not in text
    assert ".codex/config.toml" in gitignore


def test_codex_assets_wire_goal_plus_host_hooks() -> None:
    hooks = json.loads((ROOT / ".codex" / "hooks.json").read_text(encoding="utf-8"))
    expected_events = {
        "UserPromptSubmit",
        "SessionStart",
        "PreToolUse",
        "PostToolUse",
        "Stop",
        "SubagentStop",
    }

    assert hooks["hooks"].keys() == expected_events
    for event in expected_events:
        handler = hooks["hooks"][event][0]["hooks"][0]
        assert handler["type"] == "command"
        assert handler["command"] == "goal-plus --goal-plus-host-hook"
        assert "python3" not in handler["command"]
        assert handler["timeout"] == 30

    text = (ROOT / "docs" / "codex.md").read_text(encoding="utf-8")
    assert "ships project-local Goal Plus host hooks" in text
    assert "PostToolUse(goal_plus_create)" in text
    assert "UserPromptSubmit" in text
    assert "PreToolUse" in text
    assert "SubagentStop" in text
    assert "blocked until its own verifier submission" in text
    assert "Ordinary subagents do not inherit the parent's next action" in text
    assert "goal-plus --goal-plus-host-hook" in text


def test_codex_search_skill_uses_spawn_agent_and_generic_bind() -> None:
    text = (ROOT / ".codex" / "skills" / "search" / "SKILL.md").read_text(
        encoding="utf-8"
    )

    assert "search_start_agent_session" in text
    assert "search_redispatch_candidate" in text
    assert "search_get_agent_observability" in text
    assert "spawn_agent" in text
    assert "search_bind_agent_handle" in text
    assert "终态绑定会自动采集" in text
    assert "search_bind_opencode_session" not in text
    assert "background" not in text.lower()
    assert "## Verifier 冻结契约" in text
    assert '数值类型的 `spec.metric_name`' in text
    assert ".goal-plus-verifiers/" in text
    assert "`expected_outputs` 只接受" in text
    assert "GOAL_PLUS_VERIFIER_TMPDIR" in text
    assert "VerifierWorkspaceSideEffect" in text
    assert "固定 `/tmp`" in text
    assert '`workspace.backend="git_worktree"`' in text


def test_codex_search_skill_projects_launch_metadata_to_current_tool_schema() -> None:
    text = (ROOT / ".codex" / "skills" / "search" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    normalized = " ".join(text.split())

    assert "当前 `spawn_agent` 工具 schema" in text
    assert "`task_name`、`message` 和 `fork_turns`" in text
    assert "继承父 Codex model" in text
    assert "绝不能合成可选 launch metadata" in normalized
    assert "agent_type=launch.agent_type" not in text


def test_codex_search_skill_documents_parallel_loop_policy() -> None:
    text = (ROOT / ".codex" / "skills" / "search" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    normalized = " ".join(text.split())

    assert "orchestration_mode: parallel_loops" in text
    assert "一个候选工作区就是一条自主搜索循环" in normalized
    assert "只创建一次" in normalized
    assert "运行时拒绝对该 run 的第二次 `search_plan_next`" in normalized
    assert "使用无 target" in text
    assert "`wait_agent`" in text
    assert "`list_agents`" in text
    assert "`followup_task`" in text
    assert "同一个原生 Codex subagent" in normalized
    assert "继续同一条自主搜索循环" in text
    assert "低分" in normalized
    assert "不是停止或替换" in normalized
    assert "不要再调用 `search_plan_next` 或 `search_start_batch`" in normalized
    assert "最终选择：只能在所有 worker drain 后" in normalized
    assert "source_run_id" in text
    assert "search_invalidate_run" in text
    assert "interrupt_agent" in text
    assert "deepen_incumbent" not in text
    assert "transfer_feature" not in text
    assert "macro_restart" not in text


def test_codex_assets_document_runtime_owned_adaptive_allocation() -> None:
    skill = (ROOT / ".codex" / "skills" / "search" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    worker = (ROOT / ".codex" / "agents" / "search_candidate_agent.toml").read_text(
        encoding="utf-8"
    )

    assert "## Adaptive Search 契约" in skill
    assert "只有 FrozenSpec 显式设置以下配置时" in skill
    assert "orchestration_mode: adaptive_search" in skill
    assert "budget.max_candidates" in skill
    assert "search_list_allocation_decisions" in skill
    assert "search_apply_allocation_decision" in skill
    assert "spawn_agent" in skill
    assert "reward 只用于 allocation" in skill
    assert "annotation task" in skill
    assert "互不依赖的结算后分支" in skill
    assert "缺失 task 会从已持久化 iteration 幂等补齐" in skill
    assert "只 fence 下一轮 candidate iteration" in skill
    assert "allocation_decision" in worker
    assert '`context.orchestration_mode == "adaptive_search"`' in worker
    assert "并且 `search_run_verifier` 明确返回非空" in worker
    assert "独立持久化" in worker
    assert "worker 不自行计算 reward" in worker
    assert "异步 View 会在" in worker
    assert "进入 Global Evidence" in worker


def test_codex_search_skill_documents_worker_budget_watchdog() -> None:
    text = (ROOT / ".codex" / "skills" / "search" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    normalized = " ".join(text.split())

    assert "budget_control" in text
    assert "parent_watchdog" in text
    assert "initial_wait_timeout_ms" in text
    assert "soft_closeout_seconds" in text
    assert "closeout_message" in text
    assert "final_wait_timeout_ms" in text
    assert "wait_agent" in text
    assert "send_message" in text
    assert "interrupt_agent" in text
    assert "仅供参考的时间提示" in text
    assert "GOAL_PLUS_OUTER_DEADLINE_AT" in text
    assert "主 agent、普通 subagent 和最终检查员" in normalized
    assert "worker_budget.min_runtime_seconds" in text
    assert "budget_control.autoresearch_lease" in text
    assert "同一个 Codex worker" in text
    assert "该 lease active 时，绝不能发送父级 closeout" in text


def test_codex_worker_records_progress_handoff_before_returning() -> None:
    text = (ROOT / ".codex" / "agents" / "search_candidate_agent.toml").read_text(
        encoding="utf-8"
    )

    assert ".tmp/handoff.json" in text
    assert "summary" in text
    assert "key_results" in text
    assert "pitfalls" in text
    assert "condition" in text
    assert "failed_approach" in text
    assert "把分配的候选思路当作假设" in text
    assert "把任何有希望的方向当作" in text
    assert "固定产物" in text
    assert "理论或结构限制" in text
    assert "10-15 distinct verifier-recorded artifacts" not in text
    assert "verifier 是评估器，不是分析服务" in text
    assert "PostTool 时间提示仅供参考" in text
    assert "AutoResearch 最短运行时间" in text
    assert "不要休眠或忙等" in text
    assert "candidate_action=stop_and_report" in text
    assert "立即返回" in text
    assert "不要自行 reset" in text
    assert "disposition" in text
    assert "同分为 `retain`" in text
    assert "candidate-local\n最新基线" in text
    assert "verifier_assessment" in text
    assert "code_surface" in text
    assert "measured_effect" in text
    assert "portability" in text
    assert "relation_to_incumbent" in text
    assert "candidate_local" in text
    assert "feature_family" in text
    assert "evaluation_contract" in text
    assert "single_observation" in text
    assert "candidate-local analysis scripts" not in text


def test_codex_search_skill_documents_state_level_resume() -> None:
    text = (ROOT / ".codex" / "skills" / "search" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    agent = (ROOT / ".codex" / "agents" / "search_candidate_agent.toml").read_text(
        encoding="utf-8"
    )

    assert "candidate-local history 由运行时拥有，不是 `plan.md` 文件" in text
    assert "状态级恢复" in text
    assert "context.history" not in text
    assert "context.iterations" in text
    assert "worker_budget.max_runtime_seconds" in text
    assert "search_redispatch_candidate" in text
    assert "一次派发" in text
    assert "handoff metadata" in text
    assert "不要依赖聊天记录" in agent


def test_codex_worker_agent_calls_context_and_verifier() -> None:
    text = (ROOT / ".codex" / "agents" / "search_candidate_agent.toml").read_text(
        encoding="utf-8"
    )

    assert 'name = "search_candidate_agent"' in text
    assert "search_get_agent_context" in text
    assert "search_get_global_evidence" in text
    assert "每完成 3 次 `search_run_verifier` iteration" in text
    assert "global_evidence_snapshot` 已完成本次刷新" in text
    assert "search_submit_iteration_plan" not in text
    assert "search_run_verifier" in text
    assert "不得直接运行任务自带的 `runner`、`evaluator` 或 `grader`" in text
    assert "所有正确性与指标反馈必须通过 `search_run_verifier`" in text
    assert "不要传 `scope`" in text
    assert "run_id=..., candidate_id=..., agent_session_id=..., hypothesis=..., toolization_decision=..." in text
    assert "工作区根目录" in text
    assert "且只追加一条已验证记录" in text
    assert "view=null" in text
    assert "hypothesis" in text
    assert "search_copy_shared_tool" in text
    assert "search_stage_shared_tool" in text
    assert ".tmp/tool-drafts" in text
    assert "repeated_sequence" in text
    assert "domain_probe" in text
    assert "parser_or_trace" in text
    assert "peer_setup_reduction" in text
    assert "not_applicable" in text
    assert "staging inventory 始终是权威事实" in text
    assert "required-column-probe" not in text
    assert "mutation-check-trace" not in text
    assert "Astropy" not in text
    assert "receipt" in text
    assert "shared_tools[*].tool_view" in text
    assert "git diff HEAD <commit> -- <allowed-file>" in text
    assert "一条自主 Search 循环" in text
    assert "不要等待父 agent 选择方向" in text
    assert "search_select" in text
    assert "search_report" in text
    assert "search_promote" in text


def test_codex_search_reuses_exact_worker_evidence_before_parent_verification() -> None:
    text = (ROOT / ".codex" / "skills" / "search" / "SKILL.md").read_text(
        encoding="utf-8"
    )

    assert "准确 worker Evidence" in text
    assert "仅在没有匹配 Evidence 时" in text
    assert 'search_run_verifier(hypothesis="主流程完成验证")' not in text
    assert "shared_tools[*].tool_view" in text
    assert "Tool View 前不向 candidate 暴露" in text
    assert "search_copy_shared_tool" in text
    assert "search_stage_shared_tool" in text
    assert "toolization_decision" in text
    assert "receipt" in text


def test_codex_search_uses_open_posthoc_evaluation_as_non_gating_feedback() -> None:
    skill = (ROOT / ".codex" / "skills" / "search" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    agent = (ROOT / ".codex" / "agents" / "search_candidate_agent.toml").read_text(
        encoding="utf-8"
    )
    combined = "\n".join((skill, agent))

    assert "不来自 FrozenSpec" in combined
    assert "动态比较" in combined
    assert "不改变硬分结算规则" in combined
    assert "不改变结算、硬 score" in combined
    assert "或最终 PASS/FAIL" in combined


def test_codex_goal_plus_defers_report_until_terminal_state() -> None:
    goal_skill = (
        ROOT / ".codex" / "skills" / "goal-plus" / "SKILL.md"
    ).read_text(encoding="utf-8")
    search_skill = (ROOT / ".codex" / "skills" / "search" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    normalized_goal_skill = " ".join(goal_skill.split())

    assert "此时不要调用" in goal_skill
    assert "`search_report`" in goal_skill
    assert "绝不能生成中间 Goal Plus 报告" in normalized_goal_skill
    assert "只有 Goal Plus 记录达到终态" in goal_skill
    assert "对独立 Search" in search_skill
    assert "只在提升后调用 `search_report`" in search_skill


def test_codex_final_checker_and_with_check_alias_are_read_only_and_independent() -> None:
    checker = (ROOT / ".codex" / "agents" / "goal_plus_final_checker.toml").read_text(
        encoding="utf-8"
    )
    alias = (
        ROOT / ".codex" / "skills" / "goal-plus-with-final-check" / "SKILL.md"
    ).read_text(encoding="utf-8")

    assert 'name = "goal_plus_final_checker"' in checker
    assert "只能进行只读操作" in checker
    assert "goal_plus_status" in checker
    assert "goal_plus_submit_final_check" in checker
    assert "绝不能编辑文件" in checker
    assert "name: goal-plus-with-final-check" in alias
    assert 'checker_host="codex"' in alias
    assert "spawn_agent" in alias
    assert "/goal-plus resume" in alias


def test_codex_docs_record_log_inspection_paths() -> None:
    text = (ROOT / "docs" / "codex.md").read_text(encoding="utf-8")
    debug = (ROOT / "docs" / "debugging-runtime.md").read_text(encoding="utf-8")

    combined = text + "\n" + debug
    assert "codex exec --json" in combined
    assert "CODEX_HOME" in combined
    assert "rollout-*.jsonl" in combined
    assert "RUST_LOG=debug" in combined
    assert "log_dir=./.codex-log" in combined


def test_codex_docs_record_native_parity_contract() -> None:
    codex = (ROOT / "docs" / "codex.md").read_text(encoding="utf-8")
    adapters = (ROOT / "docs" / "agent-host-adapters.md").read_text(encoding="utf-8")
    debugging = (ROOT / "docs" / "debugging-runtime.md").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")

    assert "Codex 0.144.1" in codex
    assert "initial_wait_timeout_ms" in codex
    assert "closeout_message" in codex
    assert "final_wait_timeout_ms" in codex
    assert "worker_launch" in codex
    assert "codex_parallel_loop_cycle" in codex
    assert "autonomous" in codex
    assert "parallel" in adapters
    assert "current `spawn_agent` schema" in adapters
    for text in (adapters, debugging, readme, agents):
        assert "UserPromptSubmit" in text
        assert "PreToolUse" in text
        assert "SubagentStop" in text
    assert "PreToolUse/SubagentStop gates remain manual" not in agents


def test_codex_goal_plus_skill_documents_multiple_search_tasks() -> None:
    text = (ROOT / ".codex" / "skills" / "goal-plus" / "SKILL.md").read_text(
        encoding="utf-8"
    )

    assert "`goal_plus_id`" in text
    assert "另一项 Search 任务" in text
    assert "`search_tasks` 是其仅追加" in text
    assert "仅追加" in text
    assert "`linked_search` 只是当前任务的兼容视图" in text
    assert "规划轮次和已启动搜索轮次" in text
