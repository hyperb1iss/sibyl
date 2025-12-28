"""Entity CRUD endpoints.

Full create, read, update, delete operations for all entity types.
Transparently handles both graph entities (FalkorDB) and document chunks (Postgres).
"""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col, select

from sibyl.api.schemas import (
    EntityCreate,
    EntityListResponse,
    EntityResponse,
    EntityUpdate,
)
from sibyl.api.websocket import broadcast_event
from sibyl.auth.audit import AuditLogger
from sibyl.auth.context import AuthContext
from sibyl.auth.dependencies import get_auth_context, get_current_organization, require_org_role
from sibyl.db import CrawledDocument, CrawlSource, DocumentChunk, get_session
from sibyl.db.connection import get_session_dependency
from sibyl.db.models import Organization, OrganizationRole
from sibyl.errors import EntityNotFoundError
from sibyl.graph.client import get_graph_client
from sibyl.graph.entities import EntityManager
from sibyl.models.entities import EntityType

log = structlog.get_logger()
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


# =============================================================================
# List / Read
# =============================================================================


@router.get("", response_model=EntityListResponse)
async def list_entities(
    org: Organization = Depends(get_current_organization),
    entity_type: EntityType | None = Query(default=None, description="Filter by entity type"),
    language: str | None = Query(default=None, description="Filter by programming language"),
    category: str | None = Query(default=None, description="Filter by category"),
    page: int = Query(default=1, ge=1, description="Page number"),
    page_size: int = Query(default=50, ge=1, le=200, description="Items per page"),
) -> EntityListResponse:
    """List entities with optional filters and pagination."""
    try:
        group_id = str(org.id)
        client = await get_graph_client()
        entity_manager = EntityManager(client, group_id=group_id)

        # Get all entities of specified type(s)
        if entity_type:
            all_entities = await entity_manager.list_by_type(entity_type, limit=1000)
        else:
            # Fetch all types
            all_entities = []
            for et in EntityType:
                entities = await entity_manager.list_by_type(et, limit=500)
                all_entities.extend(entities)

        # Apply filters
        filtered = []
        for entity in all_entities:
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

            filtered.append(entity)

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
) -> EntityResponse:
    """Get a single entity by ID.

    Transparently handles both:
    - Graph entities (stored in FalkorDB)
    - Document chunks (stored in Postgres via crawler)

    This provides a seamless experience for search results that may come from either source.
    """
    try:
        group_id = str(org.id)
        client = await get_graph_client()
        entity_manager = EntityManager(client, group_id=group_id)

        # Try graph entity first
        try:
            entity = await entity_manager.get(entity_id)

            # Enrich epics with progress stats
            metadata = getattr(entity, "metadata", {}) or {}
            if entity.entity_type == "epic":
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
            )
        except EntityNotFoundError:
            log.debug("Entity not in graph, checking document chunks", entity_id=entity_id)

        # Fallback: check if it's a document chunk
        try:
            chunk_uuid = UUID(entity_id)
        except ValueError:
            raise HTTPException(status_code=404, detail=f"Entity not found: {entity_id}") from None

        async with get_session() as session:
            result = await session.execute(
                select(DocumentChunk, CrawledDocument, CrawlSource)
                .join(CrawledDocument, DocumentChunk.document_id == CrawledDocument.id)
                .join(CrawlSource, CrawledDocument.source_id == CrawlSource.id)
                .where(col(DocumentChunk.id) == chunk_uuid)
                .where(col(CrawlSource.organization_id) == org.id)
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
    session: AsyncSession = Depends(get_session_dependency),
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
        from sibyl.tools.core import add

        group_id = str(org.id)

        # Extract task-specific fields from metadata if present
        project = entity.metadata.get("project_id") if entity.metadata else None
        epic = entity.metadata.get("epic_id") if entity.metadata else None
        priority = entity.metadata.get("priority") if entity.metadata else None
        assignees = entity.metadata.get("assignees") if entity.metadata else None
        technologies = entity.metadata.get("technologies") if entity.metadata else None
        depends_on = entity.metadata.get("depends_on") if entity.metadata else None
        auto_link = bool(entity.metadata.get("auto_link")) if entity.metadata else False

        # Use description as content fallback (frontend sends description, add() needs content)
        content = entity.content or entity.description or entity.name

        merged_metadata: dict[str, Any] = {**(entity.metadata or {}), "organization_id": group_id}

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
            # Auto-link
            auto_link=auto_link,
            # Sync for projects, async for everything else
            sync=is_sync,
        )

        if not result.success or not result.id:
            raise HTTPException(status_code=400, detail=result.message)

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
                "entity_pending", response.model_dump(mode="json"), org_id=str(org.id)
            )
            return response

        # Sync creation - fetch the created entity
        client = await get_graph_client()
        entity_manager = EntityManager(client, group_id=group_id)
        created = await entity_manager.get(result.id)

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
            "entity_created", response.model_dump(mode="json"), org_id=str(org.id)
        )

        if created.entity_type == EntityType.PROJECT:
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
    session: AsyncSession = Depends(get_session_dependency),
) -> EntityResponse:
    """Update an existing entity."""
    try:
        group_id = str(org.id)
        client = await get_graph_client()
        entity_manager = EntityManager(client, group_id=group_id)

        # Get existing entity
        existing = await entity_manager.get(entity_id)
        if not existing:
            raise HTTPException(status_code=404, detail=f"Entity not found: {entity_id}")

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
        updated = await entity_manager.update(entity_id, update_data)
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
            "entity_updated", response.model_dump(mode="json"), org_id=str(org.id)
        )

        if existing.entity_type == EntityType.PROJECT:
            await AuditLogger(session).log(
                action="project.update",
                user_id=ctx.user.id,
                organization_id=org.id,
                request=request,
                details={"project_id": existing.id, "name": response.name},
            )

        return response

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
    session: AsyncSession = Depends(get_session_dependency),
) -> None:
    """Delete an entity."""
    try:
        group_id = str(org.id)
        client = await get_graph_client()
        entity_manager = EntityManager(client, group_id=group_id)

        # Check existence
        existing = await entity_manager.get(entity_id)
        if not existing:
            raise HTTPException(status_code=404, detail=f"Entity not found: {entity_id}")

        if existing.entity_type == EntityType.PROJECT:
            await AuditLogger(session).log(
                action="project.delete",
                user_id=ctx.user.id,
                organization_id=org.id,
                request=request,
                details={"project_id": existing.id, "name": existing.name},
            )

        # Delete
        success = await entity_manager.delete(entity_id)
        if not success:
            raise HTTPException(status_code=500, detail="Delete failed")

        # Broadcast deletion event (scoped to org)
        await broadcast_event(
            "entity_deleted",
            {"id": entity_id, "type": existing.entity_type.value, "name": existing.name},
            org_id=str(org.id),
        )

    except HTTPException:
        raise
    except Exception as e:
        log.exception("delete_entity_failed", entity_id=entity_id, error=str(e))
        raise HTTPException(
            status_code=500, detail="Failed to delete entity. Please try again."
        ) from e
