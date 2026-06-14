from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

import pytest

from sibyl_core.memory_pipeline.lifecycle import (
    memory_lifecycle_state,
    raw_memory_lifecycle_recallable,
)
from sibyl_core.models.reflection import (
    MemoryLifecycle,
    MemoryLifecycleState,
    with_memory_lifecycle_metadata,
)


@dataclass(slots=True)
class RawMemoryView:
    review_state: str = "pending"
    metadata: Mapping[str, object] = field(default_factory=dict)
    id: str = "memory-1"
    source_id: str = "source-1"


@pytest.mark.parametrize(
    ("review_state", "metadata"),
    [
        ("archived", {}),
        ("deleted", {}),
        ("hidden", {}),
        ("redacted", {}),
        ("superseded", {}),
        ("pending", {"lifecycle_state": "deleted"}),
        ("pending", {"lifecycle_state": "duplicate"}),
        ("pending", {"lifecycle_state": "hidden"}),
        ("pending", {"lifecycle_state": "redacted"}),
        ("pending", {"lifecycle_state": "sensitive"}),
        ("pending", {"lifecycle_state": "stale"}),
        ("pending", {"lifecycle_state": "superseded"}),
        ("pending", {"lifecycle_state": "wrong"}),
        ("pending", {"superseded_by_raw_memory_id": "memory-0"}),
        ("pending", {"superseded_by_source_id": "source-0"}),
        ("pending", {"duplicate_of_source_id": "source-0"}),
    ],
)
def test_raw_memory_lifecycle_recallable_rejects_excluded_states(
    review_state: str,
    metadata: Mapping[str, object],
) -> None:
    memory = RawMemoryView(review_state=review_state, metadata=metadata)

    assert raw_memory_lifecycle_recallable(memory) is False


def test_raw_memory_lifecycle_recallable_accepts_pending_memory() -> None:
    memory = RawMemoryView(metadata={"lifecycle_state": "pending"})

    assert raw_memory_lifecycle_recallable(memory) is True


def test_raw_memory_lifecycle_recallable_uses_structured_lifecycle_snapshot() -> None:
    metadata = with_memory_lifecycle_metadata(
        {},
        MemoryLifecycle(
            state=MemoryLifecycleState.STALE,
            source_id="source-1",
            action="supersede",
            reason="newer source won",
            replacement_source_id="source-2",
        ),
    )
    metadata.pop("superseded_by_source_id")
    memory = RawMemoryView(metadata=metadata)

    assert raw_memory_lifecycle_recallable(memory) is False


def test_memory_lifecycle_state_normalizes_legacy_metadata() -> None:
    assert (
        memory_lifecycle_state(
            {"lifecycle_state": "STALE"},
            source_id="source-1",
            review_state="pending",
        )
        == "stale"
    )
