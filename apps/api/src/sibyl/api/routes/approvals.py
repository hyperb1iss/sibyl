"""Approval queue endpoints for human-in-the-loop agent coordination.

REST API for managing agent approval requests.
"""

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import structlog
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from sibyl.auth.authorization import (
    list_accessible_project_graph_ids,
    verify_entity_project_access,
)
from sibyl.auth.context import AuthContext
from sibyl.auth.dependencies import get_auth_context, require_org_role
from sibyl.db.connection import get_session_dependency
from sibyl.db.models import AgentMessage, OrganizationRole, ProjectRole
from sibyl_core.graph.client import get_graph_client
from sibyl_core.graph.entities import EntityManager
from sibyl_core.models import ApprovalStatus, ApprovalType, EntityType

if TYPE_CHECKING:
    from sibyl_core.models.entities import Entity

log = structlog.get_logger()
_WRITE_ROLES = (OrganizationRole.OWNER, OrganizationRole.ADMIN, OrganizationRole.MEMBER)


async def _check_approval_view_permission(
    ctx: AuthContext,
    session: AsyncSession,
    entity: "Entity",
) -> None:
    """Verify user can view an approval.

    View is allowed if:
    - User is org admin/owner
    - User created the agent that requested approval
    - User has VIEWER+ access to the approval's project

    Raises:
        HTTPException 403: If user lacks view permission
    """
    meta = entity.metadata or {}
    project_id = meta.get("project_id")
    created_by = meta.get("created_by")

    # Org admins can view any approval
    if ctx.org_role in (OrganizationRole.OWNER, OrganizationRole.ADMIN):
        return

    # Agent creator can view their approval
    if created_by and str(ctx.user.id) == created_by:
        return

    # Check project access (VIEWER+ required for read operations)
    if project_id:
        await verify_entity_project_access(
            session,
            ctx,
            project_id,
            required_role=ProjectRole.VIEWER,
        )
        return

    # No project - only creator or admin can view
    raise HTTPException(
        status_code=403,
        detail="You don't have permission to view this approval",
    )


async def _check_approval_respond_permission(
    ctx: AuthContext,
    session: AsyncSession,
    entity: "Entity",
) -> None:
    """Verify user can respond to an approval.

    Respond is allowed if:
    - User is org admin/owner
    - User created the agent that requested approval
    - User has CONTRIBUTOR+ access to the approval's project

    Raises:
        HTTPException 403: If user lacks respond permission
    """
    meta = entity.metadata or {}
    project_id = meta.get("project_id")
    created_by = meta.get("created_by")

    # Org admins can respond to any approval
    if ctx.org_role in (OrganizationRole.OWNER, OrganizationRole.ADMIN):
        return

    # Agent creator can respond to their approval
    if created_by and str(ctx.user.id) == created_by:
        return

    # Check project access (CONTRIBUTOR+ required for respond operations)
    if project_id:
        await verify_entity_project_access(
            session,
            ctx,
            project_id,
            required_role=ProjectRole.CONTRIBUTOR,
        )
        return

    # No project - only creator or admin can respond
    raise HTTPException(
        status_code=403,
        detail="You don't have permission to respond to this approval",
    )


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
    ctx: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session_dependency),
) -> ApprovalListResponse:
    """List approval requests for the organization.

    Args:
        status: Filter by approval status (pending, approved, denied, etc.)
        approval_type: Filter by type (destructive_command, question, etc.)
        agent_id: Filter by specific agent
        project_id: Filter by project
        limit: Maximum results

    Results are filtered to approvals the user can access (own agent approvals + projects with VIEWER+).
    """
    client = await get_graph_client()
    manager = EntityManager(client, group_id=str(ctx.organization.id))

    # Get accessible project IDs for permission filtering
    is_admin = ctx.org_role in (OrganizationRole.OWNER, OrganizationRole.ADMIN)
    accessible_projects: set[str] = set()
    if not is_admin:
        accessible_projects = await list_accessible_project_graph_ids(session, ctx) or set()

    user_id_str = str(ctx.user.id)

    # Get all approvals
    results = await manager.list_by_type(entity_type=EntityType.APPROVAL, limit=limit * 3)

    # Filter by access control first
    approvals: list = []
    for a in results:
        meta = a.metadata or {}
        approval_project_id = meta.get("project_id")
        created_by = meta.get("created_by")

        # Access control: skip approvals user cannot view (unless admin)
        if not is_admin:
            is_creator = created_by == user_id_str
            has_project_access = approval_project_id and approval_project_id in accessible_projects
            if not (is_creator or has_project_access):
                continue

        approvals.append(a)

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
    ctx: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session_dependency),
) -> ApprovalListResponse:
    """List pending approval requests - convenience endpoint for the queue."""
    return await list_approvals(
        status=ApprovalStatus.PENDING,
        project_id=project_id,
        limit=limit,
        ctx=ctx,
        session=session,
    )


@router.get("/{approval_id}", response_model=ApprovalResponse)
async def get_approval(
    approval_id: str,
    ctx: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session_dependency),
) -> ApprovalResponse:
    """Get a specific approval by ID."""
    client = await get_graph_client()
    manager = EntityManager(client, group_id=str(ctx.organization.id))

    entity = await manager.get(approval_id)
    if not entity or entity.entity_type != EntityType.APPROVAL:
        raise HTTPException(status_code=404, detail=f"Approval not found: {approval_id}")

    # Check view permission (creator, org admin, or project VIEWER+)
    await _check_approval_view_permission(ctx, session, entity)

    return _entity_to_approval_response(entity)


@router.delete("/{approval_id}", response_model=RespondToApprovalResponse)
async def dismiss_approval(
    approval_id: str,
    ctx: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session_dependency),
) -> RespondToApprovalResponse:
    """Dismiss/expire a stale approval request.

    Use this to clean up orphaned approvals from dead agents.
    Uses EntityManager.delete() which handles both Entity and Episodic nodes.
    """
    client = await get_graph_client()
    manager = EntityManager(client, group_id=str(ctx.organization.id))

    # Check permission before deleting
    entity = await manager.get(approval_id)
    if entity and entity.entity_type == EntityType.APPROVAL:
        # Check respond permission (creator, org admin, or project CONTRIBUTOR+)
        await _check_approval_respond_permission(ctx, session, entity)

    # Use EntityManager.delete() - handles both EntityNode and EpisodicNode
    try:
        deleted = await manager.delete(approval_id)
        if deleted:
            log.info("Approval deleted from graph", approval_id=approval_id)
        else:
            log.warning("Approval delete returned false", approval_id=approval_id)
    except Exception as e:
        # EntityNotFoundError or other errors - log but don't fail
        log.warning("Approval deletion failed", approval_id=approval_id, error=str(e))

    # Always return success - user wants it gone either way
    return RespondToApprovalResponse(
        success=True,
        approval_id=approval_id,
        action="dismiss",
        message="Approval dismissed successfully",
    )


@router.post("/{approval_id}/respond", response_model=RespondToApprovalResponse)
async def respond_to_approval(
    approval_id: str,
    request: RespondToApprovalRequest,
    ctx: AuthContext = Depends(get_auth_context),
    db_session: AsyncSession = Depends(get_session_dependency),
) -> RespondToApprovalResponse:
    """Respond to an approval request (approve, deny, or edit)."""
    client = await get_graph_client()
    manager = EntityManager(client, group_id=str(ctx.organization.id))

    entity = await manager.get(approval_id)
    if not entity or entity.entity_type != EntityType.APPROVAL:
        raise HTTPException(status_code=404, detail=f"Approval not found: {approval_id}")

    # Check respond permission (creator, org admin, or project CONTRIBUTOR+)
    await _check_approval_respond_permission(ctx, db_session, entity)

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
        "response_by": str(ctx.user.id),
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
            await db_session.execute(stmt)
            await db_session.commit()
        except Exception as e:
            log.warning("Failed to update approval message status", error=str(e))

    # Publish to worker channel - this wakes up the waiting agent
    from sibyl.agents.redis_sub import publish_approval_response

    await publish_approval_response(
        approval_id,
        {
            "approved": request.action == "approve",
            "action": request.action,
            "by": str(ctx.user.id),
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
            "response_by": str(ctx.user.id),
        },
        org_id=str(ctx.organization.id),
    )

    # Also broadcast agent status change to UI
    if agent_id:
        await publish_event(
            "agent_status",
            {"agent_id": agent_id, "status": "working"},
            org_id=str(ctx.organization.id),
        )

    log.info(
        "Approval responded",
        approval_id=approval_id,
        action=request.action,
        user_id=str(ctx.user.id),
    )

    return RespondToApprovalResponse(
        success=True,
        approval_id=approval_id,
        action=request.action,
        message=f"Approval {request.action}d successfully",
    )


# =============================================================================
# Question Endpoints (AskUserQuestion handling)
# =============================================================================


class QuestionAnswerRequest(BaseModel):
    """Request to answer an agent's question."""

    answers: dict[str, str]  # question_id -> selected answer or text


class QuestionAnswerResponse(BaseModel):
    """Response from answering a question."""

    success: bool
    question_id: str
    message: str


@router.post("/questions/{question_id}/answer", response_model=QuestionAnswerResponse)
async def answer_question(
    question_id: str,
    request: QuestionAnswerRequest,
    ctx: AuthContext = Depends(get_auth_context),
    db_session: AsyncSession = Depends(get_session_dependency),
) -> QuestionAnswerResponse:
    """Answer an agent's question.

    Called when user responds to an AskUserQuestion prompt in the agent chat.
    Publishes the answer via Redis to wake up the waiting agent hook.

    Authorization: Any org member can answer questions (enforced at router level).
    """
    # Update the stored AgentMessage's status
    try:
        stmt = (
            update(AgentMessage)
            .where(AgentMessage.extra["question_id"].astext == question_id)
            .values(
                extra=AgentMessage.extra.op("||")(
                    {
                        "status": "answered",
                        "answered_at": datetime.now(UTC).isoformat(),
                        "answers": request.answers,
                    }
                )
            )
        )
        await db_session.execute(stmt)
        await db_session.commit()
    except Exception as e:
        log.warning("Failed to update question message status", error=str(e))

    # Publish to worker channel - this wakes up the waiting agent
    from sibyl.agents.redis_sub import publish_question_response

    await publish_question_response(
        question_id,
        {
            "answers": request.answers,
            "by": str(ctx.user.id),
        },
    )

    # Broadcast event to UI
    from sibyl.api.pubsub import publish_event

    await publish_event(
        "question_answered",
        {
            "question_id": question_id,
            "answers": request.answers,
            "answered_by": str(ctx.user.id),
        },
        org_id=str(ctx.organization.id),
    )

    log.info(
        "Question answered",
        question_id=question_id,
        user_id=str(ctx.user.id),
    )

    return QuestionAnswerResponse(
        success=True,
        question_id=question_id,
        message="Question answered successfully",
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
