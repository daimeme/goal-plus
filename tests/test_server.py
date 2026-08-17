from __future__ import annotations

import asyncio
from pathlib import Path

from fastmcp import FastMCP

import goal_plus.server as server_module
from goal_plus.server import create_mcp


def test_create_mcp(tmp_path: Path) -> None:
    mcp = create_mcp(tmp_path / ".search")
    assert isinstance(mcp, FastMCP)


def test_create_mcp_registers_search_runtime_tools(tmp_path: Path) -> None:
    mcp = create_mcp(tmp_path / ".search")

    tools = asyncio.run(mcp.get_tools())

    assert set(tools) == {
        "search_freeze_spec",
        "search_create",
        "search_status",
        "search_invalidate_run",
        "search_list_history",
        "search_plan_next",
        "search_start_batch",
        "search_start_agent_session",
        "search_redispatch_candidate",
        "search_bind_agent_handle",
        "search_continue_agent_session",
        "search_get_agent_context",
        "search_get_global_evidence",
        "search_stage_shared_tool",
        "search_copy_shared_tool",
        "search_get_evidence_detail",
        "search_get_agent_observability",
        "search_run_verifier",
        "search_list_iterations",
        "search_select",
        "search_report",
        "search_promote",
        "goal_plus_create",
        "goal_plus_status",
        "goal_plus_update_goal",
        "goal_plus_monitor_snapshot",
        "goal_plus_list_models",
        "goal_plus_record_triage",
        "goal_plus_save_spec_draft",
        "goal_plus_link_search_run",
        "goal_plus_record_search_result",
        "goal_plus_prepare_final_check",
        "goal_plus_submit_final_check",
        "goal_plus_set_status",
        "goal_plus_gate",
    }


def test_invalidate_run_and_successor_expose_contract_fields(tmp_path: Path) -> None:
    mcp = create_mcp(tmp_path / ".search")
    tools = asyncio.run(mcp.get_tools())

    create_properties = tools["search_create"].parameters["properties"]
    invalidation = tools["search_invalidate_run"].parameters

    assert "source_run_id" in create_properties
    assert set(create_properties) == {"frozen_spec_id", "source_run_id"}
    assert "goal_plus_list_models" in tools
    assert set(invalidation["required"]) == {
        "run_id",
        "reason",
        "summary",
        "evidence",
    }
    assert "verifier_coverage_inadequate" in invalidation["properties"]["reason"][
        "enum"
    ]
    description = " ".join(tools["search_invalidate_run"].description.split())
    assert "原子地阻止新规划" in description
    assert "中断 host pool" in description


def test_start_agent_session_returns_launch_payload(tmp_path: Path) -> None:
    mcp = create_mcp(tmp_path / ".search")

    tools = asyncio.run(mcp.get_tools())
    schema = tools["search_start_agent_session"].parameters

    properties = schema["properties"]
    assert "candidate_id" in properties
    assert "directive" in properties
    assert "worker_budget" in properties
    # The legacy admission parameters (budget, visibility_mode) are gone.
    assert "budget" not in properties
    assert "visibility_mode" not in properties


def test_redispatch_candidate_exposes_worker_overrides(tmp_path: Path) -> None:
    mcp = create_mcp(tmp_path / ".search")

    tools = asyncio.run(mcp.get_tools())
    schema = tools["search_redispatch_candidate"].parameters

    properties = schema["properties"]
    assert "run_id" in properties
    assert "candidate_id" in properties
    assert "directive" not in properties
    assert "worker_agent_type" in properties
    assert "worker_budget" in properties


def test_continue_agent_session_exposes_neutral_continuation_schema(tmp_path: Path) -> None:
    mcp = create_mcp(tmp_path / ".search")

    tools = asyncio.run(mcp.get_tools())

    assert "agent_session_id" in tools["search_continue_agent_session"].parameters["properties"]
    assert "directive" not in tools["search_continue_agent_session"].parameters["properties"]


def test_run_verifier_exposes_optional_agent_session_id(tmp_path: Path) -> None:
    mcp = create_mcp(tmp_path / ".search")

    tools = asyncio.run(mcp.get_tools())
    schema = tools["search_run_verifier"].parameters

    assert "agent_session_id" in schema["properties"]
    assert "hypothesis" in schema["properties"]
    assert "toolization_decision" in schema["properties"]
    assert "adopted_tools" not in schema["properties"]
    assert schema["properties"]["scope"]["enum"] == ["process", "promotion"]
    decision_schema = schema["properties"]["toolization_decision"]["anyOf"][0]
    signal_schema = decision_schema["properties"]["signals"]
    assert signal_schema["items"]["enum"] == [
        "repeated_workflow",
        "domain_construction_or_probe",
        "behavior_or_invariant_checker",
        "reproducer_fixture_or_case_generator",
        "parser_trace_or_comparator",
        "peer_setup_or_feedback_reduction",
    ]
    assert signal_schema["maxItems"] == 6
    assert signal_schema["description"] == (
        "Use the six capability-oriented toolization signals."
    )
    assert tools["search_copy_shared_tool"].parameters["required"] == [
        "agent_session_id",
        "tool_id",
        "snapshot_hash",
    ]
    copy_description = tools["search_copy_shared_tool"].description
    assert "供阅读源码或自主复用" in copy_description
    assert "复制不要求调用原工具" in copy_description
    assert "只有直接执行、导入或依赖原快照时" in copy_description
    assert "当前 adopted_tools 只证明快照曾复制进本轮上下文" in copy_description
    assert tools["search_stage_shared_tool"].parameters["required"] == [
        "agent_session_id",
        "name",
        "summary",
        "entrypoint",
        "candidate_relative_source_paths",
    ]
    stage_description = tools["search_stage_shared_tool"].description
    assert "revision_allowed=true" in stage_description
    assert "revision_head.tool_id" in stage_description
    assert "capability_extension 必须" in stage_description
    assert "adoption_fix 必须有同 family 的真实" in stage_description
    assert "contract_change 必须" in stage_description
    assert "不得靠改名或同义键制造增量" in stage_description
    assert "搜索期间的测试/checker/harness 可以 staging" in stage_description
    assert "最终交付测试、冻结 verifier/runner/grader" in stage_description
    assert tools["search_get_global_evidence"].parameters["required"] == [
        "agent_session_id"
    ]
    assert tools["search_get_evidence_detail"].parameters["required"] == [
        "agent_session_id",
        "candidate_id",
        "iteration",
    ]
    assert tools["search_get_evidence_detail"].parameters["properties"]["iteration"][
        "minimum"
    ] == 1


def test_freeze_spec_exposes_complete_nested_search_spec_schema(tmp_path: Path) -> None:
    mcp = create_mcp(tmp_path / ".search")
    tools = asyncio.run(mcp.get_tools())

    spec_schema = tools["search_freeze_spec"].parameters["properties"]["spec"]

    assert set(spec_schema["required"]) >= {
        "objective",
        "metric_name",
        "metric_direction",
        "source_path",
        "edit_surface",
        "budget",
        "process_verifiers",
    }
    assert spec_schema["properties"]["edit_surface"]["required"] == ["allow"]
    verifier = spec_schema["properties"]["process_verifiers"]["items"]
    assert set(verifier["required"]) == {"name", "role", "command"}
    assert "ranking_signal" in verifier["properties"]["role"]["enum"]
    assert verifier["properties"]["command"]["type"] == "array"
    strategy = spec_schema["properties"]["strategy"]
    assert "worker_budget" in strategy["properties"]
    assert "evidence_annotator" in strategy["properties"]

    budget = spec_schema["properties"]["budget"]["properties"]
    assert "初始创建并实际并行工作" in budget["max_parallel"]["description"]

    freeze_description = " ".join(tools["search_freeze_spec"].description.split())
    assert "唯一决定初始候选 Agent 数" in freeze_description
    assert "一次性源码副本" in freeze_description
    assert "GOAL_PLUS_VERIFIER_TMPDIR" in freeze_description
    assert "固定 `/tmp` 路径不安全" in freeze_description

    verifier_description = " ".join(
        tools["search_run_verifier"].description.split()
    )
    assert "VerifierWorkspaceSideEffect" in verifier_description
    assert 'candidate_action="stop_and_report"' in verifier_description


def test_plan_next_exposes_per_round_budget_semantics(tmp_path: Path) -> None:
    mcp = create_mcp(tmp_path / ".search")
    tools = asyncio.run(mcp.get_tools())

    plan_tool = tools["search_plan_next"]
    requested_k = plan_tool.parameters["properties"]["requested_k"]

    assert requested_k["default"] == 4
    assert requested_k["exclusiveMinimum"] == 0
    assert "请求初始候选数" in requested_k["description"]
    assert "标准流程必须传入 budget.max_parallel" in requested_k["description"]
    assert "`requested_k` 与" in plan_tool.description
    assert "标准流程必须令两者相等" in plan_tool.description


def test_spec_draft_exposes_partial_nested_search_spec_schema(tmp_path: Path) -> None:
    mcp = create_mcp(tmp_path / ".search")
    tools = asyncio.run(mcp.get_tools())

    draft = tools["goal_plus_save_spec_draft"].parameters["properties"][
        "spec_draft"
    ]
    typed_search_spec = draft["properties"]["search_spec"]

    assert "objective" in typed_search_spec["properties"]
    assert "edit_surface" in typed_search_spec["properties"]
    assert "budget" in typed_search_spec["properties"]
    assert "process_verifiers" in typed_search_spec["properties"]
    assert "required" not in typed_search_spec

    draft_budget_schema = typed_search_spec["properties"]["budget"]
    draft_budget = next(
        option
        for option in draft_budget_schema["anyOf"]
        if option.get("type") == "object"
    )["properties"]
    assert "初始创建并实际并行工作" in draft_budget["max_parallel"]["description"]


def test_goal_plus_gate_exposes_hook_friendly_schema(tmp_path: Path) -> None:
    mcp = create_mcp(tmp_path / ".search")

    tools = asyncio.run(mcp.get_tools())
    schema = tools["goal_plus_gate"].parameters

    assert "goal_plus_id" in schema["properties"]
    assert "event" in schema["properties"]
    assert "context" in schema["properties"]


def test_goal_plus_monitor_snapshot_exposes_read_only_schema(tmp_path: Path) -> None:
    mcp = create_mcp(tmp_path / ".search")

    tools = asyncio.run(mcp.get_tools())
    schema = tools["goal_plus_monitor_snapshot"].parameters

    assert "goal_plus_id" in schema["properties"]
    assert "run_id" in schema["properties"]
    assert "stale_after_seconds" in schema["properties"]


def test_agent_observability_exposes_read_only_schema(tmp_path: Path) -> None:
    mcp = create_mcp(tmp_path / ".search")

    tools = asyncio.run(mcp.get_tools())
    schema = tools["search_get_agent_observability"].parameters

    assert set(schema["required"]) == {"agent_session_id"}
    description = " ".join(tools["search_get_agent_observability"].description.split())
    assert "只读" in description
    assert "reasoning" in description


def test_goal_plus_create_has_no_mode_hint_or_verifier_confirm_tool(
    tmp_path: Path,
) -> None:
    mcp = create_mcp(tmp_path / ".search")

    tools = asyncio.run(mcp.get_tools())
    create_schema = tools["goal_plus_create"].parameters

    assert "mode_hint" not in create_schema["properties"]
    assert "raw_goal" in create_schema["properties"]
    assert "goal_plus_confirm_frozen_verifier" not in tools
    update_schema = tools["goal_plus_update_goal"].parameters
    prepare_schema = tools["goal_plus_prepare_final_check"].parameters
    submit_schema = tools["goal_plus_submit_final_check"].parameters
    assert set(update_schema["required"]) >= {
        "goal_plus_id",
        "raw_goal",
        "expected_revision",
    }
    assert "checker_host" in prepare_schema["properties"]
    assert set(submit_schema["required"]) >= {
        "goal_plus_id",
        "check_id",
        "goal_revision",
        "verdict",
        "summary",
    }
    assert submit_schema["properties"]["verdict"]["enum"] == [
        "pass",
        "fail",
        "interrupted",
    ]


def test_create_mcp_constructs_runtime_with_configured_root(
    tmp_path: Path,
    monkeypatch,
) -> None:
    created_runtimes = []
    created_goal_runtimes = []

    class FakeRuntime:
        def __init__(self, root_dir):
            created_runtimes.append(root_dir)

    class FakeGoalRuntime:
        def __init__(self, root_dir):
            created_goal_runtimes.append(root_dir)

    class FakeTools:
        def __init__(self, runtime):
            self.runtime = runtime

        def search_freeze_spec(self, *args, **kwargs):
            return {}

        def search_create(self, *args, **kwargs):
            return {}

        def search_status(self, *args, **kwargs):
            return {}

        def search_list_history(self, *args, **kwargs):
            return {}

        def search_plan_next(self, *args, **kwargs):
            return {}

        def search_start_batch(self, *args, **kwargs):
            return []

        def search_start_agent_session(self, *args, **kwargs):
            return {}

        def search_bind_agent_handle(self, *args, **kwargs):
            return {}

        def search_continue_agent_session(self, *args, **kwargs):
            return {}

        def search_get_agent_context(self, *args, **kwargs):
            return {}

        def search_get_global_evidence(self, *args, **kwargs):
            return []

        def search_get_evidence_detail(self, *args, **kwargs):
            return {}

        def search_run_verifier(self, *args, **kwargs):
            return {}

        def search_list_iterations(self, *args, **kwargs):
            return []

        def search_select(self, *args, **kwargs):
            return {}

        def search_report(self, *args, **kwargs):
            return {}

        def search_promote(self, *args, **kwargs):
            return {}

    class FakeGoalTools:
        def __init__(self, runtime):
            self.runtime = runtime

        def goal_plus_create(self, *args, **kwargs):
            return {}

        def goal_plus_status(self, *args, **kwargs):
            return {}

        def goal_plus_record_triage(self, *args, **kwargs):
            return {}

        def goal_plus_save_spec_draft(self, *args, **kwargs):
            return {}

        def goal_plus_link_search_run(self, *args, **kwargs):
            return {}

        def goal_plus_record_search_result(self, *args, **kwargs):
            return {}

        def goal_plus_set_status(self, *args, **kwargs):
            return {}

        def goal_plus_gate(self, *args, **kwargs):
            return {}

    monkeypatch.setattr(server_module, "FileSearchRuntime", FakeRuntime)
    monkeypatch.setattr(server_module, "FileGoalPlusRuntime", FakeGoalRuntime)
    monkeypatch.setattr(server_module, "SearchTools", FakeTools)
    monkeypatch.setattr(server_module, "GoalPlusTools", FakeGoalTools)

    mcp = create_mcp(tmp_path / "custom-search")

    assert isinstance(mcp, FastMCP)
    assert created_runtimes == [tmp_path / "custom-search"]
    assert created_goal_runtimes == [tmp_path / "custom-search"]
