"""Episodic edge operations for the SurrealDB driver.

Implements Graphiti's ``EpisodicEdgeOperations`` contract against SurrealDB's
``mentions`` RELATION table. The name mismatch is intentional: Graphiti calls
the edge class ``EpisodicEdge`` but the relation direction is episode -> entity,
so the SurrealDB table is named ``mentions`` to reflect the semantic role.
"""

from __future__ import annotations

import logging

from graphiti_core.edges import EpisodicEdge
from graphiti_core.errors import EdgeNotFoundError

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

EDGE_TABLE = "mentions"
SOURCE_TABLE = "episode"
TARGET_TABLE = "entity"
_EDGE_SAVE = build_relation_save_query(EDGE_TABLE, ("uuid", "group_id", "created_at"))


def _episodic_edge_from_record(record: SurrealRecord) -> EpisodicEdge:
    return EpisodicEdge(
        uuid=str(record["uuid"]),
        group_id=str(record["group_id"]),
        source_node_uuid=str(record["source_node_uuid"]),
        target_node_uuid=str(record["target_node_uuid"]),
        created_at=parse_db_date(record["created_at"]),
    )


class SurrealEpisodicEdgeOperations:
    """SurrealDB implementation of Graphiti's EpisodicEdgeOperations.

    Persists episode -> entity edges into the ``mentions`` RELATION table.
    """

    async def save(
        self,
        executor: QueryExecutor,
        edge: EpisodicEdge,
        tx: Transaction | None = None,
    ) -> None:
        src_id = await resolve_record_id(executor, tx, SOURCE_TABLE, edge.source_node_uuid)
        tgt_id = await resolve_record_id(executor, tx, TARGET_TABLE, edge.target_node_uuid)
        if src_id is None or tgt_id is None:
            msg = (
                f"Cannot save {EDGE_TABLE} edge {edge.uuid!r}: "
                f"source episode {edge.source_node_uuid!r} or target entity "
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
        edges: list[EpisodicEdge],
        tx: Transaction | None = None,
        batch_size: int = 100,
    ) -> None:
        del batch_size  # RELATE can't batch while resolving endpoints; loop is fine.
        for edge in edges:
            await self.save(executor, edge, tx=tx)

    async def delete(
        self,
        executor: QueryExecutor,
        edge: EpisodicEdge,
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
    ) -> EpisodicEdge:
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
        return _episodic_edge_from_record(records[0])

    async def get_by_uuids(
        self,
        executor: QueryExecutor,
        uuids: list[str],
    ) -> list[EpisodicEdge]:
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
        return [_episodic_edge_from_record(r) for r in records]

    async def get_between_nodes(
        self,
        executor: QueryExecutor,
        source_node_uuid: str,
        target_node_uuid: str,
        group_ids: list[str] | None = None,
        limit: int | None = None,
    ) -> list[EpisodicEdge]:
        group_clause = "AND group_id IN $group_ids" if group_ids else ""
        limit_clause = f"LIMIT {int(limit)}" if limit is not None else ""
        records = normalize_records(
            await executor.execute_query(
                f"SELECT uuid, group_id, created_at, "
                f"in.uuid AS source_node_uuid, out.uuid AS target_node_uuid "
                f"FROM {EDGE_TABLE} "
                "WHERE in.uuid = $src_uuid AND out.uuid = $tgt_uuid "
                f"{group_clause} "
                "ORDER BY uuid DESC "
                f"{limit_clause};",
                src_uuid=source_node_uuid,
                tgt_uuid=target_node_uuid,
                group_ids=group_ids,
            )
        )
        return [_episodic_edge_from_record(r) for r in records]

    async def get_by_node_uuid(
        self,
        executor: QueryExecutor,
        node_uuid: str,
        group_ids: list[str] | None = None,
        limit: int | None = None,
    ) -> list[EpisodicEdge]:
        group_clause = "AND group_id IN $group_ids" if group_ids else ""
        limit_clause = f"LIMIT {int(limit)}" if limit is not None else ""
        records = normalize_records(
            await executor.execute_query(
                f"SELECT uuid, group_id, created_at, "
                f"in.uuid AS source_node_uuid, out.uuid AS target_node_uuid "
                f"FROM {EDGE_TABLE} "
                "WHERE (in.uuid = $node_uuid OR out.uuid = $node_uuid) "
                f"{group_clause} "
                "ORDER BY uuid DESC "
                f"{limit_clause};",
                node_uuid=node_uuid,
                group_ids=group_ids,
            )
        )
        return [_episodic_edge_from_record(r) for r in records]

    async def get_by_group_ids(
        self,
        executor: QueryExecutor,
        group_ids: list[str],
        limit: int | None = None,
        uuid_cursor: str | None = None,
    ) -> list[EpisodicEdge]:
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
        return [_episodic_edge_from_record(r) for r in records]


__all__ = ["SurrealEpisodicEdgeOperations"]
