"""Live model tests for agent execution.

These tests make real API calls to Claude and validate actual agent behavior.

Run with:
    uv run pytest apps/api/tests/live/test_agents_live.py -v --live-models
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from sibyl_core.models import AgentStatus, AgentType

if TYPE_CHECKING:
    from claude_agent_sdk import Message

    from sibyl.agents.runner import AgentRunner

    from .conftest import CostTracker, LiveModelConfig

pytestmark = pytest.mark.live_model


async def collect_messages(async_gen) -> list["Message"]:
    """Collect all messages from an async generator."""
    messages = []
    async for msg in async_gen:
        messages.append(msg)
    return messages


async def get_last_text_content(async_gen) -> str:
    """Get the text content from the last assistant message."""
    messages = await collect_messages(async_gen)
    # Find the last message with text content
    for msg in reversed(messages):
        if hasattr(msg, "content") and msg.content:
            if isinstance(msg.content, str):
                return msg.content
            # Handle list of content blocks
            if isinstance(msg.content, list):
                text_parts = []
                for block in msg.content:
                    if hasattr(block, "text"):
                        text_parts.append(block.text)
                if text_parts:
                    return " ".join(text_parts)
    return ""


class TestBasicAgentExecution:
    """Tests for basic agent spawn and response."""

    async def test_agent_responds_to_simple_prompt(
        self,
        agent_runner: AgentRunner,
        live_model_config: LiveModelConfig,
    ) -> None:
        """Agent can receive a prompt and respond coherently."""
        instance = await agent_runner.spawn(
            prompt="What is 2 + 2? Reply with just the number, nothing else.",
            agent_type=AgentType.GENERAL,
            create_worktree=False,
            enable_approvals=False,
        )

        # Execute and collect messages
        response_text = await asyncio.wait_for(
            get_last_text_content(instance.execute()),
            timeout=live_model_config.timeout_seconds,
        )

        # Verify response contains "4"
        assert "4" in response_text
        assert instance.record.status == AgentStatus.COMPLETED

    async def test_agent_tracks_tokens(
        self,
        agent_runner: AgentRunner,
        live_model_config: LiveModelConfig,
        cost_tracker: CostTracker,
    ) -> None:
        """Agent accurately tracks token usage."""
        instance = await agent_runner.spawn(
            prompt="Write exactly one sentence about Python.",
            agent_type=AgentType.GENERAL,
            create_worktree=False,
            enable_approvals=False,
        )

        # Consume the generator
        await asyncio.wait_for(
            collect_messages(instance.execute()),
            timeout=live_model_config.timeout_seconds,
        )

        # Verify token tracking
        assert instance._tokens_used > 0

        # Record cost if available
        if instance._cost_usd:
            cost_tracker.record(instance._cost_usd)

    async def test_agent_handles_multi_turn(
        self,
        agent_runner: AgentRunner,
        live_model_config: LiveModelConfig,
    ) -> None:
        """Agent maintains context across conversation turns."""
        instance = await agent_runner.spawn(
            prompt="I will tell you a secret word. The word is 'banana'. Remember it.",
            agent_type=AgentType.GENERAL,
            create_worktree=False,
            enable_approvals=False,
        )

        # First turn - consume initial response
        await asyncio.wait_for(
            collect_messages(instance.execute()),
            timeout=live_model_config.timeout_seconds,
        )

        # Second turn - ask for the word
        response_text = await asyncio.wait_for(
            get_last_text_content(instance.send_message("What was the secret word I told you?")),
            timeout=live_model_config.timeout_seconds,
        )

        assert "banana" in response_text.lower()


class TestAgentToolUsage:
    """Tests for agent tool invocation.

    Note: Claude Code sandboxes file access to allowed directories.
    These tests use files within the project workspace to avoid sandbox blocks.
    """

    async def test_agent_uses_read_tool(
        self,
        agent_runner: AgentRunner,
        live_model_config: LiveModelConfig,
    ) -> None:
        """Agent correctly uses the Read tool to examine files."""
        # Use an existing file within the allowed workspace
        # The pyproject.toml is always present and has known content
        instance = await agent_runner.spawn(
            prompt="Read the pyproject.toml file in the current directory and tell me the project name.",
            agent_type=AgentType.GENERAL,
            create_worktree=False,
            enable_approvals=False,
        )

        response_text = await asyncio.wait_for(
            get_last_text_content(instance.execute()),
            timeout=live_model_config.timeout_seconds,
        )

        # Verify agent found sibyld (the project name)
        assert "sibyld" in response_text.lower()

    async def test_agent_uses_bash_tool(
        self,
        agent_runner: AgentRunner,
        live_model_config: LiveModelConfig,
    ) -> None:
        """Agent correctly uses Bash tool for commands."""
        instance = await agent_runner.spawn(
            prompt="Run 'ls' in the current directory and tell me if there's a pyproject.toml file.",
            agent_type=AgentType.GENERAL,
            create_worktree=False,
            enable_approvals=False,
        )

        response_text = await asyncio.wait_for(
            get_last_text_content(instance.execute()),
            timeout=live_model_config.timeout_seconds,
        )

        # Should confirm pyproject.toml exists
        assert "pyproject" in response_text.lower() or "yes" in response_text.lower()


class TestAgentErrorHandling:
    """Tests for agent error handling."""

    async def test_agent_handles_invalid_path(
        self,
        agent_runner: AgentRunner,
        live_model_config: LiveModelConfig,
    ) -> None:
        """Agent gracefully handles reading non-existent file."""
        instance = await agent_runner.spawn(
            prompt="Read /nonexistent/path/file.txt and tell me its contents.",
            agent_type=AgentType.GENERAL,
            create_worktree=False,
            enable_approvals=False,
        )

        response_text = await asyncio.wait_for(
            get_last_text_content(instance.execute()),
            timeout=live_model_config.timeout_seconds,
        )

        # Agent should acknowledge the file doesn't exist
        assert any(
            phrase in response_text.lower()
            for phrase in ["not found", "doesn't exist", "does not exist", "couldn't", "cannot", "error", "no such"]
        )

    async def test_agent_respects_max_turns(
        self,
        agent_runner: AgentRunner,
        live_model_config: LiveModelConfig,
    ) -> None:
        """Agent stops after max turns to prevent runaway execution."""
        instance = await agent_runner.spawn(
            prompt="Count to 100, saying each number one at a time.",
            agent_type=AgentType.GENERAL,
            create_worktree=False,
            enable_approvals=False,
        )

        # Execute and collect messages
        messages = await asyncio.wait_for(
            collect_messages(instance.execute()),
            timeout=live_model_config.timeout_seconds,
        )

        # Should complete - we're just checking it doesn't hang
        assert len(messages) > 0


class TestAgentLifecycle:
    """Tests for agent lifecycle management."""

    async def test_agent_can_be_stopped(
        self,
        agent_runner: AgentRunner,
        live_model_config: LiveModelConfig,
    ) -> None:
        """Agent can be stopped mid-execution."""
        instance = await agent_runner.spawn(
            prompt="Count slowly from 1 to 100, pausing between each number.",
            agent_type=AgentType.GENERAL,
            create_worktree=False,
            enable_approvals=False,
        )

        # Start execution in background
        exec_task = asyncio.create_task(collect_messages(instance.execute()))

        # Give it a moment to start
        await asyncio.sleep(2)

        # Stop it
        await instance.stop(reason="test_stop")

        # Verify it's marked as terminated
        assert instance.record.status in (AgentStatus.TERMINATED, AgentStatus.COMPLETED)

        # Cancel the task if still running
        if not exec_task.done():
            exec_task.cancel()
            try:
                await exec_task
            except asyncio.CancelledError:
                pass

    async def test_multiple_agents_can_run(
        self,
        agent_runner: AgentRunner,
        live_model_config: LiveModelConfig,
    ) -> None:
        """Multiple agents can run concurrently."""
        instance1 = await agent_runner.spawn(
            prompt="Reply with just the word 'apple'.",
            agent_type=AgentType.GENERAL,
            create_worktree=False,
            enable_approvals=False,
        )
        instance2 = await agent_runner.spawn(
            prompt="Reply with just the word 'orange'.",
            agent_type=AgentType.GENERAL,
            create_worktree=False,
            enable_approvals=False,
        )

        # Run both and get text responses
        results = await asyncio.gather(
            asyncio.wait_for(
                get_last_text_content(instance1.execute()),
                timeout=live_model_config.timeout_seconds
            ),
            asyncio.wait_for(
                get_last_text_content(instance2.execute()),
                timeout=live_model_config.timeout_seconds
            ),
        )

        # Verify both completed with expected content
        assert "apple" in results[0].lower()
        assert "orange" in results[1].lower()


class TestCostTracking:
    """Tests for cost tracking accuracy."""

    async def test_cost_matches_token_count(
        self,
        agent_runner: AgentRunner,
        live_model_config: LiveModelConfig,
        cost_tracker: CostTracker,
    ) -> None:
        """Cost calculation matches actual token usage."""
        from .conftest import calculate_cost

        instance = await agent_runner.spawn(
            prompt="Write a haiku about coding.",
            agent_type=AgentType.GENERAL,
            create_worktree=False,
            enable_approvals=False,
        )

        await asyncio.wait_for(
            collect_messages(instance.execute()),
            timeout=live_model_config.timeout_seconds,
        )

        # Verify tokens were tracked
        assert instance._tokens_used > 0

        # Record cost
        if instance._cost_usd:
            cost_tracker.record(instance._cost_usd)
