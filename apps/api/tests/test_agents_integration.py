"""Tests for IntegrationManager - merge orchestration for agent worktrees.

Tests git operations: conflict detection, rebase, merge workflows.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from sibyl.agents.integration import (
    ConflictError,
    IntegrationManager,
    IntegrationStatus,
    TestConfig as IntegrationTestConfig,  # Renamed to avoid pytest collection
    TestFailedError as IntegrationTestFailedError,  # Renamed to avoid pytest collection
)
from sibyl.agents.worktree import WorktreeManager

if TYPE_CHECKING:
    from tests.test_agents import MockEntityManager


pytestmark = pytest.mark.requires_worktree


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """Create a git repository with some history."""
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
    (repo / "README.md").write_text("# Test Project\n")
    (repo / "main.py").write_text("print('hello')\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "Initial commit"],
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


@pytest.fixture
def integration_manager(
    git_repo: Path,
    worktree_manager: WorktreeManager,
    entity_manager: "MockEntityManager",
) -> IntegrationManager:
    """Create IntegrationManager."""
    return IntegrationManager(
        entity_manager=entity_manager,  # type: ignore[arg-type]
        worktree_manager=worktree_manager,
        repo_path=git_repo,
    )


# =============================================================================
# Helpers
# =============================================================================


def run_git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run git command in repo."""
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )


def commit_file(repo: Path, filename: str, content: str, message: str) -> None:
    """Create/modify file and commit."""
    (repo / filename).write_text(content)
    run_git(repo, "add", filename)
    run_git(repo, "commit", "-m", message)


# =============================================================================
# Conflict Detection Tests
# =============================================================================


class TestConflictDetection:
    """Tests for merge conflict detection."""

    @pytest.mark.asyncio
    async def test_no_conflicts_clean_merge(
        self,
        integration_manager: IntegrationManager,
        worktree_manager: WorktreeManager,
        git_repo: Path,
    ) -> None:
        """No conflicts when branches don't touch same files."""
        # Create worktree and make non-conflicting change
        record = await worktree_manager.create(
            task_id="task_clean",
            branch_name="feature/clean",
        )

        worktree_path = Path(record.path)
        commit_file(worktree_path, "new_file.py", "# new file\n", "Add new file")

        # Check conflicts
        conflicts = await integration_manager.check_conflicts(record.id)

        assert len(conflicts) == 0

    @pytest.mark.asyncio
    async def test_detects_file_conflict(
        self,
        integration_manager: IntegrationManager,
        worktree_manager: WorktreeManager,
        git_repo: Path,
    ) -> None:
        """Detects conflict when same file modified in both branches."""
        # Create worktree
        record = await worktree_manager.create(
            task_id="task_conflict",
            branch_name="feature/conflict",
        )

        # Modify main.py in worktree
        worktree_path = Path(record.path)
        commit_file(worktree_path, "main.py", "print('from feature')\n", "Feature change")

        # Also modify main.py on main branch
        commit_file(git_repo, "main.py", "print('from main')\n", "Main change")

        # Check conflicts
        conflicts = await integration_manager.check_conflicts(record.id)

        assert len(conflicts) == 1
        assert "main.py" in conflicts[0]


# =============================================================================
# Integration Workflow Tests
# =============================================================================


class TestIntegrationWorkflow:
    """Tests for full integration workflow."""

    @pytest.mark.asyncio
    async def test_integrate_clean_branch(
        self,
        integration_manager: IntegrationManager,
        worktree_manager: WorktreeManager,
        git_repo: Path,
    ) -> None:
        """Successfully integrates a clean feature branch."""
        # Create worktree and make change
        record = await worktree_manager.create(
            task_id="task_integrate",
            branch_name="feature/integrate",
        )

        worktree_path = Path(record.path)
        commit_file(worktree_path, "feature.py", "# feature code\n", "Add feature")

        # Integrate
        result = await integration_manager.integrate_task(
            worktree_id=record.id,
            target_branch="main",
            auto_cleanup=False,  # Keep for verification
        )

        assert result.status == IntegrationStatus.MERGED
        assert result.conflicts == []

        # Verify commit is on main
        log = run_git(git_repo, "log", "--oneline", "-1")
        assert "Add feature" in log.stdout or "feature" in log.stdout.lower()

    @pytest.mark.asyncio
    async def test_integrate_with_rebase(
        self,
        integration_manager: IntegrationManager,
        worktree_manager: WorktreeManager,
        git_repo: Path,
    ) -> None:
        """Rebases branch before merging when main has advanced."""
        # Create worktree
        record = await worktree_manager.create(
            task_id="task_rebase",
            branch_name="feature/rebase",
        )

        # Make change in worktree
        worktree_path = Path(record.path)
        commit_file(worktree_path, "feature.py", "# feature\n", "Feature work")

        # Advance main with unrelated change
        commit_file(git_repo, "other.py", "# other\n", "Other work on main")

        # Integrate - should rebase first
        result = await integration_manager.integrate_task(
            worktree_id=record.id,
            target_branch="main",
            auto_cleanup=False,
        )

        assert result.status == IntegrationStatus.MERGED
        assert result.rebased is True

    @pytest.mark.asyncio
    async def test_integrate_with_conflict_fails(
        self,
        integration_manager: IntegrationManager,
        worktree_manager: WorktreeManager,
        git_repo: Path,
    ) -> None:
        """Integration fails on conflict."""
        # Create worktree
        record = await worktree_manager.create(
            task_id="task_conflict",
            branch_name="feature/will-conflict",
        )

        # Modify same file in both
        worktree_path = Path(record.path)
        commit_file(worktree_path, "main.py", "print('feature')\n", "Feature")
        commit_file(git_repo, "main.py", "print('main')\n", "Main")

        # Integration should fail
        with pytest.raises(ConflictError) as exc_info:
            await integration_manager.integrate_task(
                worktree_id=record.id,
                target_branch="main",
            )

        assert "main.py" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_integrate_cleans_up_worktree(
        self,
        integration_manager: IntegrationManager,
        worktree_manager: WorktreeManager,
        git_repo: Path,
    ) -> None:
        """Integration cleans up worktree by default."""
        record = await worktree_manager.create(
            task_id="task_cleanup",
            branch_name="feature/cleanup",
        )

        worktree_path = Path(record.path)
        commit_file(worktree_path, "feature.py", "# f\n", "Feature")

        await integration_manager.integrate_task(
            worktree_id=record.id,
            target_branch="main",
            auto_cleanup=True,
        )

        # Worktree should be gone
        assert not worktree_path.exists()


# =============================================================================
# Test Execution Tests
# =============================================================================


class TestIntegrationTests:
    """Tests for running tests during integration."""

    @pytest.mark.asyncio
    async def test_integration_runs_tests(
        self,
        integration_manager: IntegrationManager,
        worktree_manager: WorktreeManager,
        git_repo: Path,
    ) -> None:
        """Integration runs configured test command."""
        record = await worktree_manager.create(
            task_id="task_test",
            branch_name="feature/with-tests",
        )

        worktree_path = Path(record.path)
        commit_file(worktree_path, "feature.py", "# ok\n", "Feature")

        # Configure test that passes
        test_config = IntegrationTestConfig(
            command="echo 'tests passed'",
            timeout_seconds=30,
        )

        result = await integration_manager.integrate_task(
            worktree_id=record.id,
            target_branch="main",
            test_config=test_config,
            auto_cleanup=False,
        )

        assert result.status == IntegrationStatus.MERGED
        assert result.tests_passed is True

    @pytest.mark.asyncio
    async def test_integration_fails_on_test_failure(
        self,
        integration_manager: IntegrationManager,
        worktree_manager: WorktreeManager,
        git_repo: Path,
    ) -> None:
        """Integration fails when tests fail."""
        record = await worktree_manager.create(
            task_id="task_fail",
            branch_name="feature/failing-tests",
        )

        worktree_path = Path(record.path)
        commit_file(worktree_path, "broken.py", "syntax error here!!!\n", "Broken code")

        test_config = IntegrationTestConfig(
            command="exit 1",  # Simulate test failure
            timeout_seconds=30,
        )

        with pytest.raises(IntegrationTestFailedError):
            await integration_manager.integrate_task(
                worktree_id=record.id,
                target_branch="main",
                test_config=test_config,
            )


# =============================================================================
# Batch Integration Tests
# =============================================================================


class TestBatchIntegration:
    """Tests for batch integration of multiple worktrees."""

    @pytest.mark.asyncio
    async def test_batch_integrates_in_order(
        self,
        integration_manager: IntegrationManager,
        worktree_manager: WorktreeManager,
        git_repo: Path,
    ) -> None:
        """Batch integration processes worktrees in dependency order."""
        # Create multiple worktrees
        record1 = await worktree_manager.create(
            task_id="task_batch1",
            branch_name="feature/batch1",
        )
        record2 = await worktree_manager.create(
            task_id="task_batch2",
            branch_name="feature/batch2",
        )

        # Make commits
        commit_file(Path(record1.path), "batch1.py", "# 1\n", "Batch 1")
        commit_file(Path(record2.path), "batch2.py", "# 2\n", "Batch 2")

        # Integrate batch
        results = await integration_manager.integrate_batch(
            worktree_ids=[record1.id, record2.id],
            target_branch="main",
            auto_cleanup=False,
        )

        assert len(results) == 2
        assert all(r.status == IntegrationStatus.MERGED for r in results)

    @pytest.mark.asyncio
    async def test_batch_stops_on_conflict(
        self,
        integration_manager: IntegrationManager,
        worktree_manager: WorktreeManager,
        git_repo: Path,
    ) -> None:
        """Batch integration stops when conflict encountered."""
        # Create worktrees that will conflict
        record1 = await worktree_manager.create(
            task_id="task_conf1",
            branch_name="feature/conflict1",
        )
        record2 = await worktree_manager.create(
            task_id="task_conf2",
            branch_name="feature/conflict2",
        )

        # Both modify same file
        commit_file(Path(record1.path), "shared.py", "version = 1\n", "Version 1")
        commit_file(Path(record2.path), "shared.py", "version = 2\n", "Version 2")

        # First should merge, second should fail
        results = await integration_manager.integrate_batch(
            worktree_ids=[record1.id, record2.id],
            target_branch="main",
            auto_cleanup=False,
        )

        assert results[0].status == IntegrationStatus.MERGED
        assert results[1].status == IntegrationStatus.CONFLICT
