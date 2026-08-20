from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Protocol

from pydantic import Field

from goal_plus.models import (
    AdaptiveSearchSpec,
    CandidateRecord,
    ExpansionSource,
    IterationRecord,
    RewardEvaluation,
    SearchModel,
)


class MetricProgressRewardParams(SearchModel):
    score_scale: float = Field(default=1.0, gt=0, allow_inf_nan=False)
    baseline_score: float | None = Field(default=None, allow_inf_nan=False)
    failure_reward: float = Field(default=-1.0, allow_inf_nan=False)


class LowRewardReplaceParams(SearchModel):
    min_iterations: int = Field(default=3, ge=1)
    consecutive_below: int = Field(default=2, ge=1)
    reward_threshold: float = Field(default=0.0, allow_inf_nan=False)


@dataclass(frozen=True)
class RewardContext:
    candidate_id: str
    iteration: int
    commit: str
    process_passed: bool
    disposition: str
    attempt_score: float | None
    previous_best_score: float | None
    metric_direction: str
    created_at: str


@dataclass(frozen=True)
class AllocationContext:
    adaptive_spec: AdaptiveSearchSpec
    trigger_record: CandidateRecord
    records: list[CandidateRecord]
    pending_expansions: int
    max_candidates: int


@dataclass(frozen=True)
class AllocationRecommendation:
    retire_candidate_id: str
    source: ExpansionSource
    reason: str


class RewardEvaluator(Protocol):
    name: str
    version: int

    def validate_params(self, params: dict[str, object]) -> None: ...

    def evaluate(
        self,
        context: RewardContext,
        params: dict[str, object],
        *,
        config_hash: str,
    ) -> RewardEvaluation: ...


class AllocationPolicy(Protocol):
    name: str
    version: int

    def validate_params(self, params: dict[str, object]) -> None: ...

    def decide(
        self,
        context: AllocationContext,
        params: dict[str, object],
    ) -> AllocationRecommendation | None: ...


class MetricProgressRewardEvaluator:
    name = "metric_progress"
    version = 1

    def validate_params(self, params: dict[str, object]) -> None:
        MetricProgressRewardParams.model_validate(params)

    def evaluate(
        self,
        context: RewardContext,
        params: dict[str, object],
        *,
        config_hash: str,
    ) -> RewardEvaluation:
        config = MetricProgressRewardParams.model_validate(params)
        direction = 1.0 if context.metric_direction == "maximize" else -1.0
        previous = context.previous_best_score
        attempt = context.attempt_score

        components: dict[str, float] = {
            "direction": direction,
            "score_scale": config.score_scale,
        }
        if previous is not None:
            components["previous_best_score"] = previous
        if attempt is not None:
            components["attempt_score"] = attempt

        if not context.process_passed or attempt is None:
            reward = config.failure_reward
            state_value = (
                direction * previous / config.score_scale
                if previous is not None
                else None
            )
            components["failure_reward"] = reward
        else:
            current = (
                attempt
                if context.disposition in {"keep", "retain"}
                else previous
            )
            if current is None:
                current = attempt
            baseline = previous
            if baseline is None:
                baseline = (
                    config.baseline_score
                    if config.baseline_score is not None
                    else current
                )
            reward = direction * (current - baseline) / config.score_scale
            state_value = direction * current / config.score_scale
            components["current_best_score"] = current
            components["reward_baseline_score"] = baseline

        reward_id = (
            f"reward_{context.candidate_id}_{context.iteration:04d}_"
            f"{hashlib.sha256((context.commit + config_hash).encode('utf-8')).hexdigest()[:12]}"
        )
        return RewardEvaluation(
            reward_id=reward_id,
            evaluator_name=self.name,
            evaluator_version=self.version,
            config_hash=config_hash,
            status="evaluated",
            reward=reward,
            state_value=state_value,
            components=components,
            created_at=context.created_at,
        )


class LowRewardReplacePolicy:
    name = "low_reward_replace"
    version = 1

    def validate_params(self, params: dict[str, object]) -> None:
        config = LowRewardReplaceParams.model_validate(params)
        if config.consecutive_below > config.min_iterations:
            raise ValueError(
                "allocation consecutive_below must be <= min_iterations"
            )

    def decide(
        self,
        context: AllocationContext,
        params: dict[str, object],
    ) -> AllocationRecommendation | None:
        config = LowRewardReplaceParams.model_validate(params)
        record = context.trigger_record
        if record.allocation_eligibility != "eligible":
            return None
        evaluations = [
            iteration.reward_evaluation
            for iteration in record.iterations
            if iteration.reward_evaluation is not None
            and iteration.reward_evaluation.status == "evaluated"
            and iteration.reward_evaluation.reward is not None
        ]
        if len(evaluations) < config.min_iterations:
            return None
        recent = evaluations[-config.consecutive_below :]
        if len(recent) < config.consecutive_below or any(
            item.reward is None or item.reward > config.reward_threshold
            for item in recent
        ):
            return None
        if len(context.records) + context.pending_expansions >= context.max_candidates:
            return None

        source = _highest_value_source(
            context.records,
            max_depth=context.adaptive_spec.expansion.max_depth,
        )
        if source is None:
            return None
        return AllocationRecommendation(
            retire_candidate_id=record.candidate_id,
            source=source,
            reason=(
                f"{config.consecutive_below} consecutive rewards were <= "
                f"{config.reward_threshold} after {len(evaluations)} evaluated iterations"
            ),
        )


_REWARD_EVALUATORS: dict[tuple[str, int], RewardEvaluator] = {
    (MetricProgressRewardEvaluator.name, MetricProgressRewardEvaluator.version): (
        MetricProgressRewardEvaluator()
    ),
}
_ALLOCATION_POLICIES: dict[tuple[str, int], AllocationPolicy] = {
    (LowRewardReplacePolicy.name, LowRewardReplacePolicy.version): (
        LowRewardReplacePolicy()
    ),
}


def adaptive_component_hash(name: str, version: int, params: dict[str, object]) -> str:
    payload = json.dumps(
        {"name": name, "version": version, "params": params},
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def validate_adaptive_search_spec(spec: AdaptiveSearchSpec) -> None:
    reward = _reward_evaluator(spec.reward.name, spec.reward.version)
    reward.validate_params(spec.reward.params)
    policy = _allocation_policy(spec.allocation.name, spec.allocation.version)
    policy.validate_params(spec.allocation.params)


def evaluate_reward(
    adaptive_spec: AdaptiveSearchSpec,
    context: RewardContext,
) -> RewardEvaluation:
    config = adaptive_spec.reward
    config_hash = adaptive_component_hash(config.name, config.version, config.params)
    try:
        evaluator = _reward_evaluator(config.name, config.version)
        return evaluator.evaluate(context, config.params, config_hash=config_hash)
    except Exception as exc:
        reward_id = (
            f"reward_{context.candidate_id}_{context.iteration:04d}_"
            f"{hashlib.sha256((context.commit + config_hash).encode('utf-8')).hexdigest()[:12]}"
        )
        return RewardEvaluation(
            reward_id=reward_id,
            evaluator_name=config.name,
            evaluator_version=config.version,
            config_hash=config_hash,
            status="error",
            error=f"{type(exc).__name__}: {exc}",
            created_at=context.created_at,
        )


def decide_allocation(
    context: AllocationContext,
) -> AllocationRecommendation | None:
    config = context.adaptive_spec.allocation
    policy = _allocation_policy(config.name, config.version)
    return policy.decide(context, config.params)


def allocation_config_hash(spec: AdaptiveSearchSpec) -> str:
    config = spec.allocation
    return adaptive_component_hash(config.name, config.version, config.params)


def _reward_evaluator(name: str, version: int) -> RewardEvaluator:
    try:
        return _REWARD_EVALUATORS[(name, version)]
    except KeyError as exc:
        raise ValueError(f"unsupported reward evaluator: {name}/v{version}") from exc


def _allocation_policy(name: str, version: int) -> AllocationPolicy:
    try:
        return _ALLOCATION_POLICIES[(name, version)]
    except KeyError as exc:
        raise ValueError(f"unsupported allocation policy: {name}/v{version}") from exc


def _highest_value_source(
    records: list[CandidateRecord],
    *,
    max_depth: int,
) -> ExpansionSource | None:
    options: list[tuple[float, str, int, ExpansionSource]] = []
    for record in records:
        if (
            record.allocation_eligibility != "eligible"
            or record.task.allocation_depth >= max_depth
            or record.results_ledger_git_head is None
        ):
            continue
        for iteration in record.iterations:
            reward = iteration.reward_evaluation
            if (
                iteration.disposition not in {"keep", "retain"}
                or iteration.process_passed is not True
                or iteration.score is None
                or iteration.git_head is None
                or iteration.artifact_hash is None
                or reward is None
                or reward.status != "evaluated"
                or reward.state_value is None
            ):
                continue
            source = ExpansionSource(
                candidate_id=record.candidate_id,
                iteration=iteration.iteration,
                evidence_commit=iteration.git_head,
                settled_commit=record.results_ledger_git_head,
                artifact_hash=iteration.artifact_hash,
                score=iteration.score,
                reward_id=reward.reward_id,
                state_value=reward.state_value,
            )
            options.append(
                (reward.state_value, record.candidate_id, iteration.iteration, source)
            )
    if not options:
        return None
    return sorted(
        options,
        key=lambda item: (-item[0], item[1], -item[2]),
    )[0][3]
