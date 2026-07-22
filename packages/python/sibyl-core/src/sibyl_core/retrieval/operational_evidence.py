"""Composition laws for raw and distilled operational evidence."""

from __future__ import annotations

import math
from typing import Any, Protocol


class OperationalEvidenceResult(Protocol):
    id: str
    type: str
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
) -> tuple[list[ResultT], dict[str, Any]]:
    """Reserve three eighths of output slots for independently ranked notes."""
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

    reservation_target = max(1, math.ceil(output_limit * 3 / 8))
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
        "pool_calibration": "independent_search_ranking",
    }


__all__ = [
    "compose_operational_evidence",
    "is_distilled_operational_note",
]
