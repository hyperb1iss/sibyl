"""Community edge operations for the SurrealDB driver.

Implements Graphiti's ``CommunityEdgeOperations`` contract against
SurrealDB's ``has_member`` RELATION table. Edge direction is always
community -> entity | community (a community either contains entities or
nests another community). No bi-temporal payload — community membership is
a lightweight structural link.

No shared record parser exists upstream for community edges; the inline
``_community_edge_from_record`` mirrors FalkorDB's helper.
"""

from __future__ import annotations

import logging

from graphiti_core.driver.operations.community_edge_ops import CommunityEdgeOperations
from graphiti_core.edges import CommunityEdge
from graphiti_core.errors import EdgeNotFoundError
from graphiti_core.helpers import parse_db_date

from sibyl_core.graph.surreal.compat.ops._common import (
    QueryExecutor,
    SurrealRecord,
    Transaction,
    build_relation_save_query,
    normalize_records,
    relation_record_id,
    run_query,
)

logger = logging.getLogger(__name__)


_COMMUNITY_EDGE_SELECT = """
SELECT
    uuid, group_id, created_at,
    in.uuid AS source_node_uuid,
    out.uuid AS target_node_uuid
FROM has_member
"""


_COMMUNITY_EDGE_SAVE = build_relation_save_query(
    "has_member",
    ("uuid", "group_id", "created_at"),
    source_binding="(SELECT VALUE id FROM community WHERE uuid = $src_uuid LIMIT 1)[0]",
    target_binding=(
        "array::concat("
        "(SELECT VALUE id FROM entity WHERE uuid = $tgt_uuid LIMIT 1), "
        "(SELECT VALUE id FROM community WHERE uuid = $tgt_uuid LIMIT 1)"
        ")[0]"
    ),
)


def _community_edge_from_record(record: SurrealRecord) -> CommunityEdge:
    return CommunityEdge(
        uuid=str(record["uuid"]),
        group_id=str(record["group_id"]),
        source_node_uuid=str(record["source_node_uuid"]),
        target_node_uuid=str(record["target_node_uuid"]),
        created_at=parse_db_date(record["created_at"]),  # type: ignore[arg-type]
    )


class SurrealCommunityEdgeOperations(CommunityEdgeOperations):
    """SurrealDB implementation of Graphiti's CommunityEdgeOperations."""

    async def save(
        self,
        executor: QueryExecutor,
        edge: CommunityEdge,
        tx: Transaction | None = None,
    ) -> None:
        await run_query(
            executor,
            tx,
            _COMMUNITY_EDGE_SAVE,
            rel=relation_record_id("has_member", edge.uuid),
            src_uuid=edge.source_node_uuid,
            tgt_uuid=edge.target_node_uuid,
            uuid=edge.uuid,
            group_id=edge.group_id,
            created_at=edge.created_at,
        )
        logger.debug("Saved community edge to SurrealDB: %s", edge.uuid)

    async def delete(
        self,
        executor: QueryExecutor,
        edge: CommunityEdge,
        tx: Transaction | None = None,
    ) -> None:
        await run_query(
            executor,
            tx,
            "DELETE FROM has_member WHERE uuid = $uuid;",
            uuid=edge.uuid,
        )
        logger.debug("Deleted community edge: %s", edge.uuid)

    async def delete_by_uuids(
        self,
        executor: QueryExecutor,
        uuids: list[str],
        tx: Transaction | None = None,
    ) -> None:
        if not uuids:
            return
        await run_query(
            executor,
            tx,
            "DELETE FROM has_member WHERE uuid IN $uuids;",
            uuids=uuids,
        )

    async def get_by_uuid(
        self,
        executor: QueryExecutor,
        uuid: str,
    ) -> CommunityEdge:
        records = normalize_records(
            await executor.execute_query(
                _COMMUNITY_EDGE_SELECT + " WHERE uuid = $uuid LIMIT 1;",
                uuid=uuid,
            )
        )
        if not records:
            raise EdgeNotFoundError(uuid)
        return _community_edge_from_record(records[0])

    async def get_by_uuids(
        self,
        executor: QueryExecutor,
        uuids: list[str],
    ) -> list[CommunityEdge]:
        if not uuids:
            return []
        records = normalize_records(
            await executor.execute_query(
                _COMMUNITY_EDGE_SELECT + " WHERE uuid IN $uuids;",
                uuids=uuids,
            )
        )
        return [_community_edge_from_record(r) for r in records]

    async def get_by_group_ids(
        self,
        executor: QueryExecutor,
        group_ids: list[str],
        limit: int | None = None,
        uuid_cursor: str | None = None,
    ) -> list[CommunityEdge]:
        cursor_clause = "AND uuid < $cursor" if uuid_cursor else ""
        limit_clause = f"LIMIT {int(limit)}" if limit is not None else ""
        query = (
            _COMMUNITY_EDGE_SELECT
            + " WHERE group_id IN $group_ids "
            + cursor_clause
            + " ORDER BY uuid DESC "
            + limit_clause
            + ";"
        )
        records = normalize_records(
            await executor.execute_query(
                query,
                group_ids=group_ids,
                cursor=uuid_cursor,
            )
        )
        return [_community_edge_from_record(r) for r in records]


__all__ = ["SurrealCommunityEdgeOperations"]
