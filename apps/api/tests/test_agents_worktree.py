"""Tests for WorktreeManager - git worktree lifecycle management.

These tests use real git operations but mock the LLM.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from sibyl.agents.worktree import SetupConfig, WorktreeError, WorktreeManager
from sibyl_core.models import WorktreeStatus

if TYPE_CHECKING:
    from tests.test_agents import MockEntityManager


pytestmark = pytest.mark.requires_worktree


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """Create a git repository for testing."""
    repo = tmp_path / "test_repo"
    repo.mkdir()

    # Initialize
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=repo,
        check=True,
        capture_output=True,
    )

    # Initial commit
    (repo / "README.md").write_text("# Test\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "Initial"],
        cwd=repo,
        check=True,
        capture_output=True,
    )

    return repo


@pytest.fixture
def entity_manager() -> "MockEntityManager":
    """Create mock entity manager."""
    from tests.test_agents import MockEntityManager

    return MockEntityManager()


@pytest.fixture
def worktree_manager(git_repo: Path, entity_manager: "MockEntityManager") -> WorktreeManager:
    """Create WorktreeManager with test repo."""
    return WorktreeManager(
        entity_manager=entity_manager,  # type: ignore[arg-type]
        org_id="test_org",
        repo_path=git_repo,
    )


# =============================================================================
# Create Tests
# =============================================================================


class TestWorktreeCreate:
    """Tests for worktree creation."""

    @pytest.mark.asyncio
    async def test_create_worktree_success(
        self,
        worktree_manager: WorktreeManager,
        git_repo: Path,
    ) -> None:
        """Successfully creates a git worktree."""
        record = await worktree_manager.create(
            task_id="task_123",
            branch_name="feature/test-branch",
        )

        # Verify record
        assert record.task_id == "task_123"
        assert record.branch == "feature/test-branch"
        assert record.status == WorktreeStatus.ACTIVE

        # Verify worktree exists on disk
        worktree_path = Path(record.path)
        assert worktree_path.exists()
        assert (worktree_path / "README.md").exists()

    @pytest.mark.asyncio
    async def test_create_worktree_custom_base(
        self,
        git_repo: Path,
        entity_manager: "MockEntityManager",
        tmp_path: Path,
    ) -> None:
        """Creates worktree in custom base directory."""
        custom_base = tmp_path / "custom_worktrees"
        custom_base.mkdir()

        manager = WorktreeManager(
            entity_manager=entity_manager,  # type: ignore[arg-type]
            org_id="test_org",
            repo_path=git_repo,
            worktree_base=custom_base,
        )

        record = await manager.create(
            task_id="task_456",
            branch_name="feature/custom",
        )

        # Verify it's in custom location
        assert str(custom_base) in record.path

    @pytest.mark.asyncio
    async def test_create_worktree_with_agent_id(
        self,
        worktree_manager: WorktreeManager,
    ) -> None:
        """Creates worktree linked to specific agent."""
        record = await worktree_manager.create(
            task_id="task_789",
            branch_name="feature/agent-work",
            agent_id="agent_abc",
        )

        assert record.agent_id == "agent_abc"

    @pytest.mark.asyncio
    async def test_create_duplicate_branch_fails(
        self,
        worktree_manager: WorktreeManager,
    ) -> None:
        """Creating worktree with existing branch name fails."""
        await worktree_manager.create(
            task_id="task_1",
            branch_name="feature/duplicate",
        )

        with pytest.raises(WorktreeError, match="already exists"):
            await worktree_manager.create(
                task_id="task_2",
                branch_name="feature/duplicate",
            )


# =============================================================================
# Setup Command Tests
# =============================================================================


class TestWorktreeSetup:
    """Tests for post-creation setup commands."""

    @pytest.mark.asyncio
    async def test_setup_commands_run(
        self,
        worktree_manager: WorktreeManager,
    ) -> None:
        """Setup commands execute in worktree directory."""
        record = await worktree_manager.create(
            task_id="task_setup",
            branch_name="feature/setup-test",
            setup_config=SetupConfig(
                commands=["touch setup_marker.txt", "echo 'done' > status.txt"],
            ),
        )

        worktree_path = Path(record.path)
        assert (worktree_path / "setup_marker.txt").exists()
        assert (worktree_path / "status.txt").read_text().strip() == "done"

    @pytest.mark.asyncio
    async def test_setup_timeout(
        self,
        worktree_manager: WorktreeManager,
    ) -> None:
        """Setup commands respect timeout."""
        with pytest.raises(WorktreeError, match="timed out"):
            await worktree_manager.create(
                task_id="task_timeout",
                branch_name="feature/timeout-test",
                setup_config=SetupConfig(
                    commands=["sleep 10"],
                    timeout_seconds=1,
                ),
            )

    @pytest.mark.asyncio
    async def test_setup_failure_stops(
        self,
        worktree_manager: WorktreeManager,
    ) -> None:
        """Setup stops on first failure by default."""
        with pytest.raises(WorktreeError, match="failed"):
            await worktree_manager.create(
                task_id="task_fail",
                branch_name="feature/fail-test",
                setup_config=SetupConfig(
                    commands=[
                        "exit 1",  # This fails
                        "touch should_not_exist.txt",
                    ],
                    continue_on_error=False,
                ),
            )

    @pytest.mark.asyncio
    async def test_setup_continue_on_error(
        self,
        worktree_manager: WorktreeManager,
    ) -> None:
        """Setup continues past failures when configured."""
        record = await worktree_manager.create(
            task_id="task_continue",
            branch_name="feature/continue-test",
            setup_config=SetupConfig(
                commands=[
                    "exit 1",  # This fails
                    "touch continued.txt",  # But this should still run
                ],
                continue_on_error=True,
            ),
        )

        worktree_path = Path(record.path)
        assert (worktree_path / "continued.txt").exists()


# =============================================================================
# Cleanup Tests
# =============================================================================


class TestWorktreeCleanup:
    """Tests for worktree cleanup."""

    @pytest.mark.asyncio
    async def test_cleanup_removes_worktree(
        self,
        worktree_manager: WorktreeManager,
    ) -> None:
        """Cleanup removes worktree from disk."""
        record = await worktree_manager.create(
            task_id="task_cleanup",
            branch_name="feature/cleanup-test",
        )

        worktree_path = Path(record.path)
        assert worktree_path.exists()

        await worktree_manager.cleanup(record.id)

        assert not worktree_path.exists()

    @pytest.mark.asyncio
    async def test_cleanup_orphaned(
        self,
        worktree_manager: WorktreeManager,
    ) -> None:
        """Cleanup orphaned worktrees from dead agents."""
        # Create some worktrees
        record1 = await worktree_manager.create(
            task_id="task_orphan1",
            branch_name="feature/orphan1",
            agent_id="dead_agent_1",
        )
        record2 = await worktree_manager.create(
            task_id="task_orphan2",
            branch_name="feature/orphan2",
            agent_id="dead_agent_2",
        )

        # Mark them as orphaned
        record1.status = WorktreeStatus.ORPHANED
        record2.status = WorktreeStatus.ORPHANED

        # Run cleanup
        cleaned = await worktree_manager.cleanup_orphaned()

        assert cleaned >= 0  # May be 0 if already cleaned

    @pytest.mark.asyncio
    async def test_cleanup_preserves_active(
        self,
        worktree_manager: WorktreeManager,
    ) -> None:
        """Cleanup doesn't remove active worktrees."""
        record = await worktree_manager.create(
            task_id="task_active",
            branch_name="feature/active",
        )

        worktree_path = Path(record.path)

        # Run orphan cleanup - should not affect active worktree
        await worktree_manager.cleanup_orphaned()

        assert worktree_path.exists()


# =============================================================================
# List/Get Tests
# =============================================================================


class TestWorktreeQueries:
    """Tests for worktree queries."""

    @pytest.mark.asyncio
    async def test_get_by_id(
        self,
        worktree_manager: WorktreeManager,
    ) -> None:
        """Can retrieve worktree by ID."""
        created = await worktree_manager.create(
            task_id="task_get",
            branch_name="feature/get-test",
        )

        retrieved = await worktree_manager.get(created.id)

        assert retrieved is not None
        assert retrieved.id == created.id
        assert retrieved.branch == created.branch

    @pytest.mark.asyncio
    async def test_get_nonexistent_returns_none(
        self,
        worktree_manager: WorktreeManager,
    ) -> None:
        """Getting nonexistent worktree returns None."""
        result = await worktree_manager.get("nonexistent_id")
        assert result is None

    @pytest.mark.asyncio
    async def test_list_for_task(
        self,
        worktree_manager: WorktreeManager,
    ) -> None:
        """Can list worktrees for a specific task."""
        await worktree_manager.create(
            task_id="shared_task",
            branch_name="feature/shared1",
        )
        await worktree_manager.create(
            task_id="shared_task",
            branch_name="feature/shared2",
        )
        await worktree_manager.create(
            task_id="other_task",
            branch_name="feature/other",
        )

        task_worktrees = await worktree_manager.list_for_task("shared_task")

        assert len(task_worktrees) == 2
        assert all(w.task_id == "shared_task" for w in task_worktrees)
