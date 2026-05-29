"""Epic workflow endpoints.

Dedicated endpoints for epic lifecycle operations with proper event broadcasting.
"""

from datetime import UTC, datetime
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from sibyl.api.decorators import handle_workflow_errors
from sibyl.api.event_types import WSEvent
from sibyl.api.websocket import broadcast_event
from sibyl.auth.context import AuthContext
from sibyl.auth.dependencies import get_auth_context, get_current_organization, require_org_role
from sibyl.persistence.auth_runtime import verify_entity_project_access
from sibyl.services.work_item_workflow import WorkItemAction, transition_work_item
from sibyl_core.auth import AuthOrganization, OrganizationRole, ProjectRole
from sibyl_core.models.entities import EntityType
from sibyl_core.services import KnowledgeReadService

log = structlog.get_logger()
_WRITE_ROLES = (
    OrganizationRole.OWNER,
    OrganizationRole.ADMIN,
    OrganizationRole.MEMBER,
)


async def get_knowledge_read_adapter(group_id: str):
    from sibyl.persistence.graph_runtime import get_knowledge_read_adapter as service

    return await service(group_id)


async def update_graph_entity(group_id: str, entity_id: str, patch: dict[str, object]):
    from sibyl.persistence.graph_runtime import update_graph_entity as service

    return await service(group_id, entity_id, patch)


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


async def _get_epic(service: KnowledgeReadService, epic_id: str):
    """Get an epic by ID, raising HTTPException if not found or wrong type."""
    try:
        epic = await service.get_entity(epic_id)
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
    org: AuthOrganization,
    ctx: AuthContext,
    required_role: ProjectRole = ProjectRole.CONTRIBUTOR,
) -> Any:
    """Fetch an epic and verify project access.

    Returns the epic entity if access is granted.
    Raises ProjectAuthorizationError if user lacks required access.
    """
    service = await get_knowledge_read_adapter(str(org.id))
    epic = await _get_epic(service, epic_id)

    # Extract project_id from entity metadata
    project_id = epic.metadata.get("project_id") if epic.metadata else None
    await verify_entity_project_access(
        ctx=ctx,
        entity_project_id=project_id,
        required_role=required_role,
        require_existing_project=True,
    )

    return epic


async def _broadcast_epic_update(
    epic_id: str, action: str, data: dict[str, Any], *, org_id: str | None = None
) -> None:
    """Broadcast epic update event (scoped to org)."""
    await broadcast_event(
        WSEvent.ENTITY_UPDATED,
        {
            "id": epic_id,
            "entity_type": "epic",
            "action": action,
            **data,
        },
        org_id=org_id,
    )


async def _update_project_activity(group_id: str, epic: Any) -> None:
    """Update parent project's last_activity_at when epic changes."""
    project_id = epic.metadata.get("project_id") if hasattr(epic, "metadata") else None
    if not project_id:
        return

    try:
        await update_graph_entity(
            group_id,
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
@handle_workflow_errors("start_epic", id_param="epic_id")
async def start_epic(
    epic_id: str,
    org: AuthOrganization = Depends(get_current_organization),
    ctx: AuthContext = Depends(get_auth_context),
) -> EpicActionResponse:
    """Start working on an epic (moves to 'in_progress' status)."""
    # Verify project access (contributor role required to start work)
    epic = await _verify_epic_access(epic_id, org, ctx, ProjectRole.CONTRIBUTOR)

    result = await transition_work_item(
        str(org.id),
        epic_id,
        WorkItemAction.START_EPIC,
        entity=epic,
    )

    return EpicActionResponse(
        success=True,
        action="start_epic",
        epic_id=epic_id,
        message="Epic started",
        data=result.response_data,
    )


@router.post("/{epic_id}/complete", response_model=EpicActionResponse)
@handle_workflow_errors("complete_epic", id_param="epic_id")
async def complete_epic(
    epic_id: str,
    org: AuthOrganization = Depends(get_current_organization),
    ctx: AuthContext = Depends(get_auth_context),
    request: CompleteEpicRequest | None = None,
) -> EpicActionResponse:
    """Complete an epic with optional learnings."""
    # Verify project access (maintainer role required to complete)
    epic = await _verify_epic_access(epic_id, org, ctx, ProjectRole.MAINTAINER)

    learnings = request.learnings if request else None
    result = await transition_work_item(
        str(org.id),
        epic_id,
        WorkItemAction.COMPLETE_EPIC,
        payload={"learnings": learnings},
        entity=epic,
    )

    return EpicActionResponse(
        success=True,
        action="complete_epic",
        epic_id=epic_id,
        message="Epic completed" + (" with learnings captured" if learnings else ""),
        data=result.response_data,
    )


@router.post("/{epic_id}/archive", response_model=EpicActionResponse)
@handle_workflow_errors("archive_epic", id_param="epic_id")
async def archive_epic(
    epic_id: str,
    org: AuthOrganization = Depends(get_current_organization),
    ctx: AuthContext = Depends(get_auth_context),
    request: ArchiveEpicRequest | None = None,
) -> EpicActionResponse:
    """Archive an epic."""
    # Verify project access (maintainer role required to archive)
    epic = await _verify_epic_access(epic_id, org, ctx, ProjectRole.MAINTAINER)

    reason = request.reason if request else None
    result = await transition_work_item(
        str(org.id),
        epic_id,
        WorkItemAction.ARCHIVE_EPIC,
        entity=epic,
    )

    return EpicActionResponse(
        success=True,
        action="archive_epic",
        epic_id=epic_id,
        message="Epic archived" + (f": {reason}" if reason else ""),
        data=result.response_data,
    )


@router.patch("/{epic_id}", response_model=EpicActionResponse)
@handle_workflow_errors("update_epic", id_param="epic_id")
async def update_epic(
    epic_id: str,
    request: UpdateEpicRequest,
    org: AuthOrganization = Depends(get_current_organization),
    ctx: AuthContext = Depends(get_auth_context),
) -> EpicActionResponse:
    """Update epic fields."""
    # Verify project access (contributor role required to update)
    epic = await _verify_epic_access(epic_id, org, ctx, ProjectRole.CONTRIBUTOR)

    group_id = str(org.id)
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

    await update_graph_entity(group_id, epic_id, updates)
    await _update_project_activity(group_id, epic)

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
