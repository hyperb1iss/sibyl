"""Models for reflecting raw session traces into durable memory candidates."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ReflectionCandidate:
    kind: str
    title: str
    content: str
    reason: str
    confidence: float
    tags: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    persisted_id: str | None = None


@dataclass(frozen=True)
class ReflectionPack:
    source_title: str
    source_id: str | None
    intent: str
    domain: str | None
    project: str | None
    candidates: list[ReflectionCandidate]
    total_candidates: int
    persisted_count: int = 0
    usage_hint: str = (
        "Review candidates, persist the durable ones, and keep raw session source as provenance."
    )
