"""Project model constants and utilities.

Defines constants for the shared project pattern where each organization
has a special "_shared" project for org-wide knowledge like conventions,
crawled docs, and learnings not tied to a specific project.
"""

from __future__ import annotations

# Shared project constants
SHARED_PROJECT_SLUG = "_shared"
SHARED_PROJECT_NAME = "Shared"
SHARED_PROJECT_DESCRIPTION = "Org-wide knowledge: conventions, crawled docs, and shared learnings"


def is_shared_project_slug(slug: str) -> bool:
    """Check if a slug is the shared project slug."""
    return slug == SHARED_PROJECT_SLUG
