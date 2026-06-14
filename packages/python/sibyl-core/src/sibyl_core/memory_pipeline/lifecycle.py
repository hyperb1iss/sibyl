"""Lifecycle policy for raw memories in the canonical memory pipeline."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from sibyl_core.models.reflection import MemoryLifecycleState, memory_lifecycle_from_metadata

RECALL_EXCLUDED_REVIEW_STATES = frozenset(
    {
        "archived",
        "deleted",
        "hidden",
        "redacted",
        "superseded",
    }
)
RECALL_EXCLUDED_LIFECYCLE_STATES = frozenset(
    {
        MemoryLifecycleState.ARCHIVED.value,
        MemoryLifecycleState.DELETED.value,
        MemoryLifecycleState.DUPLICATE.value,
        MemoryLifecycleState.HIDDEN.value,
        MemoryLifecycleState.REDACTED.value,
        MemoryLifecycleState.SENSITIVE.value,
        MemoryLifecycleState.STALE.value,
        MemoryLifecycleState.SUPERSEDED.value,
        MemoryLifecycleState.WRONG.value,
    }
)


class MemoryLifecycleView(Protocol):
    @property
    def id(self) -> str: ...

    @property
    def source_id(self) -> str: ...

    @property
    def review_state(self) -> str: ...

    @property
    def metadata(self) -> Mapping[str, object]: ...


def _normalized_state(value: object) -> str:
    return str(value or "").strip().lower()


def memory_lifecycle_state(
    metadata: Mapping[str, object],
    *,
    source_id: str = "",
    review_state: str = "pending",
) -> str:
    lifecycle = memory_lifecycle_from_metadata(
        dict(metadata),
        source_id=source_id,
        review_state=review_state,
    )
    return _normalized_state(lifecycle.state)


def raw_memory_lifecycle_recallable(memory: MemoryLifecycleView) -> bool:
    metadata = dict(memory.metadata)
    review_state = _normalized_state(memory.review_state)
    lifecycle = memory_lifecycle_from_metadata(
        metadata,
        source_id=memory.source_id or memory.id,
        review_state=memory.review_state,
    )
    lifecycle_state = _normalized_state(lifecycle.state)
    if review_state in RECALL_EXCLUDED_REVIEW_STATES:
        return False
    if lifecycle_state in RECALL_EXCLUDED_LIFECYCLE_STATES:
        return False
    if lifecycle.replacement_source_id:
        return False
    if lifecycle.duplicate_of_source_id:
        return False
    if metadata.get("superseded_by_raw_memory_id"):
        return False
    if metadata.get("superseded_by_source_id"):
        return False
    return not metadata.get("duplicate_of_source_id")
