from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
import hashlib
import json
import math
from typing import Protocol

from pydantic import Field

from goal_plus.models import (
    AdaptiveSearchSpec,
    AllocationDecision,
    AllocationSourceSnapshot,
    AllocationStateSnapshot,
    CandidateRecord,
    EdgeValueEstimate,
    ExpansionActionContext,
    ExpansionSource,
    IterationRecord,
    NodeValueEstimate,
    ObservedActionEdge,
    RewardEvaluation,
    SearchGraphProjection,
    SearchModel,
    SearchNodeRecord,
    SearchTransitionRecord,
    ValueProjection,
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
    graph: SearchGraphProjection
    values: ValueProjection
    resources: "AllocationResourceState"


@dataclass(frozen=True)
class AllocationResourceState:
    """Observed capacity plus reserved fields for later search control."""

    materialized_candidates: int
    pending_expansions: int
    max_candidates: int
    frontier_width: int | None = None
    max_frontier_width: int | None = None
    unobserved_expansions_by_node: dict[str, int] = field(default_factory=dict)
    exploration_quota_remaining_by_node: dict[str, int] = field(
        default_factory=dict
    )


@dataclass(frozen=True)
class AllocationRecommendation:
    retire_candidate_id: str
    source: ExpansionSource
    reason: str
    state_snapshot: AllocationStateSnapshot | None = None


@dataclass(frozen=True)
class AllocationReservationPlan:
    """One atomically admitted batch of candidate replacements."""

    recommendations: tuple[AllocationRecommendation, ...]
    reservation_keys: tuple[str, ...]
    resources_after: AllocationResourceState


@dataclass(frozen=True)
class ValueBackupContext:
    run_id: str
    trigger_record: CandidateRecord
    records: list[CandidateRecord]
    graph: SearchGraphProjection
    reward: RewardEvaluation
    created_at: str


@dataclass(frozen=True)
class _SourceOption:
    source: ExpansionSource
    snapshot: AllocationSourceSnapshot


def _replacement_recommendation(
    context: AllocationContext,
    *,
    source: ExpansionSource,
    reason: str,
    state_snapshot: AllocationStateSnapshot | None = None,
) -> AllocationRecommendation:
    return AllocationRecommendation(
        retire_candidate_id=context.trigger_record.candidate_id,
        source=source,
        reason=reason,
        state_snapshot=state_snapshot,
    )


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
        graph: SearchGraphProjection,
        records: list[CandidateRecord],
        events: list[ValueBackupEvent],
        params: dict[str, object],
        *,
        config_hash: str,
    ) -> ValueProjection: ...


class AllocationConstraintEvaluator(Protocol):
    """Extension point for width, quota, and other admission constraints."""

    def validate(
        self,
        context: AllocationContext,
        reservation: AllocationReservationPlan,
    ) -> None: ...


class AllocationReservationPlanner(Protocol):
    """Atomically admit a batch against one allocation resource snapshot."""

    def plan(
        self,
        context: AllocationContext,
        recommendations: tuple[AllocationRecommendation, ...],
    ) -> AllocationReservationPlan: ...


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
        if (
            context.resources.materialized_candidates
            + context.resources.pending_expansions
            >= context.resources.max_candidates
        ):
            return None

        source = _highest_value_source(
            context.graph,
            context.values,
            context.records,
            context.resources,
            max_depth=context.adaptive_spec.expansion.max_depth,
        )
        if source is None:
            return None
        return _replacement_recommendation(
            context,
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
            context.graph,
            context.values,
            context.records,
            context.resources,
            max_depth=context.adaptive_spec.expansion.max_depth,
            exploration_weight=config.source_exploration_weight,
            include_retired=True,
        )
        quota = (
            context.adaptive_spec.expansion.max_unobserved_expansions_per_node
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
            materialized_candidates=context.resources.materialized_candidates,
            pending_expansions=context.resources.pending_expansions,
            max_candidates=context.resources.max_candidates,
            max_unobserved_expansions_per_node=quota,
            unobserved_expansions_by_node=(
                dict(context.resources.unobserved_expansions_by_node)
                if quota is not None
                else None
            ),
            exploration_quota_remaining_by_node=(
                dict(context.resources.exploration_quota_remaining_by_node)
                if quota is not None
                else None
            ),
            source_options=[item.snapshot for item in source_options],
            created_at=record.iterations[-1].created_at,
        )
        if (
            nonpositive < config.nonpositive_fraction
            or upper_bound > config.retire_threshold
            or context.resources.materialized_candidates
            + context.resources.pending_expansions
            >= context.resources.max_candidates
        ):
            return None
        source = next(
            (item.source for item in source_options if item.snapshot.eligible),
            None,
        )
        if source is None:
            return None
        return _replacement_recommendation(
            context,
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
        graph: SearchGraphProjection,
        records: list[CandidateRecord],
        events: list[ValueBackupEvent],
        params: dict[str, object],
        *,
        config_hash: str,
    ) -> ValueProjection:
        IdentityValueBackupParams.model_validate(params)
        projection = _initial_value_projection(
            graph,
            records,
            operator_name=self.name,
            operator_version=self.version,
            config_hash=config_hash,
        )
        nodes = {item.node_id: item for item in graph.nodes}
        for edge in projection.edge_values:
            edge.observed_return = nodes[edge.settled_node_id].settled_value
        return projection


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
        if (
            reward.status != "evaluated"
            or reward.attempt_reward is None
            or reward.settled_value is None
        ):
            return None

        source_iteration = context.trigger_record.iterations[-1].iteration
        transition = next(
            (
                item
                for item in context.graph.transitions
                if item.candidate_id == context.trigger_record.candidate_id
                and item.iteration == source_iteration
            ),
            None,
        )
        if transition is None:
            return None

        nodes = {item.node_id: item for item in context.graph.nodes}
        observed_value = (
            reward.settled_value
            + config.attempt_reward_weight * reward.attempt_reward
        )
        targets: list[ValueBackupTarget] = []
        seen_nodes: set[str] = set()
        source_node_id: str | None = transition.from_node_id
        distance = 1
        while source_node_id is not None and source_node_id not in seen_nodes:
            seen_nodes.add(source_node_id)
            node = nodes.get(source_node_id)
            if node is None:
                break
            if node.settled_value is not None:
                discounted_return = node.settled_value + (
                    config.discount ** (distance - 1)
                ) * (observed_value - node.settled_value)
                targets.append(
                    ValueBackupTarget(
                        candidate_id=node.candidate_id,
                        node_id=node.node_id,
                        distance=distance,
                        return_value=discounted_return,
                    )
                )
            source_node_id = node.parent_node_id
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
                f"{source_iteration:04d}_{digest}"
            ),
            run_id=context.run_id,
            source_candidate_id=context.trigger_record.candidate_id,
            source_iteration=source_iteration,
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
        graph: SearchGraphProjection,
        records: list[CandidateRecord],
        events: list[ValueBackupEvent],
        params: dict[str, object],
        *,
        config_hash: str,
    ) -> ValueProjection:
        config = DiscountedMeanBestBackupParams.model_validate(params)
        projection = _initial_value_projection(
            graph,
            records,
            operator_name=self.name,
            operator_version=self.version,
            config_hash=config_hash,
        )
        nodes = {item.node_id: item for item in projection.node_values}
        edges = {item.transition_id: item for item in projection.edge_values}
        graph_nodes, graph_nodes_by_coordinate = _graph_node_indexes(graph)
        transitions = {
            (item.candidate_id, item.iteration): item
            for item in graph.transitions
        }
        for transition in graph.transitions:
            settled_value = graph_nodes[transition.settled_node_id].settled_value
            if settled_value is not None and transition.attempt_reward is not None:
                edges[transition.transition_id].observed_return = (
                    settled_value
                    + config.attempt_reward_weight * transition.attempt_reward
                )
        for event in sorted(events, key=lambda item: (item.created_at, item.event_id)):
            if (
                event.operator_name != self.name
                or event.operator_version != self.version
                or event.config_hash != config_hash
            ):
                continue
            transition = transitions.get(
                (event.source_candidate_id, event.source_iteration)
            )
            if transition is None:
                raise ValueError(
                    f"backup event source transition is unavailable: {event.event_id}"
                )
            edges[transition.transition_id].observed_return = (
                event.settled_value
                + config.attempt_reward_weight * event.attempt_reward
            )
            for target in event.targets:
                target_node_id = _resolve_backup_target_node_id(
                    target,
                    graph_nodes,
                    graph_nodes_by_coordinate,
                )
                node = nodes.get(target_node_id)
                if node is None:
                    raise ValueError(
                        f"backup event target node is unavailable: {target.node_id}"
                    )
                if node.settled_value is None:
                    raise ValueError(
                        f"backup event target has no settled value: {target.node_id}"
                    )
                if graph_nodes[target_node_id].candidate_id != target.candidate_id:
                    raise ValueError(
                        f"backup event target candidate mismatch: {target.node_id}"
                    )
                count = node.backup_count + 1
                previous_mean = node.backup_mean_return
                if previous_mean is None:
                    mean_return = target.return_value
                    return_m2 = 0.0
                else:
                    delta = target.return_value - previous_mean
                    mean_return = previous_mean + delta / count
                    return_m2 = max(
                        0.0,
                        node.backup_return_m2
                        + delta * (target.return_value - mean_return),
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
                node.observation_count = count
                node.backup_mean_return = mean_return
                node.backup_return_m2 = return_m2
                node.backup_best_return = best_return
                node.expected_gain = mean_return - node.settled_value
                node.backed_up_value = (
                    (1.0 - config.best_weight) * mean_with_self
                    + config.best_weight * best_with_self
                )
                node.uncertainty = (
                    math.sqrt(return_m2 / (count - 1)) / math.sqrt(count)
                    if count > 1
                    else None
                )
                node.updated_at = event.created_at
                projection.updated_at = max(projection.updated_at, event.created_at)
            projection.applied_backup_event_ids.append(event.event_id)
        return projection


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


def adaptive_component_hash(
    name: str,
    version: int,
    params: dict[str, object],
) -> str:
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


def _reserve_allocation_resources(
    resources: AllocationResourceState,
    source: ExpansionSource,
) -> AllocationResourceState:
    unobserved = dict(resources.unobserved_expansions_by_node)
    unobserved[source.node_id] = unobserved.get(source.node_id, 0) + 1
    remaining = dict(resources.exploration_quota_remaining_by_node)
    if source.node_id in remaining:
        remaining[source.node_id] = max(0, remaining[source.node_id] - 1)
    return replace(
        resources,
        pending_expansions=resources.pending_expansions + 1,
        unobserved_expansions_by_node=unobserved,
        exploration_quota_remaining_by_node=remaining,
    )


def decide_allocation_batch(
    context: AllocationContext,
) -> tuple[AllocationRecommendation, ...]:
    """Select a deterministic replacement batch from one resource snapshot."""

    config = context.adaptive_spec.allocation
    policy = _allocation_policy(config.name, config.version)
    primary = policy.decide(context, config.params)
    if primary is None:
        return ()

    recommendations = [primary]
    resources = _reserve_allocation_resources(context.resources, primary.source)
    if config.max_replacements_per_decision == 1:
        return tuple(recommendations)

    for record in sorted(context.records, key=lambda item: item.candidate_id):
        if len(recommendations) >= config.max_replacements_per_decision:
            break
        if (
            record.candidate_id == context.trigger_record.candidate_id
            or record.allocation_eligibility != "eligible"
        ):
            continue
        recommendation = policy.decide(
            replace(
                context,
                trigger_record=record,
                resources=resources,
            ),
            config.params,
        )
        if recommendation is None:
            continue
        recommendations.append(recommendation)
        resources = _reserve_allocation_resources(
            resources,
            recommendation.source,
        )
    return tuple(recommendations)


class AtomicAllocationReservationPlanner:
    """Validate and reserve all resources for one replacement batch."""

    def plan(
        self,
        context: AllocationContext,
        recommendations: tuple[AllocationRecommendation, ...],
    ) -> AllocationReservationPlan:
        if not recommendations:
            raise ValueError("atomic allocation requires at least one replacement")
        maximum = context.adaptive_spec.allocation.max_replacements_per_decision
        if len(recommendations) > maximum:
            raise ValueError("atomic allocation exceeds the configured batch limit")

        records = {record.candidate_id: record for record in context.records}
        graph_nodes = {node.node_id: node for node in context.graph.nodes}
        resources = context.resources
        retired: set[str] = set()
        reservation_keys: list[str] = []
        for recommendation in recommendations:
            candidate_id = recommendation.retire_candidate_id
            if candidate_id in retired:
                raise ValueError(
                    f"candidate {candidate_id} appears twice in one allocation batch"
                )
            record = records.get(candidate_id)
            if record is None or record.allocation_eligibility != "eligible":
                raise ValueError(
                    f"candidate {candidate_id} is not eligible for atomic allocation"
                )
            if (
                resources.materialized_candidates
                + resources.pending_expansions
                >= resources.max_candidates
            ):
                raise ValueError("atomic allocation exceeds max_candidates")

            source = recommendation.source
            source_node = graph_nodes.get(source.node_id)
            if (
                source_node is None
                or source_node.candidate_id != source.candidate_id
                or source_node.iteration != source.iteration
            ):
                raise ValueError(
                    f"allocation source {source.node_id} is not present in the graph"
                )
            if (
                resources.exploration_quota_remaining_by_node.get(source.node_id)
                == 0
            ):
                raise ValueError(
                    f"allocation source {source.node_id} has no exploration quota"
                )

            retired.add(candidate_id)
            reservation_keys.extend(
                (
                    f"candidate:{candidate_id}",
                    f"source:{source.node_id}:{candidate_id}",
                )
            )
            resources = _reserve_allocation_resources(resources, source)

        return AllocationReservationPlan(
            recommendations=recommendations,
            reservation_keys=tuple(reservation_keys),
            resources_after=resources,
        )


def allocation_config_hash(spec: AdaptiveSearchSpec) -> str:
    config = spec.allocation
    return adaptive_component_hash(
        config.name,
        config.version,
        {
            "policy_params": config.params,
            "max_replacements_per_decision": (
                config.max_replacements_per_decision
            ),
        },
    )


def project_allocation_resources(
    adaptive_spec: AdaptiveSearchSpec,
    graph: SearchGraphProjection,
    records: list[CandidateRecord],
    decisions: list[AllocationDecision],
    *,
    max_candidates: int,
) -> AllocationResourceState:
    """Derive allocation capacity from durable candidate and decision facts."""

    materialized_ids = {record.candidate_id for record in records}
    unobserved_ids_by_node: dict[str, set[str]] = {}
    graph_nodes, graph_nodes_by_coordinate = _graph_node_indexes(graph)

    for record in records:
        source = record.task.expansion_source
        if source is not None and not record.iterations:
            source_node_id = _resolve_expansion_source_node_from_indexes(
                source,
                graph_nodes,
                graph_nodes_by_coordinate,
            ).node_id
            unobserved_ids_by_node.setdefault(source_node_id, set()).add(
                record.candidate_id
            )

    unmaterialized_expansion_ids: set[str] = set()
    for decision in decisions:
        for action in decision.actions:
            if action.kind != "expand_candidate":
                continue
            assert action.source is not None
            assert action.new_candidate_id is not None
            child_id = action.new_candidate_id
            if child_id not in materialized_ids:
                unmaterialized_expansion_ids.add(child_id)
                source_node_id = _resolve_expansion_source_node_from_indexes(
                    action.source,
                    graph_nodes,
                    graph_nodes_by_coordinate,
                ).node_id
                unobserved_ids_by_node.setdefault(
                    source_node_id,
                    set(),
                ).add(child_id)

    unobserved_expansions = {
        node_id: len(candidate_ids)
        for node_id, candidate_ids in unobserved_ids_by_node.items()
    }
    quota = adaptive_spec.expansion.max_unobserved_expansions_per_node
    quota_remaining: dict[str, int] = {}
    if quota is not None:
        quota_remaining = {
            node.node_id: max(
                0,
                quota - unobserved_expansions.get(node.node_id, 0),
            )
            for node in graph.nodes
            if node.kind == "settled_iteration"
        }
    return AllocationResourceState(
        materialized_candidates=len(records),
        pending_expansions=len(unmaterialized_expansion_ids),
        max_candidates=max_candidates,
        unobserved_expansions_by_node=unobserved_expansions,
        exploration_quota_remaining_by_node=quota_remaining,
    )


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
    graph: SearchGraphProjection,
    records: list[CandidateRecord],
    events: list[ValueBackupEvent],
) -> ValueProjection:
    config = adaptive_spec.value_backup
    config_hash = value_backup_config_hash(adaptive_spec)
    operator = _value_backup_operator(config.name, config.version)
    return operator.replay(
        graph,
        records,
        events,
        config.params,
        config_hash=config_hash,
    )


def _graph_node_indexes(
    graph: SearchGraphProjection,
) -> tuple[
    dict[str, SearchNodeRecord],
    dict[tuple[str, int], SearchNodeRecord],
]:
    return (
        {item.node_id: item for item in graph.nodes},
        {
            (item.candidate_id, item.iteration): item
            for item in graph.nodes
            if item.kind == "settled_iteration" and item.iteration is not None
        },
    )


def _resolve_expansion_source_node_from_indexes(
    source: ExpansionSource,
    nodes_by_id: Mapping[str, SearchNodeRecord],
    nodes_by_coordinate: Mapping[tuple[str, int], SearchNodeRecord],
) -> SearchNodeRecord:
    node = nodes_by_id.get(source.node_id)
    legacy_alias = node is None
    if node is None:
        legacy_iteration = _legacy_node_iteration(
            source.node_id,
            source.candidate_id,
        )
        if legacy_iteration != source.iteration:
            raise ValueError(
                f"expansion source node is unavailable: {source.node_id}"
            )
        node = nodes_by_coordinate.get((source.candidate_id, source.iteration))
    if node is None or node.kind != "settled_iteration":
        raise ValueError(
            f"expansion source node is unavailable: {source.node_id}"
        )

    expected_facts = {
        "candidate_id": source.candidate_id,
        "iteration": source.iteration,
        "evidence_commit": source.evidence_commit,
        "artifact_hash": source.artifact_hash,
        "score": source.score,
        "reward_id": source.reward_id,
        "settled_value": source.settled_value,
    }
    mismatches = [
        name
        for name, expected in expected_facts.items()
        if getattr(node, name) != expected
    ]
    if not legacy_alias and node.settled_commit != source.settled_commit:
        mismatches.append("settled_commit")
    if mismatches:
        raise ValueError(
            "expansion source does not match the projected node: "
            + ", ".join(mismatches)
        )
    return node


def _legacy_node_iteration(node_id: str, candidate_id: str) -> int | None:
    prefix = f"node_{candidate_id}_"
    if not node_id.startswith(prefix):
        return None
    iteration_text, separator, digest = node_id[len(prefix) :].partition("_")
    if (
        not separator
        or not iteration_text.isdigit()
        or len(digest) != 12
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        return None
    iteration = int(iteration_text)
    return iteration if iteration >= 1 else None


def _resolve_backup_target_node_id(
    target: ValueBackupTarget,
    nodes_by_id: Mapping[str, SearchNodeRecord],
    nodes_by_coordinate: Mapping[tuple[str, int], SearchNodeRecord],
) -> str:
    node = nodes_by_id.get(target.node_id)
    if node is not None:
        return node.node_id

    iteration = _legacy_node_iteration(target.node_id, target.candidate_id)
    if iteration is None:
        return target.node_id
    node = nodes_by_coordinate.get((target.candidate_id, iteration))
    return node.node_id if node is not None else target.node_id


def project_search_graph(
    run_id: str,
    records: list[CandidateRecord],
    *,
    created_at: str,
) -> SearchGraphProjection:
    """Project immutable search nodes and transitions from settled iterations."""

    nodes: list[SearchNodeRecord] = []
    transitions: list[SearchTransitionRecord] = []
    nodes_by_id: dict[str, SearchNodeRecord] = {}
    nodes_by_coordinate: dict[tuple[str, int], SearchNodeRecord] = {}
    current_node_ids: dict[str, str] = {}
    timestamps: list[str] = []

    ordered_records = sorted(
        records,
        key=lambda item: (item.task.allocation_depth, item.candidate_id),
    )
    for record in ordered_records:
        if record.task.run_id != run_id:
            raise ValueError(
                f"candidate {record.candidate_id} belongs to another run"
            )
        source = record.task.expansion_source
        if source is None:
            base_commit = _candidate_base_commit(record)
            root_id = _stable_id(
                "root",
                run_id,
                record.candidate_id,
                base_commit,
            )
            root = SearchNodeRecord(
                node_id=root_id,
                run_id=run_id,
                kind="virtual_root",
                candidate_id=record.candidate_id,
                settled_commit=base_commit,
                transition_depth=0,
                allocation_depth=record.task.allocation_depth,
                created_at=(
                    record.iterations[0].created_at
                    if record.iterations
                    else created_at
                ),
            )
            nodes.append(root)
            nodes_by_id[root_id] = root
            current_node_id = root_id
        else:
            current_node_id = _resolve_expansion_source_node_from_indexes(
                source,
                nodes_by_id,
                nodes_by_coordinate,
            ).node_id

        for position, iteration in enumerate(record.iterations):
            if iteration.disposition is None:
                continue
            reward = iteration.reward_evaluation
            transition_id = _stable_id(
                "transition",
                run_id,
                record.candidate_id,
                str(iteration.iteration),
                iteration.git_head or "no-attempt-commit",
            )
            accepted = (
                iteration.process_passed is True
                and iteration.disposition in {"keep", "retain"}
            )
            accepted_node_id: str | None = None
            settled_node_id = current_node_id
            settled_commit = (
                iteration.workspace_git_head_after_settlement
                or iteration.ledger_git_head
                or iteration.git_head
            )
            if accepted:
                if settled_commit is None:
                    raise ValueError(
                        "accepted iteration requires an exact settlement commit"
                    )
                accepted_node_id = _stable_id(
                    "node",
                    run_id,
                    record.candidate_id,
                    str(iteration.iteration),
                    settled_commit,
                )
                parent = nodes_by_id[current_node_id]
                node = SearchNodeRecord(
                    node_id=accepted_node_id,
                    run_id=run_id,
                    kind="settled_iteration",
                    candidate_id=record.candidate_id,
                    iteration=iteration.iteration,
                    parent_node_id=current_node_id,
                    incoming_transition_id=transition_id,
                    evidence_commit=iteration.git_head,
                    settled_commit=settled_commit,
                    artifact_hash=iteration.artifact_hash,
                    score=iteration.score,
                    reward_id=reward.reward_id if reward is not None else None,
                    settled_value=(
                        reward.settled_value if reward is not None else None
                    ),
                    transition_depth=parent.transition_depth + 1,
                    allocation_depth=record.task.allocation_depth,
                    created_at=iteration.created_at,
                )
                nodes.append(node)
                nodes_by_id[node.node_id] = node
                nodes_by_coordinate[(node.candidate_id, iteration.iteration)] = node
                settled_node_id = node.node_id

            kind = "candidate_continuation"
            if position == 0:
                kind = (
                    "derived_candidate_start"
                    if source is not None
                    else "initial_attempt"
                )
            transitions.append(
                SearchTransitionRecord(
                    transition_id=transition_id,
                    run_id=run_id,
                    candidate_id=record.candidate_id,
                    iteration=iteration.iteration,
                    kind=kind,
                    from_node_id=current_node_id,
                    accepted_node_id=accepted_node_id,
                    settled_node_id=settled_node_id,
                    attempt_base_commit=iteration.attempt_base_git_head,
                    attempt_commit=iteration.git_head,
                    settled_commit=settled_commit,
                    disposition=iteration.disposition,
                    process_passed=iteration.process_passed is True,
                    score=iteration.score,
                    reward_id=reward.reward_id if reward is not None else None,
                    attempt_reward=(
                        reward.attempt_reward if reward is not None else None
                    ),
                    created_at=iteration.created_at,
                )
            )
            timestamps.append(iteration.created_at)
            if accepted_node_id is not None:
                current_node_id = accepted_node_id
        current_node_ids[record.candidate_id] = current_node_id

    graph_created_at = min(timestamps) if timestamps else created_at
    graph_updated_at = max(timestamps) if timestamps else created_at
    return SearchGraphProjection(
        run_id=run_id,
        revision=len(transitions) + len(current_node_ids),
        nodes=nodes,
        transitions=transitions,
        candidate_current_node_ids=current_node_ids,
        created_at=graph_created_at,
        updated_at=graph_updated_at,
    )


def project_expansion_action_context(
    graph: SearchGraphProjection,
    records: list[CandidateRecord],
    source_node_id: str,
    *,
    view_descriptions: Mapping[tuple[str, int, str], str] | None = None,
) -> ExpansionActionContext | None:
    """Join settled search facts into a derived worker's observed actions."""

    nodes_by_id = {node.node_id: node for node in graph.nodes}
    source_node = nodes_by_id.get(source_node_id)
    if source_node is None or source_node.kind != "settled_iteration":
        return None

    iterations = {
        (record.candidate_id, iteration.iteration): iteration
        for record in records
        for iteration in record.iterations
        if iteration.agent_session_id is not None
    }
    descriptions = view_descriptions or {}
    ordered_transitions = sorted(
        graph.transitions,
        key=lambda item: (
            item.created_at,
            item.candidate_id,
            item.iteration,
            item.transition_id,
        ),
    )
    first_transition_by_effect: dict[str, str] = {}
    actions_by_transition: dict[str, ObservedActionEdge] = {}
    ordered_actions: list[ObservedActionEdge] = []
    for transition in ordered_transitions:
        iteration = iterations.get((transition.candidate_id, transition.iteration))
        if iteration is None:
            continue
        effect_key = (
            _stable_id("action-effect", transition.from_node_id, iteration.artifact_hash)
            if iteration.artifact_hash is not None
            else None
        )
        duplicate_of = None
        if effect_key is not None:
            duplicate_of = first_transition_by_effect.setdefault(
                effect_key,
                transition.transition_id,
            )
            if duplicate_of == transition.transition_id:
                duplicate_of = None

        view_key = (
            (transition.candidate_id, transition.iteration, transition.attempt_commit)
            if transition.attempt_commit is not None
            else None
        )
        view_description = descriptions.get(view_key) if view_key is not None else None
        description = view_description or iteration.hypothesis or iteration.summary
        description = " ".join(description.strip().split())[:1000]
        if not description:
            description = "Observed settled worker attempt."
        action = ObservedActionEdge(
            transition_id=transition.transition_id,
            candidate_id=transition.candidate_id,
            iteration=transition.iteration,
            kind=transition.kind,
            from_node_id=transition.from_node_id,
            settled_node_id=transition.settled_node_id,
            attempt_commit=transition.attempt_commit,
            description=description,
            description_source=(
                "evidence_view" if view_description is not None else "hypothesis"
            ),
            attempt_changed_files=iteration.attempt_changed_files,
            artifact_hash=iteration.artifact_hash,
            exact_effect_key=effect_key,
            duplicate_of_transition_id=duplicate_of,
            disposition=transition.disposition,
            score=transition.score,
        )
        actions_by_transition[transition.transition_id] = action
        ordered_actions.append(action)

    source_path_actions: list[ObservedActionEdge] = []
    cursor = source_node
    while cursor.incoming_transition_id is not None:
        action = actions_by_transition.get(cursor.incoming_transition_id)
        if action is not None:
            source_path_actions.append(action)
        if cursor.parent_node_id is None:
            break
        parent = nodes_by_id.get(cursor.parent_node_id)
        if parent is None:
            break
        cursor = parent
    source_path_actions.reverse()

    tried_actions = [
        action for action in ordered_actions if action.from_node_id == source_node_id
    ]
    unique_effects = {
        action.exact_effect_key or action.transition_id for action in tried_actions
    }
    return ExpansionActionContext(
        graph_revision=graph.revision,
        source_node_id=source_node_id,
        source_candidate_id=source_node.candidate_id,
        source_path_actions=source_path_actions,
        tried_actions=tried_actions,
        unique_tried_action_count=len(unique_effects),
        exact_duplicate_count=sum(
            action.duplicate_of_transition_id is not None for action in tried_actions
        ),
    )


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


def _initial_value_projection(
    graph: SearchGraphProjection,
    records: list[CandidateRecord],
    *,
    operator_name: str,
    operator_version: int,
    config_hash: str,
) -> ValueProjection:
    direct_attempts: dict[str, int] = {}
    unique_branches: dict[str, set[str]] = {}
    total_costs: dict[str, float] = {}
    iterations_by_coordinate = {
        (record.candidate_id, iteration.iteration): iteration
        for record in records
        for iteration in record.iterations
    }
    iteration_costs = {
        (record.candidate_id, iteration.iteration): float(
            (iteration.reward_evaluation.components or {}).get(
                "verifier_elapsed_seconds",
                0.0,
            )
        )
        for record in records
        for iteration in record.iterations
        if iteration.reward_evaluation is not None
    }
    for transition in graph.transitions:
        direct_attempts[transition.from_node_id] = (
            direct_attempts.get(transition.from_node_id, 0) + 1
        )
        iteration = iterations_by_coordinate.get(
            (transition.candidate_id, transition.iteration)
        )
        if iteration is not None and iteration.artifact_hash is not None:
            unique_branches.setdefault(transition.from_node_id, set()).add(
                iteration.artifact_hash
            )
        total_costs[transition.from_node_id] = (
            total_costs.get(transition.from_node_id, 0.0)
            + iteration_costs.get(
                (transition.candidate_id, transition.iteration),
                0.0,
            )
        )

    completed_expansions: dict[str, int] = {}
    unobserved_expansions: dict[str, int] = {}
    graph_nodes, graph_nodes_by_coordinate = _graph_node_indexes(graph)
    for record in records:
        source = record.task.expansion_source
        if source is None:
            continue
        target = completed_expansions if record.iterations else unobserved_expansions
        source_node_id = _resolve_expansion_source_node_from_indexes(
            source,
            graph_nodes,
            graph_nodes_by_coordinate,
        ).node_id
        target[source_node_id] = target.get(source_node_id, 0) + 1

    node_values = [
        NodeValueEstimate(
            node_id=node.node_id,
            settled_value=node.settled_value,
            backed_up_value=node.settled_value,
            direct_attempt_count=direct_attempts.get(node.node_id, 0),
            unique_branch_count=len(unique_branches.get(node.node_id, set())),
            completed_expansion_count=completed_expansions.get(node.node_id, 0),
            unobserved_expansion_count=unobserved_expansions.get(node.node_id, 0),
            total_cost=total_costs.get(node.node_id, 0.0),
            updated_at=node.created_at,
        )
        for node in graph.nodes
    ]
    edge_values = [
        EdgeValueEstimate(
            transition_id=transition.transition_id,
            from_node_id=transition.from_node_id,
            settled_node_id=transition.settled_node_id,
            attempt_reward=transition.attempt_reward,
            created_at=transition.created_at,
        )
        for transition in graph.transitions
    ]
    return ValueProjection(
        run_id=graph.run_id,
        graph_revision=graph.revision,
        operator_name=operator_name,
        operator_version=operator_version,
        config_hash=config_hash,
        node_values=node_values,
        edge_values=edge_values,
        created_at=graph.created_at,
        updated_at=graph.updated_at,
    )


def _source_options(
    graph: SearchGraphProjection,
    values: ValueProjection,
    records: list[CandidateRecord],
    resources: AllocationResourceState,
    *,
    max_depth: int,
    exploration_weight: float,
    include_retired: bool,
) -> list[_SourceOption]:
    expansion_visits: dict[str, int] = {}
    graph_nodes, graph_nodes_by_coordinate = _graph_node_indexes(graph)
    for record in records:
        source = record.task.expansion_source
        if source is not None:
            source_node_id = _resolve_expansion_source_node_from_indexes(
                source,
                graph_nodes,
                graph_nodes_by_coordinate,
            ).node_id
            expansion_visits[source_node_id] = (
                expansion_visits.get(source_node_id, 0) + 1
            )
    total_expansions = sum(expansion_visits.values())
    options: list[_SourceOption] = []
    records_by_id = {item.candidate_id: item for item in records}
    values_by_id = {item.node_id: item for item in values.node_values}
    for node in graph.nodes:
        if node.kind != "settled_iteration" or node.iteration is None:
            continue
        record = records_by_id.get(node.candidate_id)
        value = values_by_id.get(node.node_id)
        if (
            record is None
            or value is None
            or node.score is None
            or node.evidence_commit is None
            or node.artifact_hash is None
            or node.reward_id is None
            or node.settled_value is None
            or value.backed_up_value is None
        ):
            continue
        ineligible_reason = None
        if record.task.allocation_depth >= max_depth:
            ineligible_reason = "max_depth"
        elif not include_retired and record.allocation_eligibility != "eligible":
            ineligible_reason = "retired"
        elif (
            resources.exploration_quota_remaining_by_node.get(node.node_id)
            == 0
        ):
            ineligible_reason = "exploration_quota_exhausted"
        visits = expansion_visits.get(node.node_id, 0)
        exploration_bonus = exploration_weight * math.sqrt(
            math.log(total_expansions + 2.0) / (visits + 1.0)
        )
        priority = value.backed_up_value + exploration_bonus
        source = ExpansionSource(
            node_id=node.node_id,
            candidate_id=record.candidate_id,
            iteration=node.iteration,
            evidence_commit=node.evidence_commit,
            settled_commit=node.settled_commit,
            artifact_hash=node.artifact_hash,
            score=node.score,
            reward_id=node.reward_id,
            settled_value=node.settled_value,
            backed_up_value=value.backed_up_value,
        )
        snapshot = AllocationSourceSnapshot(
            candidate_id=record.candidate_id,
            node_id=node.node_id,
            iteration=node.iteration,
            allocation_depth=record.task.allocation_depth,
            settled_value=node.settled_value,
            backed_up_value=value.backed_up_value,
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
    graph: SearchGraphProjection,
    values: ValueProjection,
    records: list[CandidateRecord],
    resources: AllocationResourceState,
    *,
    max_depth: int,
) -> ExpansionSource | None:
    options = _source_options(
        graph,
        values,
        records,
        resources,
        max_depth=max_depth,
        exploration_weight=0.0,
        include_retired=False,
    )
    return next(
        (item.source for item in options if item.snapshot.eligible),
        None,
    )


def _candidate_base_commit(record: CandidateRecord) -> str:
    if record.iterations:
        first = record.iterations[0]
        commit = first.attempt_base_git_head or record.task.workspace_base_revision
    else:
        commit = record.task.workspace_base_revision
    if commit is None:
        raise ValueError(
            f"candidate {record.candidate_id} has no workspace base revision"
        )
    return commit


def _stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()[:16]
    return f"{prefix}_{digest}"


class AdaptiveSearchEngine:
    """Host-neutral adaptive search computations."""

    def __init__(
        self,
        *,
        constraint_evaluators: tuple[AllocationConstraintEvaluator, ...] = (),
        reservation_planner: AllocationReservationPlanner | None = None,
    ) -> None:
        self._constraint_evaluators = constraint_evaluators
        self._reservation_planner = (
            reservation_planner or AtomicAllocationReservationPlanner()
        )

    def evaluate_reward(
        self,
        adaptive_spec: AdaptiveSearchSpec,
        context: RewardContext,
    ) -> RewardEvaluation:
        return evaluate_reward(adaptive_spec, context)

    def validate_spec(self, adaptive_spec: AdaptiveSearchSpec) -> None:
        validate_adaptive_search_spec(adaptive_spec)

    def project(
        self,
        run_id: str,
        records: list[CandidateRecord],
        *,
        created_at: str,
    ) -> SearchGraphProjection:
        return project_search_graph(run_id, records, created_at=created_at)

    def project_action_context(
        self,
        graph: SearchGraphProjection,
        records: list[CandidateRecord],
        source_node_id: str,
        *,
        view_descriptions: Mapping[tuple[str, int, str], str] | None = None,
    ) -> ExpansionActionContext | None:
        return project_expansion_action_context(
            graph,
            records,
            source_node_id,
            view_descriptions=view_descriptions,
        )

    def project_resources(
        self,
        adaptive_spec: AdaptiveSearchSpec,
        graph: SearchGraphProjection,
        records: list[CandidateRecord],
        decisions: list[AllocationDecision],
        *,
        max_candidates: int,
    ) -> AllocationResourceState:
        return project_allocation_resources(
            adaptive_spec,
            graph,
            records,
            decisions,
            max_candidates=max_candidates,
        )

    def settle_values(
        self,
        adaptive_spec: AdaptiveSearchSpec,
        context: ValueBackupContext,
        existing_events: list[ValueBackupEvent],
    ) -> tuple[ValueBackupEvent | None, ValueProjection]:
        event = create_value_backup_event(adaptive_spec, context)
        events = list(existing_events)
        if event is not None and not any(
            item.event_id == event.event_id for item in events
        ):
            events.append(event)
        return event, replay_value_backups(
            adaptive_spec,
            context.graph,
            context.records,
            events,
        )

    def replay_values(
        self,
        adaptive_spec: AdaptiveSearchSpec,
        graph: SearchGraphProjection,
        records: list[CandidateRecord],
        events: list[ValueBackupEvent],
    ) -> ValueProjection:
        return replay_value_backups(
            adaptive_spec,
            graph,
            records,
            events,
        )

    def decide(
        self,
        context: AllocationContext,
    ) -> AllocationReservationPlan | None:
        recommendations = decide_allocation_batch(context)
        if not recommendations:
            return None
        reservation = self._reservation_planner.plan(
            context,
            recommendations,
        )
        for evaluator in self._constraint_evaluators:
            evaluator.validate(context, reservation)
        return reservation
