"""Lifecycle policy for raw memories in the canonical memory pipeline."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
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
        MemoryLifecycleState.CONTESTED.value,
        MemoryLifecycleState.DELETED.value,
        MemoryLifecycleState.SUPERSEDED.value,
    }
)
RECALL_EXCLUDED_LIFECYCLE_FLAGS = frozenset({"hidden", "redacted", "sensitive"})


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
    if RECALL_EXCLUDED_LIFECYCLE_FLAGS.intersection(lifecycle.flags):
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


GRAPH_RECALL_EXCLUSION_KEYS = (
    "excluded_from_recall",
    "superseded_by_source_id",
    "superseded_by_raw_memory_id",
    "duplicate_of_source_id",
)


def _flag_values(value: object) -> Iterable[str]:
    if isinstance(value, str):
        return (_normalized_state(value),)
    if isinstance(value, Iterable):
        return tuple(_normalized_state(item) for item in value)
    return ()


def graph_metadata_recallable(metadata: Mapping[str, object] | None) -> bool:
    """Report whether a projected graph row is still eligible to be served.

    The raw lane decides this from a `RawMemory`; the graph row carries the
    same verdict as flat metadata stamped by the correction write-through,
    because the graph has no lifecycle column and joining every candidate back
    to its capture at query time would cost a second round trip per row.
    """

    if not metadata:
        return True
    if _normalized_state(metadata.get("review_state")) in RECALL_EXCLUDED_REVIEW_STATES:
        return False
    if _normalized_state(metadata.get("lifecycle_state")) in RECALL_EXCLUDED_LIFECYCLE_STATES:
        return False
    if RECALL_EXCLUDED_LIFECYCLE_FLAGS.intersection(_flag_values(metadata.get("lifecycle_flags"))):
        return False
    return not any(metadata.get(key) for key in GRAPH_RECALL_EXCLUSION_KEYS)
