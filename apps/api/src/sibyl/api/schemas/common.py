"""Shared literals and base models reused across API schema domains.

Kept in one place so domain submodules import shared pieces from here rather
than from each other, avoiding circular imports.
"""

from typing import Literal

from pydantic import BaseModel, Field

MemoryScopeLiteral = Literal[
    "private",
    "delegated",
    "project",
    "team",
    "organization",
    "shared",
    "public",
]
MemoryCorrectionActionLiteral = Literal[
    "delete",
    "hide",
    "mark_duplicate",
    "mark_sensitive",
    "mark_stale",
    "mark_wrong",
    "redact",
    "restore",
    "supersede",
]

MemorySpaceStateLiteral = Literal["active", "disabled"]


class MutationReceipt(BaseModel):
    """Machine-readable outcome of an agent-facing mutation."""

    operation_id: str
    applied: bool
    revision: int | None = Field(default=None, ge=1)
    affected_records: list[str] = Field(default_factory=list)
    idempotency_key: str | None = None
    replayed: bool = False
