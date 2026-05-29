"""Shared literals and base models reused across API schema domains.

Kept in one place so domain submodules import shared pieces from here rather
than from each other, avoiding circular imports.
"""

from typing import Literal

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
