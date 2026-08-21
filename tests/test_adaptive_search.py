from __future__ import annotations

import math
from pathlib import Path

import pytest

from goal_plus.adaptive_search import RewardContext, evaluate_reward
from goal_plus.evidence_annotator import (
    EvidenceAnnotationResult,
    drain_evidence_annotations,
)
from goal_plus.models import (
    AdaptiveSearchSpec,
    ExpansionSource,
    RewardEvaluation,
    SearchSpec,
)
from goal_plus.monitor import goal_plus_monitor_snapshot
from goal_plus.reporting import build_html_report_data, render_html_report
from goal_plus.runtime import FileSearchRuntime

from tests._runtime_helpers import make_project, spec_for


class _StaticAnnotator:
    def annotate(self, context: dict[str, object]) -> EvidenceAnnotationResult:
        return EvidenceAnnotationResult(
            description=(
                f"Settled {context['candidate_id']} iteration {context['iteration']}."
            ),
            usage={},
        )


def adaptive_spec(project: Path, *, max_candidates: int = 4) -> SearchSpec:
    project.joinpath("evaluator.py").write_text(
        "import json\n"
        "namespace = {}\n"
        "exec(open('initial_program.py', encoding='utf-8').read(), namespace)\n"
        "print(json.dumps({'combined_score': namespace['VALUE']}))\n",
        encoding="utf-8",
    )
    data = spec_for(project, max_parallel=2).model_dump(mode="json")
    data["budget"] = {
        "max_parallel": 2,
        "max_candidates": max_candidates,
    }
    data["workspace"] = {"backend": "git_worktree"}
    data["strategy"] = {
        "name": "random",
        "orchestration_mode": "adaptive_search",
        "adaptive_search": {
            "reward": {
                "name": "metric_progress",
                "version": 1,
                "params": {},
            },
            "allocation": {
                "name": "low_reward_replace",
                "version": 1,
                "params": {
                    "min_iterations": 2,
                    "consecutive_below": 2,
                    "reward_threshold": 0.0,
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


def adaptive_v2_spec(project: Path, *, max_candidates: int = 4) -> SearchSpec:
    data = adaptive_spec(
        project,
        max_candidates=max_candidates,
    ).model_dump(mode="json")
    data["strategy"]["adaptive_search"] = {
        "reward": {
            "name": "metric_progress",
            "version": 2,
            "params": {
                "reward_clip": 4.0,
                "value_scale": 1.0,
                "failure_penalty": 1.0,
            },
        },
        "value_backup": {
            "name": "discounted_mean_best",
            "version": 1,
            "params": {
                "discount": 0.9,
                "attempt_reward_weight": 0.5,
                "best_weight": 0.25,
            },
        },
        "allocation": {
            "name": "value_guided_replace",
            "version": 1,
            "params": {
                "min_attempts": 2,
                "window_size": 2,
                "retire_threshold": 0.0,
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
    }
    return SearchSpec.model_validate(data)


def test_adaptive_search_requires_isolated_branchable_configuration(
    tmp_path: Path,
) -> None:
    project = make_project(tmp_path)
    data = spec_for(project).model_dump(mode="json")
    data["strategy"]["orchestration_mode"] = "adaptive_search"
    with pytest.raises(ValueError, match="strategy.adaptive_search is required"):
        SearchSpec.model_validate(data)

    data["strategy"]["adaptive_search"] = {}
    with pytest.raises(ValueError, match="workspace.backend=git_worktree"):
        SearchSpec.model_validate(data)


def test_metric_progress_reward_normalizes_minimize_direction() -> None:
    adaptive = AdaptiveSearchSpec()
    improved = evaluate_reward(
        adaptive,
        RewardContext(
            candidate_id="c001",
            iteration=2,
            commit="a" * 40,
            process_passed=True,
            disposition="keep",
            attempt_score=8.0,
            previous_best_score=10.0,
            metric_direction="minimize",
            created_at="2026-08-18T00:00:00Z",
        ),
    )
    discarded = evaluate_reward(
        adaptive,
        RewardContext(
            candidate_id="c001",
            iteration=3,
            commit="b" * 40,
            process_passed=True,
            disposition="discard",
            attempt_score=12.0,
            previous_best_score=8.0,
            metric_direction="minimize",
            created_at="2026-08-18T00:01:00Z",
        ),
    )

    assert improved.attempt_reward == 2.0
    assert improved.settled_value == -8.0
    assert discarded.attempt_reward == 0.0
    assert discarded.settled_value == -8.0


def test_metric_progress_v2_preserves_negative_attempt_signal_after_restore() -> None:
    adaptive = AdaptiveSearchSpec.model_validate(
        {
            "reward": {
                "name": "metric_progress",
                "version": 2,
                "params": {
                    "regression_weight": 2.0,
                    "cost_weight": 0.5,
                    "cost_scale_seconds": 10.0,
                },
            }
        }
    )
    reward = evaluate_reward(
        adaptive,
        RewardContext(
            candidate_id="c001",
            iteration=3,
            commit="c" * 40,
            process_passed=True,
            disposition="discard",
            attempt_score=12.0,
            previous_best_score=8.0,
            metric_direction="minimize",
            created_at="2026-08-18T00:02:00Z",
            verifier_elapsed_seconds=10.0,
        ),
    )

    expected_quality = -2.0 * math.log1p(4.0 / 12.0)
    expected_cost = 0.5 * math.log(2.0)
    assert reward.attempt_reward == pytest.approx(
        expected_quality - expected_cost
    )
    assert reward.settled_value == pytest.approx(-math.asinh(8.0))
    assert reward.components["raw_quality_delta"] == -4.0
    assert reward.components["settled_score"] == 8.0


def test_legacy_value_names_are_read_but_not_rewritten() -> None:
    reward = RewardEvaluation.model_validate(
        {
            "reward_id": "reward_c001_0001_legacy",
            "evaluator_name": "metric_progress",
            "evaluator_version": 1,
            "config_hash": "a" * 64,
            "status": "evaluated",
            "reward": 2.0,
            "state_value": 8.0,
            "created_at": "2026-08-18T00:00:00Z",
        }
    )
    assert reward.attempt_reward == 2.0
    assert reward.settled_value == 8.0
    assert "reward" not in reward.model_dump(mode="json")
    assert "state_value" not in reward.model_dump(mode="json")

    source = ExpansionSource.model_validate(
        {
            "candidate_id": "c001",
            "iteration": 1,
            "evidence_commit": "b" * 40,
            "settled_commit": "c" * 40,
            "artifact_hash": "d" * 64,
            "score": 8.0,
            "reward_id": reward.reward_id,
            "state_value": 8.0,
        }
    )
    assert source.settled_value == 8.0
    assert source.backed_up_value == 8.0
    assert source.node_id.startswith("node_c001_0001_")


def test_adaptive_reward_prunes_and_derives_exact_incumbent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = make_project(tmp_path)
    runtime = FileSearchRuntime(tmp_path / ".search")
    spec = adaptive_spec(project)
    frozen = runtime.freeze_spec(spec, [project / "evaluator.py"])
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=2)
    tasks = runtime.start_batch(run_id, plan.plan_id)
    sessions = {
        task.candidate_id: runtime.start_agent_session(run_id, task.candidate_id)
        for task in tasks
    }
    assert {
        runtime.get_agent_context(session.agent_session_id)["orchestration_mode"]
        for session in sessions.values()
    } == {"adaptive_search"}

    high = tasks[0]
    low = tasks[1]
    high.workspace.joinpath("initial_program.py").write_text(
        "VALUE = 10\n", encoding="utf-8"
    )
    first_high = runtime.run_verifier(
        run_id,
        high.candidate_id,
        agent_session_id=sessions[high.candidate_id].agent_session_id,
        hypothesis="establish a high-value incumbent",
    )
    assert first_high.reward_evaluation is not None
    assert first_high.reward_evaluation.attempt_reward == 0.0
    high.workspace.joinpath("initial_program.py").write_text(
        "VALUE = 11\n", encoding="utf-8"
    )
    second_high = runtime.run_verifier(
        run_id,
        high.candidate_id,
        agent_session_id=sessions[high.candidate_id].agent_session_id,
        hypothesis="improve the high-value incumbent",
    )
    assert second_high.reward_evaluation is not None
    assert second_high.reward_evaluation.attempt_reward == 1.0
    assert second_high.allocation_decision is None

    low.workspace.joinpath("initial_program.py").write_text(
        "VALUE = 1\n", encoding="utf-8"
    )
    first_low = runtime.run_verifier(
        run_id,
        low.candidate_id,
        agent_session_id=sessions[low.candidate_id].agent_session_id,
        hypothesis="establish a low-value incumbent",
    )
    assert first_low.allocation_decision is None
    create_annotation_task = runtime._create_evidence_annotation_task
    annotation_registration_blocked = True

    def flaky_annotation_registration(*args: object, **kwargs: object) -> object:
        if (
            annotation_registration_blocked
            and args[2] == low.candidate_id
            and args[3].iteration == 2
        ):
            raise OSError("simulated annotation task write failure")
        return create_annotation_task(*args, **kwargs)

    monkeypatch.setattr(
        runtime,
        "_create_evidence_annotation_task",
        flaky_annotation_registration,
    )
    second_low = runtime.run_verifier(
        run_id,
        low.candidate_id,
        agent_session_id=sessions[low.candidate_id].agent_session_id,
        hypothesis="retry without measurable progress",
    )
    decision = second_low.allocation_decision
    assert decision is not None
    assert [action.kind for action in decision.actions] == [
        "retire_candidate",
        "expand_candidate",
    ]
    expand = decision.actions[1]
    assert expand.source is not None
    assert expand.source.candidate_id == high.candidate_id
    assert expand.source.score == 11.0
    assert expand.source.settled_value == 11.0
    assert expand.source.backed_up_value == 11.0

    assert runtime._load_evidence_annotation_task(
        run_id,
        low.candidate_id,
        2,
    ) is None
    annotation_registration_blocked = False
    runtime._kick_evidence_annotator(run_id)
    annotation_task = runtime._load_evidence_annotation_task(
        run_id,
        low.candidate_id,
        2,
    )
    assert annotation_task is not None
    assert annotation_task.state == "pending"
    assert drain_evidence_annotations(
        runtime.root_dir,
        run_id,
        annotator=_StaticAnnotator(),
    ) == 4
    pruned_evidence = next(
        item
        for item in runtime.get_global_evidence(
            sessions[high.candidate_id].agent_session_id
        )
        if item["candidate_id"] == low.candidate_id and item["iteration"] == 2
    )
    assert pruned_evidence["view"] == (
        f"Settled {low.candidate_id} iteration 2."
    )

    with pytest.raises(RuntimeError, match="retired candidate"):
        runtime.continue_agent_session(sessions[low.candidate_id].agent_session_id)
    with pytest.raises(RuntimeError, match="retired by adaptive allocation"):
        runtime.run_verifier(
            run_id,
            low.candidate_id,
            agent_session_id=sessions[low.candidate_id].agent_session_id,
            hypothesis="must not continue after pruning",
        )

    applied = runtime.apply_allocation_decision(run_id, decision.decision_id)
    assert applied["decision"]["status"] == "applied"
    assert len(applied["candidate_tasks"]) == 1
    child_task = applied["candidate_tasks"][0]
    child_session = applied["agent_sessions"][0]
    assert child_task["candidate_id"] == expand.new_candidate_id
    assert child_task["parent_id"] == high.candidate_id
    assert child_task["allocation_depth"] == 1
    assert child_task["expansion_source"]["evidence_commit"] == (
        expand.source.evidence_commit
    )
    child_workspace = Path(child_task["workspace"])
    assert child_workspace.joinpath("initial_program.py").read_text(
        encoding="utf-8"
    ) == "VALUE = 11\n"

    child_workspace.joinpath("initial_program.py").write_text(
        "VALUE = 5\n", encoding="utf-8"
    )
    child_report = runtime.run_verifier(
        run_id,
        child_task["candidate_id"],
        agent_session_id=child_session["agent_session_id"],
        hypothesis="test a derived change that regresses",
    )
    assert child_report.disposition == "discard"
    assert child_report.reward_evaluation is not None
    assert child_report.reward_evaluation.attempt_reward == 0.0
    assert child_report.reward_evaluation.settled_value == 11.0
    assert child_workspace.joinpath("initial_program.py").read_text(
        encoding="utf-8"
    ) == "VALUE = 11\n"

    evidence = runtime.get_global_evidence(child_session["agent_session_id"])
    assert {item["candidate_id"] for item in evidence} == {
        high.candidate_id,
        low.candidate_id,
        child_task["candidate_id"],
    }

    repeated = runtime.apply_allocation_decision(run_id, decision.decision_id)
    assert repeated["agent_sessions"][0]["agent_session_id"] == (
        child_session["agent_session_id"]
    )
    assert runtime.status(run_id).candidates_total == 3

    snapshot = goal_plus_monitor_snapshot(runtime.root_dir, run_id=run_id)
    assert snapshot["run"]["allocation_decisions"]["applied"] == 1
    assert snapshot["candidates"][low.candidate_id][
        "allocation_eligibility"
    ] == "retired"
    assert snapshot["candidates"][child_task["candidate_id"]][
        "expansion_source"
    ]["candidate_id"] == high.candidate_id
    assert snapshot["candidates"][low.candidate_id][
        "last_reward_evaluation"
    ]["attempt_reward"] == 0.0
    assert snapshot["candidates"][high.candidate_id]["last_node_value"][
        "backed_up_value"
    ] == 11.0

    report_data = build_html_report_data(runtime.root_dir, run_id)
    [reported_task] = report_data["search_tasks"]
    assert reported_task["allocation_decisions"][0]["decision_id"] == (
        decision.decision_id
    )
    reported_child = next(
        item
        for item in reported_task["candidates"]
        if item["candidate_id"] == child_task["candidate_id"]
    )
    assert reported_child["allocation_depth"] == 1
    assert "Reward / allocation" in render_html_report(report_data)


def test_value_guided_allocation_backs_child_return_into_source_node(
    tmp_path: Path,
) -> None:
    project = make_project(tmp_path)
    runtime = FileSearchRuntime(tmp_path / ".search")
    frozen = runtime.freeze_spec(
        adaptive_v2_spec(project),
        [project / "evaluator.py"],
    )
    run_id = runtime.create_run(frozen.frozen_spec_id)
    high, low = runtime.start_batch(
        run_id,
        runtime.plan_next(run_id, requested_k=2).plan_id,
    )
    high_session = runtime.start_agent_session(run_id, high.candidate_id)
    low_session = runtime.start_agent_session(run_id, low.candidate_id)

    high.workspace.joinpath("initial_program.py").write_text(
        "VALUE = 10\n", encoding="utf-8"
    )
    runtime.run_verifier(
        run_id,
        high.candidate_id,
        agent_session_id=high_session.agent_session_id,
        hypothesis="establish a high V2 node",
    )
    high.workspace.joinpath("initial_program.py").write_text(
        "VALUE = 11\n", encoding="utf-8"
    )
    runtime.run_verifier(
        run_id,
        high.candidate_id,
        agent_session_id=high_session.agent_session_id,
        hypothesis="improve the high V2 node",
    )

    low.workspace.joinpath("initial_program.py").write_text(
        "VALUE = 1\n", encoding="utf-8"
    )
    runtime.run_verifier(
        run_id,
        low.candidate_id,
        agent_session_id=low_session.agent_session_id,
        hypothesis="establish a low V2 lane",
    )
    low_report = runtime.run_verifier(
        run_id,
        low.candidate_id,
        agent_session_id=low_session.agent_session_id,
        hypothesis="confirm the low lane has no marginal return",
    )

    decision = low_report.allocation_decision
    assert decision is not None
    assert decision.policy_name == "value_guided_replace"
    snapshot = decision.state_snapshot
    assert snapshot is not None
    assert snapshot.recent_attempt_rewards == [0.0, 0.0]
    assert snapshot.lane_upper_confidence_bound == 0.0
    assert snapshot.total_evaluated_attempts == 4
    expand = next(
        action for action in decision.actions
        if action.kind == "expand_candidate"
    )
    assert expand.source is not None
    assert expand.source.candidate_id == high.candidate_id
    assert expand.source.iteration == 2
    selected_snapshot = next(
        item for item in snapshot.source_options
        if item.node_id == expand.source.node_id
    )
    assert selected_snapshot.priority == expand.source.backed_up_value

    applied = runtime.apply_allocation_decision(run_id, decision.decision_id)
    child_task = applied["candidate_tasks"][0]
    child_session = applied["agent_sessions"][0]
    child_workspace = Path(child_task["workspace"])
    child_workspace.joinpath("initial_program.py").write_text(
        "VALUE = 5\n", encoding="utf-8"
    )
    child_report = runtime.run_verifier(
        run_id,
        child_task["candidate_id"],
        agent_session_id=child_session["agent_session_id"],
        hypothesis="measure a derived regression for value backup",
    )

    assert child_report.reward_evaluation is not None
    assert child_report.reward_evaluation.attempt_reward < 0
    child_record = runtime._load_candidate_record(
        run_id,
        child_task["candidate_id"],
    )
    child_iteration = child_record.iterations[-1]
    assert child_iteration.value_backup_event_id is not None
    assert child_iteration.value_backup_error is None
    [backup_event] = runtime._load_value_backup_events(run_id)
    assert backup_event.event_id == child_iteration.value_backup_event_id
    assert backup_event.targets[0].node_id == expand.source.node_id

    updated_high = runtime._load_candidate_record(run_id, high.candidate_id)
    source_node = next(
        item for item in updated_high.node_values
        if item.node_id == expand.source.node_id
    )
    assert source_node.backup_count == 1
    assert source_node.backup_mean_return is not None
    assert source_node.backed_up_value < source_node.settled_value


def test_allocation_error_does_not_block_annotation_task(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = make_project(tmp_path)
    runtime = FileSearchRuntime(tmp_path / ".search")
    frozen = runtime.freeze_spec(
        adaptive_spec(project),
        [project / "evaluator.py"],
    )
    run_id = runtime.create_run(frozen.frozen_spec_id)
    task = runtime.start_batch(
        run_id,
        runtime.plan_next(run_id, requested_k=1).plan_id,
    )[0]
    session = runtime.start_agent_session(run_id, task.candidate_id)

    def fail_allocation(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("simulated allocation failure")

    monkeypatch.setattr("goal_plus.runtime.decide_allocation", fail_allocation)
    task.workspace.joinpath("initial_program.py").write_text(
        "VALUE = 2\n", encoding="utf-8"
    )
    report = runtime.run_verifier(
        run_id,
        task.candidate_id,
        agent_session_id=session.agent_session_id,
        hypothesis="settle evidence despite an allocation policy error",
    )

    assert report.allocation_decision is None
    iteration = runtime._load_candidate_record(
        run_id,
        task.candidate_id,
    ).iterations[0]
    assert iteration.allocation_decision_error == (
        "RuntimeError: simulated allocation failure"
    )
    annotation_task = runtime._load_evidence_annotation_task(
        run_id,
        task.candidate_id,
        1,
    )
    assert annotation_task is not None
    assert annotation_task.state == "pending"


def test_unknown_adaptive_components_are_rejected_before_planning(
    tmp_path: Path,
) -> None:
    project = make_project(tmp_path)
    spec = adaptive_spec(project)
    data = spec.model_dump(mode="json")
    data["strategy"]["adaptive_search"]["reward"]["name"] = "unknown"
    invalid = SearchSpec.model_validate(data)
    runtime = FileSearchRuntime(tmp_path / ".search")
    with pytest.raises(ValueError, match="unsupported reward evaluator"):
        runtime.freeze_spec(invalid, [project / "evaluator.py"])

    data = spec.model_dump(mode="json")
    data["strategy"]["adaptive_search"]["value_backup"] = {
        "name": "unknown",
        "version": 1,
        "params": {},
    }
    invalid = SearchSpec.model_validate(data)
    with pytest.raises(ValueError, match="unsupported value backup operator"):
        runtime.freeze_spec(invalid, [project / "evaluator.py"])


def test_adaptive_policy_respects_total_candidate_limit(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    runtime = FileSearchRuntime(tmp_path / ".search")
    frozen = runtime.freeze_spec(
        adaptive_spec(project, max_candidates=2),
        [project / "evaluator.py"],
    )
    run_id = runtime.create_run(frozen.frozen_spec_id)
    plan = runtime.plan_next(run_id, requested_k=2)
    high, low = runtime.start_batch(run_id, plan.plan_id)
    high_session = runtime.start_agent_session(run_id, high.candidate_id)
    low_session = runtime.start_agent_session(run_id, low.candidate_id)
    high.workspace.joinpath("initial_program.py").write_text(
        "VALUE = 10\n", encoding="utf-8"
    )
    runtime.run_verifier(
        run_id,
        high.candidate_id,
        agent_session_id=high_session.agent_session_id,
        hypothesis="establish an expansion source",
    )
    low.workspace.joinpath("initial_program.py").write_text(
        "VALUE = 1\n", encoding="utf-8"
    )
    runtime.run_verifier(
        run_id,
        low.candidate_id,
        agent_session_id=low_session.agent_session_id,
        hypothesis="establish a low lane",
    )
    report = runtime.run_verifier(
        run_id,
        low.candidate_id,
        agent_session_id=low_session.agent_session_id,
        hypothesis="repeat without progress at the total candidate cap",
    )

    assert report.allocation_decision is None
    assert runtime.list_allocation_decisions(run_id) == []
    assert runtime._load_candidate_record(
        run_id, low.candidate_id
    ).allocation_eligibility == "eligible"
    assert runtime.status(run_id).candidates_total == 2
