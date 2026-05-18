"""NextEpisode edge operations for the SurrealDB driver.

Implements Graphiti's ``NextEpisodeEdgeOperations`` contract against
SurrealDB's ``next_episode`` RELATION table (episode -> episode sequence link).
"""

from __future__ import annotations

import logging

from graphiti_core.edges import NextEpisodeEdge
from graphiti_core.errors import EdgeNotFoundError
from graphiti_core.helpers import parse_db_date

from sibyl_core.graph.surreal.compat.ops._common import (
    QueryExecutor,
    SurrealRecord,
    Transaction,
    build_relation_save_query,
    normalize_records,
    relation_record_id,
    resolve_record_id,
    run_query,
)

logger = logging.getLogger(__name__)

EDGE_TABLE = "next_episode"
SOURCE_TABLE = "episode"
TARGET_TABLE = "episode"
_EDGE_SAVE = build_relation_save_query(EDGE_TABLE, ("uuid", "group_id", "created_at"))


def _next_episode_edge_from_record(record: SurrealRecord) -> NextEpisodeEdge:
    return NextEpisodeEdge(
        uuid=str(record["uuid"]),
        group_id=str(record["group_id"]),
        source_node_uuid=str(record["source_node_uuid"]),
        target_node_uuid=str(record["target_node_uuid"]),
        created_at=parse_db_date(record["created_at"]),  # type: ignore[arg-type]
    )


class SurrealNextEpisodeEdgeOperations:
    """SurrealDB implementation of Graphiti's NextEpisodeEdgeOperations."""

    async def save(
        self,
        executor: QueryExecutor,
        edge: NextEpisodeEdge,
        tx: Transaction | None = None,
    ) -> None:
        src_id = await resolve_record_id(executor, tx, SOURCE_TABLE, edge.source_node_uuid)
        tgt_id = await resolve_record_id(executor, tx, TARGET_TABLE, edge.target_node_uuid)
        if src_id is None or tgt_id is None:
            msg = (
                f"Cannot save {EDGE_TABLE} edge {edge.uuid!r}: "
                f"source episode {edge.source_node_uuid!r} or target episode "
                f"{edge.target_node_uuid!r} not found"
            )
            raise ValueError(msg)

        await run_query(
            executor,
            tx,
            _EDGE_SAVE,
            rel=relation_record_id(EDGE_TABLE, edge.uuid),
            src=src_id,
            tgt=tgt_id,
            uuid=edge.uuid,
            group_id=edge.group_id,
            created_at=edge.created_at,
        )
        logger.debug("Saved %s edge: %s", EDGE_TABLE, edge.uuid)

    async def save_bulk(
        self,
        executor: QueryExecutor,
        edges: list[NextEpisodeEdge],
        tx: Transaction | None = None,
        batch_size: int = 100,
    ) -> None:
        del batch_size
        for edge in edges:
            await self.save(executor, edge, tx=tx)

    async def delete(
        self,
        executor: QueryExecutor,
        edge: NextEpisodeEdge,
        tx: Transaction | None = None,
    ) -> None:
        await run_query(
            executor,
            tx,
            f"DELETE FROM {EDGE_TABLE} WHERE uuid = $uuid;",
            uuid=edge.uuid,
        )
        logger.debug("Deleted %s edge: %s", EDGE_TABLE, edge.uuid)

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
            f"DELETE FROM {EDGE_TABLE} WHERE uuid IN $uuids;",
            uuids=uuids,
        )

    async def get_by_uuid(
        self,
        executor: QueryExecutor,
        uuid: str,
    ) -> NextEpisodeEdge:
        records = normalize_records(
            await executor.execute_query(
                f"SELECT uuid, group_id, created_at, "
                f"in.uuid AS source_node_uuid, out.uuid AS target_node_uuid "
                f"FROM {EDGE_TABLE} WHERE uuid = $uuid LIMIT 1;",
                uuid=uuid,
            )
        )
        if not records:
            raise EdgeNotFoundError(uuid)
        return _next_episode_edge_from_record(records[0])

    async def get_by_uuids(
        self,
        executor: QueryExecutor,
        uuids: list[str],
    ) -> list[NextEpisodeEdge]:
        if not uuids:
            return []
        records = normalize_records(
            await executor.execute_query(
                f"SELECT uuid, group_id, created_at, "
                f"in.uuid AS source_node_uuid, out.uuid AS target_node_uuid "
                f"FROM {EDGE_TABLE} WHERE uuid IN $uuids;",
                uuids=uuids,
            )
        )
        return [_next_episode_edge_from_record(r) for r in records]

    async def get_by_group_ids(
        self,
        executor: QueryExecutor,
        group_ids: list[str],
        limit: int | None = None,
        uuid_cursor: str | None = None,
    ) -> list[NextEpisodeEdge]:
        cursor_clause = "AND uuid < $cursor" if uuid_cursor else ""
        limit_clause = f"LIMIT {int(limit)}" if limit is not None else ""
        query = (
            f"SELECT uuid, group_id, created_at, "
            f"in.uuid AS source_node_uuid, out.uuid AS target_node_uuid "
            f"FROM {EDGE_TABLE} "
            "WHERE group_id IN $group_ids "
            f"{cursor_clause} "
            "ORDER BY uuid DESC "
            f"{limit_clause};"
        )
        records = normalize_records(
            await executor.execute_query(
                query,
                group_ids=group_ids,
                cursor=uuid_cursor,
            )
        )
        return [_next_episode_edge_from_record(r) for r in records]


__all__ = ["SurrealNextEpisodeEdgeOperations"]
