"""AgentRunner for Claude Agent SDK integration.

Provides the bridge between Sibyl's agent harness and Claude's Agent SDK,
enabling agent spawning, lifecycle management, and tool integration.
"""

import asyncio
import contextlib
import hashlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog
from claude_agent_sdk import ClaudeSDKClient
from claude_agent_sdk.types import (
    AssistantMessage,
    ClaudeAgentOptions,
    Message,
    ResultMessage,
    UserMessage,
)

from sibyl.agents.approvals import ApprovalService
from sibyl.agents.hooks import (
    SibylContextService,
    WorkflowTracker,
    create_sibyl_hooks,
    load_user_hooks,
    merge_hooks,
)
from sibyl.agents.worktree import WorktreeManager
from sibyl.locks import EntityLockManager, LockAcquisitionError
from sibyl_core.errors import EntityNotFoundError
from sibyl_core.models import (
    AgentCheckpoint,
    AgentRecord,
    AgentSpawnSource,
    AgentStatus,
    AgentType,
    EntityType,
    Task,
)

if TYPE_CHECKING:
    from sibyl_core.graph import EntityManager

log = structlog.get_logger()


def _generate_agent_id(org_id: str, project_id: str, timestamp: str) -> str:
    """Generate a unique agent ID."""
    combined = f"{org_id}:{project_id}:{timestamp}"
    hash_bytes = hashlib.sha256(combined.encode()).hexdigest()[:12]
    return f"agent_{hash_bytes}"


def _derive_agent_name(prompt: str, agent_type: AgentType, agent_id: str) -> str:
    """Derive a descriptive agent name from the prompt.

    Extracts the first meaningful line/sentence from the prompt and truncates
    at a word boundary for a clean title.
    """
    # Clean up the prompt - take first line or sentence
    text = prompt.strip()
    # Take first line
    first_line = text.split("\n")[0].strip()
    # Or first sentence if line is too long
    if len(first_line) > 60:
        # Try to find a sentence boundary
        for sep in (".", "!", "?", ":", ";"):
            if sep in first_line[:60]:
                first_line = first_line[: first_line.index(sep) + 1]
                break

    # Truncate at word boundary around 50 chars
    if len(first_line) > 50:
        # Find last space before 50 chars
        last_space = first_line[:50].rfind(" ")
        if last_space > 20:  # Only truncate if we keep enough
            first_line = first_line[:last_space] + "..."
        else:
            first_line = first_line[:47] + "..."

    # If we got something meaningful, use it
    if len(first_line) >= 10:
        return first_line

    # Fallback to generic name
    return f"{agent_type.value}-{agent_id[-8:]}"


# Haiku model for fast, cheap tag generation
_HAIKU_MODEL = "claude-3-5-haiku-latest"


def _derive_agent_tags(
    prompt: str,
    agent_type: AgentType,
    task: Task | None = None,
) -> list[str]:
    """Derive tags automatically using Claude Haiku.

    Uses LLM to intelligently extract relevant tags from prompt context.
    Falls back to basic tags if LLM call fails.

    Args:
        prompt: Initial prompt for the agent
        agent_type: Type of agent being spawned
        task: Optional assigned task

    Returns:
        Sorted list of tags (max 8)
    """
    # Always include agent type
    base_tags = {agent_type.value}

    # Inherit from task if assigned
    if task:
        if task.tags:
            base_tags.update(task.tags[:3])
        if task.domain:
            base_tags.add(task.domain.lower())

    # Try LLM-based extraction
    try:
        import anthropic

        client = anthropic.Anthropic()

        # Build context for tag extraction
        context_parts = [f"Agent type: {agent_type.value}"]
        if task:
            context_parts.append(f"Task: {task.title}")
            if task.technologies:
                context_parts.append(f"Technologies: {', '.join(task.technologies)}")
        context_parts.append(f"Prompt: {prompt[:500]}")

        llm_prompt = f"""Extract 3-5 short, lowercase tags that categorize this AI agent's work.

{chr(10).join(context_parts)}

Good tags: fix, refactor, test, feature, api, ui, database, auth, perf, docs, migration, security
Bad tags: the, code, work, task, agent (too generic)

Reply with ONLY comma-separated tags, nothing else. Example: fix, api, auth"""

        message = client.messages.create(
            model=_HAIKU_MODEL,
            max_tokens=50,
            messages=[{"role": "user", "content": llm_prompt}],
        )

        # Parse response
        response_text = message.content[0].text.strip().lower()
        llm_tags = {t.strip() for t in response_text.split(",") if t.strip()}

        # Merge with base tags
        all_tags = base_tags | llm_tags
        return sorted(all_tags)[:8]

    except Exception as e:
        log.warning(f"LLM tag extraction failed, using base tags: {e}")
        return sorted(base_tags)[:8]


class AgentRunnerError(Exception):
    """Base exception for agent runner operations."""


class AgentRunner:
    """Runs Claude agents with Sibyl integration.

    Handles agent lifecycle from spawning through execution:
    - Creates isolated worktrees for code tasks
    - Registers agents in the knowledge graph
    - Streams conversation and tool events
    - Manages heartbeats and checkpointing
    """

    # Default system prompt preamble for all agents
    SYSTEM_PROMPT_PREAMBLE = """# Sibyl Agent

You are an AI agent running inside **Sibyl**, a Collective Intelligence Runtime that provides
persistent memory, task orchestration, and collaborative knowledge across coding sessions.

## Environment

You're running as a spawned agent within the Sibyl system. Your work persists in a knowledge
graph shared across all agents and sessions. Think of Sibyl as your team's institutional memory.

**Key capabilities:**
- **Semantic Search**: Find patterns, learnings, and past solutions by meaning
- **Task Management**: Track work items with status, priority, and progress
- **Knowledge Capture**: Store insights, gotchas, and patterns for future reference
- **Session Continuity**: Your work persists—future agents can learn from what you discover

## Workflow

Follow this cycle for effective work:

1. **RESEARCH** before implementing:
   ```bash
   sibyl search "relevant topic"           # Find existing knowledge
   sibyl entity show <id>                  # Get full content by ID
   ```

2. **TRACK** your work:
   ```bash
   sibyl task list --status doing          # Check current tasks
   sibyl task start <task_id>              # Mark task in progress
   ```

3. **CAPTURE** learnings:
   ```bash
   sibyl add "Title" "What you learned"    # Quick insight capture
   sibyl task complete <id> --learnings "..." # Complete with learnings
   ```

## Key Commands

| Command | Purpose |
|---------|---------|
| `sibyl search "query"` | Semantic search across all knowledge |
| `sibyl task list` | View tasks (add `--status todo,doing`) |
| `sibyl task start <id>` | Begin working on a task |
| `sibyl task complete <id> --learnings "..."` | Finish with captured insights |
| `sibyl add "Title" "Content"` | Quick knowledge capture |
| `sibyl entity show <id>` | Retrieve full entity content |

## Guidelines

- **Search first**: Always check existing knowledge before implementing
- **Capture non-obvious insights**: If it took time to figure out, save it
- **Update task status**: Keep status current as you work
- **Complete with learnings**: Every task completion should capture what you learned
- **Be specific**: Good learnings include the what, why, and gotchas

For comprehensive guidance, run `/sibyl` to access the full skill documentation.
"""

    AGENT_TYPE_PROMPTS = {
        AgentType.GENERAL: "You are a general-purpose agent.",
        AgentType.PLANNER: (
            "You are a senior software architect. Break features into "
            "implementable tasks with clear scope and dependencies."
        ),
        AgentType.IMPLEMENTER: (
            "You are a senior developer. Write clean, tested code that follows "
            "existing patterns in the codebase."
        ),
        AgentType.TESTER: (
            "You are a QA engineer. Write comprehensive tests that cover edge cases "
            "and ensure code correctness."
        ),
        AgentType.REVIEWER: (
            "You are a code reviewer. Analyze code for bugs, security issues, "
            "performance problems, and style violations."
        ),
        AgentType.INTEGRATOR: (
            "You are a git expert. Merge branches, resolve conflicts, and ensure "
            "clean integration of parallel work."
        ),
        AgentType.ORCHESTRATOR: (
            "You are a project coordinator. Manage multiple agents, track dependencies, "
            "and ensure work completes efficiently."
        ),
    }

    def __init__(
        self,
        entity_manager: "EntityManager",
        worktree_manager: WorktreeManager,
        org_id: str,
        project_id: str,
        add_dirs: list[str] | None = None,
        permission_mode: str | None = None,
    ):
        """Initialize AgentRunner.

        Args:
            entity_manager: Graph client for agent persistence
            worktree_manager: Worktree manager for agent isolation
            org_id: Organization UUID
            project_id: Project UUID
            add_dirs: Additional directories to allow for sandbox access
            permission_mode: SDK permission mode ("default", "acceptEdits", "bypassPermissions")
        """
        self.entity_manager = entity_manager
        self.worktree_manager = worktree_manager
        self.org_id = org_id
        self.project_id = project_id
        self.add_dirs = add_dirs
        self.permission_mode = permission_mode

        # Active agent instances (in-memory during execution)
        self._active_agents: dict[str, AgentInstance] = {}

        # Distributed lock manager for preventing race conditions
        self._lock_manager = EntityLockManager()

    def _build_system_prompt(
        self,
        agent_type: AgentType,
        task: Task | None = None,
        custom_instructions: str | None = None,
    ) -> str:
        """Build the system prompt for an agent.

        Args:
            agent_type: Type of agent being created
            task: Optional task for context
            custom_instructions: Additional instructions

        Returns:
            Complete system prompt string
        """
        parts = [self.SYSTEM_PROMPT_PREAMBLE]

        # Add agent-type-specific instructions
        type_prompt = self.AGENT_TYPE_PROMPTS.get(agent_type, "")
        if type_prompt:
            parts.append(f"\n## Role\n{type_prompt}")

        # Add task context if provided
        if task:
            task_context = f"""
## Current Task
Title: {task.title}
Description: {task.description}
Status: {task.status}
Priority: {task.priority}
"""
            if task.technologies:
                task_context += f"Technologies: {', '.join(task.technologies)}\n"
            if task.domain:
                task_context += f"Domain: {task.domain}\n"

            parts.append(task_context)

        # Add custom instructions
        if custom_instructions:
            parts.append(f"\n## Additional Instructions\n{custom_instructions}")

        return "\n".join(parts)

    async def spawn(
        self,
        prompt: str,
        agent_type: AgentType = AgentType.GENERAL,
        task: Task | None = None,
        spawn_source: AgentSpawnSource = AgentSpawnSource.USER,
        create_worktree: bool = True,
        custom_instructions: str | None = None,
        enable_approvals: bool = True,
        agent_id: str | None = None,
    ) -> "AgentInstance":
        """Spawn a new Claude agent instance.

        Args:
            prompt: Initial prompt for the agent
            agent_type: Type of specialized agent
            task: Optional task to assign
            spawn_source: How this agent was created
            create_worktree: Whether to create an isolated worktree
            custom_instructions: Additional system prompt instructions
            enable_approvals: Enable human-in-the-loop approval hooks
            agent_id: Optional pre-generated agent ID (generated if not provided)

        Returns:
            AgentInstance ready for execution
        """
        log.info(f"Spawning {agent_type} agent for task {task.id if task else 'adhoc'}")

        # Generate agent ID if not provided
        if agent_id is None:
            timestamp = datetime.now(UTC).isoformat()
            agent_id = _generate_agent_id(self.org_id, self.project_id, timestamp)

        # Check if entity was pre-created by API (avoids race condition)
        record: AgentRecord | None = None
        try:
            existing = await self.entity_manager.get(agent_id)
            if isinstance(existing, AgentRecord):
                record = existing
                log.debug("Using pre-created agent record", agent_id=agent_id)
        except EntityNotFoundError:
            pass

        # Derive tags from context
        tags = _derive_agent_tags(prompt, agent_type, task)

        # Create agent record if not pre-created
        if record is None:
            record = AgentRecord(
                id=agent_id,
                name=_derive_agent_name(prompt, agent_type, agent_id),
                organization_id=self.org_id,
                project_id=self.project_id,
                agent_type=agent_type,
                spawn_source=spawn_source,
                task_id=task.id if task else None,
                status=AgentStatus.INITIALIZING,
                initial_prompt=prompt[:500],  # Truncate for storage
                tags=tags,
            )
            # Persist to graph (use create_direct to skip LLM extraction)
            await self.entity_manager.create_direct(record)
        # Update pre-created record with tags if missing
        elif not record.tags:
            record.tags = tags
            await self.entity_manager.update(record.id, {"tags": tags})

        # Create worktree if requested
        worktree_path: Path | None = None
        if create_worktree:
            branch_name = f"agent/{record.id[-12:]}"
            if task:
                # Use task-derived branch name
                safe_title = task.title[:30].lower().replace(" ", "-")
                branch_name = f"agent/{record.id[-8:]}-{safe_title}"

            worktree = await self.worktree_manager.create(
                task_id=task.id if task else record.id,
                branch_name=branch_name,
                agent_id=record.id,
            )
            worktree_path = Path(worktree.path)

            # Update record with worktree info
            await self.entity_manager.update(
                record.id,
                {
                    "worktree_path": worktree.path,
                    "worktree_branch": worktree.branch,
                },
            )
            record.worktree_path = worktree.path
            record.worktree_branch = worktree.branch

        # Build system prompt
        system_prompt = self._build_system_prompt(
            agent_type=agent_type,
            task=task,
            custom_instructions=custom_instructions,
        )

        # Create approval service if enabled
        approval_service: ApprovalService | None = None

        if enable_approvals:
            approval_service = ApprovalService(
                entity_manager=self.entity_manager,
                org_id=self.org_id,
                project_id=self.project_id,
                agent_id=record.id,
                task_id=task.id if task else None,
            )

        # Create context service for Sibyl knowledge injection
        context_service = SibylContextService(
            entity_manager=self.entity_manager,
            org_id=self.org_id,
            project_id=self.project_id,
        )

        # Build hooks: load user's Claude Code hooks + merge with Sibyl hooks
        cwd = str(worktree_path) if worktree_path else None
        user_hooks = load_user_hooks(cwd=cwd)
        sibyl_hooks = create_sibyl_hooks(
            approval_service=approval_service,
            context_service=context_service,
        )
        merged_hooks = merge_hooks(sibyl_hooks, user_hooks)

        log.debug(
            f"Hooks configured for agent {record.id}: "
            f"user={list(user_hooks.keys()) if user_hooks else []}, "
            f"sibyl={list(sibyl_hooks.keys()) if sibyl_hooks else []}"
        )

        # Create SDK options
        # - setting_sources: Load Claude Code config from user (~/.claude) and project (.claude)
        # - can_use_tool: Integrate with SDK's permission system via our approval UI
        # - add_dirs: Additional sandbox-allowed directories (e.g., temp dirs for tests)
        sdk_kwargs: dict[str, Any] = {
            "cwd": cwd,
            "system_prompt": system_prompt,
            "hooks": merged_hooks,
            "setting_sources": ["user", "project"],
            "can_use_tool": approval_service.create_can_use_tool_callback()
            if approval_service
            else None,
        }
        if self.add_dirs:
            sdk_kwargs["add_dirs"] = self.add_dirs
        if self.permission_mode:
            sdk_kwargs["permission_mode"] = self.permission_mode
        sdk_options = ClaudeAgentOptions(**sdk_kwargs)  # type: ignore[arg-type]

        # Create instance
        instance = AgentInstance(
            record=record,
            sdk_options=sdk_options,
            entity_manager=self.entity_manager,
            initial_prompt=prompt,
            worktree_path=worktree_path,
            task=task,
            approval_service=approval_service,
            context_service=context_service,
        )

        # Register as active
        self._active_agents[record.id] = instance

        # Update status
        await self.entity_manager.update(
            record.id,
            {
                "status": AgentStatus.WORKING.value,
                "started_at": datetime.now(UTC).isoformat(),
            },
        )

        log.info(f"Agent {record.id} spawned and ready")
        return instance

    async def spawn_for_task(
        self,
        task: Task,
        agent_type: AgentType = AgentType.IMPLEMENTER,
    ) -> "AgentInstance":
        """Spawn an agent for a specific task with race condition protection.

        Uses a distributed lock to prevent concurrent spawns for the same task,
        which could create duplicate agents.

        Args:
            task: Task to work on
            agent_type: Type of agent (defaults to implementer)

        Returns:
            AgentInstance assigned to the task

        Raises:
            LockAcquisitionError: If another spawn is in progress for this task
            ValueError: If an agent is already running for this task
        """
        lock_key = f"spawn:task:{task.id}"
        lock_token: str | None = None

        try:
            # Acquire lock to prevent concurrent spawns for the same task
            lock_token = await self._lock_manager.acquire(self.org_id, lock_key, blocking=False)
            if lock_token is None:
                raise LockAcquisitionError(task.id, self.org_id, "concurrent spawn in progress")

            # Check if an agent is already running for this task
            for agent in self._active_agents.values():
                if agent.task and agent.task.id == task.id:
                    raise ValueError(f"Agent {agent.id} is already running for task {task.id}")

            prompt = f"Please work on this task:\n\n{task.title}\n\n{task.description}"
            return await self.spawn(
                prompt=prompt,
                agent_type=agent_type,
                task=task,
                spawn_source=AgentSpawnSource.ORCHESTRATOR,
                create_worktree=True,
            )
        finally:
            if lock_token:
                await self._lock_manager.release(self.org_id, lock_key, lock_token)

    async def get_agent(self, agent_id: str) -> "AgentInstance | None":
        """Get an active agent instance by ID."""
        return self._active_agents.get(agent_id)

    async def list_active(self) -> list["AgentInstance"]:
        """List all active agent instances."""
        return list(self._active_agents.values())

    async def stop_agent(self, agent_id: str, reason: str = "user_request") -> bool:
        """Stop an active agent.

        Args:
            agent_id: Agent to stop
            reason: Why the agent is being stopped

        Returns:
            True if agent was stopped
        """
        instance = self._active_agents.pop(agent_id, None)
        if not instance:
            return False

        await instance.stop(reason)
        return True

    async def stop_all(self, reason: str = "shutdown") -> int:
        """Stop all active agents.

        Returns:
            Number of agents stopped
        """
        agent_ids = list(self._active_agents.keys())
        for agent_id in agent_ids:
            await self.stop_agent(agent_id, reason)
        return len(agent_ids)

    async def resume_agent(
        self,
        agent_id: str,
        session_id: str,
        prompt: str = "Continue from where you left off.",
        enable_approvals: bool = True,
    ) -> "AgentInstance":
        """Resume an agent using Claude's session management.

        Uses the Claude SDK's session resume to restore conversation history.
        Claude handles all the conversation state - we just need the session_id.

        Args:
            agent_id: Agent to resume
            session_id: Claude session ID from previous execution
            prompt: User message or continuation prompt
            enable_approvals: Enable human-in-the-loop approval hooks

        Returns:
            Resumed AgentInstance

        Raises:
            AgentRunnerError: If agent cannot be resumed
        """
        log.info(f"Resuming agent {agent_id} with session {session_id}")

        # Get agent record
        entity = await self.entity_manager.get(agent_id)
        if not entity or entity.entity_type != EntityType.AGENT:
            raise AgentRunnerError(f"Agent not found: {agent_id}")

        agent = AgentRecord.from_entity(entity, self.org_id)

        # Validate session_id
        if not session_id:
            raise AgentRunnerError("No session_id available - cannot resume")

        # Update agent status
        await self.entity_manager.update(
            agent.id,
            {"status": AgentStatus.WORKING.value},
        )

        # Recreate approval service if enabled
        approval_service: ApprovalService | None = None
        if enable_approvals:
            approval_service = ApprovalService(
                entity_manager=self.entity_manager,
                org_id=self.org_id,
                project_id=self.project_id,
                agent_id=agent.id,
                task_id=agent.task_id,
            )

        # Create context service for Sibyl knowledge injection
        context_service = SibylContextService(
            entity_manager=self.entity_manager,
            org_id=self.org_id,
            project_id=self.project_id,
        )

        # Get worktree path from agent record if available
        worktree_path: Path | None = None
        if agent.worktree_path:
            worktree_path = Path(agent.worktree_path)
            if not worktree_path.exists():
                log.warning(f"Worktree no longer exists: {worktree_path}")
                worktree_path = None

        # Build hooks
        cwd = str(worktree_path) if worktree_path else None
        user_hooks = load_user_hooks(cwd=cwd)
        sibyl_hooks = create_sibyl_hooks(
            approval_service=approval_service,
            context_service=context_service,
        )
        merged_hooks = merge_hooks(sibyl_hooks, user_hooks)

        # Build SDK options with session resume
        sdk_options = ClaudeAgentOptions(
            cwd=cwd,
            hooks=merged_hooks,  # type: ignore[arg-type]
            setting_sources=["user", "project"],
            can_use_tool=approval_service.create_can_use_tool_callback()
            if approval_service
            else None,
            resume=session_id,  # Claude handles conversation history
        )

        # Get task if assigned
        task: Task | None = None
        if agent.task_id:
            task_entity = await self.entity_manager.get(agent.task_id)
            if task_entity and isinstance(task_entity, Task):
                task = task_entity

        # Create resumed instance
        instance = AgentInstance(
            record=agent,
            sdk_options=sdk_options,
            entity_manager=self.entity_manager,
            initial_prompt=prompt,
            worktree_path=worktree_path,
            task=task,
            approval_service=approval_service,
            context_service=context_service,
        )

        instance.set_session_id(session_id)
        self._active_agents[agent.id] = instance

        log.info(f"Agent {agent.id} resumed (session: {session_id})")
        return instance


class AgentInstance:
    """A running Claude agent instance.

    Wraps the Claude SDK with Sibyl-specific functionality:
    - Heartbeat updates
    - Progress tracking
    - Checkpoint management
    - Event streaming

    Uses ClaudeSDKClient (not query()) to enable hooks support.
    """

    HEARTBEAT_INTERVAL = 30  # seconds

    def __init__(
        self,
        record: AgentRecord,
        sdk_options: ClaudeAgentOptions,
        entity_manager: "EntityManager",
        initial_prompt: str,
        worktree_path: Path | None = None,
        task: Task | None = None,
        approval_service: ApprovalService | None = None,
        context_service: SibylContextService | None = None,
    ):
        """Initialize agent instance.

        Args:
            record: Persistent agent record
            sdk_options: Claude SDK options
            entity_manager: Graph client
            initial_prompt: Prompt to execute
            worktree_path: Working directory
            task: Assigned task
            approval_service: Optional approval service for human-in-the-loop
            context_service: Optional context service with workflow tracker
        """
        self.record = record
        self.sdk_options = sdk_options
        self.entity_manager = entity_manager
        self.initial_prompt = initial_prompt
        self.worktree_path = worktree_path
        self.task = task
        self.approval_service = approval_service
        self.context_service = context_service

        # Runtime state
        self._running = False
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._conversation_history: list[Message] = []
        self._tokens_used = 0
        self._cost_usd = 0.0
        self._session_id: str | None = None
        self._client: ClaudeSDKClient | None = None

    @property
    def id(self) -> str:
        """Agent ID."""
        return self.record.id

    @property
    def status(self) -> AgentStatus:
        """Current agent status."""
        return self.record.status

    @property
    def session_id(self) -> str | None:
        """Claude SDK session ID."""
        return self._session_id

    @property
    def workflow_tracker(self) -> WorkflowTracker | None:
        """Workflow tracker for checking Sibyl workflow compliance."""
        return self.context_service.workflow_tracker if self.context_service else None

    def set_session_id(self, session_id: str) -> None:
        """Set the session ID (used during resume)."""
        self._session_id = session_id

    async def execute(self) -> AsyncIterator[Message]:
        """Execute the agent with the initial prompt.

        Uses ClaudeSDKClient to enable hooks support (query() doesn't support hooks).

        Yields:
            Message objects from the Claude SDK
        """
        self._running = True

        # Start heartbeat task
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

        try:
            # Use ClaudeSDKClient for hooks support (query() doesn't support hooks!)
            async with ClaudeSDKClient(options=self.sdk_options) as client:
                self._client = client

                # Send initial prompt
                await client.query(self.initial_prompt)

                # Stream responses
                async for message in client.receive_response():
                    self._conversation_history.append(message)

                    # Track usage from ResultMessage
                    if isinstance(message, ResultMessage):
                        if message.usage:
                            # usage is a dict with 'input_tokens' and 'output_tokens' keys
                            self._tokens_used += message.usage.get("input_tokens", 0)
                            self._tokens_used += message.usage.get("output_tokens", 0)
                        if message.total_cost_usd:
                            self._cost_usd = message.total_cost_usd
                        if message.session_id:
                            self._session_id = message.session_id

                    yield message

        except Exception as e:
            log.exception(f"Agent {self.id} execution failed")
            await self._update_status(AgentStatus.FAILED, error=str(e))
            raise

        finally:
            self._running = False
            self._client = None
            await self._cancel_heartbeat()

        # Mark completed
        await self._update_status(AgentStatus.COMPLETED)

    async def send_message(self, content: str) -> AsyncIterator[Message]:
        """Send a follow-up message to the agent.

        Creates a new client with session resume to continue conversation.

        Args:
            content: Message content from user

        Yields:
            Response messages from the agent
        """
        # Create options with session resume if we have a session ID
        options = self.sdk_options
        if self._session_id:
            # Create new options with resume to continue conversation
            options = ClaudeAgentOptions(
                cwd=self.sdk_options.cwd,
                system_prompt=self.sdk_options.system_prompt,
                hooks=self.sdk_options.hooks,
                setting_sources=["user", "project"],
                can_use_tool=self.sdk_options.can_use_tool,  # Preserve permission callback
                resume=self._session_id,
            )

        async with ClaudeSDKClient(options=options) as client:
            await client.query(content)

            async for message in client.receive_response():
                self._conversation_history.append(message)

                # Update session ID if provided
                if isinstance(message, ResultMessage) and message.session_id:
                    self._session_id = message.session_id

                yield message

    async def stop(self, reason: str = "user_request"):
        """Stop agent execution.

        Args:
            reason: Why the agent is being stopped
        """
        self._running = False
        await self._cancel_heartbeat()

        # Cancel any pending approvals
        if self.approval_service:
            await self.approval_service.cancel_all(reason)

        await self._update_status(
            AgentStatus.TERMINATED,
            metadata={"stop_reason": reason},
        )

    async def pause(self, reason: str = "user_request"):
        """Pause agent execution.

        Args:
            reason: Why the agent is being paused
        """
        self._running = False
        await self._cancel_heartbeat()

        # Note: We don't cancel approvals on pause - they remain pending

        await self._update_status(
            AgentStatus.PAUSED,
            metadata={"paused_reason": reason},
        )

    async def _cancel_heartbeat(self) -> None:
        """Cancel and await the heartbeat task to prevent dangling coroutines."""
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._heartbeat_task
            self._heartbeat_task = None

    async def _heartbeat_loop(self):
        """Background task to update heartbeat."""
        while self._running:
            try:
                await asyncio.sleep(self.HEARTBEAT_INTERVAL)
                if self._running:
                    await self.entity_manager.update(
                        self.record.id,
                        {
                            "heartbeat_at": datetime.now(UTC).isoformat(),
                            "tokens_used": self._tokens_used,
                            "cost_usd": self._cost_usd,
                        },
                    )
            except asyncio.CancelledError:
                break
            except Exception:
                log.exception(f"Heartbeat failed for agent {self.id}")

    async def _update_status(
        self,
        status: AgentStatus,
        error: str | None = None,
        metadata: dict[str, Any] | None = None,
    ):
        """Update agent status in the graph."""
        updates: dict[str, Any] = {"status": status.value}

        if status == AgentStatus.COMPLETED:
            updates["completed_at"] = datetime.now(UTC).isoformat()
            updates["tokens_used"] = self._tokens_used
            updates["cost_usd"] = self._cost_usd

        if self._session_id:
            updates["session_id"] = self._session_id

        if error:
            updates["error_message"] = error

        if metadata:
            updates.update(metadata)

        await self.entity_manager.update(self.record.id, updates)
        self.record.status = status

    def get_conversation_history(self) -> list[dict[str, Any]]:
        """Get serializable conversation history for checkpointing."""
        return [self._serialize_message(m) for m in self._conversation_history]

    async def checkpoint(
        self,
        current_step: str | None = None,
        pending_approval_id: str | None = None,
    ) -> "AgentCheckpoint":
        """Create a checkpoint of the current agent state.

        Args:
            current_step: Optional description of current step
            pending_approval_id: Optional approval blocking the agent

        Returns:
            Created AgentCheckpoint record
        """
        from sibyl.agents.checkpoints import CheckpointManager

        manager = CheckpointManager(self.entity_manager, self.id)
        return await manager.checkpoint(
            self,
            current_step=current_step,
            pending_approval_id=pending_approval_id,
        )

    def _serialize_message(self, message: Message) -> dict[str, Any]:
        """Serialize a Message for storage."""
        result: dict[str, Any] = {}

        if isinstance(message, UserMessage):
            result["type"] = "user"
            result["content"] = message.content
        elif isinstance(message, AssistantMessage):
            result["type"] = "assistant"
            result["content"] = message.content
            if message.model:
                result["model"] = message.model
        elif isinstance(message, ResultMessage):
            result["type"] = "result"
            result["subtype"] = message.subtype
            if message.duration_ms:
                result["duration_ms"] = message.duration_ms
            if message.total_cost_usd:
                result["total_cost_usd"] = message.total_cost_usd
        else:
            # StreamEvent or other
            result["type"] = "event"

        return result
