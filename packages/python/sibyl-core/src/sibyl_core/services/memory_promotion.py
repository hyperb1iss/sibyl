"""Reflection promotion planning and graph materialization helpers."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from sibyl_core.auth.memory_policy import authorize_memory_read, stamp_memory_scope_metadata
from sibyl_core.errors import EntityNotFoundError
from sibyl_core.memory_pipeline.quality import normalize_memory_quality_metadata
from sibyl_core.models.entities import Entity, EntityType, Relationship, RelationshipType
from sibyl_core.models.reflection import ReflectionCandidate
from sibyl_core.models.relations import declared_relation_targets
from sibyl_core.services.memory_contract import ReflectionPromotionResult
from sibyl_core.services.memory_identity import (
    IDENTITY_KEY,
    reflection_entity_id,
    reflection_identity,
)
from sibyl_core.services.memory_lifecycle import _relationship
from sibyl_core.services.memory_policy import (
    _candidate_source_ids,
    _metadata_float,
    _metadata_str,
    _promotion_denied,
    suppression_target_visible,
)
from sibyl_core.services.surreal_content import (
    MemoryScope,
    RawMemory,
)

_SCOPE_RANK: dict[MemoryScope, int] = {
    MemoryScope.PRIVATE: 0,
    MemoryScope.DELEGATED: 1,
    MemoryScope.PROJECT: 2,
    MemoryScope.TEAM: 3,
    MemoryScope.ORGANIZATION: 4,
    MemoryScope.SHARED: 5,
    MemoryScope.PUBLIC: 6,
}


def _is_reflection_candidate(memory: RawMemory) -> bool:
    return (
        memory.capture_surface == "reflection_candidate"
        or _metadata_str(memory.metadata, "capture_surface") == "reflection_candidate"
    )


def _principal_denial(
    memories: Sequence[RawMemory],
    *,
    candidate_id: str,
    principal_id: str | None,
    raw_source_ids: list[str],
) -> ReflectionPromotionResult | None:
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


def _source_scope_denial(
    memories: Sequence[RawMemory],
    *,
    candidate_id: str,
    principal_id: str | None,
    raw_source_ids: list[str],
    accessible_projects: Iterable[str] | None,
    accessible_teams: Iterable[str] | None = None,
    accessible_delegations: Iterable[str] | None = None,
) -> ReflectionPromotionResult | None:
    for memory in memories:
        read_decision = authorize_memory_read(
            principal_id=principal_id,
            memory_scope=memory.memory_scope,
            scope_key=memory.scope_key,
            accessible_projects=accessible_projects,
            accessible_teams=accessible_teams,
            accessible_delegations=accessible_delegations,
        )
        if not read_decision.allowed:
            return _promotion_denied(
                candidate_id=candidate_id,
                reason=read_decision.reason,
                review_state=memories[0].review_state,
                memory_scope=memory.memory_scope,
                scope_key=memory.scope_key,
                raw_source_ids=raw_source_ids,
                policy_decisions=(read_decision,),
                metadata={"input_scopes": _scope_metadata(memories)},
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
        confidence=_metadata_float(memory.metadata, "confidence", 1.0),
        tags=list(memory.tags),
        metadata=metadata,
        raw_source_ids=list(raw_source_ids),
        suggested_memory_scope=target_scope.value,
        suggested_scope_key=target_scope_key,
        review_state=memory.review_state,
    )


def _candidate_from_raw_memory(
    memory: RawMemory,
    *,
    target_scope: MemoryScope,
    target_scope_key: str | None,
    domain: str | None,
) -> ReflectionCandidate:
    metadata = {
        **memory.metadata,
        "capture_mode": "promote",
        "imported_capture_id": memory.id,
        "native_write_path": "raw_memory_promotion",
        "promoted_capture_surface": "raw_memory_promotion",
        "raw_source_ids": [memory.id],
        "source_ids": [memory.id],
        "suggested_memory_scope": target_scope.value,
        "suggested_scope_key": target_scope_key,
    }
    resolved_domain = domain or _metadata_str(memory.metadata, "domain")
    if resolved_domain:
        metadata["domain"] = resolved_domain
    return ReflectionCandidate(
        kind=memory.entity_type or _metadata_str(memory.metadata, "remember_kind") or "episode",
        title=memory.title,
        content=memory.raw_content,
        reason=_metadata_str(memory.metadata, "promotion_reason")
        or "accepted raw memory for promotion",
        confidence=_metadata_float(memory.metadata, "confidence", 1.0),
        tags=list(memory.tags),
        metadata=metadata,
        raw_source_ids=[memory.id],
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
    source_ids = sorted(_candidate_source_ids(candidate, source_id))
    primary_source_id = source_id or (source_ids[0] if source_ids else None)
    native_write_path = _metadata_str(candidate.metadata, "native_write_path")
    if not native_write_path:
        native_write_path = "reflection_promotion"
    capture_mode = _metadata_str(candidate.metadata, "capture_mode") or "reflect"
    capture_surface = _metadata_str(candidate.metadata, "promoted_capture_surface")
    if not capture_surface:
        capture_surface = "reflection"
    metadata = normalize_memory_quality_metadata(
        # The candidate bag reaches here from a capture's caller-supplied
        # metadata, so the owner fields it carries are rebuilt from the values
        # this promotion was authorized against rather than trusted.
        stamp_memory_scope_metadata(
            {
                **candidate.metadata,
                "tags": list(candidate.tags),
                "organization_id": organization_id,
                "capture_mode": capture_mode,
                "capture_surface": capture_surface,
                "remember_kind": candidate.kind,
                "reflection_reason": candidate.reason,
                "confidence": candidate.confidence,
                "raw_source_ids": source_ids,
                "source_ids": source_ids,
                "native_write_path": native_write_path,
                **dict(policy_metadata),
            },
            memory_scope=memory_scope,
            scope_key=scope_key,
            principal_id=principal_id,
        )
    )
    if domain:
        metadata["category"] = domain
    elif metadata.get("category") is None:
        metadata.pop("category", None)
    if project:
        metadata["project_id"] = project
    else:
        metadata.pop("project_id", None)
    if primary_source_id:
        metadata["reflection_source_id"] = primary_source_id

    entity = Entity(
        id="reflection_pending",
        entity_type=entity_type,
        name=candidate.title,
        description=candidate.content[:500],
        content=candidate.content,
        organization_id=organization_id,
        created_by=principal_id,
        metadata=metadata,
        source_file=primary_source_id,
    )
    entity.id = reflection_entity_id(entity)
    entity.metadata[IDENTITY_KEY] = reflection_identity(entity)
    return entity


async def _linkable_related_targets(
    *,
    runtime: Any,
    related_to: Sequence[str] | None,
    principal_id: str | None,
    accessible_projects: Iterable[str] | None,
) -> list[str]:
    """The declared link targets this principal is allowed to name.

    Promotion reports how many relationships it requested and how many it
    wrote, and the graph writer silently skips an edge whose endpoint does not
    resolve. Those two facts together turn the link list into an existence
    oracle: a guessed id that names a real row lands as a created edge, and one
    that names nothing lands as a shortfall. Dropping targets this caller
    cannot see collapses both answers into the same one, so the counts carry no
    information about rows the caller was never allowed to know about.

    Predicates are resolved to their target here as everywhere else, but
    promotion still links untyped; `supersedes` on this path is minted only
    from the authorized channel.
    """
    linkable: list[str] = []
    for target_id in declared_relation_targets(list(related_to or ())):
        # Absence only. A store that is merely unreachable must not read as a
        # target that is not there, because dropping the edge silently is
        # indistinguishable in the receipt from never having been asked for it.
        try:
            target = await runtime.entity_manager.get(target_id)
        except (EntityNotFoundError, KeyError):
            continue
        if target is None:
            continue
        if suppression_target_visible(
            target,
            principal_id=principal_id,
            accessible_projects=accessible_projects,
        ):
            linkable.append(target_id)
    return linkable


def _relationships_for_promotion(
    entity_id: str,
    *,
    project: str | None,
    source_id: str | None,
    related_to: Sequence[str] | None,
    supersedes: Sequence[str] | None,
    raw_source_ids: Sequence[str] | None,
    native_write_path: str = "reflection_promotion",
) -> list[Relationship]:
    relationships: list[Relationship] = []
    if project and project != entity_id:
        relationships.append(
            _relationship(
                entity_id,
                project,
                RelationshipType.BELONGS_TO,
                metadata={"native_write_path": native_write_path},
            )
        )
    if source_id and source_id != entity_id:
        relationships.append(
            _relationship(
                entity_id,
                source_id,
                RelationshipType.DERIVED_FROM,
                metadata={"native_write_path": native_write_path, "source_id": source_id},
            )
        )
    excluded_targets = {entity_id, project, source_id}
    # Targets arrive already resolved and already filtered to what the caller
    # may see. SUPERSEDES on this path is minted only from `supersedes`, whose
    # targets passed `_authorized_superseded_entity_ids`; honoring a predicate
    # declared on the free `related_to` channel would route around that gate.
    for related_id in related_to or ():
        if related_id in excluded_targets:
            continue
        relationships.append(
            _relationship(
                entity_id,
                related_id,
                RelationshipType.RELATED_TO,
                metadata={"native_write_path": native_write_path},
            )
        )
    for superseded_id in supersedes or ():
        if superseded_id in excluded_targets:
            continue
        source_ids = list(raw_source_ids or [])
        valid_from = datetime.now(UTC).isoformat()
        relationships.append(
            _relationship(
                entity_id,
                superseded_id,
                RelationshipType.SUPERSEDES,
                metadata={
                    "native_write_path": native_write_path,
                    "raw_source_ids": source_ids,
                    "source_id": source_ids[0] if source_ids else None,
                    "replacement_reason": "accepted_reflection_candidate",
                    "valid_from": valid_from,
                },
            )
        )
    return relationships
