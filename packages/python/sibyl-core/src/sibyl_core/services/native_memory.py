"""Native SurrealDB memory write services."""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sibyl_core.auth.memory_policy import (
    MemoryPolicyDecision,
    authorize_memory_reflect,
    authorize_memory_write,
)
from sibyl_core.models.entities import Entity, EntityType, Relationship, RelationshipType
from sibyl_core.models.reflection import ReflectionCandidate
from sibyl_core.services import get_graph_runtime
from sibyl_core.services.surreal_content import (
    MemoryScope,
    RawMemory,
    get_raw_memory,
    save_raw_memory,
)
from sibyl_core.tools.helpers import _generate_id
from sibyl_core.tools.responses import AddResponse


class NativeWriteMode(StrEnum):
    DISABLED = "disabled"
    ENABLED = "enabled"


@dataclass(frozen=True, slots=True)
class NativeReflectionWriteResult:
    response: AddResponse
    metadata: dict[str, Any]


@dataclass(frozen=True, slots=True)
class NativeReflectionPromotionResult:
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


_PROMOTED_REVIEW_STATE = "promoted"
_SCOPE_RANK: dict[MemoryScope, int] = {
    MemoryScope.PRIVATE: 0,
    MemoryScope.DELEGATED: 1,
    MemoryScope.PROJECT: 2,
    MemoryScope.TEAM: 3,
    MemoryScope.ORGANIZATION: 4,
    MemoryScope.SHARED: 5,
    MemoryScope.PUBLIC: 6,
}


def coerce_native_write_mode(value: str | NativeWriteMode | None) -> NativeWriteMode:
    if isinstance(value, NativeWriteMode):
        return value
    if value is None or not value.strip():
        return NativeWriteMode.DISABLED
    normalized = value.strip().lower()
    if normalized in {"enabled", "enable", "true", "1", "yes", "on"}:
        return NativeWriteMode.ENABLED
    return NativeWriteMode.DISABLED


def native_write_mode_from_env(environ: Mapping[str, str] | None = None) -> NativeWriteMode:
    source = os.environ if environ is None else environ
    return coerce_native_write_mode(source.get("SIBYL_NATIVE_WRITE"))


def native_reflection_write_enabled(environ: Mapping[str, str] | None = None) -> bool:
    return native_write_mode_from_env(environ) is NativeWriteMode.ENABLED


async def persist_reflection_source_native(
    *,
    title: str,
    content: str,
    organization_id: str,
    principal_id: str | None,
    domain: str | None = None,
    project: str | None = None,
    related_to: Sequence[str] | None = None,
    accessible_projects: Iterable[str] | None = None,
    memory_scope: MemoryScope | str | None = None,
    scope_key: str | None = None,
) -> NativeReflectionWriteResult:
    candidate = ReflectionCandidate(
        kind=EntityType.SESSION.value,
        title=title,
        content=content,
        reason="preserves raw reflection source material",
        confidence=1.0,
        tags=["reflection", EntityType.SESSION.value],
        metadata={"reflection_source": True},
    )
    return await persist_reflection_candidate_native(
        candidate=candidate,
        organization_id=organization_id,
        principal_id=principal_id,
        domain=domain,
        project=project,
        source_id=None,
        related_to=related_to,
        accessible_projects=accessible_projects,
        memory_scope=memory_scope,
        scope_key=scope_key,
    )


async def persist_reflection_candidate_native(
    *,
    candidate: ReflectionCandidate,
    organization_id: str,
    principal_id: str | None,
    domain: str | None = None,
    project: str | None = None,
    source_id: str | None = None,
    related_to: Sequence[str] | None = None,
    accessible_projects: Iterable[str] | None = None,
    memory_scope: MemoryScope | str | None = None,
    scope_key: str | None = None,
    link_source_entity: bool = True,
) -> NativeReflectionWriteResult:
    scope = _resolve_memory_scope(memory_scope, project)
    resolved_scope_key = _resolve_scope_key(scope, scope_key, project)
    policy_decisions = _authorize_reflection_write(
        principal_id=principal_id,
        memory_scope=scope,
        scope_key=resolved_scope_key,
        accessible_projects=accessible_projects,
    )
    policy_metadata = _policy_metadata(policy_decisions)
    if any(not decision.allowed for decision in policy_decisions):
        return NativeReflectionWriteResult(
            response=AddResponse(
                success=False,
                id=None,
                message=_policy_denied_message(policy_decisions),
                timestamp=datetime.now(UTC),
            ),
            metadata=policy_metadata,
        )

    runtime = await get_graph_runtime(organization_id)
    entity = _entity_from_candidate(
        candidate,
        organization_id=organization_id,
        principal_id=principal_id,
        domain=domain,
        project=project,
        source_id=source_id,
        memory_scope=scope,
        scope_key=resolved_scope_key,
        policy_metadata=policy_metadata,
    )
    created_id = await runtime.entity_manager.create_direct(entity)
    source_ids = _candidate_source_ids(candidate, source_id)
    relationships = _relationships_for_promotion(
        created_id,
        project=project,
        source_id=source_id if link_source_entity else None,
        related_to=related_to,
        supersedes=_superseded_entity_ids(candidate.metadata),
        raw_source_ids=source_ids,
    )
    if relationships:
        await runtime.relationship_manager.create_bulk(relationships)

    return NativeReflectionWriteResult(
        response=AddResponse(
            success=True,
            id=created_id,
            message=f"Promoted natively: {candidate.title}",
            timestamp=datetime.now(UTC),
        ),
        metadata={
            **policy_metadata,
            "native_write_mode": NativeWriteMode.ENABLED.value,
            "native_write_path": "reflection_promotion",
            "native_relationship_count": len(relationships),
            "raw_source_ids": source_ids,
            "source_ids": source_ids,
        },
    )


async def promote_reflection_candidate_review(
    *,
    candidate_id: str,
    organization_id: str,
    principal_id: str | None,
    promote_to_scope: MemoryScope | str | None,
    promote_to_scope_key: str | None = None,
    domain: str | None = None,
    project: str | None = None,
    related_to: Sequence[str] | None = None,
    accessible_projects: Iterable[str] | None = None,
) -> NativeReflectionPromotionResult:
    candidate_memory = await get_raw_memory(
        organization_id=organization_id,
        memory_id=candidate_id,
    )
    if candidate_memory is None:
        return _promotion_denied(
            candidate_id=candidate_id,
            reason="candidate_not_found",
            review_state="missing",
            memory_scope=None,
            scope_key=None,
            raw_source_ids=[],
        )

    if not _is_reflection_candidate(candidate_memory):
        return _promotion_denied(
            candidate_id=candidate_memory.id,
            reason="not_reflection_candidate",
            review_state=candidate_memory.review_state,
            memory_scope=candidate_memory.memory_scope,
            scope_key=candidate_memory.scope_key,
            raw_source_ids=[],
        )

    if candidate_memory.review_state == _PROMOTED_REVIEW_STATE:
        return _promotion_denied(
            candidate_id=candidate_memory.id,
            reason="candidate_already_promoted",
            review_state=candidate_memory.review_state,
            memory_scope=candidate_memory.memory_scope,
            scope_key=candidate_memory.scope_key,
            raw_source_ids=_raw_source_ids(candidate_memory),
        )
    if candidate_memory.review_state == "archived":
        return _promotion_denied(
            candidate_id=candidate_memory.id,
            reason="candidate_archived",
            review_state=candidate_memory.review_state,
            memory_scope=candidate_memory.memory_scope,
            scope_key=candidate_memory.scope_key,
            raw_source_ids=_raw_source_ids(candidate_memory),
        )

    raw_source_ids = _raw_source_ids(candidate_memory)
    source_memories = await _load_raw_sources(
        organization_id=organization_id,
        raw_source_ids=raw_source_ids,
    )
    raw_source_ids = raw_source_ids or [candidate_memory.id]
    input_memories = [candidate_memory, *source_memories]

    ownership_denial = _principal_denial(
        input_memories,
        candidate_id=candidate_memory.id,
        principal_id=principal_id,
        raw_source_ids=raw_source_ids,
    )
    if ownership_denial is not None:
        return ownership_denial

    target_scope = _coerce_promotion_scope(promote_to_scope)
    if target_scope is None:
        reason = _missing_promotion_target_reason(candidate_memory, input_memories)
        return _promotion_denied(
            candidate_id=candidate_memory.id,
            reason=reason,
            review_state=candidate_memory.review_state,
            memory_scope=candidate_memory.memory_scope,
            scope_key=candidate_memory.scope_key,
            raw_source_ids=raw_source_ids,
            metadata={"input_scopes": _scope_metadata(input_memories)},
        )

    target_scope_key = _resolve_promotion_scope_key(
        target_scope=target_scope,
        promote_to_scope_key=promote_to_scope_key,
        project=project,
        candidate_memory=candidate_memory,
    )
    broadest_scope = _broadest_scope(input_memories)
    if _has_mixed_scope_inputs(input_memories) and target_scope is not broadest_scope:
        return _promotion_denied(
            candidate_id=candidate_memory.id,
            reason="promote_to_scope_must_match_broadest_input_scope",
            review_state=candidate_memory.review_state,
            memory_scope=target_scope,
            scope_key=target_scope_key,
            raw_source_ids=raw_source_ids,
            metadata={
                "broadest_input_scope": broadest_scope.value,
                "input_scopes": _scope_metadata(input_memories),
            },
        )

    promotion_candidate = _candidate_from_review_memory(
        candidate_memory,
        raw_source_ids=raw_source_ids,
        target_scope=target_scope,
        target_scope_key=target_scope_key,
        domain=domain,
    )
    target_project = project or (
        target_scope_key
        if target_scope is MemoryScope.PROJECT
        else _metadata_str(
            candidate_memory.metadata,
            "project_id",
        )
    )
    native_result = await persist_reflection_candidate_native(
        candidate=promotion_candidate,
        organization_id=organization_id,
        principal_id=principal_id,
        domain=domain or _metadata_str(candidate_memory.metadata, "domain"),
        project=target_project,
        source_id=raw_source_ids[0] if raw_source_ids else None,
        related_to=related_to,
        accessible_projects=accessible_projects,
        memory_scope=target_scope,
        scope_key=target_scope_key,
        link_source_entity=False,
    )
    if not native_result.response.success:
        return NativeReflectionPromotionResult(
            success=False,
            candidate_id=candidate_memory.id,
            promoted_id=None,
            reason=_policy_denial_reason(native_result.metadata),
            review_state=candidate_memory.review_state,
            memory_scope=target_scope,
            scope_key=target_scope_key,
            raw_source_ids=raw_source_ids,
            metadata=native_result.metadata,
        )

    promoted_at = datetime.now(UTC).isoformat()
    metadata = {
        **candidate_memory.metadata,
        **native_result.metadata,
        "review_state": _PROMOTED_REVIEW_STATE,
        "promoted_at": promoted_at,
        "promoted_entity_id": native_result.response.id,
        "promote_to_scope": target_scope.value,
        "promote_to_scope_key": target_scope_key,
        "raw_source_ids": raw_source_ids,
        "source_ids": raw_source_ids,
    }
    updated = replace(
        candidate_memory,
        review_state=_PROMOTED_REVIEW_STATE,
        metadata=metadata,
    )
    await save_raw_memory(updated)

    return NativeReflectionPromotionResult(
        success=True,
        candidate_id=candidate_memory.id,
        promoted_id=native_result.response.id,
        reason="promoted",
        review_state=_PROMOTED_REVIEW_STATE,
        memory_scope=target_scope,
        scope_key=target_scope_key,
        raw_source_ids=raw_source_ids,
        metadata=metadata,
    )


def _resolve_memory_scope(
    memory_scope: MemoryScope | str | None,
    project: str | None,
) -> MemoryScope:
    if memory_scope is not None:
        try:
            return MemoryScope(memory_scope)
        except ValueError:
            return MemoryScope.PRIVATE
    return MemoryScope.PROJECT if project else MemoryScope.PRIVATE


def _resolve_scope_key(
    memory_scope: MemoryScope,
    scope_key: str | None,
    project: str | None,
) -> str | None:
    if memory_scope is MemoryScope.PROJECT:
        return scope_key or project
    return scope_key


def _authorize_reflection_write(
    *,
    principal_id: str | None,
    memory_scope: MemoryScope,
    scope_key: str | None,
    accessible_projects: Iterable[str] | None,
) -> tuple[MemoryPolicyDecision, MemoryPolicyDecision]:
    reflect_decision = authorize_memory_reflect(
        principal_id=principal_id,
        memory_scope=memory_scope,
        scope_key=scope_key,
        accessible_projects=accessible_projects,
    )
    write_decision = authorize_memory_write(
        principal_id=principal_id,
        memory_scope=memory_scope,
        scope_key=scope_key,
        accessible_projects=accessible_projects,
    )
    return reflect_decision, write_decision


def _policy_metadata(decisions: Sequence[MemoryPolicyDecision]) -> dict[str, Any]:
    return {
        "native_write_mode": NativeWriteMode.ENABLED.value,
        "memory_scope": decisions[0].memory_scope.value,
        "scope_key": decisions[0].scope_key,
        "policy_allowed": all(decision.allowed for decision in decisions),
        "policy_reasons": [decision.reason for decision in decisions],
        "policy_actions": [decision.action.value for decision in decisions],
    }


def _policy_denied_message(decisions: Sequence[MemoryPolicyDecision]) -> str:
    denied = [decision.reason for decision in decisions if not decision.allowed]
    reason = denied[0] if denied else "unknown"
    return f"Native reflection promotion denied: {reason}"


def _promotion_denied(
    *,
    candidate_id: str,
    reason: str,
    review_state: str,
    memory_scope: MemoryScope | None,
    scope_key: str | None,
    raw_source_ids: list[str],
    metadata: dict[str, Any] | None = None,
) -> NativeReflectionPromotionResult:
    payload = {"policy_reasons": [reason], "policy_allowed": False}
    if metadata:
        payload.update(metadata)
    return NativeReflectionPromotionResult(
        success=False,
        candidate_id=candidate_id,
        promoted_id=None,
        reason=reason,
        review_state=review_state,
        memory_scope=memory_scope,
        scope_key=scope_key,
        raw_source_ids=raw_source_ids,
        metadata=payload,
    )


def _policy_denial_reason(metadata: Mapping[str, Any]) -> str:
    reasons = metadata.get("policy_reasons")
    if isinstance(reasons, list):
        denied = [str(reason) for reason in reasons if str(reason)]
        if denied:
            return denied[0]
    return "promotion_policy_denied"


def _metadata_str(metadata: Mapping[str, object], key: str) -> str | None:
    value = metadata.get(key)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _metadata_str_list(metadata: Mapping[str, object], key: str) -> list[str]:
    value = metadata.get(key)
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item)]


def _metadata_str_values(metadata: Mapping[str, object], *keys: str) -> list[str]:
    values: list[str] = []
    for key in keys:
        value = metadata.get(key)
        if isinstance(value, str):
            values.append(value)
            continue
        if isinstance(value, Iterable) and not isinstance(value, Mapping):
            values.extend(str(item) for item in value if str(item))
    return list(dict.fromkeys(item for item in values if item))


def _metadata_float(metadata: Mapping[str, object], key: str, default: float) -> float:
    value = metadata.get(key)
    if isinstance(value, int | float | str):
        try:
            return float(value)
        except ValueError:
            return default
    return default


def _raw_source_ids(memory: RawMemory) -> list[str]:
    return list(dict.fromkeys(_metadata_str_list(memory.metadata, "raw_source_ids")))


def _candidate_source_ids(
    candidate: ReflectionCandidate,
    source_id: str | None,
) -> list[str]:
    return list(
        dict.fromkeys(
            item
            for item in (
                *([source_id] if source_id else []),
                *candidate.raw_source_ids,
                *_metadata_str_values(candidate.metadata, "raw_source_ids", "source_ids"),
            )
            if item
        )
    )


def _superseded_entity_ids(metadata: Mapping[str, object]) -> list[str]:
    return _metadata_str_values(
        metadata,
        "supersedes",
        "supersedes_ids",
        "superseded_ids",
        "supersedes_entity_ids",
    )


def _is_reflection_candidate(memory: RawMemory) -> bool:
    return (
        memory.capture_surface == "reflection_candidate"
        or _metadata_str(memory.metadata, "capture_surface") == "reflection_candidate"
    )


async def _load_raw_sources(
    *,
    organization_id: str,
    raw_source_ids: Sequence[str],
) -> list[RawMemory]:
    memories: list[RawMemory] = []
    for source_id in dict.fromkeys(raw_source_ids):
        source = await get_raw_memory(
            organization_id=organization_id,
            memory_id=str(source_id),
        )
        if source is not None:
            memories.append(source)
    return memories


def _principal_denial(
    memories: Sequence[RawMemory],
    *,
    candidate_id: str,
    principal_id: str | None,
    raw_source_ids: list[str],
) -> NativeReflectionPromotionResult | None:
    if not principal_id:
        return _promotion_denied(
            candidate_id=candidate_id,
            reason="principal_mismatch",
            review_state=memories[0].review_state,
            memory_scope=memories[0].memory_scope,
            scope_key=memories[0].scope_key,
            raw_source_ids=raw_source_ids,
        )
    for memory in memories:
        if memory.memory_scope is MemoryScope.PRIVATE and memory.principal_id != principal_id:
            return _promotion_denied(
                candidate_id=candidate_id,
                reason="principal_mismatch",
                review_state=memories[0].review_state,
                memory_scope=memory.memory_scope,
                scope_key=memory.scope_key,
                raw_source_ids=raw_source_ids,
            )
    return None


def _coerce_promotion_scope(value: MemoryScope | str | None) -> MemoryScope | None:
    if isinstance(value, MemoryScope):
        return value
    if value is None:
        return None
    try:
        return MemoryScope(str(value))
    except ValueError:
        return None


def _scope_identity(memory: RawMemory) -> tuple[MemoryScope, str | None]:
    return memory.memory_scope, memory.scope_key


def _has_mixed_scope_inputs(memories: Sequence[RawMemory]) -> bool:
    return len({_scope_identity(memory) for memory in memories}) > 1


def _broadest_scope(memories: Sequence[RawMemory]) -> MemoryScope:
    return max((memory.memory_scope for memory in memories), key=lambda scope: _SCOPE_RANK[scope])


def _scope_metadata(memories: Sequence[RawMemory]) -> list[dict[str, str | None]]:
    return [
        {
            "id": memory.id,
            "memory_scope": memory.memory_scope.value,
            "scope_key": memory.scope_key,
        }
        for memory in memories
    ]


def _missing_promotion_target_reason(
    candidate_memory: RawMemory,
    input_memories: Sequence[RawMemory],
) -> str:
    if _has_mixed_scope_inputs(input_memories):
        return "mixed_scope_inputs_require_promote_to_scope"
    suggested_scope = _coerce_promotion_scope(
        _metadata_str(candidate_memory.metadata, "suggested_memory_scope")
    )
    suggested_key = _metadata_str(candidate_memory.metadata, "suggested_scope_key")
    if suggested_scope and (
        suggested_scope is not candidate_memory.memory_scope
        or suggested_key != candidate_memory.scope_key
    ):
        return "scope_crossing_requires_promotion"
    return "missing_promote_to_scope"


def _resolve_promotion_scope_key(
    *,
    target_scope: MemoryScope,
    promote_to_scope_key: str | None,
    project: str | None,
    candidate_memory: RawMemory,
) -> str | None:
    if promote_to_scope_key:
        return promote_to_scope_key
    if target_scope is MemoryScope.PROJECT:
        return project or _metadata_str(candidate_memory.metadata, "suggested_scope_key")
    return None


def _candidate_from_review_memory(
    memory: RawMemory,
    *,
    raw_source_ids: list[str],
    target_scope: MemoryScope,
    target_scope_key: str | None,
    domain: str | None,
) -> ReflectionCandidate:
    metadata = {
        **memory.metadata,
        "raw_source_ids": raw_source_ids,
        "source_ids": raw_source_ids,
        "review_capture_id": memory.id,
        "suggested_memory_scope": target_scope.value,
        "suggested_scope_key": target_scope_key,
        "review_state": memory.review_state,
    }
    resolved_domain = domain or _metadata_str(memory.metadata, "domain")
    if resolved_domain:
        metadata["domain"] = resolved_domain
    return ReflectionCandidate(
        kind=memory.entity_type or _metadata_str(memory.metadata, "remember_kind") or "episode",
        title=memory.title,
        content=memory.raw_content,
        reason=_metadata_str(memory.metadata, "reflection_reason") or "accepted for promotion",
        confidence=_metadata_float(memory.metadata, "reflection_confidence", 1.0),
        tags=list(memory.tags),
        metadata=metadata,
        raw_source_ids=list(raw_source_ids),
        suggested_memory_scope=target_scope.value,
        suggested_scope_key=target_scope_key,
        review_state=memory.review_state,
    )


def _entity_type(kind: str) -> EntityType:
    try:
        return EntityType(kind)
    except ValueError:
        return EntityType.EPISODE


def _entity_from_candidate(
    candidate: ReflectionCandidate,
    *,
    organization_id: str,
    principal_id: str | None,
    domain: str | None,
    project: str | None,
    source_id: str | None,
    memory_scope: MemoryScope,
    scope_key: str | None,
    policy_metadata: Mapping[str, Any],
) -> Entity:
    entity_type = _entity_type(candidate.kind)
    entity_id = _generate_id(entity_type.value, candidate.title, domain or "general")
    source_ids = _candidate_source_ids(candidate, source_id)
    primary_source_id = source_id or (source_ids[0] if source_ids else None)
    metadata = {
        **candidate.metadata,
        "category": domain,
        "tags": list(candidate.tags),
        "organization_id": organization_id,
        "capture_mode": "reflect",
        "capture_surface": "reflection",
        "remember_kind": candidate.kind,
        "reflection_reason": candidate.reason,
        "reflection_confidence": candidate.confidence,
        "raw_source_ids": source_ids,
        "source_ids": source_ids,
        "native_write_path": "reflection_promotion",
        **dict(policy_metadata),
    }
    if project:
        metadata["project_id"] = project
    if primary_source_id:
        metadata["reflection_source_id"] = primary_source_id

    return Entity(
        id=entity_id,
        entity_type=entity_type,
        name=candidate.title,
        description=candidate.content[:500],
        content=candidate.content,
        organization_id=organization_id,
        created_by=principal_id,
        metadata=metadata,
        source_file=primary_source_id,
    )


def _relationships_for_promotion(
    entity_id: str,
    *,
    project: str | None,
    source_id: str | None,
    related_to: Sequence[str] | None,
    supersedes: Sequence[str] | None,
    raw_source_ids: Sequence[str] | None,
) -> list[Relationship]:
    relationships: list[Relationship] = []
    if project and project != entity_id:
        relationships.append(
            _relationship(
                entity_id,
                project,
                RelationshipType.BELONGS_TO,
                metadata={"native_write_path": "reflection_promotion"},
            )
        )
    if source_id and source_id != entity_id:
        relationships.append(
            _relationship(
                entity_id,
                source_id,
                RelationshipType.DERIVED_FROM,
                metadata={"native_write_path": "reflection_promotion", "source_id": source_id},
            )
        )
    excluded_targets = {entity_id, project, source_id}
    for related_id in related_to or ():
        if related_id in excluded_targets:
            continue
        relationships.append(
            _relationship(
                entity_id,
                related_id,
                RelationshipType.RELATED_TO,
                metadata={"native_write_path": "reflection_promotion"},
            )
        )
    for superseded_id in supersedes or ():
        if superseded_id in excluded_targets:
            continue
        relationships.append(
            _relationship(
                entity_id,
                superseded_id,
                RelationshipType.SUPERSEDES,
                metadata={
                    "native_write_path": "reflection_promotion",
                    "raw_source_ids": list(raw_source_ids or []),
                },
            )
        )
    return relationships


def _relationship(
    source_id: str,
    target_id: str,
    relationship_type: RelationshipType,
    *,
    metadata: dict[str, Any],
) -> Relationship:
    return Relationship(
        id=f"rel_{source_id}_{relationship_type.value.lower()}_{target_id}",
        source_id=source_id,
        target_id=target_id,
        relationship_type=relationship_type,
        metadata={**metadata, "created_at": datetime.now(UTC).isoformat()},
    )


__all__ = [
    "NativeReflectionPromotionResult",
    "NativeReflectionWriteResult",
    "NativeWriteMode",
    "coerce_native_write_mode",
    "native_reflection_write_enabled",
    "native_write_mode_from_env",
    "persist_reflection_candidate_native",
    "persist_reflection_source_native",
    "promote_reflection_candidate_review",
]
