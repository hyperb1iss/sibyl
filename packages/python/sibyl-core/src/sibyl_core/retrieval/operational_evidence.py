"""Composition laws for raw and distilled operational evidence."""

from __future__ import annotations

from collections import Counter
from typing import Any, Literal, Protocol

# The tuned quantity is an absolute note count, not a share of the pack.
# Widening the lane from 3 to 5 lost the entire measured note gain
# (-3.70pp/domain), so the reservation must not scale with `limit`: a
# slice-granular pack raises `limit` from 8 to ~28 for reasons that have
# nothing to do with how many distilled notes are worth reading.
TYPED_NOTE_RESERVATION_ITEMS = 3
OperationalNoteDedupeMode = Literal["source", "source_kind"]
OperationalNoteLaneMode = Literal["reserved", "additive"]


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
    operational_note_dedupe_mode: OperationalNoteDedupeMode = "source",
    operational_note_lane_mode: OperationalNoteLaneMode = "reserved",
    include_activity_receipt: bool = False,
) -> tuple[list[ResultT], dict[str, Any]]:
    """Reserve a fixed count of output slots for independently ranked notes.

    The reservation is absolute rather than proportional; see
    `TYPED_NOTE_RESERVATION_ITEMS`. Under an item budget a small pack still
    shrinks it, because a reservation may never consume the whole pack.

    `char_budget` changes what bounds the pack. An item count only bounds
    payload while item size is stable, and a passage is roughly a twelfth of the
    state it was cut from, so the same `limit` means two very different reader
    payloads on the two substrates. Pass a budget in characters and item count
    stops bounding the lanes: candidates are admitted in rank order while the
    running total of returned content fits, so the pack is always a prefix of
    the ranking and never exceeds the budget. The note lane stays pinned at its
    absolute count within that budget, but the budget outranks the pin — a
    budget too small for three notes returns fewer, because a hard budget that
    one lane can overrun is not a budget. By the same token a budget that only
    the notes fit into returns an all-note pack: characters, not slot counts,
    are what a budgeted pack holds back for the raw lane.
    """
    if char_budget is not None and char_budget < 1:
        raise ValueError("char_budget must be positive")
    if operational_note_dedupe_mode not in {"source", "source_kind"}:
        raise ValueError("operational_note_dedupe_mode must be source or source_kind")
    if operational_note_lane_mode not in {"reserved", "additive"}:
        raise ValueError("operational_note_lane_mode must be reserved or additive")
    output_limit = max(1, limit)
    typed_candidates: list[ResultT] = []
    raw_candidates: list[ResultT] = []
    seen_typed_sources: set[tuple[str, ...]] = set()
    seen_ids: set[str] = set()
    source_only_seen_sources: set[tuple[str, str]] = set()
    source_only_seen_ids: set[str] = set()
    source_only_duplicate_count = 0
    excluded_typed_count = 0
    candidate_activity: list[dict[str, Any]] = []

    for rank, result in enumerate(typed_results):
        if not is_distilled_operational_note(result):
            excluded_typed_count += 1
            _record_candidate_activity(
                candidate_activity,
                result=result,
                lane="typed",
                rank=rank,
                status="dropped",
                reason="not_distilled_operational_note",
            )
            continue
        source_only_key = (
            result.type,
            str(result.metadata.get("operational_source_id") or result.id),
        )
        if source_only_key in source_only_seen_sources:
            source_only_duplicate_count += 1
        elif result.id not in source_only_seen_ids:
            source_only_seen_sources.add(source_only_key)
            source_only_seen_ids.add(result.id)
        source_key_parts = [
            result.type,
            str(result.metadata.get("operational_source_id") or result.id),
        ]
        if operational_note_dedupe_mode == "source_kind":
            source_key_parts.append(str(result.metadata.get("note_kind") or ""))
        source_key = tuple(source_key_parts)
        if source_key in seen_typed_sources:
            _record_candidate_activity(
                candidate_activity,
                result=result,
                lane="typed",
                rank=rank,
                status="dropped",
                reason=(
                    "duplicate_operational_source"
                    if operational_note_dedupe_mode == "source"
                    else "duplicate_operational_source_note_kind"
                ),
            )
            continue
        if result.id in seen_ids:
            _record_candidate_activity(
                candidate_activity,
                result=result,
                lane="typed",
                rank=rank,
                status="dropped",
                reason="duplicate_result_id",
            )
            continue
        typed_candidates.append(result)
        seen_typed_sources.add(source_key)
        seen_ids.add(result.id)
        _record_candidate_activity(
            candidate_activity,
            result=result,
            lane="typed",
            rank=rank,
            status="candidate",
            reason="eligible",
        )

    for rank, result in enumerate(raw_results):
        if result.id in seen_ids:
            _record_candidate_activity(
                candidate_activity,
                result=result,
                lane="raw",
                rank=rank,
                status="dropped",
                reason="duplicate_result_id",
            )
            continue
        if is_distilled_operational_note(result):
            _record_candidate_activity(
                candidate_activity,
                result=result,
                lane="raw",
                rank=rank,
                status="dropped",
                reason="distilled_note_in_raw_lane",
            )
            continue
        raw_candidates.append(result)
        seen_ids.add(result.id)
        _record_candidate_activity(
            candidate_activity,
            result=result,
            lane="raw",
            rank=rank,
            status="candidate",
            reason="eligible",
        )

    reservation_target = max(1, typed_reservation_items)
    if operational_note_lane_mode == "additive":
        raw_reference, raw_spent = _raw_control_selection(
            raw_candidates,
            limit=output_limit,
            char_budget=char_budget,
        )
        if char_budget is None:
            reserved = typed_candidates[:reservation_target]
            spent = raw_spent + sum(_result_chars(result) for result in reserved)
        else:
            reserved, spent = _admit_within_budget(
                typed_candidates[:reservation_target],
                budget=char_budget,
                spent=raw_spent,
            )
        typed_reservation = len(reserved)
        typed_overflow_count = 0
        selected = [*reserved, *raw_reference]
        selected_chars = spent
    elif char_budget is None:
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
    receipt: dict[str, Any] = {
        "mode": (
            "reserved_distilled_operational_notes_v1"
            if operational_note_lane_mode == "reserved"
            else "additive_distilled_operational_notes_v1"
        ),
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
    if include_activity_receipt:
        raw_reference, _spent = _raw_control_selection(
            raw_candidates,
            limit=output_limit,
            char_budget=char_budget,
        )
        selected_ids = {id(result) for result in selected}
        admitted_reason_by_identity = {
            id(result): ("typed_reservation" if index < typed_reservation else "typed_backfill")
            for index, result in enumerate(
                [result for result in selected if is_distilled_operational_note(result)]
            )
        }
        admitted_reason_by_identity.update(
            {
                id(result): "raw_control"
                if operational_note_lane_mode == "additive"
                else "raw_budget"
                for result in selected
                if not is_distilled_operational_note(result)
            }
        )
        for activity in candidate_activity:
            result_identity = activity.pop("_identity")
            if activity["status"] != "candidate":
                continue
            if result_identity in selected_ids:
                activity["status"] = "admitted"
                activity["reason"] = admitted_reason_by_identity[result_identity]
            else:
                activity["status"] = "dropped"
                activity["reason"] = (
                    "hard_char_budget" if char_budget is not None else "item_or_lane_budget"
                )
        selected_raw_ids = [
            result.id for result in selected if not is_distilled_operational_note(result)
        ]
        reference_raw_ids = [result.id for result in raw_reference]
        drop_reason_counts = Counter(
            str(activity["reason"])
            for activity in candidate_activity
            if activity["status"] == "dropped"
        )
        receipt.update(
            {
                "operational_note_dedupe_mode": operational_note_dedupe_mode,
                "operational_note_lane_mode": operational_note_lane_mode,
                "activity_receipt": {
                    "candidates": candidate_activity,
                    "admitted_ids": [result.id for result in selected],
                    "drop_reason_counts": dict(sorted(drop_reason_counts.items())),
                    "note_dedupe": {
                        "mode": operational_note_dedupe_mode,
                        "duplicate_source_count": source_only_duplicate_count,
                        "duplicate_source_kind_count": drop_reason_counts[
                            "duplicate_operational_source_note_kind"
                        ],
                    },
                    "additive_note_lane": {
                        "admitted_note_ids": [
                            result.id
                            for result in selected
                            if is_distilled_operational_note(result)
                        ]
                        if operational_note_lane_mode == "additive"
                        else [],
                    },
                    "hard_budget": {
                        "mode": "items" if char_budget is None else "characters",
                        "limit": output_limit if char_budget is None else char_budget,
                        "selected": len(selected) if char_budget is None else selected_chars,
                        "within": (
                            len(selected) <= output_limit
                            if char_budget is None
                            else selected_chars <= char_budget
                        ),
                    },
                    "raw_parity": {
                        "reference_ids": reference_raw_ids,
                        "selected_ids": selected_raw_ids,
                        "membership_equal": set(selected_raw_ids) == set(reference_raw_ids),
                        "order_equal": selected_raw_ids == reference_raw_ids,
                    },
                },
            }
        )
    return selected, receipt


def _result_chars(result: OperationalEvidenceResult) -> int:
    return len(result.content or "")


def _record_candidate_activity(
    activity: list[dict[str, Any]],
    *,
    result: OperationalEvidenceResult,
    lane: Literal["typed", "raw"],
    rank: int,
    status: Literal["candidate", "dropped"],
    reason: str,
) -> None:
    activity.append(
        {
            "_identity": id(result),
            "id": result.id,
            "lane": lane,
            "rank": rank,
            "chars": _result_chars(result),
            "status": status,
            "reason": reason,
        }
    )


def _raw_control_selection[ResultT: OperationalEvidenceResult](
    candidates: list[ResultT],
    *,
    limit: int,
    char_budget: int | None,
) -> tuple[list[ResultT], int]:
    if char_budget is None:
        selected = candidates[:limit]
        return selected, sum(_result_chars(result) for result in selected)
    return _admit_within_budget(candidates, budget=char_budget, spent=0)


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
    "OperationalNoteDedupeMode",
    "OperationalNoteLaneMode",
    "compose_operational_evidence",
    "is_distilled_operational_note",
]
