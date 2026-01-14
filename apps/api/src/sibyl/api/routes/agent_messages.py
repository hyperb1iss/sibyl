"""REST API endpoints for inter-agent messaging.

Provides HTTP endpoints for the inter-agent message bus:
- Send messages between agents
- Retrieve pending messages
- Respond to queries
- Mark messages as read
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlmodel import col, select

from sibyl.agents.message_bus import (
    MessageBus,
    format_message_for_injection,
    format_messages_digest,
)
from sibyl.auth.rls import AuthSession, get_auth_session
from sibyl.db import InterAgentMessage, InterAgentMessageType

router = APIRouter(prefix="/agent-messages", tags=["Agent Messages"])


# =============================================================================
# Request/Response Models
# =============================================================================


class SendMessageRequest(BaseModel):
    """Request to send an inter-agent message."""

    from_agent_id: str = Field(..., description="Sender agent ID")
    to_agent_id: str | None = Field(None, description="Target agent (None = orchestrator)")
    message_type: str = Field(..., description="Type of message (progress, query, etc.)")
    subject: str = Field(..., max_length=255, description="Short subject line")
    content: str = Field(..., description="Full message content")
    requires_response: bool = Field(False, description="True if sender waits for response")
    priority: int = Field(0, ge=0, le=10, description="0=normal, 10=critical")
    context: dict[str, Any] | None = Field(None, description="Additional context")


class RespondRequest(BaseModel):
    """Request to respond to a message."""

    from_agent_id: str = Field(..., description="Agent sending response")
    content: str = Field(..., description="Response content")
    context: dict[str, Any] | None = Field(None, description="Additional context")


class MessageResponse(BaseModel):
    """Response containing message details."""

    id: str
    organization_id: str
    from_agent_id: str
    to_agent_id: str | None
    message_type: str
    subject: str
    content: str
    response_to_id: str | None
    requires_response: bool
    priority: int
    context: dict[str, Any]
    read_at: datetime | None
    responded_at: datetime | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_model(cls, msg: InterAgentMessage) -> "MessageResponse":
        """Create response from database model."""
        return cls(
            id=str(msg.id),
            organization_id=str(msg.organization_id),
            from_agent_id=msg.from_agent_id,
            to_agent_id=msg.to_agent_id,
            message_type=msg.message_type,
            subject=msg.subject,
            content=msg.content,
            response_to_id=str(msg.response_to_id) if msg.response_to_id else None,
            requires_response=msg.requires_response,
            priority=msg.priority,
            context=msg.context,
            read_at=msg.read_at,
            responded_at=msg.responded_at,
            created_at=msg.created_at,
            updated_at=msg.updated_at,
        )


class MessagesListResponse(BaseModel):
    """Response containing list of messages."""

    messages: list[MessageResponse]
    count: int


class FormattedMessageResponse(BaseModel):
    """Response with formatted message for injection."""

    message: MessageResponse
    formatted: str = Field(..., description="Formatted for Claude Code injection")


class MessagesDigestResponse(BaseModel):
    """Response with formatted digest of messages."""

    messages: list[MessageResponse]
    count: int
    digest: str = Field(..., description="Formatted digest for injection")


# =============================================================================
# Endpoints
# =============================================================================


@router.post("", response_model=MessageResponse)
async def send_message(
    request: SendMessageRequest,
    auth: AuthSession = Depends(get_auth_session),
) -> MessageResponse:
    """Send an inter-agent message.

    Use this endpoint to send messages between agents for:
    - Progress updates (broadcast to orchestrator)
    - Queries (ask another agent for information)
    - Review requests (request code review)
    - Blockers (report being blocked)
    - Delegations (delegate work to another agent)
    """
    org = auth.ctx.organization
    if not org:
        raise HTTPException(status_code=400, detail="No organization context")

    # Validate message type
    try:
        msg_type = InterAgentMessageType(request.message_type)
    except ValueError:
        valid_types = [t.value for t in InterAgentMessageType]
        raise HTTPException(
            status_code=400,
            detail=f"Invalid message_type. Must be one of: {valid_types}",
        ) from None

    bus = MessageBus(auth.session, org.id)
    message = await bus.send(
        from_agent_id=request.from_agent_id,
        message_type=msg_type,
        subject=request.subject,
        content=request.content,
        to_agent_id=request.to_agent_id,
        requires_response=request.requires_response,
        priority=request.priority,
        context=request.context,
    )

    return MessageResponse.from_model(message)


@router.get("/pending/{agent_id}", response_model=MessagesListResponse)
async def get_pending_messages(
    agent_id: str,
    limit: int = 50,
    include_read: bool = False,
    auth: AuthSession = Depends(get_auth_session),
) -> MessagesListResponse:
    """Get pending messages for an agent.

    Returns messages addressed to the specified agent, ordered by
    priority (desc) and created_at (asc).
    """
    org = auth.ctx.organization
    if not org:
        raise HTTPException(status_code=400, detail="No organization context")

    bus = MessageBus(auth.session, org.id)
    messages = await bus.get_pending(agent_id, limit=limit, include_read=include_read)

    return MessagesListResponse(
        messages=[MessageResponse.from_model(m) for m in messages],
        count=len(messages),
    )


@router.get("/pending/{agent_id}/digest", response_model=MessagesDigestResponse)
async def get_pending_digest(
    agent_id: str,
    limit: int = 50,
    auth: AuthSession = Depends(get_auth_session),
) -> MessagesDigestResponse:
    """Get pending messages formatted as digest for Claude Code injection.

    Returns both the raw messages and a pre-formatted digest string
    suitable for injecting into a Claude Code session.
    """
    org = auth.ctx.organization
    if not org:
        raise HTTPException(status_code=400, detail="No organization context")

    bus = MessageBus(auth.session, org.id)
    messages = await bus.get_pending(agent_id, limit=limit)

    return MessagesDigestResponse(
        messages=[MessageResponse.from_model(m) for m in messages],
        count=len(messages),
        digest=format_messages_digest(messages),
    )


@router.post("/{message_id}/respond", response_model=MessageResponse)
async def respond_to_message(
    message_id: UUID,
    request: RespondRequest,
    auth: AuthSession = Depends(get_auth_session),
) -> MessageResponse:
    """Respond to a message.

    Creates a response message linked to the original. Use this for
    answering queries, providing review results, etc.
    """
    org = auth.ctx.organization
    if not org:
        raise HTTPException(status_code=400, detail="No organization context")

    bus = MessageBus(auth.session, org.id)

    try:
        response = await bus.respond(
            original_message_id=message_id,
            content=request.content,
            from_agent_id=request.from_agent_id,
            context=request.context,
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from None

    return MessageResponse.from_model(response)


@router.post("/{message_id}/read")
async def mark_message_read(
    message_id: UUID,
    auth: AuthSession = Depends(get_auth_session),
) -> dict[str, str]:
    """Mark a message as read.

    Updates the read_at timestamp for the message.
    """
    org = auth.ctx.organization
    if not org:
        raise HTTPException(status_code=400, detail="No organization context")

    bus = MessageBus(auth.session, org.id)
    await bus.mark_read(message_id)

    return {"status": "ok", "message_id": str(message_id)}


@router.get("/{message_id}", response_model=FormattedMessageResponse)
async def get_message(
    message_id: UUID,
    auth: AuthSession = Depends(get_auth_session),
) -> FormattedMessageResponse:
    """Get a specific message with formatted injection text.

    Returns the message details along with pre-formatted text
    suitable for injecting into a Claude Code session.
    """
    org = auth.ctx.organization
    if not org:
        raise HTTPException(status_code=400, detail="No organization context")

    result = await auth.session.execute(
        select(InterAgentMessage)
        .where(col(InterAgentMessage.id) == message_id)
        .where(col(InterAgentMessage.organization_id) == org.id)
    )
    message = result.scalar_one_or_none()

    if not message:
        raise HTTPException(status_code=404, detail="Message not found")

    return FormattedMessageResponse(
        message=MessageResponse.from_model(message),
        formatted=format_message_for_injection(message),
    )


@router.get("/conversation/{agent_id}/{other_agent_id}", response_model=MessagesListResponse)
async def get_conversation(
    agent_id: str,
    other_agent_id: str,
    limit: int = 100,
    auth: AuthSession = Depends(get_auth_session),
) -> MessagesListResponse:
    """Get conversation history between two agents.

    Returns messages exchanged between the two agents,
    ordered by created_at ascending.
    """
    org = auth.ctx.organization
    if not org:
        raise HTTPException(status_code=400, detail="No organization context")

    bus = MessageBus(auth.session, org.id)
    messages = await bus.get_conversation(agent_id, other_agent_id, limit=limit)

    return MessagesListResponse(
        messages=[MessageResponse.from_model(m) for m in messages],
        count=len(messages),
    )
