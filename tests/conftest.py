"""Pytest configuration and fixtures."""

import pytest


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
