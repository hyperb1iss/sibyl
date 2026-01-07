"""Optimized search interface for FalkorDB.

Graphiti's default edge_fulltext_search does:
  CALL db.idx.fulltext.queryRelationships(...)
  YIELD relationship AS rel, score
  MATCH (n:Entity)-[e:RELATES_TO {uuid: rel.uuid}]->(m:Entity)

This causes a cartesian product (label scan * edge scan) that's O(n^2).
With 2,600 entities and 2,500 edges, queries take 800ms+ and timeout.

Our optimized version uses startNode(rel)/endNode(rel) directly on the
fulltext result, avoiding the expensive MATCH and running in ~0.3ms.
"""

from typing import Any

import structlog
from graphiti_core.driver.search_interface.search_interface import SearchInterface

log = structlog.get_logger()


class FalkorDBSearchInterface(SearchInterface):
    """Optimized search interface for FalkorDB.

    Overrides Graphiti's default search methods with more efficient queries
    that avoid cartesian products when looking up edges by UUID.
    """

    async def edge_fulltext_search(
        self,
        driver: Any,
        query: str,
        search_filter: Any,
        group_ids: list[str] | None = None,
        limit: int = 100,
    ) -> list[Any]:
        """Optimized edge fulltext search using startNode/endNode.

        Instead of:
            MATCH (n:Entity)-[e:RELATES_TO {uuid: rel.uuid}]->(m:Entity)
        We use:
            startNode(rel).uuid, endNode(rel).uuid

        This avoids the cartesian join that causes O(n²) performance.
        """
        from graphiti_core.edges import EntityEdge
        from graphiti_core.helpers import parse_db_date
        from graphiti_core.search.search_utils import fulltext_query

        fuzzy_query = fulltext_query(query, group_ids, driver)
        if fuzzy_query == "":
            return []

        # Build filter params
        filter_params: dict[str, Any] = {}
        if group_ids is not None:
            filter_params["group_ids"] = group_ids

        # Optimized query: use startNode/endNode directly instead of MATCH
        # This is the key performance fix - no cartesian product
        cypher_query = """
        CALL db.idx.fulltext.queryRelationships('RELATES_TO', $query)
        YIELD relationship AS rel, score
        WITH rel, score
        WHERE rel.group_id IN $group_ids
        RETURN
            rel.uuid AS uuid,
            startNode(rel).uuid AS source_node_uuid,
            endNode(rel).uuid AS target_node_uuid,
            rel.group_id AS group_id,
            rel.created_at AS created_at,
            rel.name AS name,
            rel.fact AS fact,
            CASE
                WHEN rel.episodes IS NULL THEN []
                WHEN typeOf(rel.episodes) = 'LIST' THEN rel.episodes
                ELSE split(rel.episodes, ",")
            END AS episodes,
            rel.expired_at AS expired_at,
            rel.valid_at AS valid_at,
            rel.invalid_at AS invalid_at,
            properties(rel) AS attributes
        ORDER BY score DESC
        LIMIT $limit
        """

        records, _, _ = await driver.execute_query(
            cypher_query,
            query=fuzzy_query,
            limit=limit,
            routing_="r",
            **filter_params,
        )

        # Convert records to EntityEdge objects
        edges = []
        for record in records:
            attributes = dict(record.get("attributes", {}))
            # Remove standard fields from attributes
            for key in [
                "uuid",
                "source_node_uuid",
                "target_node_uuid",
                "fact",
                "fact_embedding",
                "name",
                "group_id",
                "episodes",
                "created_at",
                "expired_at",
                "valid_at",
                "invalid_at",
            ]:
                attributes.pop(key, None)

            edge = EntityEdge(
                uuid=record["uuid"],
                source_node_uuid=record["source_node_uuid"],
                target_node_uuid=record["target_node_uuid"],
                fact=record["fact"],
                fact_embedding=record.get("fact_embedding"),
                name=record["name"],
                group_id=record["group_id"],
                episodes=record["episodes"],
                created_at=parse_db_date(record["created_at"]),  # type: ignore[arg-type]
                expired_at=parse_db_date(record["expired_at"]),
                valid_at=parse_db_date(record["valid_at"]),
                invalid_at=parse_db_date(record["invalid_at"]),
                attributes=attributes,
            )
            edges.append(edge)

        return edges

    async def edge_similarity_search(
        self,
        driver: Any,
        search_vector: list[float],
        source_node_uuid: str | None,
        target_node_uuid: str | None,
        search_filter: Any,
        group_ids: list[str] | None = None,
        limit: int = 100,
        min_score: float = 0.7,
    ) -> list[Any]:
        """Delegate to default Graphiti implementation."""
        from graphiti_core.search import search_utils

        # Temporarily clear search_interface to use default implementation
        original = driver.search_interface
        driver.search_interface = None
        try:
            return await search_utils.edge_similarity_search(
                driver,
                search_vector,
                source_node_uuid,
                target_node_uuid,
                search_filter,
                group_ids,
                limit,
                min_score,
            )
        finally:
            driver.search_interface = original

    async def node_fulltext_search(
        self,
        driver: Any,
        query: str,
        search_filter: Any,
        group_ids: list[str] | None = None,
        limit: int = 100,
    ) -> list[Any]:
        """Delegate to default Graphiti implementation."""
        from graphiti_core.search import search_utils

        original = driver.search_interface
        driver.search_interface = None
        try:
            return await search_utils.node_fulltext_search(
                driver, query, search_filter, group_ids, limit
            )
        finally:
            driver.search_interface = original

    async def node_similarity_search(
        self,
        driver: Any,
        search_vector: list[float],
        search_filter: Any,
        group_ids: list[str] | None = None,
        limit: int = 100,
        min_score: float = 0.7,
    ) -> list[Any]:
        """Delegate to default Graphiti implementation."""
        from graphiti_core.search import search_utils

        original = driver.search_interface
        driver.search_interface = None
        try:
            return await search_utils.node_similarity_search(
                driver, search_vector, search_filter, group_ids, limit, min_score
            )
        finally:
            driver.search_interface = original

    async def episode_fulltext_search(
        self,
        driver: Any,
        query: str,
        search_filter: Any,
        group_ids: list[str] | None = None,
        limit: int = 100,
    ) -> list[Any]:
        """Delegate to default Graphiti implementation."""
        from graphiti_core.search import search_utils

        original = driver.search_interface
        driver.search_interface = None
        try:
            return await search_utils.episode_fulltext_search(
                driver, query, search_filter, group_ids, limit
            )
        finally:
            driver.search_interface = original

    def build_node_search_filters(self, search_filters: Any) -> Any:
        """Not used - Graphiti handles filter building internally."""
        return search_filters

    def build_edge_search_filters(self, search_filters: Any) -> Any:
        """Not used - Graphiti handles filter building internally."""
        return search_filters
