"""Pytest configuration for live model tests.

Provides fixtures and configuration for tests that call real LLM APIs.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from sibyl.agents.runner import AgentRunner
    from sibyl.agents.worktree import WorktreeManager


# =============================================================================
# pytest hooks
# =============================================================================


def pytest_addoption(parser: pytest.Parser) -> None:
    """Add custom command line options."""
    parser.addoption(
        "--live-models",
        action="store_true",
        default=False,
        help="Run tests that call real LLM APIs (requires ANTHROPIC_API_KEY)",
    )
    parser.addoption(
        "--cost-limit",
        type=float,
        default=1.0,
        help="Maximum cost in USD for live model tests (default: $1.00)",
    )


def pytest_configure(config: pytest.Config) -> None:
    """Register custom markers."""
    config.addinivalue_line("markers", "live_model: tests requiring real LLM API calls")
    config.addinivalue_line("markers", "slow: tests taking >30s")
    config.addinivalue_line("markers", "requires_worktree: tests requiring git worktree setup")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip live_model tests unless --live-models flag is passed."""
    if not config.getoption("--live-models"):
        skip_live = pytest.mark.skip(reason="need --live-models option to run live model tests")
        for item in items:
            if "live_model" in item.keywords:
                item.add_marker(skip_live)

    # Also skip if no API key
    if not os.getenv("ANTHROPIC_API_KEY"):
        skip_no_key = pytest.mark.skip(reason="ANTHROPIC_API_KEY not set")
        for item in items:
            if "live_model" in item.keywords:
                item.add_marker(skip_no_key)


# =============================================================================
# Configuration
# =============================================================================


@dataclass
class LiveModelConfig:
    """Configuration for live model tests."""

    model: str = field(default_factory=lambda: os.getenv("SIBYL_TEST_MODEL", "claude-sonnet-4-20250514"))
    max_tokens: int = field(default_factory=lambda: int(os.getenv("SIBYL_TEST_MAX_TOKENS", "1024")))
    max_turns: int = field(default_factory=lambda: int(os.getenv("SIBYL_TEST_MAX_TURNS", "5")))
    timeout_seconds: int = field(default_factory=lambda: int(os.getenv("SIBYL_TEST_TIMEOUT", "120")))


@dataclass
class CostTracker:
    """Track cumulative test costs and enforce limits."""

    limit_usd: float = 1.0
    spent_usd: float = 0.0

    def record(self, cost: float) -> None:
        """Record a cost and check limit."""
        self.spent_usd += cost
        if self.spent_usd > self.limit_usd:
            raise CostLimitExceededError(
                f"Test cost ${self.spent_usd:.4f} exceeds limit ${self.limit_usd:.2f}"
            )

    def report(self) -> str:
        """Get cost summary."""
        return f"Total test cost: ${self.spent_usd:.4f} / ${self.limit_usd:.2f}"


class CostLimitExceededError(Exception):
    """Raised when test costs exceed the configured limit."""


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture(scope="session")
def live_model_config() -> LiveModelConfig:
    """Configuration for live model tests."""
    return LiveModelConfig()


@pytest.fixture(scope="session")
def cost_tracker(request: pytest.FixtureRequest) -> CostTracker:
    """Track costs across all live model tests."""
    limit = request.config.getoption("--cost-limit")
    tracker = CostTracker(limit_usd=limit)
    yield tracker
    # Print cost summary at end of session
    print(f"\n{tracker.report()}")  # noqa: T201


@pytest.fixture
async def tmp_git_repo(tmp_path: Path) -> AsyncGenerator[Path]:
    """Create a temporary git repository for testing.

    Yields the path to an initialized git repo with one commit.
    """
    repo_path = tmp_path / "test_repo"
    repo_path.mkdir()

    # Initialize repo (sync subprocess is fine for test fixtures)
    subprocess.run(["git", "init"], cwd=repo_path, check=True, capture_output=True)  # noqa: S607, ASYNC221
    subprocess.run(  # noqa: ASYNC221
        ["git", "config", "user.email", "test@example.com"],  # noqa: S607
        cwd=repo_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(  # noqa: ASYNC221
        ["git", "config", "user.name", "Test User"],  # noqa: S607
        cwd=repo_path,
        check=True,
        capture_output=True,
    )

    # Create initial commit
    (repo_path / "README.md").write_text("# Test Repo\n")
    subprocess.run(["git", "add", "."], cwd=repo_path, check=True, capture_output=True)  # noqa: S607, ASYNC221
    subprocess.run(  # noqa: ASYNC221
        ["git", "commit", "-m", "Initial commit"],  # noqa: S607
        cwd=repo_path,
        check=True,
        capture_output=True,
    )

    return repo_path


@pytest.fixture
async def worktree_manager(tmp_git_repo: Path) -> AsyncGenerator[WorktreeManager]:
    """Create a WorktreeManager with a temporary repo."""
    from sibyl.agents.worktree import WorktreeManager

    # Create mock entity manager
    from tests.test_agents import MockEntityManager

    entity_manager = MockEntityManager()
    manager = WorktreeManager(
        entity_manager=entity_manager,  # type: ignore[arg-type]
        org_id="test_org",
        repo_path=tmp_git_repo,
    )

    yield manager

    # Cleanup any remaining worktrees
    await manager.cleanup_orphaned()


@pytest.fixture
async def agent_runner(
    tmp_git_repo: Path,
    live_model_config: LiveModelConfig,
    cost_tracker: CostTracker,
) -> AsyncGenerator[AgentRunner]:
    """Create an AgentRunner for live testing.

    This creates a real runner that will make API calls.
    """
    from unittest.mock import patch

    from sibyl.agents.runner import AgentRunner
    from sibyl.agents.worktree import WorktreeManager
    from tests.test_agents import MockEntityManager, MockLockManager

    entity_manager = MockEntityManager()
    worktree_manager = WorktreeManager(
        entity_manager=entity_manager,  # type: ignore[arg-type]
        org_id="test_org",
        project_id="test_project",
        repo_path=tmp_git_repo,
    )

    with patch("sibyl.agents.runner.EntityLockManager", return_value=MockLockManager()):
        runner = AgentRunner(
            entity_manager=entity_manager,  # type: ignore[arg-type]
            worktree_manager=worktree_manager,
            org_id="test_org",
            project_id="test_project",
            add_dirs=[tempfile.gettempdir()],  # Allow temp dirs for test files
            permission_mode="bypassPermissions",  # Auto-accept all tool usage in tests
        )

        # Inject cost tracking
        runner._cost_tracker = cost_tracker

        yield runner

        # Stop all agents
        for agent_id in list(runner._active_agents.keys()):
            await runner.stop_agent(agent_id)


# =============================================================================
# Cost calculation helpers
# =============================================================================


# Pricing per 1M tokens (as of 2024)
MODEL_PRICING = {
    "claude-sonnet-4-20250514": {"input": 3.00, "output": 15.00},
    "claude-3-5-sonnet-20241022": {"input": 3.00, "output": 15.00},
    "claude-3-haiku-20240307": {"input": 0.25, "output": 1.25},
    "claude-3-opus-20240229": {"input": 15.00, "output": 75.00},
}


def calculate_cost(input_tokens: int, output_tokens: int, model: str) -> float:
    """Calculate cost for token usage."""
    pricing = MODEL_PRICING.get(model, MODEL_PRICING["claude-sonnet-4-20250514"])
    input_cost = (input_tokens / 1_000_000) * pricing["input"]
    output_cost = (output_tokens / 1_000_000) * pricing["output"]
    return input_cost + output_cost
