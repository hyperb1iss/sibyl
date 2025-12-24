"""Entity CRUD endpoints.

Full create, read, update, delete operations for all entity types.
"""

from datetime import UTC, datetime
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

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
from sibyl.db.connection import get_session_dependency
from sibyl.db.models import Organization, OrganizationRole
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
                content=entity.content or "",
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
    """Get a single entity by ID."""
    try:
        group_id = str(org.id)
        client = await get_graph_client()
        entity_manager = EntityManager(client, group_id=group_id)

        entity = await entity_manager.get(entity_id)
        if not entity:
            raise HTTPException(status_code=404, detail=f"Entity not found: {entity_id}")

        return EntityResponse(
            id=entity.id,
            entity_type=entity.entity_type,
            name=entity.name,
            description=entity.description or "",
            content=entity.content or "",
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
) -> EntityResponse:
    """Create a new entity."""
    try:
        from sibyl.tools.core import add

        group_id = str(org.id)

        # Extract task-specific fields from metadata if present
        project = entity.metadata.get("project_id") if entity.metadata else None
        priority = entity.metadata.get("priority") if entity.metadata else None
        assignees = entity.metadata.get("assignees") if entity.metadata else None
        auto_link = bool(entity.metadata.get("auto_link")) if entity.metadata else False

        # Use description as content fallback (frontend sends description, add() needs content)
        content = entity.content or entity.description or entity.name

        merged_metadata: dict[str, Any] = {**(entity.metadata or {}), "organization_id": group_id}

        # Projects are sync (foundational - tasks depend on them existing)
        # Tasks and other entities are async (processed in background)
        is_sync = entity.entity_type.value == "project"

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
            priority=priority,
            assignees=assignees,
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
