"""HasEpisode edge operations for the SurrealDB driver.

Implements Graphiti's ``HasEpisodeEdgeOperations`` contract against SurrealDB's
``has_episode`` RELATION table (saga -> episode).
"""

from __future__ import annotations

import logging

from sibyl_core.graph.surreal.compat.models import EdgeNotFoundError, HasEpisodeEdge
from sibyl_core.graph.surreal.compat.ops._common import (
    QueryExecutor,
    SurrealRecord,
    Transaction,
    build_relation_save_query,
    normalize_records,
    parse_db_date,
    relation_record_id,
    resolve_record_id,
    run_query,
)

logger = logging.getLogger(__name__)

EDGE_TABLE = "has_episode"
SOURCE_TABLE = "saga"
TARGET_TABLE = "episode"
_EDGE_SAVE = build_relation_save_query(EDGE_TABLE, ("uuid", "group_id", "created_at"))


def _has_episode_edge_from_record(record: SurrealRecord) -> HasEpisodeEdge:
    return HasEpisodeEdge(
        uuid=str(record["uuid"]),
        group_id=str(record["group_id"]),
        source_node_uuid=str(record["source_node_uuid"]),
        target_node_uuid=str(record["target_node_uuid"]),
        created_at=parse_db_date(record["created_at"]),
    )


class SurrealHasEpisodeEdgeOperations:
    """SurrealDB implementation of Graphiti's HasEpisodeEdgeOperations."""

    async def save(
        self,
        executor: QueryExecutor,
        edge: HasEpisodeEdge,
        tx: Transaction | None = None,
    ) -> None:
        src_id = await resolve_record_id(executor, tx, SOURCE_TABLE, edge.source_node_uuid)
        tgt_id = await resolve_record_id(executor, tx, TARGET_TABLE, edge.target_node_uuid)
        if src_id is None or tgt_id is None:
            msg = (
                f"Cannot save {EDGE_TABLE} edge {edge.uuid!r}: "
                f"source saga {edge.source_node_uuid!r} or target episode "
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
        edges: list[HasEpisodeEdge],
        tx: Transaction | None = None,
        batch_size: int = 100,
    ) -> None:
        del batch_size
        for edge in edges:
            await self.save(executor, edge, tx=tx)

    async def delete(
        self,
        executor: QueryExecutor,
        edge: HasEpisodeEdge,
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
    ) -> HasEpisodeEdge:
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
        return _has_episode_edge_from_record(records[0])

    async def get_by_uuids(
        self,
        executor: QueryExecutor,
        uuids: list[str],
    ) -> list[HasEpisodeEdge]:
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
        return [_has_episode_edge_from_record(r) for r in records]

    async def get_by_group_ids(
        self,
        executor: QueryExecutor,
        group_ids: list[str],
        limit: int | None = None,
        uuid_cursor: str | None = None,
    ) -> list[HasEpisodeEdge]:
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
        return [_has_episode_edge_from_record(r) for r in records]


__all__ = ["SurrealHasEpisodeEdgeOperations"]
