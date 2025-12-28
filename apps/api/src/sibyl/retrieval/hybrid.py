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

from sibyl.models.entities import Entity, EntityType
from sibyl.retrieval.fusion import rrf_merge, rrf_merge_with_metadata
from sibyl.retrieval.temporal import temporal_boost

if TYPE_CHECKING:
    from sibyl.graph.client import GraphClient
    from sibyl.graph.entities import EntityManager

log = structlog.get_logger()

T = TypeVar("T")


@dataclass
class HybridConfig:
    """Configuration for hybrid retrieval.

    Attributes:
        vector_weight: Weight for vector search results.
        graph_weight: Weight for graph traversal results.
        bm25_weight: Weight for BM25 keyword results.
        rrf_k: RRF constant (higher = more uniform).
        graph_depth: Maximum depth for graph traversal.
        apply_temporal: Whether to apply temporal boosting.
        temporal_decay_days: Decay half-life for temporal boosting.
    """

    vector_weight: float = 1.0
    graph_weight: float = 0.8
    bm25_weight: float = 0.5
    rrf_k: float = 60.0
    graph_depth: int = 2
    apply_temporal: bool = True
    temporal_decay_days: float = 365.0


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


async def vector_search(
    query: str,
    entity_manager: EntityManager,
    entity_types: list[Any] | None = None,
    limit: int = 20,
) -> list[tuple[Any, float]]:
    """Perform vector similarity search.

    Args:
        query: Search query.
        entity_manager: Entity manager for search.
        entity_types: Optional type filter.
        limit: Maximum results.

    Returns:
        List of (entity, score) tuples.
    """
    try:
        results = await entity_manager.search(
            query=query,
            entity_types=entity_types,
            limit=limit,
        )
        log.debug("vector_search_complete", query=query[:50], results=len(results))
        return results
    except Exception as e:
        log.warning("vector_search_failed", query=query[:50], error=str(e))
        return []


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

    try:
        group_filter = ""
        params: dict[str, Any] = {"seed_ids": seed_ids, "limit": limit}
        if group_id:
            group_filter = "AND seed.group_id = $group_id AND related.group_id = $group_id"
            params["group_id"] = group_id

        # Query for related entities up to depth
        query = f"""
        MATCH (seed)-[r:RELATIONSHIP*1..{depth}]-(related)
        WHERE seed.uuid IN $seed_ids
          AND NOT related.uuid IN $seed_ids
          {group_filter}
        RETURN DISTINCT related.uuid as id,
               related.name as name,
               related.entity_type as type,
               related.description as description,
               min(length(r)) as distance
        ORDER BY distance
        LIMIT $limit
        """

        from sibyl.graph.client import GraphClient

        result = await client.client.driver.execute_query(query, **params)
        rows = GraphClient.normalize_result(result)

        # Convert to Entity objects with distance-based scores
        results: list[tuple[Entity, float]] = []
        for record in rows:
            if isinstance(record, (list, tuple)):
                entity_id = record[0] if len(record) > 0 else None
                name = record[1] if len(record) > 1 else ""
                entity_type = record[2] if len(record) > 2 else ""
                description = record[3] if len(record) > 3 else ""
                distance = record[4] if len(record) > 4 else 1
            else:
                entity_id = record.get("id")
                name = record.get("name", "")
                entity_type = record.get("type", "")
                description = record.get("description", "")
                distance = record.get("distance", 1)

            if entity_id:
                # Create proper Entity object to match vector search results
                try:
                    etype = EntityType(entity_type) if entity_type else EntityType.EPISODE
                except ValueError:
                    etype = EntityType.EPISODE

                entity = Entity(
                    id=entity_id,
                    name=name or entity_id,
                    entity_type=etype,
                    description=description or "",
                )
                # Score decreases with distance
                dist = int(distance) if distance else 1
                score = 1.0 / (dist + 1)
                results.append((entity, score))

        log.debug(
            "graph_traversal_complete",
            seeds=len(seed_ids),
            depth=depth,
            results=len(results),
        )
        return results

    except Exception as e:
        log.warning("graph_traversal_failed", seeds=seed_ids, error=str(e))
        return []


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
    1. Run vector search in parallel with BM25 search
    2. Use top vector results as seeds for graph traversal
    3. Merge all results using RRF
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

    log.info("hybrid_search_start", query=query[:50], limit=limit)

    # Phase 1: Parallel vector and BM25 search
    vector_task = asyncio.create_task(
        vector_search(query, entity_manager, entity_types, limit=limit * 2)
    )

    # Get vector results first (we need them for graph seeds)
    vector_results = await vector_task

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
                group_id=group_id,
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
        return HybridResult(results=[], metadata={"sources": [], "query": query})

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

    # Phase 4: Apply temporal boosting
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
        "vector_count": len(vector_results),
        "graph_count": len(graph_results),
        "merged_count": len(merged),
        "temporal_applied": config.apply_temporal,
    }

    if include_metadata:
        metadata["source_details"] = source_metadata

    log.info(
        "hybrid_search_complete",
        query=query[:50],
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
