"""Planning Studio API routes.

REST API for multi-agent brainstorming and planning sessions.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from sibyl.api.decorators import log_operation
from sibyl.auth.context import AuthContext
from sibyl.auth.dependencies import require_org_role
from sibyl.auth.rls import AuthSession, get_auth_session
from sibyl.db.models import (
    BrainstormThreadStatus,
    Organization,
    OrganizationRole,
    PlanningPhase,
    ProjectRole,
)
from sibyl.planning.service import PlanningSessionService

if TYPE_CHECKING:
    from sibyl.db.models import BrainstormThread, PlanningSession

log = structlog.get_logger()


def _require_org(ctx: AuthContext) -> Organization:
    """Require organization context for multi-tenant routes."""
    if not ctx.organization:
        raise HTTPException(status_code=403, detail="Organization context required")
    return ctx.organization


_WRITE_ROLES = (
    OrganizationRole.OWNER,
    OrganizationRole.ADMIN,
    OrganizationRole.MEMBER,
)

router = APIRouter(
    prefix="/planning",
    tags=["planning"],
    dependencies=[Depends(require_org_role(*_WRITE_ROLES))],
)


# =============================================================================
# Request/Response Models
# =============================================================================


class PersonaSchema(BaseModel):
    """Generated persona for brainstorming."""

    role: str = Field(..., description="Persona role/archetype")
    name: str | None = Field(None, description="Display name")
    focus: str | None = Field(None, description="What this persona focuses on")
    system_prompt: str | None = Field(None, description="Full system prompt")


class TaskDraftSchema(BaseModel):
    """Draft task for materialization."""

    title: str
    description: str | None = None
    priority: str = "medium"
    tags: list[str] = []
    depends_on: list[str] = []  # Indices of tasks this depends on


class CreateSessionRequest(BaseModel):
    """Request to create a planning session."""

    prompt: str = Field(..., min_length=10, description="Brainstorming prompt")
    title: str | None = Field(None, max_length=255, description="Optional title")
    project_id: str | None = Field(None, description="Project to scope session")


class SessionResponse(BaseModel):
    """Planning session response."""

    id: str
    org_id: str
    project_id: str | None
    created_by: str
    title: str | None
    prompt: str
    phase: str
    personas: list[PersonaSchema] | None = None
    synthesis: str | None = None
    spec_draft: str | None = None
    task_drafts: list[TaskDraftSchema] | None = None
    materialized_at: str | None = None
    epic_id: str | None = None
    task_ids: list[str] | None = None
    document_id: str | None = None
    episode_id: str | None = None
    created_at: str
    updated_at: str

    @classmethod
    def from_model(cls, session: PlanningSession) -> SessionResponse:
        """Convert database model to response."""
        return cls(
            id=str(session.id),
            org_id=str(session.org_id),
            project_id=str(session.project_id) if session.project_id else None,
            created_by=str(session.created_by),
            title=session.title,
            prompt=session.prompt,
            phase=session.phase.value if session.phase else "created",
            personas=[PersonaSchema(**p) for p in session.personas] if session.personas else None,
            synthesis=session.synthesis,
            spec_draft=session.spec_draft,
            task_drafts=(
                [TaskDraftSchema(**t) for t in session.task_drafts] if session.task_drafts else None
            ),
            materialized_at=session.materialized_at.isoformat() if session.materialized_at else None,
            epic_id=session.epic_id,
            task_ids=session.task_ids,
            document_id=session.document_id,
            episode_id=session.episode_id,
            created_at=session.created_at.isoformat() if session.created_at else "",
            updated_at=session.updated_at.isoformat() if session.updated_at else "",
        )


class SessionListResponse(BaseModel):
    """List of planning sessions."""

    sessions: list[SessionResponse]
    total: int


class ThreadResponse(BaseModel):
    """Brainstorm thread response."""

    id: str
    session_id: str
    persona_role: str
    persona_name: str | None
    persona_focus: str | None
    agent_id: str | None
    status: str
    started_at: str | None
    completed_at: str | None
    created_at: str

    @classmethod
    def from_model(cls, thread: BrainstormThread) -> ThreadResponse:
        """Convert database model to response."""
        return cls(
            id=str(thread.id),
            session_id=str(thread.session_id),
            persona_role=thread.persona_role,
            persona_name=thread.persona_name,
            persona_focus=thread.persona_focus,
            agent_id=thread.agent_id,
            status=thread.status.value if thread.status else "pending",
            started_at=thread.started_at.isoformat() if thread.started_at else None,
            completed_at=thread.completed_at.isoformat() if thread.completed_at else None,
            created_at=thread.created_at.isoformat() if thread.created_at else "",
        )


class MessageResponse(BaseModel):
    """Brainstorm message response."""

    id: str
    thread_id: str
    role: str
    content: str
    thinking: str | None
    created_at: str


class ThreadWithMessagesResponse(BaseModel):
    """Thread with its messages."""

    thread: ThreadResponse
    messages: list[MessageResponse]


class UpdateSessionRequest(BaseModel):
    """Request to update a planning session."""

    title: str | None = None
    synthesis: str | None = None
    spec_draft: str | None = None
    task_drafts: list[TaskDraftSchema] | None = None


class StartBrainstormingRequest(BaseModel):
    """Request to start brainstorming with generated personas."""

    personas: list[PersonaSchema] = Field(..., min_length=2, max_length=6)


class StartBrainstormingResponse(BaseModel):
    """Response from starting brainstorming."""

    session: SessionResponse
    threads: list[ThreadResponse]


class PhaseTransitionResponse(BaseModel):
    """Response from phase transition."""

    success: bool
    session_id: str
    previous_phase: str
    current_phase: str
    message: str


# =============================================================================
# Session Endpoints
# =============================================================================


@router.post("", response_model=SessionResponse, status_code=201)
@log_operation("create_planning_session")
async def create_session(
    request: CreateSessionRequest,
    auth: AuthSession = Depends(get_auth_session),
) -> SessionResponse:
    """Create a new planning session.

    Starts in 'created' phase. Use /start-brainstorming to begin.
    """
    ctx = auth.ctx
    org = _require_org(ctx)

    # Validate project access if specified
    project_uuid: UUID | None = None
    if request.project_id:
        from sibyl.auth.authorization import verify_entity_project_access

        await verify_entity_project_access(
            auth.session, ctx, request.project_id, required_role=ProjectRole.CONTRIBUTOR
        )
        project_uuid = UUID(request.project_id)

    service = PlanningSessionService(auth.session)
    session = await service.create_session(
        org_id=org.id,
        created_by=ctx.user.id,
        prompt=request.prompt,
        title=request.title,
        project_id=project_uuid,
    )

    # Broadcast session created event
    from sibyl.api.pubsub import publish_event

    await publish_event(
        "planning_session_created",
        {
            "session_id": str(session.id),
            "title": session.title,
            "phase": session.phase.value if session.phase else "created",
            "project_id": str(session.project_id) if session.project_id else None,
        },
        org_id=str(org.id),
    )

    return SessionResponse.from_model(session)


@router.get("", response_model=SessionListResponse)
async def list_sessions(
    project_id: str | None = Query(None, description="Filter by project"),
    phase: str | None = Query(None, description="Filter by phase"),
    include_archived: bool = Query(False, description="Include archived sessions"),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    auth: AuthSession = Depends(get_auth_session),
) -> SessionListResponse:
    """List planning sessions for the organization."""
    ctx = auth.ctx
    org = _require_org(ctx)

    phase_enum = PlanningPhase(phase) if phase else None
    project_uuid = UUID(project_id) if project_id else None

    service = PlanningSessionService(auth.session)
    sessions = await service.list_sessions(
        org_id=org.id,
        project_id=project_uuid,
        phase=phase_enum,
        include_archived=include_archived,
        limit=limit,
        offset=offset,
    )

    return SessionListResponse(
        sessions=[SessionResponse.from_model(s) for s in sessions],
        total=len(sessions),
    )


@router.get("/{session_id}", response_model=SessionResponse)
async def get_session(
    session_id: str,
    auth: AuthSession = Depends(get_auth_session),
) -> SessionResponse:
    """Get a planning session by ID."""
    ctx = auth.ctx
    org = _require_org(ctx)

    service = PlanningSessionService(auth.session)
    session = await service.get_session(
        UUID(session_id),
        org.id,
        include_threads=False,
    )

    if not session:
        raise HTTPException(status_code=404, detail=f"Session not found: {session_id}")

    return SessionResponse.from_model(session)


@router.patch("/{session_id}", response_model=SessionResponse)
@log_operation("update_planning_session")
async def update_session(
    session_id: str,
    request: UpdateSessionRequest,
    auth: AuthSession = Depends(get_auth_session),
) -> SessionResponse:
    """Update a planning session."""
    ctx = auth.ctx
    org = _require_org(ctx)

    service = PlanningSessionService(auth.session)
    session = await service.update_session(
        UUID(session_id),
        org.id,
        title=request.title,
        synthesis=request.synthesis,
        spec_draft=request.spec_draft,
        task_drafts=[t.model_dump() for t in request.task_drafts] if request.task_drafts else None,
    )

    if not session:
        raise HTTPException(status_code=404, detail=f"Session not found: {session_id}")

    return SessionResponse.from_model(session)


@router.delete("/{session_id}", status_code=204)
@log_operation("delete_planning_session")
async def delete_session(
    session_id: str,
    auth: AuthSession = Depends(get_auth_session),
) -> None:
    """Delete a planning session (hard delete)."""
    ctx = auth.ctx
    org = _require_org(ctx)

    service = PlanningSessionService(auth.session)
    deleted = await service.delete_session(UUID(session_id), org.id)

    if not deleted:
        raise HTTPException(status_code=404, detail=f"Session not found: {session_id}")


@router.post("/{session_id}/discard", response_model=PhaseTransitionResponse)
@log_operation("discard_planning_session")
async def discard_session(
    session_id: str,
    auth: AuthSession = Depends(get_auth_session),
) -> PhaseTransitionResponse:
    """Discard a planning session (soft delete)."""
    ctx = auth.ctx
    org = _require_org(ctx)

    service = PlanningSessionService(auth.session)
    session = await service.get_session(UUID(session_id), org.id)

    if not session:
        raise HTTPException(status_code=404, detail=f"Session not found: {session_id}")

    previous_phase = session.phase.value if session.phase else "created"

    updated = await service.discard_session(UUID(session_id), org.id)
    if not updated:
        raise HTTPException(status_code=500, detail="Failed to discard session")

    return PhaseTransitionResponse(
        success=True,
        session_id=session_id,
        previous_phase=previous_phase,
        current_phase="discarded",
        message="Session discarded",
    )


# =============================================================================
# Brainstorming Endpoints
# =============================================================================


class RunBrainstormingRequest(BaseModel):
    """Request to run brainstorming.

    Default: Single agent brainstorms directly on the topic.
    Optional: Set persona_count > 0 to auto-generate diverse personas,
              or provide custom personas list.
    """

    personas: list[PersonaSchema] | None = Field(
        None, description="Custom personas (optional - for multi-perspective mode)"
    )
    persona_count: int | None = Field(
        None, ge=2, le=6, description="Generate N personas (optional - default is single agent)"
    )


class RunBrainstormingResponse(BaseModel):
    """Response from running brainstorming."""

    session: SessionResponse
    threads: list[ThreadResponse]
    job_id: str


@router.post("/{session_id}/run-brainstorming", response_model=RunBrainstormingResponse)
@log_operation("run_brainstorming")
async def run_brainstorming(
    session_id: str,
    request: RunBrainstormingRequest,
    auth: AuthSession = Depends(get_auth_session),
) -> RunBrainstormingResponse:
    """Run brainstorming - generates personas and executes agents.

    This endpoint:
    1. Generates personas using Claude (if not provided)
    2. Creates threads for each persona
    3. Enqueues background job to run persona agents in parallel

    Clients should subscribe to WebSocket for real-time updates.
    """
    ctx = auth.ctx
    org = _require_org(ctx)

    service = PlanningSessionService(auth.session)

    # Validate session exists and is in correct phase
    session = await service.get_session(UUID(session_id), org.id)
    if not session:
        raise HTTPException(status_code=404, detail=f"Session not found: {session_id}")

    if session.phase != PlanningPhase.created:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot start brainstorming from {session.phase.value} phase",
        )

    # Determine brainstorming mode
    if request.personas:
        # Custom personas provided
        personas_data = [p.model_dump() for p in request.personas]
    elif request.persona_count:
        # Auto-generate N personas
        from sibyl.planning.personas import generate_personas

        personas_data = await generate_personas(session.prompt, count=request.persona_count)
    else:
        # Default: single agent mode (no personas)
        from sibyl.planning.personas import single_brainstormer

        personas_data = [single_brainstormer()]

    # Start brainstorming (creates threads, updates phase)
    updated_session = await service.start_brainstorming(UUID(session_id), org.id, personas_data)

    if not updated_session:
        raise HTTPException(status_code=500, detail="Failed to start brainstorming")

    # Get created threads
    threads = await service.list_threads(UUID(session_id))

    # Enqueue background job to run the agents
    from sibyl.jobs import enqueue_brainstorming

    job_id = await enqueue_brainstorming(session_id, str(org.id), round_number=1)

    # Broadcast brainstorming started event
    from sibyl.api.pubsub import publish_event

    await publish_event(
        "planning_brainstorm_started",
        {
            "session_id": session_id,
            "phase": "brainstorming",
            "thread_count": len(threads),
            "thread_ids": [str(t.id) for t in threads],
            "job_id": job_id,
        },
        org_id=str(org.id),
    )

    return RunBrainstormingResponse(
        session=SessionResponse.from_model(updated_session),
        threads=[ThreadResponse.from_model(t) for t in threads],
        job_id=job_id,
    )


@router.post("/{session_id}/start-brainstorming", response_model=StartBrainstormingResponse)
@log_operation("start_brainstorming")
async def start_brainstorming(
    session_id: str,
    request: StartBrainstormingRequest,
    auth: AuthSession = Depends(get_auth_session),
) -> StartBrainstormingResponse:
    """Start brainstorming with the provided personas (manual mode).

    Creates threads for each persona and transitions to 'brainstorming' phase.
    Does NOT automatically run agents - use /run-brainstorming for that.
    """
    ctx = auth.ctx
    org = _require_org(ctx)

    service = PlanningSessionService(auth.session)

    # Validate session exists and is in correct phase
    session = await service.get_session(UUID(session_id), org.id)
    if not session:
        raise HTTPException(status_code=404, detail=f"Session not found: {session_id}")

    if session.phase != PlanningPhase.created:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot start brainstorming from {session.phase.value} phase",
        )

    # Convert personas to dicts and start brainstorming
    personas_data = [p.model_dump() for p in request.personas]
    updated_session = await service.start_brainstorming(UUID(session_id), org.id, personas_data)

    if not updated_session:
        raise HTTPException(status_code=500, detail="Failed to start brainstorming")

    # Get created threads
    threads = await service.list_threads(UUID(session_id))

    # Broadcast brainstorming started event
    from sibyl.api.pubsub import publish_event

    await publish_event(
        "planning_brainstorm_started",
        {
            "session_id": session_id,
            "phase": "brainstorming",
            "thread_count": len(threads),
            "thread_ids": [str(t.id) for t in threads],
        },
        org_id=str(org.id),
    )

    return StartBrainstormingResponse(
        session=SessionResponse.from_model(updated_session),
        threads=[ThreadResponse.from_model(t) for t in threads],
    )


@router.get("/{session_id}/threads", response_model=list[ThreadResponse])
async def list_threads(
    session_id: str,
    status: str | None = Query(None, description="Filter by status"),
    auth: AuthSession = Depends(get_auth_session),
) -> list[ThreadResponse]:
    """List brainstorm threads for a session."""
    ctx = auth.ctx
    org = _require_org(ctx)

    service = PlanningSessionService(auth.session)

    # Verify session access
    session = await service.get_session(UUID(session_id), org.id)
    if not session:
        raise HTTPException(status_code=404, detail=f"Session not found: {session_id}")

    status_enum = BrainstormThreadStatus(status) if status else None
    threads = await service.list_threads(UUID(session_id), status=status_enum)

    return [ThreadResponse.from_model(t) for t in threads]


@router.get("/{session_id}/threads/{thread_id}", response_model=ThreadWithMessagesResponse)
async def get_thread_with_messages(
    session_id: str,
    thread_id: str,
    auth: AuthSession = Depends(get_auth_session),
) -> ThreadWithMessagesResponse:
    """Get a thread with its messages."""
    ctx = auth.ctx
    org = _require_org(ctx)

    service = PlanningSessionService(auth.session)

    # Verify session access
    session = await service.get_session(UUID(session_id), org.id)
    if not session:
        raise HTTPException(status_code=404, detail=f"Session not found: {session_id}")

    thread = await service.get_thread(UUID(thread_id), include_messages=True)
    if not thread:
        raise HTTPException(status_code=404, detail=f"Thread not found: {thread_id}")

    # Verify thread belongs to session
    if str(thread.session_id) != session_id:
        raise HTTPException(status_code=404, detail=f"Thread not found in session: {thread_id}")

    messages = await service.list_messages(UUID(thread_id))

    return ThreadWithMessagesResponse(
        thread=ThreadResponse.from_model(thread),
        messages=[
            MessageResponse(
                id=str(m.id),
                thread_id=str(m.thread_id),
                role=m.role,
                content=m.content,
                thinking=m.thinking,
                created_at=m.created_at.isoformat() if m.created_at else "",
            )
            for m in messages
        ],
    )


@router.post("/{session_id}/threads/{thread_id}/messages", response_model=MessageResponse)
async def add_message(
    session_id: str,
    thread_id: str,
    role: str = Query(..., description="Message role (user, assistant, system)"),
    content: str = Query(..., description="Message content"),
    thinking: str | None = Query(None, description="Optional thinking trace"),
    auth: AuthSession = Depends(get_auth_session),
) -> MessageResponse:
    """Add a message to a thread."""
    ctx = auth.ctx
    org = _require_org(ctx)

    service = PlanningSessionService(auth.session)

    # Verify session access
    session = await service.get_session(UUID(session_id), org.id)
    if not session:
        raise HTTPException(status_code=404, detail=f"Session not found: {session_id}")

    # Verify thread exists and belongs to session
    thread = await service.get_thread(UUID(thread_id))
    if not thread or str(thread.session_id) != session_id:
        raise HTTPException(status_code=404, detail=f"Thread not found: {thread_id}")

    message = await service.add_message(
        thread_id=UUID(thread_id),
        role=role,
        content=content,
        thinking=thinking,
    )

    return MessageResponse(
        id=str(message.id),
        thread_id=str(message.thread_id),
        role=message.role,
        content=message.content,
        thinking=message.thinking,
        created_at=message.created_at.isoformat() if message.created_at else "",
    )


@router.post("/{session_id}/threads/{thread_id}/start", response_model=ThreadResponse)
@log_operation("start_thread")
async def start_thread(
    session_id: str,
    thread_id: str,
    agent_id: str | None = Query(None, description="Agent ID executing this thread"),
    auth: AuthSession = Depends(get_auth_session),
) -> ThreadResponse:
    """Mark a thread as running."""
    ctx = auth.ctx
    org = _require_org(ctx)

    service = PlanningSessionService(auth.session)

    # Verify session access
    session = await service.get_session(UUID(session_id), org.id)
    if not session:
        raise HTTPException(status_code=404, detail=f"Session not found: {session_id}")

    thread = await service.update_thread_status(
        UUID(thread_id),
        BrainstormThreadStatus.running,
        agent_id=agent_id,
    )

    if not thread:
        raise HTTPException(status_code=404, detail=f"Thread not found: {thread_id}")

    return ThreadResponse.from_model(thread)


@router.post("/{session_id}/threads/{thread_id}/complete", response_model=ThreadResponse)
@log_operation("complete_thread")
async def complete_thread(
    session_id: str,
    thread_id: str,
    auth: AuthSession = Depends(get_auth_session),
) -> ThreadResponse:
    """Mark a thread as completed."""
    ctx = auth.ctx
    org = _require_org(ctx)

    service = PlanningSessionService(auth.session)

    # Verify session access
    session = await service.get_session(UUID(session_id), org.id)
    if not session:
        raise HTTPException(status_code=404, detail=f"Session not found: {session_id}")

    thread = await service.update_thread_status(
        UUID(thread_id),
        BrainstormThreadStatus.completed,
    )

    if not thread:
        raise HTTPException(status_code=404, detail=f"Thread not found: {thread_id}")

    # Check if all threads complete and transition session
    updated_session = await service.complete_brainstorming(UUID(session_id), org.id)

    # Broadcast thread completed event
    from sibyl.api.pubsub import publish_event

    await publish_event(
        "planning_thread_updated",
        {
            "session_id": session_id,
            "thread_id": thread_id,
            "status": "completed",
            "session_phase": updated_session.phase.value if updated_session else None,
        },
        org_id=str(org.id),
    )

    return ThreadResponse.from_model(thread)


@router.post("/{session_id}/threads/{thread_id}/fail", response_model=ThreadResponse)
@log_operation("fail_thread")
async def fail_thread(
    session_id: str,
    thread_id: str,
    auth: AuthSession = Depends(get_auth_session),
) -> ThreadResponse:
    """Mark a thread as failed."""
    ctx = auth.ctx
    org = _require_org(ctx)

    service = PlanningSessionService(auth.session)

    # Verify session access
    session = await service.get_session(UUID(session_id), org.id)
    if not session:
        raise HTTPException(status_code=404, detail=f"Session not found: {session_id}")

    thread = await service.update_thread_status(
        UUID(thread_id),
        BrainstormThreadStatus.failed,
    )

    if not thread:
        raise HTTPException(status_code=404, detail=f"Thread not found: {thread_id}")

    # Check if all threads complete (including failures) and transition session
    await service.complete_brainstorming(UUID(session_id), org.id)

    return ThreadResponse.from_model(thread)


# =============================================================================
# Synthesis & Spec Endpoints
# =============================================================================


class RunSynthesisResponse(BaseModel):
    """Response from running synthesis."""

    job_id: str
    session_id: str
    message: str


@router.post("/{session_id}/run-synthesis", response_model=RunSynthesisResponse)
@log_operation("run_synthesis")
async def run_synthesis(
    session_id: str,
    auth: AuthSession = Depends(get_auth_session),
) -> RunSynthesisResponse:
    """Run synthesis on brainstorm outputs.

    Enqueues a background job to synthesize all persona outputs
    into a cohesive summary. Listen to WebSocket for completion.
    """
    ctx = auth.ctx
    org = _require_org(ctx)

    service = PlanningSessionService(auth.session)

    session = await service.get_session(UUID(session_id), org.id)
    if not session:
        raise HTTPException(status_code=404, detail=f"Session not found: {session_id}")

    if session.phase != PlanningPhase.synthesizing:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot run synthesis in {session.phase.value} phase (must be 'synthesizing')",
        )

    # Enqueue synthesis job
    from sibyl.jobs import enqueue_synthesis

    job_id = await enqueue_synthesis(session_id, str(org.id))

    return RunSynthesisResponse(
        job_id=job_id,
        session_id=session_id,
        message="Synthesis job enqueued",
    )


class SynthesisRequest(BaseModel):
    """Request to save synthesis results."""

    synthesis: str = Field(..., description="Synthesized brainstorm content")


class SpecDraftRequest(BaseModel):
    """Request to save spec draft."""

    spec_draft: str = Field(..., description="Draft specification")
    task_drafts: list[TaskDraftSchema] = Field(
        default_factory=list, description="Draft tasks"
    )


@router.post("/{session_id}/synthesis", response_model=SessionResponse)
@log_operation("save_synthesis")
async def save_synthesis(
    session_id: str,
    request: SynthesisRequest,
    auth: AuthSession = Depends(get_auth_session),
) -> SessionResponse:
    """Save synthesis and transition to drafting phase (manual mode)."""
    ctx = auth.ctx
    org = _require_org(ctx)

    service = PlanningSessionService(auth.session)

    session = await service.get_session(UUID(session_id), org.id)
    if not session:
        raise HTTPException(status_code=404, detail=f"Session not found: {session_id}")

    if session.phase != PlanningPhase.synthesizing:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot save synthesis in {session.phase.value} phase",
        )

    updated = await service.update_session(
        UUID(session_id),
        org.id,
        synthesis=request.synthesis,
        phase=PlanningPhase.drafting,
    )

    if not updated:
        raise HTTPException(status_code=500, detail="Failed to save synthesis")

    return SessionResponse.from_model(updated)


@router.post("/{session_id}/spec", response_model=SessionResponse)
@log_operation("save_spec_draft")
async def save_spec_draft(
    session_id: str,
    request: SpecDraftRequest,
    auth: AuthSession = Depends(get_auth_session),
) -> SessionResponse:
    """Save spec draft and tasks, transition to ready phase."""
    ctx = auth.ctx
    org = _require_org(ctx)

    service = PlanningSessionService(auth.session)

    session = await service.get_session(UUID(session_id), org.id)
    if not session:
        raise HTTPException(status_code=404, detail=f"Session not found: {session_id}")

    if session.phase != PlanningPhase.drafting:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot save spec in {session.phase.value} phase",
        )

    updated = await service.update_session(
        UUID(session_id),
        org.id,
        spec_draft=request.spec_draft,
        task_drafts=[t.model_dump() for t in request.task_drafts],
        phase=PlanningPhase.ready,
    )

    if not updated:
        raise HTTPException(status_code=500, detail="Failed to save spec")

    return SessionResponse.from_model(updated)


# =============================================================================
# Materialization Endpoints
# =============================================================================


class MaterializeRequest(BaseModel):
    """Request to materialize a planning session."""

    project_id: str | None = Field(None, description="Project to assign entities to")
    epic_title: str | None = Field(None, max_length=255, description="Override epic title")
    epic_priority: str = Field("medium", description="Epic priority (low/medium/high/critical)")


class MaterializeResponse(BaseModel):
    """Response from materialization."""

    job_id: str
    session_id: str
    message: str


@router.post("/{session_id}/materialize", response_model=MaterializeResponse)
@log_operation("materialize_planning_session")
async def materialize_session(
    session_id: str,
    request: MaterializeRequest,
    auth: AuthSession = Depends(get_auth_session),
) -> MaterializeResponse:
    """Materialize a planning session into Sibyl entities.

    Creates:
    - Epic for the overall feature/initiative
    - Tasks from task_drafts, linked to the epic
    - Document with the spec_draft
    - Episode with the synthesis

    The session must be in the 'ready' phase.
    Listen to WebSocket for completion events.
    """
    ctx = auth.ctx
    org = _require_org(ctx)

    service = PlanningSessionService(auth.session)

    session = await service.get_session(UUID(session_id), org.id)
    if not session:
        raise HTTPException(status_code=404, detail=f"Session not found: {session_id}")

    if session.phase != PlanningPhase.ready:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot materialize in {session.phase.value} phase (must be 'ready')",
        )

    # Validate project access if specified
    project_id_str: str | None = None
    if request.project_id:
        from sibyl.auth.authorization import verify_entity_project_access

        await verify_entity_project_access(
            auth.session, ctx, request.project_id, required_role=ProjectRole.CONTRIBUTOR
        )
        project_id_str = request.project_id
    elif session.project_id:
        project_id_str = str(session.project_id)

    # Enqueue materialization job
    from sibyl.jobs import enqueue_materialization

    job_id = await enqueue_materialization(
        session_id,
        str(org.id),
        project_id=project_id_str,
        epic_title=request.epic_title,
        epic_priority=request.epic_priority,
    )

    return MaterializeResponse(
        job_id=job_id,
        session_id=session_id,
        message="Materialization job enqueued",
    )
