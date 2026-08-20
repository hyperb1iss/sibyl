"""Shared result contracts for memory service domains."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from sibyl_core.auth.memory_policy import MemoryPolicyDecision
from sibyl_core.models.reflection import ReflectionCandidate
from sibyl_core.services.surreal_content import MemoryScope, RawMemory
from sibyl_core.tools.responses import AddResponse


class WriteMode(StrEnum):
    DISABLED = "disabled"
    ENABLED = "enabled"


@dataclass(frozen=True, slots=True)
class ReflectionWriteResult:
    response: AddResponse
    metadata: dict[str, Any]


@dataclass(frozen=True, slots=True)
class _RelationshipWriteReceipt:
    requested: int = 0
    created: int = 0
    failed: int = 0
    errors: tuple[str, ...] = ()

    @property
    def state(self) -> str:
        return "partial" if self.failed or self.errors else "complete"


@dataclass(frozen=True, slots=True)
class ReflectionPromotionResult:
    success: bool
    candidate_id: str
    promoted_id: str | None
    reason: str
    review_state: str
    memory_scope: MemoryScope | None
    scope_key: str | None
    raw_source_ids: list[str]
    policy_decisions: tuple[MemoryPolicyDecision, ...] = ()
    metadata: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class ReflectionPromotionPreview:
    allowed: bool
    candidate_id: str
    reason: str
    review_state: str
    memory_scope: MemoryScope | None
    scope_key: str | None
    raw_source_ids: list[str]
    policy_decisions: tuple[MemoryPolicyDecision, ...] = ()
    metadata: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class MemorySharePreview:
    allowed: bool
    reason: str
    target_scope: MemoryScope | None
    target_scope_key: str | None
    source_ids: list[str]
    visible_source_ids: list[str]
    denied_source_ids: list[str]
    missing_source_ids: list[str]
    redacted_count: int
    hidden_but_relevant_count: int
    policy_decisions: tuple[MemoryPolicyDecision, ...] = ()
    metadata: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class MemoryShareResult:
    applied: bool
    reason: str
    preview: MemorySharePreview
    promotions: tuple[ReflectionPromotionResult, ...] = ()
    metadata: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class MemoryAccessPreview:
    allowed: bool
    reason: str
    target_principal_type: str
    target_principal_id: str
    memory_space_ids: list[str]
    visible_source_ids: list[str]
    denied_source_ids: list[str]
    missing_source_ids: list[str]
    redacted_count: int
    hidden_but_relevant_count: int
    policy_decisions: tuple[MemoryPolicyDecision, ...] = ()
    metadata: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class MemoryCorrectionPreview:
    allowed: bool
    source_id: str
    action: str
    reason: str
    target_lifecycle_state: str
    target_lifecycle_flags: list[str]
    affected_source_ids: list[str]
    affected_derived_ids: list[str]
    reversible: bool
    recall_impact: dict[str, Any]
    synthesis_impact: dict[str, Any]
    audit_action: str
    policy_decisions: tuple[MemoryPolicyDecision, ...] = ()
    metadata: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class MemoryCorrectionResult:
    applied: bool
    preview: MemoryCorrectionPreview
    updated_memory: RawMemory | None = None
    # The graph rows the correction actually reached. Empty is a real answer:
    # a capture with no projection has nothing on the retrieval lane to gate.
    affected_entity_ids: list[str] = field(default_factory=list)
    # Rows the capture named that this principal may not write. A correction
    # with a non-empty list here applied to the capture but not to everything
    # the capture claimed, and saying so is the difference between a partial
    # write and a silent no-op.
    refused_entity_ids: list[str] = field(default_factory=list)
    # The lineage walk stopped at its page ceiling, so rows projected from this
    # capture may still be servable. Reported rather than logged alone, because
    # a caller told "applied" has no other way to learn the write was partial.
    projection_walk_truncated: bool = False


@dataclass(frozen=True, slots=True)
class _ReflectionPromotionPlan:
    candidate_memory: RawMemory
    promotion_candidate: ReflectionCandidate
    target_scope: MemoryScope
    target_scope_key: str | None
    target_project: str | None
    raw_source_ids: list[str]
    input_memories: list[RawMemory]
