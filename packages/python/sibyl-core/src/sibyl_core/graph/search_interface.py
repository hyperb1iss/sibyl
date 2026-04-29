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

from copy import copy
from typing import Any

import structlog
from graphiti_core.driver.record_parsers import (
    community_node_from_record,
    entity_edge_from_record,
    entity_node_from_record,
    episodic_node_from_record,
)
from graphiti_core.driver.search_interface.search_interface import SearchInterface
from graphiti_core.nodes import CommunityNode, EpisodicNode

from sibyl_core.graph.surreal.ops._common import normalize_records
from sibyl_core.graph.surreal.ops.entity_edge_ops import _ENTITY_EDGE_SELECT

log = structlog.get_logger()


def _group_filter_clause(group_ids: list[str] | None) -> str:
    return "group_id IN $group_ids" if group_ids is not None else "true"


def _node_filter_clause(search_filter: Any) -> tuple[list[str], dict[str, Any]]:
    clauses: list[str] = []
    params: dict[str, Any] = {}
    node_labels = getattr(search_filter, "node_labels", None)
    if node_labels:
        clauses.append("labels CONTAINS $node_label")
        params["node_label"] = node_labels[0]
    return clauses, params


def _edge_filter_clause(
    search_filter: Any,
    *,
    source_node_uuid: str | None = None,
    target_node_uuid: str | None = None,
) -> tuple[list[str], dict[str, Any]]:
    clauses: list[str] = []
    params: dict[str, Any] = {}

    edge_uuids = getattr(search_filter, "edge_uuids", None)
    if edge_uuids:
        clauses.append("uuid IN $edge_uuids")
        params["edge_uuids"] = edge_uuids

    edge_types = getattr(search_filter, "edge_types", None)
    if edge_types:
        clauses.append("name IN $edge_types")
        params["edge_types"] = edge_types

    node_labels = getattr(search_filter, "node_labels", None)
    if node_labels:
        clauses.append("in.labels CONTAINS $node_label AND out.labels CONTAINS $node_label")
        params["node_label"] = node_labels[0]

    if source_node_uuid is not None:
        clauses.append("in.uuid = $source_node_uuid")
        params["source_node_uuid"] = source_node_uuid

    if target_node_uuid is not None:
        clauses.append("out.uuid = $target_node_uuid")
        params["target_node_uuid"] = target_node_uuid

    for field in ("valid_at", "invalid_at", "created_at", "expired_at"):
        if temporal_clause := _temporal_filter_clause(search_filter, field, params):
            clauses.append(temporal_clause)

    return clauses, params


def _where_clause(clauses: list[str]) -> str:
    return " AND ".join(clauses) if clauses else "true"


def _temporal_filter_clause(
    search_filter: Any,
    field: str,
    params: dict[str, Any],
) -> str:
    filters = getattr(search_filter, field, None)
    if not filters:
        return ""

    or_clauses: list[str] = []
    for or_index, and_filters in enumerate(filters):
        and_clauses: list[str] = []
        for and_index, date_filter in enumerate(and_filters):
            operator = date_filter.comparison_operator.value
            if operator == "IS NULL":
                and_clauses.append(f"{field} IS NONE")
                continue
            if operator == "IS NOT NULL":
                and_clauses.append(f"{field} IS NOT NONE")
                continue

            param_name = f"{field}_{or_index}_{and_index}"
            params[param_name] = date_filter.date
            and_clauses.append(f"{field} {operator} ${param_name}")

        if and_clauses:
            or_clauses.append("(" + " AND ".join(and_clauses) + ")")

    if not or_clauses:
        return ""
    return "(" + " OR ".join(or_clauses) + ")"


def _episode_from_record(record: dict[str, Any]) -> EpisodicNode:
    record["source_description"] = record.get("source_description") or ""
    record.setdefault("entity_edges", [])
    return episodic_node_from_record(record)


def _community_from_record(record: dict[str, Any]) -> CommunityNode:
    record.setdefault("summary", "")
    record.setdefault("name_embedding", None)
    return community_node_from_record(record)


class SurrealSearchInterface(SearchInterface):
    """Native Graphiti search adapter for SurrealDB."""

    async def node_fulltext_search(
        self,
        driver: Any,
        query: str,
        search_filter: Any,
        group_ids: list[str] | None = None,
        limit: int = 100,
    ) -> list[Any]:
        search_query = driver.build_fulltext_query(query)
        if not search_query:
            return []

        filter_clauses, filter_params = _node_filter_clause(search_filter)
        records = normalize_records(
            await driver.execute_query(
                """
                SELECT *,
                       math::max([
                           search::score(0),
                           search::score(1),
                           search::score(2),
                           search::score(3)
                       ]) AS score
                FROM entity
                WHERE """
                + _where_clause([_group_filter_clause(group_ids), *filter_clauses])
                + """
                  AND (
                      name @0@ $query
                      OR summary @1@ $query
                      OR attributes.description @2@ $query
                      OR attributes.content @3@ $query
                  )
                ORDER BY score DESC, created_at DESC, uuid DESC
                LIMIT $limit;
                """,
                query=search_query,
                group_ids=group_ids,
                limit=max(int(limit), 1),
                **filter_params,
            )
        )
        return [entity_node_from_record(record) for record in records]

    async def node_similarity_search(
        self,
        driver: Any,
        search_vector: list[float],
        search_filter: Any,
        group_ids: list[str] | None = None,
        limit: int = 100,
        min_score: float = 0.7,
    ) -> list[Any]:
        if not search_vector:
            return []

        filter_clauses, filter_params = _node_filter_clause(search_filter)
        candidate_limit = max(int(limit) * 4, int(limit), 1)
        records = normalize_records(
            await driver.execute_query(
                "SELECT * FROM ("
                "SELECT *, (1 - vector::distance::knn()) AS score FROM entity "
                "WHERE "
                + _where_clause([_group_filter_clause(group_ids), *filter_clauses])
                + " AND name_embedding IS NOT NONE "
                f"AND name_embedding <|{candidate_limit}, 40|> $search_vector"
                ") WHERE score > $min_score "
                "ORDER BY score DESC, created_at DESC, uuid DESC LIMIT $limit;",
                search_vector=search_vector,
                min_score=min_score,
                group_ids=group_ids,
                limit=max(int(limit), 1),
                **filter_params,
            )
        )
        return [entity_node_from_record(record) for record in records]

    async def edge_fulltext_search(
        self,
        driver: Any,
        query: str,
        search_filter: Any,
        group_ids: list[str] | None = None,
        limit: int = 100,
    ) -> list[Any]:
        search_query = driver.build_fulltext_query(query)
        if not search_query:
            return []

        filter_clauses, filter_params = _edge_filter_clause(search_filter)
        fulltext_select = _ENTITY_EDGE_SELECT.replace(
            "FROM relates_to",
            ", search::score(0) AS score\nFROM relates_to",
        )
        records = normalize_records(
            await driver.execute_query(
                fulltext_select
                + " WHERE "
                + _where_clause([_group_filter_clause(group_ids), *filter_clauses])
                + """
                  AND fact @0@ $query
                ORDER BY score DESC, created_at DESC, uuid DESC
                LIMIT $limit;
                """,
                query=search_query,
                group_ids=group_ids,
                limit=max(int(limit), 1),
                **filter_params,
            )
        )
        return [entity_edge_from_record(record) for record in records]

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
        if not search_vector:
            return []

        filter_clauses, filter_params = _edge_filter_clause(
            search_filter,
            source_node_uuid=source_node_uuid,
            target_node_uuid=target_node_uuid,
        )
        candidate_limit = max(int(limit) * 4, int(limit), 1)
        vector_select = _ENTITY_EDGE_SELECT.replace(
            "FROM relates_to",
            ", (1 - vector::distance::knn()) AS score\nFROM relates_to",
        )
        records = normalize_records(
            await driver.execute_query(
                "SELECT * FROM ("
                + vector_select
                + "WHERE "
                + _where_clause([_group_filter_clause(group_ids), *filter_clauses])
                + " AND fact_embedding IS NOT NONE "
                f"AND fact_embedding <|{candidate_limit}, 40|> $search_vector"
                ") WHERE score > $min_score "
                "ORDER BY score DESC, created_at DESC, uuid DESC LIMIT $limit;",
                search_vector=search_vector,
                min_score=min_score,
                group_ids=group_ids,
                limit=max(int(limit), 1),
                **filter_params,
            )
        )
        return [entity_edge_from_record(record) for record in records]

    async def episode_fulltext_search(
        self,
        driver: Any,
        query: str,
        search_filter: Any,
        group_ids: list[str] | None = None,
        limit: int = 100,
    ) -> list[Any]:
        del search_filter
        search_query = driver.build_fulltext_query(query)
        if not search_query:
            return []

        records = normalize_records(
            await driver.execute_query(
                """
                SELECT *, search::score(0) AS score
                FROM episode
                WHERE """
                + _group_filter_clause(group_ids)
                + """
                  AND content @0@ $query
                ORDER BY score DESC, created_at DESC, uuid DESC
                LIMIT $limit;
                """,
                query=search_query,
                group_ids=group_ids,
                limit=max(int(limit), 1),
            )
        )
        return [_episode_from_record(record) for record in records]

    async def community_fulltext_search(
        self,
        driver: Any,
        query: str,
        group_ids: list[str] | None = None,
        limit: int = 100,
    ) -> list[Any]:
        search_query = driver.build_fulltext_query(query)
        if not search_query:
            return []

        records = normalize_records(
            await driver.execute_query(
                """
                SELECT *,
                       math::max([search::score(0), search::score(1)]) AS score
                FROM community
                WHERE """
                + _group_filter_clause(group_ids)
                + """
                  AND (
                      name @0@ $query
                      OR summary @1@ $query
                  )
                ORDER BY score DESC, created_at DESC, uuid DESC
                LIMIT $limit;
                """,
                query=search_query,
                group_ids=group_ids,
                limit=max(int(limit), 1),
            )
        )
        return [_community_from_record(record) for record in records]

    async def community_similarity_search(
        self,
        driver: Any,
        search_vector: list[float],
        group_ids: list[str] | None = None,
        limit: int = 100,
        min_score: float = 0.6,
    ) -> list[Any]:
        if not search_vector:
            return []

        candidate_limit = max(int(limit) * 4, int(limit), 1)
        records = normalize_records(
            await driver.execute_query(
                "SELECT * FROM ("
                "SELECT *, (1 - vector::distance::knn()) AS score FROM community "
                "WHERE "
                + _group_filter_clause(group_ids)
                + " AND name_embedding IS NOT NONE "
                f"AND name_embedding <|{candidate_limit}, 40|> $search_vector"
                ") WHERE score > $min_score "
                "ORDER BY score DESC, created_at DESC, uuid DESC LIMIT $limit;",
                search_vector=search_vector,
                min_score=min_score,
                group_ids=group_ids,
                limit=max(int(limit), 1),
            )
        )
        return [_community_from_record(record) for record in records]

    async def get_embeddings_for_communities(
        self,
        driver: Any,
        communities: list[Any],
    ) -> dict[str, list[float]]:
        uuids = [community.uuid for community in communities]
        if not uuids:
            return {}
        records = normalize_records(
            await driver.execute_query(
                "SELECT uuid, name_embedding FROM community WHERE uuid IN $uuids;",
                uuids=uuids,
            )
        )
        return {
            record["uuid"]: record["name_embedding"]
            for record in records
            if record.get("name_embedding") is not None
        }


class FalkorDBSearchInterface(SearchInterface):
    """Optimized search interface for FalkorDB.

    Overrides Graphiti's default search methods with more efficient queries
    that avoid cartesian products when looking up edges by UUID.
    """

    @staticmethod
    def _fallback_driver(driver: Any) -> Any:
        """Return a driver clone without the custom search interface.

        Graphiti's fallback helpers expect the default search interface.
        Copying the driver avoids mutating the shared singleton driver across awaits.
        """
        fallback_driver = copy(driver)
        fallback_driver.search_interface = None
        return fallback_driver

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
            rel.episodes AS episodes,
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

            # Handle episodes field - can be None, List, or comma-separated String
            raw_episodes = record.get("episodes")
            if raw_episodes is None:
                episodes = []
            elif isinstance(raw_episodes, list):
                episodes = raw_episodes
            elif isinstance(raw_episodes, str):
                episodes = raw_episodes.split(",") if raw_episodes else []
            else:
                episodes = []

            edge = EntityEdge(
                uuid=record["uuid"],
                source_node_uuid=record["source_node_uuid"],
                target_node_uuid=record["target_node_uuid"],
                fact=record["fact"],
                fact_embedding=record.get("fact_embedding"),
                name=record["name"],
                group_id=record["group_id"],
                episodes=episodes,
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

        return await search_utils.edge_similarity_search(
            self._fallback_driver(driver),
            search_vector,
            source_node_uuid,
            target_node_uuid,
            search_filter,
            group_ids,
            limit,
            min_score,
        )

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

        return await search_utils.node_fulltext_search(
            self._fallback_driver(driver), query, search_filter, group_ids, limit
        )

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

        return await search_utils.node_similarity_search(
            self._fallback_driver(driver),
            search_vector,
            search_filter,
            group_ids,
            limit,
            min_score,
        )

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

        return await search_utils.episode_fulltext_search(
            self._fallback_driver(driver), query, search_filter, group_ids, limit
        )

    def build_node_search_filters(self, search_filters: Any) -> Any:
        """Not used - Graphiti handles filter building internally."""
        return search_filters

    def build_edge_search_filters(self, search_filters: Any) -> Any:
        """Not used - Graphiti handles filter building internally."""
        return search_filters
