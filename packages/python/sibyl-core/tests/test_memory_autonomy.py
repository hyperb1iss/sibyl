from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sibyl_core.services.memory_autonomy import (
    ReflectionAutonomyAction,
    ReflectionAutonomyOutcome,
    ReflectionAutonomyPolicy,
    decide_reflection_candidate_autonomy,
)
from sibyl_core.services.surreal_content import MemoryScope


@dataclass(frozen=True, slots=True)
class _Preview:
    allowed: bool = True
    candidate_id: str = "candidate-1"
    reason: str = "promotion_preview_allowed"
    review_state: str = "pending"
    memory_scope: MemoryScope | None = MemoryScope.PROJECT
    scope_key: str | None = "project_123"
    raw_source_ids: list[str] | None = None
    metadata: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.raw_source_ids is None:
            object.__setattr__(self, "raw_source_ids", ["source-1"])
        if self.metadata is None:
            object.__setattr__(
                self,
                "metadata",
                {
                    "policy_reasons": [
                        "same_scope_reflect_allowed",
                        "same_scope_write_allowed",
                    ],
                    "confidence": 0.91,
                    "source_count": 1,
                },
            )


def test_autonomy_decision_auto_promotes_safe_candidate() -> None:
    decision = decide_reflection_candidate_autonomy(_Preview())

    assert decision.outcome is ReflectionAutonomyOutcome.AUTO_PROMOTE
    assert decision.recommended_action is ReflectionAutonomyAction.PROMOTE
    assert decision.reason == "auto_promote_candidate"
    assert decision.should_promote is True
    assert decision.confidence == 0.91
    assert decision.policy_reasons == [
        "same_scope_reflect_allowed",
        "same_scope_write_allowed",
    ]


def test_autonomy_decision_dry_run_preserves_recommendation_without_apply() -> None:
    decision = decide_reflection_candidate_autonomy(_Preview(), dry_run=True)

    assert decision.outcome is ReflectionAutonomyOutcome.AUTO_PROMOTE
    assert decision.recommended_action is ReflectionAutonomyAction.PROMOTE
    assert decision.should_promote is False
    assert decision.metadata is not None
    assert decision.metadata["autonomy_dry_run"] is True


def test_autonomy_decision_routes_policy_denials_to_review() -> None:
    decision = decide_reflection_candidate_autonomy(
        _Preview(
            allowed=False,
            reason="unverified_membership",
            metadata={
                "policy_reasons": ["unverified_membership"],
                "confidence": 0.92,
            },
        )
    )

    assert decision.outcome is ReflectionAutonomyOutcome.EXCEPTION
    assert decision.recommended_action is ReflectionAutonomyAction.ROUTE_TO_REVIEW
    assert decision.reason == "policy_denied"
    assert decision.exception_reasons == ["policy_denied"]
    assert decision.should_promote is False


def test_autonomy_decision_routes_low_confidence_to_review() -> None:
    decision = decide_reflection_candidate_autonomy(_Preview(metadata={"confidence": 0.74}))

    assert decision.outcome is ReflectionAutonomyOutcome.EXCEPTION
    assert decision.reason == "low_confidence"
    assert decision.exception_reasons == ["low_confidence"]


def test_autonomy_decision_routes_sensitive_candidates_to_review() -> None:
    decision = decide_reflection_candidate_autonomy(
        _Preview(
            metadata={
                "confidence": 0.95,
                "sensitivity_flags": ["tag:secret"],
            }
        )
    )

    assert decision.outcome is ReflectionAutonomyOutcome.EXCEPTION
    assert decision.reason == "sensitive_candidate"
    assert decision.exception_reasons == ["sensitive_candidate"]


def test_autonomy_decision_routes_high_impact_scopes_to_review() -> None:
    decision = decide_reflection_candidate_autonomy(
        _Preview(memory_scope=MemoryScope.ORGANIZATION),
        policy=ReflectionAutonomyPolicy(confidence_threshold=0.8),
    )

    assert decision.outcome is ReflectionAutonomyOutcome.EXCEPTION
    assert decision.reason == "high_impact_scope"
    assert decision.exception_reasons == ["high_impact_scope"]
