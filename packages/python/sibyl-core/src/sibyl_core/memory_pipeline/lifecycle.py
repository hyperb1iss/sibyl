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


def graph_lifecycle_stamp(memory: MemoryLifecycleView) -> dict[str, object]:
    """The verdict a graph row projected from this capture has to be born with.

    Projection is asynchronous: `remember` writes the capture, queues the graph
    write, and returns an id the caller can correct immediately. The worker
    then builds the row from the payload it was handed, which was serialized
    before the correction existed, so a row created after a correction is
    created recallable and nothing later reconciles it. The capture is the
    authority (it is the row the correction actually mutated), and this is the
    verdict a reader of that row must inherit.

    Empty for a capture that is still recallable, so a caller can merge the
    result unconditionally.

    `excluded_from_recall` carries the exclusion on its own rather than
    depending on the state name, because a capture can fall out of recall on a
    review state or a bare replacement marker while its lifecycle state still
    reads active.
    """

    if raw_memory_lifecycle_recallable(memory):
        return {}
    metadata = dict(memory.metadata)
    lifecycle = memory_lifecycle_from_metadata(
        metadata,
        source_id=memory.source_id or memory.id,
        review_state=memory.review_state,
    )
    state = _normalized_state(lifecycle.state)
    stamp: dict[str, object] = {"excluded_from_recall": True}
    if state and state != MemoryLifecycleState.ACTIVE.value:
        stamp["lifecycle_state"] = state
    flags = [_normalized_state(flag) for flag in lifecycle.flags]
    if flags:
        stamp["lifecycle_flags"] = flags
    replacement = lifecycle.replacement_source_id or metadata.get("superseded_by_source_id")
    if replacement:
        stamp["superseded_by_source_id"] = str(replacement)
    duplicate = lifecycle.duplicate_of_source_id or metadata.get("duplicate_of_source_id")
    if duplicate:
        stamp["duplicate_of_source_id"] = str(duplicate)
    return stamp


GRAPH_RECALL_EXCLUSION_KEYS = (
    "excluded_from_recall",
    "superseded_by_source_id",
    "superseded_by_raw_memory_id",
    "duplicate_of_source_id",
)
# Reflection stamps `duplicate_of_source_id` on a near-duplicate candidate,
# and promotion resets that candidate's lifecycle to ACTIVE without clearing
# the key, so on the graph lane the bare marker outlives the verdict that set
# it. An explicit ACTIVE state is the later, deliberate statement and wins.
# The other markers do not get this treatment: a correction that excludes a
# row never leaves it ACTIVE, so nothing here can soften supersession.
_STATE_OVERRIDABLE_EXCLUSION_KEYS = frozenset({"duplicate_of_source_id"})


def _flag_values(value: object) -> Iterable[str]:
    if isinstance(value, str):
        return (_normalized_state(value),)
    # A Mapping is Iterable, and iterating one yields its keys, so a
    # dict-shaped flag bag would read every key as a set flag no matter what
    # it maps to: `{"hidden": False}` would retire the row. Sequence and set
    # shapes are the only ones a flag list is ever written in.
    if isinstance(value, list | tuple | set | frozenset):
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
    state = _normalized_state(metadata.get("lifecycle_state"))
    if _normalized_state(metadata.get("review_state")) in RECALL_EXCLUDED_REVIEW_STATES:
        return False
    if state in RECALL_EXCLUDED_LIFECYCLE_STATES:
        return False
    if RECALL_EXCLUDED_LIFECYCLE_FLAGS.intersection(_flag_values(metadata.get("lifecycle_flags"))):
        return False
    active = state == MemoryLifecycleState.ACTIVE.value
    return not any(
        metadata.get(key)
        for key in GRAPH_RECALL_EXCLUSION_KEYS
        if not (active and key in _STATE_OVERRIDABLE_EXCLUSION_KEYS)
    )


def current_graph_memory_recallable(value: object) -> bool:
    """Apply the graph lifecycle verdict to a row, model, or metadata bag."""

    if isinstance(value, Mapping):
        nested = value.get("metadata") or value.get("attributes")
        metadata = nested if isinstance(nested, Mapping) else value
    else:
        nested = getattr(value, "metadata", None)
        metadata = nested if isinstance(nested, Mapping) else {}
    return graph_metadata_recallable(metadata)
