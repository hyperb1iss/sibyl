"""Entity CRUD endpoints.

Full create, read, update, delete operations for all entity types.
Transparently handles both graph entities and document chunks.
"""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Any
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request

from sibyl.api.decorators import handle_workflow_errors
from sibyl.api.dependencies import get_knowledge_read_service
from sibyl.api.errors import constraint_violation, sanitize_error_text
from sibyl.api.event_types import WSEvent
from sibyl.api.idempotency import replay_idempotent_response, save_idempotent_response
from sibyl.api.schemas import (
    EntityBulkCreateRequest,
    EntityBulkCreateResponse,
    EntityCreate,
    EntityListResponse,
    EntityResponse,
    EntityUpdate,
    RawCaptureListResponse,
    RawCaptureResponse,
    RawCaptureReviewUpdate,
    RawCaptureSummary,
    RelatedEntitySummary,
)
from sibyl.api.websocket import broadcast_event
from sibyl.auth.api_key_common import api_key_memory_scope_key
from sibyl.auth.authorization import verify_entity_project_access
from sibyl.auth.context import AuthContext
from sibyl.auth.dependencies import (
    get_auth_context,
    get_current_organization,
    require_org_role,
)
from sibyl.persistence import content_runtime
from sibyl.persistence.auth_runtime import (
    create_project_record,
    delete_project_record,
    list_accessible_delegated_scope_keys,
    list_accessible_project_graph_ids,
    log_audit_event,
    update_project_record,
)
from sibyl.persistence.content_common import RawCaptureRecord
from sibyl.persistence.content_runtime import (
    get_content_read_session,
    get_content_read_session_dependency,
    save_raw_capture_record,
)
from sibyl_core.auth import AuthOrganization, MemoryPolicyContext, OrganizationRole, ProjectRole
from sibyl_core.auth.memory_policy import authorize_memory_read
from sibyl_core.models.entities import Entity, EntityType, Relationship, RelationshipType
from sibyl_core.projection import extract_projected_memory_entities
from sibyl_core.services import KnowledgeReadService
from sibyl_core.tools.helpers import _generate_id

log = structlog.get_logger()


async def get_entity_graph_runtime(group_id: str):
    from sibyl.persistence.graph_runtime import get_entity_graph_runtime as service

    return await service(group_id)


class SortField(StrEnum):
    """Fields available for sorting entities."""

    NAME = "name"
    CREATED_AT = "created_at"
    UPDATED_AT = "updated_at"
    ENTITY_TYPE = "entity_type"


class SortOrder(StrEnum):
    """Sort order direction."""

    ASC = "asc"
    DESC = "desc"


_READ_ROLES = (
    OrganizationRole.OWNER,
    OrganizationRole.ADMIN,
    OrganizationRole.MEMBER,
    OrganizationRole.VIEWER,
)
_WRITE_ROLES = (
    OrganizationRole.OWNER,
    OrganizationRole.ADMIN,
    OrganizationRole.MEMBER,
)

router = APIRouter(
    prefix="/entities",
    tags=["entities"],
    dependencies=[Depends(require_org_role(*_READ_ROLES))],
)

LIST_ALL_PAGE_SIZE = 2000
LIST_BY_TYPE_PAGE_SIZE = 1000
GRAPH_ENTITY_ID_PREFIXES = frozenset(
    {entity_type.value for entity_type in EntityType if entity_type is not EntityType.DOCUMENT}
)
LIST_RESPONSE_CONTENT = ""
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


def _sanitize_raw_capture_metadata(metadata: dict[str, object]) -> dict[str, object]:
    """Drop caller-controlled fields that map to authoritative capture columns."""
    return {
        key: value for key, value in metadata.items() if key not in _RAW_CAPTURE_METADATA_DENYLIST
    }


# =============================================================================
# Helpers
# =============================================================================


def _entity_is_archived(entity: Any) -> bool:
    metadata = getattr(entity, "metadata", None) or {}
    return bool(metadata.get("archived")) or str(metadata.get("status", "")).lower() == "archived"


def _should_fallback_to_document_entity(entity_id: str) -> bool:
    if any(entity_id.startswith(f"{prefix}_") for prefix in GRAPH_ENTITY_ID_PREFIXES):
        return False
    try:
        UUID(entity_id)
        return True
    except ValueError:
        normalized = entity_id.lower().replace("-", "")
        return len(normalized) >= 4 and all(char in "0123456789abcdef" for char in normalized)


async def _archive_raw_capture(
    session: Any,
    *,
    organization_id: UUID,
    user_id: UUID | None,
    entity_id: str,
    entity_name: str,
    entity_content: str,
    entity_type: str,
    tags: list[str],
    metadata: dict[str, object],
) -> None:
    """Persist the write-once capture sidecar."""
    capture_surface_value = metadata.get("capture_surface")
    await save_raw_capture_record(
        session,
        capture=RawCaptureRecord(
            organization_id=organization_id,
            principal_id=str(user_id) if user_id else "",
            memory_scope=str(metadata.get("memory_scope") or "private"),
            scope_key=str(metadata["scope_key"]) if metadata.get("scope_key") else None,
            agent_id=str(metadata["agent_id"]) if metadata.get("agent_id") else None,
            project_id=str(metadata["project_id"]) if metadata.get("project_id") else None,
            review_state=str(metadata.get("review_state") or "pending"),
            entity_id=entity_id,
            title=entity_name,
            raw_content=entity_content,
            entity_type=entity_type,
            tags=tags,
            metadata=metadata,
            capture_surface=str(capture_surface_value) if capture_surface_value else None,
            created_by_user_id=user_id,
        ),
    )


def _raw_capture_review_state(capture: RawCaptureRecord) -> str:
    metadata_state = str((capture.metadata or {}).get("review_state") or "").strip()
    if metadata_state and (not capture.review_state or capture.review_state == "pending"):
        return metadata_state
    return capture.review_state or "pending"


def _serialize_raw_capture_summary(capture: RawCaptureRecord) -> RawCaptureSummary:
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


def _serialize_raw_capture(capture: RawCaptureRecord) -> RawCaptureResponse:
    return RawCaptureResponse(
        **_serialize_raw_capture_summary(capture).model_dump(),
        raw_content=capture.raw_content,
    )


def _entity_from_bulk_create(
    entity: EntityCreate,
    *,
    group_id: str,
    now: datetime,
) -> Entity:
    content = entity.content or entity.description or entity.name
    metadata: dict[str, Any] = {
        "category": entity.category,
        "languages": entity.languages or [],
        "tags": entity.tags or [],
        "added_at": now.isoformat(),
        "organization_id": group_id,
        **dict(entity.metadata or {}),
    }
    return Entity(
        id=_generate_id(entity.entity_type.value, entity.name, entity.category or "general"),
        entity_type=entity.entity_type,
        name=entity.name,
        description=entity.description or content[:500],
        content=content,
        organization_id=group_id,
        metadata=metadata,
        created_at=now,
        updated_at=now,
    )


def _entity_response_from_bulk_create(
    entity: EntityCreate,
    *,
    entity_id: str,
    group_id: str,
    now: datetime,
) -> EntityResponse:
    content = entity.content or entity.description or entity.name
    metadata: dict[str, Any] = {
        "category": entity.category,
        "languages": entity.languages or [],
        "tags": entity.tags or [],
        "added_at": now.isoformat(),
        "organization_id": group_id,
        **dict(entity.metadata or {}),
    }
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


def _capture_policy_value(value: object | None) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


def _capture_memory_scope(capture: RawCaptureRecord) -> str:
    metadata = capture.metadata or {}
    return _capture_policy_value(capture.memory_scope or metadata.get("memory_scope")) or "private"


def _capture_scope_key(capture: RawCaptureRecord, memory_scope: str) -> str | None:
    metadata = capture.metadata or {}
    if memory_scope == "private":
        return _capture_policy_value(
            capture.principal_id
            or metadata.get("principal_id")
            or (str(capture.created_by_user_id) if capture.created_by_user_id else "")
        )
    if memory_scope == "project":
        return _capture_policy_value(
            capture.scope_key
            or capture.project_id
            or metadata.get("scope_key")
            or metadata.get("project_id")
        )
    return _capture_policy_value(capture.scope_key or metadata.get("scope_key"))


def _raw_capture_api_key_scope_allowed(
    *,
    ctx: AuthContext,
    memory_scope: str,
    scope_key: str | None,
) -> bool:
    allowed_scope_keys = getattr(ctx, "api_key_memory_scope_keys", None)
    if allowed_scope_keys is None:
        return True
    if not isinstance(allowed_scope_keys, list | tuple | set | frozenset):
        return True
    return api_key_memory_scope_key(memory_scope, scope_key) in {
        str(scope_key) for scope_key in allowed_scope_keys
    }


def _api_key_delegated_scope_keys(ctx: AuthContext) -> set[str]:
    allowed_scope_keys = getattr(ctx, "api_key_memory_scope_keys", None)
    if not isinstance(allowed_scope_keys, list | tuple | set | frozenset):
        return set()

    prefix = api_key_memory_scope_key("delegated", "")
    return {
        scope_key[len(prefix) :]
        for scope_key in (str(value) for value in allowed_scope_keys)
        if scope_key.startswith(prefix) and scope_key != prefix
    }


def _raw_capture_visible_to_reader(
    capture: RawCaptureRecord,
    *,
    ctx: AuthContext,
    accessible_projects: set[str],
    accessible_delegations: set[str],
) -> bool:
    reader_user_id = getattr(ctx, "user_id", None)
    memory_scope = _capture_memory_scope(capture)
    scope_key = _capture_scope_key(capture, memory_scope)

    if memory_scope == "private" and scope_key != reader_user_id:
        return False

    if not _raw_capture_api_key_scope_allowed(
        ctx=ctx,
        memory_scope=memory_scope,
        scope_key=scope_key,
    ):
        return False

    policy_context = MemoryPolicyContext(
        actor_user_id=reader_user_id,
        organization_id=getattr(ctx, "organization_id", None),
        organization_role=getattr(ctx, "org_role", None),
        memory_space=memory_scope,
        scope_key=scope_key,
        project_id=scope_key if memory_scope == "project" else capture.project_id,
        agent_id=capture.agent_id,
        accessible_projects=accessible_projects,
        accessible_delegations=accessible_delegations,
        source_surface="entities_raw_capture",
    )
    return authorize_memory_read(policy_context=policy_context).allowed


async def _list_all_entities_paginated(
    entity_manager: Any,
    *,
    batch_size: int | None = None,
) -> list[Any]:
    batch_size = LIST_ALL_PAGE_SIZE if batch_size is None else batch_size
    list_kwargs: dict[str, Any] = {
        "limit": batch_size,
        "offset": 0,
        "include_archived": True,
        **_lightweight_entity_list_kwargs(entity_manager),
    }
    batch = await entity_manager.list_all(**list_kwargs)
    return [entity for entity in batch if not _entity_is_archived(entity)]


async def _list_entities_by_type_paginated(
    entity_manager: Any,
    entity_type: EntityType,
    *,
    project_id: str | None = None,
    batch_size: int | None = None,
) -> list[Any]:
    batch_size = LIST_BY_TYPE_PAGE_SIZE if batch_size is None else batch_size
    entities: list[Any] = []
    offset = 0

    while True:
        list_kwargs: dict[str, Any] = {
            "limit": batch_size,
            "offset": offset,
            "include_archived": True,
            **_lightweight_entity_list_kwargs(entity_manager),
        }
        if project_id:
            list_kwargs["project_id"] = project_id

        batch = await entity_manager.list_by_type(entity_type, **list_kwargs)
        if not batch:
            break

        entities.extend(entity for entity in batch if not _entity_is_archived(entity))
        offset += batch_size

    return entities


def _entity_project_id(entity: Any) -> str | None:
    return getattr(entity, "project_id", None) or (
        entity.metadata.get("project_id") if getattr(entity, "metadata", None) else None
    )


def _entity_type_value(entity: Any) -> str:
    entity_type = getattr(entity, "entity_type", None)
    value = getattr(entity_type, "value", entity_type)
    return str(value or "")


def _lightweight_entity_list_kwargs(entity_manager: Any) -> dict[str, bool]:
    if getattr(entity_manager, "supports_lightweight_entity_list", False) is True:
        return {"include_content": False}
    return {}


def _entity_read_project_id(entity: Any) -> str | None:
    if _entity_type_value(entity) == EntityType.PROJECT.value:
        entity_id = getattr(entity, "id", None)
        return str(entity_id) if entity_id else None
    return _entity_project_id(entity)


def _entity_visible_to_projects(entity: Any, accessible_projects: set[str]) -> bool:
    project_id = _entity_read_project_id(entity)
    return project_id is None or project_id in accessible_projects


def _entity_visible_to_reader(
    entity: Any,
    *,
    reader_user_id: str | None,
    accessible_projects: set[str],
) -> bool:
    metadata = getattr(entity, "metadata", None) or {}
    memory_scope = str(metadata.get("memory_scope") or "").strip().lower()

    if memory_scope == "project":
        project_id = str(metadata.get("scope_key") or metadata.get("project_id") or "").strip()
        return bool(project_id and project_id in accessible_projects)

    if memory_scope == "private":
        owner = str(metadata.get("principal_id") or "").strip()
        if not owner:
            owner = str(getattr(entity, "created_by_user_id", None) or "").strip()
        return bool(owner and reader_user_id and owner == reader_user_id)

    return True


async def _accessible_project_ids_for_read(ctx: AuthContext) -> set[str]:
    accessible_projects = await list_accessible_project_graph_ids(ctx)
    return {str(project_id) for project_id in accessible_projects or set()}


async def _accessible_delegation_scope_keys_for_read(ctx: AuthContext) -> set[str]:
    accessible_delegations = await list_accessible_delegated_scope_keys(ctx)
    return {
        str(scope_key) for scope_key in accessible_delegations or set()
    } | _api_key_delegated_scope_keys(ctx)


async def _resolve_entity_list_project_filter(
    *,
    ctx: AuthContext,
    project_ids: list[str] | None,
) -> tuple[list[str], list[str], bool]:
    unassigned_marker = "__unassigned__"
    requested_project_ids = list(project_ids or [])

    if requested_project_ids:
        real_project_ids = [
            project_id for project_id in requested_project_ids if project_id != unassigned_marker
        ]
        for project_id in real_project_ids:
            await verify_entity_project_access(
                None,
                ctx,
                project_id,
                required_role=ProjectRole.VIEWER,
            )
        return (
            requested_project_ids,
            list(dict.fromkeys(real_project_ids)),
            unassigned_marker in requested_project_ids,
        )

    accessible_projects = await _accessible_project_ids_for_read(ctx)
    effective_project_ids = [*sorted(accessible_projects), unassigned_marker]
    return effective_project_ids, sorted(accessible_projects), True


async def _require_entity_read_access(ctx: AuthContext, entity: Any) -> set[str]:
    project_id = _entity_read_project_id(entity)
    if project_id is not None:
        await verify_entity_project_access(
            None,
            ctx,
            project_id,
            required_role=ProjectRole.VIEWER,
        )
    accessible_projects = await _accessible_project_ids_for_read(ctx)
    if project_id is not None:
        accessible_projects.add(project_id)
    reader_user_id = str(getattr(getattr(ctx, "user", None), "id", None) or "") or None
    if not _entity_visible_to_reader(
        entity,
        reader_user_id=reader_user_id,
        accessible_projects=accessible_projects,
    ):
        raise HTTPException(status_code=404, detail="Entity not found")
    return accessible_projects


async def _validate_related_to_targets_for_write(
    *,
    ctx: AuthContext,
    entity_manager: Any,
    related_to: list[str] | None,
) -> None:
    if not related_to:
        return

    checked_ids: set[str] = set()
    for related_id in related_to:
        if related_id in checked_ids:
            continue
        checked_ids.add(related_id)

        try:
            related_entity = await entity_manager.get(related_id)
        except Exception as exc:
            raise HTTPException(
                status_code=404, detail=f"Related entity not found: {related_id}"
            ) from exc
        if related_entity is None:
            raise HTTPException(status_code=404, detail=f"Related entity not found: {related_id}")

        related_project_id = _entity_read_project_id(related_entity)
        if related_project_id is not None:
            await verify_entity_project_access(
                None,
                ctx,
                related_project_id,
                required_role=ProjectRole.CONTRIBUTOR,
            )


def _entity_matches_project_filter(
    entity: Any,
    *,
    project_ids: list[str] | None,
    real_project_ids: list[str],
    has_unassigned: bool,
) -> bool:
    if not project_ids:
        return True

    entity_project = _entity_read_project_id(entity)
    if entity_project:
        if real_project_ids and entity_project not in real_project_ids:
            return False
        return not (has_unassigned and not real_project_ids)
    return bool(has_unassigned)


def _entity_matches_list_filters(
    entity: Any,
    *,
    project_ids: list[str] | None,
    real_project_ids: list[str],
    has_unassigned: bool,
    reader_user_id: str | None,
    accessible_projects: set[str],
    language: str | None,
    category: str | None,
    search: str | None,
) -> bool:
    if _entity_is_archived(entity):
        return False

    if not _entity_matches_project_filter(
        entity,
        project_ids=project_ids,
        real_project_ids=real_project_ids,
        has_unassigned=has_unassigned,
    ):
        return False

    if not _entity_visible_to_reader(
        entity,
        reader_user_id=reader_user_id,
        accessible_projects=accessible_projects,
    ):
        return False

    if language:
        entity_langs = getattr(entity, "languages", []) or []
        if language.lower() not in [lang.lower() for lang in entity_langs]:
            return False

    if category:
        metadata = getattr(entity, "metadata", {}) or {}
        entity_cat = getattr(entity, "category", None) or metadata.get("category") or ""
        if category.lower() not in entity_cat.lower():
            return False

    if search:
        search_lower = search.lower()
        name = (getattr(entity, "name", "") or "").lower()
        description = (getattr(entity, "description", "") or "").lower()
        if search_lower not in name and search_lower not in description:
            return False

    return True


def _can_use_bounded_entity_list(
    entity_manager: Any,
    *,
    language: str | None,
    category: str | None,
    search: str | None,
    sort_by: SortField,
    sort_order: SortOrder,
) -> bool:
    surreal_ops = getattr(entity_manager, "_surreal_entity_node_ops", None)
    bounded = getattr(entity_manager, "supports_bounded_entity_list", False) is True
    compatibility_bounded = callable(surreal_ops) and surreal_ops() is not None
    return (
        (bounded or compatibility_bounded)
        and not language
        and not category
        and not search
        and sort_by == SortField.UPDATED_AT
        and sort_order == SortOrder.DESC
    )


async def _list_entities_bounded(
    entity_manager: Any,
    *,
    entity_type: EntityType | None,
    page: int,
    page_size: int,
    project_ids: list[str] | None,
    real_project_ids: list[str],
    has_unassigned: bool,
    single_project_id: str | None,
    reader_user_id: str | None,
    accessible_projects: set[str],
) -> tuple[list[Any], int, bool]:
    start = (page - 1) * page_size
    target = start + page_size + 1
    batch_size = LIST_BY_TYPE_PAGE_SIZE if entity_type else LIST_ALL_PAGE_SIZE
    matched: list[Any] = []
    offset = 0
    exhausted = False

    while len(matched) < target:
        if entity_type:
            list_kwargs: dict[str, Any] = {
                "limit": batch_size,
                "offset": offset,
                "include_archived": True,
                **_lightweight_entity_list_kwargs(entity_manager),
            }
            if single_project_id:
                list_kwargs["project_id"] = single_project_id
            batch = await entity_manager.list_by_type(entity_type, **list_kwargs)
        else:
            batch = await entity_manager.list_all(
                limit=batch_size,
                offset=offset,
                include_archived=True,
                **_lightweight_entity_list_kwargs(entity_manager),
            )
        if not batch:
            exhausted = True
            break

        for entity in batch:
            if _entity_matches_list_filters(
                entity,
                project_ids=project_ids,
                real_project_ids=real_project_ids,
                has_unassigned=has_unassigned,
                reader_user_id=reader_user_id,
                accessible_projects=accessible_projects,
                language=None,
                category=None,
                search=None,
            ):
                matched.append(entity)
                if len(matched) >= target:
                    break

        offset += len(batch)
        if len(batch) < batch_size:
            exhausted = True
            break

    page_entities = matched[start : start + page_size]
    has_more = len(matched) > start + page_size or not exhausted
    total = len(matched) if exhausted else start + len(page_entities) + int(has_more)
    return page_entities, total, has_more


async def _enrich_entity_with_related(
    entity: Any,
    entity_id: str,
    entity_manager: Any,
    relationship_manager: Any,
    preloaded_related: list[RelatedEntitySummary] | None = None,
    *,
    accessible_projects: set[str],
    related_limit: int = 5,
) -> tuple[dict[str, Any], list[RelatedEntitySummary] | None]:
    """Enrich entity metadata and fetch related entities based on entity type.

    Returns (metadata dict, related entities list or None).
    """
    metadata = getattr(entity, "metadata", {}) or {}
    related = preloaded_related

    # Enrich projects with actionable task summary
    if entity.entity_type == "project":
        try:
            summary = await entity_manager.get_project_summary(entity_id)
            metadata = {
                **metadata,
                "total_tasks": summary.get("total_tasks", 0),
                "status_counts": summary.get("status_counts", {}),
                "progress_pct": summary.get("progress_pct", 0.0),
                "critical_tasks": summary.get("critical_tasks", []),
                "epics": summary.get("epics", []),
                "actionable_tasks": summary.get("actionable_tasks", []),
            }
            actionable = summary.get("actionable_tasks", [])
            if actionable and not related:
                related = [
                    RelatedEntitySummary(
                        id=task["id"],
                        name=task["name"],
                        entity_type="task",
                        relationship=task["status"],
                        direction="incoming",
                    )
                    for task in actionable
                ]
        except Exception as proj_err:
            log.debug("Failed to fetch project summary", error=str(proj_err))

    # Enrich epics with progress stats
    elif entity.entity_type == "epic":
        try:
            progress = await entity_manager.get_epic_progress(entity_id)
            metadata = {
                **metadata,
                "total_tasks": progress.get("total_tasks", 0),
                "completed_tasks": progress.get("completed_tasks", 0),
                "in_progress_tasks": progress.get("in_progress_tasks", 0),
                "blocked_tasks": progress.get("blocked_tasks", 0),
                "in_review_tasks": progress.get("in_review_tasks", 0),
                "completion_pct": progress.get("completion_pct", 0.0),
            }
        except Exception as epic_err:
            log.debug("Failed to fetch epic progress", error=str(epic_err))

    # For non-project/epic entities, fetch generic related entities
    if related is None and related_limit > 0:
        related = await _fetch_related_entity_summaries(
            relationship_manager,
            entity_id=entity_id,
            accessible_projects=accessible_projects,
            limit=related_limit,
        )

    return metadata, related


def _summarize_related_entities(
    entity_id: str,
    *,
    related_entities: list[Any],
    relationships: list[Any],
    accessible_projects: set[str],
    limit: int | None = None,
) -> list[RelatedEntitySummary] | None:
    if not related_entities or not relationships:
        return None

    relationships_by_other_id: dict[str, Any] = {}
    for relationship in relationships:
        if relationship.source_id == entity_id:
            other_id = relationship.target_id
            direction = "outgoing"
        elif relationship.target_id == entity_id:
            other_id = relationship.source_id
            direction = "incoming"
        else:
            continue
        relationships_by_other_id.setdefault(other_id, (relationship, direction))

    summaries: list[RelatedEntitySummary] = []
    seen_ids: set[str] = set()
    for related_entity in related_entities:
        if not _entity_visible_to_projects(related_entity, accessible_projects):
            continue
        relationship_pair = relationships_by_other_id.get(related_entity.id)
        if relationship_pair is None:
            continue
        if related_entity.id in seen_ids:
            continue
        seen_ids.add(related_entity.id)
        relationship, direction = relationship_pair
        summaries.append(
            RelatedEntitySummary(
                id=related_entity.id,
                name=related_entity.name,
                entity_type=str(related_entity.entity_type),
                relationship=str(relationship.relationship_type),
                direction=direction,
            )
        )
        if limit is not None and len(summaries) >= limit:
            break

    return summaries or None


async def _fetch_related_entity_summaries(
    relationship_manager: Any,
    *,
    entity_id: str,
    accessible_projects: set[str],
    limit: int,
) -> list[RelatedEntitySummary] | None:
    try:
        related_pairs = await relationship_manager.get_related_entities(
            entity_id=entity_id, limit=limit
        )
        if not related_pairs:
            return None

        seen_ids: set[str] = set()
        deduped: list[RelatedEntitySummary] = []
        for rel_entity, rel in related_pairs:
            if not _entity_visible_to_projects(rel_entity, accessible_projects):
                continue
            if rel_entity.id in seen_ids:
                continue
            seen_ids.add(rel_entity.id)
            deduped.append(
                RelatedEntitySummary(
                    id=rel_entity.id,
                    name=rel_entity.name,
                    entity_type=str(rel_entity.entity_type),
                    relationship=str(rel.relationship_type),
                    direction="outgoing" if rel.source_id == entity_id else "incoming",
                )
            )

        return deduped or None
    except Exception as rel_err:
        log.debug("Failed to fetch related entities", error=str(rel_err))
        return None


# =============================================================================
# List / Read
# =============================================================================


@router.get("/captures", response_model=RawCaptureListResponse)
@handle_workflow_errors("list_raw_captures")
async def list_raw_captures(
    org: AuthOrganization = Depends(get_current_organization),
    ctx: AuthContext = Depends(get_auth_context),
    session: Any = Depends(get_content_read_session_dependency),
    entity_type: str | None = Query(default=None, description="Filter by entity type"),
    capture_surface: str | None = Query(default=None, description="Filter by capture surface"),
    review_state: str | None = Query(default=None, description="Filter by review queue state"),
    limit: int = Query(default=50, ge=1, le=200, description="Items per page"),
    offset: int = Query(default=0, ge=0, description="Results to skip"),
) -> RawCaptureListResponse:
    """List archived raw quick captures for the current organization."""
    accessible_projects = await _accessible_project_ids_for_read(ctx)
    accessible_delegations = await _accessible_delegation_scope_keys_for_read(ctx)
    captures, has_more = await content_runtime.list_raw_captures(
        session,
        organization_id=org.id,
        entity_type=entity_type,
        capture_surface=capture_surface,
        review_state=review_state,
        limit=limit,
        offset=offset,
    )
    captures = [
        capture
        for capture in captures
        if _raw_capture_visible_to_reader(
            capture,
            ctx=ctx,
            accessible_projects=accessible_projects,
            accessible_delegations=accessible_delegations,
        )
    ]

    return RawCaptureListResponse(
        captures=[_serialize_raw_capture_summary(capture) for capture in captures],
        limit=limit,
        offset=offset,
        has_more=has_more,
    )


@router.get("/captures/{capture_id}", response_model=RawCaptureResponse)
@handle_workflow_errors("get_raw_capture", id_param="capture_id")
async def get_raw_capture(
    capture_id: UUID,
    org: AuthOrganization = Depends(get_current_organization),
    ctx: AuthContext = Depends(get_auth_context),
    session: Any = Depends(get_content_read_session_dependency),
) -> RawCaptureResponse:
    """Get a single archived raw quick capture."""
    accessible_projects = await _accessible_project_ids_for_read(ctx)
    accessible_delegations = await _accessible_delegation_scope_keys_for_read(ctx)
    capture = await content_runtime.get_raw_capture(
        session,
        organization_id=org.id,
        capture_id=capture_id,
    )
    if not capture or not _raw_capture_visible_to_reader(
        capture,
        ctx=ctx,
        accessible_projects=accessible_projects,
        accessible_delegations=accessible_delegations,
    ):
        raise HTTPException(status_code=404, detail=f"Raw capture not found: {capture_id}")

    return _serialize_raw_capture(capture)


@router.patch(
    "/captures/{capture_id}",
    response_model=RawCaptureResponse,
    dependencies=[Depends(require_org_role(*_WRITE_ROLES))],
)
@handle_workflow_errors("update_raw_capture_review_state", id_param="capture_id")
async def update_raw_capture_review_state(
    capture_id: UUID,
    update: RawCaptureReviewUpdate,
    org: AuthOrganization = Depends(get_current_organization),
    ctx: AuthContext = Depends(get_auth_context),
    session: Any = Depends(get_content_read_session_dependency),
) -> RawCaptureResponse:
    """Update review-state metadata for a raw capture."""
    accessible_projects = await _accessible_project_ids_for_read(ctx)
    accessible_delegations = await _accessible_delegation_scope_keys_for_read(ctx)
    existing = await content_runtime.get_raw_capture(
        session,
        organization_id=org.id,
        capture_id=capture_id,
    )
    if not existing or not _raw_capture_visible_to_reader(
        existing,
        ctx=ctx,
        accessible_projects=accessible_projects,
        accessible_delegations=accessible_delegations,
    ):
        raise HTTPException(status_code=404, detail=f"Raw capture not found: {capture_id}")

    capture = await content_runtime.update_raw_capture_review_state(
        session,
        organization_id=org.id,
        capture_id=capture_id,
        review_state=update.review_state,
    )
    if not capture:
        raise HTTPException(status_code=404, detail=f"Raw capture not found: {capture_id}")
    return _serialize_raw_capture(capture)


@router.get("", response_model=EntityListResponse)
@handle_workflow_errors("list_entities")
async def list_entities(
    org: AuthOrganization = Depends(get_current_organization),
    ctx: AuthContext = Depends(get_auth_context),
    entity_type: EntityType | None = Query(default=None, description="Filter by entity type"),
    language: str | None = Query(default=None, description="Filter by programming language"),
    category: str | None = Query(default=None, description="Filter by category"),
    search: str | None = Query(default=None, description="Search in name and description"),
    project_ids: list[str] | None = Query(
        default=None,
        description="Filter by project IDs (use '__unassigned__' for entities without project)",
    ),
    page: int = Query(default=1, ge=1, description="Page number"),
    page_size: int = Query(default=50, ge=1, le=200, description="Items per page"),
    sort_by: SortField = Query(default=SortField.UPDATED_AT, description="Field to sort by"),
    sort_order: SortOrder = Query(default=SortOrder.DESC, description="Sort direction"),
) -> EntityListResponse:
    """List entities with optional filters and pagination."""
    group_id = str(org.id)
    log.debug(
        "Listing entities with filters",
        entity_type=entity_type,
        project_ids=project_ids,
        page=page,
    )

    reader_user_id = str(getattr(getattr(ctx, "user", None), "id", None) or "") or None
    project_ids, real_project_ids, has_unassigned = await _resolve_entity_list_project_filter(
        ctx=ctx,
        project_ids=project_ids,
    )
    # Visibility check only consults accessible_projects for project-scoped
    # entities. real_project_ids is either the user-verified filter set or
    # the user's full accessible set, both of which are the correct frame
    # for "can this user see this project-scoped projection."
    accessible_projects = set(real_project_ids)
    runtime = await get_entity_graph_runtime(group_id)
    entity_manager = runtime.entity_manager

    # Get entities - single query for all types, or filtered by type
    unassigned_marker = "__unassigned__"
    unique_real_project_ids = list(dict.fromkeys(real_project_ids))
    single_project_id = (
        unique_real_project_ids[0]
        if len(unique_real_project_ids) == 1
        and unassigned_marker not in (project_ids or [])
        and entity_type != EntityType.PROJECT
        else None
    )

    if _can_use_bounded_entity_list(
        entity_manager,
        language=language,
        category=category,
        search=search,
        sort_by=sort_by,
        sort_order=sort_order,
    ):
        page_entities, total, has_more = await _list_entities_bounded(
            entity_manager,
            entity_type=entity_type,
            page=page,
            page_size=page_size,
            project_ids=project_ids,
            real_project_ids=real_project_ids,
            has_unassigned=bool(has_unassigned),
            single_project_id=single_project_id,
            reader_user_id=reader_user_id,
            accessible_projects=accessible_projects,
        )
    else:
        if entity_type:
            all_entities = await _list_entities_by_type_paginated(
                entity_manager,
                entity_type,
                project_id=single_project_id,
            )
        else:
            all_entities = await _list_all_entities_paginated(entity_manager)

        filtered = [
            entity
            for entity in all_entities
            if _entity_matches_list_filters(
                entity,
                project_ids=project_ids,
                real_project_ids=real_project_ids,
                has_unassigned=bool(has_unassigned),
                reader_user_id=reader_user_id,
                accessible_projects=accessible_projects,
                language=language,
                category=category,
                search=search,
            )
        ]

        def get_sort_key(e: Any) -> Any:
            if sort_by == SortField.NAME:
                return (getattr(e, "name", "") or "").lower()
            if sort_by == SortField.CREATED_AT:
                return getattr(e, "created_at", None) or datetime.min.replace(tzinfo=UTC)
            if sort_by == SortField.UPDATED_AT:
                return getattr(e, "updated_at", None) or datetime.min.replace(tzinfo=UTC)
            if sort_by == SortField.ENTITY_TYPE:
                return getattr(e, "entity_type", "") or ""
            return ""

        filtered.sort(key=get_sort_key, reverse=(sort_order == SortOrder.DESC))

        total = len(filtered)
        start = (page - 1) * page_size
        end = start + page_size
        page_entities = filtered[start:end]
        has_more = end < total

    # Convert to response models
    response_entities = [
        EntityResponse(
            id=entity.id,
            entity_type=entity.entity_type,
            name=entity.name,
            description=entity.description or "",
            content=LIST_RESPONSE_CONTENT,
            category=getattr(entity, "category", None) or entity.metadata.get("category"),
            languages=getattr(entity, "languages", None)
            or entity.metadata.get("languages", [])
            or [],
            tags=getattr(entity, "tags", None) or entity.metadata.get("tags", []) or [],
            metadata=getattr(entity, "metadata", {}) or {},
            source_file=getattr(entity, "source_file", None),
            created_at=getattr(entity, "created_at", None),
            updated_at=getattr(entity, "updated_at", None),
        )
        for entity in page_entities
    ]

    return EntityListResponse(
        entities=response_entities,
        total=total,
        page=page,
        page_size=page_size,
        has_more=has_more,
    )


@router.get("/{entity_id}", response_model=EntityResponse)
@handle_workflow_errors("get_entity", id_param="entity_id")
async def get_entity(
    entity_id: str,
    org: AuthOrganization = Depends(get_current_organization),
    ctx: AuthContext = Depends(get_auth_context),
    service: KnowledgeReadService = Depends(get_knowledge_read_service),
    include_summary: Annotated[
        bool,
        Query(
            description="Include expensive project/epic summary enrichment",
        ),
    ] = True,
    related_limit: Annotated[
        int,
        Query(
            ge=0,
            le=50,
            description="Maximum related entities to embed in the response",
        ),
    ] = 5,
) -> EntityResponse:
    """Get a single entity by ID with related context.

    Transparently handles both:
    - Graph entities
    - Document chunks from crawler content

    Always includes up to 5 related entities from the knowledge graph.
    """
    if not include_summary:
        entity = await service.get_entity(entity_id)
        if entity is not None:
            accessible_projects = await _require_entity_read_access(ctx, entity)
            metadata = dict(getattr(entity, "metadata", {}) or {})
            related = None
            if related_limit > 0:
                runtime = await get_entity_graph_runtime(str(org.id))
                related = await _fetch_related_entity_summaries(
                    runtime.relationship_manager,
                    entity_id=entity_id,
                    accessible_projects=accessible_projects,
                    limit=related_limit,
                )

            return EntityResponse(
                id=entity.id,
                entity_type=entity.entity_type,
                name=entity.name,
                description=entity.description or "",
                content=(entity.content or "")[:50000],
                category=getattr(entity, "category", None) or entity.metadata.get("category"),
                languages=getattr(entity, "languages", None)
                or entity.metadata.get("languages", [])
                or [],
                tags=getattr(entity, "tags", None) or entity.metadata.get("tags", []) or [],
                metadata=metadata,
                source_file=getattr(entity, "source_file", None),
                created_at=getattr(entity, "created_at", None),
                updated_at=getattr(entity, "updated_at", None),
                related=related,
            )

    if include_summary and related_limit == 0:
        entity = await service.get_entity(entity_id)
        if entity is not None:
            accessible_projects = await _require_entity_read_access(ctx, entity)
            metadata = dict(getattr(entity, "metadata", {}) or {})
            if entity.entity_type in {EntityType.PROJECT, EntityType.EPIC}:
                runtime = await get_entity_graph_runtime(str(org.id))
                metadata, _ = await _enrich_entity_with_related(
                    entity,
                    entity_id,
                    runtime.entity_manager,
                    runtime.relationship_manager,
                    preloaded_related=None,
                    accessible_projects=accessible_projects,
                    related_limit=0,
                )

            return EntityResponse(
                id=entity.id,
                entity_type=entity.entity_type,
                name=entity.name,
                description=entity.description or "",
                content=(entity.content or "")[:50000],
                category=getattr(entity, "category", None) or entity.metadata.get("category"),
                languages=getattr(entity, "languages", None)
                or entity.metadata.get("languages", [])
                or [],
                tags=getattr(entity, "tags", None) or entity.metadata.get("tags", []) or [],
                metadata=metadata,
                source_file=getattr(entity, "source_file", None),
                created_at=getattr(entity, "created_at", None),
                updated_at=getattr(entity, "updated_at", None),
                related=None,
            )

    graph_bundle = await service.get_entity_bundle(entity_id)
    if graph_bundle is not None:
        entity = graph_bundle.entity
        accessible_projects = await _require_entity_read_access(ctx, entity)
        metadata = dict(getattr(entity, "metadata", {}) or {})
        related = _summarize_related_entities(
            entity_id,
            related_entities=graph_bundle.related_entities,
            relationships=graph_bundle.relationships,
            accessible_projects=accessible_projects,
            limit=related_limit,
        )

        if entity.entity_type in {EntityType.PROJECT, EntityType.EPIC}:
            runtime = await get_entity_graph_runtime(str(org.id))

            # Enrich with project and epic summaries via the current manager until
            # those read models move behind the seam.
            metadata, related = await _enrich_entity_with_related(
                entity,
                entity_id,
                runtime.entity_manager,
                runtime.relationship_manager,
                preloaded_related=related,
                accessible_projects=accessible_projects,
                related_limit=related_limit,
            )

        return EntityResponse(
            id=entity.id,
            entity_type=entity.entity_type,
            name=entity.name,
            description=entity.description or "",
            content=(entity.content or "")[:50000],
            category=getattr(entity, "category", None) or entity.metadata.get("category"),
            languages=getattr(entity, "languages", None)
            or entity.metadata.get("languages", [])
            or [],
            tags=getattr(entity, "tags", None) or entity.metadata.get("tags", []) or [],
            metadata=metadata,
            source_file=getattr(entity, "source_file", None),
            created_at=getattr(entity, "created_at", None),
            updated_at=getattr(entity, "updated_at", None),
            related=related,
        )

    if not _should_fallback_to_document_entity(entity_id):
        raise HTTPException(status_code=404, detail=f"Entity not found: {entity_id}")

    log.debug("Entity not in graph, checking document chunks", entity_id=entity_id)

    async with get_content_read_session() as session:
        record = await content_runtime.resolve_document_entity(
            session,
            organization_id=org.id,
            entity_id=entity_id,
        )

        if not record:
            raise HTTPException(status_code=404, detail=f"Entity not found: {entity_id}")

        heading_desc = " > ".join(record.heading_path) if record.heading_path else ""

        return EntityResponse(
            id=str(record.chunk_id),
            entity_type=EntityType.DOCUMENT,
            name=record.document_title or record.source_name,
            description=heading_desc,
            content=record.content[:50000],
            category=record.chunk_type.value if record.chunk_type else None,
            languages=[record.language] if record.language else [],
            tags=[],
            metadata={
                "source_id": str(record.source_id),
                "source_name": record.source_name,
                "source_url": record.source_url,
                "document_id": str(record.document_id),
                "document_url": record.document_url,
                "chunk_index": record.chunk_index,
                "chunk_type": record.chunk_type.value if record.chunk_type else None,
                "heading_path": list(record.heading_path),
                "result_origin": "document",
            },
            source_file=record.document_url,
            created_at=record.created_at,
            updated_at=record.updated_at,
        )


# =============================================================================
# Create
# =============================================================================


@router.post(
    "/bulk",
    response_model=EntityBulkCreateResponse,
    status_code=201,
    dependencies=[Depends(require_org_role(*_WRITE_ROLES))],
)
async def create_entities_bulk(
    batch: EntityBulkCreateRequest,
    org: AuthOrganization = Depends(get_current_organization),
    ctx: AuthContext = Depends(get_auth_context),
    content_session: Any = Depends(get_content_read_session_dependency),
) -> EntityBulkCreateResponse:
    group_id = str(org.id)
    runtime = await get_entity_graph_runtime(group_id)

    for entity in batch.entities:
        if entity.entity_type in BULK_UNSUPPORTED_TYPES:
            raise HTTPException(
                status_code=400,
                detail=f"{entity.entity_type.value} entities are not supported by bulk create",
            )
        if not entity.skip_conflicts:
            raise HTTPException(
                status_code=400,
                detail="bulk create requires skip_conflicts=true; use POST /entities for conflict detection",
            )
        project = entity.metadata.get("project_id") if entity.metadata else None
        if project:
            await verify_entity_project_access(
                content_session,
                ctx,
                str(project),
                required_role=ProjectRole.CONTRIBUTOR,
                require_existing_project=True,
            )
        await _validate_related_to_targets_for_write(
            ctx=ctx,
            entity_manager=runtime.entity_manager,
            related_to=entity.related_to,
        )

    now = datetime.now(UTC)
    entities = [
        _entity_from_bulk_create(entity, group_id=group_id, now=now) for entity in batch.entities
    ]
    created_ids = await runtime.entity_manager.create_direct_bulk(
        entities,
        generate_embeddings=not batch.defer_embeddings,
    )
    persisted_entities = [
        entity.model_copy(update={"id": created_id})
        for entity, created_id in zip(entities, created_ids, strict=True)
    ]

    relationships: list[Relationship] = []
    for created_id, entity in zip(created_ids, batch.entities, strict=True):
        relationships.extend(
            [
                Relationship(
                    id=f"rel_{created_id}_related_to_{related_id}",
                    source_id=created_id,
                    target_id=related_id,
                    relationship_type=RelationshipType.RELATED_TO,
                    metadata={"created_at": now.isoformat()},
                )
                for related_id in entity.related_to or ()
            ]
        )
    if relationships:
        create_direct_bulk = getattr(runtime.relationship_manager, "create_direct_bulk", None)
        if callable(create_direct_bulk):
            await create_direct_bulk(
                relationships,
                generate_embeddings=not batch.defer_embeddings,
            )
        else:
            await runtime.relationship_manager.create_bulk(relationships)

    background_jobs: dict[str, Any] = {}
    if batch.defer_embeddings:
        try:
            from sibyl.jobs.queue import enqueue_entity_embedding_backfill

            embedding_job_id = await enqueue_entity_embedding_backfill(
                [entity.model_dump(mode="json") for entity in persisted_entities],
                group_id,
                relationships=[
                    relationship.model_dump(mode="json") for relationship in relationships
                ],
            )
            log.info(
                "bulk_entity_embedding_backfill_enqueued",
                job_id=embedding_job_id,
                entities=len(entities),
                relationships=len(relationships),
            )
            background_jobs["embedding_backfill"] = {
                "status": "queued",
                "job_ids": [embedding_job_id],
                "queued_entities": len(entities),
                "queued_relationships": len(relationships),
            }
        except Exception as exc:
            log.warning(
                "bulk_entity_embedding_backfill_enqueue_failed",
                entities=len(entities),
                relationships=len(relationships),
                error=str(exc),
            )
    projection_sources: list[Entity] = []
    projection_source_ids: list[str] = []
    for source, created_id in zip(entities, created_ids, strict=True):
        if extract_projected_memory_entities(source):
            projection_sources.append(source)
            projection_source_ids.append(created_id)

    if projection_sources:
        try:
            from sibyl.jobs.queue import enqueue_memory_projection

            projection_job_id = await enqueue_memory_projection(
                [source.model_dump(mode="json") for source in projection_sources],
                group_id,
                created_source_ids=projection_source_ids,
            )
            log.info(
                "bulk_entity_projection_enqueued",
                job_id=projection_job_id,
                sources=len(projection_sources),
            )
            background_jobs["memory_projection"] = {
                "status": "queued",
                "job_ids": [projection_job_id],
                "queued_sources": len(projection_sources),
                "skipped_sources": 0,
            }
        except Exception as exc:
            log.warning(
                "bulk_entity_projection_enqueue_failed",
                sources=len(projection_sources),
                error=str(exc),
            )

    try:
        from sibyl.jobs.memory_extraction import enqueue_memory_extraction_batches

        extraction_enqueue = await enqueue_memory_extraction_batches(
            [source.model_dump(mode="json") for source in entities],
            group_id,
            created_source_ids=list(created_ids),
        )
        if extraction_enqueue.status in {"queued", "partial"}:
            log.info(
                "bulk_entity_memory_extraction_enqueued",
                status=extraction_enqueue.status,
                jobs=len(extraction_enqueue.job_ids),
                queued_sources=extraction_enqueue.queued_sources,
                skipped_sources=extraction_enqueue.skipped_sources,
                reason=extraction_enqueue.reason,
            )
            background_jobs["memory_extraction"] = {
                "status": extraction_enqueue.status,
                "job_ids": list(extraction_enqueue.job_ids),
                "queued_sources": extraction_enqueue.queued_sources,
                "skipped_sources": extraction_enqueue.skipped_sources,
                "queue_depth": extraction_enqueue.queue_depth,
                "reason": extraction_enqueue.reason,
            }
        elif extraction_enqueue.reason != "disabled":
            log.info(
                "bulk_entity_memory_extraction_skipped",
                status=extraction_enqueue.status,
                reason=extraction_enqueue.reason,
                skipped_sources=extraction_enqueue.skipped_sources,
            )
            background_jobs["memory_extraction"] = {
                "status": extraction_enqueue.status,
                "job_ids": list(extraction_enqueue.job_ids),
                "queued_sources": extraction_enqueue.queued_sources,
                "skipped_sources": extraction_enqueue.skipped_sources,
                "queue_depth": extraction_enqueue.queue_depth,
                "reason": extraction_enqueue.reason,
            }
    except Exception as exc:
        log.warning(
            "bulk_entity_memory_extraction_enqueue_failed",
            sources=len(entities),
            error=str(exc),
        )

    responses = [
        _entity_response_from_bulk_create(
            entity,
            entity_id=entity_id,
            group_id=group_id,
            now=now,
        )
        for entity_id, entity in zip(created_ids, batch.entities, strict=True)
    ]
    return EntityBulkCreateResponse(
        entities=responses,
        created=len(responses),
        failed=0,
        background_jobs=background_jobs,
    )


@router.post(
    "",
    response_model=EntityResponse,
    status_code=201,
    dependencies=[Depends(require_org_role(*_WRITE_ROLES))],
)
@handle_workflow_errors("create_entity")
async def create_entity(
    request: Request,
    entity: EntityCreate,
    org: AuthOrganization = Depends(get_current_organization),
    ctx: AuthContext = Depends(get_auth_context),
    content_session: Any = Depends(get_content_read_session_dependency),
    sync: bool = Query(
        default=False,
        description="Wait for entity creation to complete (slower but entity is immediately available)",
    ),
) -> EntityResponse:
    """Create a new entity.

    By default, entities are created asynchronously via a background worker.
    Set sync=true to wait for creation to complete (useful for tasks that need
    immediate workflow operations like start/complete).
    """
    from sibyl_core.tools.core import add

    group_id = str(org.id)

    # Extract task-specific fields from metadata if present
    project = entity.metadata.get("project_id") if entity.metadata else None
    epic = entity.metadata.get("epic_id") if entity.metadata else None
    priority = entity.metadata.get("priority") if entity.metadata else None
    assignees = entity.metadata.get("assignees") if entity.metadata else None
    technologies = entity.metadata.get("technologies") if entity.metadata else None
    depends_on = entity.metadata.get("depends_on") if entity.metadata else None
    if project:
        await verify_entity_project_access(
            content_session,
            ctx,
            project,
            required_role=ProjectRole.CONTRIBUTOR,
            require_existing_project=True,
        )
    runtime = await get_entity_graph_runtime(group_id)
    await _validate_related_to_targets_for_write(
        ctx=ctx,
        entity_manager=runtime.entity_manager,
        related_to=entity.related_to,
    )

    # Use description as content fallback (frontend sends description, add() needs content)
    content = entity.content or entity.description or entity.name
    request_metadata: dict[str, object] = dict(entity.metadata or {})
    idempotency_payload = {
        "body": entity.model_dump(mode="json"),
        "query": {"sync": sync},
    }

    if ctx.user is not None:
        replayed = await replay_idempotent_response(
            request,
            organization_id=org.id,
            principal_id=str(ctx.user.id),
            method="POST",
            path="/entities",
            payload=idempotency_payload,
            response_model=EntityResponse,
            content_session=content_session,
        )
        if replayed is not None:
            return replayed

    merged_metadata: dict[str, Any] = {**request_metadata, "organization_id": group_id}

    # Projects are always sync (foundational - tasks depend on them existing)
    # Other entities can be async unless caller explicitly requests sync
    is_sync = entity.entity_type.value == "project" or sync

    result = await add(
        title=entity.name,
        content=content,
        entity_type=entity.entity_type.value,
        category=entity.category,
        languages=entity.languages,
        tags=entity.tags,
        related_to=entity.related_to,
        metadata=merged_metadata,
        # Task-specific fields
        project=project,
        epic=epic,
        priority=priority,
        assignees=assignees,
        technologies=technologies,
        depends_on=depends_on,
        # Sync for projects, async for everything else
        sync=is_sync,
        skip_conflicts=entity.skip_conflicts,
    )

    if not result.success or not result.id:
        message = sanitize_error_text(str(result.message or "Entity creation failed"))
        if "duplicate" in message.lower() or "already exists" in message.lower():
            raise constraint_violation(
                "duplicate entity name in scope",
                remediation="Use a different title or update the existing entity.",
                details={
                    "field": "name",
                    "entity_type": entity.entity_type.value,
                },
            )
        raise HTTPException(status_code=400, detail=message)

    if request_metadata.get("capture_mode") in {"quick", "remember"}:
        raw_capture_metadata = _sanitize_raw_capture_metadata(request_metadata)
        await _archive_raw_capture(
            content_session,
            organization_id=org.id,
            user_id=ctx.user.id if ctx.user else None,
            entity_id=result.id,
            entity_name=entity.name,
            entity_content=content,
            entity_type=entity.entity_type.value,
            tags=list(entity.tags or []),
            metadata=raw_capture_metadata,
        )

    # For async creation, return immediately with pending response.
    # Entity creation continues in the native background job path.
    if not is_sync:
        response = EntityResponse(
            id=result.id,
            entity_type=entity.entity_type,
            name=entity.name,
            description=entity.description or "",
            content=content,
            category=entity.category,
            languages=entity.languages or [],
            tags=entity.tags or [],
            metadata=merged_metadata,
            source_file=None,
            created_at=None,
            updated_at=None,
        )
        # Broadcast pending creation event
        await broadcast_event(
            WSEvent.ENTITY_PENDING, response.model_dump(mode="json"), org_id=str(org.id)
        )
        if ctx.user is not None:
            await save_idempotent_response(
                request,
                organization_id=org.id,
                principal_id=str(ctx.user.id),
                method="POST",
                path="/entities",
                payload=idempotency_payload,
                response=response,
                status_code=201,
                content_session=content_session,
            )
        return response

    response_timestamp = getattr(result, "timestamp", None) or datetime.now(UTC)
    response = EntityResponse(
        id=result.id,
        entity_type=entity.entity_type,
        name=entity.name,
        description=entity.description or "",
        content=content,
        category=entity.category,
        languages=entity.languages or [],
        tags=entity.tags or [],
        metadata=merged_metadata,
        source_file=None,
        created_at=response_timestamp,
        updated_at=response_timestamp,
    )

    # Broadcast creation event (scoped to org)
    await broadcast_event(
        WSEvent.ENTITY_CREATED, response.model_dump(mode="json"), org_id=str(org.id)
    )

    if entity.entity_type == EntityType.PROJECT:
        await create_project_record(
            organization_id=org.id,
            owner_user_id=ctx.user.id,
            graph_project_id=result.id,
            name=entity.name,
            description=content,
        )
        await log_audit_event(
            action="project.create",
            user_id=ctx.user.id,
            organization_id=org.id,
            request=request,
            details={"project_id": result.id, "name": entity.name},
        )

    if ctx.user is not None:
        await save_idempotent_response(
            request,
            organization_id=org.id,
            principal_id=str(ctx.user.id),
            method="POST",
            path="/entities",
            payload=idempotency_payload,
            response=response,
            status_code=201,
            content_session=content_session,
        )

    return response


# =============================================================================
# Update
# =============================================================================


@router.patch(
    "/{entity_id}",
    response_model=EntityResponse,
    dependencies=[Depends(require_org_role(*_WRITE_ROLES))],
)
async def update_entity(
    entity_id: str,
    update: EntityUpdate,
    request: Request,
    org: AuthOrganization = Depends(get_current_organization),
    ctx: AuthContext = Depends(get_auth_context),
    content_session: Any = Depends(get_content_read_session_dependency),
) -> EntityResponse:
    """Update an existing entity."""
    from sibyl.locks import LockAcquisitionError, entity_lock

    group_id = str(org.id)

    try:
        # Acquire distributed lock to prevent concurrent updates
        async with entity_lock(group_id, entity_id, blocking=True) as lock_token:
            if not lock_token:
                raise HTTPException(
                    status_code=409,
                    detail="Entity is being updated by another process. Please retry.",
                )

            runtime = await get_entity_graph_runtime(group_id)

            # Get existing entity
            existing = await runtime.entity_manager.get(entity_id)
            if not existing:
                raise HTTPException(status_code=404, detail=f"Entity not found: {entity_id}")

            # Verify project access for entities with project_id
            project_id = _entity_read_project_id(existing)
            await verify_entity_project_access(
                content_session,
                ctx,
                project_id,
                required_role=ProjectRole.CONTRIBUTOR,
                require_existing_project=True,
            )

            # Build update dict with only provided fields
            update_data: dict[str, Any] = {}
            if update.name is not None:
                update_data["name"] = update.name
            if update.description is not None:
                update_data["description"] = update.description
            if update.content is not None:
                update_data["content"] = update.content
            if update.category is not None:
                update_data["category"] = update.category
            if update.languages is not None:
                update_data["languages"] = update.languages
            if update.tags is not None:
                update_data["tags"] = update.tags
            if update.metadata is not None:
                # Merge metadata
                existing_meta = getattr(existing, "metadata", {}) or {}
                update_data["metadata"] = {**existing_meta, **update.metadata}

            # Update timestamp
            update_data["updated_at"] = datetime.now(UTC)

            # Perform update
            updated = await runtime.entity_manager.update(entity_id, update_data)
            if not updated:
                raise HTTPException(status_code=500, detail="Update failed")

            response = EntityResponse(
                id=updated.id,
                entity_type=updated.entity_type,
                name=updated.name,
                description=updated.description or "",
                content=updated.content or "",
                category=getattr(updated, "category", None) or updated.metadata.get("category"),
                languages=getattr(updated, "languages", None)
                or updated.metadata.get("languages", [])
                or [],
                tags=getattr(updated, "tags", None) or updated.metadata.get("tags", []) or [],
                metadata=getattr(updated, "metadata", {}) or {},
                source_file=getattr(updated, "source_file", None),
                created_at=getattr(updated, "created_at", None),
                updated_at=getattr(updated, "updated_at", None),
            )

            # Broadcast update event (scoped to org)
            await broadcast_event(
                WSEvent.ENTITY_UPDATED, response.model_dump(mode="json"), org_id=str(org.id)
            )

            if existing.entity_type == EntityType.PROJECT:
                await update_project_record(
                    organization_id=org.id,
                    graph_project_id=existing.id,
                    name=response.name,
                    description=response.description,
                )
                await log_audit_event(
                    action="project.update",
                    user_id=ctx.user.id,
                    organization_id=org.id,
                    request=request,
                    details={"project_id": existing.id, "name": response.name},
                )

            return response

    except LockAcquisitionError as e:
        raise HTTPException(
            status_code=409,
            detail="Entity is locked by another process. Please retry.",
        ) from e
    except HTTPException:
        raise
    except Exception as e:
        log.exception("update_entity_failed", entity_id=entity_id, error=str(e))
        raise HTTPException(
            status_code=500, detail="Failed to update entity. Please try again."
        ) from e


# =============================================================================
# Delete
# =============================================================================


@router.delete(
    "/{entity_id}",
    status_code=204,
    dependencies=[Depends(require_org_role(*_WRITE_ROLES))],
)
async def delete_entity(
    entity_id: str,
    request: Request,
    org: AuthOrganization = Depends(get_current_organization),
    ctx: AuthContext = Depends(get_auth_context),
    content_session: Any = Depends(get_content_read_session_dependency),
) -> None:
    """Delete an entity."""
    from sibyl.locks import LockAcquisitionError, entity_lock

    group_id = str(org.id)

    try:
        # Acquire distributed lock to prevent concurrent modifications
        async with entity_lock(group_id, entity_id, blocking=True) as lock_token:
            if not lock_token:
                raise HTTPException(
                    status_code=409,
                    detail="Entity is being modified by another process. Please retry.",
                )

            runtime = await get_entity_graph_runtime(group_id)

            # Check existence
            existing = await runtime.entity_manager.get(entity_id)
            if not existing:
                raise HTTPException(status_code=404, detail=f"Entity not found: {entity_id}")

            # Verify project access for entities with project_id (maintainer required to delete)
            project_id = _entity_read_project_id(existing)
            await verify_entity_project_access(
                content_session,
                ctx,
                project_id,
                required_role=ProjectRole.MAINTAINER,
                require_existing_project=True,
            )

            if existing.entity_type == EntityType.PROJECT:
                await log_audit_event(
                    action="project.delete",
                    user_id=ctx.user.id,
                    organization_id=org.id,
                    request=request,
                    details={"project_id": existing.id, "name": existing.name},
                )

            # Delete from graph
            success = await runtime.entity_manager.delete(entity_id)
            if not success:
                raise HTTPException(status_code=500, detail="Delete failed")

            if existing.entity_type == EntityType.PROJECT:
                await delete_project_record(
                    organization_id=org.id,
                    graph_project_id=existing.id,
                )

            # Broadcast deletion event (scoped to org)
            await broadcast_event(
                WSEvent.ENTITY_DELETED,
                {"id": entity_id, "type": existing.entity_type.value, "name": existing.name},
                org_id=str(org.id),
            )

    except LockAcquisitionError as e:
        raise HTTPException(
            status_code=409,
            detail="Entity is locked by another process. Please retry.",
        ) from e
    except HTTPException:
        raise
    except Exception as e:
        log.exception("delete_entity_failed", entity_id=entity_id, error=str(e))
        raise HTTPException(
            status_code=500, detail="Failed to delete entity. Please try again."
        ) from e
