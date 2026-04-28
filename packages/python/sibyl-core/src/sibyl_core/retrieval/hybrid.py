"""Hybrid retrieval combining vector search and graph traversal.

Implements a two-phase retrieval strategy:
1. Entity linking: Identify entities mentioned in the query
2. Parallel retrieval: Vector search + graph traversal from linked entities
3. Fusion: Merge results using RRF
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, TypeVar

import structlog

from sibyl_core.models.entities import Entity
from sibyl_core.retrieval.fusion import rrf_merge, rrf_merge_with_metadata
from sibyl_core.retrieval.temporal import temporal_boost
from sibyl_core.utils.log_safety import query_log_fields

if TYPE_CHECKING:
    from sibyl_core.graph.client import GraphClient
    from sibyl_core.graph.entities import EntityManager

log = structlog.get_logger()

T = TypeVar("T")


def _require_group_id(group_id: str | None, operation: str) -> str:
    """Require explicit org scope for graph-backed retrieval helpers."""
    if not group_id:
        raise ValueError(f"group_id is required for {operation}")
    return str(group_id)


def _resolve_group_id(entity_manager: EntityManager, group_id: str | None) -> str:
    """Resolve the organization scope for hybrid retrieval.

    Hybrid retrieval traverses the graph directly, so it must never fall back
    to the default graph in a multi-tenant deployment.
    """
    resolved = group_id or getattr(entity_manager, "_group_id", None)
    if not resolved:
        raise ValueError("group_id is required for hybrid retrieval")
    return str(resolved)


@dataclass
class HybridConfig:
    """Configuration for hybrid retrieval.

    Attributes:
        vector_weight: Weight for Graphiti node-hybrid seed results.
        graph_weight: Weight for graph traversal results.
        rrf_k: RRF constant (higher = more uniform).
        graph_depth: Maximum depth for graph traversal.
        apply_temporal: Whether to apply temporal boosting.
        temporal_decay_days: Decay half-life for temporal boosting.
        apply_reranking: Whether to apply cross-encoder reranking after RRF.
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


@dataclass
class _VectorSearchAttempt:
    results: list[tuple[Any, float]]
    completed: bool


async def _vector_search_attempt(
    query: str,
    entity_manager: EntityManager,
    entity_types: list[Any] | None = None,
    limit: int = 20,
) -> _VectorSearchAttempt:
    try:
        results = await entity_manager.search(
            query=query,
            entity_types=entity_types,
            limit=limit,
        )
        log.debug("vector_search_complete", **query_log_fields(query), results=len(results))
        return _VectorSearchAttempt(results=results, completed=True)
    except Exception as e:
        log.warning("vector_search_failed", **query_log_fields(query), error_type=type(e).__name__)
        return _VectorSearchAttempt(results=[], completed=False)


async def vector_search(
    query: str,
    entity_manager: EntityManager,
    entity_types: list[Any] | None = None,
    limit: int = 20,
) -> list[tuple[Any, float]]:
    """Perform Graphiti node-hybrid seed search.

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
    client: GraphClient,
    depth: int = 2,
    limit: int = 20,
    group_id: str | None = None,
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

    try:
        return await _graph_traversal_via_relationship_manager(
            seed_ids,
            client,
            depth=depth,
            limit=limit,
            group_id=resolved_group_id,
        )
    except Exception as e:
        log.warning("graph_traversal_failed", seeds=seed_ids, error=str(e))
        return []


async def _graph_traversal_via_relationship_manager(
    seed_ids: list[str],
    client: GraphClient,
    *,
    depth: int,
    limit: int,
    group_id: str,
) -> list[tuple[Entity, float]]:
    from sibyl_core.graph.relationships import RelationshipManager

    relationship_manager = RelationshipManager(client, group_id=group_id)

    seed_id_set = {seed_id for seed_id in seed_ids if seed_id}
    frontier = [seed_id for seed_id in seed_ids if seed_id]
    visited = set(seed_id_set)
    results_by_id: dict[str, tuple[Entity, float, int]] = {}

    for current_depth in range(1, max(depth, 0) + 1):
        if not frontier or len(results_by_id) >= limit:
            break

        next_frontier: list[str] = []
        for entity_id in frontier:
            related = await relationship_manager.get_related_entities(
                entity_id=entity_id,
                max_depth=1,
                limit=max(limit * 2, 50),
            )
            for entity, _relationship in related:
                if not entity.id or entity.id in seed_id_set or entity.id in visited:
                    continue

                visited.add(entity.id)
                score = 1.0 / (current_depth + 1)
                results_by_id[entity.id] = (entity, score, current_depth)
                next_frontier.append(entity.id)

                if len(results_by_id) >= limit:
                    break

            if len(results_by_id) >= limit:
                break

        frontier = next_frontier

    if not results_by_id:
        return []

    ordered = sorted(
        results_by_id.values(),
        key=lambda item: (item[2], item[0].name.lower(), item[0].id),
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


async def hybrid_search(
    query: str,
    client: GraphClient,
    entity_manager: EntityManager,
    entity_types: list[Any] | None = None,
    limit: int = 10,
    config: HybridConfig | None = None,
    include_metadata: bool = False,
    group_id: str | None = None,
) -> HybridResult:
    """Perform hybrid search combining multiple retrieval strategies.

    Strategy:
    1. Run Graphiti node-hybrid search for initial seed results
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
    if config is None:
        config = HybridConfig()

    resolved_group_id = _resolve_group_id(entity_manager, group_id)

    log.info("hybrid_search_start", **query_log_fields(query), limit=limit)

    # Phase 1: Graphiti node-hybrid seed search
    vector_task = asyncio.create_task(
        _vector_search_attempt(query, entity_manager, entity_types, limit=limit * 2)
    )

    # Get vector results first (we need them for graph seeds)
    vector_attempt = await vector_task
    vector_results = vector_attempt.results

    # Phase 2: Graph traversal from top vector results
    graph_results: list[tuple[Any, float]] = []
    if vector_results and config.graph_weight > 0:
        # Use top 5 results as seeds
        seed_ids = [e.id if hasattr(e, "id") else e.get("id", "") for e, _ in vector_results[:5]]
        seed_ids = [sid for sid in seed_ids if sid]

        if seed_ids:
            graph_results = await graph_traversal(
                seed_ids,
                client,
                depth=config.graph_depth,
                limit=limit * 2,
                group_id=resolved_group_id,
            )

    # Phase 3: Merge results using RRF
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
        return HybridResult(
            results=[],
            metadata={
                "sources": [],
                "query": query,
                "entity_manager_search_completed": vector_attempt.completed,
                "vector_count": len(vector_results),
                "graph_count": len(graph_results),
            },
        )

    # Merge with or without metadata
    if include_metadata:
        merged_with_meta = rrf_merge_with_metadata(
            result_lists,
            list_names=list_names,
            k=config.rrf_k,
            weights=weights,
            limit=limit * 2,  # Get extra for temporal filtering
        )
        merged = [(e, s) for e, s, _ in merged_with_meta]
        source_metadata = {
            e.id if hasattr(e, "id") else e.get("id", ""): m for e, _, m in merged_with_meta
        }
    else:
        merged = rrf_merge(
            result_lists,
            k=config.rrf_k,
            weights=weights,
            limit=limit * 2,
        )
        source_metadata = {}

    # Phase 4: Apply cross-encoder reranking (optional)
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
            log.debug(
                "reranking_complete",
                reranked_count=rerank_result.reranked_count,
                model=rerank_result.model_name,
            )
        except Exception as e:
            log.warning("reranking_failed_continuing", error=str(e))

    # Phase 5: Apply temporal boosting
    if config.apply_temporal and merged:
        merged = temporal_boost(
            merged,
            decay_days=config.temporal_decay_days,
        )

    # Trim to limit
    final_results = merged[:limit]

    metadata = {
        "query": query,
        "sources": list_names,
        "entity_manager_search_completed": vector_attempt.completed,
        "vector_count": len(vector_results),
        "graph_count": len(graph_results),
        "merged_count": len(merged),
        "reranking_applied": reranking_applied,
        "temporal_applied": config.apply_temporal,
    }

    if include_metadata:
        metadata["source_details"] = source_metadata

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


async def simple_hybrid_search(
    query: str,
    entity_manager: EntityManager,
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
