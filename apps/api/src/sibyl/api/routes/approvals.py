"""Approval queue endpoints for human-in-the-loop agent coordination.

REST API for managing agent approval requests.
"""

from datetime import UTC, datetime

import structlog
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import update

from sibyl.auth.dependencies import get_current_organization, get_current_user, require_org_role
from sibyl.db import get_session
from sibyl.db.models import AgentMessage, Organization, OrganizationRole, User
from sibyl_core.graph.client import get_graph_client
from sibyl_core.graph.entities import EntityManager
from sibyl_core.models import ApprovalStatus, ApprovalType, EntityType

log = structlog.get_logger()
_WRITE_ROLES = (OrganizationRole.OWNER, OrganizationRole.ADMIN, OrganizationRole.MEMBER)

router = APIRouter(
    prefix="/approvals",
    tags=["approvals"],
    dependencies=[Depends(require_org_role(*_WRITE_ROLES))],
)


# =============================================================================
# Request/Response Models
# =============================================================================


class ApprovalResponse(BaseModel):
    """Approval record response."""

    id: str
    agent_id: str
    agent_name: str | None = None
    task_id: str | None = None
    project_id: str
    approval_type: str
    priority: str
    title: str
    summary: str
    status: str
    actions: list[str]
    metadata: dict | None = None
    created_at: str | None = None
    expires_at: str | None = None
    responded_at: str | None = None
    response_by: str | None = None
    response_message: str | None = None


class ApprovalListResponse(BaseModel):
    """Response containing list of approvals."""

    approvals: list[ApprovalResponse]
    total: int
    by_status: dict[str, int] = {}
    by_type: dict[str, int] = {}


class RespondToApprovalRequest(BaseModel):
    """Request to respond to an approval."""

    action: str  # approve, deny, edit
    message: str | None = None
    edited_content: dict | None = None  # For edited responses


class RespondToApprovalResponse(BaseModel):
    """Response from approval action."""

    success: bool
    approval_id: str
    action: str
    message: str


# =============================================================================
# Endpoints
# =============================================================================


@router.get("", response_model=ApprovalListResponse)
async def list_approvals(
    status: ApprovalStatus | None = None,
    approval_type: ApprovalType | None = None,
    agent_id: str | None = None,
    project_id: str | None = None,
    limit: int = 50,
    org: Organization = Depends(get_current_organization),
) -> ApprovalListResponse:
    """List approval requests for the organization.

    Args:
        status: Filter by approval status (pending, approved, denied, etc.)
        approval_type: Filter by type (destructive_command, question, etc.)
        agent_id: Filter by specific agent
        project_id: Filter by project
        limit: Maximum results
        org: Current organization
    """
    client = await get_graph_client()
    manager = EntityManager(client, group_id=str(org.id))

    # Get all approvals
    results = await manager.list_by_type(entity_type=EntityType.APPROVAL, limit=limit * 2)

    approvals = list(results)

    # Apply filters
    if status:
        approvals = [a for a in approvals if (a.metadata or {}).get("status") == status.value]
    if approval_type:
        approvals = [
            a for a in approvals if (a.metadata or {}).get("approval_type") == approval_type.value
        ]
    if agent_id:
        approvals = [a for a in approvals if (a.metadata or {}).get("agent_id") == agent_id]
    if project_id:
        approvals = [a for a in approvals if (a.metadata or {}).get("project_id") == project_id]

    # Calculate stats
    by_status: dict[str, int] = {}
    by_type: dict[str, int] = {}
    for approval in approvals:
        meta = approval.metadata or {}
        s = meta.get("status", "pending")
        by_status[s] = by_status.get(s, 0) + 1
        t = meta.get("approval_type", "unknown")
        by_type[t] = by_type.get(t, 0) + 1

    # Sort by created_at descending (newest first)
    approvals.sort(key=lambda a: a.created_at or datetime.min.replace(tzinfo=UTC), reverse=True)

    return ApprovalListResponse(
        approvals=[_entity_to_approval_response(a) for a in approvals[:limit]],
        total=len(approvals),
        by_status=by_status,
        by_type=by_type,
    )


@router.get("/pending", response_model=ApprovalListResponse)
async def list_pending_approvals(
    project_id: str | None = None,
    limit: int = 50,
    org: Organization = Depends(get_current_organization),
) -> ApprovalListResponse:
    """List pending approval requests - convenience endpoint for the queue."""
    return await list_approvals(
        status=ApprovalStatus.PENDING,
        project_id=project_id,
        limit=limit,
        org=org,
    )


@router.get("/{approval_id}", response_model=ApprovalResponse)
async def get_approval(
    approval_id: str,
    org: Organization = Depends(get_current_organization),
) -> ApprovalResponse:
    """Get a specific approval by ID."""
    client = await get_graph_client()
    manager = EntityManager(client, group_id=str(org.id))

    entity = await manager.get(approval_id)
    if not entity or entity.entity_type != EntityType.APPROVAL:
        raise HTTPException(status_code=404, detail=f"Approval not found: {approval_id}")

    return _entity_to_approval_response(entity)


@router.post("/{approval_id}/respond", response_model=RespondToApprovalResponse)
async def respond_to_approval(
    approval_id: str,
    request: RespondToApprovalRequest,
    user: User = Depends(get_current_user),
    org: Organization = Depends(get_current_organization),
) -> RespondToApprovalResponse:
    """Respond to an approval request (approve, deny, or edit)."""
    client = await get_graph_client()
    manager = EntityManager(client, group_id=str(org.id))

    entity = await manager.get(approval_id)
    if not entity or entity.entity_type != EntityType.APPROVAL:
        raise HTTPException(status_code=404, detail=f"Approval not found: {approval_id}")

    # Check current status
    current_status = (entity.metadata or {}).get("status", "pending")
    if current_status != ApprovalStatus.PENDING.value:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot respond to approval in {current_status} status",
        )

    # Map action to status
    action_to_status = {
        "approve": ApprovalStatus.APPROVED.value,
        "deny": ApprovalStatus.DENIED.value,
        "edit": ApprovalStatus.EDITED.value,
    }

    if request.action not in action_to_status:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid action: {request.action}. Must be one of: approve, deny, edit",
        )

    new_status = action_to_status[request.action]

    # Update the approval
    updates = {
        "status": new_status,
        "responded_at": datetime.now(UTC).isoformat(),
        "response_by": str(user.id),
        "response_message": request.message,
    }

    if request.action == "edit" and request.edited_content:
        updates["edited_content"] = request.edited_content

    await manager.update(approval_id, updates)

    # Get agent info for status updates
    agent_id = (entity.metadata or {}).get("agent_id")

    # Update the stored AgentMessage's status so page reload shows resolved state
    if agent_id:
        try:
            async with get_session() as session:
                # Find the approval message by matching approval_id in extra JSONB
                stmt = (
                    update(AgentMessage)
                    .where(AgentMessage.agent_id == agent_id)
                    .where(AgentMessage.extra["approval_id"].astext == approval_id)
                    .values(
                        extra=AgentMessage.extra.op("||")(
                            {"status": new_status, "responded_at": datetime.now(UTC).isoformat()}
                        )
                    )
                )
                await session.execute(stmt)
                await session.commit()
        except Exception as e:
            log.warning("Failed to update approval message status", error=str(e))

    # Publish to worker channel - this wakes up the waiting agent
    from sibyl.agents.redis_sub import publish_approval_response

    await publish_approval_response(
        approval_id,
        {
            "approved": request.action == "approve",
            "action": request.action,
            "by": str(user.id),
            "message": request.message or "",
            "edited_content": request.edited_content,
        },
    )

    # Update agent status back to working
    if agent_id:
        from sibyl_core.models import AgentStatus

        await manager.update(agent_id, {"status": AgentStatus.WORKING.value})

    # Broadcast approval response event to UI
    from sibyl.api.pubsub import publish_event

    await publish_event(
        "approval_response",
        {
            "approval_id": approval_id,
            "agent_id": agent_id,
            "action": request.action,
            "status": new_status,
            "response_by": str(user.id),
        },
        org_id=str(org.id),
    )

    # Also broadcast agent status change to UI
    if agent_id:
        await publish_event(
            "agent_status",
            {"agent_id": agent_id, "status": "working"},
            org_id=str(org.id),
        )

    log.info(
        "Approval responded",
        approval_id=approval_id,
        action=request.action,
        user_id=str(user.id),
    )

    return RespondToApprovalResponse(
        success=True,
        approval_id=approval_id,
        action=request.action,
        message=f"Approval {request.action}d successfully",
    )


# =============================================================================
# Helpers
# =============================================================================


def _entity_to_approval_response(entity) -> ApprovalResponse:
    """Convert Entity to ApprovalResponse."""
    meta = entity.metadata or {}
    return ApprovalResponse(
        id=entity.id,
        agent_id=meta.get("agent_id", ""),
        agent_name=meta.get("agent_name"),
        task_id=meta.get("task_id"),
        project_id=meta.get("project_id", ""),
        approval_type=meta.get("approval_type", "unknown"),
        priority=meta.get("priority", "medium"),
        title=entity.name or meta.get("title", ""),
        summary=entity.content or meta.get("summary", ""),
        status=meta.get("status", "pending"),
        actions=meta.get("actions", ["approve", "deny"]),
        metadata=meta.get("metadata"),
        created_at=entity.created_at.isoformat() if entity.created_at else None,
        expires_at=meta.get("expires_at"),
        responded_at=meta.get("responded_at"),
        response_by=meta.get("response_by"),
        response_message=meta.get("response_message"),
    )
