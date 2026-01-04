"""WorktreeManager for agent isolation via git worktrees.

Each agent gets an isolated git worktree to work in, preventing conflicts
between parallel agents and enabling clean merge workflows.

Worktrees are stored at: ~/.sibyl-worktrees/{org}/{project}/{branch}/
"""

import asyncio
import hashlib
import shutil
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from sibyl_core.models import EntityType, WorktreeRecord, WorktreeStatus

if TYPE_CHECKING:
    from sibyl_core.graph import EntityManager

log = structlog.get_logger()


@dataclass
class SetupConfig:
    """Configuration for post-worktree-creation setup commands.

    These commands make the workspace usable (install deps, generate files, etc.)
    """

    commands: list[str] = field(default_factory=list)
    timeout_seconds: int = 300
    continue_on_error: bool = False  # Stop on first failure by default


@dataclass
class SetupResult:
    """Result of running setup commands."""

    success: bool
    outputs: list[tuple[str, str, int]]  # (command, output, returncode)
    total_time_seconds: float


# Default base directory for worktrees
DEFAULT_WORKTREE_BASE = Path.home() / ".sibyl-worktrees"


def _generate_worktree_id(org_id: str, project_id: str, branch: str) -> str:
    """Generate a deterministic worktree ID."""
    combined = f"{org_id}:{project_id}:{branch}"
    hash_bytes = hashlib.sha256(combined.encode()).hexdigest()[:12]
    return f"worktree_{hash_bytes}"


class WorktreeError(Exception):
    """Base exception for worktree operations."""


class WorktreeManager:
    """Manages git worktrees for agent isolation.

    Each task gets its own worktree, allowing multiple agents to work
    on different tasks without stepping on each other's changes.

    Worktrees are registered in the knowledge graph for persistence
    and recovery after system restarts.
    """

    def __init__(
        self,
        entity_manager: "EntityManager",
        org_id: str,
        project_id: str,
        repo_path: str | Path,
        worktree_base: Path | None = None,
    ):
        """Initialize WorktreeManager.

        Args:
            entity_manager: Graph client for persistence
            org_id: Organization UUID
            project_id: Project UUID
            repo_path: Path to the main git repository
            worktree_base: Base directory for worktrees (default: ~/.sibyl-worktrees)
        """
        self.entity_manager = entity_manager
        self.org_id = org_id
        self.project_id = project_id
        self.repo_path = Path(repo_path).resolve()
        self.worktree_base = worktree_base or DEFAULT_WORKTREE_BASE

        # Ensure base directory exists
        self.worktree_base.mkdir(parents=True, exist_ok=True)

    def _get_worktree_path(self, branch_name: str) -> Path:
        """Get the filesystem path for a worktree.

        Structure: ~/.sibyl-worktrees/{org}/{project}/{branch}/
        """
        # Sanitize branch name for filesystem
        safe_branch = branch_name.replace("/", "_").replace("\\", "_")
        return self.worktree_base / self.org_id[:8] / self.project_id[:8] / safe_branch

    async def _run_git(
        self, *args: str, cwd: Path | None = None, check: bool = True
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
            raise WorktreeError(f"Git command failed: {stderr_str or stdout_str}")

        return stdout_str, stderr_str, proc.returncode or 0

    async def create(
        self,
        task_id: str,
        branch_name: str,
        base_ref: str = "HEAD",
        agent_id: str | None = None,
        setup_config: SetupConfig | None = None,
    ) -> WorktreeRecord:
        """Create a new worktree for a task.

        Args:
            task_id: Task UUID this worktree is for
            branch_name: Git branch name for the worktree
            base_ref: Base commit/branch to create from (default: HEAD)
            agent_id: Optional agent ID that will use this worktree
            setup_config: Optional setup commands to run after creation

        Returns:
            WorktreeRecord persisted to the graph
        """
        worktree_path = self._get_worktree_path(branch_name)

        # Check if worktree already exists
        if worktree_path.exists():
            log.warning(f"Worktree path already exists: {worktree_path}")
            # Check if it's registered in the graph
            existing = await self._find_by_path(str(worktree_path))
            if existing:
                return existing
            # Path exists but not in graph - clean it up first
            shutil.rmtree(worktree_path, ignore_errors=True)

        # Ensure parent directory exists
        worktree_path.parent.mkdir(parents=True, exist_ok=True)

        # Get the base commit SHA for reference
        base_commit, _, _ = await self._run_git("rev-parse", base_ref)

        # Create the worktree with a new branch
        await self._run_git(
            "worktree",
            "add",
            "-b",
            branch_name,
            str(worktree_path),
            base_ref,
        )

        log.info(f"Created worktree at {worktree_path} on branch {branch_name}")

        # Generate deterministic ID
        worktree_id = _generate_worktree_id(self.org_id, self.project_id, branch_name)

        # Create and persist the record
        record = WorktreeRecord(
            id=worktree_id,
            name=branch_name,  # model_validator would set this, but be explicit
            task_id=task_id,
            agent_id=agent_id,
            path=str(worktree_path),
            branch=branch_name,
            base_commit=base_commit,
            status=WorktreeStatus.ACTIVE,
            last_used=datetime.now(UTC),
        )

        # Persist to graph (fast direct insert, no LLM extraction needed)
        await self.entity_manager.create_direct(record, generate_embedding=False)

        # Run setup commands if provided
        if setup_config and setup_config.commands:
            log.info(f"Running {len(setup_config.commands)} setup commands in {worktree_path}")
            setup_result = await self.run_setup(worktree_path, setup_config)
            if not setup_result.success:
                log.warning(
                    f"Setup completed with errors in {setup_result.total_time_seconds:.1f}s"
                )

        return record

    async def run_setup(
        self,
        worktree_path: Path,
        config: SetupConfig,
    ) -> SetupResult:
        """Run setup commands in a worktree.

        Executes each command in sequence, capturing output.

        Args:
            worktree_path: Path to the worktree
            config: Setup configuration with commands

        Returns:
            SetupResult with success status and outputs
        """
        import time

        start_time = time.monotonic()
        outputs: list[tuple[str, str, int]] = []
        success = True

        for cmd in config.commands:
            log.info(f"Running setup: {cmd}")
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
                returncode = proc.returncode or 0
                outputs.append((cmd, output, returncode))

                if returncode != 0:
                    log.warning(f"Setup command failed: {cmd} (exit {returncode})")
                    success = False
                    if not config.continue_on_error:
                        break

            except TimeoutError:
                outputs.append((cmd, f"TIMEOUT after {config.timeout_seconds}s", -1))
                success = False
                if not config.continue_on_error:
                    break

            except Exception as e:
                outputs.append((cmd, f"ERROR: {e}", -1))
                success = False
                if not config.continue_on_error:
                    break

        elapsed = time.monotonic() - start_time
        log.info(f"Setup completed in {elapsed:.1f}s (success={success})")

        return SetupResult(
            success=success,
            outputs=outputs,
            total_time_seconds=elapsed,
        )

    async def get(self, worktree_id: str) -> WorktreeRecord | None:
        """Get a worktree record by ID."""
        try:
            entity = await self.entity_manager.get(worktree_id)
            if entity and isinstance(entity, WorktreeRecord):
                return entity
        except Exception:
            log.debug(f"Worktree not found: {worktree_id}")
        return None

    async def _find_by_path(self, path: str) -> WorktreeRecord | None:
        """Find a worktree record by filesystem path."""
        # Query the graph for worktrees with this path
        results = await self.entity_manager.list_by_type(
            entity_type=EntityType.WORKTREE,
            limit=100,
        )
        for record in results:
            if isinstance(record, WorktreeRecord) and record.path == path:
                return record
        return None

    async def list_for_task(self, task_id: str) -> list[WorktreeRecord]:
        """Get all worktrees associated with a task."""
        results = await self.entity_manager.list_by_type(
            entity_type=EntityType.WORKTREE,
            limit=100,
        )
        return [r for r in results if isinstance(r, WorktreeRecord) and r.task_id == task_id]

    async def update_status(
        self, worktree_id: str, status: WorktreeStatus
    ) -> WorktreeRecord | None:
        """Update worktree status."""
        record = await self.get(worktree_id)
        if not record:
            return None

        # Use update() with a dict of changes
        updated = await self.entity_manager.update(
            worktree_id,
            {"status": status.value, "last_used": datetime.now(UTC).isoformat()},
        )

        if updated and isinstance(updated, WorktreeRecord):
            return updated
        return record

    async def check_uncommitted(self, worktree_id: str) -> bool:
        """Check if worktree has uncommitted changes."""
        record = await self.get(worktree_id)
        if not record:
            return False

        worktree_path = Path(record.path)
        if not worktree_path.exists():
            return False

        # Check for uncommitted changes
        stdout, _, _ = await self._run_git("status", "--porcelain", cwd=worktree_path, check=False)

        has_uncommitted = bool(stdout.strip())

        # Update the record if status changed
        if record.has_uncommitted != has_uncommitted:
            await self.entity_manager.update(worktree_id, {"has_uncommitted": has_uncommitted})

        return has_uncommitted

    async def get_uncommitted_diff(self, worktree_id: str) -> str:
        """Get the diff of uncommitted changes."""
        record = await self.get(worktree_id)
        if not record:
            return ""

        worktree_path = Path(record.path)
        if not worktree_path.exists():
            return ""

        # Get both staged and unstaged changes
        stdout, _, _ = await self._run_git("diff", "HEAD", cwd=worktree_path, check=False)
        return stdout

    async def check_conflicts(self, worktree_id: str, target_branch: str = "main") -> bool:
        """Check if worktree has merge conflicts with target branch.

        Args:
            worktree_id: Worktree record ID
            target_branch: Branch to check conflicts against (default: main)

        Returns:
            True if there would be merge conflicts
        """
        record = await self.get(worktree_id)
        if not record:
            return False

        worktree_path = Path(record.path)
        if not worktree_path.exists():
            return False

        # Fetch latest from origin
        await self._run_git("fetch", "origin", target_branch, cwd=worktree_path, check=False)

        # Try a dry-run merge to detect conflicts
        _, stderr, returncode = await self._run_git(
            "merge",
            "--no-commit",
            "--no-ff",
            f"origin/{target_branch}",
            cwd=worktree_path,
            check=False,
        )

        # Abort the merge attempt
        await self._run_git("merge", "--abort", cwd=worktree_path, check=False)

        # Non-zero return code usually means conflicts
        has_conflicts = returncode != 0 and "CONFLICT" in stderr

        if has_conflicts:
            log.warning(f"Worktree {record.branch} has conflicts with {target_branch}")

        return has_conflicts

    async def get_latest_commit(self, worktree_id: str) -> str | None:
        """Get the latest commit SHA in the worktree."""
        record = await self.get(worktree_id)
        if not record:
            return None

        worktree_path = Path(record.path)
        if not worktree_path.exists():
            return None

        stdout, _, _ = await self._run_git("rev-parse", "HEAD", cwd=worktree_path)

        # Update record if changed
        if record.last_commit != stdout:
            await self.entity_manager.update(worktree_id, {"last_commit": stdout})

        return stdout

    async def cleanup(self, worktree_id: str, force: bool = False) -> bool:
        """Remove a worktree and clean up.

        Args:
            worktree_id: Worktree record ID
            force: Force removal even if there are uncommitted changes

        Returns:
            True if cleanup succeeded
        """
        record = await self.get(worktree_id)
        if not record:
            log.warning(f"Worktree not found: {worktree_id}")
            return False

        worktree_path = Path(record.path)

        # Check for uncommitted changes
        if not force and await self.check_uncommitted(worktree_id):
            log.warning(f"Worktree has uncommitted changes: {record.path}")
            raise WorktreeError(
                f"Worktree {record.branch} has uncommitted changes. Use force=True to remove anyway."
            )

        # Remove the git worktree
        if worktree_path.exists():
            try:
                await self._run_git("worktree", "remove", str(worktree_path), "--force")
            except WorktreeError:
                # Fallback to manual removal if git worktree remove fails
                log.warning("Git worktree remove failed, removing directory manually")
                shutil.rmtree(worktree_path, ignore_errors=True)

        # Prune worktree references
        await self._run_git("worktree", "prune")

        # Delete the branch if it exists
        await self._run_git("branch", "-D", record.branch, check=False)

        # Update status in graph
        await self.entity_manager.update(worktree_id, {"status": WorktreeStatus.DELETED.value})

        log.info(f"Cleaned up worktree: {record.path}")
        return True

    async def mark_merged(self, worktree_id: str) -> WorktreeRecord | None:
        """Mark a worktree as successfully merged."""
        return await self.update_status(worktree_id, WorktreeStatus.MERGED)

    async def mark_orphaned(self, worktree_id: str) -> WorktreeRecord | None:
        """Mark a worktree as orphaned (agent died)."""
        return await self.update_status(worktree_id, WorktreeStatus.ORPHANED)

    async def cleanup_orphaned(self, max_age_hours: int = 24) -> list[str]:
        """Clean up orphaned worktrees older than max_age_hours.

        Returns:
            List of cleaned up worktree IDs
        """
        results = await self.entity_manager.list_by_type(
            entity_type=EntityType.WORKTREE,
            limit=100,
        )

        cleaned = []
        now = datetime.now(UTC)

        for record in results:
            if not isinstance(record, WorktreeRecord):
                continue

            if record.status != WorktreeStatus.ORPHANED:
                continue

            # Check age
            age_hours = (now - record.last_used).total_seconds() / 3600
            if age_hours < max_age_hours:
                continue

            try:
                await self.cleanup(record.id, force=True)
                cleaned.append(record.id)
            except Exception:
                log.exception(f"Failed to clean up orphaned worktree {record.id}")

        if cleaned:
            log.info(f"Cleaned up {len(cleaned)} orphaned worktrees")

        return cleaned

    async def audit_worktrees(self) -> dict[str, list[str]]:
        """Audit all worktrees and detect issues.

        Returns:
            Dict with lists of worktree IDs by status:
            - 'active': Active and healthy
            - 'orphaned': Agent died, worktree remains
            - 'missing': Record exists but filesystem gone
            - 'unregistered': Filesystem exists but no record
        """
        results = await self.entity_manager.list_by_type(
            entity_type=EntityType.WORKTREE,
            limit=1000,
        )

        audit: dict[str, list[str]] = {
            "active": [],
            "orphaned": [],
            "missing": [],
            "unregistered": [],
        }

        registered_paths: set[str] = set()

        for record in results:
            if not isinstance(record, WorktreeRecord):
                continue

            registered_paths.add(record.path)
            worktree_path = Path(record.path)

            if not worktree_path.exists():
                audit["missing"].append(record.id)
                continue

            if record.status == WorktreeStatus.ORPHANED:
                audit["orphaned"].append(record.id)
            elif record.status == WorktreeStatus.ACTIVE:
                audit["active"].append(record.id)

        # Check for unregistered worktrees on disk
        org_dir = self.worktree_base / self.org_id[:8]
        if org_dir.exists():
            project_dir = org_dir / self.project_id[:8]
            if project_dir.exists():
                for worktree_dir in project_dir.iterdir():
                    if worktree_dir.is_dir():
                        path_str = str(worktree_dir)
                        if path_str not in registered_paths:
                            audit["unregistered"].append(path_str)

        return audit
