"""Agent execution jobs - run Claude agents in background worker.

These jobs handle long-running AI agent tasks, keeping the API responsive
while agents work autonomously.
"""

import asyncio
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import structlog

from sibyl.agents.messages import format_agent_message, generate_workflow_reminder
from sibyl.db import AgentMessage, AgentMessageRole, AgentMessageType, get_session

log = structlog.get_logger()


def _is_valid_uuid(value: str | None) -> bool:
    """Check if a string is a valid UUID."""
    if not value:
        return False
    try:
        UUID(value)
        return True
    except (ValueError, TypeError):
        return False


async def _safe_broadcast(event: str, data: dict[str, Any], *, org_id: str | None) -> None:
    """Broadcast event via Redis pub/sub (worker runs in separate process)."""
    try:
        from sibyl.api.pubsub import publish_event

        await publish_event(event, data, org_id=org_id)
    except Exception:
        log.debug("Broadcast failed (Redis unavailable)", event=event)


async def _store_agent_message(
    agent_id: str,
    org_id: str,
    message_num: int,
    formatted: dict[str, Any],
) -> None:
    """Store agent message summary to Postgres for reload persistence.

    Only stores summarized content - full tool outputs are NOT saved.
    Real-time streaming via WebSocket shows full content during execution.
    """
    role_str = formatted.get("role", "agent")
    type_str = formatted.get("type", "text")

    # Map to enum values
    role_map: dict[str, AgentMessageRole] = {
        "assistant": AgentMessageRole.agent,
        "tool": AgentMessageRole.system,
        "system": AgentMessageRole.system,
        "user": AgentMessageRole.user,
        "unknown": AgentMessageRole.system,
    }
    role = role_map.get(role_str, AgentMessageRole.agent)

    type_map: dict[str, AgentMessageType] = {
        "text": AgentMessageType.text,
        "tool_use": AgentMessageType.tool_call,
        "tool_result": AgentMessageType.tool_result,
        "result": AgentMessageType.text,
    }
    msg_type = type_map.get(type_str, AgentMessageType.text)

    # Build content - store full content, no truncation (DB column is TEXT/unlimited)
    if type_str == "tool_use":
        content = formatted.get("preview", formatted.get("tool_name", "Tool call"))
    elif type_str == "tool_result":
        # Store full tool result content
        content = formatted.get("content", "")
    elif type_str == "multi_result":
        # Multiple results - store all content
        results = formatted.get("results", [])
        content = "\n---\n".join(r.get("content", "") for r in results)
    elif type_str == "multi_block":
        blocks = formatted.get("blocks", [])
        content = "\n".join(b.get("content", "") for b in blocks)
    else:
        content = formatted.get("content") or formatted.get("preview", "")

    # Extract tool tracking fields (stored as proper columns, not in JSONB)
    tool_id = formatted.get("tool_id")
    parent_tool_use_id = formatted.get("parent_tool_use_id")

    # Build metadata for remaining fields
    extra = {
        "icon": formatted.get("icon"),
        "tool_name": formatted.get("tool_name"),
        "is_error": formatted.get("is_error"),
    }

    # For tool calls, store full input for code viewing
    if type_str == "tool_use":
        tool_input = formatted.get("input", {})
        if tool_input:
            extra["input"] = tool_input

    # For tool results, store full content in extra as well (for UI expansion)
    if type_str == "tool_result":
        full_content = formatted.get("content", "")
        if full_content:
            extra["full_content"] = full_content

    # Remove None values
    extra = {k: v for k, v in extra.items() if v is not None}

    try:
        async with get_session() as session:
            msg = AgentMessage(
                agent_id=agent_id,
                organization_id=UUID(org_id),
                message_num=message_num,
                role=role,
                type=msg_type,
                content=content,
                tool_id=tool_id,
                parent_tool_use_id=parent_tool_use_id,
                extra=extra,
            )
            session.add(msg)
            await session.commit()
    except Exception as e:
        log.warning("Failed to store agent message", agent_id=agent_id, error=str(e))


async def _generate_and_broadcast_status_hint(
    agent_id: str,
    tool_call_id: str | None,
    tool_name: str,
    tool_input: dict[str, Any] | None,
    task_id: str | None,
    agent_type: str,
    org_id: str,
) -> None:
    """Generate a Tier 3 status hint using Haiku and broadcast it.

    This runs as a background task to avoid blocking the main agent loop.
    The hint provides a clever, contextual waiting message.
    """
    if not tool_call_id:
        return

    try:
        from sibyl.agents.status import generate_status_hint

        # Get task title if we have a task_id
        task_title = None
        if task_id:
            try:
                from sibyl_core.graph.client import get_graph_client
                from sibyl_core.graph.entities import EntityManager

                client = await get_graph_client()
                manager = EntityManager(client, group_id=org_id)
                task = await manager.get(task_id)
                if task:
                    task_title = task.name
            except Exception:  # noqa: S110 - best-effort lookup, failure is fine
                pass

        hint = generate_status_hint(tool_name, tool_input, task_title, agent_type)

        # Broadcast the hint
        await _safe_broadcast(
            "status_hint",
            {
                "agent_id": agent_id,
                "tool_call_id": tool_call_id,
                "hint": hint,
            },
            org_id=org_id,
        )

        log.debug("Broadcast status hint", agent_id=agent_id, hint=hint)

    except Exception as e:
        # Status hints are non-critical - log and continue
        log.debug("Failed to generate status hint", error=str(e))


async def run_agent_execution(  # noqa: PLR0915
    ctx: dict[str, Any],  # noqa: ARG001
    agent_id: str,
    org_id: str,
    project_id: str,
    prompt: str,
    *,
    agent_type: str = "general",
    task_id: str | None = None,
    created_by: str | None = None,
) -> dict[str, Any]:
    """Execute a Claude agent in the worker process.

    This job runs long-running AI agent tasks in the background worker,
    keeping the API responsive. Creates checkpoints only at completion.

    Args:
        ctx: arq context
        agent_id: Pre-created agent ID
        org_id: Organization ID
        project_id: Project ID
        prompt: Initial prompt for the agent
        agent_type: Type of agent
        task_id: Optional task ID
        created_by: User ID who spawned the agent

    Returns:
        Dict with execution results
    """
    from sibyl.agents import AgentRunner, WorktreeManager
    from sibyl_core.graph.client import get_graph_client
    from sibyl_core.graph.entities import EntityManager
    from sibyl_core.models import AgentCheckpoint, AgentSpawnSource, AgentStatus, AgentType

    log.info(
        "run_agent_execution_started",
        agent_id=agent_id,
        agent_type=agent_type,
        task_id=task_id,
    )

    try:
        client = await get_graph_client()
        manager = EntityManager(client, group_id=org_id)

        # Create worktree manager and agent runner
        worktree_manager = WorktreeManager(
            entity_manager=manager,
            org_id=org_id,
            project_id=project_id,
            repo_path=".",
        )

        runner = AgentRunner(
            entity_manager=manager,
            worktree_manager=worktree_manager,
            org_id=org_id,
            project_id=project_id,
        )

        # Get task if specified
        task = None
        if task_id:
            from sibyl_core.models import Task

            entity = await manager.get(task_id)
            if entity and isinstance(entity, Task):
                task = entity

        # Spawn the agent instance with pre-generated ID
        instance = await runner.spawn(
            prompt=prompt,
            agent_type=AgentType(agent_type),
            task=task,
            spawn_source=AgentSpawnSource.USER,
            create_worktree=False,
            enable_approvals=True,
            agent_id=agent_id,
        )

        # Update with created_by if provided
        if created_by:
            await manager.update(agent_id, {"created_by": created_by})

        # Broadcast that agent is now working
        await _safe_broadcast(
            "agent_status",
            {"agent_id": agent_id, "status": "working"},
            org_id=org_id,
        )

        # Track execution state (in memory only until completion)
        message_count = 0
        session_id = ""
        last_content = ""
        tool_calls: list[str] = []
        context_broadcasted = False  # Track if we've shown injected context

        # Store the initial user prompt as message #1
        message_count += 1
        initial_message = {
            "role": "user",
            "type": "text",
            "content": prompt,
            "timestamp": datetime.now(UTC).isoformat(),
            "preview": prompt[:100] + "..." if len(prompt) > 100 else prompt,
        }
        await _safe_broadcast(
            "agent_message",
            {"agent_id": agent_id, "message_num": message_count, **initial_message},
            org_id=org_id,
        )
        await _store_agent_message(agent_id, org_id, message_count, initial_message)

        # Execute agent - stream messages to UI in real-time
        log.info("run_agent_execution_starting", agent_id=agent_id)
        async for message in instance.execute():
            msg_class = type(message).__name__

            # Format message for UI (returns None for internal SDK messages)
            formatted = format_agent_message(message)
            if formatted is None:
                continue  # Skip internal messages

            message_count += 1

            log.debug(
                "run_agent_message",
                agent_id=agent_id,
                message_num=message_count,
                message_type=msg_class,
                content_preview=formatted.get("preview", "")[:100],
            )

            # Broadcast message to UI in real-time
            await _safe_broadcast(
                "agent_message",
                {
                    "agent_id": agent_id,
                    "message_num": message_count,
                    **formatted,
                },
                org_id=org_id,
            )

            # Store summarized message to Postgres for reload persistence
            await _store_agent_message(agent_id, org_id, message_count, formatted)

            # Broadcast injected Sibyl context (once, after first response)
            if not context_broadcasted and instance.workflow_tracker:
                injected = instance.workflow_tracker.injected_context
                if injected:
                    context_broadcasted = True
                    message_count += 1
                    context_message = {
                        "role": "system",
                        "type": "sibyl_context",
                        "content": injected,
                        "timestamp": datetime.now(UTC).isoformat(),
                        "preview": "Sibyl context injected",
                        "icon": "Sparkles",
                    }
                    await _safe_broadcast(
                        "agent_message",
                        {"agent_id": agent_id, "message_num": message_count, **context_message},
                        org_id=org_id,
                    )
                    await _store_agent_message(agent_id, org_id, message_count, context_message)

            # Track session ID
            if sid := getattr(message, "session_id", None):
                session_id = sid

            # Track tool calls for summary
            if "ToolUse" in msg_class or formatted.get("type") == "tool_use":
                tool_name = formatted.get("tool_name", "unknown")
                tool_calls.append(tool_name)

                # Generate and broadcast Tier 3 status hint (fire-and-forget)
                tool_id = formatted.get("tool_id")
                tool_input = formatted.get("input")
                _ = asyncio.create_task(  # noqa: RUF006 - fire-and-forget intentional
                    _generate_and_broadcast_status_hint(
                        agent_id=agent_id,
                        tool_call_id=tool_id,
                        tool_name=tool_name,
                        tool_input=tool_input,
                        task_id=task_id,
                        agent_type=agent_type,
                        org_id=org_id,
                    )
                )

            # Keep last meaningful content for summary
            if formatted.get("content") and formatted.get("type") != "tool_result":
                last_content = formatted.get("content", "")[:500]

        # Check workflow completion and send follow-up if needed
        # Only for substantive work (5+ tool calls with code changes)
        if instance.workflow_tracker and instance.workflow_tracker.should_remind():
            workflow_summary = instance.workflow_tracker.get_workflow_summary()
            log.info("run_agent_workflow_reminder", agent_id=agent_id, **workflow_summary)

            # Send follow-up to remind about Sibyl workflow
            follow_up_prompt = generate_workflow_reminder(workflow_summary)

            # Stream follow-up responses
            async for message in instance.send_message(follow_up_prompt):
                formatted = format_agent_message(message)
                if formatted is None:
                    continue

                message_count += 1

                await _safe_broadcast(
                    "agent_message",
                    {"agent_id": agent_id, "message_num": message_count, **formatted},
                    org_id=org_id,
                )
                await _store_agent_message(agent_id, org_id, message_count, formatted)

                # Track tool calls
                if "ToolUse" in type(message).__name__ or formatted.get("type") == "tool_use":
                    tool_name = formatted.get("tool_name", "unknown")
                    tool_calls.append(tool_name)

                # Update last content
                if formatted.get("content") and formatted.get("type") != "tool_result":
                    last_content = formatted.get("content", "")[:500]

        # Create checkpoint only on completion (summary, not full history)
        from uuid import uuid4

        checkpoint_id = f"chkpt_{uuid4().hex[:12]}"
        summary = f"Completed {message_count} turns. Tools: {', '.join(tool_calls[-5:]) or 'none'}"
        checkpoint = AgentCheckpoint(
            id=checkpoint_id,
            name=f"checkpoint-{agent_id[-8:]}",
            agent_id=agent_id,
            session_id=session_id,
            conversation_history=[
                {
                    "role": "user",
                    "content": prompt,
                    "timestamp": datetime.now(UTC).isoformat(),
                    "type": "text",
                },
                {
                    "role": "system",
                    "content": summary,
                    "timestamp": datetime.now(UTC).isoformat(),
                    "type": "text",
                },
            ],
            current_step=last_content[:200] if last_content else None,
        )
        await manager.create_direct(checkpoint)

        # Update agent status to completed with session_id for resumption
        await manager.update(
            agent_id,
            {
                "status": AgentStatus.COMPLETED.value,
                "conversation_turns": message_count,
                "session_id": session_id,  # Store for future resume
            },
        )

        result = {
            "agent_id": agent_id,
            "status": "completed",
            "turns": message_count,
            "tools_used": len(tool_calls),
        }

        # Broadcast completion via WebSocket
        await _safe_broadcast(
            "agent_status",
            {"agent_id": agent_id, "status": "completed", "turns": message_count},
            org_id=org_id,
        )

        log.info("run_agent_execution_completed", **result)
        return result

    except Exception as e:
        log.exception("run_agent_execution_failed", agent_id=agent_id, error=str(e))

        # Update agent status to failed
        try:
            client = await get_graph_client()
            manager = EntityManager(client, group_id=org_id)
            await manager.update(
                agent_id,
                {
                    "status": AgentStatus.FAILED.value,
                    "error_message": str(e),
                },
            )
        except Exception:
            log.warning("Failed to update agent status on error", agent_id=agent_id)

        # Broadcast failure via WebSocket
        await _safe_broadcast(
            "agent_status",
            {"agent_id": agent_id, "status": "failed", "error": str(e)},
            org_id=org_id,
        )

        raise


async def resume_agent_execution(  # noqa: PLR0915
    ctx: dict[str, Any],  # noqa: ARG001
    agent_id: str,
    org_id: str,
    prompt: str = "Continue from where you left off.",
) -> dict[str, Any]:
    """Resume an agent using Claude's session management.

    Called when user sends a message to a terminal agent or clicks resume.
    Uses the agent's stored session_id - Claude handles conversation history.

    Args:
        ctx: arq context
        agent_id: Agent ID to resume
        org_id: Organization ID
        prompt: User message or continuation prompt

    Returns:
        Dict with execution results
    """
    from sibyl.agents import AgentRunner, WorktreeManager
    from sibyl_core.graph.client import get_graph_client
    from sibyl_core.graph.entities import EntityManager
    from sibyl_core.models import AgentStatus, EntityType

    log.info("resume_agent_execution_started", agent_id=agent_id, prompt_preview=prompt[:100])

    try:
        client = await get_graph_client()
        manager = EntityManager(client, group_id=org_id)

        # Get agent record
        agent = await manager.get(agent_id)
        if not agent or agent.entity_type != EntityType.AGENT:
            raise ValueError(f"Agent not found: {agent_id}")

        # Get session_id from agent metadata (stored from previous execution)
        agent_meta = agent.metadata or {}
        session_id = agent_meta.get("session_id")
        project_id = agent_meta.get("project_id") or ""

        # Validate session_id is a proper UUID (not a placeholder like "user-initiated")
        if not _is_valid_uuid(session_id):
            raise ValueError(
                f"Agent {agent_id} has invalid session_id '{session_id}' - cannot resume. "
                "This agent was created before session tracking was implemented."
            )

        log.info("resume_using_session", agent_id=agent_id, session_id=session_id)

        # Create runner and resume
        worktree_manager = WorktreeManager(
            entity_manager=manager,
            org_id=org_id,
            project_id=project_id,
            repo_path=".",
        )

        runner = AgentRunner(
            entity_manager=manager,
            worktree_manager=worktree_manager,
            org_id=org_id,
            project_id=project_id,
        )

        # Resume using agent's session_id - Claude handles conversation history
        instance = await runner.resume_agent(
            agent_id=agent_id,
            session_id=session_id,
            prompt=prompt,
            enable_approvals=True,
        )

        # Broadcast that agent is now working
        await _safe_broadcast(
            "agent_status",
            {"agent_id": agent_id, "status": "working"},
            org_id=org_id,
        )

        # Track execution state
        message_count = 0
        new_session_id = session_id  # May get updated during execution
        tool_calls: list[str] = []
        context_broadcasted = False

        # Execute resumed agent - stream messages to UI
        log.info("resume_agent_execution_streaming", agent_id=agent_id)
        async for message in instance.execute():
            msg_class = type(message).__name__
            formatted = format_agent_message(message)
            if formatted is None:
                continue

            message_count += 1

            log.debug(
                "resume_agent_message",
                agent_id=agent_id,
                message_num=message_count,
                message_type=msg_class,
            )

            # Broadcast to UI
            await _safe_broadcast(
                "agent_message",
                {"agent_id": agent_id, "message_num": message_count, **formatted},
                org_id=org_id,
            )
            await _store_agent_message(agent_id, org_id, message_count, formatted)

            # Broadcast Sibyl context if available
            if not context_broadcasted and instance.workflow_tracker:
                injected = instance.workflow_tracker.injected_context
                if injected:
                    context_broadcasted = True
                    message_count += 1
                    context_message = {
                        "role": "system",
                        "type": "sibyl_context",
                        "content": injected,
                        "timestamp": datetime.now(UTC).isoformat(),
                        "preview": "Sibyl context injected",
                        "icon": "Sparkles",
                    }
                    await _safe_broadcast(
                        "agent_message",
                        {"agent_id": agent_id, "message_num": message_count, **context_message},
                        org_id=org_id,
                    )
                    await _store_agent_message(agent_id, org_id, message_count, context_message)

            # Track session ID (may update if forked)
            if sid := getattr(message, "session_id", None):
                new_session_id = sid

            # Track tool calls
            if "ToolUse" in msg_class or formatted.get("type") == "tool_use":
                tool_name = formatted.get("tool_name", "unknown")
                tool_calls.append(tool_name)

        # Update agent with new session_id and completion status
        await manager.update(
            agent_id,
            {
                "status": AgentStatus.COMPLETED.value,
                "conversation_turns": message_count,
                "session_id": new_session_id,  # Store for next resume
            },
        )

        result = {
            "agent_id": agent_id,
            "status": "completed",
            "turns": message_count,
            "resumed": True,
        }

        await _safe_broadcast(
            "agent_status",
            {"agent_id": agent_id, "status": "completed", "turns": message_count},
            org_id=org_id,
        )

        log.info("resume_agent_execution_completed", **result)
        return result

    except Exception as e:
        log.exception("resume_agent_execution_failed", agent_id=agent_id, error=str(e))

        try:
            client = await get_graph_client()
            manager = EntityManager(client, group_id=org_id)
            await manager.update(
                agent_id, {"status": AgentStatus.FAILED.value, "error_message": str(e)}
            )
        except Exception:
            log.warning("Failed to update agent status on error", agent_id=agent_id)

        await _safe_broadcast(
            "agent_status",
            {"agent_id": agent_id, "status": "failed", "error": str(e)},
            org_id=org_id,
        )

        raise


async def generate_status_hint(
    ctx: dict[str, Any],  # noqa: ARG001
    agent_id: str,
    tool_call_id: str,
    tool_name: str,
    tool_input: dict[str, Any] | None = None,
    task_title: str | None = None,
    agent_type: str | None = None,
    org_id: str | None = None,
) -> dict[str, Any]:
    """Generate a contextual status hint for an agent tool call.

    Uses Claude Haiku to generate clever, playful status messages
    based on the tool being used and optional task context.

    Args:
        ctx: arq context
        agent_id: Agent making the tool call
        tool_call_id: Unique ID of the tool call
        tool_name: Name of the tool (Read, Edit, Grep, etc.)
        tool_input: Tool parameters
        task_title: Optional Sibyl task for context
        agent_type: Optional agent type for context
        org_id: Organization ID for broadcast scope

    Returns:
        Dict with generated hint
    """
    from sibyl.agents.status import generate_status_hint as gen_hint

    try:
        hint = gen_hint(tool_name, tool_input, task_title, agent_type)

        # Broadcast the hint via pubsub
        await _safe_broadcast(
            "status_hint",
            {
                "agent_id": agent_id,
                "tool_call_id": tool_call_id,
                "hint": hint,
            },
            org_id=org_id,
        )

        log.debug("Generated status hint", agent_id=agent_id, hint=hint)
        return {"success": True, "hint": hint}

    except Exception as e:
        log.warning("Failed to generate status hint", error=str(e))
        return {"success": False, "error": str(e)}
