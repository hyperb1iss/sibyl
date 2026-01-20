"""E2E test fixtures.

Shared fixtures for running tests against a live Sibyl system.
"""

import json
import os
import subprocess
import time
from collections.abc import AsyncGenerator, Generator
from dataclasses import dataclass

import httpx
import pytest
import pytest_asyncio

# =============================================================================
# Configuration
# =============================================================================

API_BASE_URL = os.getenv("SIBYL_API_URL", "http://localhost:3334/api")
FRONTEND_URL = os.getenv("SIBYL_FRONTEND_URL", "http://localhost:3337")
HEALTH_TIMEOUT = 30  # seconds to wait for services
HEALTH_INTERVAL = 0.5  # seconds between health checks
E2E_TEST_EMAIL = "e2e-test@sibyl.dev"
E2E_TEST_PASSWORD = "e2e-test-password-secure-123!"


# =============================================================================
# CLI Runner
# =============================================================================


@dataclass
class CLIResult:
    """Result from running a CLI command."""

    returncode: int
    stdout: str
    stderr: str

    @property
    def success(self) -> bool:
        """Check if command succeeded (exit code 0 and no error markers)."""
        if self.returncode != 0:
            return False
        # Check for error markers in output (CLI may return 0 on API errors)
        return not (self.stdout.startswith("\u2717") or "error" in self.stdout.lower()[:50])

    @property
    def is_json(self) -> bool:
        """Check if stdout is valid JSON."""
        try:
            json.loads(self.stdout)
            return True
        except (json.JSONDecodeError, TypeError):
            return False

    def json(self) -> dict | list:
        """Parse stdout as JSON."""
        return json.loads(self.stdout)


class CLIRunner:
    """Run Sibyl CLI commands and capture output."""

    def __init__(self, auth_token: str | None = None):
        """Initialize CLI runner with optional auth token."""
        self.auth_token = auth_token

    def run(self, *args: str, timeout: float = 30) -> CLIResult:
        """Run a sibyl CLI command.

        Args:
            *args: Command arguments (e.g., "task", "list", "--status", "todo")
            timeout: Command timeout in seconds

        Returns:
            CLIResult with returncode, stdout, stderr
        """
        cmd = ["sibyl", *args]
        env = os.environ.copy()
        if self.auth_token:
            env["SIBYL_AUTH_TOKEN"] = self.auth_token
        result = subprocess.run(  # noqa: S603
            cmd,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
        return CLIResult(
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
        )

    def health(self) -> CLIResult:
        """Run sibyl health check."""
        return self.run("health")

    def project_create(self, name: str, description: str | None = None) -> CLIResult:
        """Create a project."""
        args = ["project", "create", "--name", name, "--json"]
        if description:
            args.extend(["--description", description])
        return self.run(*args)

    def project_list(self) -> CLIResult:
        """List projects."""
        return self.run("project", "list", "--json")

    def task_create(
        self,
        title: str,
        project_id: str,
        priority: str = "medium",
        feature: str | None = None,
        sync: bool = True,
    ) -> CLIResult:
        """Create a task.

        Args:
            sync: Wait for task creation to complete. Defaults to True for E2E tests
                  since workflow operations (start, complete) require the task to exist.
        """
        args = [
            "task",
            "create",
            "--title",
            title,
            "--project",
            project_id,
            "--priority",
            priority,
            "--json",
        ]
        if feature:
            args.extend(["--feature", feature])
        if sync:
            args.append("--sync")
        return self.run(*args)

    def task_list(
        self,
        status: str | None = None,
        project: str | None = None,
        all_projects: bool = False,
    ) -> CLIResult:
        """List tasks.

        Args:
            all_projects: List from all projects (bypass auto-resolved project context).
                          Use when you don't care about project scoping.
        """
        args = ["task", "list", "--json"]
        if status:
            args.extend(["--status", status])
        if project:
            args.extend(["--project", project])
        if all_projects:
            args.append("--all")
        return self.run(*args)

    def task_start(self, task_id: str) -> CLIResult:
        """Start a task."""
        return self.run("task", "start", task_id, "--json")

    def task_complete(self, task_id: str, learnings: str | None = None) -> CLIResult:
        """Complete a task."""
        args = ["task", "complete", task_id, "--json"]
        if learnings:
            args.extend(["--learnings", learnings])
        return self.run(*args)

    def add(
        self,
        title: str,
        content: str,
        entity_type: str = "pattern",
        category: str | None = None,
        language: str | None = None,
    ) -> CLIResult:
        """Add knowledge to the graph."""
        args = ["add", title, content, "--type", entity_type, "--json"]
        if category:
            args.extend(["-c", category])
        if language:
            args.extend(["-l", language])
        return self.run(*args)

    def search(self, query: str, limit: int = 5, all_projects: bool = True) -> CLIResult:
        """Search the knowledge graph.

        Args:
            all_projects: Search all projects (bypass auto-resolved project context).
                          Defaults to True for E2E tests to avoid picking up host machine's project.
        """
        args = ["search", query, "--limit", str(limit), "--json"]
        if all_projects:
            args.append("--all")
        return self.run(*args)

    def entity_list(self, entity_type: str | None = None) -> CLIResult:
        """List entities."""
        args = ["entity", "list", "--json"]
        if entity_type:
            args.extend(["--type", entity_type])
        return self.run(*args)


# =============================================================================
# Authentication
# =============================================================================


@pytest.fixture(scope="session")
def e2e_auth_token() -> str:
    """Create or login test user and return auth token.

    This is session-scoped so we only authenticate once per test run.
    Uses the local auth endpoints (not OAuth).
    """
    with httpx.Client(base_url=API_BASE_URL, timeout=30.0) as client:
        # Try to login first (user may already exist)
        login_response = client.post(
            "/auth/local/login",
            json={"email": E2E_TEST_EMAIL, "password": E2E_TEST_PASSWORD},
        )

        if login_response.status_code == 200:
            return login_response.json()["access_token"]

        # User doesn't exist, create via signup
        signup_response = client.post(
            "/auth/local/signup",
            json={
                "email": E2E_TEST_EMAIL,
                "password": E2E_TEST_PASSWORD,
                "name": "E2E Test User",
            },
        )

        if signup_response.status_code == 200:
            return signup_response.json()["access_token"]

        # If signup also failed, raise error with details
        raise RuntimeError(
            f"Failed to authenticate E2E test user. "
            f"Login: {login_response.status_code} {login_response.text}. "
            f"Signup: {signup_response.status_code} {signup_response.text}"
        )


@pytest.fixture
def cli(e2e_auth_token: str) -> CLIRunner:
    """Get CLI runner for executing sibyl commands."""
    return CLIRunner(auth_token=e2e_auth_token)


# =============================================================================
# HTTP Clients
# =============================================================================


@pytest_asyncio.fixture
async def api_client() -> AsyncGenerator[httpx.AsyncClient]:
    """Unauthenticated async HTTP client for API calls."""
    async with httpx.AsyncClient(base_url=API_BASE_URL, timeout=30.0) as client:
        yield client


@pytest_asyncio.fixture
async def auth_api_client(e2e_auth_token: str) -> AsyncGenerator[httpx.AsyncClient]:
    """Authenticated async HTTP client for API calls."""
    headers = {"Authorization": f"Bearer {e2e_auth_token}"}
    async with httpx.AsyncClient(base_url=API_BASE_URL, timeout=30.0, headers=headers) as client:
        yield client


@pytest.fixture
def sync_api_client() -> Generator[httpx.Client]:
    """Unauthenticated sync HTTP client for API calls."""
    with httpx.Client(base_url=API_BASE_URL, timeout=30.0) as client:
        yield client


@pytest.fixture
def sync_auth_client(e2e_auth_token: str) -> Generator[httpx.Client]:
    """Authenticated sync HTTP client for API calls."""
    headers = {"Authorization": f"Bearer {e2e_auth_token}"}
    with httpx.Client(base_url=API_BASE_URL, timeout=30.0, headers=headers) as client:
        yield client


# =============================================================================
# Health Check Fixtures
# =============================================================================


def _check_health(client: httpx.Client) -> bool:
    """Check if the API is healthy."""
    try:
        response = client.get("/health")
        return response.status_code == 200
    except httpx.RequestError:
        return False


@pytest.fixture(scope="session")
def wait_for_services() -> None:
    """Wait for all services to be ready before running tests.

    This is a session-scoped fixture that runs once at the start.
    """
    start = time.time()
    with httpx.Client(base_url=API_BASE_URL, timeout=5.0) as client:
        while time.time() - start < HEALTH_TIMEOUT:
            if _check_health(client):
                return
            time.sleep(HEALTH_INTERVAL)

    raise TimeoutError(f"Services not ready after {HEALTH_TIMEOUT}s. Is the backend running?")


@pytest.fixture(autouse=True)
def require_services(wait_for_services: None) -> None:
    """Automatically require services for all e2e tests."""


# =============================================================================
# Test Data Helpers
# =============================================================================


@pytest.fixture
def unique_id() -> str:
    """Generate a unique ID for test data."""
    import uuid

    return f"e2e-{uuid.uuid4().hex[:8]}"


@pytest.fixture
def test_project_name(unique_id: str) -> str:
    """Generate a unique project name."""
    return f"E2E Test Project {unique_id}"


@pytest.fixture
def test_task_title(unique_id: str) -> str:
    """Generate a unique task title."""
    return f"E2E Test Task {unique_id}"


# =============================================================================
# Frontend Helpers
# =============================================================================


@pytest.fixture
def frontend_available() -> bool:
    """Check if frontend is running."""
    try:
        with httpx.Client(timeout=2.0) as client:
            response = client.get(FRONTEND_URL)
            return response.status_code in (200, 301, 302, 307, 308)
    except httpx.RequestError:
        return False
