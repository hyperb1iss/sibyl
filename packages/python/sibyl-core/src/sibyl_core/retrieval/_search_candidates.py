"""Candidate materialization and authorization for native retrieval."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from sibyl_core.auth.memory_policy import memory_metadata_read_allowed
from sibyl_core.embeddings.providers import EmbeddingMetadata
from sibyl_core.models.context import ContextFacet
from sibyl_core.retrieval._search_plan import (
    RetrievalPlan,
    RetrievalSignal,
    ScopeSpec,
    _explicit_project_denied,
)
from sibyl_core.retrieval.candidates import CandidateKind, CandidateScope, RetrievalCandidate
from sibyl_core.services.surreal_content import MemoryScope, RawMemory

_GRAPH_EXPANSION_METADATA_KEYS = (
    "graph_expansion_depth",
    "graph_expansion_relationship",
    "graph_expansion_score",
    "graph_expansion_direction",
    "graph_expansion_community_id",
)


def _candidate_from_node_record(
    row: Mapping[str, object],
    *,
    signal: RetrievalSignal,
    score: float,
    embedding_metadata: EmbeddingMetadata | None = None,
) -> RetrievalCandidate:
    attributes = _record_attributes(row)
    entity_type = _entity_type_for_record(row, attributes)
    content = _content_for_record(row, attributes)
    project_id = _string_value(row.get("project_id") or attributes.get("project_id"))
    source = _string_value(
        row.get("source_id")
        or attributes.get("source_id")
        or attributes.get("source")
        or row.get("source_file")
        or attributes.get("source_file")
        or row.get("uuid")
    )
    metadata = {
        **attributes,
        **_selected_record_metadata(row),
        "entity_type": entity_type,
        "source_id": source,
        "retrieval_signals": [signal.value],
    }
    if embedding_metadata is not None:
        metadata["embedding_metadata"] = embedding_metadata.to_dict()
    visibility = "project" if project_id else "organization"
    policy_reason = "project_access_verified" if project_id else "graph_projection_allowed"
    return RetrievalCandidate(
        id=str(row.get("uuid", "")),
        type=entity_type,
        name=str(row.get("name") or entity_type),
        content=content,
        score=score,
        source=source,
        metadata=metadata,
        project_id=project_id,
        created_at=_datetime_value(row.get("created_at")),
        policy_reason=policy_reason,
        visibility=visibility,
        kind=CandidateKind.NODE,
        retrieval_signals=(signal.value,),
        scope=CandidateScope(
            organization_id=_string_value(row.get("group_id")),
            project_id=project_id,
            visibility=visibility,
            policy_reason=policy_reason,
        ),
    )


def _candidate_from_episode_record(
    row: Mapping[str, object],
    *,
    signal: RetrievalSignal,
    score: float,
) -> RetrievalCandidate:
    source = _string_value(row.get("source_description")) or _string_value(row.get("uuid"))
    policy_reason = "graph_projection_allowed"
    visibility = "organization"
    metadata = {
        **_selected_record_metadata(row),
        "entity_type": "episode",
        "source_id": source,
        "retrieval_signals": [signal.value],
    }
    return RetrievalCandidate(
        id=str(row.get("uuid", "")),
        type="episode",
        name=str(row.get("name") or "Episode"),
        content=str(row.get("content") or ""),
        score=score,
        source=source,
        metadata=metadata,
        created_at=_datetime_value(row.get("created_at")),
        policy_reason=policy_reason,
        visibility=visibility,
        kind=CandidateKind.EPISODE,
        retrieval_signals=(signal.value,),
        scope=CandidateScope(
            organization_id=_string_value(row.get("group_id")),
            visibility=visibility,
            policy_reason=policy_reason,
        ),
    )


def _candidate_from_edge_record(
    row: Mapping[str, object],
    *,
    signal: RetrievalSignal,
    score: float,
    embedding_metadata: EmbeddingMetadata | None = None,
) -> RetrievalCandidate:
    attributes = _record_attributes(row)
    source = _string_value(attributes.get("source_id") or row.get("uuid"))
    metadata = {
        **attributes,
        **_selected_edge_metadata(row),
        "entity_type": "claim",
        "relationship": _string_value(row.get("name")),
        "source_id": source,
        "source_node_uuid": _string_value(row.get("source_node_uuid")),
        "target_node_uuid": _string_value(row.get("target_node_uuid")),
        "retrieval_signals": [signal.value],
    }
    if embedding_metadata is not None:
        metadata["embedding_metadata"] = embedding_metadata.to_dict()
    policy_reason = "graph_projection_allowed"
    visibility = "organization"
    return RetrievalCandidate(
        id=str(row.get("uuid", "")),
        type="claim",
        name=str(row.get("name") or "Relationship"),
        content=str(row.get("fact") or ""),
        score=score,
        source=source,
        metadata=metadata,
        project_id=_string_value(metadata.get("project_id")),
        created_at=_datetime_value(row.get("created_at")),
        policy_reason=policy_reason,
        visibility=visibility,
        kind=CandidateKind.EDGE,
        retrieval_signals=(signal.value,),
        scope=CandidateScope(
            organization_id=_string_value(row.get("group_id")),
            project_id=_string_value(metadata.get("project_id")),
            visibility=visibility,
            policy_reason=policy_reason,
        ),
    )


def _record_attributes(row: Mapping[str, object]) -> dict[str, Any]:
    raw = row.get("attributes")
    if not isinstance(raw, Mapping):
        return {}
    return {str(key): value for key, value in raw.items()}


def _entity_type_for_record(
    row: Mapping[str, object],
    attributes: Mapping[str, Any],
) -> str:
    for value in (
        attributes.get("entity_type"),
        row.get("entity_type"),
        *_labels_without_entity_record(row),
    ):
        if text := _string_value(value):
            return text.lower()
    return "artifact"


def _labels_without_entity_record(row: Mapping[str, object]) -> list[str]:
    labels = row.get("labels")
    if not isinstance(labels, list | tuple):
        return []
    return [str(label) for label in labels if str(label).lower() != "entity"]


def _content_for_record(
    row: Mapping[str, object],
    attributes: Mapping[str, Any],
) -> str:
    # For memory-shaped rows the description is a derived blurb: often
    # content[:500], and for passages one shared summary across every span of an
    # observation. Either way it cannot stand in for content, so the full-text
    # carriers are exhausted first. Tasks invert this and are resolved by the
    # active-work path in tools/context.py rather than here.
    for value in (
        row.get("content"),
        attributes.get("content"),
        attributes.get("description"),
        row.get("description"),
        row.get("summary"),
    ):
        if text := _string_value(value):
            return text
    return ""


def _selected_record_metadata(row: Mapping[str, object]) -> dict[str, object]:
    metadata: dict[str, object] = {}
    for key in (
        "status",
        "priority",
        "complexity",
        "feature",
        "tags",
        "retrieval_keys",
        "project_id",
        "epic_id",
        "task_id",
        "source_id",
        "source_ids",
        "confidence",
        "valid_at",
        "valid_from",
        "valid_to",
        "invalid_at",
        "created_by",
        "modified_by",
        "last_recalled_at",
        "last_used_at",
        "retrieval_count",
        "citation_count",
        "misled_count",
        *_GRAPH_EXPANSION_METADATA_KEYS,
    ):
        value = row.get(key)
        if value is not None:
            metadata[key] = value
    return metadata


def _selected_edge_metadata(row: Mapping[str, object]) -> dict[str, object]:
    metadata: dict[str, object] = {}
    for key in (
        "group_id",
        "project_id",
        "source_ids",
        "confidence",
        "valid_at",
        "valid_from",
        "valid_to",
        "invalid_at",
        "expired_at",
        "created_by",
        "modified_by",
        "episodes",
    ):
        value = row.get(key)
        if value is not None:
            metadata[key] = value
    return metadata


def _record_score(row: Mapping[str, object]) -> float:
    raw = row.get("score")
    if isinstance(raw, int | float):
        return float(raw)
    return 1.0


def _candidate_from_raw_memory(
    memory: RawMemory,
    scope: ScopeSpec,
) -> RetrievalCandidate:
    source = memory.source_id or memory.capture_surface
    project_id = (
        memory.metadata.get("project_id")
        or memory.project_id
        or (memory.scope_key if memory.memory_scope is MemoryScope.PROJECT else None)
    )
    metadata = {
        "source_id": source,
        "principal_id": memory.principal_id,
        "memory_scope": memory.memory_scope.value,
        "scope_key": memory.scope_key,
        "capture_surface": memory.capture_surface,
        "tags": list(memory.tags),
        **memory.metadata,
    }
    return RetrievalCandidate(
        id=f"raw_memory:{memory.id}",
        type="raw_memory",
        name=memory.title or "Untitled raw memory",
        content=memory.raw_content,
        score=memory.score,
        source=source,
        metadata=metadata,
        result_origin="graph",
        project_id=str(project_id) if project_id is not None else None,
        created_at=memory.captured_at,
        policy_reason=scope.policy_reason,
        visibility=memory.memory_scope.value,
        kind=CandidateKind.RAW_MEMORY,
        retrieval_signals=(RetrievalSignal.RAW_LEXICAL.value,),
        scope=CandidateScope(
            project_id=str(project_id) if project_id is not None else None,
            memory_scope=memory.memory_scope.value,
            scope_key=memory.scope_key,
            principal_id=memory.principal_id,
            visibility=memory.memory_scope.value,
            policy_reason=scope.policy_reason,
        ),
    )


def _string_value(value: object) -> str | None:
    if value is None or value == "":
        return None
    return str(value)


def _datetime_value(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


def _candidate_allowed(
    candidate: RetrievalCandidate,
    *,
    plan: RetrievalPlan,
    requested_types: set[str],
    facet: ContextFacet | None,
) -> bool:
    if requested_types and not _candidate_matches_types(candidate, requested_types, facet):
        return False
    if not _candidate_scope_allowed(candidate, plan):
        return False
    if _explicit_project_denied(plan) and candidate.type != "raw_memory":
        return False
    if candidate.type == "episode" and (plan.project or plan.accessible_projects is not None):
        return False
    if plan.project and candidate.project_id and candidate.project_id != plan.project:
        return False
    if candidate.type == "claim" and plan.accessible_projects is not None:
        endpoint_project_ids = {
            _string_value(candidate.metadata.get("source_node_project_id")),
            _string_value(candidate.metadata.get("target_node_project_id")),
        }
        endpoint_project_ids.discard(None)
        if endpoint_project_ids and not endpoint_project_ids.issubset(plan.accessible_projects):
            return False
    return not (
        plan.accessible_projects is not None
        and candidate.project_id is not None
        and candidate.project_id not in plan.accessible_projects
    )


def _candidate_scope_allowed(candidate: RetrievalCandidate, plan: RetrievalPlan) -> bool:
    metadata = candidate.metadata if isinstance(candidate.metadata, Mapping) else {}
    return memory_metadata_read_allowed(
        metadata,
        principal_id=plan.scopes[0].principal_id if plan.scopes else None,
        project_id=plan.project,
        accessible_projects=plan.accessible_projects,
        agent_id=next((scope.agent_id for scope in plan.scopes if scope.agent_id), None),
        # A private candidate is only authorized if a private scope survived
        # plan filtering; otherwise an API key without a private memory grant
        # could still read the principal's own private graph rows.
        private_scope_granted=any(
            scope.memory_scope is MemoryScope.PRIVATE for scope in plan.scopes
        ),
    )


def _candidate_matches_types(
    candidate: RetrievalCandidate,
    requested_types: set[str],
    facet: ContextFacet | None,
) -> bool:
    if candidate.type in requested_types:
        return True
    if candidate.type == "claim" and "relationship" in requested_types:
        return bool(candidate.metadata.get("relationship"))
    return candidate.type == "raw_memory" and facet is ContextFacet.RECENT_MEMORY
