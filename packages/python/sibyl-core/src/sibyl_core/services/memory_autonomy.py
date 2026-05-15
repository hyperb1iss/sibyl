"""Deterministic review decisions for automatic memory promotion."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from sibyl_core.services.surreal_content import MemoryScope, RawMemory


class ReflectionAutonomyOutcome(StrEnum):
    AUTO_PROMOTE = "auto_promote"
    EXCEPTION = "exception"
    SKIP = "skip"


class ReflectionAutonomyAction(StrEnum):
    PROMOTE = "promote"
    ROUTE_TO_REVIEW = "route_to_review"
    SKIP = "skip"


class ReflectionPromotionPreviewLike(Protocol):
    allowed: bool
    candidate_id: str
    reason: str
    review_state: str
    memory_scope: MemoryScope | None
    scope_key: str | None
    raw_source_ids: Sequence[str]
    metadata: Mapping[str, Any] | None


@dataclass(frozen=True, slots=True)
class ReflectionAutonomyPolicy:
    confidence_threshold: float = 0.8
    safe_scopes: frozenset[MemoryScope] = frozenset(
        {
            MemoryScope.PRIVATE,
            MemoryScope.DELEGATED,
            MemoryScope.PROJECT,
        }
    )


@dataclass(frozen=True, slots=True)
class ReflectionAutonomyDecision:
    outcome: ReflectionAutonomyOutcome
    recommended_action: ReflectionAutonomyAction
    candidate_id: str
    reason: str
    review_state: str
    memory_scope: MemoryScope | None
    scope_key: str | None
    raw_source_ids: list[str]
    policy_reasons: list[str]
    exception_reasons: list[str]
    confidence: float | None
    confidence_threshold: float
    dry_run: bool = False
    metadata: dict[str, Any] | None = None

    @property
    def should_promote(self) -> bool:
        return (
            self.outcome is ReflectionAutonomyOutcome.AUTO_PROMOTE
            and self.recommended_action is ReflectionAutonomyAction.PROMOTE
            and not self.dry_run
        )


_SENSITIVE_TAGS = frozenset(
    {
        "credential",
        "credentials",
        "key",
        "pii",
        "private_key",
        "secret",
        "sensitive",
        "token",
    }
)
_DESTRUCTIVE_KINDS = frozenset(
    {
        "correction",
        "delete",
        "deletion",
        "redact",
        "redaction",
        "rollback",
    }
)
_BLOCKING_FLAGS = (
    ("duplicate_of_source_id", "duplicate_candidate"),
    ("candidate_duplicate_of_source_id", "duplicate_candidate"),
    ("conflicts_with_source_ids", "contradiction_candidate"),
    ("contradiction_source_ids", "contradiction_candidate"),
    ("stale_source_ids", "stale_candidate"),
    ("superseded_by_source_id", "stale_candidate"),
)


def reflection_autonomy_candidate_metadata(memory: RawMemory) -> dict[str, Any]:
    metadata = dict(memory.metadata or {})
    tags = [str(tag).strip().lower() for tag in memory.tags if str(tag).strip()]
    sensitivity_flags = _sensitivity_flags(metadata, tags)
    return {
        "autonomy_candidate_entity_type": memory.entity_type,
        "autonomy_candidate_tags": tags,
        "autonomy_capture_surface": memory.capture_surface,
        "autonomy_remember_kind": _metadata_str(metadata, "remember_kind"),
        "reflection_confidence": _metadata_float(metadata, "reflection_confidence"),
        "sensitivity_flags": sensitivity_flags,
        "duplicate_of_source_id": _metadata_str(metadata, "duplicate_of_source_id"),
        "candidate_duplicate_of_source_id": _metadata_str(
            metadata,
            "candidate_duplicate_of_source_id",
        ),
        "conflicts_with_source_ids": _metadata_list(metadata, "conflicts_with_source_ids"),
        "contradiction_source_ids": _metadata_list(metadata, "contradiction_source_ids"),
        "stale_source_ids": _metadata_list(metadata, "stale_source_ids"),
        "superseded_by_source_id": _metadata_str(metadata, "superseded_by_source_id"),
    }


def decide_reflection_candidate_autonomy(
    preview: ReflectionPromotionPreviewLike,
    *,
    policy: ReflectionAutonomyPolicy | None = None,
    dry_run: bool = False,
) -> ReflectionAutonomyDecision:
    active_policy = policy or ReflectionAutonomyPolicy()
    metadata = dict(preview.metadata or {})
    policy_reasons = _str_list(metadata.get("policy_reasons"))
    exception_reasons = _exception_reasons(
        preview=preview,
        metadata=metadata,
        policy=active_policy,
    )
    confidence = _metadata_float(metadata, "reflection_confidence")

    if preview.reason in {"candidate_already_promoted", "candidate_archived"}:
        return _decision(
            preview=preview,
            policy=active_policy,
            outcome=ReflectionAutonomyOutcome.SKIP,
            recommended_action=ReflectionAutonomyAction.SKIP,
            reason=preview.reason,
            policy_reasons=policy_reasons,
            exception_reasons=[],
            confidence=confidence,
            dry_run=dry_run,
            metadata=metadata,
        )

    if exception_reasons:
        return _decision(
            preview=preview,
            policy=active_policy,
            outcome=ReflectionAutonomyOutcome.EXCEPTION,
            recommended_action=ReflectionAutonomyAction.ROUTE_TO_REVIEW,
            reason=exception_reasons[0],
            policy_reasons=policy_reasons,
            exception_reasons=exception_reasons,
            confidence=confidence,
            dry_run=dry_run,
            metadata=metadata,
        )

    return _decision(
        preview=preview,
        policy=active_policy,
        outcome=ReflectionAutonomyOutcome.AUTO_PROMOTE,
        recommended_action=ReflectionAutonomyAction.PROMOTE,
        reason="auto_promote_candidate",
        policy_reasons=policy_reasons,
        exception_reasons=[],
        confidence=confidence,
        dry_run=dry_run,
        metadata=metadata,
    )


def _decision(
    *,
    preview: ReflectionPromotionPreviewLike,
    policy: ReflectionAutonomyPolicy,
    outcome: ReflectionAutonomyOutcome,
    recommended_action: ReflectionAutonomyAction,
    reason: str,
    policy_reasons: list[str],
    exception_reasons: list[str],
    confidence: float | None,
    dry_run: bool,
    metadata: dict[str, Any],
) -> ReflectionAutonomyDecision:
    return ReflectionAutonomyDecision(
        outcome=outcome,
        recommended_action=recommended_action,
        candidate_id=preview.candidate_id,
        reason=reason,
        review_state=preview.review_state,
        memory_scope=preview.memory_scope,
        scope_key=preview.scope_key,
        raw_source_ids=[str(source_id) for source_id in preview.raw_source_ids],
        policy_reasons=policy_reasons,
        exception_reasons=exception_reasons,
        confidence=confidence,
        confidence_threshold=policy.confidence_threshold,
        dry_run=dry_run,
        metadata={
            **metadata,
            "autonomy_outcome": outcome.value,
            "autonomy_recommended_action": recommended_action.value,
            "autonomy_reason": reason,
            "autonomy_exception_reasons": exception_reasons,
            "autonomy_confidence_threshold": policy.confidence_threshold,
            "autonomy_dry_run": dry_run,
        },
    )


def _exception_reasons(
    *,
    preview: ReflectionPromotionPreviewLike,
    metadata: Mapping[str, Any],
    policy: ReflectionAutonomyPolicy,
) -> list[str]:
    reasons: list[str] = []
    if preview.reason == "candidate_not_found":
        reasons.append("candidate_not_found")
    if not preview.allowed:
        reasons.append("policy_denied")
    if preview.memory_scope not in policy.safe_scopes:
        reasons.append("high_impact_scope")
    if not preview.raw_source_ids:
        reasons.append("missing_source")

    confidence = _metadata_float(metadata, "reflection_confidence")
    if confidence is None:
        reasons.append("missing_confidence")
    elif confidence < policy.confidence_threshold:
        reasons.append("low_confidence")

    sensitivity_flags = _str_list(metadata.get("sensitivity_flags"))
    if sensitivity_flags:
        reasons.append("sensitive_candidate")

    remember_kind = _metadata_str(metadata, "autonomy_remember_kind")
    if remember_kind and remember_kind.lower() in _DESTRUCTIVE_KINDS:
        reasons.append("destructive_candidate")

    for key, reason in _BLOCKING_FLAGS:
        value = metadata.get(key)
        if value and reason not in reasons:
            reasons.append(reason)

    return reasons


def _sensitivity_flags(metadata: Mapping[str, Any], tags: Sequence[str]) -> list[str]:
    flags: list[str] = []
    for key in ("contains_sensitive", "sensitive", "contains_pii", "contains_secret"):
        if metadata.get(key) is True:
            flags.append(key)
    tag_hits = sorted({tag for tag in tags if tag in _SENSITIVE_TAGS})
    flags.extend(f"tag:{tag}" for tag in tag_hits)
    return flags


def _metadata_str(metadata: Mapping[str, Any], key: str) -> str | None:
    value = metadata.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _metadata_float(metadata: Mapping[str, Any], key: str) -> float | None:
    value = metadata.get(key)
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _metadata_list(metadata: Mapping[str, Any], key: str) -> list[str]:
    return _str_list(metadata.get(key))


def _str_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item)]


__all__ = [
    "ReflectionAutonomyAction",
    "ReflectionAutonomyDecision",
    "ReflectionAutonomyOutcome",
    "ReflectionAutonomyPolicy",
    "decide_reflection_candidate_autonomy",
    "reflection_autonomy_candidate_metadata",
]
