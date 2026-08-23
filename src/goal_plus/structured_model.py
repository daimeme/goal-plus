from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import tempfile
import time
from typing import Any, Protocol
import uuid

from goal_plus.codex_pricing import estimate_codex_request_cost
from goal_plus.models import (
    ModelInvocationRecord,
    ResolvedStructuredModelProfile,
)


PI_STRUCTURED_OUTPUT_ENV = "GOAL_PLUS_PI_STRUCTURED_OUTPUT"
MAX_STRUCTURED_PROMPT_BYTES = 2 * 1024 * 1024


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _deadline_epoch(value: str | None) -> float | None:
    if not value:
        return None
    try:
        numeric = float(value)
    except ValueError:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    if not math.isfinite(numeric):
        return None
    return numeric / 1000 if numeric > 10_000_000_000 else numeric


class StructuredModelError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        usage: dict[str, int | float] | None = None,
    ) -> None:
        super().__init__(message)
        self.usage = dict(usage or {})


class PermanentStructuredModelError(StructuredModelError):
    pass


class TransientStructuredModelError(StructuredModelError):
    pass


class StructuredModelOutputError(TransientStructuredModelError):
    pass


@dataclass(frozen=True)
class StructuredModelRequest:
    task_kind: str
    profile: ResolvedStructuredModelProfile
    instructions: str
    prompt: str
    output_schema: dict[str, object]
    identity: dict[str, object]
    timeout_seconds: int
    outer_deadline_at: str | None = None
    pi_tool_name: str = "submit_structured_output"
    pi_tool_label: str = "Submit Structured Output"
    pi_tool_description: str = "Submit the final result using the required schema."
    pi_output_env: str = PI_STRUCTURED_OUTPUT_ENV
    monitor_path: Path | None = None
    is_active: Callable[[], bool] | None = None


@dataclass(frozen=True)
class StructuredModelResult:
    payload: dict[str, object]
    usage: dict[str, int | float]
    invocation: ModelInvocationRecord


class StructuredModelExecutor(Protocol):
    def execute(self, request: StructuredModelRequest) -> StructuredModelResult: ...

    def terminate(self) -> None: ...


def strict_output_schema(schema: dict[str, Any]) -> dict[str, Any]:
    normalized = json.loads(json.dumps(schema))

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            value.pop("default", None)
            properties = value.get("properties")
            if isinstance(properties, dict):
                value["required"] = list(properties)
                value["additionalProperties"] = False
            for nested in value.values():
                visit(nested)
        elif isinstance(value, list):
            for nested in value:
                visit(nested)

    visit(normalized)
    return normalized


def _transient_failure(detail: str) -> bool:
    lowered = detail.lower()
    return any(
        marker in lowered
        for marker in (
            "429",
            "500",
            "502",
            "503",
            "504",
            "timeout",
            "timed out",
            "connection",
            "temporarily unavailable",
            "rate limit",
        )
    )


def _provider_args(config: dict[str, Any]) -> list[str]:
    provider = config.get("provider")
    if not isinstance(provider, dict):
        return []
    base_url = provider.get("base_url")
    base_url_env = provider.get("base_url_env")
    if base_url_env:
        base_url = os.environ.get(str(base_url_env))
        if not base_url:
            raise PermanentStructuredModelError(
                f"missing provider URL environment {base_url_env}"
            )
        expected_hash = provider.get("base_url_sha256")
        actual_hash = hashlib.sha256(str(base_url).encode("utf-8")).hexdigest()
        if expected_hash != actual_hash:
            raise PermanentStructuredModelError("provider URL environment changed")
    if not base_url:
        raise PermanentStructuredModelError("provider profile has no base URL")
    api_key_env = str(provider.get("api_key_env") or "")
    if not api_key_env or not os.environ.get(api_key_env):
        raise PermanentStructuredModelError(
            f"missing provider credential environment {api_key_env or '<empty>'}"
        )
    provider_id = str(provider.get("provider_id") or "")
    if not provider_id:
        raise PermanentStructuredModelError("provider profile has no id")
    name = str(provider.get("name") or provider_id)
    wire_api = str(provider.get("wire_api") or "responses")
    return [
        "--config",
        f"model_provider={json.dumps(provider_id)}",
        "--config",
        f"model_providers.{provider_id}.name={json.dumps(name)}",
        "--config",
        f"model_providers.{provider_id}.base_url={json.dumps(base_url)}",
        "--config",
        f"model_providers.{provider_id}.env_key={json.dumps(api_key_env)}",
        "--config",
        f"model_providers.{provider_id}.wire_api={json.dumps(wire_api)}",
    ]


def _codex_usage(stdout: str, model: str | None) -> dict[str, int | float]:
    usage: dict[str, int | float] = {}
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        candidate = event.get("usage") if isinstance(event, dict) else None
        if not isinstance(candidate, dict):
            continue
        for key in (
            "input_tokens",
            "cached_input_tokens",
            "output_tokens",
            "reasoning_output_tokens",
            "total_tokens",
        ):
            value = candidate.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                usage[key] = int(value)
    estimate = estimate_codex_request_cost(
        usage,
        model=model,
        service_tier=None,
    )
    if estimate is not None:
        usage["cost_usd"] = float(estimate["cost_usd"])
    return usage


def _pi_assistant_message(stdout: str) -> dict[str, Any] | None:
    selected: dict[str, Any] | None = None
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "message_end":
            message = event.get("message")
            if isinstance(message, dict) and message.get("role") == "assistant":
                selected = message
        elif event.get("type") == "agent_end":
            messages = event.get("messages")
            if isinstance(messages, list):
                selected = next(
                    (
                        item
                        for item in reversed(messages)
                        if isinstance(item, dict)
                        and item.get("role") == "assistant"
                    ),
                    selected,
                )
    return selected


def _pi_usage(message: dict[str, Any]) -> dict[str, int | float]:
    raw = message.get("usage")
    if not isinstance(raw, dict):
        return {}
    usage: dict[str, int | float] = {}
    for source, target in (
        ("input", "input_tokens"),
        ("output", "output_tokens"),
        ("cacheRead", "cached_input_tokens"),
        ("cacheWrite", "cache_write_tokens"),
        ("totalTokens", "total_tokens"),
    ):
        value = raw.get(source)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            usage[target] = int(value)
    cost = raw.get("cost")
    total = cost.get("total") if isinstance(cost, dict) else None
    if isinstance(total, (int, float)) and not isinstance(total, bool):
        usage["cost_usd"] = float(total)
    return usage


def _pi_extension(request: StructuredModelRequest) -> str:
    schema = json.dumps(
        strict_output_schema(request.output_schema),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return f'''import {{ writeFileSync }} from "node:fs";

const parameters = {schema};

export default function (pi: any) {{
  pi.registerTool({{
    name: "{request.pi_tool_name}",
    label: {json.dumps(request.pi_tool_label, ensure_ascii=False)},
    description: {json.dumps(request.pi_tool_description, ensure_ascii=False)},
    parameters,
    async execute(_toolCallId: string, params: unknown) {{
      const outputPath = process.env.{request.pi_output_env};
      if (!outputPath) throw new Error("missing structured output path");
      writeFileSync(outputPath, JSON.stringify(params), "utf8");
      return {{
        content: [{{ type: "text", text: "Structured output recorded." }}],
        details: {{}},
        terminate: true,
      }};
    }},
  }});
}}
'''


def _event_snapshot(stdout: str) -> dict[str, Any]:
    json_lines = 0
    non_json_lines = 0
    event_types: dict[str, int] = {}
    assistant_types: dict[str, int] = {}
    last_events: list[dict[str, Any]] = []
    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            non_json_lines += 1
            continue
        if not isinstance(event, dict):
            non_json_lines += 1
            continue
        json_lines += 1
        event_type = str(event.get("type") or "unknown")
        event_types[event_type] = event_types.get(event_type, 0) + 1
        summary: dict[str, Any] = {"type": event_type}
        assistant = event.get("assistantMessageEvent")
        if isinstance(assistant, dict):
            assistant_type = str(assistant.get("type") or "unknown")
            assistant_types[assistant_type] = assistant_types.get(assistant_type, 0) + 1
            summary["assistant_event_type"] = assistant_type
            content_index = assistant.get("contentIndex")
            if isinstance(content_index, int) and not isinstance(content_index, bool):
                summary["content_index"] = content_index
            delta = assistant.get("delta")
            if isinstance(delta, str):
                summary["delta_bytes"] = len(delta.encode("utf-8"))
        message = event.get("message")
        if isinstance(message, dict):
            for source, target in (
                ("role", "message_role"),
                ("stopReason", "stop_reason"),
                ("responseId", "response_id"),
            ):
                value = message.get(source)
                if isinstance(value, str) and value:
                    summary[target] = value
        last_events.append(summary)
        if len(last_events) > 12:
            last_events.pop(0)
    return {
        "json_lines": json_lines,
        "non_json_lines": non_json_lines,
        "event_type_counts": dict(sorted(event_types.items())),
        "assistant_event_type_counts": dict(sorted(assistant_types.items())),
        "last_events": last_events,
    }


class _StructuredProcessMonitor:
    def __init__(self, request: StructuredModelRequest) -> None:
        self.path = request.monitor_path
        self.started = time.monotonic()
        self.last_write = 0.0
        identity = request.identity
        profile = request.profile
        self.payload: dict[str, Any] = {
            "schema_version": 1,
            "task_kind": request.task_kind,
            "run_id": identity.get("run_id"),
            "candidate_id": identity.get("candidate_id"),
            "iteration": identity.get("iteration"),
            "attempt": identity.get("attempt"),
            "host": profile.host,
            "model": profile.model,
            "reasoning_effort": profile.reasoning_effort,
            "timeout_seconds": request.timeout_seconds,
            "state": "starting",
            "started_at": _utc_timestamp(),
            "updated_at": _utc_timestamp(),
            "elapsed_seconds": 0.0,
            "pid": None,
            "process_returncode": None,
            "stdout_bytes": 0,
            "stderr_bytes": 0,
            "json_lines": 0,
            "non_json_lines": 0,
            "event_type_counts": {},
            "assistant_event_type_counts": {},
            "last_events": [],
            "stdout_tail": "",
            "stderr_tail": "",
            "detail": None,
        }

    def observe(
        self,
        state: str,
        process: subprocess.Popen[str],
        *,
        stdout: str = "",
        stderr: str = "",
        detail: str | None = None,
        force: bool = False,
    ) -> None:
        if self.path is None:
            return
        now = time.monotonic()
        if not force and self.last_write and now - self.last_write < 5.0:
            return
        self.payload.update(
            {
                "state": state,
                "updated_at": _utc_timestamp(),
                "elapsed_seconds": round(now - self.started, 3),
                "pid": getattr(process, "pid", None),
                "process_returncode": getattr(process, "returncode", None),
                "stdout_bytes": len(stdout.encode("utf-8")),
                "stderr_bytes": len(stderr.encode("utf-8")),
                "stdout_tail": stdout[-2000:],
                "stderr_tail": stderr[-2000:],
                "detail": detail[:2000] if detail else None,
                **_event_snapshot(stdout),
            }
        )
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_name(
                f".{self.path.name}.{uuid.uuid4().hex}.tmp"
            )
            temporary.write_text(
                json.dumps(self.payload, indent=2, sort_keys=True),
                encoding="utf-8",
            )
            temporary.replace(self.path)
            self.last_write = now
        except OSError:
            return


class _ProcessExecutor:
    def __init__(
        self,
        *,
        popen: Callable[..., subprocess.Popen[str]] = subprocess.Popen,
    ) -> None:
        self._popen = popen
        self._active_process: subprocess.Popen[str] | None = None

    def terminate(self) -> None:
        process = self._active_process
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()

    @staticmethod
    def _timeout(request: StructuredModelRequest) -> float:
        timeout = float(min(request.timeout_seconds, request.profile.timeout_seconds))
        deadline = _deadline_epoch(request.outer_deadline_at)
        if deadline is not None:
            timeout = min(timeout, deadline - time.time())
        if timeout <= 0:
            raise PermanentStructuredModelError(
                "structured model outer deadline expired"
            )
        return timeout

    def _communicate(
        self,
        process: subprocess.Popen[str],
        request: StructuredModelRequest,
        timeout: float,
    ) -> tuple[str, str, _StructuredProcessMonitor]:
        started = time.monotonic()
        stdout = ""
        stderr = ""
        monitor = _StructuredProcessMonitor(request)
        monitor.observe("running", process, force=True)
        while True:
            remaining = timeout - (time.monotonic() - started)
            if remaining <= 0:
                self.terminate()
                detail = (
                    f"structured model process timed out after {timeout:.3f} seconds"
                )
                monitor.observe(
                    "timed_out",
                    process,
                    stdout=stdout,
                    stderr=stderr,
                    detail=detail,
                    force=True,
                )
                raise TransientStructuredModelError(detail)
            if request.is_active is not None and not request.is_active():
                self.terminate()
                detail = "structured model task closed during inference"
                monitor.observe(
                    "terminated",
                    process,
                    stdout=stdout,
                    stderr=stderr,
                    detail=detail,
                    force=True,
                )
                raise PermanentStructuredModelError(detail)
            try:
                observed_stdout, observed_stderr = process.communicate(
                    input=None,
                    timeout=min(0.5, remaining),
                )
                stdout = observed_stdout or stdout
                stderr = observed_stderr or stderr
                monitor.observe(
                    "process_exited",
                    process,
                    stdout=stdout,
                    stderr=stderr,
                    force=True,
                )
                return stdout, stderr, monitor
            except subprocess.TimeoutExpired as exc:
                observed_stdout = getattr(exc, "output", None)
                observed_stderr = getattr(exc, "stderr", None)
                if isinstance(observed_stdout, bytes):
                    observed_stdout = observed_stdout.decode(errors="replace")
                if isinstance(observed_stderr, bytes):
                    observed_stderr = observed_stderr.decode(errors="replace")
                if isinstance(observed_stdout, str):
                    stdout = observed_stdout
                if isinstance(observed_stderr, str):
                    stderr = observed_stderr
                monitor.observe(
                    "running",
                    process,
                    stdout=stdout,
                    stderr=stderr,
                )

    @staticmethod
    def _invocation(
        request: StructuredModelRequest,
        *,
        started_at: str,
        elapsed_seconds: float,
    ) -> ModelInvocationRecord:
        profile = request.profile
        provider = (
            profile.provider.provider_id
            if profile.provider is not None
            else profile.pi_provider
        )
        return ModelInvocationRecord(
            invocation_id=f"invocation_{uuid.uuid4().hex}",
            task_kind=request.task_kind,
            host=profile.host,
            model=profile.model,
            provider=provider,
            reasoning_effort=profile.reasoning_effort,
            started_at=started_at,
            completed_at=_utc_timestamp(),
            elapsed_seconds=max(0.0, elapsed_seconds),
        )


class CodexStructuredExecutor(_ProcessExecutor):
    def execute(self, request: StructuredModelRequest) -> StructuredModelResult:
        prompt_bytes = len(request.prompt.encode("utf-8"))
        if prompt_bytes > MAX_STRUCTURED_PROMPT_BYTES:
            raise PermanentStructuredModelError(
                f"structured prompt is {prompt_bytes} bytes; limit is "
                f"{MAX_STRUCTURED_PROMPT_BYTES}"
            )
        timeout = self._timeout(request)
        started_at = _utc_timestamp()
        started = time.monotonic()
        profile = request.profile
        config = profile.model_dump(mode="json")
        with tempfile.TemporaryDirectory(prefix="goal-plus-structured-") as temporary:
            request_dir = Path(temporary)
            (request_dir / "AGENTS.md").write_text(
                request.instructions,
                encoding="utf-8",
            )
            schema_path = request_dir / "output.schema.json"
            output_path = request_dir / "output.json"
            schema_path.write_text(
                json.dumps(strict_output_schema(request.output_schema)),
                encoding="utf-8",
            )
            prompt_path = request_dir / "prompt.txt"
            prompt_path.write_text(request.prompt, encoding="utf-8")
            command = [
                "codex",
                "exec",
                "--json",
                "--ephemeral",
                "--sandbox",
                "read-only",
                "--skip-git-repo-check",
                "--color",
                "never",
                "--output-schema",
                str(schema_path),
                "--output-last-message",
                str(output_path),
                "-C",
                str(request_dir),
            ]
            command.extend(_provider_args(config))
            if profile.model:
                command.extend(("--model", profile.model))
            if profile.reasoning_effort:
                command.extend(
                    (
                        "--config",
                        "model_reasoning_effort="
                        + json.dumps(profile.reasoning_effort),
                    )
                )
            command.append("-")
            environment = os.environ.copy()
            if profile.codex_home:
                environment["CODEX_HOME"] = profile.codex_home
            with prompt_path.open("r", encoding="utf-8") as prompt_input:
                process = self._popen(
                    command,
                    text=True,
                    stdin=prompt_input,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=environment,
                )
                self._active_process = process
                try:
                    stdout, stderr, monitor = self._communicate(
                        process,
                        request,
                        timeout,
                    )
                finally:
                    self._active_process = None
            usage = _codex_usage(stdout, profile.model)
            if process.returncode != 0:
                detail = (stderr or stdout).strip()[-2000:]
                error = f"codex exec exited {process.returncode}: {detail}"
                error_type = (
                    TransientStructuredModelError
                    if _transient_failure(detail)
                    else PermanentStructuredModelError
                )
                monitor.observe(
                    "failed",
                    process,
                    stdout=stdout,
                    stderr=stderr,
                    detail=error,
                    force=True,
                )
                raise error_type(error, usage=usage)
            if not output_path.exists():
                monitor.observe(
                    "failed",
                    process,
                    stdout=stdout,
                    stderr=stderr,
                    detail="codex exec did not write structured output",
                    force=True,
                )
                raise StructuredModelOutputError(
                    "codex exec did not write structured output",
                    usage=usage,
                )
            try:
                payload = json.loads(output_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise StructuredModelOutputError(
                    f"codex exec wrote invalid structured output: {exc}",
                    usage=usage,
                ) from exc
            if not isinstance(payload, dict):
                raise StructuredModelOutputError(
                    "codex structured output must be an object",
                    usage=usage,
                )
            monitor.observe(
                "completed",
                process,
                stdout=stdout,
                stderr=stderr,
                force=True,
            )
        return StructuredModelResult(
            payload=payload,
            usage=usage,
            invocation=self._invocation(
                request,
                started_at=started_at,
                elapsed_seconds=time.monotonic() - started,
            ),
        )


class PiStructuredExecutor(_ProcessExecutor):
    def execute(self, request: StructuredModelRequest) -> StructuredModelResult:
        prompt_bytes = len(request.prompt.encode("utf-8"))
        if prompt_bytes > MAX_STRUCTURED_PROMPT_BYTES:
            raise PermanentStructuredModelError(
                f"structured prompt is {prompt_bytes} bytes; limit is "
                f"{MAX_STRUCTURED_PROMPT_BYTES}"
            )
        timeout = self._timeout(request)
        started_at = _utc_timestamp()
        started = time.monotonic()
        profile = request.profile
        with tempfile.TemporaryDirectory(prefix="goal-plus-structured-") as temporary:
            request_dir = Path(temporary)
            output_path = request_dir / "output.json"
            extension_path = request_dir / "structured-output.ts"
            extension_path.write_text(_pi_extension(request), encoding="utf-8")
            prompt_path = request_dir / "prompt.txt"
            prompt_path.write_text(request.prompt, encoding="utf-8")
            instructions = request.instructions + (
                f"必须调用 {request.pi_tool_name} 作为最后且唯一的输出动作；"
                "不要直接输出 JSON 文本，也不要调用其他工具。\n"
            )
            command = [
                "pi",
                "--mode",
                "json",
                "--print",
                "--no-session",
                "--no-builtin-tools",
                "--tools",
                request.pi_tool_name,
                "--no-extensions",
                "--extension",
                str(extension_path),
                "--no-skills",
                "--no-prompt-templates",
                "--no-context-files",
                "--no-approve",
                "--system-prompt",
                instructions,
            ]
            if profile.model:
                model_provider, separator, model_id = profile.model.partition("/")
                provider = profile.pi_provider or ""
                if separator:
                    if provider and provider != model_provider:
                        raise PermanentStructuredModelError(
                            "Pi provider conflicts with its model reference"
                        )
                    provider = model_provider
                else:
                    model_id = profile.model
                if provider:
                    command.extend(("--provider", provider))
                command.extend(("--model", model_id))
            if profile.reasoning_effort:
                command.extend(("--thinking", profile.reasoning_effort))
            environment = os.environ.copy()
            environment[request.pi_output_env] = str(output_path)
            if profile.pi_home:
                environment["PI_CODING_AGENT_DIR"] = profile.pi_home
            with prompt_path.open("r", encoding="utf-8") as prompt_input:
                process = self._popen(
                    command,
                    cwd=request_dir,
                    text=True,
                    stdin=prompt_input,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=environment,
                )
                self._active_process = process
                try:
                    stdout, stderr, monitor = self._communicate(
                        process,
                        request,
                        timeout,
                    )
                finally:
                    self._active_process = None
            if process.returncode != 0:
                detail = (stderr or stdout).strip()[-2000:]
                error = f"pi exited {process.returncode}: {detail}"
                error_type = (
                    TransientStructuredModelError
                    if _transient_failure(detail)
                    else PermanentStructuredModelError
                )
                monitor.observe(
                    "failed",
                    process,
                    stdout=stdout,
                    stderr=stderr,
                    detail=error,
                    force=True,
                )
                raise error_type(error)
            message = _pi_assistant_message(stdout)
            if message is None:
                raise StructuredModelOutputError(
                    "pi did not emit an assistant structured result"
                )
            usage = _pi_usage(message)
            stop_reason = message.get("stopReason")
            if stop_reason in {"error", "aborted"} or message.get("errorMessage"):
                detail = str(message.get("errorMessage") or stop_reason)
                error_type = (
                    TransientStructuredModelError
                    if _transient_failure(detail)
                    else PermanentStructuredModelError
                )
                raise error_type(detail, usage=usage)
            if not output_path.exists():
                raise StructuredModelOutputError(
                    f"pi did not call {request.pi_tool_name}",
                    usage=usage,
                )
            try:
                payload = json.loads(output_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise StructuredModelOutputError(
                    f"pi wrote invalid structured output: {exc}",
                    usage=usage,
                ) from exc
            if not isinstance(payload, dict):
                raise StructuredModelOutputError(
                    "pi structured output must be an object",
                    usage=usage,
                )
            monitor.observe(
                "completed",
                process,
                stdout=stdout,
                stderr=stderr,
                force=True,
            )
        return StructuredModelResult(
            payload=payload,
            usage=usage,
            invocation=self._invocation(
                request,
                started_at=started_at,
                elapsed_seconds=time.monotonic() - started,
            ),
        )


class HostStructuredModelExecutor:
    def __init__(
        self,
        *,
        popen: Callable[..., subprocess.Popen[str]] = subprocess.Popen,
    ) -> None:
        self._popen = popen
        self._active: StructuredModelExecutor | None = None

    def execute(self, request: StructuredModelRequest) -> StructuredModelResult:
        if request.profile.host == "codex":
            selected: StructuredModelExecutor = CodexStructuredExecutor(
                popen=self._popen
            )
        elif request.profile.host == "pi-rpc":
            selected = PiStructuredExecutor(popen=self._popen)
        else:  # pragma: no cover - model validation owns current host literals
            raise PermanentStructuredModelError(
                f"unsupported structured model host {request.profile.host!r}"
            )
        self._active = selected
        try:
            return selected.execute(request)
        finally:
            self._active = None

    def terminate(self) -> None:
        if self._active is not None:
            self._active.terminate()


__all__ = [
    "HostStructuredModelExecutor",
    "PermanentStructuredModelError",
    "StructuredModelError",
    "StructuredModelOutputError",
    "StructuredModelRequest",
    "StructuredModelResult",
    "TransientStructuredModelError",
    "strict_output_schema",
]
