"""Inter-agent message bus for distributed agent communication.

Enables agents to communicate with each other during distributed execution:
- Progress updates (broadcast to orchestrator/UI)
- Query/response (blocking asks between agents)
- Review requests (code review workflow)
- Blockers and delegations

Architecture:
- Persistence: Postgres InterAgentMessage table for audit and async pickup
- Real-time: Redis pub/sub for instant delivery (reuses existing pubsub.py)
- Blocking: Async futures with polling for query/response pattern
"""

import asyncio
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col, select

from sibyl.db import InterAgentMessage, InterAgentMessageType

log = structlog.get_logger()

# Timeout for blocking queries (seconds)
DEFAULT_QUERY_TIMEOUT = 60.0
POLL_INTERVAL = 0.5


class MessageBus:
    """Inter-agent message bus for distributed communication.

    Handles message routing, persistence, and delivery between agents.
    Uses Postgres for persistence and Redis pub/sub for real-time delivery.

    Usage:
        bus = MessageBus(session, org_id)

        # Send a progress update (non-blocking)
        await bus.send_progress(from_agent, "Completed step 1", progress=25)

        # Send a blocking query
        response = await bus.query(
            from_agent, to_agent,
            subject="Need clarification",
            content="How should I handle the edge case?",
        )

        # Respond to a query
        await bus.respond(message_id, "Use the fallback handler")
    """

    def __init__(self, session: AsyncSession, org_id: UUID) -> None:
        """Initialize the message bus.

        Args:
            session: SQLAlchemy async session for persistence
            org_id: Organization context for multi-tenancy
        """
        self.session = session
        self.org_id = org_id

    # =========================================================================
    # Core Send Methods
    # =========================================================================

    async def send(
        self,
        from_agent_id: str,
        message_type: InterAgentMessageType,
        subject: str,
        content: str,
        *,
        to_agent_id: str | None = None,
        requires_response: bool = False,
        priority: int = 0,
        context: dict[str, Any] | None = None,
    ) -> InterAgentMessage:
        """Send a message through the bus.

        Args:
            from_agent_id: Sender agent ID
            message_type: Type of message (progress, query, etc.)
            subject: Short subject line
            content: Full message content
            to_agent_id: Target agent (None = broadcast to orchestrator)
            requires_response: True if sender will wait for response
            priority: 0-10, higher = more urgent
            context: Additional context data

        Returns:
            The persisted message record
        """
        message = InterAgentMessage(
            organization_id=self.org_id,
            from_agent_id=from_agent_id,
            to_agent_id=to_agent_id,
            message_type=message_type.value,
            subject=subject,
            content=content,
            requires_response=requires_response,
            priority=priority,
            context=context or {},
        )
        self.session.add(message)
        await self.session.commit()
        await self.session.refresh(message)

        # Publish real-time event for instant delivery
        # Import here to avoid circular dependency (api -> routes -> message_bus -> api)
        from sibyl.api.pubsub import publish_event

        await publish_event(
            "inter_agent_message",
            {
                "id": str(message.id),
                "from_agent_id": from_agent_id,
                "to_agent_id": to_agent_id,
                "message_type": message_type.value,
                "subject": subject,
                "content": content,
                "priority": priority,
                "requires_response": requires_response,
                "context": context or {},
                "created_at": message.created_at.isoformat(),
            },
            org_id=str(self.org_id),
        )

        log.info(
            "message_sent",
            message_id=str(message.id),
            from_agent=from_agent_id,
            to_agent=to_agent_id,
            message_type=message_type.value,
        )

        return message

    async def respond(
        self,
        original_message_id: UUID,
        content: str,
        *,
        from_agent_id: str,
        context: dict[str, Any] | None = None,
    ) -> InterAgentMessage:
        """Respond to a message (typically a query).

        Args:
            original_message_id: ID of the message being responded to
            content: Response content
            from_agent_id: Agent sending the response
            context: Additional context

        Returns:
            The response message record
        """
        # Get original message to find sender
        result = await self.session.execute(
            select(InterAgentMessage).where(col(InterAgentMessage.id) == original_message_id)
        )
        original = result.scalar_one_or_none()

        if not original:
            raise ValueError(f"Original message not found: {original_message_id}")

        # Create response message
        response = InterAgentMessage(
            organization_id=self.org_id,
            from_agent_id=from_agent_id,
            to_agent_id=original.from_agent_id,  # Send back to original sender
            message_type=InterAgentMessageType.response.value,
            subject=f"Re: {original.subject}",
            content=content,
            response_to_id=original_message_id,
            priority=original.priority,
            context=context or {},
        )
        self.session.add(response)

        # Mark original as responded
        await self.session.execute(
            update(InterAgentMessage)
            .where(col(InterAgentMessage.id) == original_message_id)
            .values(responded_at=datetime.now(UTC).replace(tzinfo=None))
        )

        await self.session.commit()
        await self.session.refresh(response)

        # Publish real-time event
        # Import here to avoid circular dependency
        from sibyl.api.pubsub import publish_event

        await publish_event(
            "inter_agent_message",
            {
                "id": str(response.id),
                "from_agent_id": from_agent_id,
                "to_agent_id": original.from_agent_id,
                "message_type": InterAgentMessageType.response.value,
                "subject": response.subject,
                "content": content,
                "response_to_id": str(original_message_id),
                "context": context or {},
                "created_at": response.created_at.isoformat(),
            },
            org_id=str(self.org_id),
        )

        log.info(
            "message_responded",
            response_id=str(response.id),
            original_id=str(original_message_id),
            from_agent=from_agent_id,
            to_agent=original.from_agent_id,
        )

        return response

    # =========================================================================
    # Convenience Methods
    # =========================================================================

    async def send_progress(
        self,
        from_agent_id: str,
        content: str,
        *,
        progress: int | None = None,
        to_agent_id: str | None = None,
    ) -> InterAgentMessage:
        """Send a progress update.

        Args:
            from_agent_id: Agent reporting progress
            content: Progress description
            progress: Percentage complete (0-100)
            to_agent_id: Specific target (None = orchestrator)
        """
        context = {}
        if progress is not None:
            context["progress_percent"] = progress

        return await self.send(
            from_agent_id,
            InterAgentMessageType.progress,
            subject=f"Progress: {progress}%" if progress else "Progress update",
            content=content,
            to_agent_id=to_agent_id,
            context=context,
        )

    async def send_blocker(
        self,
        from_agent_id: str,
        subject: str,
        content: str,
        *,
        blocking_resource: str | None = None,
    ) -> InterAgentMessage:
        """Report a blocker.

        Args:
            from_agent_id: Blocked agent
            subject: Short blocker description
            content: Full details
            blocking_resource: What resource is blocking (file, API, etc.)
        """
        context = {}
        if blocking_resource:
            context["blocking_resource"] = blocking_resource

        return await self.send(
            from_agent_id,
            InterAgentMessageType.blocker,
            subject=subject,
            content=content,
            priority=7,  # Blockers are high priority
            context=context,
        )

    async def request_review(
        self,
        from_agent_id: str,
        to_agent_id: str,
        subject: str,
        content: str,
        *,
        files: list[str] | None = None,
        diff: str | None = None,
    ) -> InterAgentMessage:
        """Request a code review from another agent.

        Args:
            from_agent_id: Agent requesting review
            to_agent_id: Agent to review
            subject: Review request title
            content: What to review and why
            files: List of files to review
            diff: Git diff to review
        """
        context: dict[str, Any] = {}
        if files:
            context["files"] = files
        if diff:
            context["diff"] = diff

        return await self.send(
            from_agent_id,
            InterAgentMessageType.review_request,
            subject=subject,
            content=content,
            to_agent_id=to_agent_id,
            requires_response=True,
            priority=5,
            context=context,
        )

    async def delegate(
        self,
        from_agent_id: str,
        to_agent_id: str,
        subject: str,
        content: str,
        *,
        task_id: str | None = None,
    ) -> InterAgentMessage:
        """Delegate work to another agent.

        Args:
            from_agent_id: Delegating agent
            to_agent_id: Agent receiving work
            subject: Delegation title
            content: Work description
            task_id: Associated task ID
        """
        context = {}
        if task_id:
            context["task_id"] = task_id

        return await self.send(
            from_agent_id,
            InterAgentMessageType.delegation,
            subject=subject,
            content=content,
            to_agent_id=to_agent_id,
            priority=5,
            context=context,
        )

    # =========================================================================
    # Query Methods (Blocking)
    # =========================================================================

    async def query(
        self,
        from_agent_id: str,
        to_agent_id: str,
        subject: str,
        content: str,
        *,
        timeout_seconds: float = DEFAULT_QUERY_TIMEOUT,
        context: dict[str, Any] | None = None,
    ) -> InterAgentMessage | None:
        """Send a blocking query and wait for response.

        Args:
            from_agent_id: Agent asking
            to_agent_id: Agent to ask
            subject: Query subject
            content: Query content
            timeout_seconds: Max seconds to wait for response
            context: Additional context

        Returns:
            Response message, or None if timeout
        """
        # Send query with requires_response flag
        query_msg = await self.send(
            from_agent_id,
            InterAgentMessageType.query,
            subject=subject,
            content=content,
            to_agent_id=to_agent_id,
            requires_response=True,
            priority=5,
            context=context,
        )

        # Poll for response
        return await self.wait_for_response(query_msg.id, timeout_seconds=timeout_seconds)

    async def wait_for_response(
        self,
        message_id: UUID,
        *,
        timeout_seconds: float = DEFAULT_QUERY_TIMEOUT,
    ) -> InterAgentMessage | None:
        """Wait for a response to a message.

        Args:
            message_id: Message ID to wait for response to
            timeout_seconds: Max seconds to wait

        Returns:
            Response message, or None if timeout
        """
        deadline = asyncio.get_event_loop().time() + timeout_seconds

        while asyncio.get_event_loop().time() < deadline:
            # Check for response
            result = await self.session.execute(
                select(InterAgentMessage).where(col(InterAgentMessage.response_to_id) == message_id)
            )
            response = result.scalar_one_or_none()

            if response:
                log.info(
                    "response_received",
                    original_id=str(message_id),
                    response_id=str(response.id),
                )
                return response

            await asyncio.sleep(POLL_INTERVAL)

        log.warning("query_timeout", message_id=str(message_id), timeout=timeout_seconds)
        return None

    # =========================================================================
    # Retrieval Methods
    # =========================================================================

    async def get_pending(
        self,
        agent_id: str,
        *,
        limit: int = 50,
        include_read: bool = False,
    ) -> list[InterAgentMessage]:
        """Get pending messages for an agent.

        Args:
            agent_id: Agent to get messages for
            limit: Max messages to return
            include_read: Include already-read messages

        Returns:
            List of messages, ordered by priority desc, created_at asc
        """
        query = (
            select(InterAgentMessage)
            .where(col(InterAgentMessage.organization_id) == self.org_id)
            .where(col(InterAgentMessage.to_agent_id) == agent_id)
        )

        if not include_read:
            query = query.where(col(InterAgentMessage.read_at).is_(None))

        query = query.order_by(
            col(InterAgentMessage.priority).desc(),
            col(InterAgentMessage.created_at).asc(),
        ).limit(limit)

        result = await self.session.execute(query)
        return list(result.scalars().all())

    async def mark_read(self, message_id: UUID) -> None:
        """Mark a message as read.

        Args:
            message_id: Message to mark as read
        """
        await self.session.execute(
            update(InterAgentMessage)
            .where(col(InterAgentMessage.id) == message_id)
            .values(read_at=datetime.now(UTC).replace(tzinfo=None))
        )
        await self.session.commit()

    async def get_conversation(
        self,
        agent_id: str,
        other_agent_id: str,
        *,
        limit: int = 100,
    ) -> list[InterAgentMessage]:
        """Get conversation between two agents.

        Args:
            agent_id: First agent
            other_agent_id: Second agent
            limit: Max messages to return

        Returns:
            List of messages between the agents, ordered by created_at
        """
        result = await self.session.execute(
            select(InterAgentMessage)
            .where(col(InterAgentMessage.organization_id) == self.org_id)
            .where(
                (
                    (col(InterAgentMessage.from_agent_id) == agent_id)
                    & (col(InterAgentMessage.to_agent_id) == other_agent_id)
                )
                | (
                    (col(InterAgentMessage.from_agent_id) == other_agent_id)
                    & (col(InterAgentMessage.to_agent_id) == agent_id)
                )
            )
            .order_by(col(InterAgentMessage.created_at).asc())
            .limit(limit)
        )
        return list(result.scalars().all())


# =============================================================================
# Message Formatting for Claude Code Injection
# =============================================================================


def format_message_for_injection(message: InterAgentMessage) -> str:
    """Format a message for injection into Claude Code session.

    Creates a formatted prompt that can be injected into an agent's
    Claude Code session to deliver the inter-agent message.

    Args:
        message: Message to format

    Returns:
        Formatted string suitable for Claude Code prompt injection
    """
    type_labels = {
        InterAgentMessageType.progress.value: "Progress Update",
        InterAgentMessageType.query.value: "Question from Agent",
        InterAgentMessageType.response.value: "Response",
        InterAgentMessageType.review_request.value: "Code Review Request",
        InterAgentMessageType.review_result.value: "Code Review Result",
        InterAgentMessageType.blocker.value: "Blocker Alert",
        InterAgentMessageType.delegation.value: "Work Delegation",
        InterAgentMessageType.system.value: "System Message",
    }

    type_label = type_labels.get(message.message_type, "Message")
    from_label = f"Agent {message.from_agent_id}"

    lines = [
        f"━━━ {type_label} from {from_label} ━━━",
        f"Subject: {message.subject}",
        "",
        message.content,
    ]

    # Add context if present
    if message.context:
        if "files" in message.context:
            lines.append("")
            lines.append(f"Files: {', '.join(message.context['files'])}")
        if "diff" in message.context:
            lines.append("")
            lines.append("```diff")
            lines.append(message.context["diff"])
            lines.append("```")
        if "progress_percent" in message.context:
            lines.append(f"Progress: {message.context['progress_percent']}%")

    # Add response instruction for queries
    if message.requires_response:
        lines.append("")
        lines.append(
            f"⚡ This message requires a response. "
            f"Use the respond_to_agent tool with message_id={message.id}"
        )

    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

    return "\n".join(lines)


def format_messages_digest(messages: list[InterAgentMessage]) -> str:
    """Format multiple messages as a digest for injection.

    Args:
        messages: Messages to format

    Returns:
        Formatted digest string
    """
    if not messages:
        return ""

    lines = [
        f"📬 You have {len(messages)} new message(s) from other agents:",
        "",
    ]

    for msg in messages:
        lines.append(format_message_for_injection(msg))
        lines.append("")

    return "\n".join(lines)
