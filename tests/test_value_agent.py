from __future__ import annotations

from pathlib import Path

import pytest

from goal_plus.models import ModelInvocationRecord, SearchSpec, ValueAssessment
from goal_plus.monitor import goal_plus_monitor_snapshot
from goal_plus.reporting import _task_details
from goal_plus.runtime import FileSearchRuntime
from goal_plus.value_agent import (
    PermanentValueAgentError,
    ValueAgentResult,
    resolve_candidate_values,
)
import goal_plus.value_agent as value_agent_module

from tests._runtime_helpers import make_project, spec_for


def lazy_value_spec(
    project: Path,
    *,
    max_pending_per_candidate: int = 1,
    max_reward_staleness_seconds: float = 180,
) -> SearchSpec:
    project.joinpath("evaluator.py").write_text(
        "import json\n"
        "namespace = {}\n"
        "exec(open('initial_program.py', encoding='utf-8').read(), namespace)\n"
        "print(json.dumps({'combined_score': namespace['VALUE']}))\n",
        encoding="utf-8",
    )
    data = spec_for(project, max_parallel=1).model_dump(mode="json")
    data["budget"] = {"max_parallel": 1, "max_candidates": 2}
    data["workspace"] = {"backend": "git_worktree"}
    data["strategy"] = {
        "name": "random",
        "orchestration_mode": "adaptive_search",
        "adaptive_search": {
            "reward": {
                "name": "evidence_llm_value",
                "version": 1,
                "params": {
                    "timeout_seconds": 30,
                    "scheduling": {
                        "mode": "lazy_near_prune",
                        "max_pending_per_candidate": max_pending_per_candidate,
                        "max_reward_staleness_seconds": (
                            max_reward_staleness_seconds
                        ),
                        "max_concurrent_value_calls": 1,
                    },
                },
            },
            "value_backup": {
                "name": "discounted_mean_best",
                "version": 2,
                "params": {"discount": 0.9, "best_weight": 0.25},
            },
            "allocation": {
                "name": "value_guided_replace",
                "version": 2,
                "max_replacements_per_decision": 1,
                "params": {
                    "min_attempts": 2,
                    "window_size": 2,
                    "retire_threshold": 0.0,
                    "barrier_guard": 0.0,
                    "exploration_weight": 0.0,
                    "uncertainty_weight": 0.0,
                    "nonpositive_fraction": 1.0,
                    "source_exploration_weight": 0.0,
                },
            },
            "expansion": {
                "source_policy": "highest_value",
                "model_policy": "inherit_source",
                "max_depth": 3,
            },
        },
    }
    return SearchSpec.model_validate(data)


class _SequenceValueAgent:
    def __init__(self, values: list[int]) -> None:
        self.values = list(values)

    def assess(self, context: dict[str, object]) -> ValueAgentResult:
        value = self.values.pop(0)
        iteration = int(context["iteration"])
        return ValueAgentResult(
            assessment=ValueAssessment(
                value=value,
                explanation=f"Iteration {iteration} leaves important work open.",
                hindsight_feedback=(
                    f"From the parent, avoid attempt {iteration} and cover the missing branch."
                ),
                limitations=["Hidden benchmark tests were not observed."],
            ),
            usage={"input_tokens": 10, "output_tokens": 5},
            invocation=ModelInvocationRecord(
                invocation_id=f"invocation-{iteration}",
                task_kind="evidence_value",
                host="codex",
                started_at="2026-08-24T00:00:00Z",
                completed_at="2026-08-24T00:00:01Z",
                elapsed_seconds=1.0,
            ),
        )

    def terminate(self) -> None:
        return None


class _PermanentFailureValueAgent:
    def assess(self, context: dict[str, object]) -> ValueAgentResult:
        raise PermanentValueAgentError("configured Value provider is unavailable")

    def terminate(self) -> None:
        return None


def _start_lazy_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    max_pending_per_candidate: int = 1,
    max_reward_staleness_seconds: float = 180,
) -> tuple[FileSearchRuntime, str, object, object]:
    project = make_project(tmp_path)
    runtime = FileSearchRuntime(tmp_path / ".search")
    monkeypatch.setattr(runtime, "_kick_evidence_annotator", lambda _run_id: None)
    monkeypatch.setattr(runtime, "_kick_value_agent", lambda _run_id: None)
    frozen = runtime.freeze_spec(
        lazy_value_spec(
            project,
            max_pending_per_candidate=max_pending_per_candidate,
            max_reward_staleness_seconds=max_reward_staleness_seconds,
        ),
        [project / "evaluator.py"],
    )
    run_id = runtime.create_run(frozen.frozen_spec_id)
    task = runtime.start_batch(
        run_id,
        runtime.plan_next(run_id, requested_k=1).plan_id,
    )[0]
    session = runtime.start_agent_session(run_id, task.candidate_id)
    return runtime, run_id, task, session


def test_lazy_value_async_completion_never_allocates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, run_id, task, session = _start_lazy_candidate(
        tmp_path,
        monkeypatch,
    )
    task.workspace.joinpath("initial_program.py").write_text(
        "VALUE = 1\n",
        encoding="utf-8",
    )
    report = runtime.run_verifier(
        run_id,
        task.candidate_id,
        agent_session_id=session.agent_session_id,
        hypothesis="establish a binary-verifier candidate",
    )

    assert report.value_status == "pending"
    assert report.value_task_id is not None
    assert report.reward_evaluation is None
    assert report.allocation_decision is None
    value_task = runtime._load_evidence_value_task(run_id, task.candidate_id, 1)
    assert value_task is not None
    assert value_task.barrier_recommendation is not None
    assert value_task.barrier_recommendation.mode == "async"
    graph = runtime._load_search_graph_projection(run_id)
    assert graph is not None
    pending_node = next(node for node in graph.nodes if node.iteration == 1)
    assert pending_node.reward_id is None
    assert pending_node.settled_value is None

    resolve_candidate_values(
        runtime.root_dir,
        run_id,
        task.candidate_id,
        through_iteration=1,
        agent=_SequenceValueAgent([-20]),
        barrier=False,
    )

    record = runtime._load_candidate_record(run_id, task.candidate_id)
    settled = record.iterations[0]
    assert record.value_settlement_watermark == 1
    assert settled.reward_evaluation is not None
    assert settled.reward_evaluation.attempt_reward == pytest.approx(-0.2)
    assert settled.reward_evaluation.settled_value == pytest.approx(-0.2)
    assert settled.value_backup_event_id is not None
    assert runtime._load_allocation_decisions(run_id) == []

    snapshot = goal_plus_monitor_snapshot(runtime.root_dir, run_id=run_id)
    value_agent = snapshot["run"]["value_agent"]
    assert value_agent["tasks"] == 1
    assert value_agent["states"] == {"completed": 1}
    assert value_agent["consumed"] == 1
    assert value_agent["usage"]["input_tokens"] == 10
    candidate = snapshot["candidates"][task.candidate_id]
    assert candidate["value_settlement_watermark"] == 1
    assert candidate["value_tasks_pending"] == 0

    details = _task_details(
        runtime.root_dir,
        {"run_id": run_id},
        run_id,
    )
    assert len(details["value_tasks"]) == 1
    assert details["candidates"][0]["value_settlement_watermark"] == 1
    assert details["candidates"][0]["iterations"][0]["value_task"][
        "assessment"
    ]["value"] == -20


def test_value_context_stays_bound_to_exact_attempt_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, run_id, task, session = _start_lazy_candidate(tmp_path, monkeypatch)
    task.workspace.joinpath("initial_program.py").write_text(
        "VALUE = 1\n",
        encoding="utf-8",
    )
    runtime.run_verifier(
        run_id,
        task.candidate_id,
        agent_session_id=session.agent_session_id,
        hypothesis="capture an exact attempt for delayed assessment",
    )
    task.workspace.joinpath("initial_program.py").write_text(
        "VALUE = 999\n",
        encoding="utf-8",
    )

    context = runtime._evidence_value_context(run_id, task.candidate_id, 1)

    assert "+VALUE = 1" in context["actual_diff"]
    assert "VALUE = 999" not in context["actual_diff"]
    assert context["input_limitations"][-1] == (
        "Hidden fail-to-pass tests and final benchmark labels are absent."
    )


def test_terminal_value_error_advances_watermark_without_allocation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, run_id, task, session = _start_lazy_candidate(tmp_path, monkeypatch)
    task.workspace.joinpath("initial_program.py").write_text(
        "VALUE = 1\n",
        encoding="utf-8",
    )
    runtime.run_verifier(
        run_id,
        task.candidate_id,
        agent_session_id=session.agent_session_id,
        hypothesis="exercise Value Agent fail-open settlement",
    )

    resolve_candidate_values(
        runtime.root_dir,
        run_id,
        task.candidate_id,
        through_iteration=1,
        agent=_PermanentFailureValueAgent(),
        barrier=False,
    )

    record = runtime._load_candidate_record(run_id, task.candidate_id)
    iteration = record.iterations[0]
    value_task = runtime._load_evidence_value_task(run_id, task.candidate_id, 1)
    assert value_task is not None
    assert value_task.state == "terminal_error"
    assert value_task.consumed_at is not None
    assert record.value_settlement_watermark == 1
    assert iteration.reward_evaluation is not None
    assert iteration.reward_evaluation.status == "error"
    assert iteration.value_backup_event_id is None
    assert runtime._load_allocation_decisions(run_id) == []


def test_pending_limit_barrier_orders_value_backup_and_allocates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, run_id, task, session = _start_lazy_candidate(
        tmp_path,
        monkeypatch,
    )
    task.workspace.joinpath("initial_program.py").write_text(
        "VALUE = 1\n",
        encoding="utf-8",
    )
    first = runtime.run_verifier(
        run_id,
        task.candidate_id,
        agent_session_id=session.agent_session_id,
        hypothesis="first unresolved process-value attempt",
    )
    assert first.value_status == "pending"

    fake = _SequenceValueAgent([-20, -40])
    original_resolve = value_agent_module.resolve_candidate_values

    def resolve_with_fake(
        root_dir: Path,
        selected_run_id: str,
        candidate_id: str,
        *,
        through_iteration: int,
    ) -> int:
        return original_resolve(
            root_dir,
            selected_run_id,
            candidate_id,
            through_iteration=through_iteration,
            agent=fake,
        )

    monkeypatch.setattr(
        value_agent_module,
        "resolve_candidate_values",
        resolve_with_fake,
    )
    second = runtime.run_verifier(
        run_id,
        task.candidate_id,
        agent_session_id=session.agent_session_id,
        hypothesis="second unresolved process-value attempt",
    )

    second_task = runtime._load_evidence_value_task(
        run_id,
        task.candidate_id,
        2,
    )
    assert second_task is not None
    assert second_task.barrier_recommendation is not None
    assert second_task.barrier_recommendation.mode == "await_through_current"
    assert "pending_limit_exceeded" in second_task.barrier_recommendation.reasons
    record = runtime._load_candidate_record(run_id, task.candidate_id)
    assert record.value_settlement_watermark == 2
    assert [
        item.reward_evaluation.components["attempt_value"]
        for item in record.iterations
        if item.reward_evaluation is not None
    ] == pytest.approx([-0.2, -0.4])
    assert record.iterations[1].reward_evaluation is not None
    assert record.iterations[1].reward_evaluation.attempt_reward == pytest.approx(-0.2)
    assert second.value_status == "completed"
    assert second.allocation_decision is not None

    events = runtime._load_value_backup_events(run_id)
    assert [item.source_iteration for item in events] == [1, 2]
    assert [item.observed_return for item in events] == pytest.approx([-0.2, -0.4])
    expand = next(
        action
        for action in second.allocation_decision.actions
        if action.kind == "expand_candidate"
    )
    assert expand.source is not None
    assert second_task.assessment_ref in expand.source.hindsight_feedback_refs

    applied = runtime.apply_allocation_decision(
        run_id,
        second.allocation_decision.decision_id,
    )
    child_context = runtime.get_agent_context(
        applied["agent_sessions"][0]["agent_session_id"]
    )
    action_context = child_context["expansion_action_context"]
    tried = action_context["tried_actions"]
    assert tried
    assert tried[0]["value_feedback_ref"] == second_task.assessment_ref
    assert "missing branch" in tried[0]["value_feedback"]


def test_stale_reward_triggers_barrier_before_pending_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, run_id, task, session = _start_lazy_candidate(
        tmp_path,
        monkeypatch,
        max_pending_per_candidate=3,
        max_reward_staleness_seconds=1,
    )
    task.workspace.joinpath("initial_program.py").write_text(
        "VALUE = 1\n",
        encoding="utf-8",
    )
    runtime.run_verifier(
        run_id,
        task.candidate_id,
        agent_session_id=session.agent_session_id,
        hypothesis="leave one Value task pending past its staleness bound",
    )
    first_task = runtime._load_evidence_value_task(run_id, task.candidate_id, 1)
    assert first_task is not None
    runtime._write_evidence_value_task(
        first_task.model_copy(
            update={
                "created_at": "2000-01-01T00:00:00Z",
                "updated_at": "2000-01-01T00:00:00Z",
            }
        )
    )

    fake = _SequenceValueAgent([-10, -20])
    original_resolve = value_agent_module.resolve_candidate_values

    def resolve_with_fake(
        root_dir: Path,
        selected_run_id: str,
        candidate_id: str,
        *,
        through_iteration: int,
    ) -> int:
        return original_resolve(
            root_dir,
            selected_run_id,
            candidate_id,
            through_iteration=through_iteration,
            agent=fake,
        )

    monkeypatch.setattr(
        value_agent_module,
        "resolve_candidate_values",
        resolve_with_fake,
    )
    runtime.run_verifier(
        run_id,
        task.candidate_id,
        agent_session_id=session.agent_session_id,
        hypothesis="reach the stale Value barrier",
    )

    second_task = runtime._load_evidence_value_task(run_id, task.candidate_id, 2)
    assert second_task is not None
    recommendation = second_task.barrier_recommendation
    assert recommendation is not None
    assert recommendation.mode == "await_through_current"
    assert "reward_stale" in recommendation.reasons
    assert "pending_limit_exceeded" not in recommendation.reasons


def test_missing_value_task_is_recreated_from_durable_iteration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, run_id, task, session = _start_lazy_candidate(tmp_path, monkeypatch)
    task.workspace.joinpath("initial_program.py").write_text(
        "VALUE = 1\n",
        encoding="utf-8",
    )
    create_task = runtime._create_evidence_value_task_locked
    failures = 0

    def fail_once(*args: object, **kwargs: object):
        nonlocal failures
        failures += 1
        if failures == 1:
            raise OSError("simulated task registration interruption")
        return create_task(*args, **kwargs)

    monkeypatch.setattr(runtime, "_create_evidence_value_task_locked", fail_once)
    first_report = runtime.run_verifier(
        run_id,
        task.candidate_id,
        agent_session_id=session.agent_session_id,
        hypothesis="persist hard settlement before task registration",
    )
    assert first_report.value_task_id is None
    assert runtime._load_evidence_value_task(run_id, task.candidate_id, 1) is None

    fake = _SequenceValueAgent([-10, -20])
    original_resolve = value_agent_module.resolve_candidate_values

    def resolve_with_fake(
        root_dir: Path,
        selected_run_id: str,
        candidate_id: str,
        *,
        through_iteration: int,
    ) -> int:
        return original_resolve(
            root_dir,
            selected_run_id,
            candidate_id,
            through_iteration=through_iteration,
            agent=fake,
        )

    monkeypatch.setattr(
        value_agent_module,
        "resolve_candidate_values",
        resolve_with_fake,
    )
    runtime.run_verifier(
        run_id,
        task.candidate_id,
        agent_session_id=session.agent_session_id,
        hypothesis="reconcile the missing task from hard settlement",
    )

    record = runtime._load_candidate_record(run_id, task.candidate_id)
    assert record.value_settlement_watermark == 2
    assert all(item.value_task_id is not None for item in record.iterations)
    assert all(
        not item.value_backup_error
        or not item.value_backup_error.startswith("value task registration failed:")
        for item in record.iterations
    )


def test_lazy_value_configuration_is_coupled_to_v2_components(
    tmp_path: Path,
) -> None:
    project = make_project(tmp_path)
    data = lazy_value_spec(project).model_dump(mode="json")
    data["strategy"]["adaptive_search"]["value_backup"]["version"] = 1
    with pytest.raises(ValueError, match="requires discounted_mean_best/v2"):
        SearchSpec.model_validate(data)

    data = lazy_value_spec(project).model_dump(mode="json")
    data["strategy"]["adaptive_search"]["reward"]["params"]["scheduling"][
        "max_concurrent_value_calls"
    ] = 2
    with pytest.raises(ValueError, match="must not exceed budget.max_parallel"):
        SearchSpec.model_validate(data)
