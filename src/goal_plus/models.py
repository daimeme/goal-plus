from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
    model_validator,
)


class SearchModel(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=True)


class RunState(str, Enum):
    FROZEN_SPEC = "frozen_spec"
    RUNNING = "running"
    WAITING_FOR_WORKERS = "waiting_for_workers"
    EVALUATING = "evaluating"
    SELECTING = "selecting"
    SELECTION_BLOCKED = "selection_blocked"
    READY_TO_PROMOTE = "ready_to_promote"
    PROMOTED = "promoted"
    ABORTED = "aborted"
    FAILED = "failed"


class VerifierRole(str, Enum):
    VALIDITY_GATE = "validity_gate"
    PROCESS_GATE = "process_gate"
    RANKING_SIGNAL = "ranking_signal"
    DIAGNOSTIC_SIGNAL = "diagnostic_signal"
    PROMOTION_GATE = "promotion_gate"
    ANTI_CHEAT_GATE = "anti_cheat_gate"


class FeedbackPolicy(str, Enum):
    VISIBLE_TO_WORKERS = "visible_to_workers"
    SUMMARY_ONLY = "summary_only"
    FINAL_ONLY = "final_only"


class Budget(SearchModel):
    max_parallel: int = Field(
        gt=0,
        description=(
            "一个 Search run 初始创建并实际并行工作的候选 Agent 数量。"
            "parallel_loops 只创建这一组长期候选，后续继续已有 candidate/session。"
        ),
    )
    max_candidates: int | None = Field(
        default=None,
        gt=0,
        description=(
            "一个 run 最多可物化的唯一 candidate 数量。"
            "parallel_loops 未设置时等同于 max_parallel；adaptive_search 必须显式设置。"
        ),
    )
    max_tokens: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def candidate_limit_covers_parallel_width(self) -> "Budget":
        if self.max_candidates is not None and self.max_candidates < self.max_parallel:
            raise ValueError("budget.max_candidates must be >= budget.max_parallel")
        return self


WorkspaceBackend = Literal["copy", "git_worktree"]
IterationDisposition = Literal["keep", "retain", "discard", "failure"]
SharedToolPublishStatus = Literal[
    "legacy_unknown", "not_staged",
    "skipped_unattributed_verifier", "skipped_failed_verifier", "published",
    "partially_published", "consumed_unchanged", "snapshot_rejected",
    "snapshot_error",
]
ToolizationSignal = Literal[
    "repeated_sequence",
    "domain_probe",
    "parser_or_trace",
    "peer_setup_reduction",
]
ToolizationExclusion = Literal[
    "single_common_command",
    "logic_free_wrapper",
    "restricted_artifact",
    "candidate_private_state",
    "duplicate_snapshot",
]
ToolizationAdvisory = Literal[
    "toolization_review_missing",
    "toolization_stage_missing",
    "toolization_decision_mismatch",
]
VerifierInvalidationReason = Literal[
    "verifier_contract_invalid",
    "verifier_coverage_inadequate",
    "verifier_nondeterministic",
    "verifier_target_mismatch",
    "verifier_infrastructure_failure",
]


class WorkspaceSpec(SearchModel):
    backend: WorkspaceBackend = "git_worktree"


class EditSurface(SearchModel):
    allow: list[str] = Field(min_length=1)
    deny: list[str] = Field(default_factory=list)
    max_file_changes: int | None = Field(default=None, gt=0)


AgentHostKind = Literal["codex", "pi-rpc"]


class AgentHostHandle(SearchModel):
    host: AgentHostKind = "codex"
    external_id: str | None = None
    task_name: str | None = None
    nickname: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class WorkerBudget(SearchModel):
    max_runtime_seconds: int | None = Field(default=None, gt=0)
    max_turns: int | None = Field(default=None, gt=0)
    on_exceed: Literal["interrupt"] = "interrupt"
    min_runtime_seconds: int | None = Field(default=None, gt=0)
    min_verifier_runs: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def require_runtime_or_turn_limit(self) -> "WorkerBudget":
        if self.max_runtime_seconds is None and self.max_turns is None:
            raise ValueError(
                "worker_budget requires max_runtime_seconds or max_turns"
            )
        if (
            self.min_runtime_seconds is not None
            and self.max_runtime_seconds is None
        ):
            raise ValueError(
                "worker_budget.min_runtime_seconds requires max_runtime_seconds"
            )
        if (
            self.min_runtime_seconds is not None
            and self.max_runtime_seconds is not None
            and self.min_runtime_seconds >= self.max_runtime_seconds
        ):
            raise ValueError(
                "worker_budget.min_runtime_seconds must be less than "
                "max_runtime_seconds"
            )
        return self


class WorkerLaunchOptions(SearchModel):
    model: str | None = None
    reasoning_effort: str | None = None
    service_tier: str | None = None

    @field_validator("model", "reasoning_effort", "service_tier")
    @classmethod
    def values_must_be_nonempty(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("worker launch option must be non-empty when provided")
        return value


class ModelSpec(SearchModel):
    """User-facing model request, normalized before the spec is frozen."""

    model: str = Field(min_length=1)
    count: int | None = Field(default=None, gt=0)
    provider: str | None = None
    adapter_version: str | None = None
    reasoning_effort: str | None = None
    service_tier: str | None = None
    context_policy: dict[str, Any] = Field(default_factory=dict)

    @field_validator(
        "model", "provider", "adapter_version", "reasoning_effort", "service_tier"
    )
    @classmethod
    def string_values_must_be_nonempty(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("model option must be non-empty when provided")
        return value


class SelectedModel(SearchModel):
    """Exact host model bound immutably to one candidate slot."""

    slot: int = Field(ge=1)
    model: str = Field(min_length=1)
    provider: str | None = None
    adapter_version: str | None = None
    reasoning_effort: str | None = None
    service_tier: str | None = None
    context_policy: dict[str, Any] = Field(default_factory=dict)


class EvidenceAnnotatorSpec(SearchModel):
    host: AgentHostKind | None = None
    model: str | None = None
    pi_provider: str | None = None
    reasoning_effort: str | None = None
    timeout_seconds: int = Field(default=1800, gt=0, le=1800)
    provider: "EvidenceAnnotatorProviderSpec | None" = None

    @field_validator("model", "pi_provider", "reasoning_effort")
    @classmethod
    def values_must_be_nonempty(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("annotator option must be non-empty when provided")
        return value

    @model_validator(mode="after")
    def provider_options_are_host_specific(self) -> "EvidenceAnnotatorSpec":
        if self.provider is not None and self.pi_provider is not None:
            raise ValueError(
                "annotator provider and pi_provider cannot both be configured"
            )
        return self


class EvidenceAnnotatorProviderSpec(SearchModel):
    provider_id: str = Field(
        default="goal-plus-evidence", pattern=r"^[A-Za-z0-9_-]+$"
    )
    name: str = Field(default="Goal Plus Evidence provider", min_length=1)
    base_url: str = Field(min_length=1)
    api_key_env: str = Field(default="OPENAI_API_KEY", min_length=1)
    wire_api: str = Field(default="responses", min_length=1)


class ResolvedCodexProvider(SearchModel):
    provider_id: str = Field(pattern=r"^[A-Za-z0-9_-]+$")
    name: str = Field(min_length=1)
    base_url: str | None = None
    base_url_env: str | None = None
    base_url_sha256: str | None = None
    api_key_env: str = Field(min_length=1)
    wire_api: str = Field(min_length=1)

    @model_validator(mode="after")
    def require_provider_location(self) -> "ResolvedCodexProvider":
        if bool(self.base_url) == bool(self.base_url_env):
            raise ValueError("provider requires exactly one base URL source")
        if self.base_url_env and not self.base_url_sha256:
            raise ValueError("environment-backed provider requires a URL hash")
        return self


class ResolvedEvidenceAnnotatorProfile(SearchModel):
    host: AgentHostKind = "codex"
    model: str | None = None
    pi_provider: str | None = Field(default=None, min_length=1)
    reasoning_effort: str | None = None
    timeout_seconds: int = Field(gt=0, le=1800)
    codex_home: str | None = None
    pi_home: str | None = None
    provider: ResolvedCodexProvider | None = None


EvaluationConfidence = Literal["high", "medium", "low"]


ComparisonRelation = Literal[
    "similar",
    "different",
    "tradeoff",
    "complementary",
    "unknown",
]


class SupplementalDimension(SearchModel):
    name: str = Field(min_length=1, max_length=120)
    finding: str = Field(min_length=1, max_length=1000)
    confidence: EvaluationConfidence
    evidence: list[str] = Field(default_factory=list, max_length=8)

    @field_validator("name", "finding", mode="before")
    @classmethod
    def text_must_be_one_line(cls, value: Any) -> Any:
        if not isinstance(value, str):
            return value
        if "\n" in value or "\r" in value:
            raise ValueError("supplemental evaluation text must be one line")
        return " ".join(value.strip().split())


class EvidenceComparisonReference(SearchModel):
    candidate_id: str = Field(min_length=1)
    iteration: int = Field(ge=1)
    commit: str = Field(min_length=1)


class PeerComparison(EvidenceComparisonReference):
    relation: ComparisonRelation
    rationale: str = Field(min_length=1, max_length=1000)
    evidence: list[str] = Field(default_factory=list, max_length=8)

    @field_validator("rationale", mode="before")
    @classmethod
    def rationale_must_be_one_line(cls, value: Any) -> Any:
        if not isinstance(value, str):
            return value
        if "\n" in value or "\r" in value:
            raise ValueError("peer comparison rationale must be one line")
        return " ".join(value.strip().split())


class SupplementalEvaluation(SearchModel):
    summary: str = Field(min_length=1, max_length=1000)
    dimensions: list[SupplementalDimension] = Field(min_length=1, max_length=8)
    comparisons: list[PeerComparison] = Field(default_factory=list, max_length=8)
    limitations: list[str] = Field(default_factory=list, max_length=8)

    @field_validator("summary", mode="before")
    @classmethod
    def summary_must_be_one_line(cls, value: Any) -> Any:
        if not isinstance(value, str):
            return value
        if "\n" in value or "\r" in value:
            raise ValueError("supplemental evaluation summary must be one line")
        return " ".join(value.strip().split())

    @field_validator("limitations", mode="before")
    @classmethod
    def limitations_must_be_one_line(cls, value: Any) -> Any:
        if not isinstance(value, list):
            return value
        normalized = []
        for item in value:
            if not isinstance(item, str):
                normalized.append(item)
                continue
            if "\n" in item or "\r" in item:
                raise ValueError("supplemental evaluation limitation must be one line")
            normalized.append(" ".join(item.strip().split()))
        return normalized


class ToolViewRef(SearchModel):
    tool_id: str = Field(min_length=1)
    summary: str = Field(min_length=1, max_length=1000)
    capabilities: list[str] = Field(max_length=16)
    when_to_use: str = Field(min_length=1, max_length=1000)
    entrypoint: str | None = Field(max_length=500)
    inputs: list[str] = Field(max_length=16)
    outputs: list[str] = Field(max_length=16)
    dependencies: list[str] = Field(max_length=16)
    adoption_steps: list[str] = Field(max_length=16)
    limitations: list[str] = Field(max_length=16)

    @field_validator(
        "capabilities", "inputs", "outputs", "dependencies",
        "adoption_steps", "limitations", mode="before"
    )
    @classmethod
    def normalize_items(cls, value: Any) -> Any:
        if not isinstance(value, list):
            return value
        normalized = []
        for item in value:
            if not isinstance(item, str):
                normalized.append(item)
                continue
            text = " ".join(item.strip().split())
            if not text or len(text) > 500:
                raise ValueError("tool view list items must be non-empty and at most 500 characters")
            normalized.append(text)
        return normalized


class ToolViewRecord(ToolViewRef):
    snapshot_hash: str = Field(min_length=1)
    source_commit: str = Field(min_length=1)
    evidence_scope: str = Field(min_length=1, max_length=1000)


class ToolAdoptionRecord(SearchModel):
    tool_id: str = Field(min_length=1)
    snapshot_hash: str = Field(min_length=1)
    receipt_id: str | None = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode="before")
    @classmethod
    def migrate_legacy_declaration(cls, value: Any) -> Any:
        if not isinstance(value, dict) or "mode" not in value:
            return value
        payload = dict(value)
        payload.pop("mode", None)
        return payload


class ToolCopyReceipt(SearchModel):
    receipt_id: str = Field(min_length=1)
    tool_id: str = Field(min_length=1)
    snapshot_hash: str = Field(min_length=1)
    source_commit: str | None = None
    agent_session_id: str = Field(min_length=1)
    candidate_base_git_head: str = Field(min_length=1)
    inbox_path: Path
    copied_at: str


class EvidenceViewRecord(SearchModel):
    run_id: str = Field(min_length=1)
    candidate_id: str = Field(min_length=1)
    iteration: int = Field(ge=1)
    attempt_commit: str = Field(min_length=1)
    description: str = Field(min_length=1, max_length=1000)
    supplemental_evaluation: SupplementalEvaluation | None = None
    comparison_basis: list[EvidenceComparisonReference] = Field(
        default_factory=list,
        max_length=8,
    )
    tool_views: list[ToolViewRecord] = Field(default_factory=list)
    created_at: str

    @field_validator("description", mode="before")
    @classmethod
    def normalize_description(cls, value: Any) -> Any:
        if not isinstance(value, str):
            return value
        if "\n" in value or "\r" in value:
            raise ValueError("evidence view description must be one line")
        return " ".join(value.strip().split())


class GlobalEvidenceViewReference(SearchModel):
    candidate_id: str = Field(min_length=1)
    iteration: int = Field(ge=1)
    commit: str = Field(min_length=1)
    view_created_at: str
    supplemental_evaluation_present: bool = False


class GlobalEvidenceReadRecord(SearchModel):
    read_at: str
    evidence_count: int = Field(ge=0)
    completed_view_count: int = Field(ge=0)
    completed_supplemental_evaluation_count: int = Field(ge=0)
    completed_views: list[GlobalEvidenceViewReference] = Field(default_factory=list)

    @model_validator(mode="after")
    def counts_match_completed_views(self) -> "GlobalEvidenceReadRecord":
        if self.completed_view_count != len(self.completed_views):
            raise ValueError("completed_view_count must match completed_views")
        supplemental_count = sum(
            item.supplemental_evaluation_present for item in self.completed_views
        )
        if self.completed_supplemental_evaluation_count != supplemental_count:
            raise ValueError(
                "completed_supplemental_evaluation_count must match completed_views"
            )
        if self.completed_view_count > self.evidence_count:
            raise ValueError("completed_view_count cannot exceed evidence_count")
        return self


class EvidenceAnnotationTask(SearchModel):
    run_id: str = Field(min_length=1)
    candidate_id: str = Field(min_length=1)
    iteration: int = Field(ge=1)
    attempt_base_commit: str = Field(min_length=1)
    attempt_commit: str = Field(min_length=1)
    attempt_changed_files: list[str] = Field(default_factory=list)
    task_context_source: Literal[
        "goal_plus_raw_goal",
        "frozen_objective",
    ] | None = None
    task_context_ref: str | None = None
    task_context_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    supplemental_evaluation_enabled: bool = False
    comparison_basis: list[EvidenceComparisonReference] = Field(
        default_factory=list,
        max_length=8,
    )
    profile: ResolvedEvidenceAnnotatorProfile | None = None
    outer_deadline_at: str | None = None
    state: Literal["pending", "retry_wait", "completed", "terminal_error"] = (
        "pending"
    )
    attempts: int = Field(default=0, ge=0)
    next_attempt_at: str | None = None
    error_fingerprint: str | None = None
    last_error: str | None = None
    attempt_history: list[dict[str, Any]] = Field(default_factory=list)
    usage: dict[str, int | float] = Field(default_factory=dict)
    view: EvidenceViewRecord | None = None
    created_at: str
    updated_at: str


class RewardEvaluatorSpec(SearchModel):
    name: str = Field(default="metric_progress", min_length=1)
    version: int = Field(default=1, ge=1)
    params: dict[str, Any] = Field(default_factory=dict)


class AllocationPolicySpec(SearchModel):
    name: str = Field(default="low_reward_replace", min_length=1)
    version: int = Field(default=1, ge=1)
    params: dict[str, Any] = Field(default_factory=dict)
    max_replacements_per_decision: int = Field(
        default=1,
        ge=1,
        description=(
            "一次原子 allocation decision 最多退休并替换的 candidate 数量。"
        ),
    )


class ValueBackupOperatorSpec(SearchModel):
    name: str = Field(default="identity", min_length=1)
    version: int = Field(default=1, ge=1)
    params: dict[str, Any] = Field(default_factory=dict)


class ExpansionPolicySpec(SearchModel):
    source_policy: Literal["highest_value"] = "highest_value"
    model_policy: Literal["inherit_source"] = "inherit_source"
    max_depth: int = Field(default=3, ge=1, le=64)
    max_unobserved_expansions_per_node: int | None = Field(
        default=None,
        gt=0,
        exclude_if=lambda value: value is None,
        description=(
            "每个 source node 同时允许的未观测派生 candidate 上限；"
            "candidate 首次 verifier 结算后释放。"
        ),
    )
    worker_budget: WorkerBudget | None = None


class AdaptiveSearchSpec(SearchModel):
    reward: RewardEvaluatorSpec = Field(default_factory=RewardEvaluatorSpec)
    value_backup: ValueBackupOperatorSpec = Field(
        default_factory=ValueBackupOperatorSpec
    )
    allocation: AllocationPolicySpec = Field(default_factory=AllocationPolicySpec)
    expansion: ExpansionPolicySpec = Field(default_factory=ExpansionPolicySpec)


class StrategySpec(SearchModel):
    name: str = "agent_guided"
    orchestration_mode: Literal[
        "rolling_candidates", "parallel_loops", "adaptive_search"
    ] = (
        "parallel_loops"
    )
    worker_host: AgentHostKind = "codex"
    worker_agent_type: str | None = None
    worker_budget: WorkerBudget | None = None
    worker_launch: WorkerLaunchOptions | None = None
    evidence_annotator: EvidenceAnnotatorSpec = Field(
        default_factory=EvidenceAnnotatorSpec
    )
    models: list[ModelSpec] = Field(
        default_factory=list,
        exclude_if=lambda value: not value,
    )
    adaptive_search: AdaptiveSearchSpec | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    config: dict[str, Any] = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def name_must_be_nonempty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("strategy name must be non-empty")
        return value

    @field_validator("worker_agent_type")
    @classmethod
    def worker_agent_type_must_be_nonempty(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("worker_agent_type must be non-empty when provided")
        return value

    @model_validator(mode="after")
    def adaptive_config_matches_orchestration_mode(self) -> "StrategySpec":
        if self.orchestration_mode == "adaptive_search":
            if self.adaptive_search is None:
                raise ValueError(
                    "strategy.adaptive_search is required for adaptive_search mode"
                )
        elif self.adaptive_search is not None:
            raise ValueError(
                "strategy.adaptive_search requires orchestration_mode=adaptive_search"
            )
        return self


class VerifierCommand(SearchModel):
    name: str = Field(min_length=1)
    role: VerifierRole
    command: list[str] = Field(min_length=1)
    cwd: str = "."
    timeout_seconds: int = Field(default=300, gt=0)
    resource_lock: str | None = Field(
        default=None,
        min_length=1,
        description=(
            "可选的 host 级独占资源名。值相同的 verifier 命令会跨候选和 run 串行执行。"
        ),
    )
    feedback_policy: FeedbackPolicy = FeedbackPolicy.VISIBLE_TO_WORKERS
    expected_outputs: list[str] = Field(
        default_factory=list,
        description=(
            "候选工作区中预期的产物路径或 glob；此字段不解析 verifier stdout metric。"
        ),
    )

    @field_validator("resource_lock")
    @classmethod
    def resource_lock_must_be_nonempty(
        cls,
        value: str | None,
    ) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("resource_lock must be non-empty when provided")
        return normalized


class SharedDirSpec(SearchModel):
    enabled: bool = False
    max_tools_per_iteration: int = Field(default=16, gt=0, le=128)
    max_files_per_iteration: int = Field(default=64, gt=0, le=512)
    max_path_entries_per_iteration: int = Field(default=512, gt=0, le=8192)
    max_depth: int = Field(default=8, ge=1, le=32)
    max_bytes_per_iteration: int = Field(default=2 * 1024 * 1024, gt=0, le=64 * 1024 * 1024)


class SearchSpec(SearchModel):
    objective: str = Field(min_length=1)
    metric_name: str = Field(min_length=1)
    metric_direction: Literal["minimize", "maximize"]
    source_path: str
    edit_surface: EditSurface
    budget: Budget
    process_verifiers: list[VerifierCommand] = Field(min_length=1)
    promotion_verifiers: list[VerifierCommand] = Field(default_factory=list)
    constraints: dict[str, Any] = Field(default_factory=dict)
    root_hypotheses: list[str] = Field(default_factory=list)
    strategy: StrategySpec = Field(default_factory=StrategySpec)
    workspace: WorkspaceSpec = Field(default_factory=WorkspaceSpec)
    shared_dir: SharedDirSpec = Field(default_factory=SharedDirSpec)

    @field_validator("source_path")
    @classmethod
    def source_path_must_be_nonempty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("source_path must be non-empty")
        return value

    @model_validator(mode="after")
    def adaptive_search_requires_branchable_budget(self) -> "SearchSpec":
        if self.strategy.orchestration_mode != "adaptive_search":
            return self
        if self.workspace.backend != "git_worktree":
            raise ValueError("adaptive_search requires workspace.backend=git_worktree")
        if self.budget.max_candidates is None:
            raise ValueError("adaptive_search requires budget.max_candidates")
        return self


class SearchSpecDraft(SearchModel):
    """Partially discovered SearchSpec with the same nested field contracts."""

    objective: str | None = Field(default=None, min_length=1)
    metric_name: str | None = Field(default=None, min_length=1)
    metric_direction: Literal["minimize", "maximize"] | None = None
    source_path: str | None = Field(default=None, min_length=1)
    edit_surface: EditSurface | None = None
    budget: Budget | None = None
    process_verifiers: list[VerifierCommand] | None = Field(default=None, min_length=1)
    promotion_verifiers: list[VerifierCommand] | None = None
    constraints: dict[str, Any] | None = None
    root_hypotheses: list[str] | None = None
    strategy: StrategySpec | None = None
    workspace: WorkspaceSpec | None = None
    shared_dir: SharedDirSpec | None = None


GoalPlusStatus = Literal["active", "needs_user", "blocked", "complete", "abandoned"]
GoalPlusPhase = Literal[
    "intake",
    "goal",
    "spec_discovery",
    "search",
    "final_audit",
    "final_check",
]
GoalPlusConfidence = Literal["high", "medium", "low"]
GoalPlusRecommendedPhase = Literal["goal", "spec_discovery", "search"]
GoalPlusDiscoveryOrigin = Literal["initial", "in_progress"]
GoalPlusGateEvent = Literal["stop", "subagent_stop", "pre_tool_use", "user_prompt_submit"]
GoalPlusGateDecision = Literal["allow", "block"]
GoalPlusSessionState = Literal["attached", "stale", "detached"]


class GoalPlusNextAction(SearchModel):
    kind: str = Field(min_length=1)
    description: str = Field(min_length=1)
    required: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)


GoalPlusFinalCheckMode = Literal["disabled", "required"]
GoalPlusFinalCheckStatus = Literal[
    "pending",
    "passed",
    "failed",
    "interrupted",
    "superseded",
]
GoalPlusFinalCheckerHost = Literal["codex", "pi"]


class GoalPlusGoalRevision(SearchModel):
    revision: int = Field(ge=1)
    raw_goal: str = Field(min_length=1)
    reason: str | None = None
    created_at: str


class GoalPlusFinalCheck(SearchModel):
    check_id: str = Field(min_length=1)
    goal_revision: int = Field(ge=1)
    checker_host: GoalPlusFinalCheckerHost
    status: GoalPlusFinalCheckStatus = "pending"
    requested_phase: GoalPlusPhase
    requested_at: str
    completed_at: str | None = None
    summary: str | None = None
    findings: list[dict[str, Any]] = Field(default_factory=list)
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    checker_metadata: dict[str, Any] = Field(default_factory=dict)


class GoalPlusTriage(SearchModel):
    is_optimization: bool
    confidence: GoalPlusConfidence
    recommended_phase: GoalPlusRecommendedPhase
    identified_at: GoalPlusDiscoveryOrigin = "initial"
    scenario: str | None = None
    reasons: list[str] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)


class GoalPlusSpecDraft(SearchModel):
    baseline: dict[str, Any]
    metric: dict[str, Any]
    correctness_gate: dict[str, Any]
    edit_surface: dict[str, Any]
    verifier_artifacts: list[str] = Field(default_factory=list)
    search_spec: SearchSpecDraft | dict[str, Any] = Field(union_mode="left_to_right")
    promotion_rule: str = Field(min_length=1)
    confidence: GoalPlusConfidence
    origin: GoalPlusDiscoveryOrigin | None = None
    open_questions: list[str] = Field(default_factory=list)

    @field_serializer("search_spec")
    def serialize_search_spec(
        self, value: SearchSpecDraft | dict[str, Any]
    ) -> dict[str, Any]:
        if isinstance(value, SearchSpecDraft):
            return value.model_dump(mode="json", exclude_none=True)
        return value


class GoalPlusSpecDraftInput(GoalPlusSpecDraft):
    """Strict tool-input shape; persisted legacy drafts remain backward-readable."""

    search_spec: SearchSpecDraft


class GoalPlusLinkedSearch(SearchModel):
    goal_revision: int = Field(default=1, ge=1)
    frozen_spec_id: str | None = None
    run_id: str | None = None
    linked_at: str | None = None
    selected_candidate_id: str | None = None
    report_path: str | None = None
    html_report_path: str | None = None
    promotion_artifact_path: str | None = None
    summary: str | None = None
    result_recorded_at: str | None = None


class GoalPlusActiveSession(SearchModel):
    host: AgentHostKind
    session_id: str = Field(min_length=1)
    transcript_path: str | None = None
    tool_use_id: str | None = None
    state: GoalPlusSessionState = "attached"
    attached_at: str
    last_seen_at: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class GoalPlusRecord(SearchModel):
    goal_plus_id: str
    raw_goal: str = Field(min_length=1)
    source_path: str | None = None
    status: GoalPlusStatus = "active"
    phase: GoalPlusPhase = "intake"
    policy: dict[str, Any] = Field(default_factory=dict)
    goal_revision: int = Field(default=1, ge=1)
    goal_revisions: list[GoalPlusGoalRevision] = Field(default_factory=list)
    final_checks: list[GoalPlusFinalCheck] = Field(default_factory=list)
    triage: GoalPlusTriage | None = None
    spec_draft: GoalPlusSpecDraft | None = None
    search_tasks: list[GoalPlusLinkedSearch] = Field(default_factory=list)
    linked_search: GoalPlusLinkedSearch | None = None
    next_action: GoalPlusNextAction | None = None
    active_session: GoalPlusActiveSession | None = None
    hook_counters: dict[str, int] = Field(default_factory=dict)
    created_at: str
    updated_at: str

    @model_validator(mode="after")
    def synchronize_search_task_compatibility_view(self) -> "GoalPlusRecord":
        if not self.goal_revisions:
            self.goal_revisions = [
                GoalPlusGoalRevision(
                    revision=self.goal_revision,
                    raw_goal=self.raw_goal,
                    reason="legacy record imported",
                    created_at=self.created_at,
                )
            ]
        latest_revision = self.goal_revisions[-1]
        if latest_revision.revision != self.goal_revision or latest_revision.raw_goal != self.raw_goal:
            raise ValueError("raw_goal and goal_revision must match the latest goal revision")
        if not self.search_tasks and self.linked_search is not None:
            self.search_tasks = [self.linked_search.model_copy(deep=True)]
        elif self.search_tasks:
            latest_task = self.search_tasks[-1]
            self.linked_search = (
                latest_task.model_copy(deep=True)
                if latest_task.goal_revision == self.goal_revision
                else None
            )
        return self


class GoalPlusGateResult(SearchModel):
    decision: GoalPlusGateDecision
    phase: GoalPlusPhase
    status: GoalPlusStatus
    reason: str | None = None
    continuation_prompt: str | None = None


class FrozenSpec(SearchModel):
    frozen_spec_id: str
    spec_hash: str
    spec: SearchSpec
    verifier_hashes: dict[str, str]
    frozen_verifier_paths: dict[str, str]
    created_at: str


class SharedToolRecord(SearchModel):
    tool_id: str = Field(min_length=1)
    candidate_id: str = Field(min_length=1)
    iteration: int = Field(ge=1)
    source_commit: str | None = None
    snapshot_hash: str = Field(min_length=1)
    name: str = Field(min_length=1)
    summary: str | None = None
    entrypoint: str | None = None
    source_relative_path: str = Field(min_length=1)
    read_only_path: Path
    files: list[str] = Field(default_factory=list)
    size_bytes: int = Field(ge=0)
    created_at: str

    @model_validator(mode="before")
    @classmethod
    def migrate_legacy_asset_id(cls, value: Any) -> Any:
        if not isinstance(value, dict) or "tool_id" in value or "asset_id" not in value:
            return value
        payload = dict(value)
        payload["tool_id"] = payload.pop("asset_id")
        return payload


RewardEvaluationStatus = Literal["evaluated", "ineligible", "error"]
AllocationEligibility = Literal["eligible", "retired"]
AllocationDecisionStatus = Literal["pending", "applied"]


class RewardEvaluation(SearchModel):
    reward_id: str = Field(min_length=1)
    evaluator_name: str = Field(min_length=1)
    evaluator_version: int = Field(ge=1)
    config_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: RewardEvaluationStatus
    attempt_reward: float | None = Field(default=None, allow_inf_nan=False)
    settled_value: float | None = Field(default=None, allow_inf_nan=False)
    components: dict[str, float] = Field(default_factory=dict)
    error: str | None = None
    created_at: str

    @model_validator(mode="before")
    @classmethod
    def migrate_legacy_value_names(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        payload = dict(value)
        legacy_reward = payload.pop("reward", None)
        legacy_state_value = payload.pop("state_value", None)
        if "attempt_reward" not in payload and legacy_reward is not None:
            payload["attempt_reward"] = legacy_reward
        if "settled_value" not in payload and legacy_state_value is not None:
            payload["settled_value"] = legacy_state_value
        return payload

    @field_validator("components")
    @classmethod
    def components_must_be_finite(
        cls, value: dict[str, float]
    ) -> dict[str, float]:
        import math

        if any(not math.isfinite(item) for item in value.values()):
            raise ValueError("reward components must be finite")
        return value

    @model_validator(mode="after")
    def status_matches_payload(self) -> "RewardEvaluation":
        if self.status == "evaluated" and self.attempt_reward is None:
            raise ValueError("evaluated reward requires an attempt_reward value")
        if self.status == "error" and not self.error:
            raise ValueError("reward error status requires an error message")
        return self

    @property
    def reward(self) -> float | None:
        """Compatibility accessor for pre-value-layer callers."""

        return self.attempt_reward

    @property
    def state_value(self) -> float | None:
        """Compatibility accessor for pre-value-layer callers."""

        return self.settled_value


SearchNodeKind = Literal["virtual_root", "settled_iteration"]
SearchTransitionKind = Literal[
    "initial_attempt", "candidate_continuation", "derived_candidate_start"
]


class SearchNodeRecord(SearchModel):
    schema_version: Literal[1] = 1
    node_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    kind: SearchNodeKind
    candidate_id: str = Field(min_length=1)
    iteration: int | None = Field(default=None, ge=1)
    parent_node_id: str | None = None
    incoming_transition_id: str | None = None
    evidence_commit: str | None = None
    settled_commit: str = Field(min_length=1)
    artifact_hash: str | None = None
    score: float | None = Field(default=None, allow_inf_nan=False)
    reward_id: str | None = None
    settled_value: float | None = Field(default=None, allow_inf_nan=False)
    transition_depth: int = Field(ge=0)
    allocation_depth: int = Field(ge=0)
    created_at: str

    @model_validator(mode="after")
    def kind_matches_iteration(self) -> "SearchNodeRecord":
        if self.kind == "virtual_root":
            if self.iteration is not None or self.parent_node_id is not None:
                raise ValueError("virtual root nodes cannot have an iteration or parent")
            if self.incoming_transition_id is not None:
                raise ValueError("virtual root nodes cannot have an incoming transition")
        elif (
            self.iteration is None
            or self.parent_node_id is None
            or self.incoming_transition_id is None
            or self.evidence_commit is None
            or self.artifact_hash is None
            or self.score is None
            or self.reward_id is None
        ):
            raise ValueError(
                "settled iteration nodes require iteration, parent, incoming "
                "transition, evidence, score, artifact, and reward"
            )
        return self


class SearchTransitionRecord(SearchModel):
    schema_version: Literal[1] = 1
    transition_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    candidate_id: str = Field(min_length=1)
    iteration: int = Field(ge=1)
    kind: SearchTransitionKind
    from_node_id: str = Field(min_length=1)
    accepted_node_id: str | None = None
    settled_node_id: str = Field(min_length=1)
    attempt_base_commit: str | None = None
    attempt_commit: str | None = None
    settled_commit: str | None = None
    disposition: IterationDisposition
    process_passed: bool
    score: float | None = Field(default=None, allow_inf_nan=False)
    reward_id: str | None = None
    attempt_reward: float | None = Field(default=None, allow_inf_nan=False)
    created_at: str

    @model_validator(mode="after")
    def settlement_matches_disposition(self) -> "SearchTransitionRecord":
        if self.disposition in {"keep", "retain"}:
            if not self.process_passed:
                raise ValueError("accepted transitions require process success")
            if self.accepted_node_id is None:
                raise ValueError("accepted transitions require an accepted node")
            if self.settled_node_id != self.accepted_node_id:
                raise ValueError("accepted transitions must settle on the accepted node")
        else:
            if self.accepted_node_id is not None:
                raise ValueError("discarded or failed transitions cannot create a node")
            if self.settled_node_id != self.from_node_id:
                raise ValueError("discarded or failed transitions settle on their source")
        if self.disposition == "failure" and self.process_passed:
            raise ValueError("failure transitions cannot pass process verification")
        return self


class SearchGraphProjection(SearchModel):
    schema_version: Literal[1] = 1
    run_id: str = Field(min_length=1)
    revision: int = Field(ge=0)
    nodes: list[SearchNodeRecord] = Field(default_factory=list)
    transitions: list[SearchTransitionRecord] = Field(default_factory=list)
    candidate_current_node_ids: dict[str, str] = Field(default_factory=dict)
    created_at: str
    updated_at: str

    @model_validator(mode="after")
    def references_are_consistent(self) -> "SearchGraphProjection":
        node_ids = [item.node_id for item in self.nodes]
        transition_ids = [item.transition_id for item in self.transitions]
        expected_revision = len(self.transitions) + len(
            self.candidate_current_node_ids
        )
        if self.revision != expected_revision:
            raise ValueError(
                "search graph revision must equal candidate and transition fact count"
            )
        if len(node_ids) != len(set(node_ids)):
            raise ValueError("search graph node ids must be unique")
        if len(transition_ids) != len(set(transition_ids)):
            raise ValueError("search graph transition ids must be unique")
        transition_coordinates = [
            (item.candidate_id, item.iteration) for item in self.transitions
        ]
        if len(transition_coordinates) != len(set(transition_coordinates)):
            raise ValueError("candidate iteration transitions must be unique")
        known_nodes = set(node_ids)
        transitions_by_id = {
            item.transition_id: item for item in self.transitions
        }
        for node in self.nodes:
            if node.run_id != self.run_id:
                raise ValueError("search graph node belongs to another run")
            if node.parent_node_id is not None and node.parent_node_id not in known_nodes:
                raise ValueError("search graph node parent is unavailable")
            if node.incoming_transition_id is not None:
                incoming = transitions_by_id.get(node.incoming_transition_id)
                if incoming is None or incoming.accepted_node_id != node.node_id:
                    raise ValueError("search graph node incoming transition is invalid")
        for transition in self.transitions:
            if transition.run_id != self.run_id:
                raise ValueError("search graph transition belongs to another run")
            references = {
                transition.from_node_id,
                transition.settled_node_id,
            }
            if transition.accepted_node_id is not None:
                references.add(transition.accepted_node_id)
            if not references.issubset(known_nodes):
                raise ValueError("search graph transition references an unavailable node")
        if not set(self.candidate_current_node_ids.values()).issubset(known_nodes):
            raise ValueError("candidate current node is unavailable")
        return self


class NodeValueEstimate(SearchModel):
    node_id: str = Field(min_length=1)
    settled_value: float | None = Field(default=None, allow_inf_nan=False)
    expected_gain: float | None = Field(default=None, allow_inf_nan=False)
    backed_up_value: float | None = Field(default=None, allow_inf_nan=False)
    direct_attempt_count: int = Field(default=0, ge=0)
    observation_count: int = Field(default=0, ge=0)
    unique_branch_count: int = Field(default=0, ge=0)
    completed_expansion_count: int = Field(default=0, ge=0)
    unobserved_expansion_count: int = Field(default=0, ge=0)
    backup_count: int = Field(default=0, ge=0)
    backup_mean_return: float | None = Field(default=None, allow_inf_nan=False)
    backup_return_m2: float = Field(default=0.0, ge=0, allow_inf_nan=False)
    backup_best_return: float | None = Field(default=None, allow_inf_nan=False)
    total_cost: float = Field(default=0.0, ge=0, allow_inf_nan=False)
    uncertainty: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    updated_at: str


class EdgeValueEstimate(SearchModel):
    transition_id: str = Field(min_length=1)
    from_node_id: str = Field(min_length=1)
    settled_node_id: str = Field(min_length=1)
    attempt_reward: float | None = Field(default=None, allow_inf_nan=False)
    observed_return: float | None = Field(default=None, allow_inf_nan=False)
    created_at: str


class ValueProjection(SearchModel):
    schema_version: Literal[1] = 1
    run_id: str = Field(min_length=1)
    graph_revision: int = Field(ge=0)
    operator_name: str = Field(min_length=1)
    operator_version: int = Field(ge=1)
    config_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    applied_backup_event_ids: list[str] = Field(default_factory=list)
    node_values: list[NodeValueEstimate] = Field(default_factory=list)
    edge_values: list[EdgeValueEstimate] = Field(default_factory=list)
    created_at: str
    updated_at: str

    @model_validator(mode="after")
    def estimate_ids_are_unique(self) -> "ValueProjection":
        node_ids = [item.node_id for item in self.node_values]
        transition_ids = [item.transition_id for item in self.edge_values]
        if len(self.applied_backup_event_ids) != len(
            set(self.applied_backup_event_ids)
        ):
            raise ValueError("applied backup event ids must be unique")
        if len(node_ids) != len(set(node_ids)):
            raise ValueError("node value estimate ids must be unique")
        if len(transition_ids) != len(set(transition_ids)):
            raise ValueError("edge value estimate ids must be unique")
        return self


ActionDescriptionSource = Literal["evidence_view", "hypothesis"]


class ObservedActionEdge(SearchModel):
    """One settled worker attempt projected as an explored search action."""

    schema_version: Literal[1] = 1
    transition_id: str = Field(min_length=1)
    candidate_id: str = Field(min_length=1)
    iteration: int = Field(ge=1)
    kind: SearchTransitionKind
    from_node_id: str = Field(min_length=1)
    settled_node_id: str = Field(min_length=1)
    attempt_commit: str | None = None
    description: str = Field(min_length=1, max_length=1000)
    description_source: ActionDescriptionSource
    attempt_changed_files: list[str] = Field(default_factory=list)
    artifact_hash: str | None = None
    exact_effect_key: str | None = None
    duplicate_of_transition_id: str | None = None
    disposition: IterationDisposition
    score: float | None = Field(default=None, allow_inf_nan=False)


class ExpansionActionContext(SearchModel):
    """Observed path and sibling actions available to one derived candidate."""

    schema_version: Literal[1] = 1
    graph_revision: int = Field(ge=0)
    source_node_id: str = Field(min_length=1)
    source_candidate_id: str = Field(min_length=1)
    source_path_actions: list[ObservedActionEdge] = Field(default_factory=list)
    tried_actions: list[ObservedActionEdge] = Field(default_factory=list)
    unique_tried_action_count: int = Field(ge=0)
    exact_duplicate_count: int = Field(ge=0)


class ExpansionSource(SearchModel):
    node_id: str = Field(min_length=1)
    candidate_id: str = Field(min_length=1)
    iteration: int = Field(ge=1)
    evidence_commit: str = Field(min_length=1)
    settled_commit: str = Field(min_length=1)
    artifact_hash: str = Field(min_length=1)
    score: float = Field(allow_inf_nan=False)
    reward_id: str = Field(min_length=1)
    settled_value: float = Field(allow_inf_nan=False)
    backed_up_value: float = Field(allow_inf_nan=False)

    @model_validator(mode="before")
    @classmethod
    def migrate_legacy_state_value(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        payload = dict(value)
        legacy_state_value = payload.pop("state_value", None)
        if "settled_value" not in payload and legacy_state_value is not None:
            payload["settled_value"] = legacy_state_value
        if "backed_up_value" not in payload and legacy_state_value is not None:
            payload["backed_up_value"] = legacy_state_value
        if "node_id" not in payload:
            candidate_id = str(payload.get("candidate_id") or "candidate")
            iteration = int(payload.get("iteration") or 0)
            commit = str(payload.get("evidence_commit") or "no-commit")
            payload["node_id"] = (
                f"node_{candidate_id}_{iteration:04d}_{commit[:12]}"
            )
        return payload

    @property
    def state_value(self) -> float:
        """Compatibility accessor for pre-value-layer callers."""

        return self.backed_up_value


class ValueBackupTarget(SearchModel):
    candidate_id: str = Field(min_length=1)
    node_id: str = Field(min_length=1)
    distance: int = Field(ge=1)
    return_value: float = Field(allow_inf_nan=False)


class ValueBackupEvent(SearchModel):
    schema_version: Literal[1] = 1
    event_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    source_candidate_id: str = Field(min_length=1)
    source_iteration: int = Field(ge=1)
    source_reward_id: str = Field(min_length=1)
    operator_name: str = Field(min_length=1)
    operator_version: int = Field(ge=1)
    config_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    attempt_reward: float = Field(allow_inf_nan=False)
    settled_value: float = Field(allow_inf_nan=False)
    targets: list[ValueBackupTarget] = Field(min_length=1)
    created_at: str


class AllocationSourceSnapshot(SearchModel):
    candidate_id: str = Field(min_length=1)
    node_id: str = Field(min_length=1)
    iteration: int = Field(ge=1)
    allocation_depth: int = Field(ge=0)
    settled_value: float = Field(allow_inf_nan=False)
    backed_up_value: float = Field(allow_inf_nan=False)
    expansion_visits: int = Field(ge=0)
    exploration_bonus: float = Field(ge=0, allow_inf_nan=False)
    priority: float = Field(allow_inf_nan=False)
    eligible: bool
    ineligible_reason: str | None = None


class AllocationStateSnapshot(SearchModel):
    schema_version: Literal[1] = 1
    trigger_candidate_id: str = Field(min_length=1)
    trigger_iteration: int = Field(ge=1)
    evaluated_attempts: int = Field(ge=1)
    total_evaluated_attempts: int = Field(ge=1)
    recent_attempt_rewards: list[float] = Field(min_length=1)
    recent_mean_reward: float = Field(allow_inf_nan=False)
    recent_standard_error: float = Field(ge=0, allow_inf_nan=False)
    lane_exploration_bonus: float = Field(ge=0, allow_inf_nan=False)
    lane_upper_confidence_bound: float = Field(allow_inf_nan=False)
    nonpositive_fraction: float = Field(ge=0, le=1, allow_inf_nan=False)
    materialized_candidates: int = Field(ge=0)
    pending_expansions: int = Field(ge=0)
    max_candidates: int = Field(gt=0)
    max_unobserved_expansions_per_node: int | None = Field(
        default=None,
        gt=0,
        exclude_if=lambda value: value is None,
    )
    unobserved_expansions_by_node: dict[str, int] | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    exploration_quota_remaining_by_node: dict[str, int] | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    source_options: list[AllocationSourceSnapshot] = Field(default_factory=list)
    created_at: str


class AllocationAction(SearchModel):
    action_id: str = Field(min_length=1)
    kind: Literal["continue_candidate", "retire_candidate", "expand_candidate"]
    candidate_id: str | None = None
    source: ExpansionSource | None = None
    new_candidate_id: str | None = None
    worker_budget: WorkerBudget | None = None
    reason: str = Field(min_length=1)

    @model_validator(mode="after")
    def action_fields_match_kind(self) -> "AllocationAction":
        if self.kind in {"continue_candidate", "retire_candidate"}:
            if self.candidate_id is None:
                raise ValueError(f"{self.kind} requires candidate_id")
            if self.source is not None or self.new_candidate_id is not None:
                raise ValueError(f"{self.kind} cannot declare an expansion source")
        elif self.source is None or self.new_candidate_id is None:
            raise ValueError("expand_candidate requires source and new_candidate_id")
        return self


class AllocationDecision(SearchModel):
    decision_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    trigger_candidate_id: str = Field(min_length=1)
    trigger_iteration: int = Field(ge=1)
    trigger_commit: str = Field(min_length=1)
    trigger_reward_id: str = Field(min_length=1)
    policy_name: str = Field(min_length=1)
    policy_version: int = Field(ge=1)
    config_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    state_snapshot: AllocationStateSnapshot | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    actions: list[AllocationAction] = Field(min_length=1)
    status: AllocationDecisionStatus = "pending"
    created_at: str
    applied_at: str | None = None

    @model_validator(mode="after")
    def allocation_actions_are_unique(self) -> "AllocationDecision":
        action_ids = [action.action_id for action in self.actions]
        if len(action_ids) != len(set(action_ids)):
            raise ValueError("allocation action_id values must be unique")
        child_ids = [
            action.new_candidate_id
            for action in self.actions
            if action.kind == "expand_candidate"
        ]
        if len(child_ids) != len(set(child_ids)):
            raise ValueError("allocation expansion candidate ids must be unique")
        return self


class CandidateTask(SearchModel):
    run_id: str
    candidate_id: str
    parent_id: str | None = None
    parent_candidate_ids: list[str] = Field(default_factory=list)
    base_candidate_id: str | None = None
    plan_id: str | None = None
    hypothesis: str
    workspace: Path
    workspace_backend: WorkspaceBackend = "copy"
    workspace_branch: str | None = None
    workspace_base_revision: str | None = None
    allocation_depth: int = Field(default=0, ge=0)
    expansion_source: ExpansionSource | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    share_out_dir: Path | None = None
    allowed_files: list[str]
    denied_files: list[str]
    instructions: list[str] = Field(default_factory=list)
    expected_artifacts: list[str] = Field(default_factory=list)
    stop_conditions: dict[str, Any] = Field(default_factory=dict)
    proposal: "CandidateProposal | None" = None
    strategy_metadata: dict[str, Any] = Field(default_factory=dict)
    selected_model: SelectedModel | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    model_provenance: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def drop_legacy_shared_dir(cls, value: Any) -> Any:
        if not isinstance(value, dict) or "shared_dir" not in value:
            return value
        payload = dict(value)
        payload.pop("shared_dir", None)
        return payload


class CandidateProposal(SearchModel):
    hypothesis: str | None = None
    intent: str = Field(min_length=1)
    expected_tradeoff: str = ""
    instructions: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class CandidateWorkOrder(SearchModel):
    slot: int = Field(gt=0)
    intent: str = Field(min_length=1)
    hypothesis: str | None = None
    instructions: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class SearchPlan(SearchModel):
    run_id: str
    plan_id: str
    status: Literal["planned", "started"] = "planned"
    strategy: StrategySpec
    requested_k: int = Field(gt=0)
    planned_k: int = Field(ge=0)
    remaining_budget: int = Field(ge=0)
    requires_agent_proposals: bool = False
    worker_policy: dict[str, Any] = Field(default_factory=dict)
    work_orders: list[CandidateWorkOrder] = Field(default_factory=list)
    strategy_trace: dict[str, Any] = Field(default_factory=dict)
    started_candidate_ids: list[str] = Field(default_factory=list)
    selected_models: list[SelectedModel] = Field(
        default_factory=list,
        exclude_if=lambda value: not value,
    )
    created_at: str


class VerifierResult(SearchModel):
    name: str
    role: VerifierRole
    passed: bool
    score: float | None = None
    metrics: dict[str, Any] = Field(default_factory=dict)
    log_path: Path | None = None
    failure_class: str | None = None


class ToolizationDecision(SearchModel):
    outcome: Literal["staged", "not_applicable"]
    signals: list[ToolizationSignal] = Field(default_factory=list, max_length=4)
    exclusion: ToolizationExclusion | None = None
    rationale: str = Field(min_length=1, max_length=1000)
    tool_names: list[str] = Field(default_factory=list, max_length=16)

    @field_validator("rationale")
    @classmethod
    def rationale_must_be_nonempty(cls, value: str) -> str:
        normalized = " ".join(value.split()).strip()
        if not normalized:
            raise ValueError("toolization rationale must be non-empty")
        return normalized

    @field_validator("signals")
    @classmethod
    def signals_must_be_unique(
        cls, value: list[ToolizationSignal]
    ) -> list[ToolizationSignal]:
        if len(value) != len(set(value)):
            raise ValueError("toolization signals must be unique")
        return value

    @field_validator("tool_names")
    @classmethod
    def tool_names_must_be_unique_and_nonempty(
        cls, value: list[str]
    ) -> list[str]:
        normalized = [" ".join(item.split()).strip() for item in value]
        if any(not item for item in normalized):
            raise ValueError("toolization tool_names must be non-empty")
        if any(len(item) > 120 for item in normalized):
            raise ValueError("toolization tool_names must not exceed 120 characters")
        if len(normalized) != len(set(normalized)):
            raise ValueError("toolization tool_names must be unique")
        return normalized

    @model_validator(mode="after")
    def outcome_matches_evidence(self) -> "ToolizationDecision":
        if self.outcome == "staged":
            if not self.signals:
                raise ValueError("staged toolization requires at least one signal")
            if self.exclusion is not None:
                raise ValueError("staged toolization cannot declare an exclusion")
            if not self.tool_names:
                raise ValueError("staged toolization requires at least one tool name")
        else:
            if self.exclusion is None:
                raise ValueError("not_applicable toolization requires an exclusion")
            if self.tool_names:
                raise ValueError("not_applicable toolization cannot declare tool names")
        return self


class ScoreReport(SearchModel):
    run_id: str
    candidate_id: str
    parent_id: str | None = None
    validity_passed: bool
    process_passed: bool
    promotion_passed: bool | None = None
    aggregate_score: float | None = None
    verifier_results: list[VerifierResult]
    touched_denied_files: bool = False
    changed_outside_allowed: bool = False
    hardcoding_suspected: bool = False
    disposition: IterationDisposition | None = None
    best_iteration: int | None = Field(default=None, ge=1)
    best_git_head: str | None = None
    workspace_git_head_after_settlement: str | None = None
    shared_tool_staged_entries: list[str] | None = None
    shared_tool_staged_file_count: int | None = Field(default=None, ge=0)
    shared_tool_staged_bytes: int | None = Field(default=None, ge=0)
    shared_tool_publish_status: SharedToolPublishStatus | None = None
    shared_tool_errors: list[str] | None = None
    shared_tool_consumed_entries: list[str] | None = None
    shared_tool_deduplicated_entries: list[str] | None = None
    toolization_decision: ToolizationDecision | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    toolization_advisories: list[ToolizationAdvisory] = Field(
        default_factory=list,
        exclude_if=lambda value: not value,
    )
    reward_evaluation: RewardEvaluation | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    allocation_decision: AllocationDecision | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )


class PromotionEvidence(SearchModel):
    candidate_id: str
    selected_git_head: str | None = None
    git_head: str | None = None
    artifact_hash: str
    passed: bool
    created_at: str


class IterationRecord(SearchModel):
    iteration: int
    agent_session_id: str | None = None
    selected_model: str | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    exact_model_ref: str | None = None
    adapter_version: str | None = None
    model_provenance: dict[str, Any] = Field(default_factory=dict)
    score: float | None = None
    process_passed: bool | None = None
    git_head: str | None = None
    attempt_base_git_head: str | None = None
    attempt_changed_files: list[str] = Field(default_factory=list)
    ledger_git_head: str | None = None
    git_artifact_clean: bool | None = None
    git_status: list[str] = Field(default_factory=list)
    failure_class: str | None = None
    summary: str = ""
    hypothesis: str = ""
    changed_files: list[str] = Field(default_factory=list)
    touched_denied_files: bool = False
    changed_outside_allowed: bool = False
    artifact_hash: str | None = None
    metrics: dict[str, Any] = Field(default_factory=dict)
    log_paths: list[str] = Field(default_factory=list)
    disposition: IterationDisposition | None = None
    reward_evaluation: RewardEvaluation | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    value_backup_event_id: str | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    value_backup_error: str | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    allocation_decision_id: str | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    allocation_decision_error: str | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    restored_to_iteration: int | None = Field(default=None, ge=1)
    restored_to_git_head: str | None = None
    workspace_git_head_after_settlement: str | None = None
    adopted_tools: list[ToolAdoptionRecord] = Field(default_factory=list, exclude_if=lambda value: not value)
    adoption_confounded: bool | None = Field(default=None, exclude_if=lambda value: value is None)
    shared_tools: list[SharedToolRecord] = Field(default_factory=list)
    shared_tool_errors: list[str] = Field(default_factory=list)
    shared_tool_staged_entries: list[str] = Field(default_factory=list)
    shared_tool_staged_file_count: int = Field(default=0, ge=0)
    shared_tool_staged_bytes: int = Field(default=0, ge=0)
    shared_tool_consumed_entries: list[str] = Field(default_factory=list)
    shared_tool_deduplicated_entries: list[str] = Field(default_factory=list)
    shared_tool_publish_status: SharedToolPublishStatus = "legacy_unknown"
    toolization_decision: ToolizationDecision | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    toolization_advisories: list[ToolizationAdvisory] = Field(
        default_factory=list,
        exclude_if=lambda value: not value,
    )
    created_at: str

    @model_validator(mode="before")
    @classmethod
    def infer_legacy_shared_tool_publish_status(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        payload = dict(value)
        if "shared_tools" not in payload and "shared_assets" in payload:
            payload["shared_tools"] = payload.pop("shared_assets")
        if "shared_tool_publish_status" not in payload:
            tools = payload.get("shared_tools") or []
            errors = payload.get("shared_tool_errors") or []
            if tools:
                payload["shared_tool_publish_status"] = "partially_published" if errors else "published"
            elif errors:
                payload["shared_tool_publish_status"] = "snapshot_error"
        return payload


class ResultLedgerEntry(SearchModel):
    source_run_id: str
    source_candidate_id: str
    iteration: int | None = Field(default=None, ge=1)
    git_head: str | None = None
    ledger_git_head: str | None = None
    metric_name: str = Field(min_length=1)
    score: float | None = None
    status: str = Field(min_length=1)
    hypothesis: str = ""
    failure_class: str | None = None
    created_at: str | None = None


class RunSummary(SearchModel):
    run_id: str
    state: RunState
    frozen_spec_id: str
    candidates_total: int
    candidates_evaluated: int
    best_candidate_id: str | None = None
    best_score: float | None = None
    budget_used: dict[str, Any] = Field(default_factory=dict)
    source_run_id: str | None = None
    invalidated_at: str | None = None
    invalidation_reason: VerifierInvalidationReason | None = None
    replacement_run_id: str | None = None


class BestArtifactRecord(SearchModel):
    schema_version: Literal[1] = 1
    run_id: str
    candidate_id: str
    iteration: int = Field(ge=1)
    commit: str = Field(min_length=1)
    score: float
    metric_name: str = Field(min_length=1)
    metric_direction: Literal["minimize", "maximize"]
    artifact_hash: str = Field(min_length=1)
    workspace: str = Field(min_length=1)
    changed_files: list[str] = Field(default_factory=list)
    updated_at: str


class RunRecord(SearchModel):
    run_id: str
    state: RunState
    frozen_spec_id: str
    source_path: str
    created_at: str
    next_candidate_index: int = 1
    next_plan_index: int = 1
    next_agent_session_index: int = 1
    next_allocation_decision_index: int = 1
    candidates_total: int = 0
    candidates_evaluated: int = 0
    best_candidate_id: str | None = None
    best_score: float | None = None
    selected_candidate_id: str | None = None
    selected_score: float | None = None
    selected_iteration: int | None = None
    selected_git_head: str | None = None
    selected_artifact_hash: str | None = None
    budget_used: dict[str, Any] = Field(default_factory=dict)
    source_run_id: str | None = None
    inherited_research: dict[str, Any] = Field(default_factory=dict)
    invalidated_at: str | None = None
    invalidation_reason: VerifierInvalidationReason | None = None
    invalidation_summary: str | None = None
    invalidation_evidence: list[dict[str, Any]] = Field(default_factory=list)
    replacement_run_id: str | None = None
    selected_models: list[SelectedModel] = Field(
        default_factory=list,
        exclude_if=lambda value: not value,
    )


class CandidateRecord(SearchModel):
    candidate_id: str
    status: Literal["created", "evaluated", "failed"]
    task: CandidateTask
    allocation_eligibility: AllocationEligibility = "eligible"
    retired_by_decision_id: str | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    detected_changed_files: list[str] = Field(default_factory=list)
    touched_denied_files: bool = False
    changed_outside_allowed: bool = False
    score_report: ScoreReport | None = None
    promotion_report: ScoreReport | None = None
    promotion_evidence: PromotionEvidence | None = None
    pending_tool_copies: list[ToolCopyReceipt] = Field(default_factory=list, exclude_if=lambda value: not value)
    iterations: list[IterationRecord] = Field(default_factory=list)
    results_ledger: list[ResultLedgerEntry] = Field(default_factory=list)
    results_ledger_git_head: str | None = None


class AgentSessionRecord(SearchModel):
    agent_session_id: str
    run_id: str
    candidate_id: str
    host: AgentHostKind = "codex"
    host_handle: AgentHostHandle = Field(default_factory=AgentHostHandle)
    created_at: str
    updated_at: str
    directive: dict[str, Any] = Field(default_factory=dict)
    selected_model: SelectedModel | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    model_provenance: dict[str, Any] = Field(default_factory=dict)
    workspace: Path
    launch: dict[str, Any] = Field(default_factory=dict)
    counters: dict[str, int] = Field(default_factory=dict)
    global_evidence_reads: list[GlobalEvidenceReadRecord] = Field(
        default_factory=list
    )
