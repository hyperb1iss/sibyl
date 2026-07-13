"""Hybrid retrieval combining vector search and graph traversal.

Implements a two-phase retrieval strategy:
1. Entity linking: Identify entities mentioned in the query
2. Parallel retrieval: Vector search + graph traversal from linked entities
3. Fusion: Merge results using RRF
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, TypeVar

import structlog

from sibyl_core.models.entities import Entity
from sibyl_core.retrieval.fusion import rrf_merge_with_metadata
from sibyl_core.retrieval.query_ranking import (
    extract_keywords,
    extract_primary_text_from_text,
    generic_assistant_marker_count,
    rank_items_by_query_coverage,
)
from sibyl_core.retrieval.temporal import (
    get_entity_timestamp,
    resolve_temporal_reference,
    temporal_boost,
    temporal_proximity_boost,
)
from sibyl_core.utils.log_safety import query_log_fields

log = structlog.get_logger()

T = TypeVar("T")
MIN_PRIMARY_SEEDS_BEFORE_LINKING = 5
DEFAULT_GRAPH_RELATIONSHIP_TYPE_WEIGHTS: Mapping[str, float] = {
    "APPLIES_TO": 1.1,
    "REQUIRES": 1.15,
    "ENABLES": 1.15,
    "BREAKS": 1.15,
    "CONFLICTS_WITH": 1.1,
    "SUPERSEDES": 1.1,
    "BELONGS_TO": 1.2,
    "CONTAINS": 1.1,
    "DEPENDS_ON": 1.25,
    "BLOCKS": 1.25,
    "REFERENCES": 1.15,
    "ENCOUNTERED": 1.1,
    "IMPLEMENTED": 1.15,
    "VALIDATED_BY": 1.15,
    "USES_PROCEDURE": 1.15,
    "DERIVED_FROM": 1.05,
    "DOCUMENTED_IN": 1.0,
    "RELATED_TO": 0.85,
    "MENTIONS": 0.35,
}


def _require_group_id(group_id: str | None, operation: str) -> str:
    """Require explicit org scope for graph-backed retrieval helpers."""
    if not group_id:
        raise ValueError(f"group_id is required for {operation}")
    return str(group_id)


def _resolve_group_id(entity_manager: Any, group_id: str | None) -> str:
    """Resolve the organization scope for hybrid retrieval.

    Hybrid retrieval traverses the graph directly, so it must never fall back
    to the default graph in a multi-tenant deployment.
    """
    resolved = group_id or getattr(entity_manager, "_group_id", None)
    if not resolved:
        raise ValueError("group_id is required for hybrid retrieval")
    return str(resolved)


def _extract_keywords(query: str) -> list[str]:
    return extract_keywords(query)


def _entity_text(entity: Any) -> str:
    parts: list[str] = []
    for field_name in ("name", "description", "content"):
        if isinstance(entity, dict):
            value = entity.get(field_name)
        else:
            value = getattr(entity, field_name, None)
        if isinstance(value, str) and value:
            parts.append(value)

    return " ".join(parts).lower()


def _entity_primary_text(entity: Any) -> str:
    primary_text, _has_user_turns = extract_primary_text_from_text(_entity_text(entity))
    return primary_text


def _extract_primary_text_from_text(text: str) -> tuple[str, bool]:
    return extract_primary_text_from_text(text)


def _apply_keyword_boost(
    query: str,
    results: list[tuple[Any, float]],
) -> tuple[list[tuple[Any, float]], bool]:
    keywords = _extract_keywords(query)
    if not keywords:
        return results, False

    boosted_results: list[tuple[Any, float]] = []
    applied = False
    for entity, score in results:
        entity_text = _entity_text(entity)
        hit_count = sum(1 for keyword in keywords if keyword in entity_text)
        if hit_count:
            applied = True
            score *= 1.0 + min(0.75, 0.3 * (hit_count / len(keywords)))
        boosted_results.append((entity, score))

    boosted_results.sort(key=lambda item: item[1], reverse=True)
    return boosted_results, applied


def _apply_query_coverage_rerank(
    query: str,
    results: list[tuple[Any, float]],
    *,
    temporal_target: datetime | None = None,
) -> tuple[list[tuple[Any, float]], bool, bool]:
    return rank_items_by_query_coverage(
        query,
        results,
        text_fn=_entity_text,
        id_fn=_entity_id,
        timestamp_fn=get_entity_timestamp,
        temporal_target=temporal_target,
    )


@dataclass
class HybridConfig:
    """Configuration for hybrid retrieval.

    Attributes:
        vector_weight: Weight for native vector/fulltext seed results.
        graph_weight: Weight for graph traversal results.
        rrf_k: RRF constant (higher = more uniform).
        graph_depth: Maximum depth for graph traversal.
        apply_temporal: Whether to apply temporal boosting.
        temporal_decay_days: Decay half-life for temporal boosting.
        apply_reranking: Whether to apply cross-encoder reranking after RRF.
            Optional: requires the `reranking` extra (sentence-transformers);
            no-ops with a clean fallback when the extra is absent.
        rerank_top_k: Number of top results to rerank (rest pass through).
        rerank_model: Cross-encoder model for reranking.
    """

    vector_weight: float = 1.0
    graph_weight: float = 0.8
    rrf_k: float = 60.0
    graph_depth: int = 2
    apply_temporal: bool = True
    temporal_decay_days: float = 365.0
    # Cross-encoder reranking (disabled by default for performance)
    apply_reranking: bool = False
    rerank_top_k: int = 20
    rerank_model: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"
    apply_keyword_boost: bool = True
    apply_query_coverage_rerank: bool = True
    apply_query_entity_linking: bool = True
    graph_expansion_only_boost: float = 0.45
    graph_relationship_type_weights: Mapping[str, float] = field(
        default_factory=lambda: dict(DEFAULT_GRAPH_RELATIONSHIP_TYPE_WEIGHTS)
    )
    reference_time: datetime | None = None


@dataclass
class HybridResult:
    """Result from hybrid search.

    Attributes:
        results: List of (entity, score) tuples.
        metadata: Additional information about the search.
    """

    results: list[tuple[Any, float]]
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def entities(self) -> list[Any]:
        """Get just the entities."""
        return [e for e, _ in self.results]

    @property
    def total(self) -> int:
        """Number of results."""
        return len(self.results)


def _initial_reranking_receipt(config: HybridConfig) -> dict[str, Any]:
    receipt: dict[str, Any] = {
        "enabled": config.apply_reranking,
        "applied": False,
        "reranked_count": 0,
    }
    if config.apply_reranking:
        receipt["requested_model"] = config.rerank_model
        receipt["top_k"] = config.rerank_top_k
    else:
        receipt["reranking_skipped"] = "disabled"
    return receipt


@dataclass
class _VectorSearchAttempt:
    results: list[tuple[Any, float]]
    completed: bool


async def _vector_search_attempt(
    query: str,
    entity_manager: Any,
    entity_types: list[Any] | None = None,
    limit: int = 20,
    result_filter: Callable[[Any], bool] | None = None,
) -> _VectorSearchAttempt:
    try:
        results = await entity_manager.search(
            query=query,
            entity_types=entity_types,
            limit=limit,
        )
        results = _filter_entity_results(results, result_filter)
        log.debug("vector_search_complete", **query_log_fields(query), results=len(results))
        return _VectorSearchAttempt(results=results, completed=True)
    except Exception as e:
        log.warning("vector_search_failed", **query_log_fields(query), error_type=type(e).__name__)
        return _VectorSearchAttempt(results=[], completed=False)


def _entity_matches_types(entity: Any, entity_types: list[Any] | None) -> bool:
    if not entity_types:
        return True
    entity_type = (
        entity.get("entity_type")
        if isinstance(entity, dict)
        else getattr(entity, "entity_type", None)
    )
    if entity_type is None:
        return False
    normalized = str(getattr(entity_type, "value", entity_type)).lower()
    return normalized in {
        str(getattr(candidate, "value", candidate)).lower() for candidate in entity_types
    }


def _entity_id(entity: Any) -> str:
    return str(entity.get("id", "") if isinstance(entity, dict) else getattr(entity, "id", ""))


def _diagnostic_entity_metadata(entity: Any) -> dict[str, Any]:
    metadata = (
        entity.get("metadata", {}) if isinstance(entity, dict) else getattr(entity, "metadata", {})
    )
    if not isinstance(metadata, Mapping):
        return {}

    bounded: dict[str, Any] = {}
    for key, value in list(metadata.items())[:64]:
        if isinstance(value, str):
            bounded[str(key)] = value[:500]
        elif value is None or isinstance(value, bool | int | float):
            bounded[str(key)] = value
        elif (
            isinstance(value, list | tuple)
            and len(value) <= 64
            and all(item is None or isinstance(item, bool | int | float | str) for item in value)
        ):
            bounded[str(key)] = [item[:500] if isinstance(item, str) else item for item in value]
    return bounded


def _generic_assistant_marker_count(entity: Any) -> int:
    return generic_assistant_marker_count(_entity_text(entity))


def _filter_entity_results(
    results: list[tuple[Any, float]],
    result_filter: Callable[[Any], bool] | None,
) -> list[tuple[Any, float]]:
    if result_filter is None:
        return results
    return [(entity, score) for entity, score in results if result_filter(entity)]


async def _hydrate_lightweight_candidates(
    results: list[tuple[Any, float]],
    entity_manager: Any,
    source_metadata: Mapping[str, Mapping[str, Any]],
) -> tuple[list[tuple[Any, float]], dict[str, Any]]:
    graph_expansion_only_ids = {
        entity_id
        for entity_id, metadata in source_metadata.items()
        if metadata.get("graph_expansion_only") is True
    }
    missing_content_ids = {
        _entity_id(entity)
        for entity, _score in results
        if _entity_id(entity) and not getattr(entity, "content", None)
    }
    missing_ids = list(
        dict.fromkeys(
            _entity_id(entity)
            for entity, _score in results
            if _entity_id(entity) in graph_expansion_only_ids | missing_content_ids
        )
    )
    receipt: dict[str, Any] = {
        "requested_count": len(missing_ids),
        "hydrated_count": 0,
        "graph_expansion_only_count": len(graph_expansion_only_ids),
        "missing_content_count": len(missing_content_ids),
    }
    if not missing_ids:
        return results, receipt

    get_many = getattr(entity_manager, "get_many", None)
    if not callable(get_many):
        receipt["skipped"] = "batch_read_unavailable"
        return results, receipt

    try:
        hydrated = await get_many(missing_ids)
    except Exception as exc:
        log.warning("candidate_hydration_failed", error_type=type(exc).__name__)
        receipt["failed"] = type(exc).__name__
        return results, receipt

    hydrated_by_id = {_entity_id(entity): entity for entity in hydrated}
    receipt["hydrated_count"] = len(hydrated_by_id)
    return [
        (hydrated_by_id.get(_entity_id(entity), entity), score) for entity, score in results
    ], receipt


async def vector_search(
    query: str,
    entity_manager: Any,
    entity_types: list[Any] | None = None,
    limit: int = 20,
) -> list[tuple[Any, float]]:
    """Perform the seed search for hybrid retrieval.

    Args:
        query: Search query.
        entity_manager: Entity manager for search.
        entity_types: Optional type filter.
        limit: Maximum results.

    Returns:
        List of (entity, score) tuples.
    """
    return (
        await _vector_search_attempt(
            query=query,
            entity_manager=entity_manager,
            entity_types=entity_types,
            limit=limit,
        )
    ).results


async def graph_traversal(
    seed_ids: list[str],
    client: Any,
    depth: int = 2,
    limit: int = 20,
    group_id: str | None = None,
    relationship_type_weights: Mapping[str, float] | None = None,
) -> list[tuple[Any, float]]:
    """Traverse graph from seed entities.

    Uses DEPENDS_ON, RELATES_TO, and BELONGS_TO relationships
    to find related entities.

    Args:
        seed_ids: Starting entity IDs.
        client: Graph client for queries.
        depth: Maximum traversal depth.
        limit: Maximum results.

    Returns:
        List of (entity, score) tuples where score decreases with depth.
    """
    if not seed_ids:
        return []

    resolved_group_id = _require_group_id(group_id, "graph traversal")
    from sibyl_core.services.graph import SurrealGraphClient

    if not isinstance(client, SurrealGraphClient):
        raise RuntimeError("Graph traversal requires a native graph client")

    try:
        return await _graph_traversal_via_relationship_manager(
            seed_ids,
            client,
            depth=depth,
            limit=limit,
            group_id=resolved_group_id,
            relationship_type_weights=relationship_type_weights,
        )
    except Exception as e:
        log.warning("graph_traversal_failed", seeds=seed_ids, error=str(e))
        return []


async def _graph_traversal_via_relationship_manager(
    seed_ids: list[str],
    client: Any,
    *,
    depth: int,
    limit: int,
    group_id: str,
    relationship_type_weights: Mapping[str, float] | None,
) -> list[tuple[Entity, float]]:
    from sibyl_core.services.graph import (
        RelationshipManager,
        SurrealGraphClient,
    )

    if not isinstance(client, SurrealGraphClient):
        raise RuntimeError("Graph traversal requires a native graph client")

    relationship_manager = RelationshipManager(client, group_id=group_id)
    effective_relationship_type_weights = (
        DEFAULT_GRAPH_RELATIONSHIP_TYPE_WEIGHTS
        if relationship_type_weights is None
        else relationship_type_weights
    )

    seed_id_set = {seed_id for seed_id in seed_ids if seed_id}
    frontier = [seed_id for seed_id in seed_ids if seed_id]
    visited = set(seed_id_set)
    results_by_id: dict[str, tuple[Entity, float, int]] = {}

    for current_depth in range(1, max(depth, 0) + 1):
        if not frontier or len(results_by_id) >= limit:
            break

        batch_method = getattr(type(relationship_manager), "get_related_entities_batch", None)
        if batch_method is not None:
            related_by_seed = await relationship_manager.get_related_entities_batch(
                frontier,
                limit_per_entity=max(limit * 2, 50),
            )
        else:
            related_by_seed = {}
            for entity_id in frontier:
                related_by_seed[entity_id] = await relationship_manager.get_related_entities(
                    entity_id=entity_id,
                    max_depth=1,
                    limit=max(limit * 2, 50),
                )

        tier_candidates: dict[str, tuple[Entity, float, int]] = {}
        for entity_id in frontier:
            related = related_by_seed.get(entity_id, [])
            for entity, relationship in related:
                if not entity.id or entity.id in seed_id_set or entity.id in visited:
                    continue

                score = _graph_relationship_score(
                    relationship,
                    depth=current_depth,
                    relationship_type_weights=effective_relationship_type_weights,
                )
                existing = tier_candidates.get(entity.id)
                if existing is None or score > existing[1]:
                    tier_candidates[entity.id] = (entity, score, current_depth)

        ordered_tier = sorted(
            tier_candidates.values(),
            key=lambda item: (-item[1], item[0].name.lower(), item[0].id),
        )
        remaining = max(limit - len(results_by_id), 0)
        selected_tier = ordered_tier[:remaining]
        for entity, score, candidate_depth in selected_tier:
            results_by_id[entity.id] = (entity, score, candidate_depth)
            visited.add(entity.id)

        frontier = [entity.id for entity, _score, _depth in selected_tier]

    if not results_by_id:
        return []

    ordered = sorted(
        results_by_id.values(),
        key=lambda item: (item[2], -item[1], item[0].name.lower(), item[0].id),
    )
    results = [(entity, score) for entity, score, _distance in ordered[:limit]]
    log.debug(
        "graph_traversal_complete",
        seeds=len(seed_ids),
        depth=depth,
        results=len(results),
        strategy="relationship_manager",
    )
    return results


def _graph_relationship_score(
    relationship: Any,
    *,
    depth: int,
    relationship_type_weights: Mapping[str, float] | None,
) -> float:
    distance_score = 1.0 / (depth + 1)
    return (
        distance_score
        * _relationship_weight(relationship)
        * _relationship_type_multiplier(relationship, relationship_type_weights)
    )


def _relationship_weight(relationship: Any) -> float:
    weight = getattr(relationship, "weight", 1.0)
    if isinstance(weight, int | float) and not isinstance(weight, bool):
        return max(float(weight), 0.0)
    return 1.0


def _relationship_type_multiplier(
    relationship: Any,
    relationship_type_weights: Mapping[str, float] | None,
) -> float:
    if not relationship_type_weights:
        return 1.0
    relationship_type = getattr(relationship, "relationship_type", None)
    key = str(getattr(relationship_type, "value", relationship_type) or "")
    if not key:
        return 1.0
    sentinel = object()
    multiplier = relationship_type_weights.get(key, sentinel)
    if multiplier is sentinel:
        multiplier = relationship_type_weights.get(key.upper(), sentinel)
    if isinstance(multiplier, int | float) and not isinstance(multiplier, bool):
        return max(float(multiplier), 0.0)
    return 1.0


async def hybrid_search(
    query: str,
    client: Any,
    entity_manager: Any,
    entity_types: list[Any] | None = None,
    limit: int = 10,
    config: HybridConfig | None = None,
    include_metadata: bool = False,
    group_id: str | None = None,
    result_filter: Callable[[Any], bool] | None = None,
) -> HybridResult:
    """Perform hybrid search combining multiple retrieval strategies.

    Strategy:
    1. Run native vector/fulltext search for initial seed results
    2. Use top seed results as inputs for graph traversal
    3. Merge seed and graph-traversal results using RRF
    4. Optionally apply temporal boosting

    Args:
        query: Search query.
        client: Graph client.
        entity_manager: Entity manager.
        entity_types: Optional type filter.
        limit: Maximum results.
        config: Hybrid configuration.
        include_metadata: Include detailed source metadata.

    Returns:
        HybridResult with merged, scored results.
    """
    search_started_at = time.perf_counter()
    stage_timings_ms = {
        "seed_search": 0.0,
        "entity_linking": 0.0,
        "graph_traversal": 0.0,
        "fusion": 0.0,
        "candidate_hydration": 0.0,
        "reranking": 0.0,
        "keyword_boost": 0.0,
        "temporal_boost": 0.0,
        "query_coverage": 0.0,
        "total": 0.0,
    }
    if config is None:
        config = HybridConfig()
    reranking_receipt = _initial_reranking_receipt(config)

    resolved_group_id = _resolve_group_id(entity_manager, group_id)

    log.info("hybrid_search_start", **query_log_fields(query), limit=limit)

    # Phase 1: native vector/fulltext seed search
    stage_started_at = time.perf_counter()
    vector_attempt = await _vector_search_attempt(
        query,
        entity_manager,
        entity_types,
        limit=limit * 2,
        result_filter=result_filter,
    )
    vector_results = vector_attempt.results
    stage_timings_ms["seed_search"] = _elapsed_ms(stage_started_at)

    stage_started_at = time.perf_counter()
    link_search_skipped = False
    link_attempt = _VectorSearchAttempt([], True)
    should_link_entities = (
        entity_types and config.apply_query_entity_linking and config.graph_weight > 0
    )
    if should_link_entities:
        if len(vector_results) >= MIN_PRIMARY_SEEDS_BEFORE_LINKING:
            link_search_skipped = True
        else:
            link_attempt = await _vector_search_attempt(
                query,
                entity_manager,
                None,
                limit=limit * 2,
                result_filter=result_filter,
            )
    vector_ids = {_entity_id(entity) for entity, _score in vector_results}
    link_results = [
        result for result in link_attempt.results if _entity_id(result[0]) not in vector_ids
    ]
    stage_timings_ms["entity_linking"] = _elapsed_ms(stage_started_at)

    # Phase 2: Graph traversal from top vector results
    stage_started_at = time.perf_counter()
    graph_results: list[tuple[Any, float]] = []
    graph_seed_results = [*vector_results[:5], *link_results[:5]]
    if graph_seed_results and config.graph_weight > 0:
        # Use top 5 results as seeds
        seed_ids = [_entity_id(e) for e, _ in graph_seed_results[:8]]
        seed_ids = [sid for sid in seed_ids if sid]

        if seed_ids:
            graph_results = await graph_traversal(
                seed_ids,
                client,
                depth=config.graph_depth,
                limit=limit * 2,
                group_id=resolved_group_id,
                relationship_type_weights=config.graph_relationship_type_weights,
            )
            graph_results = _filter_entity_results(graph_results, result_filter)
            if entity_types:
                graph_results = [
                    (entity, score)
                    for entity, score in graph_results
                    if _entity_matches_types(entity, entity_types)
                ]
    stage_timings_ms["graph_traversal"] = _elapsed_ms(stage_started_at)

    # Phase 3: Merge results using RRF
    stage_started_at = time.perf_counter()
    result_lists = []
    weights = []
    list_names = []

    if vector_results:
        result_lists.append(vector_results)
        weights.append(config.vector_weight)
        list_names.append("vector")

    if graph_results:
        result_lists.append(graph_results)
        weights.append(config.graph_weight)
        list_names.append("graph")

    if not result_lists:
        stage_timings_ms["fusion"] = _elapsed_ms(stage_started_at)
        stage_timings_ms["total"] = _elapsed_ms(search_started_at)
        if config.apply_reranking:
            reranking_receipt["reranking_skipped"] = "no_results"
        return HybridResult(
            results=[],
            metadata={
                "sources": [],
                "query": query,
                "entity_manager_search_completed": vector_attempt.completed,
                "link_search_completed": link_attempt.completed,
                "link_search_skipped": link_search_skipped,
                "vector_count": len(vector_results),
                "link_count": len(link_results),
                "graph_count": len(graph_results),
                "reranking_applied": False,
                "reranking": reranking_receipt,
                "stage_timings_ms": stage_timings_ms,
            },
        )

    merged_with_meta = rrf_merge_with_metadata(
        result_lists,
        list_names=list_names,
        k=config.rrf_k,
        weights=weights,
    )
    has_vector_source = "vector" in list_names
    merged = []
    source_metadata = {}
    for entity, score, metadata in merged_with_meta:
        sources = set(metadata.get("sources") or [])
        graph_expansion_only = has_vector_source and sources == {"graph"}
        if graph_expansion_only:
            score *= config.graph_expansion_only_boost
        merged.append((entity, score))
        source_metadata[_entity_id(entity)] = {
            **metadata,
            "graph_expansion_only": graph_expansion_only,
            "fused_score": score,
        }
    merged.sort(key=lambda item: item[1], reverse=True)
    stage_timings_ms["fusion"] = _elapsed_ms(stage_started_at)

    stage_started_at = time.perf_counter()
    merged, candidate_hydration = await _hydrate_lightweight_candidates(
        merged,
        entity_manager,
        source_metadata,
    )
    stage_timings_ms["candidate_hydration"] = _elapsed_ms(stage_started_at)

    # Phase 4: Apply cross-encoder reranking (optional)
    stage_started_at = time.perf_counter()
    reranking_applied = False
    if config.apply_reranking and merged:
        try:
            from sibyl_core.retrieval.reranking import CrossEncoderConfig, rerank_results

            rerank_config = CrossEncoderConfig(
                enabled=True,
                model_name=config.rerank_model,
                top_k=config.rerank_top_k,
                fallback_on_error=True,
            )
            rerank_result = await rerank_results(query, merged, rerank_config)
            merged = rerank_result.results
            reranking_applied = rerank_result.reranked_count > 0
            reranking_receipt.update(rerank_result.metadata)
            reranking_receipt["applied"] = reranking_applied
            reranking_receipt["reranked_count"] = rerank_result.reranked_count
            reranking_receipt["model"] = rerank_result.model_name
            log.debug(
                "reranking_complete",
                reranked_count=rerank_result.reranked_count,
                model=rerank_result.model_name,
            )
        except Exception as e:
            log.warning("reranking_failed_continuing", error=str(e))
            reranking_receipt["reranking_failed"] = type(e).__name__
    stage_timings_ms["reranking"] = _elapsed_ms(stage_started_at)

    # Phase 5: Apply lightweight lexical boosting
    stage_started_at = time.perf_counter()
    keyword_boost_applied = False
    if config.apply_keyword_boost and merged:
        boosted, keyword_boost_applied = _apply_keyword_boost(query, merged)
        merged = boosted
    stage_timings_ms["keyword_boost"] = _elapsed_ms(stage_started_at)

    # Phase 6: Apply temporal boosting
    stage_started_at = time.perf_counter()
    temporal_target = resolve_temporal_reference(query, config.reference_time)
    if config.apply_temporal and merged:
        if temporal_target is not None:
            merged = temporal_proximity_boost(merged, target_time=temporal_target)
        else:
            merged = temporal_boost(
                merged,
                decay_days=config.temporal_decay_days,
                reference_time=config.reference_time,
            )
    stage_timings_ms["temporal_boost"] = _elapsed_ms(stage_started_at)

    stage_started_at = time.perf_counter()
    query_coverage_rerank_applied = False
    query_coverage_refinement_applied = False
    if config.apply_query_coverage_rerank and merged:
        (
            merged,
            query_coverage_rerank_applied,
            query_coverage_refinement_applied,
        ) = await asyncio.to_thread(
            _apply_query_coverage_rerank,
            query,
            merged,
            temporal_target=temporal_target,
        )
    stage_timings_ms["query_coverage"] = _elapsed_ms(stage_started_at)
    stage_timings_ms["total"] = _elapsed_ms(search_started_at)

    # Trim to limit
    final_results = merged[:limit]

    metadata: dict[str, Any] = {
        "query": query,
        "sources": list_names,
        "entity_manager_search_completed": vector_attempt.completed,
        "link_search_completed": link_attempt.completed,
        "link_search_skipped": link_search_skipped,
        "vector_count": len(vector_results),
        "link_count": len(link_results),
        "graph_count": len(graph_results),
        "merged_count": len(merged),
        "candidate_hydration": candidate_hydration,
        "reranking_applied": reranking_applied,
        "reranking": reranking_receipt,
        "keyword_boost_applied": keyword_boost_applied,
        "query_coverage_rerank_applied": query_coverage_rerank_applied,
        "query_coverage_refinement_applied": query_coverage_refinement_applied,
        "temporal_applied": config.apply_temporal,
        "temporal_target": temporal_target.isoformat() if temporal_target else None,
        "stage_timings_ms": stage_timings_ms,
    }

    if include_metadata:
        metadata["source_details"] = source_metadata
        metadata["ranking_trace"] = [
            {
                "entity_id": _entity_id(entity),
                "rank": rank,
                "score": score,
                "sources": source_metadata.get(_entity_id(entity), {}).get("sources", []),
                "metadata": _diagnostic_entity_metadata(entity),
            }
            for rank, (entity, score) in enumerate(merged, start=1)
        ]

    log.info(
        "hybrid_search_complete",
        **query_log_fields(query),
        results=len(final_results),
        **{
            f"{n}_count": c
            for n, c in zip(list_names, [len(r) for r in result_lists], strict=False)
        },
    )

    return HybridResult(results=final_results, metadata=metadata)


def _elapsed_ms(started_at: float) -> float:
    return (time.perf_counter() - started_at) * 1000.0


async def simple_hybrid_search(
    query: str,
    entity_manager: Any,
    entity_types: list[Any] | None = None,
    limit: int = 10,
    apply_temporal: bool = True,
) -> list[tuple[Any, float]]:
    """Simplified hybrid search using just vector + temporal.

    For cases where graph traversal isn't needed or available.

    Args:
        query: Search query.
        entity_manager: Entity manager.
        entity_types: Optional type filter.
        limit: Maximum results.
        apply_temporal: Whether to apply temporal boosting.

    Returns:
        List of (entity, score) tuples.
    """
    results = await vector_search(query, entity_manager, entity_types, limit * 2)

    if apply_temporal and results:
        results = temporal_boost(results)

    return results[:limit]
