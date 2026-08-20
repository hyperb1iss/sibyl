"""Canonical serialization ownership for entity routes."""

from datetime import datetime
from typing import Any

import structlog
from fastapi import HTTPException

from sibyl.api.errors import unprocessable_entity
from sibyl.api.schemas import (
    EntityCreate,
    EntityResponse,
    RawCaptureResponse,
    RawCaptureSummary,
)
from sibyl.persistence.content_common import RawCaptureRecord
from sibyl_core.auth.memory_policy import (
    stamp_memory_scope_metadata,
)
from sibyl_core.memory_pipeline.retrieval_keys import normalize_retrieval_keys
from sibyl_core.memory_pipeline.structure import strip_structure_metadata
from sibyl_core.models.entities import Entity, EntityType
from sibyl_core.tools.helpers import _generate_id

log = structlog.get_logger()

BULK_UNSUPPORTED_TYPES = frozenset(
    {EntityType.DOCUMENT, EntityType.EPIC, EntityType.PROJECT, EntityType.TASK}
)
_RAW_CAPTURE_METADATA_DENYLIST = frozenset(
    {
        "principal_id",
        "memory_scope",
        "scope_key",
        "agent_id",
        "project_id",
        "review_state",
        "source_id",
        "raw_source_id",
    }
)
_RAW_CAPTURE_REVIEW_STATES = frozenset({"pending", "deferred", "promoted", "archived"})


def sanitize_raw_capture_metadata(metadata: dict[str, object]) -> dict[str, object]:
    """Drop caller-controlled fields that map to authoritative capture columns."""
    return {
        key: value for key, value in metadata.items() if key not in _RAW_CAPTURE_METADATA_DENYLIST
    }


def scoped_graph_metadata(
    metadata: dict[str, Any] | None,
    *,
    principal_id: str | None,
    verified_project_id: str | None,
) -> dict[str, Any]:
    """Stamp a write's scope metadata from the authenticated request context.

    The scope key is taken from the project this request already proved access
    to, so a payload cannot address a row at a project the caller is not a
    contributor on.
    """
    declared = (metadata or {}).get("memory_scope")
    return stamp_memory_scope_metadata(
        metadata,
        memory_scope=declared,
        scope_key=verified_project_id,
        principal_id=principal_id,
    )


def normalized_raw_capture_review_state(value: object) -> str:
    state = str(value or "pending").strip().lower()
    return state if state in _RAW_CAPTURE_REVIEW_STATES else "pending"


def _raw_capture_review_state(capture: RawCaptureRecord) -> str:
    return normalized_raw_capture_review_state(capture.review_state)


def serialize_raw_capture_summary(capture: RawCaptureRecord) -> RawCaptureSummary:
    return RawCaptureSummary(
        id=str(capture.id),
        entity_id=capture.entity_id,
        title=capture.title,
        entity_type=capture.entity_type,
        tags=list(capture.tags or []),
        metadata=dict(capture.metadata or {}),
        capture_surface=capture.capture_surface,
        review_state=_raw_capture_review_state(capture),
        created_by_user_id=str(capture.created_by_user_id) if capture.created_by_user_id else None,
        created_at=capture.created_at,
    )


def serialize_raw_capture(capture: RawCaptureRecord) -> RawCaptureResponse:
    return RawCaptureResponse(
        **serialize_raw_capture_summary(capture).model_dump(),
        raw_content=capture.raw_content,
    )


def bulk_create_metadata(
    entity: EntityCreate,
    *,
    group_id: str,
    now: datetime,
    principal_id: str | None,
) -> dict[str, Any]:
    request_metadata = strip_structure_metadata(entity.metadata)
    project_id = str(request_metadata.get("project_id") or "").strip()
    metadata: dict[str, Any] = {
        "category": entity.category,
        "languages": entity.languages or [],
        "tags": entity.tags or [],
        "added_at": now.isoformat(),
        "organization_id": group_id,
        **scoped_graph_metadata(
            request_metadata,
            principal_id=principal_id,
            verified_project_id=project_id or None,
        ),
    }
    # The bulk path builds its Entity rows directly rather than through add(),
    # so it has to normalize the declaration itself or the field would be
    # accepted on the wire and silently dropped before the row is written.
    declared_keys, _match_forms = normalize_retrieval_keys(entity.retrieval_keys)
    if declared_keys:
        metadata["retrieval_keys"] = declared_keys
    else:
        metadata.pop("retrieval_keys", None)
    return metadata


def reject_unsupported_bulk_entry(entity: EntityCreate) -> None:
    """Fault a batch entry the direct-write path cannot honor.

    All three structure fields are refused rather than accepted and ignored. This
    path writes rows itself and enqueues ``project_memory_batch``, which extracts
    memory entities and never mints passages, so there is no cutter here to honor
    a declared plan and nothing searchable to rehearse a probe against. Storing a
    validated plan no cutter reads would be a promise with no keeper.
    """
    if entity.entity_type in BULK_UNSUPPORTED_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"{entity.entity_type.value} entities are not supported by bulk create",
        )
    if not entity.skip_conflicts:
        raise HTTPException(
            status_code=400,
            detail=(
                "bulk create requires skip_conflicts=true; "
                "use POST /entities for conflict detection"
            ),
        )
    declared = [
        name
        for name, sent in (
            ("spans", entity.spans is not None),
            ("atomic", entity.atomic),
            ("probes", bool(entity.probes)),
        )
        if sent
    ]
    if declared:
        raise unprocessable_entity(
            f"{', '.join(declared)} not supported on bulk create; "
            "write the memory through POST /entities",
            field=declared[0],
        )


def entity_from_bulk_create(
    entity: EntityCreate,
    *,
    group_id: str,
    now: datetime,
    principal_id: str | None = None,
) -> Entity:
    content = entity.content or entity.description or entity.name
    metadata = bulk_create_metadata(
        entity,
        group_id=group_id,
        now=now,
        principal_id=principal_id,
    )
    identity_parts = [entity.name, entity.category or "general"]
    project_id = str(metadata.get("project_id") or "").strip()
    if project_id:
        identity_parts.insert(0, project_id)
    return Entity(
        id=_generate_id(entity.entity_type.value, *identity_parts),
        entity_type=entity.entity_type,
        name=entity.name,
        description=entity.description or content[:500],
        content=content,
        organization_id=group_id,
        metadata=metadata,
        created_at=now,
        updated_at=now,
    )


def entity_response_from_bulk_create(
    entity: EntityCreate,
    *,
    entity_id: str,
    group_id: str,
    now: datetime,
    principal_id: str | None = None,
) -> EntityResponse:
    content = entity.content or entity.description or entity.name
    metadata = bulk_create_metadata(
        entity,
        group_id=group_id,
        now=now,
        principal_id=principal_id,
    )
    return EntityResponse(
        id=entity_id,
        entity_type=entity.entity_type,
        name=entity.name,
        description=entity.description or content[:500],
        content=content,
        category=entity.category,
        languages=entity.languages or [],
        tags=entity.tags or [],
        metadata=metadata,
        source_file=None,
        created_at=now,
        updated_at=now,
    )
