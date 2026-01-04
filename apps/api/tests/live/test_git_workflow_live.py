"""Live model tests for agent git workflow operations.

These tests validate agents working with git worktrees, making commits,
and integrating changes back to main.

Run with:
    uv run pytest apps/api/tests/live/test_git_workflow_live.py -v --live-models
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from sibyl_core.models import AgentStatus, AgentType

if TYPE_CHECKING:
    from sibyl.agents.runner import AgentRunner

    from .conftest import LiveModelConfig

pytestmark = pytest.mark.live_model


# =============================================================================
# Helpers
# =============================================================================


def run_git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run git command in repo."""
    return subprocess.run(  # noqa: S603
        ["git", *args],  # noqa: S607
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )


def get_current_branch(repo: Path) -> str:
    """Get the current branch name."""
    result = run_git(repo, "branch", "--show-current")
    return result.stdout.strip()


def get_commit_messages(repo: Path, count: int = 5) -> list[str]:
    """Get recent commit messages."""
    result = run_git(repo, "log", f"-{count}", "--oneline", "--format=%s")
    return result.stdout.strip().split("\n")


async def collect_messages(async_gen) -> list:
    """Collect all messages from an async generator."""
    messages = []
    async for msg in async_gen:
        messages.append(msg)
    return messages


async def get_last_text_content(async_gen) -> str:
    """Get the text content from the last assistant message."""
    messages = await collect_messages(async_gen)
    for msg in reversed(messages):
        if hasattr(msg, "content") and msg.content:
            if isinstance(msg.content, str):
                return msg.content
            if isinstance(msg.content, list):
                text_parts = [block.text for block in msg.content if hasattr(block, "text")]
                if text_parts:
                    return " ".join(text_parts)
    return ""


# =============================================================================
# Agent Worktree Tests
# =============================================================================


class TestAgentWorktreeOperations:
    """Tests for agents working in git worktrees."""

    async def test_agent_works_in_worktree(
        self,
        agent_runner: AgentRunner,
        live_model_config: LiveModelConfig,
        tmp_git_repo: Path,
    ) -> None:
        """Agent can work in an isolated worktree."""
        # Spawn agent with worktree creation
        instance = await agent_runner.spawn(
            prompt="Create a file called 'hello.py' with a simple hello world function.",
            agent_type=AgentType.IMPLEMENTER,
            create_worktree=True,
            enable_approvals=False,
        )

        # Execute agent
        await asyncio.wait_for(
            collect_messages(instance.execute()),
            timeout=live_model_config.timeout_seconds,
        )

        # Verify agent completed
        assert instance.record.status == AgentStatus.COMPLETED

        # Verify worktree was created
        assert instance.worktree_path is not None
        assert instance.worktree_path.exists()

        # Verify file was created in worktree
        hello_file = instance.worktree_path / "hello.py"
        assert hello_file.exists(), f"Expected hello.py in {instance.worktree_path}"

        # Verify main branch is unchanged
        main_hello = tmp_git_repo / "hello.py"
        assert not main_hello.exists(), "Main branch should be unchanged"

    async def test_agent_commits_work(
        self,
        agent_runner: AgentRunner,
        live_model_config: LiveModelConfig,
        tmp_git_repo: Path,
    ) -> None:
        """Agent creates commits for its work."""
        instance = await agent_runner.spawn(
            prompt=(
                "Create a file called 'feature.py' with a simple add function, "
                "then commit it with message 'Add feature.py with add function'."
            ),
            agent_type=AgentType.IMPLEMENTER,
            create_worktree=True,
            enable_approvals=False,
        )

        await asyncio.wait_for(
            collect_messages(instance.execute()),
            timeout=live_model_config.timeout_seconds,
        )

        assert instance.record.status == AgentStatus.COMPLETED
        assert instance.worktree_path is not None

        # Verify commit was made in worktree
        commits = get_commit_messages(instance.worktree_path)
        assert any("feature" in msg.lower() or "add" in msg.lower() for msg in commits), (
            f"Expected commit about feature.py, got: {commits}"
        )

    async def test_agent_modifies_existing_file(
        self,
        agent_runner: AgentRunner,
        live_model_config: LiveModelConfig,
        tmp_git_repo: Path,
    ) -> None:
        """Agent can modify existing files in worktree."""
        # Create a file to modify
        (tmp_git_repo / "config.py").write_text("DEBUG = False\n")
        run_git(tmp_git_repo, "add", "config.py")
        run_git(tmp_git_repo, "commit", "-m", "Add config.py")

        instance = await agent_runner.spawn(
            prompt="Read config.py and change DEBUG to True.",
            agent_type=AgentType.IMPLEMENTER,
            create_worktree=True,
            enable_approvals=False,
        )

        await asyncio.wait_for(
            collect_messages(instance.execute()),
            timeout=live_model_config.timeout_seconds,
        )

        assert instance.record.status == AgentStatus.COMPLETED
        assert instance.worktree_path is not None

        # Verify file was modified in worktree
        config_content = (instance.worktree_path / "config.py").read_text()
        assert "True" in config_content, f"Expected DEBUG = True, got: {config_content}"

        # Verify main branch is unchanged
        main_config = (tmp_git_repo / "config.py").read_text()
        assert "False" in main_config, "Main branch should be unchanged"


# =============================================================================
# Integration Tests
# =============================================================================


class TestAgentIntegration:
    """Tests for integrating agent work back to main."""

    async def test_integrate_agent_work(
        self,
        agent_runner: AgentRunner,
        live_model_config: LiveModelConfig,
        tmp_git_repo: Path,
    ) -> None:
        """Agent work can be integrated back to main branch."""
        # Spawn agent and do work
        instance = await agent_runner.spawn(
            prompt="Create a file called 'integration_test.py' with a test function.",
            agent_type=AgentType.IMPLEMENTER,
            create_worktree=True,
            enable_approvals=False,
        )

        await asyncio.wait_for(
            collect_messages(instance.execute()),
            timeout=live_model_config.timeout_seconds,
        )

        assert instance.worktree_path is not None

        # Commit the work if agent didn't
        try:
            run_git(instance.worktree_path, "add", ".")
            run_git(instance.worktree_path, "commit", "-m", "Agent work")
        except subprocess.CalledProcessError:
            pass  # Already committed

        # Get the branch name from worktree
        branch = get_current_branch(instance.worktree_path)

        # Merge to main
        run_git(tmp_git_repo, "merge", branch, "--no-edit")

        # Verify file is now on main
        main_file = tmp_git_repo / "integration_test.py"
        assert main_file.exists(), "File should be merged to main"


# =============================================================================
# Multi-Agent Collaboration Tests
# =============================================================================


class TestMultiAgentGitWorkflow:
    """Tests for multiple agents working on related tasks."""

    async def test_sequential_agent_work(
        self,
        agent_runner: AgentRunner,
        live_model_config: LiveModelConfig,
        tmp_git_repo: Path,
    ) -> None:
        """Multiple agents can work sequentially on a codebase."""
        # First agent creates a module
        agent1 = await agent_runner.spawn(
            prompt="Create a file called 'math_utils.py' with a function called 'square' that returns x * x.",
            agent_type=AgentType.IMPLEMENTER,
            create_worktree=True,
            enable_approvals=False,
        )

        await asyncio.wait_for(
            collect_messages(agent1.execute()),
            timeout=live_model_config.timeout_seconds,
        )

        assert agent1.record.status == AgentStatus.COMPLETED
        assert agent1.worktree_path is not None

        # Commit and merge first agent's work
        try:
            run_git(agent1.worktree_path, "add", ".")
            run_git(agent1.worktree_path, "commit", "-m", "Add math_utils.py")
        except subprocess.CalledProcessError:
            pass

        # Merge to main manually for this test
        branch = get_current_branch(agent1.worktree_path)
        run_git(tmp_git_repo, "merge", branch, "--no-edit")

        # Second agent extends the module
        agent2 = await agent_runner.spawn(
            prompt="Read math_utils.py and add a 'cube' function that returns x * x * x.",
            agent_type=AgentType.IMPLEMENTER,
            create_worktree=True,
            enable_approvals=False,
        )

        await asyncio.wait_for(
            collect_messages(agent2.execute()),
            timeout=live_model_config.timeout_seconds,
        )

        assert agent2.record.status == AgentStatus.COMPLETED
        assert agent2.worktree_path is not None

        # Verify second agent's work includes cube function
        math_utils = (agent2.worktree_path / "math_utils.py").read_text()
        assert "cube" in math_utils.lower(), f"Expected cube function, got: {math_utils}"
        assert "square" in math_utils.lower(), "Should still have square function"
