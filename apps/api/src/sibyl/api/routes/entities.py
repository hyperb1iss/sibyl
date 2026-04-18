"""Entity CRUD endpoints.

Full create, read, update, delete operations for all entity types.
Transparently handles both graph entities (FalkorDB) and document chunks (Postgres).
"""

from datetime import UTC, datetime
from enum import Enum
from typing import Any
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlmodel import col, select

from sibyl.api.dependencies import get_legacy_knowledge_read_service
from sibyl.api.event_types import WSEvent
from sibyl.api.schemas import (
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
from sibyl.auth.audit import AuditLogger
from sibyl.auth.authorization import ProjectRole, verify_entity_project_access
from sibyl.auth.context import AuthContext
from sibyl.auth.dependencies import (
    get_auth_context,
    get_current_organization,
    require_org_role,
)
from sibyl.db import CrawledDocument, CrawlSource, DocumentChunk, get_session
from sibyl.db.connection import get_session_dependency
from sibyl.db.models import Organization, OrganizationRole, RawCapture
from sibyl.db.project_sync import sync_project_create, sync_project_delete, sync_project_update
from sibyl.persistence.legacy.graph import get_legacy_entity_runtime
from sibyl_core.models.entities import EntityType
from sibyl_core.services import KnowledgeReadService

log = structlog.get_logger()


class SortField(str, Enum):
    """Fields available for sorting entities."""

    NAME = "name"
    CREATED_AT = "created_at"
    UPDATED_AT = "updated_at"
    ENTITY_TYPE = "entity_type"


class SortOrder(str, Enum):
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


# =============================================================================
# Helpers
# =============================================================================


def _entity_is_archived(entity: Any) -> bool:
    metadata = getattr(entity, "metadata", None) or {}
    return bool(metadata.get("archived")) or str(metadata.get("status", "")).lower() == "archived"


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
    metadata: dict[str, Any],
) -> None:
    """Persist the write-once capture sidecar."""
    session.add(
        RawCapture(
            organization_id=organization_id,
            entity_id=entity_id,
            title=entity_name,
            raw_content=entity_content,
            entity_type=entity_type,
            tags=tags,
            metadata_=metadata,
            capture_surface=str(metadata.get("capture_surface"))
            if metadata.get("capture_surface")
            else None,
            created_by_user_id=user_id,
        )
    )
    await session.commit()


def _raw_capture_review_state(capture: RawCapture) -> str:
    return str((capture.metadata_ or {}).get("review_state") or "pending")


def _serialize_raw_capture_summary(capture: RawCapture) -> RawCaptureSummary:
    return RawCaptureSummary(
        id=str(capture.id),
        entity_id=capture.entity_id,
        title=capture.title,
        entity_type=capture.entity_type,
        tags=list(capture.tags or []),
        metadata=dict(capture.metadata_ or {}),
        capture_surface=capture.capture_surface,
        review_state=_raw_capture_review_state(capture),
        created_by_user_id=str(capture.created_by_user_id) if capture.created_by_user_id else None,
        created_at=capture.created_at,
    )


def _serialize_raw_capture(capture: RawCapture) -> RawCaptureResponse:
    return RawCaptureResponse(
        **_serialize_raw_capture_summary(capture).model_dump(),
        raw_content=capture.raw_content,
    )


async def _list_all_entities_paginated(
    entity_manager: Any,
    *,
    batch_size: int | None = None,
) -> list[Any]:
    batch_size = LIST_ALL_PAGE_SIZE if batch_size is None else batch_size
    entities: list[Any] = []
    offset = 0

    while True:
        batch = await entity_manager.list_all(
            limit=batch_size,
            offset=offset,
            include_archived=True,
        )
        if not batch:
            break

        entities.extend(entity for entity in batch if not _entity_is_archived(entity))
        offset += batch_size

    return entities


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
        }
        if project_id:
            list_kwargs["project_id"] = project_id

        batch = await entity_manager.list_by_type(entity_type, **list_kwargs)
        if not batch:
            break

        entities.extend(entity for entity in batch if not _entity_is_archived(entity))
        offset += batch_size

    return entities


async def _enrich_entity_with_related(
    entity: Any,
    entity_id: str,
    entity_manager: Any,
    relationship_manager: Any,
    preloaded_related: list[RelatedEntitySummary] | None = None,
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
            }
            # Return actionable tasks as "related"
            actionable = summary.get("actionable_tasks", [])
            if actionable:
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
    if related is None:
        try:
            related_pairs = await relationship_manager.get_related_entities(entity_id, limit=5)
            if related_pairs:
                # Dedupe by entity ID
                seen_ids: set[str] = set()
                deduped: list[RelatedEntitySummary] = []
                for rel_entity, rel in related_pairs:
                    if rel_entity.id not in seen_ids:
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
                related = deduped if deduped else None
        except Exception as rel_err:
            log.debug("Failed to fetch related entities", error=str(rel_err))

    return metadata, related


def _summarize_related_entities(
    entity_id: str,
    *,
    related_entities: list[Any],
    relationships: list[Any],
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
    for related_entity in related_entities:
        relationship_pair = relationships_by_other_id.get(related_entity.id)
        if relationship_pair is None:
            continue
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

    return summaries or None


# =============================================================================
# List / Read
# =============================================================================


@router.get("/captures", response_model=RawCaptureListResponse)
async def list_raw_captures(
    org: Organization = Depends(get_current_organization),
    session: Any = Depends(get_session_dependency),
    entity_type: str | None = Query(default=None, description="Filter by entity type"),
    capture_surface: str | None = Query(default=None, description="Filter by capture surface"),
    review_state: str | None = Query(default=None, description="Filter by review queue state"),
    limit: int = Query(default=50, ge=1, le=200, description="Items per page"),
    offset: int = Query(default=0, ge=0, description="Results to skip"),
) -> RawCaptureListResponse:
    """List archived raw quick captures for the current organization."""
    try:
        stmt = (
            select(RawCapture)
            .where(col(RawCapture.organization_id) == org.id)
            .order_by(col(RawCapture.created_at).desc())
            .offset(offset)
            .limit(limit + 1)
        )
        if entity_type:
            stmt = stmt.where(col(RawCapture.entity_type) == entity_type)
        if capture_surface:
            stmt = stmt.where(col(RawCapture.capture_surface) == capture_surface)
        if review_state:
            if review_state == "pending":
                stmt = stmt.where(
                    col(RawCapture.metadata_).op("->>")("review_state").is_(None)
                    | (col(RawCapture.metadata_).op("->>")("review_state") == "pending")
                )
            else:
                stmt = stmt.where(
                    col(RawCapture.metadata_).op("->>")("review_state") == review_state
                )

        result = await session.execute(stmt)
        rows = result.scalars().all()
        has_more = len(rows) > limit
        captures = rows[:limit]

        return RawCaptureListResponse(
            captures=[_serialize_raw_capture_summary(capture) for capture in captures],
            limit=limit,
            offset=offset,
            has_more=has_more,
        )
    except Exception as e:
        log.exception("list_raw_captures_failed", error=str(e))
        raise HTTPException(
            status_code=500, detail="Failed to list raw captures. Please try again."
        ) from e


@router.get("/captures/{capture_id}", response_model=RawCaptureResponse)
async def get_raw_capture(
    capture_id: UUID,
    org: Organization = Depends(get_current_organization),
    session: Any = Depends(get_session_dependency),
) -> RawCaptureResponse:
    """Get a single archived raw quick capture."""
    try:
        result = await session.execute(
            select(RawCapture).where(
                col(RawCapture.id) == capture_id,
                col(RawCapture.organization_id) == org.id,
            )
        )
        capture = result.scalar_one_or_none()
        if not capture:
            raise HTTPException(status_code=404, detail=f"Raw capture not found: {capture_id}")

        return _serialize_raw_capture(capture)
    except HTTPException:
        raise
    except Exception as e:
        log.exception("get_raw_capture_failed", capture_id=str(capture_id), error=str(e))
        raise HTTPException(
            status_code=500, detail="Failed to get raw capture. Please try again."
        ) from e


@router.patch(
    "/captures/{capture_id}",
    response_model=RawCaptureResponse,
    dependencies=[Depends(require_org_role(*_WRITE_ROLES))],
)
async def update_raw_capture_review_state(
    capture_id: UUID,
    update: RawCaptureReviewUpdate,
    org: Organization = Depends(get_current_organization),
    session: Any = Depends(get_session_dependency),
) -> RawCaptureResponse:
    """Update review-state metadata for a raw capture."""
    try:
        result = await session.execute(
            select(RawCapture).where(
                col(RawCapture.id) == capture_id,
                col(RawCapture.organization_id) == org.id,
            )
        )
        capture = result.scalar_one_or_none()
        if not capture:
            raise HTTPException(status_code=404, detail=f"Raw capture not found: {capture_id}")

        metadata = dict(capture.metadata_ or {})
        metadata["review_state"] = update.review_state
        metadata["reviewed_at"] = datetime.now(UTC).isoformat()
        if update.review_state == "pending":
            metadata.pop("deferred_at", None)
            metadata.pop("archived_at", None)
        elif update.review_state == "deferred":
            metadata["deferred_at"] = metadata["reviewed_at"]
            metadata.pop("archived_at", None)
        else:
            metadata["archived_at"] = metadata["reviewed_at"]
            metadata.pop("deferred_at", None)

        capture.metadata_ = metadata
        session.add(capture)
        await session.commit()
        await session.refresh(capture)
        return _serialize_raw_capture(capture)
    except HTTPException:
        raise
    except Exception as e:
        log.exception(
            "update_raw_capture_review_state_failed", capture_id=str(capture_id), error=str(e)
        )
        raise HTTPException(
            status_code=500, detail="Failed to update raw capture review state. Please try again."
        ) from e


@router.get("", response_model=EntityListResponse)
async def list_entities(
    org: Organization = Depends(get_current_organization),
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
    try:
        group_id = str(org.id)
        log.debug(
            "Listing entities with filters",
            entity_type=entity_type,
            project_ids=project_ids,
            page=page,
        )
        runtime = await get_legacy_entity_runtime(group_id)
        entity_manager = runtime.entity_manager

        # Get entities - single query for all types, or filtered by type
        unassigned_marker = "__unassigned__"
        real_project_ids = [pid for pid in (project_ids or []) if pid != unassigned_marker]
        unique_real_project_ids = list(dict.fromkeys(real_project_ids))
        single_project_id = (
            unique_real_project_ids[0]
            if len(unique_real_project_ids) == 1 and unassigned_marker not in (project_ids or [])
            else None
        )

        if entity_type:
            all_entities = await _list_entities_by_type_paginated(
                entity_manager,
                entity_type,
                project_id=single_project_id,
            )
        else:
            all_entities = await _list_all_entities_paginated(entity_manager)

        # Apply filters
        filtered = []

        # Prepare project filter
        has_unassigned = project_ids and unassigned_marker in project_ids

        for entity in all_entities:
            # Project filter
            if project_ids:
                entity_project = getattr(entity, "project_id", None) or (
                    entity.metadata.get("project_id") if entity.metadata else None
                )
                # Check if entity matches filter criteria
                if entity_project:
                    # Entity has a project - check if it's in the filter list
                    if real_project_ids and entity_project not in real_project_ids:
                        continue
                    # If only unassigned is selected, skip entities with projects
                    if has_unassigned and not real_project_ids:
                        continue
                elif not has_unassigned:
                    # Entity has no project - only include if unassigned is selected
                    continue

            # Language filter
            if language:
                entity_langs = getattr(entity, "languages", []) or []
                if language.lower() not in [lang.lower() for lang in entity_langs]:
                    continue

            # Category filter
            if category:
                entity_cat = getattr(entity, "category", "") or ""
                if category.lower() not in entity_cat.lower():
                    continue

            # Search filter (name and description)
            if search:
                search_lower = search.lower()
                name = (getattr(entity, "name", "") or "").lower()
                description = (getattr(entity, "description", "") or "").lower()
                if search_lower not in name and search_lower not in description:
                    continue

            filtered.append(entity)

        # Sort entities
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

        # Paginate
        total = len(filtered)
        start = (page - 1) * page_size
        end = start + page_size
        page_entities = filtered[start:end]

        # Convert to response models
        response_entities = [
            EntityResponse(
                id=entity.id,
                entity_type=entity.entity_type,
                name=entity.name,
                description=entity.description or "",
                content=(entity.content or "")[:50000],  # Truncate for list view
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
            has_more=end < total,
        )

    except Exception as e:
        log.exception("list_entities_failed", error=str(e))
        raise HTTPException(
            status_code=500, detail="Failed to list entities. Please try again."
        ) from e


@router.get("/{entity_id}", response_model=EntityResponse)
async def get_entity(
    entity_id: str,
    org: Organization = Depends(get_current_organization),
    service: KnowledgeReadService = Depends(get_legacy_knowledge_read_service),
) -> EntityResponse:
    """Get a single entity by ID with related context.

    Transparently handles both:
    - Graph entities (stored in FalkorDB)
    - Document chunks (stored in Postgres via crawler)

    Always includes up to 5 related entities from the knowledge graph.
    """
    try:
        graph_bundle = await service.get_entity_bundle(entity_id)
        if graph_bundle is not None:
            entity = graph_bundle.entity
            metadata = dict(getattr(entity, "metadata", {}) or {})
            related = _summarize_related_entities(
                entity_id,
                related_entities=graph_bundle.related_entities,
                relationships=graph_bundle.relationships,
            )

            if entity.entity_type in {EntityType.PROJECT, EntityType.EPIC}:
                runtime = await get_legacy_entity_runtime(str(org.id))

                # Enrich with project and epic summaries via the current manager until
                # those read models move behind the seam.
                metadata, related = await _enrich_entity_with_related(
                    entity,
                    entity_id,
                    runtime.entity_manager,
                    runtime.relationship_manager,
                    preloaded_related=related,
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

        log.debug("Entity not in graph, checking document chunks", entity_id=entity_id)

        # Fallback: check if it's a document chunk
        # Support both full UUIDs and prefix matching (e.g., "2cebcab8" matches "2cebcab8-...")
        async with get_session() as session:
            # Try exact UUID match first
            try:
                chunk_uuid = UUID(entity_id)
                result = await session.execute(
                    select(DocumentChunk, CrawledDocument, CrawlSource)
                    .join(CrawledDocument, DocumentChunk.document_id == CrawledDocument.id)
                    .join(CrawlSource, CrawledDocument.source_id == CrawlSource.id)
                    .where(col(DocumentChunk.id) == chunk_uuid)
                    .where(col(CrawlSource.organization_id) == org.id)
                )
                row = result.first()
            except ValueError:
                row = None

            # If no exact match and ID looks like a prefix (4-32 hex chars), try prefix match
            if (
                not row
                and len(entity_id) >= 4
                and all(c in "0123456789abcdef-" for c in entity_id.lower())
            ):
                from sqlalchemy import String, cast

                prefix = entity_id.lower().replace("-", "")
                result = await session.execute(
                    select(DocumentChunk, CrawledDocument, CrawlSource)
                    .join(CrawledDocument, DocumentChunk.document_id == CrawledDocument.id)
                    .join(CrawlSource, CrawledDocument.source_id == CrawlSource.id)
                    .where(cast(DocumentChunk.id, String).like(f"{prefix[:8]}%"))
                    .where(col(CrawlSource.organization_id) == org.id)
                    .limit(1)
                )
                row = result.first()

            if not row:
                raise HTTPException(status_code=404, detail=f"Entity not found: {entity_id}")

            chunk, doc, source = row

            # Build heading path as description
            heading_desc = " > ".join(chunk.heading_path) if chunk.heading_path else ""

            # For heading chunks, fetch the section content (chunks until next heading)
            # This provides context instead of just showing the heading text
            from sibyl.db.models import ChunkType

            section_content = chunk.content or ""
            if chunk.chunk_type == ChunkType.HEADING:
                # Get subsequent chunks until next heading (max 10 for reasonable size)
                following_result = await session.execute(
                    select(DocumentChunk)
                    .where(col(DocumentChunk.document_id) == chunk.document_id)
                    .where(col(DocumentChunk.chunk_index) > chunk.chunk_index)
                    .order_by(col(DocumentChunk.chunk_index))
                    .limit(10)
                )
                following_chunks = following_result.scalars().all()

                # Concatenate content until we hit another heading
                section_parts = [section_content]
                for fc in following_chunks:
                    if fc.chunk_type == ChunkType.HEADING:
                        break
                    section_parts.append(fc.content or "")

                section_content = "\n\n".join(section_parts)

            return EntityResponse(
                id=str(chunk.id),
                entity_type=EntityType.DOCUMENT,
                name=doc.title or source.name,
                description=heading_desc,
                content=section_content[:50000],
                category=chunk.chunk_type.value if chunk.chunk_type else None,
                languages=[chunk.language] if chunk.language else [],
                tags=[],
                metadata={
                    "source_id": str(source.id),
                    "source_name": source.name,
                    "source_url": source.url,
                    "document_id": str(doc.id),
                    "document_url": doc.url,
                    "chunk_index": chunk.chunk_index,
                    "chunk_type": chunk.chunk_type.value if chunk.chunk_type else None,
                    "heading_path": chunk.heading_path or [],
                    "result_origin": "document",
                },
                source_file=doc.url,
                created_at=chunk.created_at,
                updated_at=chunk.updated_at,
            )

    except HTTPException:
        raise
    except Exception as e:
        log.exception("get_entity_failed", entity_id=entity_id, error=str(e))
        raise HTTPException(
            status_code=500, detail="Failed to get entity. Please try again."
        ) from e


# =============================================================================
# Create
# =============================================================================


@router.post(
    "",
    response_model=EntityResponse,
    status_code=201,
    dependencies=[Depends(require_org_role(*_WRITE_ROLES))],
)
async def create_entity(
    request: Request,
    entity: EntityCreate,
    org: Organization = Depends(get_current_organization),
    ctx: AuthContext = Depends(get_auth_context),
    session: Any = Depends(get_session_dependency),
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
    try:
        from sibyl_core.tools.core import add

        group_id = str(org.id)

        # Extract task-specific fields from metadata if present
        project = entity.metadata.get("project_id") if entity.metadata else None
        epic = entity.metadata.get("epic_id") if entity.metadata else None
        priority = entity.metadata.get("priority") if entity.metadata else None
        assignees = entity.metadata.get("assignees") if entity.metadata else None
        technologies = entity.metadata.get("technologies") if entity.metadata else None
        depends_on = entity.metadata.get("depends_on") if entity.metadata else None

        # Use description as content fallback (frontend sends description, add() needs content)
        content = entity.content or entity.description or entity.name
        request_metadata = dict(entity.metadata or {})

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
        )

        if not result.success or not result.id:
            raise HTTPException(status_code=400, detail=result.message)

        if request_metadata.get("capture_mode") == "quick":
            await _archive_raw_capture(
                session,
                organization_id=org.id,
                user_id=ctx.user.id if ctx.user else None,
                entity_id=result.id,
                entity_name=entity.name,
                entity_content=content,
                entity_type=entity.entity_type.value,
                tags=list(entity.tags or []),
                metadata=request_metadata,
            )

        # For async creation, return immediately with pending response
        # Entity will be created in background via Graphiti
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
            return response

        # Sync creation - fetch the created entity
        runtime = await get_legacy_entity_runtime(group_id)
        created = await runtime.entity_manager.get(result.id)

        if not created:
            raise HTTPException(status_code=500, detail="Entity created but not found")

        response = EntityResponse(
            id=created.id,
            entity_type=created.entity_type,
            name=created.name,
            description=created.description or "",
            content=created.content or "",
            category=getattr(created, "category", None) or created.metadata.get("category"),
            languages=getattr(created, "languages", None)
            or created.metadata.get("languages", [])
            or [],
            tags=getattr(created, "tags", None) or created.metadata.get("tags", []) or [],
            metadata=getattr(created, "metadata", {}) or {},
            source_file=getattr(created, "source_file", None),
            created_at=getattr(created, "created_at", None),
            updated_at=getattr(created, "updated_at", None),
        )

        # Broadcast creation event (scoped to org)
        await broadcast_event(
            WSEvent.ENTITY_CREATED, response.model_dump(mode="json"), org_id=str(org.id)
        )

        if created.entity_type == EntityType.PROJECT:
            # Sync to Postgres for RBAC enforcement
            await sync_project_create(
                session,
                organization_id=org.id,
                owner_user_id=ctx.user.id,
                graph_project_id=created.id,
                name=created.name,
                description=created.description,
            )
            await session.commit()

            await AuditLogger(session).log(
                action="project.create",
                user_id=ctx.user.id,
                organization_id=org.id,
                request=request,
                details={"project_id": created.id, "name": created.name},
            )

        return response

    except HTTPException:
        raise
    except Exception as e:
        log.exception("create_entity_failed", error=str(e))
        raise HTTPException(
            status_code=500, detail="Failed to create entity. Please try again."
        ) from e


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
    org: Organization = Depends(get_current_organization),
    ctx: AuthContext = Depends(get_auth_context),
    session: Any = Depends(get_session_dependency),
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

            runtime = await get_legacy_entity_runtime(group_id)

            # Get existing entity
            existing = await runtime.entity_manager.get(entity_id)
            if not existing:
                raise HTTPException(status_code=404, detail=f"Entity not found: {entity_id}")

            # Verify project access for entities with project_id
            project_id = existing.metadata.get("project_id") if existing.metadata else None
            await verify_entity_project_access(
                session, ctx, project_id, required_role=ProjectRole.CONTRIBUTOR
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
                # Sync to Postgres for RBAC enforcement
                await sync_project_update(
                    session,
                    organization_id=org.id,
                    graph_project_id=existing.id,
                    name=response.name,
                    description=response.description,
                )
                await session.commit()

                await AuditLogger(session).log(
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
    org: Organization = Depends(get_current_organization),
    ctx: AuthContext = Depends(get_auth_context),
    session: Any = Depends(get_session_dependency),
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

            runtime = await get_legacy_entity_runtime(group_id)

            # Check existence
            existing = await runtime.entity_manager.get(entity_id)
            if not existing:
                raise HTTPException(status_code=404, detail=f"Entity not found: {entity_id}")

            # Verify project access for entities with project_id (maintainer required to delete)
            project_id = existing.metadata.get("project_id") if existing.metadata else None
            await verify_entity_project_access(
                session, ctx, project_id, required_role=ProjectRole.MAINTAINER
            )

            if existing.entity_type == EntityType.PROJECT:
                await AuditLogger(session).log(
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

            # Sync delete to Postgres (cascades project_members)
            if existing.entity_type == EntityType.PROJECT:
                await sync_project_delete(
                    session,
                    organization_id=org.id,
                    graph_project_id=existing.id,
                )
                await session.commit()

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
