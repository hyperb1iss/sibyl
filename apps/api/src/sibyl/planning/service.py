"""Planning Session service for multi-agent brainstorming.

Manages the lifecycle of planning sessions from creation through
materialization into Sibyl epics and tasks.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlmodel import col

from sibyl.db import get_session
from sibyl.db.models import (
    BrainstormMessage,
    BrainstormThread,
    BrainstormThreadStatus,
    PlanningPhase,
    PlanningSession,
)

log = structlog.get_logger()


class PlanningSessionService:
    """Service for managing planning sessions and brainstorm threads.

    Handles the ephemeral brainstorming layer before materialization
    to the Sibyl knowledge graph.
    """

    def __init__(self, session: AsyncSession) -> None:
        """Initialize with database session.

        Args:
            session: SQLAlchemy async session for database operations
        """
        self._session = session

    # =========================================================================
    # Session CRUD
    # =========================================================================

    async def create_session(
        self,
        *,
        org_id: UUID,
        created_by: UUID,
        prompt: str,
        title: str | None = None,
        project_id: UUID | None = None,
    ) -> PlanningSession:
        """Create a new planning session.

        Args:
            org_id: Organization ID for multi-tenancy
            created_by: User ID who created the session
            prompt: Initial brainstorming prompt
            title: Optional title (can be auto-generated later)
            project_id: Optional project to scope the session

        Returns:
            Created PlanningSession
        """
        planning_session = PlanningSession(
            org_id=org_id,
            created_by=created_by,
            project_id=project_id,
            title=title,
            prompt=prompt,
            phase=PlanningPhase.created,
        )
        self._session.add(planning_session)
        await self._session.flush()
        await self._session.refresh(planning_session)

        log.info(
            "Planning session created",
            session_id=str(planning_session.id),
            org_id=str(org_id),
            project_id=str(project_id) if project_id else None,
        )

        return planning_session

    async def get_session(
        self,
        session_id: UUID,
        org_id: UUID,
        *,
        include_threads: bool = False,
    ) -> PlanningSession | None:
        """Get a planning session by ID.

        Args:
            session_id: Session UUID
            org_id: Organization ID for multi-tenancy scoping
            include_threads: Whether to eager-load threads

        Returns:
            PlanningSession if found, None otherwise
        """
        stmt = select(PlanningSession).where(
            col(PlanningSession.id) == session_id,
            col(PlanningSession.org_id) == org_id,
        )

        if include_threads:
            stmt = stmt.options(selectinload(PlanningSession.threads))  # type: ignore[arg-type]

        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_sessions(
        self,
        org_id: UUID,
        *,
        project_id: UUID | None = None,
        phase: PlanningPhase | None = None,
        created_by: UUID | None = None,
        include_archived: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> list[PlanningSession]:
        """List planning sessions with filtering.

        Args:
            org_id: Organization ID for multi-tenancy scoping
            project_id: Filter by project
            phase: Filter by phase
            created_by: Filter by creator
            include_archived: Include materialized/discarded sessions
            limit: Maximum results
            offset: Pagination offset

        Returns:
            List of PlanningSession objects
        """
        stmt = select(PlanningSession).where(col(PlanningSession.org_id) == org_id)

        if project_id:
            stmt = stmt.where(col(PlanningSession.project_id) == project_id)
        if phase:
            stmt = stmt.where(col(PlanningSession.phase) == phase)
        if created_by:
            stmt = stmt.where(col(PlanningSession.created_by) == created_by)

        if not include_archived:
            stmt = stmt.where(
                col(PlanningSession.phase).notin_(
                    [PlanningPhase.materialized, PlanningPhase.discarded]
                )
            )

        stmt = stmt.order_by(col(PlanningSession.updated_at).desc())
        stmt = stmt.offset(offset).limit(limit)

        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def update_session(
        self,
        session_id: UUID,
        org_id: UUID,
        *,
        title: str | None = None,
        phase: PlanningPhase | None = None,
        personas: list[dict] | None = None,
        synthesis: str | None = None,
        spec_draft: str | None = None,
        task_drafts: list[dict] | None = None,
    ) -> PlanningSession | None:
        """Update a planning session.

        Args:
            session_id: Session UUID
            org_id: Organization ID for multi-tenancy scoping
            title: New title
            phase: New phase
            personas: Generated personas
            synthesis: Synthesized brainstorm output
            spec_draft: Draft specification
            task_drafts: Draft tasks for materialization

        Returns:
            Updated PlanningSession if found
        """
        planning_session = await self.get_session(session_id, org_id)
        if not planning_session:
            return None

        if title is not None:
            planning_session.title = title
        if phase is not None:
            planning_session.phase = phase
        if personas is not None:
            planning_session.personas = personas
        if synthesis is not None:
            planning_session.synthesis = synthesis
        if spec_draft is not None:
            planning_session.spec_draft = spec_draft
        if task_drafts is not None:
            planning_session.task_drafts = task_drafts

        planning_session.updated_at = datetime.now(UTC).replace(tzinfo=None)
        await self._session.flush()

        log.info(
            "Planning session updated",
            session_id=str(session_id),
            phase=planning_session.phase.value if planning_session.phase else None,
        )

        return planning_session

    async def delete_session(self, session_id: UUID, org_id: UUID) -> bool:
        """Delete a planning session (cascades to threads/messages).

        Args:
            session_id: Session UUID
            org_id: Organization ID for multi-tenancy scoping

        Returns:
            True if deleted, False if not found
        """
        planning_session = await self.get_session(session_id, org_id)
        if not planning_session:
            return False

        await self._session.delete(planning_session)
        await self._session.flush()

        log.info("Planning session deleted", session_id=str(session_id))
        return True

    async def discard_session(self, session_id: UUID, org_id: UUID) -> PlanningSession | None:
        """Discard a session (soft delete - marks as discarded).

        Args:
            session_id: Session UUID
            org_id: Organization ID for multi-tenancy scoping

        Returns:
            Updated PlanningSession if found
        """
        return await self.update_session(session_id, org_id, phase=PlanningPhase.discarded)

    # =========================================================================
    # Thread Management
    # =========================================================================

    async def create_thread(
        self,
        *,
        session_id: UUID,
        persona_role: str,
        persona_name: str | None = None,
        persona_focus: str | None = None,
        persona_system_prompt: str | None = None,
    ) -> BrainstormThread:
        """Create a brainstorm thread for a persona.

        Args:
            session_id: Parent session UUID
            persona_role: Role/archetype of the persona
            persona_name: Display name for the persona
            persona_focus: What this persona focuses on
            persona_system_prompt: Full system prompt for the agent

        Returns:
            Created BrainstormThread
        """
        thread = BrainstormThread(
            session_id=session_id,
            persona_role=persona_role,
            persona_name=persona_name,
            persona_focus=persona_focus,
            persona_system_prompt=persona_system_prompt,
            status=BrainstormThreadStatus.pending,
        )
        self._session.add(thread)
        await self._session.flush()
        await self._session.refresh(thread)

        log.debug(
            "Brainstorm thread created",
            thread_id=str(thread.id),
            session_id=str(session_id),
            persona_role=persona_role,
        )

        return thread

    async def get_thread(
        self,
        thread_id: UUID,
        *,
        include_messages: bool = False,
    ) -> BrainstormThread | None:
        """Get a brainstorm thread by ID.

        Args:
            thread_id: Thread UUID
            include_messages: Whether to eager-load messages

        Returns:
            BrainstormThread if found
        """
        stmt = select(BrainstormThread).where(col(BrainstormThread.id) == thread_id)

        if include_messages:
            stmt = stmt.options(selectinload(BrainstormThread.messages))  # type: ignore[arg-type]

        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_threads(
        self,
        session_id: UUID,
        *,
        status: BrainstormThreadStatus | None = None,
    ) -> list[BrainstormThread]:
        """List threads for a session.

        Args:
            session_id: Parent session UUID
            status: Filter by status

        Returns:
            List of BrainstormThread objects
        """
        stmt = select(BrainstormThread).where(col(BrainstormThread.session_id) == session_id)

        if status:
            stmt = stmt.where(col(BrainstormThread.status) == status)

        stmt = stmt.order_by(col(BrainstormThread.created_at))

        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def update_thread_status(
        self,
        thread_id: UUID,
        status: BrainstormThreadStatus,
        *,
        agent_id: str | None = None,
    ) -> BrainstormThread | None:
        """Update thread status and optionally link agent.

        Args:
            thread_id: Thread UUID
            status: New status
            agent_id: Agent ID if started

        Returns:
            Updated thread if found
        """
        thread = await self.get_thread(thread_id)
        if not thread:
            return None

        thread.status = status

        if status == BrainstormThreadStatus.running:
            thread.started_at = datetime.now(UTC).replace(tzinfo=None)
            if agent_id:
                thread.agent_id = agent_id
        elif status in (BrainstormThreadStatus.completed, BrainstormThreadStatus.failed):
            thread.completed_at = datetime.now(UTC).replace(tzinfo=None)

        await self._session.flush()

        log.debug(
            "Thread status updated",
            thread_id=str(thread_id),
            status=status.value,
        )

        return thread

    # =========================================================================
    # Message Management
    # =========================================================================

    async def add_message(
        self,
        *,
        thread_id: UUID,
        role: str,
        content: str,
        thinking: str | None = None,
    ) -> BrainstormMessage:
        """Add a message to a brainstorm thread.

        Args:
            thread_id: Parent thread UUID
            role: Message role (user, assistant, system)
            content: Message content
            thinking: Optional thinking/reasoning trace

        Returns:
            Created BrainstormMessage
        """
        message = BrainstormMessage(
            thread_id=thread_id,
            role=role,
            content=content,
            thinking=thinking,
        )
        self._session.add(message)
        await self._session.flush()
        await self._session.refresh(message)

        return message

    async def list_messages(
        self,
        thread_id: UUID,
        *,
        limit: int = 100,
    ) -> list[BrainstormMessage]:
        """List messages for a thread.

        Args:
            thread_id: Thread UUID
            limit: Maximum messages to return

        Returns:
            List of BrainstormMessage objects ordered by creation time
        """
        stmt = (
            select(BrainstormMessage)
            .where(col(BrainstormMessage.thread_id) == thread_id)
            .order_by(col(BrainstormMessage.created_at))
            .limit(limit)
        )

        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    # =========================================================================
    # Phase Transitions
    # =========================================================================

    async def start_brainstorming(
        self,
        session_id: UUID,
        org_id: UUID,
        personas: list[dict],
    ) -> PlanningSession | None:
        """Transition session to brainstorming phase with generated personas.

        Args:
            session_id: Session UUID
            org_id: Organization ID
            personas: List of persona definitions

        Returns:
            Updated session if found
        """
        planning_session = await self.get_session(session_id, org_id)
        if not planning_session:
            return None

        if planning_session.phase != PlanningPhase.created:
            log.warning(
                "Invalid phase transition",
                current=planning_session.phase.value,
                target="brainstorming",
            )
            return None

        # Store personas and update phase
        planning_session.personas = personas
        planning_session.phase = PlanningPhase.brainstorming
        planning_session.updated_at = datetime.now(UTC).replace(tzinfo=None)

        # Create threads for each persona
        for persona in personas:
            await self.create_thread(
                session_id=session_id,
                persona_role=persona.get("role", "analyst"),
                persona_name=persona.get("name"),
                persona_focus=persona.get("focus"),
                persona_system_prompt=persona.get("system_prompt"),
            )

        await self._session.flush()

        log.info(
            "Brainstorming started",
            session_id=str(session_id),
            persona_count=len(personas),
        )

        return planning_session

    async def complete_brainstorming(
        self,
        session_id: UUID,
        org_id: UUID,
    ) -> PlanningSession | None:
        """Check if all threads complete and transition to synthesizing.

        Args:
            session_id: Session UUID
            org_id: Organization ID

        Returns:
            Updated session if all threads complete
        """
        planning_session = await self.get_session(session_id, org_id, include_threads=True)
        if not planning_session:
            return None

        if planning_session.phase != PlanningPhase.brainstorming:
            return None

        # Check all threads are complete
        threads = planning_session.threads or []
        if not threads:
            return None

        all_complete = all(
            t.status in (BrainstormThreadStatus.completed, BrainstormThreadStatus.failed)
            for t in threads
        )

        if not all_complete:
            return None

        # Transition to synthesizing
        planning_session.phase = PlanningPhase.synthesizing
        planning_session.updated_at = datetime.now(UTC).replace(tzinfo=None)
        await self._session.flush()

        log.info(
            "Brainstorming complete, moving to synthesis",
            session_id=str(session_id),
            thread_count=len(threads),
        )

        return planning_session


# Factory function for dependency injection
async def get_planning_service():  # type: ignore[misc]
    """Get a PlanningSessionService instance with database session.

    Returns:
        AsyncGenerator yielding PlanningSessionService
    """
    async with get_session() as session:
        yield PlanningSessionService(session)
