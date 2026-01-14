"""Project registry for runner worktree management.

Manages project registrations, worktree creation, and capability detection
for isolated agent execution environments.
"""

import asyncio
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import structlog

log = structlog.get_logger()

# Capability detection patterns
CAPABILITY_INDICATORS = {
    "python": ["pyproject.toml", "setup.py", "requirements.txt", "Pipfile"],
    "node": ["package.json", "yarn.lock", "pnpm-lock.yaml"],
    "rust": ["Cargo.toml"],
    "go": ["go.mod"],
    "ruby": ["Gemfile"],
    "java": ["pom.xml", "build.gradle", "build.gradle.kts"],
    "dotnet": ["*.csproj", "*.fsproj", "*.sln"],
    "docker": ["Dockerfile", "docker-compose.yml", "docker-compose.yaml"],
}


@dataclass
class ProjectInfo:
    """Information about a registered project."""

    project_id: str
    path: Path
    capabilities: list[str] = field(default_factory=list)
    worktree_path: Path | None = None
    worktree_branch: str | None = None
    repo_url: str | None = None


class ProjectRegistry:
    """Manages project registrations and worktrees for the runner.

    Reads project links from ~/.sibyl/config.toml (same format as CLI)
    and manages isolated git worktrees for agent execution.
    """

    def __init__(self, worktree_base: Path | None = None) -> None:
        """Initialize registry.

        Args:
            worktree_base: Base directory for worktrees. Defaults to ~/.sibyl/worktrees.
        """
        self.worktree_base = worktree_base or (Path.home() / ".sibyl" / "worktrees")
        self._projects: dict[str, ProjectInfo] = {}
        self._lock = asyncio.Lock()

    @property
    def projects(self) -> dict[str, ProjectInfo]:
        """Get all registered projects."""
        return self._projects

    def load_from_config(self) -> int:
        """Load project mappings from CLI config file.

        Returns:
            Number of projects loaded.
        """
        config_path = Path.home() / ".sibyl" / "config.toml"
        if not config_path.exists():
            log.debug("no_config_file", path=str(config_path))
            return 0

        try:
            import tomllib

            with open(config_path, "rb") as f:
                config = tomllib.load(f)
        except Exception as e:
            log.warning("config_load_failed", path=str(config_path), error=str(e))
            return 0

        paths = config.get("paths", {})
        count = 0

        for path_str, project_id in paths.items():
            path = Path(path_str)
            if not path.exists():
                log.debug("project_path_not_found", path=path_str, project_id=project_id)
                continue

            # Detect capabilities
            capabilities = self._detect_capabilities(path)

            # Get repo URL if it's a git repo
            repo_url = self._get_repo_url(path)

            self._projects[project_id] = ProjectInfo(
                project_id=project_id,
                path=path,
                capabilities=capabilities,
                repo_url=repo_url,
            )
            count += 1
            log.debug(
                "project_loaded",
                project_id=project_id,
                path=path_str,
                capabilities=capabilities,
            )

        log.info("projects_loaded", count=count)
        return count

    def get_project(self, project_id: str) -> ProjectInfo | None:
        """Get project info by ID."""
        return self._projects.get(project_id)

    def register_project(
        self,
        project_id: str,
        path: Path,
        capabilities: list[str] | None = None,
    ) -> ProjectInfo:
        """Manually register a project.

        Args:
            project_id: Project ID from Sibyl.
            path: Local path to project.
            capabilities: Override auto-detected capabilities.

        Returns:
            ProjectInfo for the registered project.
        """
        if capabilities is None:
            capabilities = self._detect_capabilities(path)

        repo_url = self._get_repo_url(path)

        info = ProjectInfo(
            project_id=project_id,
            path=path,
            capabilities=capabilities,
            repo_url=repo_url,
        )
        self._projects[project_id] = info

        log.info(
            "project_registered",
            project_id=project_id,
            path=str(path),
            capabilities=capabilities,
        )
        return info

    async def ensure_worktree(
        self,
        project_id: str,
        branch: str | None = None,
        task_id: str | None = None,
    ) -> Path | None:
        """Ensure a worktree exists for a project.

        Creates a new worktree if needed, or returns existing one.

        Args:
            project_id: Project to create worktree for.
            branch: Branch to check out. If None, creates a new branch.
            task_id: Task ID for unique branch naming.

        Returns:
            Path to worktree, or None if project not found.
        """
        async with self._lock:
            project = self._projects.get(project_id)
            if not project:
                log.warning("project_not_found", project_id=project_id)
                return None

            # If we already have a worktree, return it
            if project.worktree_path and project.worktree_path.exists():
                return project.worktree_path

            # Create worktree directory
            self.worktree_base.mkdir(parents=True, exist_ok=True)

            # Generate branch name
            if branch is None:
                import secrets

                suffix = task_id[:8] if task_id else secrets.token_hex(4)
                branch = f"sibyl/agent-{suffix}"

            # Create worktree
            worktree_path = self.worktree_base / project_id / branch.replace("/", "-")
            worktree_path.parent.mkdir(parents=True, exist_ok=True)

            try:
                # First, fetch latest
                await self._run_git(project.path, "fetch", "--all")

                # Create worktree with new branch from main
                await self._run_git(
                    project.path,
                    "worktree",
                    "add",
                    "-b",
                    branch,
                    str(worktree_path),
                    "origin/main",
                )

                project.worktree_path = worktree_path
                project.worktree_branch = branch

                log.info(
                    "worktree_created",
                    project_id=project_id,
                    path=str(worktree_path),
                    branch=branch,
                )
                return worktree_path

            except subprocess.CalledProcessError as e:
                log.error(
                    "worktree_creation_failed",
                    project_id=project_id,
                    error=e.stderr if e.stderr else str(e),
                )
                return None

    async def cleanup_worktree(self, project_id: str) -> bool:
        """Remove a project's worktree.

        Args:
            project_id: Project whose worktree to remove.

        Returns:
            True if removed, False if not found or failed.
        """
        async with self._lock:
            project = self._projects.get(project_id)
            if not project or not project.worktree_path:
                return False

            try:
                # Remove from git worktree list
                await self._run_git(
                    project.path,
                    "worktree",
                    "remove",
                    str(project.worktree_path),
                    "--force",
                )

                # Delete the branch too
                if project.worktree_branch:
                    await self._run_git(
                        project.path,
                        "branch",
                        "-D",
                        project.worktree_branch,
                    )

                project.worktree_path = None
                project.worktree_branch = None

                log.info("worktree_removed", project_id=project_id)
                return True

            except subprocess.CalledProcessError as e:
                log.warning(
                    "worktree_cleanup_failed",
                    project_id=project_id,
                    error=e.stderr if e.stderr else str(e),
                )
                return False

    def get_projects_with_capability(self, capability: str) -> list[ProjectInfo]:
        """Get all projects that have a specific capability.

        Args:
            capability: Capability to filter by (e.g., "python", "docker").

        Returns:
            List of matching projects.
        """
        return [p for p in self._projects.values() if capability in p.capabilities]

    def get_warm_projects(self) -> list[ProjectInfo]:
        """Get all projects that have an active worktree."""
        return [p for p in self._projects.values() if p.worktree_path is not None]

    def _detect_capabilities(self, path: Path) -> list[str]:
        """Auto-detect project capabilities from filesystem.

        Args:
            path: Path to project root.

        Returns:
            List of detected capabilities.
        """
        capabilities = []

        for capability, indicators in CAPABILITY_INDICATORS.items():
            for indicator in indicators:
                if "*" in indicator:
                    # Glob pattern
                    if list(path.glob(indicator)):
                        capabilities.append(capability)
                        break
                else:
                    # Direct file check
                    if (path / indicator).exists():
                        capabilities.append(capability)
                        break

        return capabilities

    def _get_repo_url(self, path: Path) -> str | None:
        """Get git remote URL for a project.

        Args:
            path: Path to git repository.

        Returns:
            Remote URL or None if not a git repo.
        """
        try:
            result = subprocess.run(
                ["git", "-C", str(path), "remote", "get-url", "origin"],
                capture_output=True,
                text=True,
                check=True,
            )
            return result.stdout.strip()
        except subprocess.CalledProcessError:
            return None

    async def _run_git(self, cwd: Path, *args: str) -> str:
        """Run a git command asynchronously.

        Args:
            cwd: Working directory for git.
            *args: Git command and arguments.

        Returns:
            Command stdout.

        Raises:
            subprocess.CalledProcessError: If command fails.
        """
        proc = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            str(cwd),
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()

        if proc.returncode != 0:
            error = subprocess.CalledProcessError(proc.returncode or 1, ["git", *args])
            error.stderr = stderr.decode() if stderr else ""
            raise error

        return stdout.decode() if stdout else ""
