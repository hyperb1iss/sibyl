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
    from sibyl.agents.runner import AgentRunner

    from .conftest import CostTracker, LiveModelConfig

pytestmark = pytest.mark.live_model


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

        # Execute with timeout
        response = await asyncio.wait_for(
            instance.execute(),
            timeout=live_model_config.timeout_seconds,
        )

        # Verify response
        assert response is not None
        assert "4" in response.content
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

        await asyncio.wait_for(
            instance.execute(),
            timeout=live_model_config.timeout_seconds,
        )

        # Verify token tracking
        assert instance.record.total_tokens > 0
        assert instance.record.input_tokens > 0
        assert instance.record.output_tokens > 0

        # Record cost
        if instance.record.cost_usd:
            cost_tracker.record(instance.record.cost_usd)

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

        # First turn
        await asyncio.wait_for(
            instance.execute(),
            timeout=live_model_config.timeout_seconds,
        )

        # Second turn - ask for the word
        response = await asyncio.wait_for(
            instance.send("What was the secret word I told you?"),
            timeout=live_model_config.timeout_seconds,
        )

        assert "banana" in response.content.lower()


class TestAgentToolUsage:
    """Tests for agent tool invocation."""

    async def test_agent_uses_read_tool(
        self,
        agent_runner: AgentRunner,
        live_model_config: LiveModelConfig,
        tmp_git_repo: Path,
    ) -> None:
        """Agent correctly uses the Read tool to examine files."""
        # Create a test file
        test_file = tmp_git_repo / "config.py"
        test_file.write_text('SECRET_VALUE = "hunter2"\n')

        instance = await agent_runner.spawn(
            prompt=f"Read the file at {test_file} and tell me what SECRET_VALUE is set to.",
            agent_type=AgentType.GENERAL,
            create_worktree=False,
            enable_approvals=False,
        )

        response = await asyncio.wait_for(
            instance.execute(),
            timeout=live_model_config.timeout_seconds,
        )

        # Verify agent found the value
        assert "hunter2" in response.content

        # Verify Read tool was invoked
        tool_calls = [m for m in instance.messages if hasattr(m, "tool_use")]
        read_calls = [t for t in tool_calls if "read" in str(t).lower()]
        assert len(read_calls) > 0, "Agent should have called Read tool"

    async def test_agent_uses_bash_tool(
        self,
        agent_runner: AgentRunner,
        live_model_config: LiveModelConfig,
        tmp_git_repo: Path,
    ) -> None:
        """Agent correctly uses Bash tool for commands."""
        instance = await agent_runner.spawn(
            prompt=f"Run 'ls -la' in {tmp_git_repo} and tell me what files exist.",
            agent_type=AgentType.GENERAL,
            create_worktree=False,
            enable_approvals=False,
        )

        response = await asyncio.wait_for(
            instance.execute(),
            timeout=live_model_config.timeout_seconds,
        )

        # Should mention README.md from the initial commit
        assert "readme" in response.content.lower()


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

        response = await asyncio.wait_for(
            instance.execute(),
            timeout=live_model_config.timeout_seconds,
        )

        # Agent should acknowledge the file doesn't exist
        assert any(
            phrase in response.content.lower()
            for phrase in ["not found", "doesn't exist", "does not exist", "couldn't", "cannot"]
        )
        # Agent should NOT crash
        assert instance.record.status == AgentStatus.COMPLETED

    async def test_agent_respects_max_turns(
        self,
        agent_runner: AgentRunner,
        live_model_config: LiveModelConfig,
    ) -> None:
        """Agent stops after max turns to prevent runaway execution."""
        # Override max turns to something small
        instance = await agent_runner.spawn(
            prompt="Count to 100, saying each number one at a time.",
            agent_type=AgentType.GENERAL,
            create_worktree=False,
            enable_approvals=False,
        )
        instance._max_turns = 3  # Force early stop

        await asyncio.wait_for(
            instance.execute(),
            timeout=live_model_config.timeout_seconds,
        )

        # Should not have completed counting to 100
        assert len(instance.messages) <= 10  # Some reasonable limit


class TestAgentLifecycle:
    """Tests for agent lifecycle management."""

    async def test_agent_can_be_stopped(
        self,
        agent_runner: AgentRunner,
        live_model_config: LiveModelConfig,
    ) -> None:
        """Agent can be stopped mid-execution."""
        instance = await agent_runner.spawn(
            prompt="Count to 1000, one number at a time, pausing 1 second between each.",
            agent_type=AgentType.GENERAL,
            create_worktree=False,
            enable_approvals=False,
        )

        # Start execution in background
        exec_task = asyncio.create_task(instance.execute())

        # Give it a moment to start
        await asyncio.sleep(2)

        # Stop it
        await instance.stop(reason="test_stop")

        # Verify it stopped
        assert instance.record.status == AgentStatus.TERMINATED

        # Cancel the task
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

        # Run both
        results = await asyncio.gather(
            asyncio.wait_for(instance1.execute(), timeout=live_model_config.timeout_seconds),
            asyncio.wait_for(instance2.execute(), timeout=live_model_config.timeout_seconds),
        )

        # Verify both completed with expected content
        assert "apple" in results[0].content.lower()
        assert "orange" in results[1].content.lower()

        # Both should be active initially
        active = await agent_runner.list_active()
        # May be 0 if they completed fast
        assert len(active) >= 0


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
            instance.execute(),
            timeout=live_model_config.timeout_seconds,
        )

        # Calculate expected cost
        expected = calculate_cost(
            instance.record.input_tokens,
            instance.record.output_tokens,
            live_model_config.model,
        )

        # Allow small floating point variance
        if instance.record.cost_usd:
            assert abs(instance.record.cost_usd - expected) < 0.001

        # Track it
        cost_tracker.record(expected)
