"""Canonical reads ownership for entity routes."""

from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query

from sibyl.api.decorators import handle_workflow_errors
from sibyl.api.dependencies import get_knowledge_read_service
from sibyl.api.routes import entity_contracts as contracts, entity_policy as policy
from sibyl.api.schemas import (
    EntityListResponse,
    EntityResponse,
    RelatedEntitySummary,
)
from sibyl.auth.context import AuthContext
from sibyl.auth.dependencies import (
    get_auth_context,
    get_current_organization,
    require_org_role,
)
from sibyl.persistence import content_runtime
from sibyl.persistence.content_runtime import (
    get_content_read_session,
)
from sibyl_core.auth import AuthOrganization
from sibyl_core.models.entities import EntityType
from sibyl_core.services import KnowledgeReadService

log = structlog.get_logger()

router = APIRouter(
    prefix="/entities",
    tags=["entities"],
    dependencies=[Depends(require_org_role(*contracts.READ_ROLES))],
)

LIST_ALL_PAGE_SIZE = 2000
LIST_BY_TYPE_PAGE_SIZE = 1000
GRAPH_ENTITY_ID_PREFIXES = frozenset(
    {entity_type.value for entity_type in EntityType if entity_type is not EntityType.DOCUMENT}
)
LIST_RESPONSE_CONTENT = ""


def should_fallback_to_document_entity(entity_id: str) -> bool:
    if any(entity_id.startswith(f"{prefix}_") for prefix in GRAPH_ENTITY_ID_PREFIXES):
        return False
    try:
        UUID(entity_id)
        return True
    except ValueError:
        normalized = entity_id.lower().replace("-", "")
        return len(normalized) >= 4 and all(char in "0123456789abcdef" for char in normalized)


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
        **policy.lightweight_entity_list_kwargs(entity_manager),
    }
    batch = await entity_manager.list_all(**list_kwargs)
    return [entity for entity in batch if not policy.entity_is_archived(entity)]


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
            **policy.lightweight_entity_list_kwargs(entity_manager),
        }
        if project_id:
            list_kwargs["project_id"] = project_id

        batch = await entity_manager.list_by_type(entity_type, **list_kwargs)
        if not batch:
            break

        entities.extend(entity for entity in batch if not policy.entity_is_archived(entity))
        offset += batch_size

    return entities


def _can_use_bounded_entity_list(
    entity_manager: Any,
    *,
    language: str | None,
    category: str | None,
    search: str | None,
    sort_by: contracts.SortField,
    sort_order: contracts.SortOrder,
) -> bool:
    surreal_ops = getattr(entity_manager, "_surreal_entity_node_ops", None)
    bounded = getattr(entity_manager, "supports_bounded_entity_list", False) is True
    compatibility_bounded = callable(surreal_ops) and surreal_ops() is not None
    return (
        (bounded or compatibility_bounded)
        and not language
        and not category
        and not search
        and sort_by == contracts.SortField.UPDATED_AT
        and sort_order == contracts.SortOrder.DESC
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
    allowed_memory_scope_keys: set[str] | None,
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
                **policy.lightweight_entity_list_kwargs(entity_manager),
            }
            if single_project_id:
                list_kwargs["project_id"] = single_project_id
            batch = await entity_manager.list_by_type(entity_type, **list_kwargs)
        else:
            batch = await entity_manager.list_all(
                limit=batch_size,
                offset=offset,
                include_archived=True,
                **policy.lightweight_entity_list_kwargs(entity_manager),
            )
        if not batch:
            exhausted = True
            break

        for entity in batch:
            if policy.entity_matches_list_filters(
                entity,
                project_ids=project_ids,
                real_project_ids=real_project_ids,
                has_unassigned=has_unassigned,
                reader_user_id=reader_user_id,
                allowed_memory_scope_keys=allowed_memory_scope_keys,
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
    reader_user_id: str | None,
    allowed_memory_scope_keys: set[str] | None = None,
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
            reader_user_id=reader_user_id,
            allowed_memory_scope_keys=allowed_memory_scope_keys,
            limit=related_limit,
        )

    return metadata, related


def summarize_related_entities(
    entity_id: str,
    *,
    related_entities: list[Any],
    relationships: list[Any],
    accessible_projects: set[str],
    reader_user_id: str | None,
    allowed_memory_scope_keys: set[str] | None = None,
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
        if not policy.related_entity_visible(
            related_entity,
            reader_user_id=reader_user_id,
            allowed_memory_scope_keys=allowed_memory_scope_keys,
            accessible_projects=accessible_projects,
        ):
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
    reader_user_id: str | None,
    allowed_memory_scope_keys: set[str] | None = None,
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
            if not policy.related_entity_visible(
                rel_entity,
                reader_user_id=reader_user_id,
                allowed_memory_scope_keys=allowed_memory_scope_keys,
                accessible_projects=accessible_projects,
            ):
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
    sort_by: contracts.SortField = Query(
        default=contracts.SortField.UPDATED_AT, description="Field to sort by"
    ),
    sort_order: contracts.SortOrder = Query(
        default=contracts.SortOrder.DESC, description="Sort direction"
    ),
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
    allowed_memory_scope_keys = policy.reader_memory_grants(ctx)
    project_ids, real_project_ids, has_unassigned = await policy.resolve_entity_list_project_filter(
        ctx=ctx,
        project_ids=project_ids,
    )
    # Visibility check only consults accessible_projects for project-scoped
    # entities. real_project_ids is either the user-verified filter set or
    # the user's full accessible set, both of which are the correct frame
    # for "can this user see this project-scoped projection."
    accessible_projects = set(real_project_ids)
    runtime = await policy.get_entity_graph_runtime(group_id)
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
            allowed_memory_scope_keys=allowed_memory_scope_keys,
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
            if policy.entity_matches_list_filters(
                entity,
                project_ids=project_ids,
                real_project_ids=real_project_ids,
                has_unassigned=bool(has_unassigned),
                reader_user_id=reader_user_id,
                allowed_memory_scope_keys=allowed_memory_scope_keys,
                accessible_projects=accessible_projects,
                language=language,
                category=category,
                search=search,
            )
        ]

        def get_sort_key(e: Any) -> Any:
            if sort_by == contracts.SortField.NAME:
                return (getattr(e, "name", "") or "").lower()
            if sort_by == contracts.SortField.CREATED_AT:
                return getattr(e, "created_at", None) or datetime.min.replace(tzinfo=UTC)
            if sort_by == contracts.SortField.UPDATED_AT:
                return getattr(e, "updated_at", None) or datetime.min.replace(tzinfo=UTC)
            if sort_by == contracts.SortField.ENTITY_TYPE:
                return getattr(e, "entity_type", "") or ""
            return ""

        filtered.sort(key=get_sort_key, reverse=(sort_order == contracts.SortOrder.DESC))

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
            accessible_projects = await policy.require_entity_read_access(ctx, entity)
            metadata = dict(getattr(entity, "metadata", {}) or {})
            related = None
            if related_limit > 0:
                runtime = await policy.get_entity_graph_runtime(str(org.id))
                related = await _fetch_related_entity_summaries(
                    runtime.relationship_manager,
                    entity_id=entity_id,
                    accessible_projects=accessible_projects,
                    reader_user_id=policy.reader_user_id(ctx),
                    allowed_memory_scope_keys=policy.reader_memory_grants(ctx),
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
            accessible_projects = await policy.require_entity_read_access(ctx, entity)
            metadata = dict(getattr(entity, "metadata", {}) or {})
            if entity.entity_type in {EntityType.PROJECT, EntityType.EPIC}:
                runtime = await policy.get_entity_graph_runtime(str(org.id))
                metadata, _ = await _enrich_entity_with_related(
                    entity,
                    entity_id,
                    runtime.entity_manager,
                    runtime.relationship_manager,
                    preloaded_related=None,
                    accessible_projects=accessible_projects,
                    reader_user_id=policy.reader_user_id(ctx),
                    allowed_memory_scope_keys=policy.reader_memory_grants(ctx),
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
        accessible_projects = await policy.require_entity_read_access(ctx, entity)
        metadata = dict(getattr(entity, "metadata", {}) or {})
        related = summarize_related_entities(
            entity_id,
            related_entities=graph_bundle.related_entities,
            relationships=graph_bundle.relationships,
            accessible_projects=accessible_projects,
            reader_user_id=policy.reader_user_id(ctx),
            allowed_memory_scope_keys=policy.reader_memory_grants(ctx),
            limit=related_limit,
        )

        if entity.entity_type in {EntityType.PROJECT, EntityType.EPIC}:
            runtime = await policy.get_entity_graph_runtime(str(org.id))

            # Enrich with project and epic summaries via the current manager until
            # those read models move behind the seam.
            metadata, related = await _enrich_entity_with_related(
                entity,
                entity_id,
                runtime.entity_manager,
                runtime.relationship_manager,
                preloaded_related=related,
                accessible_projects=accessible_projects,
                reader_user_id=policy.reader_user_id(ctx),
                allowed_memory_scope_keys=policy.reader_memory_grants(ctx),
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

    if not should_fallback_to_document_entity(entity_id):
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
