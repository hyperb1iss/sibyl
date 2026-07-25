from __future__ import annotations

import pytest

from sibyl_core.retrieval.operational_evidence import compose_operational_evidence
from sibyl_core.tools.responses import SearchResult


def _result(
    result_id: str,
    *,
    result_type: str = "session",
    source_id: str | None = None,
    distilled: bool = False,
) -> SearchResult:
    metadata = {"operational_source_id": source_id} if source_id else {}
    if distilled:
        metadata["projection_kind"] = "distilled_note"
    return SearchResult(
        id=result_id,
        type=result_type,
        name=result_id,
        content=result_id,
        score=1.0,
        metadata=metadata,
    )


def _note_pool(count: int) -> list[SearchResult]:
    return [
        _result(
            f"note-{index}",
            result_type="note",
            source_id=f"capture-{index}",
            distilled=True,
        )
        for index in range(count)
    ]


@pytest.mark.parametrize(
    ("limit", "expected_typed", "expected_raw"),
    [(1, 1, 0), (2, 1, 1), (3, 2, 1), (8, 3, 5)],
)
def test_reserved_lane_never_consumes_a_small_pack(
    limit: int,
    expected_typed: int,
    expected_raw: int,
) -> None:
    """Packs at or below the shipped size keep the behaviour they shipped with."""
    selected, receipt = compose_operational_evidence(
        typed_results=_note_pool(8),
        raw_results=[_result(f"raw-{index}") for index in range(8)],
        limit=limit,
    )

    assert [item.id for item in selected] == [
        *[f"note-{index}" for index in range(expected_typed)],
        *[f"raw-{index}" for index in range(expected_raw)],
    ]
    assert receipt["selected_typed_count"] == expected_typed
    assert receipt["selected_raw_count"] == expected_raw


@pytest.mark.parametrize("limit", [8, 24, 28])
def test_reserved_lane_is_an_absolute_count_a_wider_pack_cannot_widen(limit: int) -> None:
    """A slice-granular pack raises `limit`; it must not raise the note lane.

    The proportional law this replaced would have reserved 11 of 28 slots,
    and the tuning kill measured that widening as a total loss of the note
    gain. Both ends are pinned so neither the shipped pack size nor the
    slice pack size can drift.
    """
    selected, receipt = compose_operational_evidence(
        typed_results=_note_pool(limit),
        raw_results=[_result(f"raw-{index}") for index in range(limit)],
        limit=limit,
    )

    assert receipt["reservation_target"] == 3
    assert receipt["selected_typed_count"] == 3
    assert receipt["selected_raw_count"] == limit - 3
    assert len(selected) == limit
    assert [item.id for item in selected[:3]] == ["note-0", "note-1", "note-2"]


def test_reserved_lane_honours_an_explicit_override() -> None:
    """The pin is the default, not a hard-coded constant callers cannot move."""
    _, receipt = compose_operational_evidence(
        typed_results=_note_pool(8),
        raw_results=[_result(f"raw-{index}") for index in range(16)],
        limit=16,
        typed_reservation_items=5,
    )

    assert receipt["reservation_target"] == 5
    assert receipt["typed_reservation"] == 5
    assert receipt["selected_typed_overflow_count"] == 0


def test_reserved_lane_preserves_source_diversity_and_excludes_generic_notes() -> None:
    typed = [
        _result("workflow-a", result_type="note", source_id="capture-a", distilled=True),
        _result("facts-a", result_type="note", source_id="capture-a", distilled=True),
        _result("generic", result_type="note"),
        _result("workflow-b", result_type="note", source_id="capture-b", distilled=True),
    ]

    selected, receipt = compose_operational_evidence(
        typed_results=typed,
        raw_results=[_result("raw-a"), _result("raw-b")],
        limit=3,
    )

    assert [item.id for item in selected] == ["workflow-a", "workflow-b", "raw-a"]
    assert receipt["typed_candidate_count"] == 2
    assert receipt["excluded_typed_count"] == 1


def test_reserved_lane_backfills_sparse_pools_without_cross_pool_score_comparison() -> None:
    typed = [
        _result("note-a", result_type="note", source_id="capture-a", distilled=True),
        _result("note-b", result_type="note", source_id="capture-b", distilled=True),
        _result("note-c", result_type="note", source_id="capture-c", distilled=True),
        _result("note-d", result_type="note", source_id="capture-d", distilled=True),
        _result("note-e", result_type="note", source_id="capture-e", distilled=True),
    ]

    sparse_typed, typed_receipt = compose_operational_evidence(
        typed_results=typed[:1],
        raw_results=[_result(f"raw-{index}") for index in range(8)],
        limit=8,
    )
    sparse_raw, raw_receipt = compose_operational_evidence(
        typed_results=typed,
        raw_results=[_result("raw-a")],
        limit=8,
    )

    assert [item.id for item in sparse_typed] == [
        "note-a",
        *[f"raw-{index}" for index in range(7)],
    ]
    assert typed_receipt["selected_typed_count"] == 1
    assert [item.id for item in sparse_raw] == [
        "note-a",
        "note-b",
        "note-c",
        "raw-a",
        "note-d",
        "note-e",
    ]
    assert raw_receipt["selected_typed_overflow_count"] == 2
