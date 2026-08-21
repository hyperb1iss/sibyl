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
    content_chars: int | None = None,
    note_kind: str | None = None,
) -> SearchResult:
    metadata = {"operational_source_id": source_id} if source_id else {}
    if distilled:
        metadata["projection_kind"] = "distilled_note"
    if note_kind:
        metadata["note_kind"] = note_kind
    return SearchResult(
        id=result_id,
        type=result_type,
        name=result_id,
        content=result_id if content_chars is None else "x" * content_chars,
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


def _passage_pool(count: int, *, content_chars: int) -> list[SearchResult]:
    return [_result(f"passage-{index}", content_chars=content_chars) for index in range(count)]


def test_item_budget_stays_the_default_and_is_reported_as_such() -> None:
    """Callers that never ask for a character budget keep the shipped law."""
    selected, receipt = compose_operational_evidence(
        typed_results=_note_pool(8),
        raw_results=_passage_pool(40, content_chars=100),
        limit=8,
    )

    assert len(selected) == 8
    assert receipt["budget_mode"] == "items"
    assert receipt["char_budget"] is None


def test_char_budget_admits_units_an_item_budget_would_have_truncated() -> None:
    """A passage is roughly a twelfth of a state, so `limit` stops meaning payload.

    The same eight-item pack that used to be eight whole states is eight
    passages of a state each: an order of magnitude less for the reader. Under a
    character budget the pack grows to whatever fits instead.
    """
    selected, receipt = compose_operational_evidence(
        typed_results=[],
        raw_results=_passage_pool(40, content_chars=100),
        limit=8,
        char_budget=3_000,
    )

    assert len(selected) == 30
    assert receipt["budget_mode"] == "characters"
    assert receipt["selected_chars"] == 3_000
    assert [item.id for item in selected[:3]] == ["passage-0", "passage-1", "passage-2"]


def test_char_budget_refuses_a_payload_that_would_exceed_it() -> None:
    """The budget is hard: the pack stops at the last unit that still fits."""
    selected, receipt = compose_operational_evidence(
        typed_results=[],
        raw_results=_passage_pool(40, content_chars=100),
        limit=40,
        char_budget=250,
    )

    assert sum(len(item.content) for item in selected) <= 250
    assert [item.id for item in selected] == ["passage-0", "passage-1"]
    assert receipt["raw_candidate_count"] == 40
    assert receipt["selected_raw_count"] == 2


def test_char_budget_keeps_the_note_lane_pinned_to_its_absolute_count() -> None:
    """A wider pack must not widen the reserved lane, whichever budget bounds it.

    The character budget is the reason `limit` no longer describes the pack, so
    it is exactly the knob that could smuggle the proportional reservation back
    in through a larger pack.
    """
    selected, receipt = compose_operational_evidence(
        typed_results=_note_pool(8),
        raw_results=_passage_pool(60, content_chars=1_000),
        limit=8,
        char_budget=30_000,
    )

    assert receipt["reservation_target"] == 3
    assert receipt["typed_reservation"] == 3
    assert [item.id for item in selected[:3]] == ["note-0", "note-1", "note-2"]
    assert len(selected) > 8


def test_char_budget_outranks_the_note_pin_when_it_cannot_hold_three() -> None:
    """A lane that may overrun the budget is not a budget.

    The pin fixes how many notes a pack reserves, not whether the pack may be
    larger than the caller asked for.
    """
    selected, receipt = compose_operational_evidence(
        typed_results=[
            _result(
                f"note-{index}",
                result_type="note",
                source_id=f"capture-{index}",
                distilled=True,
                content_chars=400,
            )
            for index in range(8)
        ],
        raw_results=_passage_pool(8, content_chars=100),
        limit=8,
        char_budget=900,
    )

    assert [item.id for item in selected] == ["note-0", "note-1", "passage-0"]
    assert receipt["typed_reservation"] == 2
    assert receipt["selected_chars"] == 900


def test_char_budget_must_be_positive() -> None:
    with pytest.raises(ValueError, match="char_budget must be positive"):
        compose_operational_evidence(
            typed_results=[],
            raw_results=_passage_pool(2, content_chars=10),
            limit=8,
            char_budget=0,
        )


def test_source_kind_dedupe_admits_distinct_note_kinds_and_receipts_drops() -> None:
    typed = [
        _result(
            "workflow-a",
            result_type="note",
            source_id="capture-a",
            distilled=True,
            note_kind="workflow",
        ),
        _result(
            "facts-a",
            result_type="note",
            source_id="capture-a",
            distilled=True,
            note_kind="facts",
        ),
        _result(
            "workflow-a-duplicate",
            result_type="note",
            source_id="capture-a",
            distilled=True,
            note_kind="workflow",
        ),
    ]

    selected, receipt = compose_operational_evidence(
        typed_results=typed,
        raw_results=[_result("raw-a")],
        limit=3,
        operational_note_dedupe_mode="source_kind",
        include_activity_receipt=True,
    )

    assert [result.id for result in selected] == ["workflow-a", "facts-a", "raw-a"]
    assert receipt["operational_note_dedupe_mode"] == "source_kind"
    assert receipt["activity_receipt"]["drop_reason_counts"] == {
        "duplicate_operational_source_note_kind": 1
    }


def test_additive_note_lane_preserves_raw_membership_and_order() -> None:
    selected, receipt = compose_operational_evidence(
        typed_results=_note_pool(4),
        raw_results=[_result(f"raw-{index}") for index in range(5)],
        limit=5,
        operational_note_lane_mode="additive",
        include_activity_receipt=True,
    )

    assert [result.id for result in selected] == [
        "note-0",
        "note-1",
        "note-2",
        "raw-0",
        "raw-1",
        "raw-2",
        "raw-3",
        "raw-4",
    ]
    assert receipt["selected_typed_count"] == 3
    assert receipt["selected_raw_count"] == 5
    assert receipt["activity_receipt"]["raw_parity"] == {
        "reference_ids": ["raw-0", "raw-1", "raw-2", "raw-3", "raw-4"],
        "selected_ids": ["raw-0", "raw-1", "raw-2", "raw-3", "raw-4"],
        "membership_equal": True,
        "order_equal": True,
    }


def test_additive_note_lane_never_breaks_the_hard_character_budget() -> None:
    selected, receipt = compose_operational_evidence(
        typed_results=[
            _result(
                f"note-{index}",
                result_type="note",
                source_id=f"capture-{index}",
                distilled=True,
                content_chars=40,
            )
            for index in range(2)
        ],
        raw_results=_passage_pool(2, content_chars=100),
        limit=2,
        char_budget=250,
        operational_note_lane_mode="additive",
        include_activity_receipt=True,
    )

    assert [result.id for result in selected] == ["note-0", "passage-0", "passage-1"]
    assert sum(len(result.content) for result in selected) == 240
    assert receipt["selected_chars"] == 240
    assert receipt["activity_receipt"]["drop_reason_counts"] == {"hard_char_budget": 1}
    assert receipt["activity_receipt"]["raw_parity"]["order_equal"] is True


def test_default_composition_omits_treatment_receipt_fields() -> None:
    _selected, receipt = compose_operational_evidence(
        typed_results=_note_pool(1),
        raw_results=[_result("raw-a")],
        limit=2,
    )

    assert "operational_note_dedupe_mode" not in receipt
    assert "operational_note_lane_mode" not in receipt
    assert "activity_receipt" not in receipt
