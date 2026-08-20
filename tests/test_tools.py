from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

from goal_plus.models import (
    AgentHostHandle,
    AgentSessionRecord,
    CandidateProposal,
    CandidateTask,
    FrozenSpec,
    GoalPlusGateResult,
    GoalPlusNextAction,
    GoalPlusRecord,
    RunState,
    RunRecord,
    RunSummary,
    ScoreReport,
    SearchPlan,
    SearchSpec,
    StrategySpec,
    VerifierResult,
    VerifierRole,
)
from goal_plus.tools import GoalPlusTools, SearchTools


def spec_dict() -> dict:
    return {
        "objective": "maximize toy score",
        "metric_name": "combined_score",
        "metric_direction": "maximize",
        "source_path": ".",
        "edit_surface": {
            "allow": ["initial_program.py"],
            "deny": ["evaluator.py"],
        },
        "budget": {
            "max_parallel": 2,
        },
        "process_verifiers": [
            {
                "name": "score",
                "role": "ranking_signal",
                "command": ["python", "evaluator.py"],
            }
        ],
    }


def frozen_spec() -> FrozenSpec:
    spec = SearchSpec.model_validate(spec_dict())
    return FrozenSpec(
        frozen_spec_id="spec_123",
        spec_hash="hash",
        spec=spec,
        verifier_hashes={"evaluator.py": "abc"},
        frozen_verifier_paths={"evaluator.py": "/tmp/evaluator.py"},
        created_at="2026-06-24T00:00:00Z",
    )


def goal_plus_record() -> GoalPlusRecord:
    return GoalPlusRecord(
        goal_plus_id="gp_0001",
        raw_goal="Optimize a benchmark if possible",
        status="active",
        phase="intake",
        next_action=GoalPlusNextAction(
            kind="record_triage",
            description="classify the goal",
        ),
        created_at="2026-07-06T00:00:00Z",
        updated_at="2026-07-06T00:00:00Z",
    )


def test_search_freeze_spec_converts_input_and_serializes_output() -> None:
    runtime = Mock()
    runtime.freeze_spec.return_value = frozen_spec()
    tools = SearchTools(runtime)

    result = tools.search_freeze_spec(spec_dict(), ["evaluator.py"])

    spec_arg, paths_arg = runtime.freeze_spec.call_args.args
    assert isinstance(spec_arg, SearchSpec)
    assert paths_arg == [Path("evaluator.py")]
    assert result["frozen_spec_id"] == "spec_123"
    assert result["spec"]["process_verifiers"][0]["role"] == "ranking_signal"


def test_search_tools_delegate_runtime_calls_with_models() -> None:
    runtime = Mock()
    agent_session = AgentSessionRecord(
        agent_session_id="agent_001",
        run_id="run_1",
        candidate_id="c001",
        created_at="2026-06-24T00:00:00Z",
        updated_at="2026-06-24T00:00:00Z",
        workspace=Path("/tmp/c001"),
        launch={
            "agent_type": "search_candidate_agent",
            "description": "c001 try one",
            "prompt": "agent_session_id=agent_001; candidate_id=c001; idea: try one",
        },
    )
    runtime.create_run.return_value = "run_1"
    runtime.status.return_value = RunSummary(
        run_id="run_1",
        state=RunState.RUNNING,
        frozen_spec_id="spec_123",
        candidates_total=0,
        candidates_evaluated=0,
    )
    runtime.invalidate_run.return_value = RunRecord(
        run_id="run_1",
        state=RunState.ABORTED,
        frozen_spec_id="spec_123",
        source_path=".",
        created_at="2026-06-24T00:00:00Z",
        invalidated_at="2026-06-24T00:01:00Z",
        invalidation_reason="verifier_coverage_inadequate",
        invalidation_summary="missing required case",
        invalidation_evidence=[{"case": "edge"}],
    )
    runtime.list_history.return_value = {
        "run_id": "run_1",
        "candidates": [],
    }
    runtime.plan_next.return_value = SearchPlan(
        run_id="run_1",
        plan_id="plan_001",
        strategy=StrategySpec(),
        requested_k=1,
        planned_k=1,
        remaining_budget=4,
        created_at="2026-06-24T00:00:00Z",
    )
    runtime.start_batch.return_value = [
        CandidateTask(
            run_id="run_1",
            candidate_id="c001",
            hypothesis="try",
            workspace=Path("/tmp/c001"),
            allowed_files=["initial_program.py"],
            denied_files=["evaluator.py"],
        )
    ]
    runtime.start_agent_session.return_value = agent_session
    runtime.get_global_evidence.return_value = [
        {
            "candidate_id": "c001",
            "iteration": 1,
            "score": 1.0,
            "disposition": "keep",
            "commit": "abc123",
            "view": "Changed the candidate value.",
        }
    ]
    runtime.get_evidence_detail.return_value = {
        "candidate_id": "c001",
        "iteration": 1,
        "supplemental_evaluation": {"summary": "Changed the candidate value."},
    }
    runtime.redispatch_candidate.return_value = agent_session.model_copy(
        update={
            "agent_session_id": "agent_002",
            "launch": {
                "agent_type": "search_candidate_agent_deep",
                "description": "c001 resume",
                "prompt": "state_level_resume=true; agent_session_id=agent_002",
            },
        }
    )
    generic_bound_session = agent_session.model_copy(
        update={
            "host": "codex",
            "host_handle": AgentHostHandle(host="codex", task_name="search_agent_001"),
        }
    )
    continued_session = generic_bound_session.model_copy(
        update={
            "launch": {
                "task_name": "search_agent_001",
                "agent_type": "search_candidate_agent",
                "description": "c001 continue try one",
                "prompt": "continue_existing_agent_session=true; agent_session_id=agent_001",
            }
        }
    )
    runtime.bind_agent_handle.return_value = generic_bound_session
    runtime.continue_agent_session.return_value = continued_session
    runtime.get_agent_context.return_value = {"agent_session_id": "agent_001"}
    runtime.get_agent_observability.return_value = {
        "agent_session_id": "agent_001",
        "source": "codex_session_jsonl",
    }
    runtime.should_inject_global_evidence_after_verifier.return_value = False
    runtime.run_verifier.return_value = ScoreReport(
        run_id="run_1",
        candidate_id="c001",
        validity_passed=True,
        process_passed=True,
        aggregate_score=1.0,
        verifier_results=[
            VerifierResult(
                name="score",
                role=VerifierRole.RANKING_SIGNAL,
                passed=True,
                score=1.0,
                metrics={"stderr_tail": "candidate diagnostic"},
                log_path=Path("/tmp/iteration-0001-score-a1b2c3d4.log"),
            )
        ],
    )
    runtime.list_iterations.return_value = [
        {"iteration": 1, "score": 0.4, "agent_session_id": "agent_001"},
        {"iteration": 2, "score": 0.7, "agent_session_id": "agent_001"},
    ]
    runtime.list_allocation_decisions.return_value = []
    runtime.apply_allocation_decision.return_value = {
        "decision": {"decision_id": "allocation_0001", "status": "applied"},
        "candidate_tasks": [],
        "agent_sessions": [],
    }
    runtime.select.return_value = {"selected_candidate_id": "c001"}
    runtime.report.return_value = Path("/tmp/report.md")
    runtime.promote.return_value = Path("/tmp/c001.patch")
    tools = SearchTools(runtime)

    assert tools.search_create("spec_123") == {"run_id": "run_1"}
    invalidated = tools.search_invalidate_run(
        "run_1",
        "verifier_coverage_inadequate",
        "missing required case",
        [{"case": "edge"}],
    )
    assert invalidated["state"] == "aborted"
    assert tools.search_status("run_1")["state"] == "running"
    assert tools.search_list_history("run_1", top_n=3, sort_by="created") == {
        "run_id": "run_1",
        "candidates": [],
    }
    assert tools.search_plan_next("run_1", requested_k=1)["plan_id"] == "plan_001"
    assert tools.search_start_batch(
        "run_1",
        "plan_001",
        [{"intent": "try an independent approach"}],
    )[0]["candidate_id"] == "c001"
    assert tools.search_start_agent_session(
        "run_1",
        "c001",
        {"goal": "try one"},
    )["agent_session_id"] == "agent_001"
    redispatched = tools.search_redispatch_candidate(
        "run_1",
        "c001",
        worker_agent_type="search_candidate_agent_deep",
        worker_budget={"max_turns": 16},
    )
    assert redispatched["agent_session_id"] == "agent_002"
    assert redispatched["launch"]["agent_type"] == "search_candidate_agent_deep"
    assert tools.search_bind_agent_handle(
        "agent_001",
        {"host": "codex", "task_name": "search_agent_001"},
    )["host_handle"]["task_name"] == "search_agent_001"
    continued = tools.search_continue_agent_session("agent_001")
    assert continued["launch"]["task_name"] == "search_agent_001"
    assert tools.search_get_agent_context("agent_001") == {"agent_session_id": "agent_001"}
    assert tools.search_get_global_evidence("agent_001")[0]["view"] == (
        "Changed the candidate value."
    )
    runtime.stage_shared_tool.return_value = {
        "staged_name": "trace-helper",
        "staging_path": "/tmp/c001/.tmp/share-out/trace-helper",
    }
    assert tools.search_stage_shared_tool(
        "agent_001",
        "trace-helper",
        "Collect a bounded trace.",
        "trace.py:main",
        [".tmp/tool-drafts/trace.py"],
    )["staged_name"] == "trace-helper"
    assert tools.search_get_evidence_detail("agent_001", "c001", 1)[
        "supplemental_evaluation"
    ]["summary"] == "Changed the candidate value."
    runtime.get_evidence_detail.assert_called_once_with("agent_001", "c001", 1)
    assert tools.search_get_agent_observability("agent_001") == {
        "agent_session_id": "agent_001",
        "source": "codex_session_jsonl",
    }
    verifier_report = tools.search_run_verifier("run_1", "c001")
    assert verifier_report["aggregate_score"] == 1.0
    assert verifier_report["verifier_results"][0]["metrics"]["stderr_tail"] == (
        "candidate diagnostic"
    )
    assert verifier_report["verifier_results"][0]["log_path"].endswith(
        "iteration-0001-score-a1b2c3d4.log"
    )
    worker_verifier_report = tools.search_run_verifier(
        "run_1",
        "c001",
        agent_session_id="agent_001",
        hypothesis="try a fused path",
        toolization_decision=None,
    )
    assert worker_verifier_report["aggregate_score"] == 1.0
    assert "global_evidence_injected" not in worker_verifier_report
    assert "global_evidence_snapshot" not in worker_verifier_report
    runtime.run_verifier.assert_called_with(
        "run_1",
        "c001",
        scope="process",
        agent_session_id="agent_001",
        hypothesis="try a fused path",
        toolization_decision=None,
    )
    iterations = tools.search_list_iterations("run_1", "c001")
    assert len(iterations) == 2
    assert iterations[0]["iteration"] == 1
    assert iterations[1]["score"] == 0.7
    runtime.list_iterations.assert_called_once_with("run_1", "c001")
    assert tools.search_list_allocation_decisions("run_1", "pending") == []
    runtime.list_allocation_decisions.assert_called_once_with("run_1", "pending")
    applied = tools.search_apply_allocation_decision("run_1", "allocation_0001")
    assert applied["decision"]["status"] == "applied"
    runtime.apply_allocation_decision.assert_called_once_with(
        "run_1", "allocation_0001"
    )
    assert tools.search_select("run_1") == {"selected_candidate_id": "c001"}
    assert tools.search_report("run_1") == {
        "report_path": "/tmp/report.md",
        "html_report_path": "/tmp/report.html",
    }
    assert tools.search_promote("run_1", "c001") == {"artifact_path": "/tmp/c001.patch"}

    proposal_arg = runtime.start_batch.call_args.args[2][0]
    assert isinstance(proposal_arg, CandidateProposal)
    runtime.list_history.assert_called_once_with("run_1", top_n=3, sort_by="created")
    runtime.create_run.assert_called_once_with("spec_123", source_run_id=None)
    runtime.invalidate_run.assert_called_once_with(
        "run_1",
        reason="verifier_coverage_inadequate",
        summary="missing required case",
        evidence=[{"case": "edge"}],
    )
    runtime.plan_next.assert_called_once_with("run_1", requested_k=1)
    runtime.start_agent_session.assert_called_once_with(
        run_id="run_1",
        candidate_id="c001",
        directive={"goal": "try one"},
        worker_budget=None,
    )
    runtime.redispatch_candidate.assert_called_once_with(
        run_id="run_1",
        candidate_id="c001",
        worker_agent_type="search_candidate_agent_deep",
        worker_budget={"max_turns": 16},
    )
    runtime.bind_agent_handle.assert_called_once_with(
        agent_session_id="agent_001",
        handle={"host": "codex", "task_name": "search_agent_001"},
    )
    runtime.continue_agent_session.assert_called_once_with(
        agent_session_id="agent_001",
        worker_budget=None,
    )
    runtime.get_agent_observability.assert_called_once_with("agent_001")
    runtime.get_global_evidence.assert_called_once_with("agent_001")
    runtime.stage_shared_tool.assert_called_once_with(
        agent_session_id="agent_001",
        name="trace-helper",
        summary="Collect a bounded trace.",
        entrypoint="trace.py:main",
        candidate_relative_source_paths=[".tmp/tool-drafts/trace.py"],
    )


def _successful_score_report() -> ScoreReport:
    return ScoreReport(
        run_id="run_1",
        candidate_id="c001",
        validity_passed=True,
        process_passed=True,
        aggregate_score=1.0,
        verifier_results=[],
    )


def test_worker_verifier_injects_global_evidence_when_enabled() -> None:
    runtime = Mock()
    runtime.run_verifier.return_value = _successful_score_report()
    snapshot = [{"candidate_id": "c002", "iteration": 3, "score": 2.0}]
    runtime.should_inject_global_evidence_after_verifier.return_value = True
    runtime.get_global_evidence.return_value = snapshot

    result = SearchTools(runtime).search_run_verifier(
        "run_1",
        "c001",
        agent_session_id="agent_001",
        hypothesis="Try a different layout",
    )

    assert result["global_evidence_snapshot"] == snapshot
    assert result["global_evidence_injected"] is True
    assert result["global_evidence_entry_count"] == 1


def test_worker_verifier_keeps_success_when_evidence_injection_fails() -> None:
    runtime = Mock()
    runtime.run_verifier.return_value = _successful_score_report()
    runtime.should_inject_global_evidence_after_verifier.return_value = True
    runtime.get_global_evidence.side_effect = RuntimeError("view unavailable")

    result = SearchTools(runtime).search_run_verifier(
        "run_1",
        "c001",
        agent_session_id="agent_001",
        hypothesis="Try a different layout",
    )

    assert result["aggregate_score"] == 1.0
    assert result["global_evidence_injected"] is False
    assert "view unavailable" in result["global_evidence_warning"]
    assert "global_evidence_snapshot" not in result


def test_parent_and_promotion_verifiers_do_not_inject_global_evidence() -> None:
    runtime = Mock()
    runtime.run_verifier.return_value = _successful_score_report()
    tools = SearchTools(runtime)

    assert "global_evidence_injected" not in tools.search_run_verifier(
        "run_1", "c001"
    )
    assert "global_evidence_injected" not in tools.search_run_verifier(
        "run_1", "c001", scope="promotion"
    )
    runtime.should_inject_global_evidence_after_verifier.assert_not_called()


def test_search_create_uses_models_frozen_in_the_spec() -> None:
    runtime = Mock()
    runtime.create_run.return_value = "run_1"
    tools = SearchTools(runtime)

    assert tools.search_create("spec_123") == {"run_id": "run_1"}
    runtime.create_run.assert_called_once_with(
        "spec_123",
        source_run_id=None,
    )


def test_goal_plus_list_models_delegates_to_host_adapter() -> None:
    runtime = Mock()
    runtime.list_available_models.return_value = {
        "host": "pi-rpc",
        "adapter_version": "pi-rpc-v1",
        "models": [{"model": "openai-codex/gpt-5.6-sol"}],
    }
    tools = SearchTools(runtime)

    assert tools.goal_plus_list_models("pi-rpc", "sol") == (
        runtime.list_available_models.return_value
    )
    runtime.list_available_models.assert_called_once_with("pi-rpc", "sol")


def test_search_tools_expose_no_lifecycle_methods() -> None:
    runtime = Mock()
    tools = SearchTools(runtime)
    for deleted in (
        "search_update_agent_status",
        "search_list_agent_status",
        "search_finish_agent_session",
        "search_abort_agent_session",
        "search_abort_all_agent_sessions",
        "search_publish_observation",
        "search_list_observations",
        "search_wait_agent_events",
        "search_submit_candidate",
        "search_submit_iteration_plan",
        "search_get_global_plan",
    ):
        assert not hasattr(tools, deleted), f"SearchTools should not expose {deleted}"


def test_goal_plus_tools_delegate_runtime_calls_with_models() -> None:
    runtime = Mock()
    record = goal_plus_record()
    runtime.create_goal.return_value = record
    runtime.status.return_value = record
    runtime.update_goal.return_value = record.model_copy(update={"goal_revision": 2})
    runtime.list_events.return_value = [{"event_type": "created"}]
    runtime.record_triage.return_value = record
    runtime.save_spec_draft.return_value = record
    runtime.link_search_run.return_value = record
    runtime.record_search_result.return_value = record
    runtime.prepare_final_check.return_value = {
        "goal_plus_id": "gp_0001",
        "check": {"check_id": "fc_0001_r1_001"},
        "launch": {"tool": "spawn_agent"},
    }
    runtime.submit_final_check.return_value = record.model_copy(update={"status": "complete"})
    runtime.set_status.return_value = record.model_copy(update={"status": "complete"})
    runtime.gate.return_value = GoalPlusGateResult(
        decision="block",
        phase="intake",
        status="active",
        reason="needs triage",
        continuation_prompt="continue triage",
    )
    tools = GoalPlusTools(runtime)

    created = tools.goal_plus_create(
        raw_goal="Optimize a benchmark if possible",
        source_path=".",
        policy={"max_discovery_turns": 1},
    )
    assert created["goal_plus_id"] == "gp_0001"
    status = tools.goal_plus_status("gp_0001")
    assert status["evidence_log"][0]["event_type"] == "created"
    assert status["search_tasks_total"] == 0
    assert status["current_search_run_id"] is None
    assert tools.goal_plus_update_goal(
        "gp_0001",
        "Revised objective",
        expected_revision=1,
        reason="user edit",
    )["goal_revision"] == 2
    assert tools.goal_plus_record_triage(
        "gp_0001",
        {
            "is_optimization": True,
            "confidence": "medium",
            "recommended_phase": "spec_discovery",
        },
    )["phase"] == "intake"
    assert tools.goal_plus_save_spec_draft(
        "gp_0001",
        {
            "baseline": {},
            "metric": {},
            "correctness_gate": {},
            "edit_surface": {},
            "search_spec": spec_dict(),
            "promotion_rule": "must pass",
            "confidence": "high",
        },
    )["goal_plus_id"] == "gp_0001"
    assert tools.goal_plus_link_search_run("gp_0001", "spec_1", "run_1")["goal_plus_id"] == "gp_0001"
    assert tools.goal_plus_record_search_result(
        "gp_0001",
        run_id="run_1",
        selected_candidate_id="c001",
        report_path="/tmp/report.md",
        promotion_artifact_path="/tmp/c001.patch",
        summary="c001 selected",
    )["goal_plus_id"] == "gp_0001"
    prepared = tools.goal_plus_prepare_final_check("gp_0001", "codex")
    assert prepared["launch"]["tool"] == "spawn_agent"
    assert tools.goal_plus_submit_final_check(
        "gp_0001",
        "fc_0001_r1_001",
        1,
        "pass",
        "all requirements proven",
        evidence=[{"kind": "pytest"}],
    )["status"] == "complete"
    assert tools.goal_plus_set_status(
        "gp_0001",
        status="complete",
        reason="done",
        evidence=[{"kind": "test"}],
    )["status"] == "complete"
    assert tools.goal_plus_gate(
        "gp_0001",
        event="stop",
        context={"turn_id": "turn_1"},
    )["continuation_prompt"] == "continue triage"

    runtime.create_goal.assert_called_once_with(
        raw_goal="Optimize a benchmark if possible",
        source_path=".",
        policy={"max_discovery_turns": 1},
    )
    runtime.update_goal.assert_called_once_with(
        "gp_0001",
        raw_goal="Revised objective",
        expected_revision=1,
        reason="user edit",
    )
    runtime.link_search_run.assert_called_once_with("gp_0001", "spec_1", "run_1")
