from __future__ import annotations

import os
from pathlib import Path
import threading
import time
from typing import Any

import pytest

import goal_plus.pi_pool as pi_pool
from goal_plus.pi_pool import (
    close_pi_search_pool,
    continue_pi_search_pool,
    open_pi_search_pool,
    snapshot_pi_search_pool,
    run_pool_worker,
    wait_any_pi_search_pool,
)
from goal_plus.models import SearchSpec
from goal_plus.runtime import FileSearchRuntime, exclusive_file_lock, load_json, utc_timestamp, write_json
from tests.test_pi_driver import _make_project, _pi_rpc_spec_with_budget


pytestmark = pytest.mark.pi


def _planned_candidates(
    runtime: FileSearchRuntime,
    run_id: str,
    count: int,
) -> list[str]:
    plan = runtime.plan_next(run_id, requested_k=count)
    return [task.candidate_id for task in runtime.start_batch(run_id, plan.plan_id)]


def _open_active_pool_with_closeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[FileSearchRuntime, str, str]:
    project = _make_project(tmp_path)
    runtime = FileSearchRuntime(tmp_path / ".search")
    spec_data = _pi_rpc_spec_with_budget(
        project, max_parallel=1
    ).model_dump(mode="json")
    spec_data["strategy"]["config"] = {"closeout_reserve_seconds": 30}
    frozen = runtime.freeze_spec(
        SearchSpec.model_validate(spec_data),
        [project / "evaluator.py"],
    )
    run_id = runtime.create_run(frozen.frozen_spec_id)
    candidate_id = _planned_candidates(runtime, run_id, 1)[0]
    monkeypatch.setattr(pi_pool, "_launch_pool_job", lambda **_kwargs: os.getpid())
    opened = open_pi_search_pool(
        root_dir=runtime.root_dir,
        run_id=run_id,
        candidate_ids=[candidate_id],
    )
    return runtime, run_id, opened["pool_id"]


def test_pi_pool_wait_any_reports_free_slot_without_refilling(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _make_project(tmp_path)
    runtime = FileSearchRuntime(tmp_path / ".search")
    frozen = runtime.freeze_spec(
        _pi_rpc_spec_with_budget(project, max_parallel=2),
        [project / "evaluator.py"],
    )
    run_id = runtime.create_run(frozen.frozen_spec_id)
    initial = _planned_candidates(runtime, run_id, 2)
    completion_delays = iter((0.05, None))
    release_slowest = threading.Event()
    threads: list[threading.Thread] = []

    def fake_launcher(*, root_dir: Path | str, pool_id: str, job_id: str) -> int:
        delay = next(completion_delays)

        def complete() -> None:
            request = load_json(pi_pool._job_dir(root_dir, pool_id, job_id) / "request.json")
            if delay is None:
                release_slowest.wait(timeout=5)
            else:
                time.sleep(delay)
            result = {
                "ok": True,
                "run_id": request["run_id"],
                "candidate_id": request["candidate_id"],
                "agent_session_id": f"agent_{job_id}",
                "steps": [
                    {"tool": "search_bind_agent_handle"},
                    {"tool": "search_run_verifier"},
                ],
                "final_score_report": {"aggregate_score": 1.0, "process_passed": True},
            }
            with exclusive_file_lock(pi_pool._pool_lock_path(root_dir, pool_id)):
                write_json(pi_pool._job_dir(root_dir, pool_id, job_id) / "result.json", result)
                job = pi_pool._load_job(root_dir, pool_id, job_id)
                job.update(
                    {
                        "status": "completed",
                        "finished_at": utc_timestamp(),
                        "error": None,
                    }
                )
                pi_pool._write_job(root_dir, pool_id, job)

        thread = threading.Thread(target=complete, daemon=True)
        thread.start()
        threads.append(thread)
        return os.getpid()

    monkeypatch.setattr(pi_pool, "_launch_pool_job", fake_launcher)
    opened = open_pi_search_pool(
        root_dir=runtime.root_dir,
        run_id=run_id,
        candidate_ids=initial,
        max_parallel=2,
    )
    pool_id = opened["pool_id"]
    rediscovered = snapshot_pi_search_pool(root_dir=runtime.root_dir, run_id=run_id)
    assert [pool["pool_id"] for pool in rediscovered["pools"]] == [pool_id]

    first = wait_any_pi_search_pool(
        root_dir=runtime.root_dir,
        pool_id=pool_id,
        timeout_seconds=1,
        poll_interval_seconds=0.01,
    )

    assert [event["candidate_id"] for event in first["events"]] == [initial[0]]
    assert first["events"][0]["kind"] == "candidate_ready"
    assert first["free_slots"] == 1
    assert first["active_count"] == 1

    after_first = snapshot_pi_search_pool(root_dir=runtime.root_dir, pool_id=pool_id)
    assert after_first["active_count"] == 1
    assert after_first["free_slots"] == 1
    assert [job["candidate_id"] for job in after_first["jobs"]] == initial

    release_slowest.set()
    observed = []
    while len(observed) < 1:
        update = wait_any_pi_search_pool(
            root_dir=runtime.root_dir,
            pool_id=pool_id,
            timeout_seconds=1,
            poll_interval_seconds=0.01,
        )
        observed.extend(event["candidate_id"] for event in update["events"])
    assert observed == [initial[1]]

    closed = close_pi_search_pool(
        root_dir=runtime.root_dir,
        pool_id=pool_id,
        mode="drain",
        timeout_seconds=1,
    )
    assert closed["state"] == "closed"
    assert closed["active_count"] == 0
    for thread in threads:
        thread.join(timeout=1)


def test_pi_pool_rejects_early_close_while_outer_lease_is_active(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, _run_id, pool_id = _open_active_pool_with_closeout(
        tmp_path, monkeypatch
    )
    monkeypatch.setenv("GOAL_PLUS_OUTER_DEADLINE_AT", "1100")
    monkeypatch.setattr(pi_pool.time, "time", lambda: 1000.0)

    with pytest.raises(RuntimeError, match="outside the closeout reserve"):
        close_pi_search_pool(
            root_dir=runtime.root_dir,
            pool_id=pool_id,
            mode="drain",
            timeout_seconds=0,
        )

    snapshot = snapshot_pi_search_pool(root_dir=runtime.root_dir, pool_id=pool_id)
    assert snapshot["state"] == "open"
    assert snapshot["active_count"] == 1


@pytest.mark.parametrize("invalidate_run", [False, True])
def test_pi_pool_allows_active_close_in_reserve_or_after_invalidation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    invalidate_run: bool,
) -> None:
    runtime, run_id, pool_id = _open_active_pool_with_closeout(
        tmp_path, monkeypatch
    )
    monkeypatch.setenv(
        "GOAL_PLUS_OUTER_DEADLINE_AT", "1100" if invalidate_run else "1020"
    )
    monkeypatch.setattr(pi_pool.time, "time", lambda: 1000.0)
    if invalidate_run:
        runtime.invalidate_run(
            run_id,
            reason="verifier_infrastructure_failure",
            summary="confirmed infrastructure failure",
            evidence=[{"failure_class": "VerifierInfrastructureFailure"}],
        )

    closed = close_pi_search_pool(
        root_dir=runtime.root_dir,
        pool_id=pool_id,
        mode="drain",
        timeout_seconds=0,
    )

    assert closed["state"] == "draining"
    assert closed["active_count"] == 1
    assert closed["close_timed_out"] is True


def test_pi_pool_enforces_frozen_parallel_limit(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    runtime = FileSearchRuntime(tmp_path / ".search")
    frozen = runtime.freeze_spec(
        _pi_rpc_spec_with_budget(project, max_parallel=1),
        [project / "evaluator.py"],
    )
    run_id = runtime.create_run(frozen.frozen_spec_id)

    with pytest.raises(ValueError, match="exceeds frozen Search limit"):
        open_pi_search_pool(
            root_dir=runtime.root_dir,
            run_id=run_id,
            max_parallel=2,
        )


def test_pi_pool_continue_pins_existing_native_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _make_project(tmp_path)
    runtime = FileSearchRuntime(tmp_path / ".search")
    frozen = runtime.freeze_spec(
        _pi_rpc_spec_with_budget(project, max_parallel=1),
        [project / "evaluator.py"],
    )
    run_id = runtime.create_run(frozen.frozen_spec_id)
    candidate_id = _planned_candidates(runtime, run_id, 1)[0]
    session = runtime.start_agent_session(run_id, candidate_id)
    opened = open_pi_search_pool(root_dir=runtime.root_dir, run_id=run_id)
    monkeypatch.setattr(pi_pool, "_launch_pool_job", lambda **_kwargs: 999999)

    submitted = continue_pi_search_pool(
        root_dir=runtime.root_dir,
        pool_id=opened["pool_id"],
        candidate_id=candidate_id,
    )

    request = load_json(
        pi_pool._job_dir(
            runtime.root_dir,
            opened["pool_id"],
            submitted["job_id"],
        )
        / "request.json"
    )
    assert submitted["continuation"] == "native_session"
    assert submitted["refresh_session"] is False
    assert request["redispatch"] is True
    assert request["refresh_session"] is False
    assert request["resume_agent_session_id"] == session.agent_session_id


def test_pi_pool_continue_honors_pending_session_refresh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _make_project(tmp_path)
    runtime = FileSearchRuntime(tmp_path / ".search")
    frozen = runtime.freeze_spec(
        _pi_rpc_spec_with_budget(project, max_parallel=1),
        [project / "evaluator.py"],
    )
    run_id = runtime.create_run(frozen.frozen_spec_id)
    candidate_id = _planned_candidates(runtime, run_id, 1)[0]
    session = runtime.start_agent_session(run_id, candidate_id)
    monkeypatch.setattr(pi_pool, "_launch_pool_job", lambda **_kwargs: 999999)
    opened = open_pi_search_pool(
        root_dir=runtime.root_dir,
        run_id=run_id,
        candidate_ids=[candidate_id],
    )
    first_job_id = opened["submitted"][0]["job_id"]
    with exclusive_file_lock(
        pi_pool._pool_lock_path(runtime.root_dir, opened["pool_id"])
    ):
        write_json(
            pi_pool._job_dir(runtime.root_dir, opened["pool_id"], first_job_id)
            / "result.json",
            {
                "ok": True,
                "agent_session_id": session.agent_session_id,
                "handle": {
                    "metadata": {
                        "refresh_required": True,
                        "refresh_reason": "length_without_tool_call",
                    }
                },
                "lease": {
                    "satisfied": True,
                    "pending_session_refresh_reason": "length_without_tool_call",
                },
            },
        )
        job = pi_pool._load_job(runtime.root_dir, opened["pool_id"], first_job_id)
        job.update(
            {
                "status": "completed",
                "finished_at": utc_timestamp(),
                "error": None,
            }
        )
        pi_pool._write_job(runtime.root_dir, opened["pool_id"], job)

    submitted = continue_pi_search_pool(
        root_dir=runtime.root_dir,
        pool_id=opened["pool_id"],
        candidate_id=candidate_id,
    )
    request = load_json(
        pi_pool._job_dir(
            runtime.root_dir,
            opened["pool_id"],
            submitted["job_id"],
        )
        / "request.json"
    )

    assert submitted["continuation"] == "new_session_refresh"
    assert submitted["refresh_session"] is True
    assert request["refresh_reason"] == "length_without_tool_call"
    assert request["resume_agent_session_id"] == session.agent_session_id


def test_pi_pool_rejects_work_after_run_invalidation(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    runtime = FileSearchRuntime(tmp_path / ".search")
    frozen = runtime.freeze_spec(
        _pi_rpc_spec_with_budget(project, max_parallel=1),
        [project / "evaluator.py"],
    )
    run_id = runtime.create_run(frozen.frozen_spec_id)
    runtime.invalidate_run(
        run_id,
        reason="verifier_infrastructure_failure",
        summary="main agent confirmed verifier infrastructure failure",
        evidence=[{"failure_class": "VerifierWorkspaceSideEffect"}],
    )

    with pytest.raises(RuntimeError, match="invalidated"):
        open_pi_search_pool(root_dir=runtime.root_dir, run_id=run_id)


def test_pi_pool_worker_publishes_candidate_ready_after_driver_completion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _make_project(tmp_path)
    runtime = FileSearchRuntime(tmp_path / ".search")
    frozen = runtime.freeze_spec(
        _pi_rpc_spec_with_budget(project, max_parallel=1),
        [project / "evaluator.py"],
    )
    run_id = runtime.create_run(frozen.frozen_spec_id)
    candidate_id = _planned_candidates(runtime, run_id, 1)[0]
    monkeypatch.setattr(pi_pool, "_launch_pool_job", lambda **_kwargs: os.getpid())
    opened = open_pi_search_pool(
        root_dir=runtime.root_dir,
        run_id=run_id,
        candidate_ids=[candidate_id],
    )
    pool_id = opened["pool_id"]
    submitted = opened["submitted"][0]

    def fake_driver(**request: Any) -> dict[str, Any]:
        return {
            "ok": True,
            "run_id": request["run_id"],
            "candidate_id": request["candidate_id"],
            "agent_session_id": "agent_ready",
            "steps": [
                {"tool": "search_start_agent_session"},
                {"tool": "search_bind_agent_handle"},
                {"tool": "search_run_verifier"},
            ],
            "final_score_report": {"aggregate_score": 2.0, "process_passed": True},
        }

    monkeypatch.setattr(pi_pool, "run_pi_search_candidate", fake_driver)
    assert run_pool_worker(
        root_dir=runtime.root_dir,
        pool_id=pool_id,
        job_id=submitted["job_id"],
    ) == 0

    waited = wait_any_pi_search_pool(
        root_dir=runtime.root_dir,
        pool_id=pool_id,
        timeout_seconds=0,
    )
    assert waited["events"][0]["kind"] == "candidate_ready"
    assert waited["events"][0]["agent_session_id"] == "agent_ready"
    assert [step["tool"] for step in waited["events"][0]["result"]["steps"]] == [
        "search_start_agent_session",
        "search_bind_agent_handle",
        "search_run_verifier",
    ]


def test_pi_pool_allocation_decision_releases_unsatisfied_minimum_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _make_project(tmp_path)
    runtime = FileSearchRuntime(tmp_path / ".search")
    frozen = runtime.freeze_spec(
        _pi_rpc_spec_with_budget(project, max_parallel=1),
        [project / "evaluator.py"],
    )
    run_id = runtime.create_run(frozen.frozen_spec_id)
    candidate_id = _planned_candidates(runtime, run_id, 1)[0]
    monkeypatch.setattr(pi_pool, "_launch_pool_job", lambda **_kwargs: os.getpid())
    opened = open_pi_search_pool(
        root_dir=runtime.root_dir,
        run_id=run_id,
        candidate_ids=[candidate_id],
        worker_budgets={
            candidate_id: {
                "min_runtime_seconds": 10,
                "min_verifier_runs": 3,
                "max_runtime_seconds": 20,
                "on_exceed": "interrupt",
            }
        },
    )
    submitted = opened["submitted"][0]
    now = [0.0]

    def fake_driver(**_request: Any) -> dict[str, Any]:
        now[0] += 1
        return {
            "ok": True,
            "run_id": run_id,
            "candidate_id": candidate_id,
            "agent_session_id": "agent_retired",
            "bound_session": {"counters": {"verifier_runs": 1}},
            "steps": [],
            "final_score_report": {
                "aggregate_score": 1.0,
                "process_passed": True,
                "allocation_decision": {"decision_id": "allocation_0001"},
            },
        }

    monkeypatch.setattr(pi_pool.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(pi_pool, "run_pi_search_candidate", fake_driver)

    assert run_pool_worker(
        root_dir=runtime.root_dir,
        pool_id=opened["pool_id"],
        job_id=submitted["job_id"],
    ) == 0
    waited = wait_any_pi_search_pool(
        root_dir=runtime.root_dir,
        pool_id=opened["pool_id"],
        timeout_seconds=0,
    )
    [event] = waited["events"]
    assert event["kind"] == "candidate_ready"
    assert event["result"]["lease"]["satisfied"] is False
    assert event["result"]["lease"]["release_reason"] == "allocation_decision"
    assert event["result"]["lease"]["allocation_decision_id"] == (
        "allocation_0001"
    )
    assert event["result"]["lease"]["dispatch_count"] == 1


def test_pi_pool_worker_continues_same_session_until_cumulative_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _make_project(tmp_path)
    runtime = FileSearchRuntime(tmp_path / ".search")
    frozen = runtime.freeze_spec(
        _pi_rpc_spec_with_budget(project, max_parallel=1),
        [project / "evaluator.py"],
    )
    run_id = runtime.create_run(frozen.frozen_spec_id)
    candidate_id = _planned_candidates(runtime, run_id, 1)[0]
    monkeypatch.setattr(pi_pool, "_launch_pool_job", lambda **_kwargs: os.getpid())
    opened = open_pi_search_pool(
        root_dir=runtime.root_dir,
        run_id=run_id,
        candidate_ids=[candidate_id],
        worker_budgets={
            candidate_id: {
                "min_runtime_seconds": 10,
                "min_verifier_runs": 2,
                "max_runtime_seconds": 20,
                "on_exceed": "interrupt",
            }
        },
    )
    submitted = opened["submitted"][0]
    now = [0.0]
    calls: list[dict[str, Any]] = []

    def fake_driver(**request: Any) -> dict[str, Any]:
        calls.append(request)
        now[0] += 4
        verifier_runs = 2 if len(calls) >= 2 else 1
        return {
            "ok": True,
            "run_id": run_id,
            "candidate_id": candidate_id,
            "agent_session_id": "agent_same",
            "bound_session": {"counters": {"verifier_runs": verifier_runs}},
            "steps": [],
            "final_score_report": {
                "aggregate_score": float(verifier_runs),
                "process_passed": True,
            },
        }

    monkeypatch.setattr(pi_pool.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(pi_pool, "run_pi_search_candidate", fake_driver)

    assert run_pool_worker(
        root_dir=runtime.root_dir,
        pool_id=opened["pool_id"],
        job_id=submitted["job_id"],
    ) == 0

    assert len(calls) == 3
    assert [call["redispatch"] for call in calls] == [False, True, True]
    assert [call["resume_agent_session_id"] for call in calls] == [
        None,
        "agent_same",
        "agent_same",
    ]
    assert [call["worker_budget"]["max_runtime_seconds"] for call in calls] == [
        20,
        16,
        12,
    ]
    assert [call["worker_budget"].get("min_runtime_seconds") for call in calls] == [
        10,
        6,
        2,
    ]
    assert [call["worker_budget"].get("min_verifier_runs") for call in calls] == [
        2,
        1,
        None,
    ]
    result = load_json(
        pi_pool._job_dir(runtime.root_dir, opened["pool_id"], submitted["job_id"])
        / "result.json"
    )
    assert result["lease"]["satisfied"] is True
    assert result["lease"]["dispatch_count"] == 3
    assert result["lease"]["elapsed_seconds"] == 12


def test_pi_pool_worker_refreshes_length_only_session_and_keeps_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _make_project(tmp_path)
    runtime = FileSearchRuntime(tmp_path / ".search")
    frozen = runtime.freeze_spec(
        _pi_rpc_spec_with_budget(project, max_parallel=1),
        [project / "evaluator.py"],
    )
    run_id = runtime.create_run(frozen.frozen_spec_id)
    candidate_id = _planned_candidates(runtime, run_id, 1)[0]
    monkeypatch.setattr(pi_pool, "_launch_pool_job", lambda **_kwargs: os.getpid())
    opened = open_pi_search_pool(
        root_dir=runtime.root_dir,
        run_id=run_id,
        candidate_ids=[candidate_id],
        worker_budgets={
            candidate_id: {
                "min_runtime_seconds": 8,
                "min_verifier_runs": 1,
                "max_runtime_seconds": 20,
                "on_exceed": "interrupt",
            }
        },
    )
    submitted = opened["submitted"][0]
    now = [0.0]
    calls: list[dict[str, Any]] = []

    def fake_driver(**request: Any) -> dict[str, Any]:
        calls.append(request)
        now[0] += 4
        if len(calls) == 1:
            return {
                "ok": True,
                "run_id": run_id,
                "candidate_id": candidate_id,
                "agent_session_id": "agent_old",
                "handle": {
                    "metadata": {
                        "refresh_required": True,
                        "refresh_reason": "length_without_tool_call",
                    }
                },
                "bound_session": {"counters": {"verifier_runs": 0}},
                "steps": [],
                "final_score_report": None,
            }
        return {
            "ok": True,
            "run_id": run_id,
            "candidate_id": candidate_id,
            "agent_session_id": "agent_fresh",
            "handle": {"metadata": {"refresh_required": False}},
            "bound_session": {"counters": {"verifier_runs": 1}},
            "steps": [],
            "final_score_report": {
                "aggregate_score": 1.0,
                "process_passed": True,
            },
        }

    monkeypatch.setattr(pi_pool.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(pi_pool, "run_pi_search_candidate", fake_driver)

    assert run_pool_worker(
        root_dir=runtime.root_dir,
        pool_id=opened["pool_id"],
        job_id=submitted["job_id"],
    ) == 0

    assert len(calls) == 2
    assert calls[0]["refresh_session"] is False
    assert calls[1]["refresh_session"] is True
    assert calls[1]["redispatch"] is False
    assert calls[1]["resume_agent_session_id"] is None
    result = load_json(
        pi_pool._job_dir(runtime.root_dir, opened["pool_id"], submitted["job_id"])
        / "result.json"
    )
    assert result["lease"]["satisfied"] is True
    assert result["lease"]["verifier_runs"] == 1
    assert result["lease"]["agent_session_ids"] == ["agent_old", "agent_fresh"]
    assert result["lease"]["session_refreshes"] == [
        {
            "dispatch": 2,
            "reason": "length_without_tool_call",
            "from_agent_session_id": "agent_old",
            "to_agent_session_id": "agent_fresh",
        }
    ]


def test_pi_pool_worker_backs_off_after_dispatch_without_new_verifier(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _make_project(tmp_path)
    runtime = FileSearchRuntime(tmp_path / ".search")
    frozen = runtime.freeze_spec(
        _pi_rpc_spec_with_budget(project, max_parallel=1),
        [project / "evaluator.py"],
    )
    run_id = runtime.create_run(frozen.frozen_spec_id)
    candidate_id = _planned_candidates(runtime, run_id, 1)[0]
    monkeypatch.setattr(pi_pool, "_launch_pool_job", lambda **_kwargs: os.getpid())
    opened = open_pi_search_pool(
        root_dir=runtime.root_dir,
        run_id=run_id,
        candidate_ids=[candidate_id],
        worker_budgets={
            candidate_id: {
                "min_runtime_seconds": 12,
                "min_verifier_runs": 1,
                "max_runtime_seconds": 30,
                "on_exceed": "interrupt",
            }
        },
    )
    now = [0.0]
    calls = 0
    sleeps: list[float] = []

    def fake_driver(**_request: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        now[0] += 1
        return {
            "ok": True,
            "run_id": run_id,
            "candidate_id": candidate_id,
            "agent_session_id": "agent_same",
            "bound_session": {
                "counters": {"verifier_runs": 1 if calls >= 3 else 0}
            },
            "steps": [],
            "final_score_report": {
                "aggregate_score": 1.0,
                "process_passed": True,
            },
        }

    def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now[0] += seconds

    monkeypatch.setattr(pi_pool.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(pi_pool.time, "sleep", fake_sleep)
    monkeypatch.setattr(pi_pool, "run_pi_search_candidate", fake_driver)

    assert run_pool_worker(
        root_dir=runtime.root_dir,
        pool_id=opened["pool_id"],
        job_id=opened["submitted"][0]["job_id"],
    ) == 0

    assert calls == 3
    assert sleeps == [5.0, 10.0]
    result = load_json(
        pi_pool._job_dir(
            runtime.root_dir,
            opened["pool_id"],
            opened["submitted"][0]["job_id"],
        )
        / "result.json"
    )
    assert result["lease"]["dispatch_count"] == 3
    assert result["lease"]["satisfied"] is True


def test_pi_pool_continuation_counts_only_new_job_verifier_runs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _make_project(tmp_path)
    runtime = FileSearchRuntime(tmp_path / ".search")
    frozen = runtime.freeze_spec(
        _pi_rpc_spec_with_budget(project, max_parallel=1),
        [project / "evaluator.py"],
    )
    run_id = runtime.create_run(frozen.frozen_spec_id)
    candidate_id = _planned_candidates(runtime, run_id, 1)[0]
    session = runtime.start_agent_session(run_id, candidate_id)
    runtime._write_agent_session(
        session.model_copy(update={"counters": {"verifier_runs": 3}})
    )
    opened = open_pi_search_pool(root_dir=runtime.root_dir, run_id=run_id)
    monkeypatch.setattr(pi_pool, "_launch_pool_job", lambda **_kwargs: os.getpid())
    submitted = continue_pi_search_pool(
        root_dir=runtime.root_dir,
        pool_id=opened["pool_id"],
        candidate_id=candidate_id,
        worker_budget={
            "min_verifier_runs": 2,
            "max_runtime_seconds": 10,
            "on_exceed": "interrupt",
        },
    )
    now = [0.0]
    calls = 0

    def fake_driver(**_request: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        now[0] += 1
        return {
            "ok": True,
            "run_id": run_id,
            "candidate_id": candidate_id,
            "agent_session_id": session.agent_session_id,
            "bound_session": {"counters": {"verifier_runs": 3 + calls}},
            "steps": [],
            "final_score_report": {
                "aggregate_score": float(calls),
                "process_passed": True,
            },
        }

    monkeypatch.setattr(pi_pool.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(pi_pool, "run_pi_search_candidate", fake_driver)

    assert run_pool_worker(
        root_dir=runtime.root_dir,
        pool_id=opened["pool_id"],
        job_id=submitted["job_id"],
    ) == 0
    assert calls == 2
    result = load_json(
        pi_pool._job_dir(runtime.root_dir, opened["pool_id"], submitted["job_id"])
        / "result.json"
    )
    assert result["lease"]["verifier_runs"] == 2


def test_pi_pool_wait_any_rejects_completed_job_with_unsatisfied_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _make_project(tmp_path)
    runtime = FileSearchRuntime(tmp_path / ".search")
    frozen = runtime.freeze_spec(
        _pi_rpc_spec_with_budget(project, max_parallel=1),
        [project / "evaluator.py"],
    )
    run_id = runtime.create_run(frozen.frozen_spec_id)
    candidate_id = _planned_candidates(runtime, run_id, 1)[0]
    monkeypatch.setattr(pi_pool, "_launch_pool_job", lambda **_kwargs: os.getpid())
    opened = open_pi_search_pool(
        root_dir=runtime.root_dir,
        run_id=run_id,
        candidate_ids=[candidate_id],
        worker_budgets={
            candidate_id: {
                "min_runtime_seconds": 5,
                "min_verifier_runs": 1,
                "max_runtime_seconds": 10,
                "on_exceed": "interrupt",
            }
        },
    )
    job_id = opened["submitted"][0]["job_id"]
    with exclusive_file_lock(
        pi_pool._pool_lock_path(runtime.root_dir, opened["pool_id"])
    ):
        write_json(
            pi_pool._job_dir(runtime.root_dir, opened["pool_id"], job_id)
            / "result.json",
            {"ok": True, "lease": {"satisfied": False}},
        )
        job = pi_pool._load_job(runtime.root_dir, opened["pool_id"], job_id)
        job.update({"status": "completed", "finished_at": utc_timestamp()})
        pi_pool._write_job(runtime.root_dir, opened["pool_id"], job)

    waited = wait_any_pi_search_pool(
        root_dir=runtime.root_dir,
        pool_id=opened["pool_id"],
        timeout_seconds=0,
    )

    assert waited["events"][0]["kind"] == "timed_out"


def test_pi_pool_worker_does_not_continue_driver_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _make_project(tmp_path)
    runtime = FileSearchRuntime(tmp_path / ".search")
    frozen = runtime.freeze_spec(
        _pi_rpc_spec_with_budget(project, max_parallel=1),
        [project / "evaluator.py"],
    )
    run_id = runtime.create_run(frozen.frozen_spec_id)
    candidate_id = _planned_candidates(runtime, run_id, 1)[0]
    monkeypatch.setattr(pi_pool, "_launch_pool_job", lambda **_kwargs: os.getpid())
    opened = open_pi_search_pool(
        root_dir=runtime.root_dir,
        run_id=run_id,
        candidate_ids=[candidate_id],
        worker_budgets={
            candidate_id: {
                "min_runtime_seconds": 10,
                "min_verifier_runs": 1,
                "max_runtime_seconds": 20,
                "on_exceed": "interrupt",
            }
        },
    )
    calls = 0

    def failed_driver(**_request: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return {
            "ok": False,
            "candidate_id": candidate_id,
            "agent_session_id": "agent_failed",
            "failure": {
                "stage": "worker_runner",
                "error_type": "RuntimeError",
                "message": "provider failed",
            },
            "error": "provider failed",
        }

    monkeypatch.setattr(pi_pool, "run_pi_search_candidate", failed_driver)

    assert run_pool_worker(
        root_dir=runtime.root_dir,
        pool_id=opened["pool_id"],
        job_id=opened["submitted"][0]["job_id"],
    ) == 1
    assert calls == 1
    waited = wait_any_pi_search_pool(
        root_dir=runtime.root_dir,
        pool_id=opened["pool_id"],
        timeout_seconds=0,
    )
    assert waited["events"][0]["kind"] == "failed"


def test_pi_pool_lease_reports_hard_limit_when_minimum_is_also_satisfied(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _make_project(tmp_path)
    runtime = FileSearchRuntime(tmp_path / ".search")
    frozen = runtime.freeze_spec(
        _pi_rpc_spec_with_budget(project, max_parallel=1),
        [project / "evaluator.py"],
    )
    run_id = runtime.create_run(frozen.frozen_spec_id)
    candidate_id = _planned_candidates(runtime, run_id, 1)[0]
    monkeypatch.setattr(pi_pool, "_launch_pool_job", lambda **_kwargs: os.getpid())
    opened = open_pi_search_pool(
        root_dir=runtime.root_dir,
        run_id=run_id,
        candidate_ids=[candidate_id],
        worker_budgets={
            candidate_id: {
                "min_runtime_seconds": 5,
                "min_verifier_runs": 1,
                "max_runtime_seconds": 10,
                "on_exceed": "interrupt",
            }
        },
    )
    now = [0.0]

    def fake_driver(**_request: Any) -> dict[str, Any]:
        now[0] = 10.1
        return {
            "ok": True,
            "run_id": run_id,
            "candidate_id": candidate_id,
            "agent_session_id": "agent_same",
            "bound_session": {"counters": {"verifier_runs": 1}},
            "steps": [],
            "final_score_report": {
                "aggregate_score": 1.0,
                "process_passed": True,
            },
        }

    monkeypatch.setattr(pi_pool.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(pi_pool, "run_pi_search_candidate", fake_driver)

    assert run_pool_worker(
        root_dir=runtime.root_dir,
        pool_id=opened["pool_id"],
        job_id=opened["submitted"][0]["job_id"],
    ) == 0
    result = load_json(
        pi_pool._job_dir(
            runtime.root_dir,
            opened["pool_id"],
            opened["submitted"][0]["job_id"],
        )
        / "result.json"
    )
    assert result["lease"]["satisfied"] is True
    assert result["lease"]["release_reason"] == "max_runtime_reached"


def test_pi_pool_lease_without_required_verifier_times_out_instead_of_ready(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _make_project(tmp_path)
    runtime = FileSearchRuntime(tmp_path / ".search")
    frozen = runtime.freeze_spec(
        _pi_rpc_spec_with_budget(project, max_parallel=1),
        [project / "evaluator.py"],
    )
    run_id = runtime.create_run(frozen.frozen_spec_id)
    candidate_id = _planned_candidates(runtime, run_id, 1)[0]
    monkeypatch.setattr(pi_pool, "_launch_pool_job", lambda **_kwargs: os.getpid())
    opened = open_pi_search_pool(
        root_dir=runtime.root_dir,
        run_id=run_id,
        candidate_ids=[candidate_id],
        worker_budgets={
            candidate_id: {
                "min_runtime_seconds": 5,
                "min_verifier_runs": 1,
                "max_runtime_seconds": 10,
                "on_exceed": "interrupt",
            }
        },
    )
    now = [0.0]

    def fake_driver(**_request: Any) -> dict[str, Any]:
        now[0] = 10.1
        return {
            "ok": True,
            "run_id": run_id,
            "candidate_id": candidate_id,
            "agent_session_id": "agent_same",
            "bound_session": {"counters": {"verifier_runs": 0}},
            "steps": [],
            "final_score_report": {
                "aggregate_score": 1.0,
                "process_passed": True,
            },
        }

    monkeypatch.setattr(pi_pool.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(pi_pool, "run_pi_search_candidate", fake_driver)

    assert run_pool_worker(
        root_dir=runtime.root_dir,
        pool_id=opened["pool_id"],
        job_id=opened["submitted"][0]["job_id"],
    ) == 0
    waited = wait_any_pi_search_pool(
        root_dir=runtime.root_dir,
        pool_id=opened["pool_id"],
        timeout_seconds=0,
    )
    assert waited["events"][0]["kind"] == "timed_out"
    assert waited["events"][0]["result"]["lease"]["satisfied"] is False


def test_pi_pool_lease_reserves_outer_closeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _make_project(tmp_path)
    runtime = FileSearchRuntime(tmp_path / ".search")
    spec_data = _pi_rpc_spec_with_budget(
        project, max_parallel=1
    ).model_dump(mode="json")
    spec_data["strategy"]["config"] = {"closeout_reserve_seconds": 30}
    frozen = runtime.freeze_spec(
        SearchSpec.model_validate(spec_data),
        [project / "evaluator.py"],
    )
    run_id = runtime.create_run(frozen.frozen_spec_id)
    monkeypatch.setenv(
        "GOAL_PLUS_OUTER_DEADLINE_AT", "1970-01-01T00:18:50+00:00"
    )
    monkeypatch.setattr(pi_pool.time, "time", lambda: 1000.0)

    effective = pi_pool._lease_max_runtime_seconds(
        runtime.root_dir,
        run_id,
        {"max_runtime_seconds": 200},
    )

    assert effective == 100


def test_pi_pool_minimum_lease_fits_effective_outer_budget() -> None:
    effective = pi_pool._lease_min_runtime_seconds(
        {"min_runtime_seconds": 6900},
        5003,
    )

    assert effective == 4958
