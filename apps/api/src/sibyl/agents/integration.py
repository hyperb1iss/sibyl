"""IntegrationManager for merging agent worktrees.

Orchestrates the merge process from agent branches back to target (main/develop):
- Rebase onto target to ensure clean history
- Run configured test suite before merge
- Fast-forward merge for clean integration
- Handle conflicts with human-in-the-loop
"""

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, cast

import structlog

from sibyl.agents.worktree import WorktreeManager
from sibyl_core.models import EntityType, Task, WorktreeRecord, WorktreeStatus

if TYPE_CHECKING:
    from sibyl_core.graph import EntityManager

log = structlog.get_logger()


class IntegrationStatus(StrEnum):
    """Status of an integration attempt."""

    PENDING = "pending"
    REBASING = "rebasing"
    TESTING = "testing"
    READY = "ready"  # Tests passed, ready for merge
    MERGING = "merging"
    MERGED = "merged"
    CONFLICT = "conflict"
    TEST_FAILED = "test_failed"
    FAILED = "failed"


@dataclass
class IntegrationResult:
    """Result of an integration attempt."""

    status: IntegrationStatus
    worktree_id: str
    branch: str
    target_branch: str
    commit_sha: str | None = None
    error_message: str | None = None
    conflict_files: list[str] = field(default_factory=list)
    test_output: str | None = None
    merged_at: datetime | None = None


@dataclass
class TestConfig:
    """Configuration for pre-merge test execution."""

    commands: list[str] = field(default_factory=list)
    timeout_seconds: int = 300
    require_passing: bool = True


class IntegrationError(Exception):
    """Base exception for integration operations."""


class ConflictError(IntegrationError):
    """Raised when merge conflicts are detected."""

    def __init__(self, message: str, conflict_files: list[str]):
        super().__init__(message)
        self.conflict_files = conflict_files


class TestFailedError(IntegrationError):
    """Raised when pre-merge tests fail."""

    def __init__(self, message: str, output: str):
        super().__init__(message)
        self.output = output


class IntegrationManager:
    """Manages merging agent worktrees back to target branches.

    Handles the full merge workflow:
    1. Fetch latest target branch
    2. Rebase worktree onto target
    3. Run pre-merge tests
    4. Fast-forward merge to target
    5. Clean up worktree

    Supports both single-task integration and batch integration
    with dependency ordering.
    """

    def __init__(
        self,
        entity_manager: "EntityManager",
        worktree_manager: WorktreeManager,
        org_id: str,
        project_id: str,
        repo_path: str | Path,
    ):
        """Initialize IntegrationManager.

        Args:
            entity_manager: Graph client for persistence
            worktree_manager: Worktree manager for git operations
            org_id: Organization UUID
            project_id: Project UUID
            repo_path: Path to the main git repository
        """
        self.entity_manager = entity_manager
        self.worktree_manager = worktree_manager
        self.org_id = org_id
        self.project_id = project_id
        self.repo_path = Path(repo_path).resolve()

    async def _run_git(
        self,
        *args: str,
        cwd: Path | None = None,
        check: bool = True,
    ) -> tuple[str, str, int]:
        """Run a git command asynchronously.

        Returns:
            Tuple of (stdout, stderr, returncode)
        """
        cwd = cwd or self.repo_path
        cmd = ["git", *args]

        log.debug(f"Running: {' '.join(cmd)} in {cwd}")

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()

        stdout_str = stdout.decode().strip()
        stderr_str = stderr.decode().strip()

        if check and proc.returncode != 0:
            raise IntegrationError(f"Git command failed: {stderr_str or stdout_str}")

        return stdout_str, stderr_str, proc.returncode or 0

    async def _run_tests(
        self,
        worktree_path: Path,
        config: TestConfig,
    ) -> tuple[bool, str]:
        """Run pre-merge tests in the worktree.

        Args:
            worktree_path: Path to the worktree
            config: Test configuration

        Returns:
            Tuple of (success, output)
        """
        if not config.commands:
            return True, "No tests configured"

        outputs: list[str] = []

        for cmd in config.commands:
            log.info(f"Running test: {cmd}")
            try:
                proc = await asyncio.create_subprocess_shell(
                    cmd,
                    cwd=worktree_path,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
                stdout, _ = await asyncio.wait_for(
                    proc.communicate(),
                    timeout=config.timeout_seconds,
                )
                output = stdout.decode()
                outputs.append(f"$ {cmd}\n{output}")

                if proc.returncode != 0:
                    log.warning(f"Test failed: {cmd}")
                    return False, "\n".join(outputs)

            except TimeoutError:
                outputs.append(f"$ {cmd}\nTIMEOUT after {config.timeout_seconds}s")
                return False, "\n".join(outputs)

            except Exception as e:
                outputs.append(f"$ {cmd}\nERROR: {e}")
                return False, "\n".join(outputs)

        return True, "\n".join(outputs)

    async def check_conflicts(
        self,
        worktree_id: str,
        target_branch: str = "main",
    ) -> list[str]:
        """Check for merge conflicts without actually merging.

        Uses git merge-tree to simulate the merge and detect conflicts.

        Args:
            worktree_id: Worktree record ID
            target_branch: Branch to check conflicts against

        Returns:
            List of conflicting file paths (empty if no conflicts)
        """
        record = await self.worktree_manager.get(worktree_id)
        if not record:
            raise IntegrationError(f"Worktree not found: {worktree_id}")

        worktree_path = Path(record.path)
        if not worktree_path.exists():
            raise IntegrationError(f"Worktree path missing: {record.path}")

        # Fetch latest target
        await self._run_git("fetch", "origin", target_branch, cwd=worktree_path, check=False)

        # Get merge base
        merge_base, _, _ = await self._run_git(
            "merge-base",
            f"origin/{target_branch}",
            "HEAD",
            cwd=worktree_path,
        )

        # Use merge-tree to check for conflicts (available in git 2.38+)
        _stdout, stderr, returncode = await self._run_git(
            "merge-tree",
            "--write-tree",
            merge_base,
            f"origin/{target_branch}",
            "HEAD",
            cwd=worktree_path,
            check=False,
        )

        if returncode == 0:
            return []  # No conflicts

        # Parse conflict output
        conflict_files = []
        for line in stderr.split("\n"):
            if line.startswith("CONFLICT"):
                # Extract filename from "CONFLICT (content): Merge conflict in <file>"
                if " in " in line:
                    filename = line.split(" in ")[-1].strip()
                    conflict_files.append(filename)

        return conflict_files

    async def rebase_onto_target(
        self,
        worktree_id: str,
        target_branch: str = "main",
    ) -> IntegrationResult:
        """Rebase worktree branch onto latest target.

        Args:
            worktree_id: Worktree record ID
            target_branch: Branch to rebase onto

        Returns:
            IntegrationResult with status

        Raises:
            ConflictError: If rebase encounters conflicts
        """
        record = await self.worktree_manager.get(worktree_id)
        if not record:
            raise IntegrationError(f"Worktree not found: {worktree_id}")

        worktree_path = Path(record.path)

        # Fetch latest target
        await self._run_git("fetch", "origin", target_branch, cwd=worktree_path)

        # Attempt rebase
        _, stderr, returncode = await self._run_git(
            "rebase",
            f"origin/{target_branch}",
            cwd=worktree_path,
            check=False,
        )

        if returncode != 0:
            # Check if it's a conflict
            if "CONFLICT" in stderr or "could not apply" in stderr:
                # Get list of conflicting files
                status_out, _, _ = await self._run_git(
                    "diff",
                    "--name-only",
                    "--diff-filter=U",
                    cwd=worktree_path,
                    check=False,
                )
                conflict_files = [f for f in status_out.split("\n") if f.strip()]

                # Abort the rebase
                await self._run_git("rebase", "--abort", cwd=worktree_path, check=False)

                raise ConflictError(
                    f"Rebase conflict on {record.branch}",
                    conflict_files=conflict_files,
                )

            # Other failure
            return IntegrationResult(
                status=IntegrationStatus.FAILED,
                worktree_id=worktree_id,
                branch=record.branch,
                target_branch=target_branch,
                error_message=stderr,
            )

        # Get new HEAD commit
        commit_sha, _, _ = await self._run_git("rev-parse", "HEAD", cwd=worktree_path)

        return IntegrationResult(
            status=IntegrationStatus.REBASING,
            worktree_id=worktree_id,
            branch=record.branch,
            target_branch=target_branch,
            commit_sha=commit_sha,
        )

    async def integrate_task(
        self,
        worktree_id: str,
        target_branch: str = "main",
        test_config: TestConfig | None = None,
        auto_cleanup: bool = True,
    ) -> IntegrationResult:
        """Integrate a single worktree branch into target.

        Full workflow:
        1. Check for conflicts
        2. Rebase onto target
        3. Run pre-merge tests
        4. Fast-forward merge
        5. Clean up worktree (optional)

        Args:
            worktree_id: Worktree record ID
            target_branch: Branch to merge into
            test_config: Optional test configuration
            auto_cleanup: Whether to clean up worktree after merge

        Returns:
            IntegrationResult with final status
        """
        log.info(f"Starting integration for worktree {worktree_id}")

        record = await self.worktree_manager.get(worktree_id)
        if not record:
            raise IntegrationError(f"Worktree not found: {worktree_id}")

        worktree_path = Path(record.path)

        # Step 1: Check for conflicts first
        conflict_files = await self.check_conflicts(worktree_id, target_branch)
        if conflict_files:
            log.warning(f"Conflicts detected in {len(conflict_files)} files")
            return IntegrationResult(
                status=IntegrationStatus.CONFLICT,
                worktree_id=worktree_id,
                branch=record.branch,
                target_branch=target_branch,
                conflict_files=conflict_files,
            )

        # Step 2: Rebase onto target
        try:
            rebase_result = await self.rebase_onto_target(worktree_id, target_branch)
        except ConflictError as e:
            return IntegrationResult(
                status=IntegrationStatus.CONFLICT,
                worktree_id=worktree_id,
                branch=record.branch,
                target_branch=target_branch,
                conflict_files=e.conflict_files,
            )

        if rebase_result.status == IntegrationStatus.FAILED:
            return rebase_result

        # Step 3: Run tests if configured
        if test_config and test_config.commands:
            log.info("Running pre-merge tests")
            tests_passed, test_output = await self._run_tests(worktree_path, test_config)

            if not tests_passed and test_config.require_passing:
                log.warning("Pre-merge tests failed")
                return IntegrationResult(
                    status=IntegrationStatus.TEST_FAILED,
                    worktree_id=worktree_id,
                    branch=record.branch,
                    target_branch=target_branch,
                    test_output=test_output,
                )

        # Step 4: Push rebased branch (needed for remote merge)
        await self._run_git(
            "push",
            "--force-with-lease",
            "origin",
            record.branch,
            cwd=worktree_path,
        )

        # Step 5: Merge into target (fast-forward)
        # Switch to main repo for the merge
        await self._run_git("fetch", "origin", record.branch)
        await self._run_git("checkout", target_branch)
        await self._run_git("pull", "origin", target_branch)

        # Fast-forward merge
        await self._run_git("merge", "--ff-only", f"origin/{record.branch}")
        await self._run_git("push", "origin", target_branch)

        # Get final commit
        commit_sha, _, _ = await self._run_git("rev-parse", "HEAD")

        # Step 6: Clean up
        if auto_cleanup:
            await self.worktree_manager.mark_merged(worktree_id)
            await self.worktree_manager.cleanup(worktree_id, force=True)

            # Delete remote branch
            await self._run_git("push", "origin", "--delete", record.branch, check=False)

        log.info(f"Successfully integrated {record.branch} -> {target_branch}")

        return IntegrationResult(
            status=IntegrationStatus.MERGED,
            worktree_id=worktree_id,
            branch=record.branch,
            target_branch=target_branch,
            commit_sha=commit_sha,
            merged_at=datetime.now(UTC),
        )

    async def integrate_batch(
        self,
        worktree_ids: list[str],
        target_branch: str = "main",
        test_config: TestConfig | None = None,
        respect_dependencies: bool = True,
    ) -> list[IntegrationResult]:
        """Integrate multiple worktrees in dependency order.

        Args:
            worktree_ids: List of worktree record IDs to integrate
            target_branch: Branch to merge into
            test_config: Optional test configuration
            respect_dependencies: Whether to order by task dependencies

        Returns:
            List of IntegrationResults in processing order
        """
        results: list[IntegrationResult] = []

        # Get worktree records with task info
        worktrees: list[tuple[WorktreeRecord, Task | None]] = []
        for wt_id in worktree_ids:
            record = await self.worktree_manager.get(wt_id)
            if not record:
                log.warning(f"Worktree not found: {wt_id}")
                continue

            task = None
            if record.task_id:
                task_entity = await self.entity_manager.get(record.task_id)
                if task_entity and task_entity.entity_type == EntityType.TASK:
                    task = cast("Task", task_entity)

            worktrees.append((record, task))

        if respect_dependencies:
            # Sort by task dependencies (tasks with no deps first)
            # For now, simple sort by created_at as placeholder
            worktrees.sort(key=lambda x: x[0].created_at or datetime.min.replace(tzinfo=UTC))

        # Process in order, stopping on first failure
        for record, task in worktrees:
            log.info(f"Processing worktree {record.id} (task: {task.title if task else 'N/A'})")

            result = await self.integrate_task(
                worktree_id=record.id,
                target_branch=target_branch,
                test_config=test_config,
                auto_cleanup=True,
            )
            results.append(result)

            # Stop on conflict or test failure
            if result.status in (IntegrationStatus.CONFLICT, IntegrationStatus.TEST_FAILED):
                log.warning(f"Stopping batch integration due to {result.status}")
                break

        return results

    async def get_integration_status(
        self,
        worktree_id: str,
        target_branch: str = "main",
    ) -> IntegrationResult:
        """Get current integration readiness status for a worktree.

        Checks:
        - Has uncommitted changes?
        - Has conflicts with target?
        - Is behind target?

        Args:
            worktree_id: Worktree record ID
            target_branch: Target branch to check against

        Returns:
            IntegrationResult with current status
        """
        record = await self.worktree_manager.get(worktree_id)
        if not record:
            raise IntegrationError(f"Worktree not found: {worktree_id}")

        # Check for uncommitted changes
        has_uncommitted = await self.worktree_manager.check_uncommitted(worktree_id)
        if has_uncommitted:
            return IntegrationResult(
                status=IntegrationStatus.PENDING,
                worktree_id=worktree_id,
                branch=record.branch,
                target_branch=target_branch,
                error_message="Has uncommitted changes",
            )

        # Check for conflicts
        conflict_files = await self.check_conflicts(worktree_id, target_branch)
        if conflict_files:
            return IntegrationResult(
                status=IntegrationStatus.CONFLICT,
                worktree_id=worktree_id,
                branch=record.branch,
                target_branch=target_branch,
                conflict_files=conflict_files,
            )

        # Ready for integration
        return IntegrationResult(
            status=IntegrationStatus.READY,
            worktree_id=worktree_id,
            branch=record.branch,
            target_branch=target_branch,
        )

    async def list_ready_for_integration(
        self,
        target_branch: str = "main",
    ) -> list[tuple[WorktreeRecord, IntegrationResult]]:
        """List all worktrees that are ready for integration.

        Returns:
            List of (WorktreeRecord, IntegrationResult) tuples
        """
        results: list[tuple[WorktreeRecord, IntegrationResult]] = []

        # Get all active worktrees for this project
        worktrees = await self.entity_manager.list_by_type(
            entity_type=EntityType.WORKTREE,
            limit=100,
        )

        for record in worktrees:
            if record.entity_type != EntityType.WORKTREE:
                continue
            record = cast("WorktreeRecord", record)

            if record.status != WorktreeStatus.ACTIVE:
                continue

            try:
                status = await self.get_integration_status(record.id, target_branch)
                results.append((record, status))
            except Exception:
                log.exception(f"Error checking status for {record.id}")

        return results
