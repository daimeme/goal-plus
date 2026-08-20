"""Host hook backstop for Goal Plus adapters."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from goal_plus.paths import DEFAULT_RUNTIME_ROOT, LEGACY_RUNTIME_ROOT
from goal_plus.time_advisory import (
    build_search_time_advisory,
    find_agent_session,
    is_search_candidate_session,
)


GOAL_ID_RE = re.compile(r"\bgp_\d+\b")
GOAL_PLUS_PROMPT_RE = re.compile(
    r"^\s*(?:/|\$)(?P<command>goal-plus(?:-with-final-check)?)"
    r"(?:\s+(?P<body>.*\S))?\s*$",
    re.IGNORECASE | re.DOTALL,
)
DISABLE_VALUES = {"1", "true", "yes", "on"}
STOP_HOOK_EVENT_SCHEMA_VERSION = 1
STOP_HOOK_TEXT_LIMIT = 1024


def _read_hook_input() -> dict[str, Any]:
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _hook_disabled() -> bool:
    return any(
        os.environ.get(name, "").lower() in DISABLE_VALUES
        for name in ("GOAL_PLUS_STOP_HOOK_DISABLED", "GOAL_PLUS_HOST_HOOK_DISABLED")
    )


def _find_session_root() -> Path:
    override = os.environ.get("GOAL_PLUS_PROJECT_ROOT")
    if override:
        return Path(override).expanduser().resolve()

    claude_project_dir = os.environ.get("CLAUDE_PROJECT_DIR")
    if claude_project_dir:
        return Path(claude_project_dir).expanduser().resolve()

    cwd = Path.cwd().resolve()
    for candidate in (cwd, *cwd.parents):
        if (
            (candidate / DEFAULT_RUNTIME_ROOT).exists()
            or (candidate / LEGACY_RUNTIME_ROOT).exists()
            or (candidate / ".git").exists()
        ):
            return candidate
    return cwd


def _search_root(session_root: Path) -> Path:
    override = os.environ.get("GOAL_PLUS_SEARCH_ROOT")
    if override:
        root = Path(override).expanduser()
        return root.resolve() if root.is_absolute() else (session_root / root).resolve()
    default_root = (session_root / DEFAULT_RUNTIME_ROOT).resolve()
    if default_root.exists():
        return default_root
    legacy_root = (session_root / LEGACY_RUNTIME_ROOT).resolve()
    if legacy_root.exists():
        return legacy_root
    return default_root


def _first_goal_id(value: Any) -> str | None:
    if isinstance(value, str):
        match = GOAL_ID_RE.search(value)
        return match.group(0) if match else None
    if isinstance(value, dict):
        for key in ("goal_plus_id", "goalPlusId", "goal_id", "goalId"):
            found = _first_goal_id(value.get(key))
            if found:
                return found
        for item in value.values():
            found = _first_goal_id(item)
            if found:
                return found
    if isinstance(value, list):
        for item in value:
            found = _first_goal_id(item)
            if found:
                return found
    return None


def _explicit_goal_ids(hook_input: dict[str, Any]) -> list[str]:
    explicit = os.environ.get("GOAL_PLUS_ID")
    return [goal_id for goal_id in (explicit, _first_goal_id(hook_input)) if goal_id]


def _hook_event_name(hook_input: dict[str, Any]) -> str:
    for key in ("hook_event_name", "hookEventName", "event_name", "eventName", "event"):
        value = hook_input.get(key)
        if isinstance(value, str) and value:
            return value
    return "Stop"


def _tool_name(hook_input: dict[str, Any]) -> str:
    for key in ("tool_name", "toolName", "name"):
        value = hook_input.get(key)
        if isinstance(value, str):
            return value
    return ""


def _prompt(hook_input: dict[str, Any]) -> str:
    value = hook_input.get("prompt")
    return value if isinstance(value, str) else ""


def _goal_plus_prompt(hook_input: dict[str, Any]) -> str | None:
    request = _goal_plus_request(hook_input)
    if request is None:
        return None
    raw_goal = request.get("raw_goal")
    return raw_goal if isinstance(raw_goal, str) and raw_goal else None


def _goal_plus_request(hook_input: dict[str, Any]) -> dict[str, Any] | None:
    match = GOAL_PLUS_PROMPT_RE.match(_prompt(hook_input))
    if match is None:
        return None
    command = match.group("command").lower()
    body = (match.group("body") or "").strip()
    if command == "goal-plus" and body.lower() == "resume":
        return {"action": "resume", "raw_goal": None}
    if command == "goal-plus" and body.lower().startswith("edit "):
        raw_goal = body[5:].strip()
        return {"action": "edit", "raw_goal": raw_goal or None}
    return {
        "action": "start",
        "raw_goal": body or None,
        "final_check": command == "goal-plus-with-final-check",
    }


def _is_goal_plus_create_tool(tool_name: str) -> bool:
    normalized = tool_name.replace("-", "_")
    return normalized.endswith("goal_plus_create")


def _session_id(hook_input: dict[str, Any]) -> str | None:
    for key in ("session_id", "sessionId"):
        value = hook_input.get(key)
        if isinstance(value, str) and value:
            return value
    session = hook_input.get("session")
    if isinstance(session, dict):
        for key in ("id", "session_id", "sessionId"):
            value = session.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def _stop_reason(hook_input: dict[str, Any]) -> str | None:
    for key in ("stop_reason", "stopReason"):
        value = hook_input.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _host_agent_id(hook_input: dict[str, Any]) -> str | None:
    for key in ("agent_id", "agentId"):
        value = hook_input.get(key)
        if isinstance(value, str) and value:
            return value
    target = hook_input.get("target")
    if isinstance(target, dict):
        for key in ("agent_id", "agentId", "agent"):
            value = target.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def _transcript_path(hook_input: dict[str, Any]) -> str | None:
    for key in ("transcript_path", "transcriptPath"):
        value = hook_input.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _tool_use_id(hook_input: dict[str, Any]) -> str | None:
    for key in ("tool_use_id", "toolUseId"):
        value = hook_input.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _tool_input(hook_input: dict[str, Any]) -> dict[str, Any]:
    value = _raw_tool_input(hook_input)
    return value if isinstance(value, dict) else {}


def _raw_tool_input(hook_input: dict[str, Any]) -> Any:
    for key in ("tool_input", "toolInput", "input"):
        value = hook_input.get(key)
        if isinstance(value, (dict, list, str)):
            return value
    return {}


def _runtime_agent_session_ids(value: Any) -> list[str]:
    """Extract candidate-session-looking IDs from JSON or Code Mode source."""
    found: list[str] = []
    if isinstance(value, dict):
        direct = value.get("agent_session_id")
        if isinstance(direct, str) and direct:
            found.append(direct)
        for item in value.values():
            found.extend(_runtime_agent_session_ids(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(_runtime_agent_session_ids(item))
    elif isinstance(value, str):
        found.extend(
            re.findall(r"\bagent_[A-Za-z0-9][A-Za-z0-9_.:-]*\b", value)
        )
    return list(dict.fromkeys(found))


def _is_subagent_context(hook_input: dict[str, Any]) -> bool:
    if _hook_event_name(hook_input) == "SubagentStop":
        return True
    for key in (
        "agent_id",
        "agentId",
        "agent_type",
        "agentType",
        "agent_transcript_path",
        "agentTranscriptPath",
    ):
        if hook_input.get(key):
            return True
    target = hook_input.get("target")
    if isinstance(target, dict):
        target_type = target.get("type") or target.get("kind")
        if target_type in {"agent", "subagent"}:
            return True
        if target.get("agent") or target.get("agent_id") or target.get("agentId"):
            return True
    return False


def _is_final_checker_context(hook_input: dict[str, Any]) -> bool:
    for key in ("agent_type", "agentType", "task_name", "taskName", "role"):
        value = hook_input.get(key)
        if isinstance(value, str) and (
            value == "goal_plus_final_checker"
            or value == "final-checker"
            or value.startswith("goal_plus_final_check_")
        ):
            return True
    return False


def _subagent_identity(hook_input: dict[str, Any]) -> str | None:
    for key in (
        "agent_id",
        "agentId",
        "agent_transcript_path",
        "agentTranscriptPath",
    ):
        value = hook_input.get(key)
        if isinstance(value, str) and value:
            return value
    target = hook_input.get("target")
    if isinstance(target, dict):
        for key in ("agent_id", "agentId", "agent", "transcript_path"):
            value = target.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def _agent_transcript_path(hook_input: dict[str, Any]) -> Path | None:
    value = hook_input.get("agent_transcript_path") or hook_input.get(
        "agentTranscriptPath"
    )
    if not isinstance(value, str) or not value:
        return None
    path = Path(value).expanduser()
    return path if path.is_file() else None


def _agent_transcript_started_at(hook_input: dict[str, Any]) -> datetime | None:
    path = _agent_transcript_path(hook_input)
    if path is None:
        return None
    try:
        with path.open(encoding="utf-8") as handle:
            for _ in range(8):
                line = handle.readline()
                if not line:
                    break
                payload = json.loads(line)
                timestamp = payload.get("timestamp")
                if isinstance(timestamp, str):
                    parsed = _utc_datetime(timestamp)
                    if parsed is not None:
                        return parsed
    except (OSError, json.JSONDecodeError):
        return None
    return None


def _candidate_session_from_transcript(
    search_root: Path,
    hook_input: dict[str, Any],
) -> Any | None:
    path = _agent_transcript_path(hook_input)
    if path is None:
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    for candidate_session_id in _runtime_agent_session_ids(text):
        session = find_agent_session(search_root, candidate_session_id)
        if (
            session is not None
            and session.host == "codex"
            and is_search_candidate_session(session)
        ):
            return session
    return None


def _candidate_session_from_native_identity(
    search_root: Path,
    hook_input: dict[str, Any],
) -> Any | None:
    identity = _subagent_identity(hook_input)
    hook_transcript = _agent_transcript_path(hook_input)
    if identity is None and hook_transcript is None:
        return None

    from goal_plus.host_observability import discover_codex_session_file

    for path in sorted((search_root / "runs").glob("*/agent_sessions/*.json")):
        session = find_agent_session(search_root, path.stem)
        if (
            session is None
            or session.host != "codex"
            or not is_search_candidate_session(session)
        ):
            continue
        native_path = discover_codex_session_file(session)
        if native_path is None:
            continue
        if hook_transcript is not None:
            try:
                if native_path.resolve() == hook_transcript.resolve():
                    return session
            except OSError:
                pass
        if identity in {
            session.host_handle.external_id,
            session.host_handle.task_name,
            session.launch.get("task_name"),
        }:
            return session
        try:
            with native_path.open(encoding="utf-8") as handle:
                for _ in range(8):
                    line = handle.readline()
                    if not line:
                        break
                    payload = json.loads(line)
                    if payload.get("type") != "session_meta":
                        continue
                    meta = payload.get("payload")
                    if isinstance(meta, dict) and identity in {
                        meta.get("id"),
                        meta.get("session_id"),
                        meta.get("agent_path"),
                    }:
                        return session
        except (OSError, json.JSONDecodeError):
            continue
    return None


def _time_advisory_dir(search_root: Path) -> Path:
    return search_root / "host-logs" / "codex-time-advisory"


def _identity_path(search_root: Path, identity: str) -> Path:
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return _time_advisory_dir(search_root) / "workers" / f"{digest}.json"


def _read_json_object(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _write_json_object(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp-{os.getpid()}")
    temporary.write_text(
        json.dumps(payload, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _autoresearch_lease_dir(search_root: Path) -> Path:
    return search_root / "host-logs" / "codex-autoresearch-leases"


def _autoresearch_lease_path(search_root: Path, agent_session_id: str) -> Path:
    return _autoresearch_lease_dir(search_root) / f"{agent_session_id}.json"


def _autoresearch_lease_policy(session: Any) -> dict[str, Any] | None:
    launch = session.launch if isinstance(session.launch, dict) else {}
    budget_control = launch.get("budget_control")
    if not isinstance(budget_control, dict):
        return None
    lease = budget_control.get("autoresearch_lease")
    if not isinstance(lease, dict) or lease.get("mode") != "subagent_stop":
        return None
    min_runtime_seconds = lease.get("min_runtime_seconds", 0)
    min_verifier_runs = lease.get("min_verifier_runs", 1)
    if not isinstance(min_runtime_seconds, int) or min_runtime_seconds < 0:
        return None
    if not isinstance(min_verifier_runs, int) or min_verifier_runs <= 0:
        return None
    return {
        "min_runtime_seconds": min_runtime_seconds,
        "min_verifier_runs": min_verifier_runs,
        "max_runtime_seconds": budget_control.get("max_runtime_seconds"),
        "soft_closeout_seconds": budget_control.get("soft_closeout_seconds"),
        "initial_wait_timeout_ms": budget_control.get("initial_wait_timeout_ms"),
    }


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_text(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _bounded_hook_text(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    if len(value) <= STOP_HOOK_TEXT_LIMIT:
        return value
    return value[: STOP_HOOK_TEXT_LIMIT - 3] + "..."


def _stop_hook_event_dir(search_root: Path) -> Path:
    return search_root / "host-logs" / "codex-hook-events"


def _record_stop_hook_event(
    search_root: Path,
    hook_input: dict[str, Any],
    *,
    event_name: str,
    invocation_id: str,
    started_at: datetime,
    started_monotonic: float,
    result: dict[str, Any] | None = None,
    error: Exception | None = None,
) -> None:
    """Best-effort, content-minimal evidence for one automatic stop hook."""
    try:
        finished_at = _utc_now()
        details = result or {}
        goal_plus_id = details.get("goal_plus_id")
        if not isinstance(goal_plus_id, str):
            goal_plus_id = _first_goal_id(hook_input) or _first_goal_id(
                os.environ.get("GOAL_PLUS_ID")
            )
        payload = {
            "schema_version": STOP_HOOK_EVENT_SCHEMA_VERSION,
            "invocation_id": invocation_id,
            "hook_event_name": event_name,
            "started_at": _utc_text(started_at),
            "finished_at": _utc_text(finished_at),
            "duration_ms": round(
                max(0.0, time.monotonic() - started_monotonic) * 1000.0,
                3,
            ),
            "outcome": "failed" if error is not None else "completed",
            "decision": "error" if error is not None else details.get("decision"),
            "reason": _bounded_hook_text(details.get("reason")),
            "goal_plus_id": goal_plus_id,
            "session_id": _session_id(hook_input),
            "host_agent_id": _host_agent_id(hook_input),
            "agent_session_id": details.get("agent_session_id"),
            "run_id": details.get("run_id"),
            "candidate_id": details.get("candidate_id"),
            "stop_reason": _bounded_hook_text(_stop_reason(hook_input)),
            "error_type": type(error).__name__ if error is not None else None,
            "error": _bounded_hook_text(str(error)) if error is not None else None,
        }
        _write_json_object(
            _stop_hook_event_dir(search_root) / f"{invocation_id}.json",
            payload,
        )
    except Exception:
        # Statistics must never alter the host hook's fail-open contract.
        return


def _utc_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _ensure_autoresearch_lease(
    search_root: Path,
    session: Any,
    hook_input: dict[str, Any],
) -> dict[str, Any] | None:
    policy = _autoresearch_lease_policy(session)
    if policy is None:
        return None
    path = _autoresearch_lease_path(search_root, session.agent_session_id)
    existing = _read_json_object(path)
    transcript_started_at = _agent_transcript_started_at(hook_input)
    if transcript_started_at is None:
        from goal_plus.host_observability import discover_codex_session_file

        native_path = discover_codex_session_file(session)
        if native_path is not None:
            transcript_started_at = _agent_transcript_started_at(
                {"agent_transcript_path": str(native_path)}
            )
    if existing is not None:
        existing_started_at = _utc_datetime(existing.get("started_at"))
        if transcript_started_at is not None and (
            existing_started_at is None
            or transcript_started_at < existing_started_at
        ):
            min_runtime_seconds = int(existing.get("min_runtime_seconds") or 0)
            existing = {
                **existing,
                "started_at": _utc_text(transcript_started_at),
                "start_event": "SubagentStartTranscript",
                "lease_deadline_at": _utc_text(
                    transcript_started_at
                    + timedelta(seconds=min_runtime_seconds)
                ),
            }
            _write_json_object(path, existing)
        return existing

    now = transcript_started_at or _utc_now()
    initial_wait_ms = policy.get("initial_wait_timeout_ms")
    parent_closeout_after_seconds = (
        int(initial_wait_ms) / 1000
        if isinstance(initial_wait_ms, int)
        else None
    )
    max_runtime_seconds = policy.get("max_runtime_seconds")
    lease_precedes_parent_closeout = (
        policy["min_runtime_seconds"] < parent_closeout_after_seconds
        if parent_closeout_after_seconds is not None
        else None
    )
    lease_precedes_parent_hard_deadline = (
        policy["min_runtime_seconds"] < max_runtime_seconds
        if isinstance(max_runtime_seconds, int)
        else None
    )
    payload = {
        "agent_session_id": session.agent_session_id,
        "run_id": session.run_id,
        "candidate_id": session.candidate_id,
        "status": "active",
        "started_at": _utc_text(now),
        "lease_deadline_at": _utc_text(
            now + timedelta(seconds=policy["min_runtime_seconds"])
        ),
        "start_event": (
            "SubagentStartTranscript"
            if transcript_started_at is not None
            else _hook_event_name(hook_input)
        ),
        "start_tool": _tool_name(hook_input) or None,
        "min_runtime_seconds": policy["min_runtime_seconds"],
        "min_verifier_runs": policy["min_verifier_runs"],
        "max_runtime_seconds": policy["max_runtime_seconds"],
        "soft_closeout_seconds": policy["soft_closeout_seconds"],
        "parent_closeout_after_seconds": parent_closeout_after_seconds,
        "lease_precedes_parent_closeout": lease_precedes_parent_closeout,
        "lease_precedes_parent_hard_deadline": (
            lease_precedes_parent_hard_deadline
        ),
        "stop_attempts": 0,
        "blocked_stop_attempts": 0,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return _read_json_object(path)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, sort_keys=True, ensure_ascii=False)
        handle.write("\n")
    return payload


def _candidate_requires_immediate_stop(search_root: Path, session: Any) -> bool:
    payload = _read_json_object(
        search_root
        / "runs"
        / session.run_id
        / "candidates"
        / session.candidate_id
        / "candidate.json"
    )
    iterations = payload.get("iterations") if payload else None
    if not isinstance(iterations, list):
        return False
    for iteration in reversed(iterations):
        if not isinstance(iteration, dict):
            continue
        owner = iteration.get("agent_session_id")
        if owner not in {None, session.agent_session_id}:
            continue
        if iteration.get("failure_class") == "VerifierWorkspaceSideEffect":
            return True
        metrics = iteration.get("metrics")
        if isinstance(metrics, dict) and (
            metrics.get("infrastructure_failure") is True
            or metrics.get("candidate_action") == "stop_and_report"
        ):
            return True
        return False
    return False


def _autoresearch_lease_stop_context(
    search_root: Path,
    session: Any,
    *,
    verifier_runs: int,
) -> dict[str, Any] | None:
    evidence = _ensure_autoresearch_lease(search_root, session, {})
    if evidence is None:
        return None
    started_at = _utc_datetime(evidence.get("started_at"))
    if started_at is None:
        return None

    now = _utc_now()
    elapsed_seconds = max(0.0, (now - started_at).total_seconds())
    min_runtime_seconds = int(evidence.get("min_runtime_seconds") or 0)
    min_verifier_runs = int(evidence.get("min_verifier_runs") or 1)
    remaining_seconds = max(0.0, min_runtime_seconds - elapsed_seconds)
    infrastructure_stop = _candidate_requires_immediate_stop(search_root, session)
    candidate_path = (
        search_root
        / "runs"
        / session.run_id
        / "candidates"
        / session.candidate_id
        / "candidate.json"
    )
    allocation_stop = False
    try:
        candidate_payload = json.loads(candidate_path.read_text(encoding="utf-8"))
        allocation_stop = (
            isinstance(candidate_payload, dict)
            and candidate_payload.get("allocation_eligibility") == "retired"
            and bool(candidate_payload.get("retired_by_decision_id"))
        )
    except (OSError, UnicodeError, json.JSONDecodeError):
        pass
    verifier_complete = verifier_runs >= min_verifier_runs
    runtime_complete = remaining_seconds <= 0
    completion_complete = infrastructure_stop or allocation_stop or (
        verifier_complete and runtime_complete
    )

    stop_attempts = int(evidence.get("stop_attempts") or 0) + 1
    blocked_stop_attempts = int(evidence.get("blocked_stop_attempts") or 0)
    if not completion_complete:
        blocked_stop_attempts += 1

    updated = {
        **evidence,
        "status": "released" if completion_complete else "active",
        "stop_attempts": stop_attempts,
        "blocked_stop_attempts": blocked_stop_attempts,
        "last_stop_attempt_at": _utc_text(now),
        "elapsed_seconds": round(elapsed_seconds, 3),
        "remaining_seconds": round(remaining_seconds, 3),
        "verifier_runs": verifier_runs,
    }
    if evidence.get("first_stop_attempt_at") is None:
        updated["first_stop_attempt_at"] = _utc_text(now)
    if completion_complete:
        parent_closeout_after_seconds = evidence.get(
            "parent_closeout_after_seconds"
        )
        max_runtime_seconds = evidence.get("max_runtime_seconds")
        updated.update(
            {
                "released_at": _utc_text(now),
                "released_within_parent_closeout_budget": (
                    elapsed_seconds < parent_closeout_after_seconds
                    if isinstance(parent_closeout_after_seconds, (int, float))
                    else None
                ),
                "released_within_max_runtime": (
                    elapsed_seconds < max_runtime_seconds
                    if isinstance(max_runtime_seconds, int)
                    else None
                ),
                "release_reason": (
                    "infrastructure_stop_and_report"
                    if infrastructure_stop
                    else "allocation_decision"
                    if allocation_stop
                    else "lease_satisfied"
                ),
            }
        )
    _write_json_object(
        _autoresearch_lease_path(search_root, session.agent_session_id),
        updated,
    )

    reason: str | None = None
    if not completion_complete:
        requirements = []
        if not runtime_complete:
            requirements.append(
                f"至少再继续 {remaining_seconds:.1f} 秒"
            )
        if not verifier_complete:
            requirements.append(
                f"再完成 {min_verifier_runs - verifier_runs} 次 "
                "search_run_verifier iteration"
            )
        reason = (
            f"Search 候选 {session.agent_session_id} 的 AutoResearch lease 仍处于 active："
            f"{'，并且'.join(requirements)}。不要返回父级。先使用此 agent_session_id 调用 "
            "search_get_agent_context 刷新 candidate-local 证据，并继续遵循 Global Evidence 的"
            "定期刷新节奏；到刷新节点时调用 search_get_global_evidence。选择不同且有证据支持的"
            "假设并实现修改，然后使用同一 "
            "agent_session_id 运行 search_run_verifier，省略 scope 以使用 process verifier，"
            "并用一句话 hypothesis 客观概括本轮实际尝试。"
            "将结果与当前最佳项比较并继续深度搜索。不要因为完成一次 iteration、暂时没有"
            "改进或一次失败就尝试结束，也不要通过重复返回来轮询此 hook。只要 lease 仍"
            "处于 active，就必须把局部饱和视为转向信号，重新评估瓶颈并选择与已有尝试"
            "不同的实质方向；只有收到父级 closeout/deadline 后才准备收尾。不要自行估算时间，"
            "不要休眠或忙等。父级 watchdog 在该 lease 之后另有 closeout 窗口。"
        )

    return {
        "search_candidate_completion_complete": completion_complete,
        "search_candidate_completion_reason": reason,
        "search_candidate_min_runtime_seconds": min_runtime_seconds,
        "search_candidate_elapsed_seconds": elapsed_seconds,
        "search_candidate_remaining_seconds": remaining_seconds,
        "search_candidate_required_verifier_runs": min_verifier_runs,
        "search_candidate_infrastructure_stop": infrastructure_stop,
        "search_candidate_allocation_stop": allocation_stop,
    }


def _search_candidate_agent_session_id(
    search_root: Path,
    hook_input: dict[str, Any],
) -> str | None:
    if not _is_subagent_context(hook_input) or _is_final_checker_context(hook_input):
        return None
    identity = _subagent_identity(hook_input)
    direct: str | None = None
    for candidate_session_id in _runtime_agent_session_ids(
        _raw_tool_input(hook_input)
    ):
        session = find_agent_session(search_root, candidate_session_id)
        if (
            session is not None
            and session.host == "codex"
            and is_search_candidate_session(session)
        ):
            direct = candidate_session_id
            break
    if direct is not None:
        if identity is not None:
            _write_json_object(
                _identity_path(search_root, identity),
                {
                    "agent_session_id": direct,
                    "mapped_at": datetime.now(timezone.utc)
                    .replace(microsecond=0)
                    .isoformat()
                    .replace("+00:00", "Z"),
                },
            )
        direct_session = find_agent_session(search_root, direct)
        if direct_session is not None:
            _ensure_autoresearch_lease(
                search_root,
                direct_session,
                hook_input,
            )
        return direct
    transcript_session = _candidate_session_from_transcript(
        search_root,
        hook_input,
    ) or _candidate_session_from_native_identity(search_root, hook_input)
    if transcript_session is not None:
        if identity is not None:
            _write_json_object(
                _identity_path(search_root, identity),
                {
                    "agent_session_id": transcript_session.agent_session_id,
                    "mapped_at": _utc_text(_utc_now()),
                    "source": "agent_transcript_path",
                },
            )
        _ensure_autoresearch_lease(
            search_root,
            transcript_session,
            hook_input,
        )
        return transcript_session.agent_session_id
    if identity is None:
        return None
    mapping = _read_json_object(_identity_path(search_root, identity))
    mapped = mapping.get("agent_session_id") if mapping else None
    if not isinstance(mapped, str) or not mapped:
        return None
    session = find_agent_session(search_root, mapped)
    if (
        session is None
        or session.host != "codex"
        or not is_search_candidate_session(session)
    ):
        return None
    _ensure_autoresearch_lease(search_root, session, hook_input)
    return mapped


def _declares_search_candidate(hook_input: dict[str, Any]) -> bool:
    for key in ("agent_type", "agentType", "task_name", "taskName", "role"):
        value = hook_input.get(key)
        if not isinstance(value, str):
            continue
        normalized = value.strip().lower().replace("-", "_")
        if normalized.startswith("search_candidate"):
            return True
    return False


def _search_candidate_stop_context(
    search_root: Path,
    hook_input: dict[str, Any],
) -> dict[str, Any] | None:
    """Resolve candidate-owned completion without inheriting parent actions."""
    agent_session_id = _search_candidate_agent_session_id(search_root, hook_input)
    if agent_session_id is None:
        if not _declares_search_candidate(hook_input):
            return None
        return {
            "goal_plus_subagent_role": "search_candidate",
            "search_candidate_verifier_complete": False,
        }
    session = find_agent_session(search_root, agent_session_id)
    if session is None:
        return None
    verifier_runs = session.counters.get("verifier_runs", 0)
    lease_context = _autoresearch_lease_stop_context(
        search_root,
        session,
        verifier_runs=verifier_runs,
    )
    return {
        "goal_plus_subagent_role": "search_candidate",
        "search_candidate_agent_session_id": agent_session_id,
        "search_candidate_run_id": session.run_id,
        "search_candidate_id": session.candidate_id,
        "search_candidate_verifier_runs": verifier_runs,
        "search_candidate_verifier_complete": verifier_runs > 0,
        **(lease_context or {}),
    }


def _bind_codex_subagent_observability(
    search_root: Path,
    agent_session_id: str,
    hook_input: dict[str, Any],
) -> None:
    """Persist native transcript identity exposed by Codex SubagentStop."""
    transcript_path = hook_input.get("agent_transcript_path") or hook_input.get(
        "agentTranscriptPath"
    )
    model = hook_input.get("model")
    agent_id = hook_input.get("agent_id") or hook_input.get("agentId")
    metadata: dict[str, Any] = {
        "subagent_stop_observed_at": datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    }
    if isinstance(transcript_path, str) and transcript_path:
        metadata["session_file"] = transcript_path
    if isinstance(model, str) and model:
        metadata["model"] = model
    handle: dict[str, Any] = {"host": "codex", "metadata": metadata}
    session = find_agent_session(search_root, agent_session_id)
    if (
        isinstance(agent_id, str)
        and agent_id
        and session is not None
        and session.host_handle.external_id is None
    ):
        handle["external_id"] = agent_id
    from goal_plus.runtime import FileSearchRuntime

    FileSearchRuntime(search_root).bind_agent_handle(agent_session_id, handle)


def _claim_time_advisory(
    search_root: Path,
    agent_session_id: str,
    payload: dict[str, Any],
    tool_name: str,
) -> bool:
    path = _time_advisory_dir(search_root) / "sent" / f"{agent_session_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    evidence = {
        **payload,
        "trigger_tool": tool_name,
        "sent_at": datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
    }
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(evidence, handle, sort_keys=True, ensure_ascii=False)
        handle.write("\n")
    return True


def _search_candidate_time_advisory(
    search_root: Path,
    hook_input: dict[str, Any],
) -> str | None:
    agent_session_id = _search_candidate_agent_session_id(search_root, hook_input)
    if agent_session_id is None:
        return None
    advisory = build_search_time_advisory(search_root, agent_session_id)
    if advisory is None:
        return None
    if not _claim_time_advisory(
        search_root,
        agent_session_id,
        advisory,
        _tool_name(hook_input),
    ):
        return None
    return str(advisory["message"])


def _host_kind(hook_input: dict[str, Any]) -> str:
    value = hook_input.get("host")
    if value in {"codex", "pi-rpc"}:
        return value
    return "codex"


def _load_record(path: Path):
    from goal_plus.goal_plus import read_json
    from goal_plus.models import GoalPlusRecord

    try:
        return GoalPlusRecord.model_validate(read_json(path))
    except Exception:
        return None


def _active_records(search_root: Path) -> list[Any]:
    goals_dir = search_root / "goal-plus"
    if not goals_dir.is_dir():
        return []
    active = []
    for path in goals_dir.glob("gp_*/goal.json"):
        record = _load_record(path)
        if record is not None and record.status == "active":
            active.append(record)

    active.sort(key=lambda record: (record.updated_at, record.goal_plus_id), reverse=True)
    return active


def _select_explicit_goal_id(search_root: Path, hook_input: dict[str, Any]) -> str | None:
    goals_dir = search_root / "goal-plus"
    if not goals_dir.is_dir():
        return None
    for goal_id in _explicit_goal_ids(hook_input):
        if (goals_dir / goal_id / "goal.json").is_file():
            return goal_id
    return None


def _select_session_goal_id(search_root: Path, session_id: str | None) -> str | None:
    if not session_id:
        return None
    for record in _active_records(search_root):
        if (
            record.active_session is not None
            and record.active_session.session_id == session_id
        ):
            return record.goal_plus_id
    return None


def _select_search_goal_id(search_root: Path, run_id: Any) -> str | None:
    if not isinstance(run_id, str) or not run_id:
        return None
    for record in _active_records(search_root):
        if any(task.run_id == run_id for task in record.search_tasks):
            return record.goal_plus_id
    return None


def _select_hook_goal_id(search_root: Path, hook_input: dict[str, Any]) -> str | None:
    return _select_explicit_goal_id(search_root, hook_input) or _select_session_goal_id(
        search_root,
        _session_id(hook_input),
    )


def _goal_context(record: Any) -> str:
    next_action = record.next_action
    next_action_text = (
        f"{next_action.kind}: {next_action.description}"
        if next_action is not None
        else "无"
    )
    return (
        f"此 Codex session 的 Goal Plus 处于 active：goal_plus_id={record.goal_plus_id}。\n"
        "运行时记录已经存在；不要再次调用 goal_plus_create。\n"
        f"当前目标修订版：{record.goal_revision}。\n"
        f"当前原始目标：{record.raw_goal}\n"
        f"最终检查 policy：{record.policy.get('final_check', {'mode': 'disabled'})}。\n"
        f"当前 phase：{record.phase}；next action：{next_action_text}\n"
        "本轮加载并遵循 goal-plus skill。\n"
        "以最新用户消息作为判断继续、修订或讨论无关内容的权威依据；不要仅因 Goal Plus "
        "处于 active 就恢复工作。\n"
        "如果消息改变了实际范围、交付物或成功标准，使用完整修订后的原始目标和当前预期"
        "修订版调用 goal_plus_update_goal，然后重新 triage。否则保持修订版不变，"
        "并在恢复前澄清有歧义的意图。\n"
        "以 goal_plus_* 工具和已链接的 Search 运行时作为权威依据。"
    )


def _emit_additional_context(event_name: str, context: str) -> None:
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": event_name,
                    "additionalContext": context,
                }
            },
            ensure_ascii=False,
        )
    )


def _emit_pre_tool_block(reason: str) -> None:
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                }
            },
            ensure_ascii=False,
        )
    )


def _should_gate_tool(tool_name: str) -> bool:
    normalized = tool_name.strip().lower().replace("-", "_")
    logical_name = normalized.rsplit("__", 1)[-1]
    return (
        logical_name.startswith("search_")
        or logical_name in {
            "pi_rpc_run_worker",
            "pi_search_pool_open",
            "pi_search_pool_wait_any",
            "pi_search_pool_snapshot",
            "pi_search_pool_continue",
            "pi_search_pool_close",
            "bash",
            "edit",
            "write",
            "exec_command",
            "apply_patch",
        }
    )


def _handle_post_tool_use(
    runtime: Any,
    hook_input: dict[str, Any],
    goal_id: str,
    session_id: str,
) -> None:
    runtime.activate_session(
        goal_id,
        {
            "host": _host_kind(hook_input),
            "session_id": session_id,
            "transcript_path": _transcript_path(hook_input),
            "tool_use_id": _tool_use_id(hook_input),
        },
    )


def _post_tool_use_bind_target(hook_input: dict[str, Any]) -> tuple[str, str] | None:
    if not _is_goal_plus_create_tool(_tool_name(hook_input)):
        return None
    if _is_subagent_context(hook_input):
        return None
    goal_id = _first_goal_id(hook_input.get("tool_response"))
    if goal_id is None:
        goal_id = _first_goal_id(hook_input.get("toolResponse"))
    session_id = _session_id(hook_input)
    if not goal_id or not session_id:
        return None
    return goal_id, session_id


def _record_session_gate_skipped(
    runtime: Any,
    search_root: Path,
    hook_input: dict[str, Any],
    current_session_id: str | None,
) -> None:
    active = _active_records(search_root)
    if not active:
        return
    runtime.record_session_gate_skipped(
        active[0].goal_plus_id,
        "no_matching_session",
        current_session_id=current_session_id,
        context={"hook_event_name": _hook_event_name(hook_input)},
    )


def _emit_block(reason: str) -> None:
    print(json.dumps({"decision": "block", "reason": reason}, ensure_ascii=False))


def _emit_terminal_stats(record: Any) -> None:
    counters = ",".join(
        f"{name}={count}" for name, count in sorted(record.hook_counters.items())
    ) or "none"
    linked_run = (
        record.linked_search.run_id
        if record.linked_search is not None and record.linked_search.run_id
        else "none"
    )
    message = (
        "Goal Plus 统计："
        f"goal_plus_id={record.goal_plus_id}; "
        f"status={record.status}; "
        f"phase={record.phase}; "
        f"goal_revision={record.goal_revision}; "
        f"final_checks={len(record.final_checks)}; "
        f"search_tasks={len(record.search_tasks)}; "
        f"linked_run={linked_run}; "
        f"gates={counters}"
    )
    print(json.dumps({"systemMessage": message}, ensure_ascii=False))


def _handle_user_prompt_submit(
    runtime: Any,
    search_root: Path,
    hook_input: dict[str, Any],
) -> None:
    session_id = _session_id(hook_input)
    goal_id = _select_session_goal_id(search_root, session_id)
    request = _goal_plus_request(hook_input)
    if (
        goal_id is not None
        and request is not None
        and request.get("action") == "edit"
        and isinstance(request.get("raw_goal"), str)
    ):
        current = runtime.status(goal_id)
        runtime.update_goal(
            goal_id,
            raw_goal=request["raw_goal"],
            expected_revision=current.goal_revision,
            reason="user edited the Goal Plus objective through Codex",
        )
    elif (
        goal_id is None
        and request is not None
        and request.get("action") == "start"
        and isinstance(request.get("raw_goal"), str)
        and session_id is not None
    ):
        policy = (
            {"final_check": {"mode": "required"}}
            if request.get("final_check") is True
            else None
        )
        record = runtime.create_goal(
            request["raw_goal"],
            source_path=str(search_root.parent),
            policy=policy,
        )
        goal_id = record.goal_plus_id
    if goal_id is None or session_id is None:
        return
    record = runtime.activate_session(
        goal_id,
        {
            "host": _host_kind(hook_input),
            "session_id": session_id,
            "transcript_path": _transcript_path(hook_input),
        },
    )
    runtime.gate(goal_id, event="user_prompt_submit", context=hook_input)
    _emit_additional_context("UserPromptSubmit", _goal_context(record))


def _handle_session_start(
    runtime: Any,
    search_root: Path,
    hook_input: dict[str, Any],
) -> None:
    goal_id = _select_hook_goal_id(search_root, hook_input)
    if goal_id is None:
        return
    _emit_additional_context("SessionStart", _goal_context(runtime.status(goal_id)))


def _handle_pre_tool_use(
    runtime: Any,
    search_root: Path,
    hook_input: dict[str, Any],
) -> None:
    if _is_subagent_context(hook_input) or not _should_gate_tool(_tool_name(hook_input)):
        return
    goal_id = _select_hook_goal_id(search_root, hook_input)
    if goal_id is None:
        return
    gate = runtime.gate(goal_id, event="pre_tool_use", context=hook_input)
    if gate.decision == "block":
        _emit_pre_tool_block(gate.reason or "Goal Plus 阻止了此工具调用。")


def _handle_stop_event(
    runtime: Any,
    search_root: Path,
    hook_input: dict[str, Any],
    *,
    event: str,
) -> dict[str, Any]:
    candidate_context = None
    if event == "subagent_stop" and not _is_final_checker_context(hook_input):
        candidate_context = _search_candidate_stop_context(search_root, hook_input)
    goal_id = _select_hook_goal_id(search_root, hook_input)
    if goal_id is None and candidate_context is not None:
        goal_id = _select_search_goal_id(
            search_root,
            candidate_context.get("search_candidate_run_id"),
        )
    if goal_id is None:
        current_session_id = _session_id(hook_input)
        active = _active_records(search_root)
        if event == "stop" and active:
            _record_session_gate_skipped(
                runtime,
                search_root,
                hook_input,
                current_session_id,
            )
        return {
            "decision": "skipped",
            "reason": "no_matching_session" if active else "no_matching_goal",
            # An unmatched top-level Stop still belongs to the active Goal Plus
            # record for diagnostics. A SubagentStop may instead belong to a
            # standalone or unlinked Search run, so do not attach it to an
            # arbitrary active goal.
            "goal_plus_id": (
                active[0].goal_plus_id if event == "stop" and active else None
            ),
            "agent_session_id": (
                candidate_context.get("search_candidate_agent_session_id")
                if candidate_context is not None
                else None
            ),
            "run_id": (
                candidate_context.get("search_candidate_run_id")
                if candidate_context is not None
                else None
            ),
            "candidate_id": (
                candidate_context.get("search_candidate_id")
                if candidate_context is not None
                else None
            ),
        }
    gate_context = hook_input
    if event == "subagent_stop" and _is_final_checker_context(hook_input):
        record = runtime.status(goal_id)
        latest = record.final_checks[-1] if record.final_checks else None
        if (
            latest is not None
            and latest.goal_revision == record.goal_revision
            and latest.status == "pending"
        ):
            runtime.submit_final_check(
                goal_id,
                check_id=latest.check_id,
                goal_revision=record.goal_revision,
                verdict="interrupted",
                summary="Codex 最终检查员在提交结论前停止。",
                checker_metadata={"hook_event": "SubagentStop"},
            )
    elif event == "subagent_stop":
        if candidate_context is not None:
            gate_context = {**hook_input, **candidate_context}
            agent_session_id = candidate_context.get(
                "search_candidate_agent_session_id"
            )
            if isinstance(agent_session_id, str):
                _bind_codex_subagent_observability(
                    search_root,
                    agent_session_id,
                    hook_input,
                )
        else:
            record = runtime.status(goal_id)
            latest = record.final_checks[-1] if record.final_checks else None
            pending_final_check = (
                latest is not None
                and latest.goal_revision == record.goal_revision
                and latest.status == "pending"
            )
            if not pending_final_check:
                gate_context = {
                    **hook_input,
                    "goal_plus_subagent_role": "ordinary",
                }
    gate = runtime.gate(goal_id, event=event, context=gate_context)
    if gate.decision == "block":
        _emit_block(
            gate.continuation_prompt
            or gate.reason
            or "Goal Plus 仍处于 active；停止前继续完成任务。"
        )
    elif event == "stop" and gate.status != "active":
        _emit_terminal_stats(runtime.status(goal_id))
    return {
        "decision": gate.decision,
        "reason": gate.reason,
        "goal_plus_id": goal_id,
        "agent_session_id": (
            candidate_context.get("search_candidate_agent_session_id")
            if candidate_context is not None
            else None
        ),
        "run_id": (
            candidate_context.get("search_candidate_run_id")
            if candidate_context is not None
            else None
        ),
        "candidate_id": (
            candidate_context.get("search_candidate_id")
            if candidate_context is not None
            else None
        ),
    }


def _run_recorded_stop_event(
    search_root: Path,
    hook_input: dict[str, Any],
    *,
    event_name: str,
) -> None:
    invocation_id = f"hook_{uuid4().hex}"
    started_at = _utc_now()
    started_monotonic = time.monotonic()
    try:
        from goal_plus.goal_plus import FileGoalPlusRuntime

        result = _handle_stop_event(
            FileGoalPlusRuntime(search_root),
            search_root,
            hook_input,
            event="subagent_stop" if event_name == "SubagentStop" else "stop",
        )
    except Exception as exc:
        _record_stop_hook_event(
            search_root,
            hook_input,
            event_name=event_name,
            invocation_id=invocation_id,
            started_at=started_at,
            started_monotonic=started_monotonic,
            error=exc,
        )
        raise
    _record_stop_hook_event(
        search_root,
        hook_input,
        event_name=event_name,
        invocation_id=invocation_id,
        started_at=started_at,
        started_monotonic=started_monotonic,
        result=result,
    )


def main() -> int:
    if _hook_disabled():
        return 0

    hook_input = _read_hook_input()
    session_root = _find_session_root()
    search_root = _search_root(session_root)

    try:
        event_name = _hook_event_name(hook_input)
        if event_name in {"UserPromptSubmit", "SessionStart", "PreToolUse"}:
            from goal_plus.goal_plus import FileGoalPlusRuntime

            if event_name != "UserPromptSubmit" and not search_root.exists():
                return 0
            if event_name == "UserPromptSubmit":
                if not search_root.exists() and _goal_plus_prompt(hook_input) is None:
                    return 0
                runtime = FileGoalPlusRuntime(search_root)
                _handle_user_prompt_submit(runtime, search_root, hook_input)
            elif event_name == "SessionStart":
                runtime = FileGoalPlusRuntime(search_root)
                _handle_session_start(runtime, search_root, hook_input)
            else:
                runtime = FileGoalPlusRuntime(search_root)
                _handle_pre_tool_use(runtime, search_root, hook_input)
            return 0

        if event_name == "PostToolUse":
            time_advisory = _search_candidate_time_advisory(search_root, hook_input)
            if time_advisory is not None:
                _emit_additional_context("PostToolUse", time_advisory)
                return 0
            target = _post_tool_use_bind_target(hook_input)
            if target is None:
                return 0
            goal_id, session_id = target
            goal_path = search_root / "goal-plus" / goal_id / "goal.json"
            if not goal_path.is_file():
                return 0
            from goal_plus.goal_plus import FileGoalPlusRuntime

            runtime = FileGoalPlusRuntime(search_root)
            _handle_post_tool_use(runtime, hook_input, goal_id, session_id)
            return 0

        if event_name not in {"Stop", "SubagentStop"}:
            return 0
        if not search_root.exists():
            return 0

        _run_recorded_stop_event(
            search_root,
            hook_input,
            event_name=event_name,
        )
        return 0
    except Exception as exc:
        print(f"[goal-plus-hook] allowing host action because hook failed: {exc}", file=sys.stderr)
        return 0
