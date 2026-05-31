"""Pytest configuration and fixtures."""

import subprocess
from collections.abc import Generator
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def disable_raw_memory_auto_embedding(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "sibyl_core.services.surreal_content._configured_raw_memory_embedding_provider",
        lambda: None,
    )


# =============================================================================
# pytest hooks
# =============================================================================


def pytest_configure(config: pytest.Config) -> None:
    """Register custom markers."""
    config.addinivalue_line("markers", "live_model: tests requiring real LLM API calls")
    config.addinivalue_line("markers", "slow: tests taking >30s")
    config.addinivalue_line("markers", "requires_worktree: tests requiring git worktree setup")
    config.addinivalue_line("markers", "requires_redis: tests requiring Redis connection")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Auto-skip tests based on environment."""
    # Skip worktree tests if git not available
    try:
        subprocess.run(["git", "--version"], check=True, capture_output=True)  # noqa: S607
    except (subprocess.CalledProcessError, FileNotFoundError):
        skip_git = pytest.mark.skip(reason="git not available")
        for item in items:
            if "requires_worktree" in item.keywords:
                item.add_marker(skip_git)


# =============================================================================
# Git Repository Fixtures
# =============================================================================


@pytest.fixture
def tmp_git_repo(tmp_path: Path) -> Generator[Path]:
    """Create a temporary git repository.

    Yields:
        Path to an initialized git repo with one commit.
    """
    repo_path = tmp_path / "test_repo"
    repo_path.mkdir()

    # Initialize
    subprocess.run(["git", "init"], cwd=repo_path, check=True, capture_output=True)  # noqa: S607
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],  # noqa: S607
        cwd=repo_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"],  # noqa: S607
        cwd=repo_path,
        check=True,
        capture_output=True,
    )

    # Initial commit
    (repo_path / "README.md").write_text("# Test Repository\n")
    subprocess.run(["git", "add", "."], cwd=repo_path, check=True, capture_output=True)  # noqa: S607
    subprocess.run(
        ["git", "commit", "-m", "Initial commit"],  # noqa: S607
        cwd=repo_path,
        check=True,
        capture_output=True,
    )

    return repo_path


# =============================================================================
# Sample Data Fixtures
# =============================================================================


@pytest.fixture
def sample_pattern() -> dict[str, object]:
    """Return a sample pattern for testing."""
    return {
        "id": "pattern-001",
        "entity_type": "pattern",
        "name": "Error Boundary Pattern",
        "description": "Wrap risky operations in error boundaries",
        "category": "error-handling",
        "languages": ["python", "typescript"],
    }


@pytest.fixture
def sample_rule() -> dict[str, object]:
    """Return a sample sacred rule for testing."""
    return {
        "id": "rule-001",
        "entity_type": "rule",
        "name": "Never commit secrets",
        "description": "API keys, passwords, and credentials must never be committed",
        "severity": "error",
        "enforcement": "pre-commit",
    }
