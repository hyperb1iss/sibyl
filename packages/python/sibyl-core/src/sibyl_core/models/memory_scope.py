"""Shared memory-scope model."""

from __future__ import annotations

from enum import StrEnum


class MemoryScope(StrEnum):
    PRIVATE = "private"
    DELEGATED = "delegated"
    PROJECT = "project"
    TEAM = "team"
    ORGANIZATION = "organization"
    SHARED = "shared"
    PUBLIC = "public"


# Scopes a project may name as the landing place for captures that declare
# none. Team needs a team key and reaches the graph through reviewed promotion,
# so it is never a default.
PROJECT_DEFAULT_MEMORY_SCOPES = (MemoryScope.PRIVATE, MemoryScope.PROJECT)
