from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from typing import Protocol

from pydantic import Field

from goal_plus.models import (
    AdaptiveSearchSpec,
    AllocationSourceSnapshot,
    AllocationStateSnapshot,
    CandidateRecord,
    ExpansionSource,
    IterationRecord,
    NodeValueRecord,
    RewardEvaluation,
    SearchModel,
    ValueBackupEvent,
    ValueBackupTarget,
)


class MetricProgressRewardParams(SearchModel):
    score_scale: float = Field(default=1.0, gt=0, allow_inf_nan=False)
    baseline_score: float | None = Field(default=None, allow_inf_nan=False)
    failure_reward: float = Field(default=-1.0, allow_inf_nan=False)


class MetricProgressRewardV2Params(SearchModel):
    baseline_score: float | None = Field(default=None, allow_inf_nan=False)
    epsilon: float = Field(default=1.0e-9, gt=0, allow_inf_nan=False)
    reward_clip: float = Field(default=4.0, gt=0, allow_inf_nan=False)
    value_scale: float = Field(default=1.0, gt=0, allow_inf_nan=False)
    quality_weight: float = Field(default=1.0, ge=0, allow_inf_nan=False)
    regression_weight: float = Field(default=1.0, ge=0, allow_inf_nan=False)
    failure_penalty: float = Field(default=1.0, ge=0, allow_inf_nan=False)
    cost_weight: float = Field(default=0.0, ge=0, allow_inf_nan=False)
    cost_scale_seconds: float = Field(default=60.0, gt=0, allow_inf_nan=False)


class LowRewardReplaceParams(SearchModel):
    min_iterations: int = Field(default=3, ge=1)
    consecutive_below: int = Field(default=2, ge=1)
    reward_threshold: float = Field(default=0.0, allow_inf_nan=False)


class ValueGuidedReplaceParams(SearchModel):
    min_attempts: int = Field(default=3, ge=2)
    window_size: int = Field(default=3, ge=2)
    retire_threshold: float = Field(default=0.1, allow_inf_nan=False)
    exploration_weight: float = Field(default=0.1, ge=0, allow_inf_nan=False)
    uncertainty_weight: float = Field(default=1.0, ge=0, allow_inf_nan=False)
    nonpositive_fraction: float = Field(default=1.0, ge=0, le=1)
    source_exploration_weight: float = Field(
        default=0.1,
        ge=0,
        allow_inf_nan=False,
    )


class IdentityValueBackupParams(SearchModel):
    pass


class DiscountedMeanBestBackupParams(SearchModel):
    discount: float = Field(default=0.9, gt=0, le=1, allow_inf_nan=False)
    attempt_reward_weight: float = Field(
        default=0.5,
        ge=0,
        allow_inf_nan=False,
    )
    best_weight: float = Field(default=0.25, ge=0, le=1, allow_inf_nan=False)


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
    verifier_elapsed_seconds: float = 0.0


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
    state_snapshot: AllocationStateSnapshot | None = None


@dataclass(frozen=True)
class ValueBackupContext:
    run_id: str
    trigger_record: CandidateRecord
    records: list[CandidateRecord]
    reward: RewardEvaluation
    created_at: str


@dataclass(frozen=True)
class _SourceOption:
    source: ExpansionSource
    snapshot: AllocationSourceSnapshot


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


class ValueBackupOperator(Protocol):
    name: str
    version: int

    def validate_params(self, params: dict[str, object]) -> None: ...

    def create_event(
        self,
        context: ValueBackupContext,
        params: dict[str, object],
        *,
        config_hash: str,
    ) -> ValueBackupEvent | None: ...

    def replay(
        self,
        records: list[CandidateRecord],
        events: list[ValueBackupEvent],
        params: dict[str, object],
        *,
        config_hash: str,
    ) -> list[CandidateRecord]: ...


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
            attempt_reward = config.failure_reward
            settled_value = (
                direction * previous / config.score_scale
                if previous is not None
                else None
            )
            components["failure_reward"] = attempt_reward
        else:
            settled_score = (
                attempt
                if context.disposition in {"keep", "retain"}
                else previous
            )
            baseline = previous
            if baseline is None:
                baseline = (
                    config.baseline_score
                    if config.baseline_score is not None
                    else attempt
                )
            reward_score = settled_score if settled_score is not None else attempt
            attempt_reward = (
                direction * (reward_score - baseline) / config.score_scale
            )
            settled_value = (
                direction * settled_score / config.score_scale
                if settled_score is not None
                else None
            )
            components["reward_baseline_score"] = baseline
            if settled_score is not None:
                components["settled_score"] = settled_score

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
            attempt_reward=attempt_reward,
            settled_value=settled_value,
            components=components,
            created_at=context.created_at,
        )


class MetricProgressRewardV2Evaluator:
    name = "metric_progress"
    version = 2

    def validate_params(self, params: dict[str, object]) -> None:
        MetricProgressRewardV2Params.model_validate(params)

    def evaluate(
        self,
        context: RewardContext,
        params: dict[str, object],
        *,
        config_hash: str,
    ) -> RewardEvaluation:
        config = MetricProgressRewardV2Params.model_validate(params)
        direction = 1.0 if context.metric_direction == "maximize" else -1.0
        previous = context.previous_best_score
        attempt = context.attempt_score
        baseline = previous
        if baseline is None:
            baseline = (
                config.baseline_score
                if config.baseline_score is not None
                else attempt
            )

        raw_quality_delta = 0.0
        normalized_quality_delta = 0.0
        if context.process_passed and attempt is not None and baseline is not None:
            raw_quality_delta = direction * (attempt - baseline)
            scale = max(abs(baseline), abs(attempt), config.epsilon)
            normalized_quality_delta = math.copysign(
                math.log1p(abs(raw_quality_delta) / scale),
                raw_quality_delta,
            )
            if normalized_quality_delta < 0:
                normalized_quality_delta *= config.regression_weight

        quality_term = config.quality_weight * normalized_quality_delta
        failure_penalty = (
            0.0 if context.process_passed and attempt is not None
            else config.failure_penalty
        )
        elapsed = max(0.0, context.verifier_elapsed_seconds)
        cost_penalty = config.cost_weight * math.log1p(
            elapsed / config.cost_scale_seconds
        )
        unclipped_reward = quality_term - failure_penalty - cost_penalty
        attempt_reward = max(
            -config.reward_clip,
            min(config.reward_clip, unclipped_reward),
        )

        settled_score = (
            attempt
            if context.process_passed
            and attempt is not None
            and context.disposition in {"keep", "retain"}
            else previous
        )
        if settled_score is None and context.process_passed and attempt is not None:
            settled_score = attempt
        settled_value = (
            direction * math.asinh(settled_score / config.value_scale)
            if settled_score is not None
            else None
        )

        components: dict[str, float] = {
            "direction": direction,
            "raw_quality_delta": raw_quality_delta,
            "normalized_quality_delta": normalized_quality_delta,
            "quality_term": quality_term,
            "failure_penalty": failure_penalty,
            "verifier_elapsed_seconds": elapsed,
            "cost_penalty": cost_penalty,
            "unclipped_attempt_reward": unclipped_reward,
            "reward_clip": config.reward_clip,
            "value_scale": config.value_scale,
        }
        if baseline is not None:
            components["reward_baseline_score"] = baseline
        if attempt is not None:
            components["attempt_score"] = attempt
        if settled_score is not None:
            components["settled_score"] = settled_score

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
            attempt_reward=attempt_reward,
            settled_value=settled_value,
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
            and iteration.reward_evaluation.attempt_reward is not None
        ]
        if len(evaluations) < config.min_iterations:
            return None
        recent = evaluations[-config.consecutive_below :]
        if len(recent) < config.consecutive_below or any(
            item.attempt_reward is None
            or item.attempt_reward > config.reward_threshold
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


class ValueGuidedReplacePolicy:
    name = "value_guided_replace"
    version = 1

    def validate_params(self, params: dict[str, object]) -> None:
        config = ValueGuidedReplaceParams.model_validate(params)
        if config.window_size > config.min_attempts:
            raise ValueError("allocation window_size must be <= min_attempts")

    def decide(
        self,
        context: AllocationContext,
        params: dict[str, object],
    ) -> AllocationRecommendation | None:
        config = ValueGuidedReplaceParams.model_validate(params)
        record = context.trigger_record
        if record.allocation_eligibility != "eligible":
            return None
        evaluations = _evaluated_rewards(record)
        if len(evaluations) < config.min_attempts:
            return None

        recent = evaluations[-config.window_size :]
        recent_rewards = [
            float(item.attempt_reward)
            for item in recent
            if item.attempt_reward is not None
        ]
        if len(recent_rewards) < config.window_size:
            return None
        recent_mean = sum(recent_rewards) / len(recent_rewards)
        if len(recent_rewards) > 1:
            variance = sum(
                (value - recent_mean) ** 2 for value in recent_rewards
            ) / (len(recent_rewards) - 1)
            standard_error = math.sqrt(variance / len(recent_rewards))
        else:  # pragma: no cover - window_size validation keeps this unreachable
            standard_error = 0.0
        total_evaluated = sum(
            len(_evaluated_rewards(item)) for item in context.records
        )
        exploration_bonus = config.exploration_weight * math.sqrt(
            math.log(total_evaluated + 1.0) / len(evaluations)
        )
        upper_bound = (
            recent_mean
            + config.uncertainty_weight * standard_error
            + exploration_bonus
        )
        nonpositive = sum(value <= 0.0 for value in recent_rewards) / len(
            recent_rewards
        )
        source_options = _source_options(
            context.records,
            max_depth=context.adaptive_spec.expansion.max_depth,
            exploration_weight=config.source_exploration_weight,
            include_retired=True,
        )
        snapshot = AllocationStateSnapshot(
            trigger_candidate_id=record.candidate_id,
            trigger_iteration=record.iterations[-1].iteration,
            evaluated_attempts=len(evaluations),
            total_evaluated_attempts=total_evaluated,
            recent_attempt_rewards=recent_rewards,
            recent_mean_reward=recent_mean,
            recent_standard_error=standard_error,
            lane_exploration_bonus=exploration_bonus,
            lane_upper_confidence_bound=upper_bound,
            nonpositive_fraction=nonpositive,
            materialized_candidates=len(context.records),
            pending_expansions=context.pending_expansions,
            max_candidates=context.max_candidates,
            source_options=[item.snapshot for item in source_options],
            created_at=record.iterations[-1].created_at,
        )
        if (
            nonpositive < config.nonpositive_fraction
            or upper_bound > config.retire_threshold
            or len(context.records) + context.pending_expansions
            >= context.max_candidates
        ):
            return None
        source = next(
            (item.source for item in source_options if item.snapshot.eligible),
            None,
        )
        if source is None:
            return None
        return AllocationRecommendation(
            retire_candidate_id=record.candidate_id,
            source=source,
            reason=(
                f"lane upper confidence bound {upper_bound:.6g} was <= "
                f"{config.retire_threshold:.6g} with {nonpositive:.3f} "
                "nonpositive recent reward fraction"
            ),
            state_snapshot=snapshot,
        )


class IdentityValueBackupOperator:
    name = "identity"
    version = 1

    def validate_params(self, params: dict[str, object]) -> None:
        IdentityValueBackupParams.model_validate(params)

    def create_event(
        self,
        context: ValueBackupContext,
        params: dict[str, object],
        *,
        config_hash: str,
    ) -> ValueBackupEvent | None:
        IdentityValueBackupParams.model_validate(params)
        return None

    def replay(
        self,
        records: list[CandidateRecord],
        events: list[ValueBackupEvent],
        params: dict[str, object],
        *,
        config_hash: str,
    ) -> list[CandidateRecord]:
        IdentityValueBackupParams.model_validate(params)
        _reset_node_values(records, self.name, self.version, config_hash)
        return records


class DiscountedMeanBestBackupOperator:
    name = "discounted_mean_best"
    version = 1

    def validate_params(self, params: dict[str, object]) -> None:
        DiscountedMeanBestBackupParams.model_validate(params)

    def create_event(
        self,
        context: ValueBackupContext,
        params: dict[str, object],
        *,
        config_hash: str,
    ) -> ValueBackupEvent | None:
        config = DiscountedMeanBestBackupParams.model_validate(params)
        reward = context.reward
        source = context.trigger_record.task.expansion_source
        if (
            source is None
            or reward.status != "evaluated"
            or reward.attempt_reward is None
            or reward.settled_value is None
        ):
            return None

        records_by_id = {item.candidate_id: item for item in context.records}
        observed_value = (
            reward.settled_value
            + config.attempt_reward_weight * reward.attempt_reward
        )
        targets: list[ValueBackupTarget] = []
        seen_nodes: set[str] = set()
        distance = 1
        while source is not None and source.node_id not in seen_nodes:
            seen_nodes.add(source.node_id)
            source_record = records_by_id.get(source.candidate_id)
            if source_record is None:
                break
            node = next(
                (
                    item for item in source_record.node_values
                    if item.node_id == source.node_id
                ),
                None,
            )
            if node is None:
                break
            discounted_return = node.settled_value + (
                config.discount ** (distance - 1)
            ) * (observed_value - node.settled_value)
            targets.append(
                ValueBackupTarget(
                    candidate_id=source_record.candidate_id,
                    node_id=node.node_id,
                    distance=distance,
                    return_value=discounted_return,
                )
            )
            source = source_record.task.expansion_source
            distance += 1
        if not targets:
            return None

        digest = hashlib.sha256(
            (
                context.run_id
                + reward.reward_id
                + config_hash
                + "|".join(item.node_id for item in targets)
            ).encode("utf-8")
        ).hexdigest()[:12]
        return ValueBackupEvent(
            event_id=(
                f"backup_{context.trigger_record.candidate_id}_"
                f"{context.trigger_record.iterations[-1].iteration:04d}_{digest}"
            ),
            run_id=context.run_id,
            source_candidate_id=context.trigger_record.candidate_id,
            source_iteration=context.trigger_record.iterations[-1].iteration,
            source_reward_id=reward.reward_id,
            operator_name=self.name,
            operator_version=self.version,
            config_hash=config_hash,
            attempt_reward=reward.attempt_reward,
            settled_value=reward.settled_value,
            targets=targets,
            created_at=context.created_at,
        )

    def replay(
        self,
        records: list[CandidateRecord],
        events: list[ValueBackupEvent],
        params: dict[str, object],
        *,
        config_hash: str,
    ) -> list[CandidateRecord]:
        config = DiscountedMeanBestBackupParams.model_validate(params)
        _reset_node_values(records, self.name, self.version, config_hash)
        nodes = {
            node.node_id: node
            for record in records
            for node in record.node_values
        }
        last_updated: dict[str, str] = {}
        for event in sorted(events, key=lambda item: (item.created_at, item.event_id)):
            if (
                event.operator_name != self.name
                or event.operator_version != self.version
                or event.config_hash != config_hash
            ):
                continue
            for target in event.targets:
                node = nodes.get(target.node_id)
                if node is None:
                    continue
                count = node.backup_count + 1
                mean_return = (
                    target.return_value
                    if node.backup_mean_return is None
                    else node.backup_mean_return
                    + (target.return_value - node.backup_mean_return) / count
                )
                best_return = (
                    target.return_value
                    if node.backup_best_return is None
                    else max(node.backup_best_return, target.return_value)
                )
                mean_with_self = (
                    node.settled_value + count * mean_return
                ) / (count + 1)
                best_with_self = max(node.settled_value, best_return)
                node.backup_count = count
                node.backup_mean_return = mean_return
                node.backup_best_return = best_return
                node.backed_up_value = (
                    (1.0 - config.best_weight) * mean_with_self
                    + config.best_weight * best_with_self
                )
                last_updated[node.node_id] = event.created_at
        for node_id, updated_at in last_updated.items():
            nodes[node_id].updated_at = updated_at
        return records


_REWARD_EVALUATORS: dict[tuple[str, int], RewardEvaluator] = {
    (MetricProgressRewardEvaluator.name, MetricProgressRewardEvaluator.version): (
        MetricProgressRewardEvaluator()
    ),
    (MetricProgressRewardV2Evaluator.name, MetricProgressRewardV2Evaluator.version): (
        MetricProgressRewardV2Evaluator()
    ),
}
_ALLOCATION_POLICIES: dict[tuple[str, int], AllocationPolicy] = {
    (LowRewardReplacePolicy.name, LowRewardReplacePolicy.version): (
        LowRewardReplacePolicy()
    ),
    (ValueGuidedReplacePolicy.name, ValueGuidedReplacePolicy.version): (
        ValueGuidedReplacePolicy()
    ),
}
_VALUE_BACKUP_OPERATORS: dict[tuple[str, int], ValueBackupOperator] = {
    (IdentityValueBackupOperator.name, IdentityValueBackupOperator.version): (
        IdentityValueBackupOperator()
    ),
    (
        DiscountedMeanBestBackupOperator.name,
        DiscountedMeanBestBackupOperator.version,
    ): DiscountedMeanBestBackupOperator(),
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
    backup = _value_backup_operator(
        spec.value_backup.name,
        spec.value_backup.version,
    )
    backup.validate_params(spec.value_backup.params)
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


def value_backup_config_hash(spec: AdaptiveSearchSpec) -> str:
    config = spec.value_backup
    return adaptive_component_hash(config.name, config.version, config.params)


def create_value_backup_event(
    adaptive_spec: AdaptiveSearchSpec,
    context: ValueBackupContext,
) -> ValueBackupEvent | None:
    config = adaptive_spec.value_backup
    config_hash = value_backup_config_hash(adaptive_spec)
    operator = _value_backup_operator(config.name, config.version)
    return operator.create_event(context, config.params, config_hash=config_hash)


def replay_value_backups(
    adaptive_spec: AdaptiveSearchSpec,
    records: list[CandidateRecord],
    events: list[ValueBackupEvent],
) -> list[CandidateRecord]:
    config = adaptive_spec.value_backup
    config_hash = value_backup_config_hash(adaptive_spec)
    for record in records:
        record.node_values = materialize_node_values(record, adaptive_spec)
    operator = _value_backup_operator(config.name, config.version)
    return operator.replay(
        records,
        events,
        config.params,
        config_hash=config_hash,
    )


def materialize_node_values(
    record: CandidateRecord,
    adaptive_spec: AdaptiveSearchSpec | None = None,
) -> list[NodeValueRecord]:
    """Backfill immutable settled nodes without changing existing backup state."""

    backup_name = "identity"
    backup_version = 1
    backup_hash: str | None = None
    if adaptive_spec is not None:
        backup_name = adaptive_spec.value_backup.name
        backup_version = adaptive_spec.value_backup.version
        backup_hash = value_backup_config_hash(adaptive_spec)
    nodes = list(record.node_values)
    existing_iterations = {node.iteration for node in nodes}
    for iteration in record.iterations:
        reward = iteration.reward_evaluation
        if (
            iteration.iteration in existing_iterations
            or iteration.disposition not in {"keep", "retain"}
            or iteration.process_passed is not True
            or iteration.git_head is None
            or reward is None
            or reward.status != "evaluated"
            or reward.settled_value is None
        ):
            continue
        digest = hashlib.sha256(
            (iteration.git_head + reward.reward_id).encode("utf-8")
        ).hexdigest()[:12]
        nodes.append(
            NodeValueRecord(
                node_id=(
                    f"node_{record.candidate_id}_{iteration.iteration:04d}_{digest}"
                ),
                candidate_id=record.candidate_id,
                iteration=iteration.iteration,
                evidence_commit=iteration.git_head,
                reward_id=reward.reward_id,
                settled_value=reward.settled_value,
                backed_up_value=reward.settled_value,
                backup_operator_name=backup_name,
                backup_operator_version=backup_version,
                backup_config_hash=backup_hash,
                created_at=iteration.created_at,
                updated_at=iteration.created_at,
            )
        )
    return sorted(nodes, key=lambda item: item.iteration)


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


def _value_backup_operator(name: str, version: int) -> ValueBackupOperator:
    try:
        return _VALUE_BACKUP_OPERATORS[(name, version)]
    except KeyError as exc:
        raise ValueError(
            f"unsupported value backup operator: {name}/v{version}"
        ) from exc


def _evaluated_rewards(record: CandidateRecord) -> list[RewardEvaluation]:
    return [
        iteration.reward_evaluation
        for iteration in record.iterations
        if iteration.reward_evaluation is not None
        and iteration.reward_evaluation.status == "evaluated"
        and iteration.reward_evaluation.attempt_reward is not None
    ]


def _reset_node_values(
    records: list[CandidateRecord],
    operator_name: str,
    operator_version: int,
    config_hash: str,
) -> None:
    for record in records:
        record.node_values = [
            node.model_copy(
                update={
                    "backed_up_value": node.settled_value,
                    "backup_operator_name": operator_name,
                    "backup_operator_version": operator_version,
                    "backup_config_hash": config_hash,
                    "backup_count": 0,
                    "backup_mean_return": None,
                    "backup_best_return": None,
                    "updated_at": node.created_at,
                }
            )
            for node in record.node_values
        ]


def _source_options(
    records: list[CandidateRecord],
    *,
    max_depth: int,
    exploration_weight: float,
    include_retired: bool,
) -> list[_SourceOption]:
    expansion_visits: dict[str, int] = {}
    for record in records:
        source = record.task.expansion_source
        if source is not None:
            expansion_visits[source.node_id] = (
                expansion_visits.get(source.node_id, 0) + 1
            )
    total_expansions = sum(expansion_visits.values())
    options: list[_SourceOption] = []
    for record in records:
        if record.results_ledger_git_head is None:
            continue
        iterations = {item.iteration: item for item in record.iterations}
        for node in materialize_node_values(record):
            iteration = iterations.get(node.iteration)
            if (
                iteration is None
                or iteration.disposition not in {"keep", "retain"}
                or iteration.process_passed is not True
                or iteration.score is None
                or iteration.git_head is None
                or iteration.artifact_hash is None
            ):
                continue
            ineligible_reason = None
            if record.task.allocation_depth >= max_depth:
                ineligible_reason = "max_depth"
            elif (
                not include_retired
                and record.allocation_eligibility != "eligible"
            ):
                ineligible_reason = "retired"
            visits = expansion_visits.get(node.node_id, 0)
            exploration_bonus = exploration_weight * math.sqrt(
                math.log(total_expansions + 2.0) / (visits + 1.0)
            )
            priority = node.backed_up_value + exploration_bonus
            source = ExpansionSource(
                node_id=node.node_id,
                candidate_id=record.candidate_id,
                iteration=iteration.iteration,
                evidence_commit=iteration.git_head,
                settled_commit=record.results_ledger_git_head,
                artifact_hash=iteration.artifact_hash,
                score=iteration.score,
                reward_id=node.reward_id,
                settled_value=node.settled_value,
                backed_up_value=node.backed_up_value,
            )
            snapshot = AllocationSourceSnapshot(
                candidate_id=record.candidate_id,
                node_id=node.node_id,
                iteration=iteration.iteration,
                allocation_depth=record.task.allocation_depth,
                settled_value=node.settled_value,
                backed_up_value=node.backed_up_value,
                expansion_visits=visits,
                exploration_bonus=exploration_bonus,
                priority=priority,
                eligible=ineligible_reason is None,
                ineligible_reason=ineligible_reason,
            )
            options.append(_SourceOption(source=source, snapshot=snapshot))
    return sorted(
        options,
        key=lambda item: (
            not item.snapshot.eligible,
            -item.snapshot.priority,
            item.snapshot.candidate_id,
            -item.snapshot.iteration,
        ),
    )


def _highest_value_source(
    records: list[CandidateRecord],
    *,
    max_depth: int,
) -> ExpansionSource | None:
    options = _source_options(
        records,
        max_depth=max_depth,
        exploration_weight=0.0,
        include_retired=False,
    )
    return next(
        (item.source for item in options if item.snapshot.eligible),
        None,
    )
