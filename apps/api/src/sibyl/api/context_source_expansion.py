"""Manifest-backed operational source expansion for accurate context evidence."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import asdict
from typing import Any

import structlog

from sibyl.api.schemas import SearchResponse, SearchResult
from sibyl_core.retrieval.operational_sources import (
    OperationalSourceEntityReader,
    fetch_operational_source_inventory,
    select_operational_source_span,
)

log = structlog.get_logger()

_MAX_EXPANDED_SOURCES = 2
_MAX_SOURCE_OBSERVATIONS = 4
_MAX_TOTAL_EXPANDED_RESULTS = 8
_SOURCE_EXPANSION_STRATEGY = "manifest_ordered_span_v1"


async def expand_operational_source_evidence(
    response: SearchResponse,
    *,
    entity_reader: OperationalSourceEntityReader,
    query: str,
    organization_id: str,
    principal_id: str | None,
    allowed_project_ids: set[str] | None,
    allowed_memory_scope_keys: set[str] | None,
    content_max_chars: int,
    record_exposure: bool,
) -> SearchResponse:
    """Expand the strongest fused operational sources into ordered raw spans."""
    source_ids = _rank_operational_source_ids(response.results)
    allocations = _source_allocations(response.limit, source_ids)
    receipt: dict[str, Any] = {
        "strategy": _SOURCE_EXPANSION_STRATEGY,
        "status": "not_applicable" if not allocations else "attempted",
        "attempted_source_ids": list(allocations),
        "total_result_budget": sum(allocations.values()),
        "sources": [],
        "inserted_result_ids": [],
    }
    if not allocations:
        return _with_receipt(response, receipt)

    outcomes = await asyncio.gather(
        *(
            fetch_operational_source_inventory(
                entity_reader,
                source_id,
                allowed_project_ids=allowed_project_ids,
                allowed_memory_scope_keys=allowed_memory_scope_keys,
                principal_id=principal_id,
            )
            for source_id in allocations
        ),
        return_exceptions=True,
    )
    existing_results = {result.id: result for result in response.results}
    core_results_by_source: dict[str, list[Any]] = {}
    expanded_source_receipts: dict[str, dict[str, Any]] = {}
    for source_id, outcome in zip(allocations, outcomes, strict=True):
        if isinstance(outcome, BaseException):
            if not isinstance(outcome, Exception):
                raise outcome
            receipt["sources"].append(
                {
                    "source_id": source_id,
                    "status": "read_failed",
                    "error_type": type(outcome).__name__,
                }
            )
            continue
        source_receipt = {
            "source_id": source_id,
            "manifest_id": outcome.manifest_id,
            "status": outcome.status,
            "expected_entity_count": outcome.expected_entity_count,
            "loaded_entity_count": outcome.loaded_entity_count,
            "raw_observation_count": len(outcome.raw_observations),
            "memory_scope": outcome.memory_scope,
        }
        receipt["sources"].append(source_receipt)
        if outcome.status != "complete":
            continue

        allocation = allocations[source_id]
        span = select_operational_source_span(
            query,
            outcome,
            max_observations=min(_MAX_SOURCE_OBSERVATIONS, allocation),
            max_entities=allocation,
        )
        source_receipt.update(
            {
                "candidate_window_count": span.candidate_window_count,
                "ranking_applied": span.ranking_applied,
                "selected_observation_ordinals": list(span.observation_ordinals),
                "selected_entity_ids": [entity.id for entity in span.entities],
            }
        )
        if not span.entities:
            continue

        from sibyl_core.tools.search import graph_entity_to_search_result

        seed_score = _source_seed_score(response.results, source_id)
        core_results = [
            graph_entity_to_search_result(
                entity,
                organization_id=organization_id,
                principal_id=principal_id,
                score=seed_score,
                include_content=True,
                content_max_chars=content_max_chars,
                policy_reason="manifest_scope_verified",
            )
            for entity in span.entities
        ]
        for result in core_results:
            result.metadata["operational_source_expansion"] = {
                "strategy": _SOURCE_EXPANSION_STRATEGY,
                "source_id": source_id,
                "manifest_id": outcome.manifest_id,
                "selected_observation_ordinals": list(span.observation_ordinals),
            }
            existing_exposure = existing_results.get(result.id)
            if existing_exposure and "usage_exposure" in existing_exposure.metadata:
                result.metadata["usage_exposure"] = existing_exposure.metadata["usage_exposure"]
        core_results_by_source[source_id] = core_results
        expanded_source_receipts[source_id] = source_receipt

    expanded_results_by_source = {
        source_id: [SearchResult.model_validate(asdict(result)) for result in results]
        for source_id, results in core_results_by_source.items()
    }
    merged = _merge_expanded_results(
        response.results,
        expanded_results_by_source,
        limit=response.limit,
    )
    expanded_ids = {
        result.id for result in merged if result.metadata.get("operational_source_expansion")
    }
    inserted_ids = expanded_ids - set(existing_results)
    if record_exposure and inserted_ids:
        new_core_results = [
            result
            for results in core_results_by_source.values()
            for result in results
            if result.id in inserted_ids and result.id not in existing_results
        ]
        if new_core_results:
            try:
                from sibyl_core.tools.usage_exposure import annotate_search_result_exposures

                receipt["usage_exposure"] = await annotate_search_result_exposures(
                    new_core_results,
                    organization_id=organization_id,
                    principal_id=principal_id,
                    project_id=None,
                    source_surface="context_source_expansion",
                    request_metadata={
                        "query": query,
                        "source_ids": list(core_results_by_source),
                        "result_ids": [result.id for result in new_core_results],
                    },
                )
                expanded_results_by_source = {
                    source_id: [SearchResult.model_validate(asdict(result)) for result in results]
                    for source_id, results in core_results_by_source.items()
                }
                merged = _merge_expanded_results(
                    response.results,
                    expanded_results_by_source,
                    limit=response.limit,
                )
            except Exception as exc:
                log.warning(
                    "context_source_expansion_exposure_failed",
                    error_type=type(exc).__name__,
                )
                receipt["usage_exposure"] = {
                    "status": "failed",
                    "error_type": type(exc).__name__,
                }

    receipt["inserted_result_ids"] = [result.id for result in merged if result.id in inserted_ids]
    receipt["status"] = "expanded" if expanded_ids else "unavailable"
    for source_id, source_receipt in expanded_source_receipts.items():
        selected_ids = set(source_receipt.get("selected_entity_ids", []))
        source_receipt["inserted_entity_ids"] = [
            result.id
            for result in merged
            if result.id in selected_ids
            and result.id in inserted_ids
            and _operational_source_id(result) == source_id
        ]

    original_ids = {result.id for result in response.results}
    expanded_entity_ids = {
        result.id for results in expanded_results_by_source.values() for result in results
    }
    replaced_stale_ids = {
        result.id
        for result in response.results
        if _operational_source_id(result) in expanded_results_by_source
        and _is_raw_observation(result)
        and result.id not in expanded_entity_ids
    }
    inserted_count = sum(result.id not in original_ids for result in merged)
    total = max(len(merged), response.total - len(replaced_stale_ids) + inserted_count)
    filters = dict(response.filters)
    filters["operational_source_expansion"] = receipt
    return response.model_copy(
        update={
            "results": merged,
            "total": total,
            "graph_count": sum(result.result_origin == "graph" for result in merged),
            "document_count": sum(result.result_origin == "document" for result in merged),
            "raw_memory_count": sum(result.result_origin == "raw_memory" for result in merged),
            "has_more": total > len(merged),
            "filters": filters,
        }
    )


def operational_source_expansion_applicable(response: SearchResponse) -> bool:
    return bool(_rank_operational_source_ids(response.results))


def source_expansion_not_applicable(response: SearchResponse) -> SearchResponse:
    return _with_receipt(
        response,
        {
            "strategy": _SOURCE_EXPANSION_STRATEGY,
            "status": "not_applicable",
            "attempted_source_ids": [],
            "total_result_budget": 0,
            "sources": [],
            "inserted_result_ids": [],
        },
    )


def source_expansion_unavailable(
    response: SearchResponse,
    *,
    error_type: str,
) -> SearchResponse:
    return _with_receipt(
        response,
        {
            "strategy": _SOURCE_EXPANSION_STRATEGY,
            "status": "unavailable",
            "error_type": error_type,
            "attempted_source_ids": _rank_operational_source_ids(response.results),
            "total_result_budget": 0,
            "sources": [],
            "inserted_result_ids": [],
        },
    )


def _rank_operational_source_ids(results: list[SearchResult]) -> list[str]:
    stats: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"queries": set(), "best_rank": len(results) + 1, "best_score": 0.0}
    )
    for rank, result in enumerate(results, start=1):
        source_id = _operational_source_id(result)
        if source_id is None or result.result_origin != "graph":
            continue
        fusion = result.metadata.get("retrieval_fusion")
        fusion_sources = fusion.get("sources") if isinstance(fusion, dict) else None
        queries = (
            {str(source) for source in fusion_sources if source}
            if isinstance(fusion_sources, list)
            else {"unfused"}
        )
        stats[source_id]["queries"].update(queries)
        stats[source_id]["best_rank"] = min(stats[source_id]["best_rank"], rank)
        stats[source_id]["best_score"] = max(stats[source_id]["best_score"], result.score)
    return sorted(
        stats,
        key=lambda source_id: (
            -len(stats[source_id]["queries"]),
            stats[source_id]["best_rank"],
            -stats[source_id]["best_score"],
            source_id,
        ),
    )[:_MAX_EXPANDED_SOURCES]


def _source_allocations(limit: int, source_ids: list[str]) -> dict[str, int]:
    total_budget = min(_MAX_TOTAL_EXPANDED_RESULTS, max(1, limit // 2))
    selected_sources = source_ids[: min(len(source_ids), total_budget)]
    if not selected_sources:
        return {}
    if len(selected_sources) == 1:
        return {selected_sources[0]: min(_MAX_SOURCE_OBSERVATIONS, total_budget)}
    first_budget = min(
        _MAX_SOURCE_OBSERVATIONS,
        total_budget - len(selected_sources) + 1,
    )
    allocations = {selected_sources[0]: first_budget}
    remaining = total_budget - first_budget
    for index, source_id in enumerate(selected_sources[1:], start=1):
        remaining_sources = len(selected_sources) - index
        allocation = max(1, remaining // remaining_sources)
        allocations[source_id] = allocation
        remaining -= allocation
    return allocations


def _merge_expanded_results(
    original: list[SearchResult],
    expanded_by_source: dict[str, list[SearchResult]],
    *,
    limit: int,
) -> list[SearchResult]:
    expanded_ids = {result.id for results in expanded_by_source.values() for result in results}
    inserted_sources: set[str] = set()
    merged: list[SearchResult] = []
    for result in original:
        source_id = _operational_source_id(result)
        if source_id in expanded_by_source and source_id not in inserted_sources:
            if result.id not in expanded_ids and not _is_raw_observation(result):
                merged.append(result)
            merged.extend(expanded_by_source[source_id])
            inserted_sources.add(source_id)
        elif result.id not in expanded_ids and not (
            source_id in expanded_by_source and _is_raw_observation(result)
        ):
            merged.append(result)
        if len(merged) >= limit:
            break
    return merged[:limit]


def _source_seed_score(results: list[SearchResult], source_id: str) -> float:
    return max(
        (result.score for result in results if _operational_source_id(result) == source_id),
        default=0.0,
    )


def _operational_source_id(result: SearchResult) -> str | None:
    value = result.metadata.get("operational_source_id")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _is_raw_observation(result: SearchResult) -> bool:
    return result.metadata.get("projection_kind") == "raw_observation"


def _with_receipt(response: SearchResponse, receipt: dict[str, Any]) -> SearchResponse:
    filters = dict(response.filters)
    filters["operational_source_expansion"] = receipt
    return response.model_copy(update={"filters": filters})


__all__ = [
    "expand_operational_source_evidence",
    "operational_source_expansion_applicable",
    "source_expansion_not_applicable",
    "source_expansion_unavailable",
]
