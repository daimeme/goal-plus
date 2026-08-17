from __future__ import annotations

import json
from pathlib import Path

import pytest

from goal_plus.goal_plus import FileGoalPlusRuntime
from goal_plus.models import (
    EvidenceViewRecord,
    GoalPlusRecord,
    IterationRecord,
    ScoreReport,
    SearchSpec,
)
from goal_plus.reporting import (
    _build_loop_agent_statistics,
    _build_stop_hook_statistics,
    _build_timeline,
    _duration,
    _epoch,
    _render_loop_agent_statistics,
    _render_shared_evidence_view,
    _render_stop_hook_statistics,
    _render_timeline,
    _render_statistics,
    _search_trajectory_payload,
    build_html_report_data,
    render_html_report,
    render_report_document,
    write_html_report,
)
from goal_plus.runtime import (
    EXTERNAL_EVIDENCE_DIR_ENV,
    FileSearchRuntime,
    write_json,
)

from tests._runtime_helpers import make_project, spec_for


def _write_stop_hook_event(
    root: Path,
    invocation_id: str,
    **values: object,
) -> None:
    event_dir = root / "host-logs" / "codex-hook-events"
    event_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "invocation_id": invocation_id,
        "hook_event_name": "Stop",
        "started_at": "2026-01-01T00:00:00Z",
        "finished_at": "2026-01-01T00:00:00.010000Z",
        "duration_ms": 10.0,
        "outcome": "completed",
        "decision": "block",
        "reason": "active goal",
        "goal_plus_id": None,
        "session_id": "session-main",
        "host_agent_id": None,
        "agent_session_id": None,
        "run_id": None,
        "candidate_id": None,
        "stop_reason": "end_turn",
        "error_type": None,
        "error": None,
        **values,
    }
    (event_dir / f"{invocation_id}.json").write_text(
        json.dumps(payload),
        encoding="utf-8",
    )


def test_stop_hook_statistics_tolerate_corrupt_files_and_invalid_durations(
    tmp_path: Path,
) -> None:
    root = tmp_path / ".gp"
    goal_id = "gp_123"
    _write_stop_hook_event(
        root,
        "hook_valid",
        goal_plus_id=goal_id,
        duration_ms=2.5,
    )
    _write_stop_hook_event(
        root,
        "hook_negative",
        goal_plus_id=goal_id,
        duration_ms=-1.0,
    )
    _write_stop_hook_event(
        root,
        "hook_infinite",
        goal_plus_id=goal_id,
        duration_ms=float("inf"),
    )
    event_dir = root / "host-logs" / "codex-hook-events"
    (event_dir / "hook_corrupt.json").write_bytes(b"\xff")

    statistics = _build_stop_hook_statistics(
        root,
        goal_plus_id=goal_id,
        run_ids=set(),
    )

    assert statistics["source_available"] is True
    assert statistics["events_total"] == 3
    assert statistics["duration_ms_total"] == 2.5
    assert {
        event["invocation_id"]: event["duration_ms"] for event in statistics["events"]
    } == {
        "hook_infinite": None,
        "hook_negative": None,
        "hook_valid": 2.5,
    }


def test_duration_rounding_never_displays_sixty_seconds() -> None:
    assert _duration(599.828) == "10m 0s"
    assert _duration(3599.8) == "1h 0m"


def test_stop_hook_statistics_distinguish_missing_source_from_zero(
    tmp_path: Path,
) -> None:
    root = tmp_path / ".gp"

    statistics = _build_stop_hook_statistics(
        root,
        goal_plus_id="gp_missing",
        run_ids={"run_missing"},
    )
    html = _render_stop_hook_statistics(statistics)

    assert statistics["source_available"] is False
    assert statistics["events_total"] == 0
    assert html.count("Not observed") >= 15
    assert "Counts cannot be distinguished from zero" in html


def test_codex_run_report_uses_canonical_renderer_and_submission_trajectory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "goal_plus.reporting._load_plotly_javascript",
        lambda: "window.Plotly = {};",
    )
    observation = {
        "schema_version": "0.2",
        "generated_at": "2026-07-30T12:00:00+08:00",
        "source_kind": "codex-run-bundle",
        "run": {
            "run_id": "codex-terra-medium-case",
            "task_id": "optimization_task",
            "agent": "codex",
            "model": "gpt-5.6-terra",
            "reasoning_effort": "medium",
            "codex_versions": ["0.144.1"],
            "session_ids": ["session-1"],
            "runtime_seconds": 600.0,
            "timed_out": True,
            "termination_reason": "budget_exhausted",
            "budget_exhausted": True,
            "terminal_state": "budget_reached",
            "resume_count": 0,
        },
        "result": {
            "best_pass_rate": 1.0,
            "best_score": 2460.0,
            "best_round": "agent-2",
            "total_rounds": 3,
            "agent_submissions": 2,
            "auto_submissions": 1,
            "outcome_status": "success",
        },
        "evaluations": {
            "metric_name": "score",
            "metric_direction": "minimize",
            "best_score": 2460.0,
            "best_round": "agent-2",
            "entries": [
                {
                    "round": "agent-1",
                    "kind": "agent",
                    "score": 5103.0,
                    "pass_rate": 1.0,
                    "valid": True,
                    "passed": 9,
                    "total_tests": 9,
                    "summary": "cycles=5103; lower is better",
                    "at": None,
                },
                {
                    "round": "auto-1",
                    "kind": "auto",
                    "score": 5103.0,
                    "pass_rate": 1.0,
                    "valid": True,
                    "passed": 9,
                    "total_tests": 9,
                    "summary": "cycles=5103; lower is better",
                    "at": "2026-07-30T12:05:00",
                },
                {
                    "round": "agent-2",
                    "kind": "agent",
                    "score": 2460.0,
                    "pass_rate": 1.0,
                    "valid": True,
                    "passed": 9,
                    "total_tests": 9,
                    "summary": "cycles=2460; lower is better",
                    "at": None,
                },
            ],
        },
        "hooks": {
            "stop": {
                "started": 691,
                "blocked": 691,
                "completed_or_allowed": 0,
                "other_terminal_statuses": 0,
                "blocked_per_agent_hour": 386.4,
                "blocked_output_progress_bins": [10, 20, 30],
            },
            "subagent_stop": {
                "started": 0,
                "blocked": 0,
                "completed_or_allowed": 0,
                "other_terminal_statuses": 0,
            },
        },
        "run_log": {
            "first_timestamp": "2026-07-30 12:00:00,000",
            "last_timestamp": "2026-07-30 12:10:00,000",
        },
        "availability": {
            "per_event_wall_clock_timestamps": False,
            "stop_hook_active": False,
            "token_usage": False,
            "reason": "Persisted human output has no per-hook timestamps.",
        },
        "warnings": [],
        "evidence": {
            "agent_output": str(tmp_path / "agent_output.txt"),
            "evaluation_history": str(tmp_path / "run_history.json"),
        },
    }

    html = render_report_document(
        {
            "schema_version": 1,
            "report_kind": "codex-run",
            "data": observation,
        }
    )

    assert 'data-report-schema="codex-observability-report/v1"' in html
    assert "Codex Execution Report" in html
    assert "Codex Execution Timeline" in html
    assert "Submission Score Trajectory" in html
    assert "data-search-trajectory=" in html
    assert "agent submissions" in html
    assert "auto submissions" in html
    assert "Blocked Stop Distribution" in html
    assert "Top-level Stop" in html
    assert "Budget reached" in html
    assert "Successful · resumes 0" in html
    assert "Budget Exhausted" in html
    assert "Raw timeout flag" in html
    assert "Codex execution · budget reached" in html
    assert "691" in html
    assert "run_history.json" in html
    assert "Goal And Completion" not in html
    assert "EdgeBench" not in html

    observation["goal_plus"] = {
        "source_available": True,
        "overall_status": "incomplete",
        "records_total": 1,
        "active_records": 1,
        "terminal_records": 0,
        "reports_generated": 0,
        "records": [
            {
                "goal_plus_id": "gp_0001",
                "status": "active",
                "phase": "search",
                "next_action": "drive_search_run",
                "result_recorded_at": None,
                "selected_candidate_id": None,
                "search_tasks": [
                    {
                        "run_id": "run_promoted",
                        "state": "promoted",
                    }
                ],
            }
        ],
    }
    goal_plus_html = render_report_document(
        {
            "schema_version": 1,
            "report_kind": "codex-run",
            "data": observation,
        }
    )
    assert "Goal Plus Finalization Evidence" in goal_plus_html
    assert "Goal Plus finalization" in goal_plus_html
    assert "Incomplete" in goal_plus_html
    assert "gp_0001" in goal_plus_html
    assert "run_promoted=promoted" in goal_plus_html
    assert "valid promoted result does not make an active Goal" in goal_plus_html


def test_search_report_generates_self_contained_html_with_multi_search_timeline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "goal_plus.reporting._load_plotly_javascript",
        lambda: "window.Plotly={newPlot:function(){},Plots:{resize:function(){}}};",
    )
    project = make_project(tmp_path)
    root = tmp_path / ".search"
    search = FileSearchRuntime(root)
    frozen = search.freeze_spec(spec_for(project), [project / "evaluator.py"])

    first_run = search.create_run(frozen.frozen_spec_id)
    search.plan_next(first_run, requested_k=1)

    second_run = search.create_run(frozen.frozen_spec_id)
    second_plan = search.plan_next(second_run, requested_k=1)
    [candidate] = search.start_batch(second_run, second_plan.plan_id)
    session = search.start_agent_session(second_run, candidate.candidate_id)
    search.run_verifier(
        second_run,
        candidate.candidate_id,
        agent_session_id=session.agent_session_id,
        hypothesis="Exercise the durable timeline",
    )
    annotation = search._load_evidence_annotation_task(
        second_run, candidate.candidate_id, 1
    )
    assert annotation is not None
    annotation.state = "completed"
    annotation.attempts = 1
    monitor_relative = (
        f"runs/{second_run}/evidence-annotator/attempts/"
        f"{candidate.candidate_id}-iteration-0001-attempt-01.json"
    )
    annotation.attempt_history = [
        {
            "attempt": 1,
            "started_at": "2026-01-01T00:00:00Z",
            "finished_at": "2026-01-01T00:00:01Z",
            "monitor_path": monitor_relative,
        }
    ]
    annotation.view = EvidenceViewRecord(
        run_id=second_run,
        candidate_id=candidate.candidate_id,
        iteration=1,
        attempt_commit=annotation.attempt_commit,
        description="Moved dependency scheduling into the durable shared View.",
        created_at="2026-01-01T00:00:01Z",
    )
    search._write_evidence_annotation_task(annotation)
    external_evidence = tmp_path / "evaluations" / "auto-1.json"
    external_evidence.parent.mkdir()
    write_json(
        external_evidence,
        {
            "source": "edgebench",
            "artifact": {
                "source": "goal_plus_best",
                "run_id": second_run,
                "candidate_id": candidate.candidate_id,
                "iteration": 1,
                "commit": annotation.attempt_commit,
            },
            "evaluation": {
                "authority": "edgebench_official_hidden_judge",
                "round_id": "auto-1",
                "status": "completed",
                "valid": True,
                "score": 42,
                "score_0_100": 73.5,
                "published_at": "2026-01-01T00:02:00Z",
                "summary": "Official <hidden> result.",
            },
        },
    )
    write_json(
        external_evidence.with_name("auto-2.json"),
        {
            "source": "edgebench",
            "artifact": {
                "source": "goal_plus_best",
                "run_id": second_run,
                "candidate_id": candidate.candidate_id,
                "iteration": 1,
                "commit": annotation.attempt_commit,
            },
            "evaluation": {
                "authority": "edgebench_official_hidden_judge",
                "round_id": "auto-2",
                "status": "completed",
                "valid": True,
                "score": 43,
                "score_0_100": 74.5,
                "published_at": "2026-01-01T00:03:00Z",
                "summary": "Second official result.",
            },
        },
    )
    monkeypatch.setenv(EXTERNAL_EVIDENCE_DIR_ENV, str(external_evidence.parent))
    write_json(
        root / monitor_relative,
        {
            "schema_version": 1,
            "run_id": second_run,
            "candidate_id": candidate.candidate_id,
            "iteration": 1,
            "attempt": 1,
            "host": "pi-rpc",
            "model": "gpt-5.6-sol",
            "state": "completed",
            "started_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:01Z",
            "elapsed_seconds": 1.0,
            "stdout_bytes": 321,
            "stderr_bytes": 0,
            "json_lines": 4,
            "event_type_counts": {"message_end": 1, "message_update": 3},
            "last_events": [{"type": "message_end"}],
        },
    )

    goals = FileGoalPlusRuntime(root)
    goal = goals.create_goal("Optimize <script>alert('unsafe')</script> safely")
    goals.link_search_run(goal.goal_plus_id, frozen.frozen_spec_id, first_run)
    goals.link_search_run(goal.goal_plus_id, frozen.frozen_spec_id, second_run)
    _write_stop_hook_event(
        root,
        "hook_stop",
        goal_plus_id=goal.goal_plus_id,
    )
    _write_stop_hook_event(
        root,
        "hook_subagent_a",
        hook_event_name="SubagentStop",
        goal_plus_id=goal.goal_plus_id,
        host_agent_id="codex-child-a",
        agent_session_id=session.agent_session_id,
        run_id=second_run,
        candidate_id=candidate.candidate_id,
        decision="block",
        duration_ms=4.5,
    )
    _write_stop_hook_event(
        root,
        "hook_subagent_a_early",
        hook_event_name="SubagentStop",
        goal_plus_id=goal.goal_plus_id,
        host_agent_id="codex-child-a",
        decision="allow",
        duration_ms=1.5,
    )
    _write_stop_hook_event(
        root,
        "hook_subagent_b",
        hook_event_name="SubagentStop",
        goal_plus_id=goal.goal_plus_id,
        host_agent_id="codex-child-b",
        decision="allow",
        duration_ms=2.5,
    )
    _write_stop_hook_event(
        root,
        "hook_unrelated",
        goal_plus_id="gp_9999",
        run_id=second_run,
        decision="error",
    )

    with pytest.raises(RuntimeError, match="before every linked Goal Plus record"):
        search.report(second_run)
    direct_html_path = root / "debug-active-report.html"
    with pytest.raises(RuntimeError, match="before the linked record reaches"):
        write_html_report(root, second_run, direct_html_path)
    assert not (root / "runs" / second_run / "report.md").exists()
    assert not (root / "runs" / second_run / "report.html").exists()
    assert not direct_html_path.exists()

    goals.set_status(
        goal.goal_plus_id,
        "complete",
        reason="synthetic reporting fixture is ready",
        evidence=[{"kind": "unit_test"}],
    )
    markdown_path = search.report(second_run)
    html_path = markdown_path.with_suffix(".html")
    html = html_path.read_text(encoding="utf-8")

    assert markdown_path.is_file()
    assert html_path.is_file()
    assert 'data-report-schema="goal-plus-report/v1"' in html
    assert first_run in html
    assert second_run in html
    assert "Search Task 01" in html
    assert "Search Task 02" in html
    assert "Orchestration" in html
    assert "parallel_loops" in html
    assert session.agent_session_id in html
    assert html.count("<h2>Search Execution Timeline</h2>") == 2
    assert "Goal Plus Summary" in html
    assert "Goal status" in html
    assert "Selected score" in html
    assert "Estimated worker cost" in html
    assert "Combined estimated cost" in html
    assert "No score threshold was configured" in html
    assert "Metric availability" in html
    assert "Goal Plus Lifecycle" not in html
    assert "Planning Rounds" not in html
    assert "Final GP Aggregate" not in html
    assert "Unavailable Metrics Audit" not in html
    assert "Verifier activity" in html
    assert "Shared Evidence View" in html
    assert "Settled iterations" in html
    assert "Views published" in html
    assert "Official evaluations" in html
    assert "<strong>2</strong>Official evaluations" in html
    assert "Official Judge evaluation" in html
    assert "73.5" in html
    assert "74.5" in html
    assert "official / 100" in html
    assert "Official &lt;hidden&gt; result." in html
    assert "Official <hidden> result." not in html
    assert "Worker attempt" in html
    assert "Objective View" in html
    assert "Exercise the durable timeline" in html
    assert "Moved dependency scheduling into the durable shared View." in html
    assert "Annotator completed | 1.0s | 4 JSON events | last message_end" in html
    assert 'data-evidence-filter="candidate"' in html
    assert 'data-evidence-filter="view-state"' in html
    assert "data-evidence-row" in html
    assert "Complete normalized report data" in html
    assert "Stop Hook Activity" in html
    assert "Loop Agent Stop Outcomes" in html
    assert "Top-level Stop" in html
    assert "Subagent Breakdown" in html
    assert session.agent_session_id in html
    assert "codex-child-a" in html
    assert "codex-child-b" in html
    assert "hook_unrelated" not in html
    assert "Complete Search Trajectory" in html
    assert "data-search-trajectory=" in html
    assert "window.Plotly={newPlot" in html
    assert "payload.call_window" in html
    assert "Failed verifier · not scored" in html
    assert "linear score axis" in html
    assert 'class="score-step"' not in html
    assert "&lt;script&gt;alert(&#x27;unsafe&#x27;)&lt;/script&gt;" in html
    assert "<script>alert('unsafe')</script>" not in html
    assert "<script src=" not in html

    monkeypatch.setattr("goal_plus.reporting._load_plotly_javascript", lambda: None)
    report_data = build_html_report_data(root, second_run)
    [reported_iteration] = report_data["search_tasks"][1]["candidates"][0]["iterations"]
    assert reported_iteration["view"] == (
        "Moved dependency scheduling into the durable shared View."
    )
    assert reported_iteration["view_state"] == "completed"
    assert reported_iteration["annotation_monitor"]["state"] == "completed"
    assert reported_iteration["annotation_monitor"]["json_lines"] == 4
    assert len(reported_iteration["external_evaluations"]) == 2
    assert reported_iteration["external_evaluations"][0]["authority"] == (
        "edgebench_official_hidden_judge"
    )
    assert reported_iteration["external_evaluations"][0]["score_0_100"] == 73.5
    assert reported_iteration["external_evaluations"][1]["score_0_100"] == 74.5
    hook_stats = report_data["stop_hook_statistics"]
    assert hook_stats["events_total"] == 4
    assert hook_stats["by_event"]["Stop"]["events_total"] == 1
    assert hook_stats["by_event"]["SubagentStop"]["events_total"] == 3
    assert hook_stats["by_decision"]["block"] == 2
    assert hook_stats["by_decision"]["allow"] == 2
    assert [row["identity"] for row in hook_stats["subagents"]] == [
        session.agent_session_id,
        "codex-child-b",
    ]
    assert hook_stats["subagents"][0]["events_total"] == 2
    assert hook_stats["subagents"][0]["decisions"]["allow"] == 1
    fallback_html = render_html_report(report_data)
    assert "data-search-trajectory=" not in fallback_html
    assert 'class="score-step"' in fallback_html


def test_shared_evidence_view_does_not_substitute_worker_text_for_missing_view() -> (
    None
):
    html = _render_shared_evidence_view(
        {
            "candidates": [
                {
                    "candidate_id": "c001",
                    "selected": True,
                    "iterations": [
                        {
                            "iteration": 1,
                            "agent_session_id": "agent_001",
                            "score": 12,
                            "disposition": "keep",
                            "hypothesis": "Worker attempt <strong>is not a View</strong>.",
                            "git_head": "a" * 40,
                            "created_at": "2026-01-01T00:00:00Z",
                            "view": None,
                            "view_state": "terminal_error",
                            "view_error": "Annotation <failed> safely.",
                        },
                        {
                            "iteration": 2,
                            "agent_session_id": "agent_001",
                            "score": 10,
                            "disposition": "keep",
                            "hypothesis": "Second worker attempt.",
                            "git_head": "b" * 40,
                            "created_at": "2026-01-01T00:01:00Z",
                            "view": "Objective View <script>is escaped</script>.",
                            "view_state": "completed",
                            "view_error": None,
                            "toolization_decision": {
                                "outcome": "staged",
                                "signals": ["parser_trace_or_comparator"],
                                "exclusion": None,
                                "rationale": "Parses a non-trivial trace <safely>.",
                                "tool_names": ["score-helper"],
                            },
                            "toolization_advisories": [
                                "toolization_stage_missing"
                            ],
                            "shared_tool_staged_entries": [],
                            "shared_tool_publish_status": "not_staged",
                            "published_tool_views": [
                                {
                                    "tool_id": "score-helper",
                                    "summary": "Parses score output <safely>.",
                                    "entrypoint": "helper.py",
                                    "when_to_use": "Use for score parsing.",
                                }
                            ],
                            "adopted_tools": [
                                {
                                    "tool_id": "shared-parser",
                                    "snapshot_hash": "b" * 64,
                                    "receipt_id": "receipt-001",
                                }
                            ],
                            "adoption_confounded": True,
                        },
                    ],
                }
            ]
        }
    )

    assert "Not published" in html
    assert "Worker attempt &lt;strong&gt;is not a View&lt;/strong&gt;." in html
    assert "Annotation &lt;failed&gt; safely." in html
    assert "Objective View &lt;script&gt;is escaped&lt;/script&gt;." in html
    assert "Published Tool View" in html
    assert "Toolization Review" in html
    assert "parser_trace_or_comparator" in html
    assert "toolization_stage_missing" in html
    assert "Parses a non-trivial trace &lt;safely&gt;." in html
    assert "Tool Adoption Summary" in html
    assert "score-helper" in html
    assert "Parses score output &lt;safely&gt;." in html
    assert "shared-parser" in html
    assert "receipt receipt-001" in html
    assert "Confounded adoption trial" in html
    assert "No shared tool adopted" in html
    assert "<strong>is not a View</strong>" not in html
    assert "<script>is escaped</script>" not in html
    assert html.count("data-evidence-row") == 2
    assert 'data-view-state="terminal_error"' in html
    assert 'data-view-state="completed"' in html


def test_loop_agent_statistics_distinguish_productive_and_stalled_tails() -> None:
    run_id = "run_loop"

    def tool_events(category: str, count: int) -> list[dict[str, object]]:
        return [
            {
                "at": "2026-01-01T00:00:30Z",
                "kind": "tool_call",
                "category": category,
            }
            for _ in range(count)
        ]

    def message_events(
        classification: str,
        count: int,
    ) -> list[dict[str, object]]:
        return [
            {
                "at": "2026-01-01T00:00:30Z",
                "kind": "assistant_message",
                "classification": classification,
                "duplicate": index > 0,
            }
            for index in range(count)
        ]

    sessions = [
        {
            "host": "codex",
            "agent_session_id": "agent_c001",
            "candidate_id": "c001",
            "activity": {
                "available": True,
                "events": (
                    message_events("empty", 37) + message_events("substantive", 4)
                ),
            },
        },
        {
            "host": "codex",
            "agent_session_id": "agent_c002",
            "candidate_id": "c002",
            "activity": {"available": True, "events": []},
        },
        {
            "host": "codex",
            "agent_session_id": "agent_c003",
            "candidate_id": "c003",
            "activity": {
                "available": True,
                "events": (
                    tool_events("context", 137) + tool_events("global_evidence", 143)
                ),
            },
        },
        {
            "host": "codex",
            "agent_session_id": "agent_c004",
            "candidate_id": "c004",
            "activity": {
                "available": True,
                "events": (
                    tool_events("context", 1) + message_events("best_remains", 4)
                ),
            },
        },
    ]
    candidates = [
        {
            "candidate_id": candidate_id,
            "selected": candidate_id == "c002",
            "best_score": score,
            "iterations": (
                [
                    {
                        "iteration": 1,
                        "agent_session_id": f"agent_{candidate_id}",
                        "score": score,
                        "process_passed": True,
                        "git_head": f"{candidate_id}-base",
                        "created_at": "2026-01-01T00:00:05Z",
                    }
                ]
                if candidate_id != "c002"
                else [
                    {
                        "iteration": 1,
                        "agent_session_id": "agent_c002",
                        "score": 2900.0,
                        "process_passed": True,
                        "git_head": "c002-rev-1",
                        "created_at": "2026-01-01T00:00:12Z",
                    },
                    {
                        "iteration": 2,
                        "agent_session_id": "agent_c002",
                        "score": 2936.0,
                        "process_passed": True,
                        "git_head": "c002-rev-2",
                        "created_at": "2026-01-01T00:00:22Z",
                    },
                ]
            ),
        }
        for candidate_id, score in (
            ("c001", 2500.0),
            ("c002", 2936.0),
            ("c003", 2936.0),
            ("c004", 2588.0),
        )
    ]
    next(candidate for candidate in candidates if candidate["candidate_id"] == "c003")[
        "iterations"
    ].append(
        {
            "iteration": 2,
            "agent_session_id": None,
            "score": 2936.0,
            "process_passed": True,
            "git_head": "c003-parent-verifier",
            "created_at": "2026-01-01T00:00:40Z",
        }
    )
    tasks = [
        {
            "run_id": run_id,
            "frozen_spec": {"metric_direction": "maximize"},
            "sessions": sessions,
            "candidates": candidates,
        }
    ]
    stop_events = [
        {
            "hook_event_name": "SubagentStop",
            "run_id": run_id,
            "candidate_id": "c002",
            "agent_session_id": "agent_c002",
            "started_at": started_at,
            "finished_at": started_at,
            "decision": "block",
        }
        for started_at in (
            "2026-01-01T00:00:10Z",
            "2026-01-01T00:00:20Z",
        )
    ]

    statistics = _build_loop_agent_statistics(
        tasks,
        {"events": stop_events, "subagents": []},
    )
    rows = {row["candidate_id"]: row for row in statistics["rows"]}

    assert rows["c002"]["productive_blocked_stops"] == 2
    assert rows["c002"]["verified_revision_changes_after_blocked_stops"] == 2
    assert rows["c002"]["improvements_after_blocked_stops"] == 2
    assert rows["c002"]["tail_signal"] == "quiet"
    assert rows["c003"]["post_last_verifier"]["context_calls"] == 137
    assert rows["c003"]["post_last_verifier"]["global_evidence_calls"] == 143
    assert rows["c003"]["tail_signal"] == "polling-only"
    assert rows["c004"]["tail_signal"] == "answer-only"
    assert rows["c001"]["post_last_verifier"]["assistant_messages"] == 41
    assert rows["c001"]["post_last_verifier"]["empty_messages"] == 37
    assert rows["c001"]["tail_signal"] == "empty-output"

    html = _render_loop_agent_statistics(statistics)
    assert "137 total / 137 tail" in html
    assert "143 total / 143 tail" in html
    assert "41 messages / 37 empty" in html
    assert "polling-only" in html
    assert "answer-only" in html
    assert "empty-output" in html
    assert html.count("productive") >= 2


def test_search_trajectory_payload_keeps_parallel_candidate_loops() -> None:
    task = {
        "frozen_spec": {"metric_name": "quality", "metric_direction": "maximize"},
        "statistics": {
            "scores": {
                "metric_name": "quality",
                "direction": "maximize",
                "baseline": 0.0,
                "selected": 3.0,
            }
        },
        "candidates": [
            {
                "candidate_id": "c001",
                "selected": False,
                "iterations": [
                    {
                        "iteration": 1,
                        "score": 1.0,
                        "process_passed": True,
                        "created_at": "2026-01-01T00:00:01Z",
                    },
                    {
                        "iteration": 2,
                        "score": 2.0,
                        "process_passed": True,
                        "created_at": "2026-01-01T00:00:03Z",
                    },
                ],
            },
            {
                "candidate_id": "c002",
                "selected": True,
                "iterations": [
                    {
                        "iteration": 1,
                        "score": 0.5,
                        "process_passed": True,
                        "created_at": "2026-01-01T00:00:02Z",
                    },
                    {
                        "iteration": 2,
                        "agent_session_id": "agent_002",
                        "score": 3.0,
                        "process_passed": True,
                        "created_at": "2026-01-01T00:00:04Z",
                    },
                    {
                        "iteration": 3,
                        "agent_session_id": "agent_002",
                        "score": 3.0,
                        "process_passed": True,
                        "created_at": "2026-01-01T00:00:05Z",
                    },
                ],
            },
        ],
    }

    payload = _search_trajectory_payload(task)

    assert payload is not None
    assert payload["evaluations"] == 5
    assert payload["passing_evaluations"] == 5
    assert payload["failed_evaluations"] == 0
    assert payload["call_window"] == {
        "start": 0,
        "end": 5,
        "tick": 1,
        "marker_size": 7,
    }
    assert payload["score_axis"]["type"] == "linear"
    assert [trace["calls"] for trace in payload["trajectories"]] == [[1, 3], [2, 4, 5]]
    assert payload["global_best"] == {
        "calls": [0, 1, 2, 3, 4, 5],
        "scores": [0.0, 1.0, 1.0, 2.0, 3.0, 3.0],
    }
    assert payload["selected_point"] == {
        "candidate_id": "c002",
        "call": 5,
        "score": 3.0,
    }
    assert payload["trajectories"][1]["details"][1][1] == "worker verifier"


def test_ineligible_iterations_do_not_enter_report_best_score_paths() -> None:
    task = {
        "run_id": "run_0001",
        "run": {"created_at": "2026-01-01T00:00:00Z"},
        "frozen_spec": {"metric_name": "cycles", "metric_direction": "minimize"},
        "statistics": {
            "scores": {
                "metric_name": "cycles",
                "direction": "minimize",
                "baseline": 100.0,
                "selected": 90.0,
            }
        },
        "sessions": [
            {
                "agent_session_id": "agent_001",
                "candidate_id": "c001",
                "started_at": "2026-01-01T00:00:30Z",
                "ended_at": "2026-01-01T00:03:30Z",
                "terminal_state": "completed",
            }
        ],
        "candidates": [
            {
                "candidate_id": "c001",
                "selected": True,
                "iterations": [
                    {
                        "iteration": 1,
                        "agent_session_id": "agent_001",
                        "score": 0.0,
                        "process_passed": False,
                        "created_at": "2026-01-01T00:01:00Z",
                    },
                    {
                        "iteration": 2,
                        "agent_session_id": "agent_001",
                        "score": 90.0,
                        "process_passed": True,
                        "created_at": "2026-01-01T00:02:00Z",
                    },
                    {
                        "iteration": 3,
                        "agent_session_id": "agent_001",
                        "score": 0.5,
                        "process_passed": None,
                        "created_at": "2026-01-01T00:03:00Z",
                    },
                ],
            }
        ],
    }

    _build_timeline(None, [], [task])

    [worker] = [
        event
        for event in task["timeline"]["events"]
        if event["kind"] == "worker_session"
    ]
    assert worker["score"] == 90.0
    assert [
        point["score"] for point in task["timeline"]["performance"]["score"]["points"]
    ] == [90.0]

    payload = _search_trajectory_payload(task)
    assert payload is not None
    assert payload["passing_evaluations"] == 1
    assert payload["failed_evaluations"] == 2
    assert payload["trajectories"][0]["scores"] == [90.0]
    assert payload["trajectories"][0]["failed_scores"] == [0.0, 0.5]
    assert payload["global_best"] == {
        "calls": [0, 1, 2, 3],
        "scores": [100.0, 100.0, 90.0, 90.0],
    }


def test_search_trajectory_payload_adapts_axes_and_excludes_failed_scores() -> None:
    iterations = [
        {
            "iteration": iteration,
            "score": 0.0 if iteration == 1 else 1000.0 / (iteration + 1),
            "process_passed": iteration != 1,
        }
        for iteration in range(1, 131)
    ]
    task = {
        "frozen_spec": {"metric_name": "cycles", "metric_direction": "minimize"},
        "statistics": {
            "scores": {
                "metric_name": "cycles",
                "direction": "minimize",
                "baseline": 1000.0,
                "selected": 1000.0 / 131,
            }
        },
        "candidates": [
            {
                "candidate_id": "c001",
                "selected": True,
                "iterations": iterations,
            }
        ],
    }

    payload = _search_trajectory_payload(task)

    assert payload is not None
    assert payload["evaluations"] == 130
    assert payload["passing_evaluations"] == 129
    assert payload["failed_evaluations"] == 1
    assert payload["call_window"] == {
        "start": 0,
        "end": 130,
        "tick": 20,
        "marker_size": 5,
    }
    assert payload["score_axis"]["type"] == "log"
    assert payload["trajectories"][0]["calls"][0] == 2
    assert payload["trajectories"][0]["failed_calls"] == [1]
    assert payload["global_best"]["scores"][:2] == [1000.0, 1000.0]
    assert min(payload["global_best"]["scores"]) > 0
    assert payload["selected_point"] == {
        "candidate_id": "c001",
        "call": 130,
        "score": 1000.0 / 131,
    }


def test_html_report_data_keeps_search_tasks_and_rounds_separate(
    tmp_path: Path,
) -> None:
    project = make_project(tmp_path)
    root = tmp_path / ".search"
    search = FileSearchRuntime(root)
    frozen = search.freeze_spec(spec_for(project), [project / "evaluator.py"])
    runs = [search.create_run(frozen.frozen_spec_id) for _ in range(2)]
    for run_id in runs:
        search.plan_next(run_id, requested_k=1)

    goals = FileGoalPlusRuntime(root)
    goal = goals.create_goal("Compare two independent Search tasks")
    for run_id in runs:
        goals.link_search_run(goal.goal_plus_id, frozen.frozen_spec_id, run_id)

    data = build_html_report_data(root, runs[-1])

    assert data["goal_plus_id"] == goal.goal_plus_id
    assert [task["run_id"] for task in data["search_tasks"]] == runs
    assert all(
        task["strategy"]["orchestration_mode"] == "parallel_loops"
        for task in data["search_tasks"]
    )
    assert [len(task["plans"]) for task in data["search_tasks"]] == [1, 1]
    assert all(task["timeline"]["duration_seconds"] for task in data["search_tasks"])
    assert data["snapshot"]["search_task_aggregate"]["search_tasks_total"] == 2
    assert data["snapshot"]["search_task_aggregate"]["planning_rounds_total"] == 2


def test_pi_native_session_resume_renders_distinct_process_dispatches(
    tmp_path: Path,
) -> None:
    project = make_project(tmp_path)
    root = tmp_path / ".search"
    search = FileSearchRuntime(root)
    spec_data = spec_for(project, max_parallel=1).model_dump(mode="json")
    spec_data["strategy"] = {
        "name": "random",
        "worker_host": "pi-rpc",
        "worker_budget": {
            "max_runtime_seconds": 60,
            "on_exceed": "interrupt",
        },
    }
    frozen = search.freeze_spec(
        SearchSpec.model_validate(spec_data),
        [project / "evaluator.py"],
    )
    run_id = search.create_run(frozen.frozen_spec_id)
    plan = search.plan_next(run_id, requested_k=1)
    candidate = search.start_batch(run_id, plan.plan_id)[0]
    session = search.start_agent_session(run_id, candidate.candidate_id)

    def bind_dispatch(
        *,
        pid: int,
        start_at: str,
        end_at: str,
        last_entry_id: str,
        entry_count: int,
        cumulative_input: int,
    ) -> None:
        search.bind_agent_handle(
            session.agent_session_id,
            {
                "host": "pi-rpc",
                "external_id": session.agent_session_id,
                "metadata": {
                    "process_pid": pid,
                    "continuation": "native_session",
                    "pi_metrics": {
                        "scope": "session_cumulative_incremental",
                        "dispatch_started_at": start_at,
                        "dispatch_ended_at": end_at,
                        "dispatch_duration_seconds": 10.0,
                        "started_at": "2026-07-19T00:00:00Z",
                        "ended_at": end_at,
                        "duration_seconds": entry_count * 10.0,
                        "final_last_entry_id": last_entry_id,
                        "final_entry_count": entry_count,
                        "usage_delta": {
                            "assistantMessages": 1,
                            "input": 10,
                            "output": 2,
                            "cacheRead": 3,
                            "cacheWrite": 0,
                            "costTotal": 0.01,
                        },
                        "usage_total": {
                            "assistantMessages": entry_count,
                            "input": cumulative_input,
                            "output": entry_count * 2,
                            "cacheRead": entry_count * 3,
                            "cacheWrite": 0,
                            "costTotal": entry_count * 0.01,
                        },
                    },
                },
            },
        )

    bind_dispatch(
        pid=111,
        start_at="2026-07-19T00:00:00Z",
        end_at="2026-07-19T00:00:10Z",
        last_entry_id="entry_1",
        entry_count=1,
        cumulative_input=10,
    )
    continued = search.continue_agent_session(session.agent_session_id)
    assert continued.launch["metrics_baseline"]["last_entry_id"] == "entry_1"
    bind_dispatch(
        pid=222,
        start_at="2026-07-19T00:00:20Z",
        end_at="2026-07-19T00:00:30Z",
        last_entry_id="entry_2",
        entry_count=2,
        cumulative_input=20,
    )
    _write_stop_hook_event(
        root,
        "hook_resume",
        hook_event_name="SubagentStop",
        run_id=run_id,
        candidate_id=candidate.candidate_id,
        agent_session_id=session.agent_session_id,
        decision="block",
    )
    record = search._load_candidate_record(run_id, candidate.candidate_id)
    record.iterations = [
        IterationRecord(
            iteration=1,
            score=0.0,
            process_passed=True,
            hypothesis="parent baseline",
            summary="parent baseline",
            disposition="discard",
            created_at="2026-07-19T00:00:10Z",
        ),
        IterationRecord(
            iteration=2,
            agent_session_id=session.agent_session_id,
            score=9.0,
            process_passed=True,
            hypothesis="worker improvement",
            summary="worker improvement",
            disposition="keep",
            created_at="2026-07-19T00:00:25Z",
        ),
    ]
    search._write_candidate_record(run_id, record)

    data = build_html_report_data(root, run_id)
    task = data["search_tasks"][0]
    activity = data["snapshot"]["search_task_aggregate"]["statistics"]["activity"]
    assert activity == {
        "candidates_submitted": 1,
        "candidates_completed_with_result": 1,
        "results_total": 2,
        "results_kept": 1,
        "results_rejected": 1,
        "results_unsettled": 0,
        "agent_resumes": 1,
        "same_session_resumes": 1,
        "redispatch_resumes": 0,
    }
    assert len(task["sessions"]) == 2
    assert [item["dispatch_index"] for item in task["sessions"]] == [1, 2]
    assert {item["agent_session_id"] for item in task["sessions"]} == {
        session.agent_session_id
    }
    assert [item["score"] for item in task["sessions"]] == [0.0, 9.0]
    worker_events = [
        event
        for event in task["timeline"]["events"]
        if event["kind"] == "worker_session"
    ]
    assert [event["process_pid"] for event in worker_events] == [111, 222]
    assert [event["attempt_index"] for event in worker_events] == [1, 2]
    assert all(event["attempt_count"] == 2 for event in worker_events)
    assert [event["score"] for event in worker_events] == [0.0, 9.0]
    assert [event["score_raw"] for event in worker_events] == [0.0, 9.0]
    assert [event["score_gain"] for event in worker_events] == [None, None]
    performance = task["timeline"]["performance"]
    assert performance["score"]["baseline"] is None
    assert performance["score"]["baseline_source"] is None
    assert "score_gain" not in performance["metric_ranges"]
    assert performance["metric_ranges"]["score_raw"] == {
        "min": 0.0,
        "max": 9.0,
        "observed": 2,
    }
    html = _render_timeline(task["timeline"], title="Pi dispatch score gain")
    assert "No baseline" in html
    assert "Score gain</button><button" in html
    assert ">Score raw</button>" in html
    assert 'data-score-gain-baseline="false"' in html
    assert "data-metric-score-gain=" not in html
    assert 'data-metric-score-raw="0.000000000"' in html
    assert 'data-metric-score-raw="9.000000000"' in html
    report_html = render_html_report(data)
    assert "Candidate Loop Activity" in report_html
    assert "Candidate Submissions" in report_html
    assert "Completed With Result" in report_html
    assert "Rejected Results" in report_html
    assert "Agent Resumes" in report_html
    assert "Stop Hook Continue Triggers" in report_html


def test_worker_duration_uses_search_scale_not_goal_record_lifecycle() -> None:
    goal = GoalPlusRecord(
        goal_plus_id="gp_0001",
        raw_goal="Keep worker execution separate from record activity",
        status="complete",
        phase="final_audit",
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T01:00:00Z",
    )
    tasks = [
        {
            "run_id": "run_0001",
            "run": {"created_at": "2026-01-01T00:01:00Z"},
            "plans": [],
            "sessions": [
                {
                    "agent_session_id": "agent_0002",
                    "started_at": "2026-01-01T00:02:00Z",
                    "ended_at": "2026-01-01T00:03:00Z",
                    "duration_seconds": 60.0,
                    "terminal_state": "completed",
                }
            ],
            "candidates": [
                {
                    "candidate_id": "c001",
                    "iterations": [],
                    "promotion_passed": True,
                    "promotion_evidence_at": "2026-01-01T00:06:29Z",
                }
            ],
        }
    ]

    goal_timeline = _build_timeline(goal, [], tasks)
    search_timeline = tasks[0]["timeline"]
    [worker] = [
        event
        for event in search_timeline["events"]
        if event["kind"] == "worker_session"
    ]

    assert goal_timeline["duration_seconds"] == 3600.0
    assert search_timeline["duration_seconds"] == 329.0
    assert _epoch(worker["end_at"]) - _epoch(worker["start_at"]) == 60.0
    assert goal_timeline["events"][0]["label"] == "Goal record activity window"


def test_statistics_renders_observed_session_count_as_count() -> None:
    html = _render_statistics(
        {
            "statistics": {
                "timing": {
                    "worker_duration_seconds_total": 12.5,
                    "worker_duration_sessions_observed": 4,
                }
            }
        }
    )

    assert "12.5s" in html
    assert ">4</strong>" in html
    assert "4.0s" not in html


def test_report_candidate_final_score_uses_settled_best_after_failure(
    tmp_path: Path,
) -> None:
    project = make_project(tmp_path)
    root = tmp_path / ".search"
    search = FileSearchRuntime(root)
    frozen = search.freeze_spec(
        spec_for(project, max_parallel=1), [project / "evaluator.py"]
    )
    run_id = search.create_run(frozen.frozen_spec_id)
    plan = search.plan_next(run_id, requested_k=1)
    candidate = search.start_batch(run_id, plan.plan_id)[0]
    record = search._load_candidate_record(run_id, candidate.candidate_id)
    record.iterations = [
        IterationRecord(
            iteration=1,
            score=0.9,
            process_passed=True,
            created_at="2026-01-01T00:00:01Z",
        ),
        IterationRecord(
            iteration=2,
            score=0.0,
            process_passed=False,
            created_at="2026-01-01T00:00:02Z",
        ),
    ]
    record.score_report = ScoreReport(
        run_id=run_id,
        candidate_id=candidate.candidate_id,
        validity_passed=False,
        process_passed=False,
        aggregate_score=0.0,
        verifier_results=[],
    )
    search._write_candidate_record(run_id, record)

    [payload] = build_html_report_data(root, run_id)["search_tasks"][0]["candidates"]

    assert payload["score"] == 0.9
    assert payload["process_passed"] is True
    assert payload["best_score"] == 0.9


def test_metric_lens_combines_score_progression_and_session_efficiency() -> None:
    tasks = [
        {
            "run_id": "run_metric_lens",
            "run": {"created_at": "2026-01-01T00:00:00Z"},
            "frozen_spec": {
                "metric_name": "quality",
                "metric_direction": "maximize",
                "budget": {"max_parallel": 2},
            },
            "statistics": {
                "scores": {
                    "metric_name": "quality",
                    "direction": "maximize",
                    "baseline": 0.5,
                    "selected": 0.8,
                }
            },
            "plans": [],
            "sessions": [
                {
                    "agent_session_id": "agent_metric_001",
                    "candidate_id": "c001",
                    "started_at": "2026-01-01T00:01:00Z",
                    "ended_at": "2026-01-01T00:02:00Z",
                    "duration_seconds": 60.0,
                    "terminal_state": "completed",
                    "processed_tokens": 600,
                    "cost_usd": 0.06,
                    "verifier_runs": 2,
                },
                {
                    "agent_session_id": "agent_metric_002",
                    "candidate_id": "c001",
                    "started_at": "2026-01-01T00:10:00Z",
                    "ended_at": "2026-01-01T00:11:00Z",
                    "duration_seconds": 60.0,
                    "terminal_state": "timed_out",
                    "processed_tokens": 1200,
                    "cost_usd": 0.12,
                    "verifier_runs": 4,
                },
            ],
            "candidates": [
                {
                    "candidate_id": "c001",
                    "iterations": [
                        {
                            "iteration": 1,
                            "agent_session_id": "agent_metric_001",
                            "score": 0.6,
                            "process_passed": True,
                            "created_at": "2026-01-01T00:01:30Z",
                        },
                        {
                            "iteration": 2,
                            "agent_session_id": "agent_metric_002",
                            "score": 0.8,
                            "process_passed": True,
                            "created_at": "2026-01-01T00:10:30Z",
                        },
                    ],
                    "promotion_passed": True,
                    "promotion_evidence_at": "2026-01-01T00:12:00Z",
                }
            ],
        }
    ]

    _build_timeline(None, [], tasks)
    timeline = tasks[0]["timeline"]
    workers = [
        event for event in timeline["events"] if event["kind"] == "worker_session"
    ]
    performance = timeline["performance"]

    assert [event["tokens_per_minute"] for event in workers] == [600.0, 1200.0]
    assert [event["attempt_index"] for event in workers] == [1, 2]
    assert all(event["attempt_count"] == 2 for event in workers)
    assert [point["score"] for point in performance["score"]["points"]] == [0.6, 0.8]
    assert performance["metric_ranges"]["tokens_per_minute"] == {
        "min": 600.0,
        "max": 1200.0,
        "observed": 2,
    }
    assert performance["metric_ranges"]["score_gain"] == {
        "min": 0.09999999999999998,
        "max": 0.30000000000000004,
        "observed": 2,
    }
    assert performance["metric_ranges"]["score_raw"] == {
        "min": 0.6,
        "max": 0.8,
        "observed": 2,
    }
    assert performance["idle_intervals"][0]["duration_seconds"] == 480.0

    html = _render_timeline(timeline, title="Metric Lens Timeline")

    assert "data-metric-lens" in html
    assert 'data-metric-mode="score-gain"' in html
    assert 'data-metric-score-gain="0.100000000"' in html
    assert 'data-metric-score-raw="0.600000000"' in html
    assert 'data-score-gain-baseline="true"' in html
    assert "Score gain</button><button" in html
    assert ">Score raw</button>" in html
    assert 'data-metric-tokens-per-minute="600.000000000"' in html
    assert 'data-metric-verifier-density="2.000000000" style=' in html
    assert 'class="score-step"' in html
    assert "Baseline 0.5" in html
    assert "Selected 0.8" in html
    assert "retry 2/2" in html
    assert "session-failure" in html
    assert "Idle 8m 0s" in html


def test_long_dense_timeline_renders_horizontal_and_vertical_scroll_surfaces() -> None:
    events = [
        {
            "lane": "main",
            "kind": "main_span",
            "label": "Search orchestration",
            "start_at": "2026-01-01T00:00:00Z",
            "end_at": "2026-01-01T02:00:00Z",
        }
    ]
    events.extend(
        {
            "lane": "worker",
            "kind": "worker_session",
            "label": f"agent_{index:04d} / completed",
            "session_id": f"agent_{index:04d}",
            "start_at": "2026-01-01T00:00:00Z",
            "end_at": "2026-01-01T00:01:00Z",
            "terminal_state": "completed",
        }
        for index in range(20)
    )

    html = _render_timeline(
        {
            "start_at": "2026-01-01T00:00:00Z",
            "end_at": "2026-01-01T02:00:00Z",
            "duration_seconds": 7200.0,
            "events": events,
        },
        title="Dense Search Timeline",
    )

    assert 'class="timeline-scroll" tabindex="0"' in html
    assert 'style="--timeline-width:9790px"' in html
    assert 'class="timeline-rows" data-track-count="21"' in html
