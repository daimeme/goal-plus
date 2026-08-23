from __future__ import annotations

import math
from pathlib import Path

import pytest

from goal_plus.adaptive_search import (
    RewardContext,
    evaluate_reward,
    project_allocation_resources,
    project_search_graph,
    replay_value_backups,
    value_backup_config_hash,
)
from goal_plus.evidence_annotator import (
    EvidenceAnnotationResult,
    drain_evidence_annotations,
)
from goal_plus.models import (
    AdaptiveSearchSpec,
    AllocationDecision,
    CandidateRecord,
    ExpansionSource,
    RewardEvaluation,
    SearchSpec,
    ValueBackupEvent,
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


def adaptive_spec(
    project: Path,
    *,
    max_candidates: int = 4,
    max_parallel: int = 2,
) -> SearchSpec:
    project.joinpath("evaluator.py").write_text(
        "import json\n"
        "namespace = {}\n"
        "exec(open('initial_program.py', encoding='utf-8').read(), namespace)\n"
        "print(json.dumps({'combined_score': namespace['VALUE']}))\n",
        encoding="utf-8",
    )
    data = spec_for(project, max_parallel=max_parallel).model_dump(mode="json")
    data["budget"] = {
        "max_parallel": max_parallel,
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


def adaptive_v2_spec(
    project: Path,
    *,
    max_candidates: int = 4,
    max_parallel: int = 2,
) -> SearchSpec:
    data = adaptive_spec(
        project,
        max_candidates=max_candidates,
        max_parallel=max_parallel,
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


def test_candidate_record_reads_but_does_not_rewrite_legacy_node_value_cache() -> None:
    record = CandidateRecord.model_validate(
        {
            "candidate_id": "c001",
            "status": "created",
            "task": {
                "run_id": "run_test",
                "candidate_id": "c001",
                "hypothesis": "",
                "workspace": ".",
                "allowed_files": [],
                "denied_files": [],
            },
            "node_values": [{"obsolete_cache_payload": True}],
        }
    )

    assert "node_values" not in record.model_dump(mode="json")


def test_legacy_adaptive_state_projects_into_current_value_and_allocation_layers() -> None:
    run_id = "run_legacy"
    created_at = "2026-08-18T00:00:00Z"
    root_reward_id = "reward_c001_0001_legacy"
    child_reward_id = "reward_c002_0001_legacy"
    root = CandidateRecord.model_validate(
        {
            "candidate_id": "c001",
            "status": "evaluated",
            "task": {
                "run_id": run_id,
                "candidate_id": "c001",
                "hypothesis": "root",
                "workspace": ".",
                "workspace_base_revision": "0" * 40,
                "allowed_files": [],
                "denied_files": [],
            },
            "iterations": [
                {
                    "iteration": 1,
                    "score": 10.0,
                    "process_passed": True,
                    "git_head": "1" * 40,
                    "attempt_base_git_head": "0" * 40,
                    "ledger_git_head": "2" * 40,
                    "artifact_hash": "a" * 64,
                    "disposition": "keep",
                    "reward_evaluation": {
                        "reward_id": root_reward_id,
                        "evaluator_name": "metric_progress",
                        "evaluator_version": 1,
                        "config_hash": "a" * 64,
                        "status": "evaluated",
                        "reward": 1.0,
                        "state_value": 10.0,
                        "created_at": created_at,
                    },
                    "workspace_git_head_after_settlement": "2" * 40,
                    "created_at": created_at,
                }
            ],
            "node_values": [{"obsolete_cache_payload": True}],
            "results_ledger_git_head": "2" * 40,
        }
    )
    child = CandidateRecord.model_validate(
        {
            "candidate_id": "c002",
            "status": "evaluated",
            "task": {
                "run_id": run_id,
                "candidate_id": "c002",
                "parent_id": "c001",
                "hypothesis": "legacy derived candidate",
                "workspace": ".",
                "workspace_base_revision": "3" * 40,
                "allocation_depth": 1,
                "expansion_source": {
                    "candidate_id": "c001",
                    "iteration": 1,
                    "evidence_commit": "1" * 40,
                    "settled_commit": "3" * 40,
                    "artifact_hash": "a" * 64,
                    "score": 10.0,
                    "reward_id": root_reward_id,
                    "state_value": 10.0,
                },
                "allowed_files": [],
                "denied_files": [],
            },
            "iterations": [
                {
                    "iteration": 1,
                    "score": 9.0,
                    "process_passed": True,
                    "git_head": "4" * 40,
                    "attempt_base_git_head": "3" * 40,
                    "ledger_git_head": "5" * 40,
                    "artifact_hash": "b" * 64,
                    "disposition": "discard",
                    "reward_evaluation": {
                        "reward_id": child_reward_id,
                        "evaluator_name": "metric_progress",
                        "evaluator_version": 1,
                        "config_hash": "a" * 64,
                        "status": "evaluated",
                        "reward": -1.0,
                        "state_value": 10.0,
                        "created_at": "2026-08-18T00:01:00Z",
                    },
                    "workspace_git_head_after_settlement": "5" * 40,
                    "created_at": "2026-08-18T00:01:00Z",
                }
            ],
            "results_ledger_git_head": "5" * 40,
        }
    )
    pending_child = child.model_copy(
        update={
            "candidate_id": "c004",
            "status": "created",
            "task": child.task.model_copy(update={"candidate_id": "c004"}),
            "iterations": [],
            "results_ledger_git_head": None,
        }
    )
    records = [root, child, pending_child]
    graph = project_search_graph(run_id, records, created_at=created_at)
    source_node = next(
        node
        for node in graph.nodes
        if node.candidate_id == "c001" and node.iteration == 1
    )
    assert child.task.expansion_source is not None
    assert child.task.expansion_source.node_id != source_node.node_id
    child_transition = next(
        item for item in graph.transitions if item.candidate_id == "c002"
    )
    assert child_transition.from_node_id == source_node.node_id
    assert graph.candidate_current_node_ids["c002"] == source_node.node_id

    invalid_source = child.task.expansion_source.model_copy(
        update={"node_id": "unknown_node_alias"}
    )
    invalid_child = child.model_copy(
        update={
            "task": child.task.model_copy(
                update={"expansion_source": invalid_source}
            )
        }
    )
    with pytest.raises(ValueError, match="expansion source node is unavailable"):
        project_search_graph(run_id, [root, invalid_child], created_at=created_at)

    adaptive_spec = AdaptiveSearchSpec.model_validate(
        {
            "value_backup": {
                "name": "discounted_mean_best",
                "version": 1,
                "params": {},
            }
        }
    )
    backup = ValueBackupEvent.model_validate(
        {
            "event_id": "backup_c002_0001_legacy",
            "run_id": run_id,
            "source_candidate_id": "c002",
            "source_iteration": 1,
            "source_reward_id": child_reward_id,
            "operator_name": "discounted_mean_best",
            "operator_version": 1,
            "config_hash": value_backup_config_hash(adaptive_spec),
            "attempt_reward": -1.0,
            "settled_value": 10.0,
            "targets": [
                {
                    "candidate_id": "c001",
                    "node_id": "node_c001_0001_abcdefabcdef",
                    "distance": 1,
                    "return_value": 9.5,
                }
            ],
            "created_at": "2026-08-18T00:01:00Z",
        }
    )
    values = replay_value_backups(adaptive_spec, graph, records, [backup])
    source_value = next(
        item for item in values.node_values if item.node_id == source_node.node_id
    )
    assert source_value.backup_count == 1
    assert source_value.completed_expansion_count == 1
    assert source_value.unobserved_expansion_count == 1
    assert values.applied_backup_event_ids == [backup.event_id]

    legacy_source = child.task.expansion_source.model_dump(mode="json")
    legacy_source.pop("node_id")
    legacy_source["state_value"] = legacy_source.pop("settled_value")
    legacy_source.pop("backed_up_value")
    decision = AllocationDecision.model_validate(
        {
            "decision_id": "allocation_0001",
            "run_id": run_id,
            "trigger_candidate_id": "c002",
            "trigger_iteration": 1,
            "trigger_commit": "4" * 40,
            "trigger_reward_id": child_reward_id,
            "policy_name": "low_reward_replace",
            "policy_version": 1,
            "config_hash": "c" * 64,
            "actions": [
                {
                    "action_id": "allocation_0001:retire",
                    "kind": "retire_candidate",
                    "candidate_id": "c002",
                    "reason": "legacy low reward",
                },
                {
                    "action_id": "allocation_0001:expand",
                    "kind": "expand_candidate",
                    "source": legacy_source,
                    "new_candidate_id": "c003",
                    "reason": "legacy highest value source",
                },
            ],
            "created_at": "2026-08-18T00:01:00Z",
        }
    )
    resources = project_allocation_resources(
        adaptive_spec,
        graph,
        records,
        [decision],
        max_candidates=5,
    )
    assert resources.pending_expansions == 1
    assert resources.unobserved_expansions_by_node == {source_node.node_id: 2}


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
    initial_contexts = [
        runtime.get_agent_context(session.agent_session_id)
        for session in sessions.values()
    ]
    assert {context["orchestration_mode"] for context in initial_contexts} == {
        "adaptive_search"
    }
    assert all("expansion_action_context" not in context for context in initial_contexts)

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
    high_after_second = runtime._load_candidate_record(run_id, high.candidate_id)
    exact_high_source_commit = high_after_second.iterations[1].ledger_git_head
    assert exact_high_source_commit is not None
    high.workspace.joinpath("initial_program.py").write_text(
        "VALUE = 9\n", encoding="utf-8"
    )
    third_high = runtime.run_verifier(
        run_id,
        high.candidate_id,
        agent_session_id=sessions[high.candidate_id].agent_session_id,
        hypothesis="reject a regression after the historical high-value node",
    )
    assert third_high.disposition == "discard"
    high_after_third = runtime._load_candidate_record(run_id, high.candidate_id)
    assert high_after_third.results_ledger_git_head != exact_high_source_commit

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
    assert expand.source.settled_commit == exact_high_source_commit

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
    ) == 5
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
    applied_values = runtime._load_value_projection(run_id)
    assert applied_values is not None
    applied_source_value = next(
        item
        for item in applied_values.node_values
        if item.node_id == expand.source.node_id
    )
    assert applied_source_value.unobserved_expansion_count == 1
    child_workspace = Path(child_task["workspace"])
    assert child_workspace.joinpath("initial_program.py").read_text(
        encoding="utf-8"
    ) == "VALUE = 11\n"

    child_context = runtime.get_agent_context(child_session["agent_session_id"])
    action_context = child_context["expansion_action_context"]
    assert action_context["source_node_id"] == expand.source.node_id
    assert [
        action["iteration"] for action in action_context["source_path_actions"]
    ] == [1, 2]
    assert [
        action["description_source"]
        for action in action_context["source_path_actions"]
    ] == ["evidence_view", "evidence_view"]
    assert [
        (action["candidate_id"], action["iteration"])
        for action in action_context["tried_actions"]
    ] == [(high.candidate_id, 3)]
    assert action_context["tried_actions"][0]["description"] == (
        f"Settled {high.candidate_id} iteration 3."
    )

    child_workspace.joinpath("initial_program.py").write_text(
        "VALUE = 9\n", encoding="utf-8"
    )
    child_report = runtime.run_verifier(
        run_id,
        child_task["candidate_id"],
        agent_session_id=child_session["agent_session_id"],
        hypothesis="repeat the settled regression from the same source",
    )
    assert child_report.disposition == "discard"
    assert child_report.reward_evaluation is not None
    assert child_report.reward_evaluation.attempt_reward == 0.0
    assert child_report.reward_evaluation.settled_value == 11.0
    assert child_workspace.joinpath("initial_program.py").read_text(
        encoding="utf-8"
    ) == "VALUE = 11\n"

    pending_action_context = runtime.get_agent_context(
        child_session["agent_session_id"]
    )["expansion_action_context"]
    first_action, duplicate_action = pending_action_context["tried_actions"]
    assert first_action["exact_effect_key"] == duplicate_action["exact_effect_key"]
    assert duplicate_action["duplicate_of_transition_id"] == first_action[
        "transition_id"
    ]
    assert duplicate_action["description_source"] == "hypothesis"
    assert duplicate_action["description"] == (
        "repeat the settled regression from the same source"
    )
    assert duplicate_action["attempt_changed_files"] == ["initial_program.py"]
    assert pending_action_context["unique_tried_action_count"] == 1
    assert pending_action_context["exact_duplicate_count"] == 1

    assert drain_evidence_annotations(
        runtime.root_dir,
        run_id,
        annotator=_StaticAnnotator(),
    ) == 1
    enriched_action_context = runtime.get_agent_context(
        child_session["agent_session_id"]
    )["expansion_action_context"]
    enriched_duplicate = enriched_action_context["tried_actions"][1]
    assert enriched_duplicate["transition_id"] == duplicate_action["transition_id"]
    assert enriched_duplicate["exact_effect_key"] == duplicate_action[
        "exact_effect_key"
    ]
    assert enriched_duplicate["duplicate_of_transition_id"] == duplicate_action[
        "duplicate_of_transition_id"
    ]
    assert enriched_duplicate["description_source"] == "evidence_view"
    assert enriched_duplicate["description"] == (
        f"Settled {child_task['candidate_id']} iteration 1."
    )

    child_values = runtime._load_value_projection(run_id)
    assert child_values is not None
    child_source_value = next(
        item
        for item in child_values.node_values
        if item.node_id == expand.source.node_id
    )
    assert child_source_value.unique_branch_count == 1

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
    assert snapshot["candidates"][high.candidate_id]["current_value_estimate"][
        "backed_up_value"
    ] == 11.0
    assert snapshot["run"]["adaptive_search"]["search_graph"]["available"]
    assert snapshot["run"]["adaptive_search"]["value_projection"]["available"]

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
    backup_event = next(
        item
        for item in runtime._load_value_backup_events(run_id)
        if item.event_id == child_iteration.value_backup_event_id
    )
    assert backup_event.event_id == child_iteration.value_backup_event_id
    assert backup_event.targets[0].node_id == expand.source.node_id

    graph = runtime._load_search_graph_projection(run_id)
    values = runtime._load_value_projection(run_id)
    assert graph is not None
    assert values is not None
    source_node = next(
        item for item in values.node_values
        if item.node_id == expand.source.node_id
    )
    assert source_node.backup_count == 1
    assert source_node.backup_mean_return is not None
    assert source_node.backed_up_value < source_node.settled_value
    child_transition = next(
        item
        for item in graph.transitions
        if item.candidate_id == child_task["candidate_id"]
    )
    assert child_transition.kind == "derived_candidate_start"
    assert child_transition.from_node_id == expand.source.node_id
    assert child_transition.accepted_node_id is None
    assert child_transition.settled_node_id == expand.source.node_id
    source_graph_node = next(
        item for item in graph.nodes if item.node_id == expand.source.node_id
    )
    assert source_graph_node.parent_node_id is not None
    assert [item.node_id for item in backup_event.targets[:2]] == [
        expand.source.node_id,
        source_graph_node.parent_node_id,
    ]


def test_independent_evidence_omits_derived_action_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = make_project(tmp_path)
    runtime = FileSearchRuntime(tmp_path / ".search")
    monkeypatch.setattr(runtime, "_kick_evidence_annotator", lambda _run_id: None)
    data = adaptive_spec(project, max_candidates=3).model_dump(mode="json")
    data["strategy"]["config"] = {"global_evidence_mode": "independent"}
    frozen = runtime.freeze_spec(
        SearchSpec.model_validate(data),
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
        hypothesis="establish the independent expansion source",
    )
    low.workspace.joinpath("initial_program.py").write_text(
        "VALUE = 1\n", encoding="utf-8"
    )
    runtime.run_verifier(
        run_id,
        low.candidate_id,
        agent_session_id=low_session.agent_session_id,
        hypothesis="establish the independent low lane",
    )
    report = runtime.run_verifier(
        run_id,
        low.candidate_id,
        agent_session_id=low_session.agent_session_id,
        hypothesis="confirm the independent low lane has no progress",
    )
    assert report.allocation_decision is not None
    applied = runtime.apply_allocation_decision(
        run_id,
        report.allocation_decision.decision_id,
    )
    child_session_id = applied["agent_sessions"][0]["agent_session_id"]

    def unexpected_projection(*args: object, **kwargs: object) -> object:
        raise AssertionError("independent mode must not project peer actions")

    monkeypatch.setattr(
        runtime._adaptive_search,
        "project_action_context",
        unexpected_projection,
    )
    assert "expansion_action_context" not in runtime.get_agent_context(
        child_session_id
    )


def test_exploration_quota_reserves_pending_source_until_child_is_observed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GOAL_PLUS_SUPPLEMENTAL_EVALUATION_ENABLED", "0")
    project = make_project(tmp_path)
    data = adaptive_v2_spec(
        project,
        max_candidates=6,
        max_parallel=3,
    ).model_dump(mode="json")
    data["strategy"]["adaptive_search"]["expansion"][
        "max_unobserved_expansions_per_node"
    ] = 1
    runtime = FileSearchRuntime(tmp_path / ".search")
    monkeypatch.setattr(runtime, "_kick_evidence_annotator", lambda _run_id: None)
    frozen = runtime.freeze_spec(
        SearchSpec.model_validate(data),
        [project / "evaluator.py"],
    )
    run_id = runtime.create_run(frozen.frozen_spec_id)
    high, low_one, low_two = runtime.start_batch(
        run_id,
        runtime.plan_next(run_id, requested_k=3).plan_id,
    )
    sessions = {
        task.candidate_id: runtime.start_agent_session(
            run_id,
            task.candidate_id,
        )
        for task in (high, low_one, low_two)
    }

    high.workspace.joinpath("initial_program.py").write_text(
        "VALUE = 10\n",
        encoding="utf-8",
    )
    runtime.run_verifier(
        run_id,
        high.candidate_id,
        agent_session_id=sessions[high.candidate_id].agent_session_id,
        hypothesis="establish the preferred expansion source",
    )

    decisions = []
    for low in (low_one, low_two):
        low.workspace.joinpath("initial_program.py").write_text(
            "VALUE = 1\n",
            encoding="utf-8",
        )
        runtime.run_verifier(
            run_id,
            low.candidate_id,
            agent_session_id=sessions[low.candidate_id].agent_session_id,
            hypothesis="establish a low-value lane",
        )
        report = runtime.run_verifier(
            run_id,
            low.candidate_id,
            agent_session_id=sessions[low.candidate_id].agent_session_id,
            hypothesis="confirm a low-value lane has no marginal return",
        )
        assert report.allocation_decision is not None
        decisions.append(report.allocation_decision)

    first_expand = next(
        action
        for action in decisions[0].actions
        if action.kind == "expand_candidate"
    )
    second_expand = next(
        action
        for action in decisions[1].actions
        if action.kind == "expand_candidate"
    )
    assert first_expand.source is not None
    assert second_expand.source is not None
    preferred_node_id = first_expand.source.node_id
    assert first_expand.source.candidate_id == high.candidate_id
    assert second_expand.source.node_id != preferred_node_id

    second_snapshot = decisions[1].state_snapshot
    assert second_snapshot is not None
    assert second_snapshot.max_unobserved_expansions_per_node == 1
    assert second_snapshot.unobserved_expansions_by_node == {
        preferred_node_id: 1
    }
    assert second_snapshot.exploration_quota_remaining_by_node[
        preferred_node_id
    ] == 0
    preferred_option = next(
        item
        for item in second_snapshot.source_options
        if item.node_id == preferred_node_id
    )
    assert not preferred_option.eligible
    assert preferred_option.ineligible_reason == "exploration_quota_exhausted"

    applied = runtime.apply_allocation_decision(
        run_id,
        decisions[0].decision_id,
    )
    child_task = applied["candidate_tasks"][0]
    child_session = applied["agent_sessions"][0]
    child_workspace = Path(child_task["workspace"])
    adaptive = frozen.spec.strategy.adaptive_search
    assert adaptive is not None

    def current_resources():
        graph = runtime._load_search_graph_projection(run_id)
        assert graph is not None
        return runtime._adaptive_search.project_resources(
            adaptive,
            graph,
            runtime._load_candidate_records(run_id),
            runtime._load_allocation_decisions(run_id),
            max_candidates=6,
        )

    materialized_resources = current_resources()
    assert materialized_resources.unobserved_expansions_by_node == {
        preferred_node_id: 1,
        second_expand.source.node_id: 1,
    }
    assert materialized_resources.exploration_quota_remaining_by_node[
        preferred_node_id
    ] == 0

    child_workspace.joinpath("initial_program.py").write_text(
        "VALUE = 5\n",
        encoding="utf-8",
    )
    first_child_report = runtime.run_verifier(
        run_id,
        child_task["candidate_id"],
        agent_session_id=child_session["agent_session_id"],
        hypothesis="observe the derived candidate and release its source quota",
    )
    assert first_child_report.allocation_decision is None
    observed_resources = current_resources()
    assert observed_resources.unobserved_expansions_by_node.get(
        preferred_node_id,
        0,
    ) == 0
    assert observed_resources.exploration_quota_remaining_by_node[
        preferred_node_id
    ] == 1


def test_atomic_allocation_reserves_and_applies_multiple_replacements(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GOAL_PLUS_SUPPLEMENTAL_EVALUATION_ENABLED", "0")
    project = make_project(tmp_path)
    data = adaptive_spec(
        project,
        max_candidates=6,
        max_parallel=3,
    ).model_dump(mode="json")
    adaptive = data["strategy"]["adaptive_search"]
    adaptive["allocation"]["max_replacements_per_decision"] = 2
    adaptive["expansion"]["max_unobserved_expansions_per_node"] = 1
    runtime = FileSearchRuntime(tmp_path / ".search")
    monkeypatch.setattr(runtime, "_kick_evidence_annotator", lambda _run_id: None)
    frozen = runtime.freeze_spec(
        SearchSpec.model_validate(data),
        [project / "evaluator.py"],
    )
    run_id = runtime.create_run(frozen.frozen_spec_id)
    high, low_one, low_two = runtime.start_batch(
        run_id,
        runtime.plan_next(run_id, requested_k=3).plan_id,
    )
    sessions = {
        task.candidate_id: runtime.start_agent_session(run_id, task.candidate_id)
        for task in (high, low_one, low_two)
    }

    high.workspace.joinpath("initial_program.py").write_text(
        "VALUE = 10\n",
        encoding="utf-8",
    )
    runtime.run_verifier(
        run_id,
        high.candidate_id,
        agent_session_id=sessions[high.candidate_id].agent_session_id,
        hypothesis="establish the preferred atomic expansion source",
    )

    for low in (low_one, low_two):
        low.workspace.joinpath("initial_program.py").write_text(
            "VALUE = 1\n",
            encoding="utf-8",
        )
        runtime.run_verifier(
            run_id,
            low.candidate_id,
            hypothesis="stage a low lane without worker allocation",
        )

    runtime.run_verifier(
        run_id,
        low_one.candidate_id,
        hypothesis="make the first low lane batch-eligible",
    )
    report = runtime.run_verifier(
        run_id,
        low_two.candidate_id,
        agent_session_id=sessions[low_two.candidate_id].agent_session_id,
        hypothesis="trigger one atomic allocation for both low lanes",
    )

    decision = report.allocation_decision
    assert decision is not None
    assert [action.kind for action in decision.actions] == [
        "retire_candidate",
        "expand_candidate",
        "retire_candidate",
        "expand_candidate",
    ]
    retired_ids = [
        action.candidate_id
        for action in decision.actions
        if action.kind == "retire_candidate"
    ]
    expansions = [
        action
        for action in decision.actions
        if action.kind == "expand_candidate"
    ]
    assert retired_ids == [low_two.candidate_id, low_one.candidate_id]
    assert len({action.source.node_id for action in expansions}) == 2
    for candidate_id in retired_ids:
        record = runtime._load_candidate_record(run_id, candidate_id)
        assert record.allocation_eligibility == "retired"
        assert record.retired_by_decision_id == decision.decision_id

    monitor = goal_plus_monitor_snapshot(runtime.root_dir, run_id=run_id)
    allocation_summary = monitor["run"]["allocation_decisions"]
    assert allocation_summary["reserved_replacements"] == 2
    assert allocation_summary["max_atomic_replacements"] == 2

    second_expand = expansions[1]
    assert second_expand.source is not None
    broken_source = second_expand.source.model_copy(
        update={"candidate_id": "missing-source"}
    )
    broken_actions = [
        action.model_copy(update={"source": broken_source})
        if action.action_id == second_expand.action_id
        else action
        for action in decision.actions
    ]
    runtime._write_allocation_decision(
        decision.model_copy(update={"actions": broken_actions})
    )
    with pytest.raises(
        RuntimeError,
        match="allocation source candidate is unavailable",
    ):
        runtime.apply_allocation_decision(run_id, decision.decision_id)
    child_ids = {action.new_candidate_id for action in expansions}
    assert child_ids.isdisjoint(
        {record.candidate_id for record in runtime._load_candidate_records(run_id)}
    )

    runtime._write_allocation_decision(decision)
    applied = runtime.apply_allocation_decision(run_id, decision.decision_id)
    assert len(applied["candidate_tasks"]) == 2
    assert len(applied["agent_sessions"]) == 2
    assert {item["candidate_id"] for item in applied["candidate_tasks"]} == child_ids
    repeated = runtime.apply_allocation_decision(run_id, decision.decision_id)
    assert [item["agent_session_id"] for item in repeated["agent_sessions"]] == [
        item["agent_session_id"] for item in applied["agent_sessions"]
    ]


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

    monkeypatch.setattr(runtime._adaptive_search, "decide", fail_allocation)
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
