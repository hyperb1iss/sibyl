"""BrainstormOrchestrator for parallel persona agent execution.

Coordinates multiple Claude agents, each with a unique persona,
to brainstorm on a topic concurrently.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any
from uuid import UUID

import structlog
from claude_agent_sdk import ClaudeSDKClient
from claude_agent_sdk.types import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
)

from sibyl.api.pubsub import publish_event
from sibyl.db.models import BrainstormThreadStatus
from sibyl.planning.personas import build_persona_system_prompt
from sibyl.planning.service import PlanningSessionService

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

log = structlog.get_logger()

# Use Sonnet for brainstorming - good balance of quality and speed
_BRAINSTORM_MODEL = "claude-sonnet-4-20250514"


class BrainstormAgent:
    """A single persona agent for brainstorming.

    Lightweight wrapper around ClaudeSDKClient for brainstorming.
    No worktrees, hooks, or approvals - just pure ideation.
    """

    def __init__(
        self,
        thread_id: UUID,
        persona: dict,
        session_prompt: str,
        round_number: int = 1,
    ):
        """Initialize brainstorm agent.

        Args:
            thread_id: Database thread ID to store messages
            persona: Persona definition dict
            session_prompt: Main brainstorming topic/prompt
            round_number: Which brainstorming round this is
        """
        self.thread_id = thread_id
        self.persona = persona
        self.session_prompt = session_prompt
        self.round_number = round_number

        self.system_prompt = build_persona_system_prompt(
            persona, session_prompt, round_number
        )

        # Result storage
        self.response_content: str = ""
        self.thinking: str | None = None
        self.tokens_used: int = 0
        self.cost_usd: float = 0.0

    @property
    def persona_name(self) -> str:
        """Display name for this persona."""
        return self.persona.get("name", "Participant")

    @property
    def persona_role(self) -> str:
        """Role/archetype for this persona."""
        return self.persona.get("role", "analyst")

    async def execute(self) -> str:
        """Execute the brainstorm agent and return response.

        Returns:
            The agent's brainstorm response
        """
        log.debug(
            "Starting brainstorm agent",
            persona=self.persona_name,
            thread_id=str(self.thread_id),
        )

        # Simple prompt - just ask for thoughts on the topic
        user_prompt = f"Share your perspective on this brainstorming topic. Remember you are {self.persona_name}."

        # Configure SDK - minimal options, no tools needed
        # Note: Model and token limits are configured via the SDK defaults
        options = ClaudeAgentOptions(
            system_prompt=self.system_prompt,
            model=_BRAINSTORM_MODEL,
        )

        async with ClaudeSDKClient(options=options) as client:
            await client.query(user_prompt)

            # Collect full response
            response_parts: list[str] = []

            async for message in client.receive_response():
                if isinstance(message, AssistantMessage):
                    if message.content:
                        # Extract text from content blocks
                        response_parts.extend(
                            block.text
                            for block in message.content
                            if isinstance(block, TextBlock)
                        )

                elif isinstance(message, ResultMessage):
                    # Track usage
                    if message.usage:
                        self.tokens_used = message.usage.get(
                            "input_tokens", 0
                        ) + message.usage.get("output_tokens", 0)
                    if message.total_cost_usd:
                        self.cost_usd = message.total_cost_usd

            self.response_content = "".join(response_parts)

            log.debug(
                "Brainstorm agent complete",
                persona=self.persona_name,
                tokens=self.tokens_used,
                response_length=len(self.response_content),
            )

        return self.response_content


class BrainstormOrchestrator:
    """Orchestrates parallel brainstorm agent execution.

    Runs multiple persona agents concurrently and coordinates
    their outputs for synthesis.
    """

    def __init__(
        self,
        db_session: AsyncSession,
        org_id: UUID,
    ):
        """Initialize orchestrator.

        Args:
            db_session: Database session for service operations
            org_id: Organization ID for multi-tenancy
        """
        self.db_session = db_session
        self.org_id = org_id
        self.service = PlanningSessionService(db_session)

    async def run_brainstorming(
        self,
        session_id: UUID,
        round_number: int = 1,
    ) -> dict[str, Any]:
        """Run a brainstorming round with all personas.

        Executes all persona agents in parallel and stores
        their responses to the respective threads.

        Args:
            session_id: Planning session ID
            round_number: Which brainstorming round (1, 2, etc.)

        Returns:
            Summary dict with results per persona
        """
        log.info(
            "Starting brainstorming round",
            session_id=str(session_id),
            round=round_number,
        )

        # Get session with threads
        session = await self.service.get_session(
            session_id, self.org_id, include_threads=True
        )
        if not session:
            raise ValueError(f"Session not found: {session_id}")

        threads = session.threads or []
        if not threads:
            raise ValueError("No threads found for session")

        # Create agents for each thread/persona
        agents: list[tuple[BrainstormAgent, UUID]] = []

        for thread in threads:
            if thread.status == BrainstormThreadStatus.completed:
                continue  # Skip already completed

            persona = {
                "role": thread.persona_role,
                "name": thread.persona_name,
                "focus": thread.persona_focus,
                "system_prompt": thread.persona_system_prompt,
            }

            agent = BrainstormAgent(
                thread_id=thread.id,
                persona=persona,
                session_prompt=session.prompt,
                round_number=round_number,
            )
            agents.append((agent, thread.id))

            # Mark thread as running
            await self.service.update_thread_status(
                thread.id, BrainstormThreadStatus.running
            )

        # Publish status update
        await publish_event(
            "planning_brainstorming_started",
            {
                "session_id": str(session_id),
                "org_id": str(self.org_id),
                "round": round_number,
                "agent_count": len(agents),
            },
        )

        # Execute all agents in parallel
        tasks = [self._run_agent_with_storage(agent, thread_id) for agent, thread_id in agents]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Process results
        summary: dict[str, Any] = {
            "session_id": str(session_id),
            "round": round_number,
            "personas": [],
            "total_tokens": 0,
            "total_cost_usd": 0.0,
            "errors": [],
        }

        for i, (agent, thread_id) in enumerate(agents):
            result = results[i]

            if isinstance(result, Exception):
                # Mark thread as failed
                await self.service.update_thread_status(
                    thread_id, BrainstormThreadStatus.failed
                )
                summary["errors"].append(
                    {"persona": agent.persona_name, "error": str(result)}
                )
            else:
                # Thread already marked complete in _run_agent_with_storage
                summary["personas"].append(
                    {
                        "role": agent.persona_role,
                        "name": agent.persona_name,
                        "response_length": len(agent.response_content),
                        "tokens": agent.tokens_used,
                    }
                )
                summary["total_tokens"] += agent.tokens_used
                summary["total_cost_usd"] += agent.cost_usd

        # Check if all complete and transition session
        await self.service.complete_brainstorming(session_id, self.org_id)

        # Publish completion
        await publish_event(
            "planning_brainstorming_completed",
            {
                "session_id": str(session_id),
                "org_id": str(self.org_id),
                "round": round_number,
                "persona_count": len(summary["personas"]),
                "error_count": len(summary["errors"]),
            },
        )

        log.info(
            "Brainstorming round complete",
            session_id=str(session_id),
            round=round_number,
            personas=len(summary["personas"]),
            errors=len(summary["errors"]),
        )

        return summary

    async def _run_agent_with_storage(
        self,
        agent: BrainstormAgent,
        thread_id: UUID,
    ) -> str:
        """Run an agent and store its response.

        Args:
            agent: The brainstorm agent to run
            thread_id: Thread ID for message storage

        Returns:
            The agent's response content
        """
        try:
            # Execute agent
            response = await agent.execute()

            # Store response as message
            await self.service.add_message(
                thread_id=thread_id,
                role="assistant",
                content=response,
                thinking=agent.thinking,
            )

            # Mark thread complete
            await self.service.update_thread_status(
                thread_id, BrainstormThreadStatus.completed
            )

            # Publish per-thread update
            await publish_event(
                "planning_thread_completed",
                {
                    "thread_id": str(thread_id),
                    "persona_name": agent.persona_name,
                    "tokens": agent.tokens_used,
                },
            )

            return response

        except Exception:
            log.exception(
                "Agent execution failed",
                thread_id=str(thread_id),
                persona=agent.persona_name,
            )
            # Re-raise to be caught by gather
            raise


async def run_brainstorming_job(
    session_id: UUID,
    org_id: UUID,
    round_number: int = 1,
) -> dict[str, Any]:
    """Job entry point for brainstorming execution.

    Designed to be called from the ARQ worker.

    Args:
        session_id: Planning session ID
        org_id: Organization ID
        round_number: Brainstorming round number

    Returns:
        Summary dict with results
    """
    from sibyl.db import get_session

    async with get_session() as db_session:
        orchestrator = BrainstormOrchestrator(db_session, org_id)
        result = await orchestrator.run_brainstorming(session_id, round_number)
        await db_session.commit()
        return result
