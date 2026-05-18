"""Graph maintenance operations for the SurrealDB driver.

Implements Graphiti's ``GraphMaintenanceOperations`` contract:

* ``clear_data`` — drop rows per group_id or globally
* ``build_indices_and_constraints`` — delegate to schema bootstrap
* ``delete_all_indexes`` — walk every knowledge-graph table and drop
  every named index (analyzers survive)
* ``get_community_clusters`` — label-propagate over relates_to neighbors
  to form candidate entity clusters
* ``remove_communities`` — drop all community nodes
* ``determine_entity_community`` — no-op returning None, matching the
  FalkorDB semantics (the side effects are handled upstream after
  surrounding-entity inspection)
* ``get_mentioned_nodes`` — entities that appear in a batch of episodes
* ``get_communities_by_nodes`` — communities containing the given entities
"""

from __future__ import annotations

import logging

from graphiti_core.driver.operations.graph_ops import GraphMaintenanceOperations
from graphiti_core.driver.operations.graph_utils import Neighbor, label_propagation
from graphiti_core.driver.record_parsers import (
    community_node_from_record,
    entity_node_from_record,
)
from graphiti_core.nodes import CommunityNode, EntityNode, EpisodicNode

from sibyl_core.backends.surreal.driver import SurrealDriver
from sibyl_core.backends.surreal.schema import GRAPH_EDGES, GRAPH_TABLES
from sibyl_core.graph.surreal.compat.ops._common import QueryExecutor, normalize_records

logger = logging.getLogger(__name__)


def _count_value(value: object) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return int(value)
    return 0


class SurrealGraphMaintenanceOperations(GraphMaintenanceOperations):
    """SurrealDB implementation of GraphMaintenanceOperations."""

    async def clear_data(
        self,
        executor: QueryExecutor,
        group_ids: list[str] | None = None,
    ) -> None:
        if group_ids is None:
            for table in (*GRAPH_EDGES, *GRAPH_TABLES):
                await executor.execute_query(f"DELETE FROM {table};")
            return
        for table in (*GRAPH_EDGES, *GRAPH_TABLES):
            await executor.execute_query(
                f"DELETE FROM {table} WHERE group_id IN $group_ids;",
                group_ids=group_ids,
            )

    async def build_indices_and_constraints(
        self,
        executor: QueryExecutor,
        delete_existing: bool = False,
    ) -> None:
        # Schema-module bootstrap is the canonical path; delegate to the
        # driver so callers with QueryExecutor-only access still work.
        if not isinstance(executor, SurrealDriver):
            msg = (
                "SurrealGraphMaintenanceOperations.build_indices_and_constraints "
                "requires a SurrealDriver executor"
            )
            raise TypeError(msg)
        await executor.build_indices_and_constraints(delete_existing=delete_existing)

    async def delete_all_indexes(
        self,
        executor: QueryExecutor,
    ) -> None:
        for table in (*GRAPH_TABLES, *GRAPH_EDGES):
            info = await executor.execute_query(f"INFO FOR TABLE {table};")
            indexes: dict[str, object] = {}
            if isinstance(info, dict):
                indexes = info.get("indexes", {}) or {}
            elif isinstance(info, list) and info and isinstance(info[0], dict):
                indexes = info[0].get("indexes", {}) or {}
            for index_name in indexes:
                await executor.execute_query(
                    f"REMOVE INDEX IF EXISTS {index_name} ON TABLE {table};"
                )

    async def get_community_clusters(
        self,
        executor: QueryExecutor,
        group_ids: list[str] | None = None,
    ) -> list[list[EntityNode]]:
        community_clusters: list[list[EntityNode]] = []

        if group_ids is None:
            raw = await executor.execute_query(
                "SELECT array::distinct(array::flatten("
                "(SELECT group_id FROM entity WHERE group_id != NONE))) "
                "AS group_ids;",
            )
            rows = normalize_records(raw)
            if rows and isinstance(rows[0].get("group_ids"), list):
                group_ids = list(rows[0]["group_ids"])
            else:
                group_ids = []

        for group_id in group_ids or []:
            entity_rows = normalize_records(
                await executor.execute_query(
                    "SELECT * FROM entity WHERE group_id = $gid;",
                    gid=group_id,
                )
            )
            nodes = [entity_node_from_record(r) for r in entity_rows]

            projection: dict[str, list[Neighbor]] = {}
            for node in nodes:
                # Count relates_to neighbors in the same group_id. Walk both
                # directions because RELATES_TO in Graphiti is semantically
                # undirected for community formation.
                neighbor_rows = normalize_records(
                    await executor.execute_query(
                        """
                        SELECT out.uuid AS uuid, count() AS count
                        FROM relates_to
                        WHERE in.uuid = $uuid AND group_id = $gid
                            AND out.group_id = $gid
                        GROUP BY out.uuid;
                        """,
                        uuid=node.uuid,
                        gid=group_id,
                    )
                )
                reverse_rows = normalize_records(
                    await executor.execute_query(
                        """
                        SELECT in.uuid AS uuid, count() AS count
                        FROM relates_to
                        WHERE out.uuid = $uuid AND group_id = $gid
                            AND in.group_id = $gid
                        GROUP BY in.uuid;
                        """,
                        uuid=node.uuid,
                        gid=group_id,
                    )
                )
                combined: dict[str, int] = {}
                for row in (*neighbor_rows, *reverse_rows):
                    uuid = str(row.get("uuid") or "")
                    if not uuid or uuid == node.uuid:
                        continue
                    combined[uuid] = combined.get(uuid, 0) + _count_value(row.get("count"))
                projection[node.uuid] = [
                    Neighbor(node_uuid=uuid, edge_count=count) for uuid, count in combined.items()
                ]

            cluster_uuids = label_propagation(projection)
            for cluster in cluster_uuids:
                if not cluster:
                    continue
                cluster_rows = normalize_records(
                    await executor.execute_query(
                        "SELECT * FROM entity WHERE uuid IN $uuids;",
                        uuids=list(cluster),
                    )
                )
                community_clusters.append([entity_node_from_record(r) for r in cluster_rows])

        return community_clusters

    async def remove_communities(
        self,
        executor: QueryExecutor,
    ) -> None:
        # has_member RELATION edges cascade when the source community is
        # deleted because every edge row in SurrealDB carries a direct
        # reference to its in/out records.
        await executor.execute_query("DELETE FROM community;")

    async def determine_entity_community(
        self,
        executor: QueryExecutor,
        entity: EntityNode,
    ) -> None:
        # Mirrors FalkorDB semantics: probe existing membership + the
        # surrounding context but return None. Graphiti's caller handles
        # the subsequent write.
        await executor.execute_query(
            "SELECT id FROM has_member WHERE out IN (SELECT VALUE id FROM entity WHERE uuid = $uuid);",
            uuid=entity.uuid,
        )
        return None

    async def get_mentioned_nodes(
        self,
        executor: QueryExecutor,
        episodes: list[EpisodicNode],
    ) -> list[EntityNode]:
        if not episodes:
            return []
        episode_uuids = [ep.uuid for ep in episodes]

        # Two-step: collect entity record IDs via the mentions relation,
        # then hydrate. SurrealDB rejects `out.*` in SELECT lists because
        # ``out`` is a reserved context keyword there.
        edge_rows = await executor.execute_query(
            "SELECT out FROM mentions WHERE in IN (SELECT VALUE id FROM episode WHERE uuid IN $uuids);",
            uuids=episode_uuids,
        )
        ids = _collect_record_ids(edge_rows, field="out")
        if not ids:
            return []
        entity_rows = normalize_records(
            await executor.execute_query(
                "SELECT * FROM entity WHERE id IN $ids;",
                ids=ids,
            )
        )
        return [entity_node_from_record(r) for r in entity_rows]

    async def get_communities_by_nodes(
        self,
        executor: QueryExecutor,
        nodes: list[EntityNode],
    ) -> list[CommunityNode]:
        if not nodes:
            return []
        node_uuids = [n.uuid for n in nodes]
        edge_rows = await executor.execute_query(
            "SELECT in FROM has_member WHERE out IN (SELECT VALUE id FROM entity WHERE uuid IN $uuids);",
            uuids=node_uuids,
        )
        ids = _collect_record_ids(edge_rows, field="in")
        if not ids:
            return []
        community_rows = normalize_records(
            await executor.execute_query(
                "SELECT * FROM community WHERE id IN $ids;",
                ids=ids,
            )
        )
        for row in community_rows:
            # SurrealDB elides option<> fields when NONE; Graphiti's
            # community_node_from_record keys off record['name_embedding']
            # and record['summary'] directly, so backfill the misses.
            row.setdefault("name_embedding", None)
            row.setdefault("summary", None)
        return [community_node_from_record(r) for r in community_rows]


def _collect_record_ids(rows: object, *, field: str) -> list[object]:
    """Extract unique RecordID values from RELATION row projections.

    ``SELECT <field> FROM <relation>`` returns rows shaped like
    ``[{"<field>": RecordID(...)}, ...]``. This helper dedupes while
    preserving RecordID identity so downstream ``IN $ids`` lookups hit
    the target table's primary key directly.
    """
    if not isinstance(rows, list):
        return []
    seen: set[str] = set()
    out: list[object] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        record = {str(key): value for key, value in row.items()}
        record_id = record.get(field)
        if record_id is None:
            continue
        key = str(record_id)
        if key in seen:
            continue
        seen.add(key)
        out.append(record_id)
    return out


__all__ = ["SurrealGraphMaintenanceOperations"]
