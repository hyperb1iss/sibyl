"""Ask a memory, at the moment it is written, whether it can be found.

A probe is the question the writing agent expects this memory to answer. Running
it here turns retrievability from an assumption into a measurement: the probe
goes through the same fused retrieval path a reader would use, and the receipt
records the rank the memory came back at, or that it did not come back at all.

Two exclusions make the measurement capable of failing, which is the only kind
worth recording. Raw memory recall is off, because the verbatim capture of the
text just written matches its own vocabulary by construction and would let every
probe pass. Exposure recording is off, because a rehearsal is a synthetic query
and counting it would inflate the usage signals that ranking and decay read.

A rehearsal never fails a write. The memory is already durable when this runs;
an unretrievable probe is a finding about retrieval, and a broken search is a
finding about the search path. Both are recorded and neither is raised.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence

log = structlog.get_logger()

# One rehearsal reads the top of the ranking, not the tail. A memory that only
# appears at rank 40 is not one a reader with a budget would ever see, so
# counting it as retrievable would make the receipt flattering and useless.
REHEARSAL_LIMIT = 10

# Wall clock for the whole probe set. Past it the remaining probes are recorded
# as skipped rather than silently dropped, so a truncated receipt is legible as
# truncation instead of reading like an unretrievable memory.
REHEARSAL_BUDGET_SECONDS = 6.0

REHEARSAL_STATUS_RETRIEVABLE = "retrievable"
REHEARSAL_STATUS_ABSENT = "absent"
REHEARSAL_STATUS_SKIPPED = "skipped"
REHEARSAL_STATUS_ERROR = "error"


async def _default_search(**kwargs: Any) -> Any:
    # Imported at call time: the search path imports the memory pipeline, so a
    # module-level import here would close the loop.
    from sibyl_core.tools.search import search

    return await search(**kwargs)


async def rehearse_memory_probes(
    *,
    probes: Sequence[str],
    organization_id: str,
    entity_id: str,
    passage_ids: Iterable[str] = (),
    principal_id: str | None = None,
    memory_scope: str = "private",
    scope_key: str | None = None,
    project: str | None = None,
    accessible_projects: set[str] | None = None,
    limit: int = REHEARSAL_LIMIT,
    budget_seconds: float = REHEARSAL_BUDGET_SECONDS,
    search_fn: Callable[..., Any] | None = None,
    surface: str = "write",
) -> dict[str, Any]:
    """Run every probe against live retrieval and return the receipt.

    ``entity_id`` and ``passage_ids`` are the rows that count as a hit: a probe
    that surfaces one of the memory's own spans found the memory, since the
    reader can widen from a span to its parent in one lookup.
    """
    passage_targets = {str(passage_id) for passage_id in passage_ids if passage_id}
    targets = {entity_id: "parent"} | dict.fromkeys(passage_targets, "passage")
    run_search = search_fn or _default_search
    started_at = time.perf_counter()
    entries: list[dict[str, Any]] = []
    truncated = False

    for probe in probes:
        if time.perf_counter() - started_at >= budget_seconds:
            truncated = True
            entries.append(
                {
                    "probe": probe,
                    "status": REHEARSAL_STATUS_SKIPPED,
                    "reason": "budget_exhausted",
                    "rank": None,
                }
            )
            continue
        entries.append(
            await _rehearse_one(
                probe=probe,
                targets=targets,
                organization_id=organization_id,
                principal_id=principal_id,
                memory_scope=memory_scope,
                scope_key=scope_key,
                project=project,
                accessible_projects=accessible_projects,
                limit=limit,
                run_search=run_search,
            )
        )

    retrievable = sum(1 for entry in entries if entry["status"] == REHEARSAL_STATUS_RETRIEVABLE)
    receipt = {
        "checked_at": datetime.now(UTC).isoformat(),
        "surface": surface,
        "limit": limit,
        "total": len(entries),
        "retrievable": retrievable,
        "truncated": truncated,
        "probes": entries,
    }
    log.info(
        "probe_rehearsal_complete",
        entity_id=entity_id,
        surface=surface,
        probes_total=len(entries),
        probes_retrievable=retrievable,
        truncated=truncated,
        duration_ms=round((time.perf_counter() - started_at) * 1000, 2),
    )
    return receipt


async def _rehearse_one(
    *,
    probe: str,
    targets: dict[str, str],
    organization_id: str,
    principal_id: str | None,
    memory_scope: str,
    scope_key: str | None,
    project: str | None,
    accessible_projects: set[str] | None,
    limit: int,
    run_search: Callable[..., Any],
) -> dict[str, Any]:
    try:
        response = await run_search(
            query=probe,
            organization_id=organization_id,
            principal_id=principal_id,
            memory_scope=memory_scope,
            scope_key=scope_key,
            project=project,
            accessible_projects=accessible_projects,
            limit=limit,
            include_content=False,
            include_documents=False,
            include_raw_memory=False,
            record_exposure=False,
        )
    except Exception as exc:
        log.warning(
            "probe_rehearsal_search_failed",
            error_type=type(exc).__name__,
        )
        return {
            "probe": probe,
            "status": REHEARSAL_STATUS_ERROR,
            "error_type": type(exc).__name__,
            "rank": None,
        }

    for rank, result in enumerate(getattr(response, "results", []) or [], start=1):
        result_id = str(getattr(result, "id", "") or "")
        matched_kind = targets.get(result_id)
        if matched_kind is not None:
            return {
                "probe": probe,
                "status": REHEARSAL_STATUS_RETRIEVABLE,
                "rank": rank,
                "matched_id": result_id,
                "matched_kind": matched_kind,
            }
    return {"probe": probe, "status": REHEARSAL_STATUS_ABSENT, "rank": None}


__all__ = [
    "REHEARSAL_BUDGET_SECONDS",
    "REHEARSAL_LIMIT",
    "REHEARSAL_STATUS_ABSENT",
    "REHEARSAL_STATUS_ERROR",
    "REHEARSAL_STATUS_RETRIEVABLE",
    "REHEARSAL_STATUS_SKIPPED",
    "rehearse_memory_probes",
]
