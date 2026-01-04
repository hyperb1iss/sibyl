"""Epic workflow endpoints.

Dedicated endpoints for epic lifecycle operations with proper event broadcasting.
"""

from datetime import UTC, datetime
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from sibyl.api.websocket import broadcast_event
from sibyl.auth.authorization import ProjectRole, verify_entity_project_access
from sibyl.auth.context import AuthContext
from sibyl.auth.dependencies import get_auth_context, get_current_organization, require_org_role
from sibyl.db.connection import get_session_dependency
from sibyl.db.models import Organization, OrganizationRole
from sibyl_core.graph.client import get_graph_client
from sibyl_core.graph.entities import EntityManager
from sibyl_core.models.entities import EntityType

log = structlog.get_logger()
_WRITE_ROLES = (
    OrganizationRole.OWNER,
    OrganizationRole.ADMIN,
    OrganizationRole.MEMBER,
)

router = APIRouter(
    prefix="/epics",
    tags=["epics"],
    dependencies=[Depends(require_org_role(*_WRITE_ROLES))],
)


# =============================================================================
# Request/Response Models
# =============================================================================


class EpicActionResponse(BaseModel):
    """Response from epic workflow action."""

    success: bool
    action: str
    epic_id: str
    message: str
    data: dict[str, Any] = {}


class CompleteEpicRequest(BaseModel):
    """Request to complete an epic."""

    learnings: str | None = None


class ArchiveEpicRequest(BaseModel):
    """Request to archive an epic."""

    reason: str | None = None


class UpdateEpicRequest(BaseModel):
    """Request to update epic fields."""

    status: str | None = None
    priority: str | None = None
    title: str | None = None
    description: str | None = None
    assignees: list[str] | None = None
    tags: list[str] | None = None


# =============================================================================
# Helper Functions
# =============================================================================


async def _get_epic(entity_manager: EntityManager, epic_id: str):
    """Get an epic by ID, raising HTTPException if not found or wrong type."""
    try:
        epic = await entity_manager.get(epic_id)
        if not epic:
            raise HTTPException(status_code=404, detail=f"Epic not found: {epic_id}")
        if epic.entity_type != EntityType.EPIC:
            raise HTTPException(status_code=400, detail=f"Entity is not an epic: {epic_id}")
        return epic
    except HTTPException:
        raise
    except Exception as e:
        log.exception("get_epic_failed", epic_id=epic_id, error=str(e))
        raise HTTPException(status_code=404, detail=f"Epic not found: {epic_id}") from e


async def _verify_epic_access(
    epic_id: str,
    org: Organization,
    ctx: AuthContext,
    session: AsyncSession,
    required_role: ProjectRole = ProjectRole.CONTRIBUTOR,
) -> Any:
    """Fetch an epic and verify project access.

    Returns the epic entity if access is granted.
    Raises ProjectAuthorizationError if user lacks required access.
    """
    client = await get_graph_client()
    entity_manager = EntityManager(client, group_id=str(org.id))
    epic = await _get_epic(entity_manager, epic_id)

    # Extract project_id from entity metadata
    project_id = epic.metadata.get("project_id") if epic.metadata else None
    await verify_entity_project_access(session, ctx, project_id, required_role=required_role)

    return epic


async def _broadcast_epic_update(
    epic_id: str, action: str, data: dict[str, Any], *, org_id: str | None = None
) -> None:
    """Broadcast epic update event (scoped to org)."""
    await broadcast_event(
        "entity_updated",
        {
            "id": epic_id,
            "entity_type": "epic",
            "action": action,
            **data,
        },
        org_id=org_id,
    )


async def _update_project_activity(entity_manager: EntityManager, epic: Any) -> None:
    """Update parent project's last_activity_at when epic changes."""
    project_id = epic.metadata.get("project_id") if hasattr(epic, "metadata") else None
    if not project_id:
        return

    try:
        await entity_manager.update(
            project_id,
            {"last_activity_at": datetime.now(UTC).isoformat()},
        )
        log.debug("Project activity updated from epic", project_id=project_id, epic_id=epic.id)
    except Exception as e:
        # Don't fail the epic operation if project update fails
        log.warning("Failed to update project activity", project_id=project_id, error=str(e))


# =============================================================================
# Workflow Endpoints
# =============================================================================


@router.post("/{epic_id}/start", response_model=EpicActionResponse)
async def start_epic(
    epic_id: str,
    org: Organization = Depends(get_current_organization),
    ctx: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session_dependency),
) -> EpicActionResponse:
    """Start working on an epic (moves to 'in_progress' status)."""
    try:
        # Verify project access (contributor role required to start work)
        epic = await _verify_epic_access(epic_id, org, ctx, session, ProjectRole.CONTRIBUTOR)

        group_id = str(org.id)
        client = await get_graph_client()
        entity_manager = EntityManager(client, group_id=group_id)

        await entity_manager.update(epic_id, {"status": "in_progress"})
        await _update_project_activity(entity_manager, epic)

        await _broadcast_epic_update(
            epic_id,
            "start_epic",
            {"status": "in_progress", "name": epic.name},
            org_id=group_id,
        )

        return EpicActionResponse(
            success=True,
            action="start_epic",
            epic_id=epic_id,
            message="Epic started",
            data={"status": "in_progress"},
        )

    except HTTPException:
        raise
    except Exception as e:
        log.exception("start_epic_failed", epic_id=epic_id, error=str(e))
        raise HTTPException(
            status_code=500, detail="Failed to start epic. Please try again."
        ) from e


@router.post("/{epic_id}/complete", response_model=EpicActionResponse)
async def complete_epic(
    epic_id: str,
    org: Organization = Depends(get_current_organization),
    ctx: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session_dependency),
    request: CompleteEpicRequest | None = None,
) -> EpicActionResponse:
    """Complete an epic with optional learnings."""
    try:
        # Verify project access (maintainer role required to complete)
        epic = await _verify_epic_access(epic_id, org, ctx, session, ProjectRole.MAINTAINER)

        group_id = str(org.id)
        client = await get_graph_client()
        entity_manager = EntityManager(client, group_id=group_id)

        learnings = request.learnings if request else None
        updates = {
            "status": "completed",
            "completed_date": datetime.now(UTC).isoformat(),
        }
        if learnings:
            updates["learnings"] = learnings

        await entity_manager.update(epic_id, updates)
        await _update_project_activity(entity_manager, epic)

        await _broadcast_epic_update(
            epic_id,
            "complete_epic",
            {"status": "completed", "learnings": learnings or "", "name": epic.name},
            org_id=group_id,
        )

        return EpicActionResponse(
            success=True,
            action="complete_epic",
            epic_id=epic_id,
            message="Epic completed" + (" with learnings captured" if learnings else ""),
            data={"status": "completed", "learnings": learnings or ""},
        )

    except HTTPException:
        raise
    except Exception as e:
        log.exception("complete_epic_failed", epic_id=epic_id, error=str(e))
        raise HTTPException(
            status_code=500, detail="Failed to complete epic. Please try again."
        ) from e


@router.post("/{epic_id}/archive", response_model=EpicActionResponse)
async def archive_epic(
    epic_id: str,
    org: Organization = Depends(get_current_organization),
    ctx: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session_dependency),
    request: ArchiveEpicRequest | None = None,
) -> EpicActionResponse:
    """Archive an epic."""
    try:
        # Verify project access (maintainer role required to archive)
        epic = await _verify_epic_access(epic_id, org, ctx, session, ProjectRole.MAINTAINER)

        group_id = str(org.id)
        client = await get_graph_client()
        entity_manager = EntityManager(client, group_id=group_id)

        reason = request.reason if request else None
        await entity_manager.update(epic_id, {"status": "archived"})
        await _update_project_activity(entity_manager, epic)

        await _broadcast_epic_update(
            epic_id,
            "archive_epic",
            {"status": "archived", "name": epic.name},
            org_id=group_id,
        )

        return EpicActionResponse(
            success=True,
            action="archive_epic",
            epic_id=epic_id,
            message="Epic archived" + (f": {reason}" if reason else ""),
            data={"status": "archived"},
        )

    except HTTPException:
        raise
    except Exception as e:
        log.exception("archive_epic_failed", epic_id=epic_id, error=str(e))
        raise HTTPException(
            status_code=500, detail="Failed to archive epic. Please try again."
        ) from e


@router.patch("/{epic_id}", response_model=EpicActionResponse)
async def update_epic(
    epic_id: str,
    request: UpdateEpicRequest,
    org: Organization = Depends(get_current_organization),
    ctx: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session_dependency),
) -> EpicActionResponse:
    """Update epic fields."""
    try:
        # Verify project access (contributor role required to update)
        epic = await _verify_epic_access(epic_id, org, ctx, session, ProjectRole.CONTRIBUTOR)

        group_id = str(org.id)
        client = await get_graph_client()
        entity_manager = EntityManager(client, group_id=group_id)

        # Build update dict from request
        updates = {}
        if request.status is not None:
            updates["status"] = request.status
        if request.priority is not None:
            updates["priority"] = request.priority
        if request.title is not None:
            updates["title"] = request.title
            updates["name"] = request.title  # Keep name in sync
        if request.description is not None:
            updates["description"] = request.description
        if request.assignees is not None:
            updates["assignees"] = request.assignees
        if request.tags is not None:
            updates["tags"] = request.tags

        if not updates:
            raise HTTPException(status_code=400, detail="No fields to update")

        await entity_manager.update(epic_id, updates)
        await _update_project_activity(entity_manager, epic)

        await _broadcast_epic_update(
            epic_id,
            "update_epic",
            {"updates": list(updates.keys()), "name": epic.name},
            org_id=group_id,
        )

        return EpicActionResponse(
            success=True,
            action="update_epic",
            epic_id=epic_id,
            message=f"Epic updated: {', '.join(updates.keys())}",
            data=updates,
        )

    except HTTPException:
        raise
    except Exception as e:
        log.exception("update_epic_failed", epic_id=epic_id, error=str(e))
        raise HTTPException(
            status_code=500, detail="Failed to update epic. Please try again."
        ) from e
