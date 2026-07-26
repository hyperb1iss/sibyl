"""Composition laws for raw and distilled operational evidence."""

from __future__ import annotations

from typing import Any, Protocol

# The tuned quantity is an absolute note count, not a share of the pack.
# Widening the lane from 3 to 5 lost the entire measured note gain
# (-3.70pp/domain), so the reservation must not scale with `limit`: a
# slice-granular pack raises `limit` from 8 to ~28 for reasons that have
# nothing to do with how many distilled notes are worth reading.
TYPED_NOTE_RESERVATION_ITEMS = 3


class OperationalEvidenceResult(Protocol):
    id: str
    type: str
    content: str
    metadata: dict[str, Any]


def is_distilled_operational_note(result: OperationalEvidenceResult) -> bool:
    return (
        result.type == "note"
        and result.metadata.get("projection_kind") == "distilled_note"
        and bool(result.metadata.get("operational_source_id"))
    )


def compose_operational_evidence[ResultT: OperationalEvidenceResult](
    *,
    typed_results: list[ResultT],
    raw_results: list[ResultT],
    limit: int,
    typed_reservation_items: int = TYPED_NOTE_RESERVATION_ITEMS,
    char_budget: int | None = None,
) -> tuple[list[ResultT], dict[str, Any]]:
    """Reserve a fixed count of output slots for independently ranked notes.

    The reservation is absolute rather than proportional; see
    `TYPED_NOTE_RESERVATION_ITEMS`. Small packs still shrink it, because a
    reservation may never consume the whole pack.

    `char_budget` changes what bounds the pack. An item count only bounds
    payload while item size is stable, and a passage is roughly a twelfth of the
    state it was cut from, so the same `limit` means two very different reader
    payloads on the two substrates. Pass a budget in characters and item count
    stops bounding the lanes: candidates are admitted in rank order while the
    running total of returned content fits, so the pack is always a prefix of
    the ranking and never exceeds the budget. The note lane stays pinned at its
    absolute count within that budget, but the budget outranks the pin — a
    budget too small for three notes returns fewer, because a hard budget that
    one lane can overrun is not a budget.
    """
    if char_budget is not None and char_budget < 1:
        raise ValueError("char_budget must be positive")
    output_limit = max(1, limit)
    typed_candidates: list[ResultT] = []
    raw_candidates: list[ResultT] = []
    seen_typed_sources: set[tuple[str, str]] = set()
    seen_ids: set[str] = set()
    excluded_typed_count = 0

    for result in typed_results:
        if not is_distilled_operational_note(result):
            excluded_typed_count += 1
            continue
        source_key = (
            result.type,
            str(result.metadata.get("operational_source_id") or result.id),
        )
        if source_key in seen_typed_sources or result.id in seen_ids:
            continue
        typed_candidates.append(result)
        seen_typed_sources.add(source_key)
        seen_ids.add(result.id)

    for result in raw_results:
        if result.id in seen_ids or is_distilled_operational_note(result):
            continue
        raw_candidates.append(result)
        seen_ids.add(result.id)

    reservation_target = max(1, typed_reservation_items)
    if char_budget is None:
        typed_reservation = min(
            len(typed_candidates),
            max(1, min(reservation_target, output_limit - 1)),
        )
        raw_budget = min(len(raw_candidates), output_limit - typed_reservation)
        selected = [
            *typed_candidates[:typed_reservation],
            *raw_candidates[:raw_budget],
        ]
        typed_overflow_count = min(
            len(typed_candidates) - typed_reservation,
            output_limit - len(selected),
        )
        if typed_overflow_count:
            selected.extend(
                typed_candidates[typed_reservation : typed_reservation + typed_overflow_count]
            )
        selected_chars = sum(_result_chars(result) for result in selected)
    else:
        reserved, spent = _admit_within_budget(
            typed_candidates[:reservation_target],
            budget=char_budget,
            spent=0,
        )
        raw_selection, spent = _admit_within_budget(
            raw_candidates,
            budget=char_budget,
            spent=spent,
        )
        overflow, spent = _admit_within_budget(
            typed_candidates[len(reserved) :],
            budget=char_budget,
            spent=spent,
        )
        typed_reservation = len(reserved)
        typed_overflow_count = len(overflow)
        selected = [*reserved, *raw_selection, *overflow]
        selected_chars = spent

    selected_typed_count = typed_reservation + typed_overflow_count
    return selected, {
        "mode": "reserved_distilled_operational_notes_v1",
        "candidate_count": len(typed_candidates) + len(raw_candidates),
        "typed_candidate_count": len(typed_candidates),
        "raw_candidate_count": len(raw_candidates),
        "excluded_typed_count": excluded_typed_count,
        "reservation_target": reservation_target,
        "typed_reservation": typed_reservation,
        "selected_typed_overflow_count": typed_overflow_count,
        "selected_typed_count": selected_typed_count,
        "selected_raw_count": len(selected) - selected_typed_count,
        "output_limit": output_limit,
        "budget_mode": "items" if char_budget is None else "characters",
        "char_budget": char_budget,
        "selected_chars": selected_chars,
        "pool_calibration": "independent_search_ranking",
    }


def _result_chars(result: OperationalEvidenceResult) -> int:
    return len(result.content or "")


def _admit_within_budget[ResultT: OperationalEvidenceResult](
    candidates: list[ResultT],
    *,
    budget: int,
    spent: int,
) -> tuple[list[ResultT], int]:
    """Take the longest rank prefix of `candidates` that still fits.

    Admission stops at the first candidate that does not fit rather than
    skipping it for a smaller one further down. Packing by size would reorder
    relevance against length and cost the guarantee that a pack is a prefix of
    its ranking, which is what makes exposure reasoning about the pack hold.
    """
    admitted: list[ResultT] = []
    for candidate in candidates:
        candidate_chars = _result_chars(candidate)
        if spent + candidate_chars > budget:
            break
        admitted.append(candidate)
        spent += candidate_chars
    return admitted, spent


__all__ = [
    "TYPED_NOTE_RESERVATION_ITEMS",
    "compose_operational_evidence",
    "is_distilled_operational_note",
]
