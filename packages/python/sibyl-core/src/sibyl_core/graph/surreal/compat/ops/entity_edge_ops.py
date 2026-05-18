"""Entity edge operations for the SurrealDB driver.

Implements Graphiti's ``EntityEdgeOperations`` contract against SurrealDB's
``relates_to`` RELATION table. Edges carry the full bi-temporal payload —
``fact``, ``fact_embedding``, ``episodes``, ``attributes`` (FLEXIBLE dict),
``created_at``, ``expired_at``, ``valid_at``, ``invalid_at`` — all of which
round-trip untouched so Graphiti's temporal reasoning stays intact.
"""

from __future__ import annotations

import logging

from graphiti_core.driver.record_parsers import entity_edge_from_record
from graphiti_core.edges import EntityEdge
from graphiti_core.errors import EdgeNotFoundError

from sibyl_core.graph.surreal.compat.ops._common import (
    QueryExecutor,
    SurrealRecord,
    Transaction,
    build_relation_save_query,
    normalize_embedding,
    normalize_records,
    relation_record_id,
    resolve_record_id,
    run_query,
)

logger = logging.getLogger(__name__)


# SELECT projection that yields the record shape Graphiti's parser expects:
# in/out record pointers are translated back to source_node_uuid / target_node_uuid,
# and all bi-temporal fields ride along.
_ENTITY_EDGE_SELECT = """
SELECT
    uuid, name, fact, fact_embedding, group_id,
    episodes, attributes,
    created_at, expired_at, valid_at, invalid_at,
    in.uuid AS source_node_uuid,
    out.uuid AS target_node_uuid
FROM relates_to
"""


_ENTITY_EDGE_SAVE = build_relation_save_query(
    "relates_to",
    (
        "uuid",
        "name",
        "fact",
        "fact_embedding",
        "group_id",
        "episodes",
        "attributes",
        "created_at",
        "expired_at",
        "valid_at",
        "invalid_at",
    ),
)


def _entity_edge_save_payload(edge: EntityEdge) -> SurrealRecord:
    return {
        "uuid": edge.uuid,
        "name": edge.name,
        "fact": edge.fact,
        "fact_embedding": edge.fact_embedding,
        "group_id": edge.group_id,
        "episodes": list(edge.episodes or []),
        "attributes": dict(edge.attributes or {}),
        "created_at": edge.created_at,
        "expired_at": edge.expired_at,
        "valid_at": edge.valid_at,
        "invalid_at": edge.invalid_at,
    }


class SurrealEntityEdgeOperations:
    """SurrealDB implementation of Graphiti's EntityEdgeOperations."""

    async def save(
        self,
        executor: QueryExecutor,
        edge: EntityEdge,
        tx: Transaction | None = None,
    ) -> None:
        src_id = await resolve_record_id(executor, tx, "entity", edge.source_node_uuid)
        tgt_id = await resolve_record_id(executor, tx, "entity", edge.target_node_uuid)
        if src_id is None or tgt_id is None:
            raise ValueError(
                "relates_to endpoint not found: "
                f"{edge.source_node_uuid!r} -> {edge.target_node_uuid!r}"
            )

        payload = _entity_edge_save_payload(edge)
        await run_query(
            executor,
            tx,
            _ENTITY_EDGE_SAVE,
            rel=relation_record_id("relates_to", edge.uuid),
            src=src_id,
            tgt=tgt_id,
            **payload,
        )
        logger.debug("Saved entity edge to SurrealDB: %s", edge.uuid)

    async def save_bulk(
        self,
        executor: QueryExecutor,
        edges: list[EntityEdge],
        tx: Transaction | None = None,
        batch_size: int = 100,
    ) -> None:
        if not edges:
            return
        # RELATE cannot be batched with INSERT semantics — the source/target
        # record pointers must be resolved per-edge. Iterate with the
        # supplied batch_size chunking for potential future batching hooks.
        for start in range(0, len(edges), batch_size):
            batch = edges[start : start + batch_size]
            for edge in batch:
                await self.save(executor, edge, tx=tx)

    async def delete(
        self,
        executor: QueryExecutor,
        edge: EntityEdge,
        tx: Transaction | None = None,
    ) -> None:
        await run_query(
            executor,
            tx,
            "DELETE FROM relates_to WHERE uuid = $uuid;",
            uuid=edge.uuid,
        )
        logger.debug("Deleted entity edge: %s", edge.uuid)

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
            "DELETE FROM relates_to WHERE uuid IN $uuids;",
            uuids=uuids,
        )

    async def get_by_uuid(
        self,
        executor: QueryExecutor,
        uuid: str,
    ) -> EntityEdge:
        records = normalize_records(
            await executor.execute_query(
                _ENTITY_EDGE_SELECT + " WHERE uuid = $uuid LIMIT 1;",
                uuid=uuid,
            )
        )
        if not records:
            raise EdgeNotFoundError(uuid)
        return entity_edge_from_record(records[0])

    async def get_by_uuids(
        self,
        executor: QueryExecutor,
        uuids: list[str],
    ) -> list[EntityEdge]:
        if not uuids:
            return []
        records = normalize_records(
            await executor.execute_query(
                _ENTITY_EDGE_SELECT + " WHERE uuid IN $uuids;",
                uuids=uuids,
            )
        )
        return [entity_edge_from_record(r) for r in records]

    async def get_by_group_ids(
        self,
        executor: QueryExecutor,
        group_ids: list[str],
        limit: int | None = None,
        uuid_cursor: str | None = None,
        offset: int | None = None,
    ) -> list[EntityEdge]:
        cursor_clause = "AND uuid < $cursor" if uuid_cursor else ""
        limit_clause = f"LIMIT {int(limit)}" if limit is not None else ""
        offset_clause = f"START {max(int(offset), 0)}" if offset else ""
        query = (
            _ENTITY_EDGE_SELECT
            + " WHERE group_id IN $group_ids "
            + cursor_clause
            + " ORDER BY uuid DESC "
            + limit_clause
            + " "
            + offset_clause
            + ";"
        )
        records = normalize_records(
            await executor.execute_query(
                query,
                group_ids=group_ids,
                cursor=uuid_cursor,
            )
        )
        return [entity_edge_from_record(r) for r in records]

    async def get_between_nodes(
        self,
        executor: QueryExecutor,
        source_node_uuid: str,
        target_node_uuid: str,
        group_ids: list[str] | None = None,
        limit: int | None = None,
    ) -> list[EntityEdge]:
        group_clause = "AND group_id IN $group_ids" if group_ids else ""
        limit_clause = f"LIMIT {int(limit)}" if limit is not None else ""
        records = normalize_records(
            await executor.execute_query(
                _ENTITY_EDGE_SELECT
                + " WHERE in.uuid = $src_uuid AND out.uuid = $tgt_uuid "
                + group_clause
                + " ORDER BY uuid DESC "
                + limit_clause
                + ";",
                src_uuid=source_node_uuid,
                tgt_uuid=target_node_uuid,
                group_ids=group_ids,
            )
        )
        return [entity_edge_from_record(r) for r in records]

    async def get_by_node_uuid(
        self,
        executor: QueryExecutor,
        node_uuid: str,
        group_ids: list[str] | None = None,
        limit: int | None = None,
    ) -> list[EntityEdge]:
        group_clause = "AND group_id IN $group_ids" if group_ids else ""
        limit_clause = f"LIMIT {int(limit)}" if limit is not None else ""
        records = normalize_records(
            await executor.execute_query(
                _ENTITY_EDGE_SELECT
                + " WHERE (in.uuid = $node_uuid OR out.uuid = $node_uuid) "
                + group_clause
                + " ORDER BY uuid DESC "
                + limit_clause
                + ";",
                node_uuid=node_uuid,
                group_ids=group_ids,
            )
        )
        return [entity_edge_from_record(r) for r in records]

    async def get_by_node_uuids(
        self,
        executor: QueryExecutor,
        node_uuids: list[str],
        group_ids: list[str] | None = None,
        limit: int | None = None,
    ) -> list[EntityEdge]:
        if not node_uuids:
            return []
        group_clause = "AND group_id IN $group_ids" if group_ids else ""
        limit_clause = f"LIMIT {int(limit)}" if limit is not None else ""
        records = normalize_records(
            await executor.execute_query(
                _ENTITY_EDGE_SELECT
                + " WHERE (in.uuid IN $node_uuids OR out.uuid IN $node_uuids) "
                + group_clause
                + " ORDER BY uuid DESC "
                + limit_clause
                + ";",
                node_uuids=node_uuids,
                group_ids=group_ids,
            )
        )
        return [entity_edge_from_record(r) for r in records]

    async def load_embeddings(
        self,
        executor: QueryExecutor,
        edge: EntityEdge,
    ) -> None:
        records = normalize_records(
            await executor.execute_query(
                "SELECT fact_embedding FROM relates_to WHERE uuid = $uuid LIMIT 1;",
                uuid=edge.uuid,
            )
        )
        if not records:
            raise EdgeNotFoundError(edge.uuid)
        edge.fact_embedding = normalize_embedding(records[0].get("fact_embedding"))

    async def load_embeddings_bulk(
        self,
        executor: QueryExecutor,
        edges: list[EntityEdge],
        batch_size: int = 100,
    ) -> None:
        del batch_size  # SurrealDB returns all matches; batch is advisory
        if not edges:
            return
        uuids = [e.uuid for e in edges]
        records = normalize_records(
            await executor.execute_query(
                "SELECT uuid, fact_embedding FROM relates_to WHERE uuid IN $uuids;",
                uuids=uuids,
            )
        )
        embedding_map = {
            str(record["uuid"]): embedding
            for record in records
            if (embedding := normalize_embedding(record.get("fact_embedding"))) is not None
        }
        for edge in edges:
            if edge.uuid in embedding_map:
                edge.fact_embedding = embedding_map[edge.uuid]


__all__ = ["SurrealEntityEdgeOperations"]
