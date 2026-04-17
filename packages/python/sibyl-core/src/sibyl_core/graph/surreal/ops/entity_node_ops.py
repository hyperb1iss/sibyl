"""Entity node operations for the SurrealDB driver.

Implements Graphiti's ``EntityNodeOperations`` contract against SurrealDB's
``entity`` table. Dynamic node attributes merge into the FLEXIBLE
``attributes`` field rather than leaking into typed columns, preserving
Graphiti's open-world property model.
"""

from __future__ import annotations

import logging
from typing import Any

from graphiti_core.driver.operations.entity_node_ops import EntityNodeOperations
from graphiti_core.driver.query_executor import QueryExecutor, Transaction
from graphiti_core.driver.record_parsers import entity_node_from_record
from graphiti_core.errors import NodeNotFoundError
from graphiti_core.nodes import EntityNode

from sibyl_core.graph.surreal.ops._common import normalize_records

logger = logging.getLogger(__name__)


def _entity_save_payload(node: EntityNode) -> dict[str, Any]:
    return {
        "uuid": node.uuid,
        "name": node.name,
        "entity_type": (node.labels[0] if node.labels else "Entity"),
        "summary": node.summary,
        "labels": list(set([*node.labels, "Entity"])),
        "attributes": dict(node.attributes or {}),
        "group_id": node.group_id,
        "created_at": node.created_at,
        "name_embedding": node.name_embedding,
    }


async def _run(
    executor: QueryExecutor,
    tx: Transaction | None,
    query: str,
    **params: Any,
) -> Any:
    """Execute via transaction when supplied, else the executor."""
    if tx is not None:
        return await tx.run(query, **params)
    return await executor.execute_query(query, **params)


class SurrealEntityNodeOperations(EntityNodeOperations):
    """SurrealDB implementation of Graphiti's EntityNodeOperations."""

    async def save(
        self,
        executor: QueryExecutor,
        node: EntityNode,
        tx: Transaction | None = None,
    ) -> None:
        payload = _entity_save_payload(node)
        # UPSERT semantics: delete-by-uuid + CREATE is simplest and keeps
        # the DEFAULT time::now() behavior intact for brand-new inserts.
        # SurrealDB's UPSERT statement would preserve created_at but is
        # noisier to express here; revisit if save() becomes hot-path.
        await _run(
            executor,
            tx,
            "DELETE FROM entity WHERE uuid = $uuid;",
            uuid=payload["uuid"],
        )
        await _run(
            executor,
            tx,
            """
            CREATE entity SET
                uuid = $uuid,
                name = $name,
                entity_type = $entity_type,
                summary = $summary,
                labels = $labels,
                attributes = $attributes,
                group_id = $group_id,
                created_at = $created_at,
                name_embedding = $name_embedding;
            """,
            **payload,
        )
        logger.debug("Saved entity to SurrealDB: %s", node.uuid)

    async def save_bulk(
        self,
        executor: QueryExecutor,
        nodes: list[EntityNode],
        tx: Transaction | None = None,
        batch_size: int = 100,
    ) -> None:
        if not nodes:
            return
        for start in range(0, len(nodes), batch_size):
            batch = nodes[start : start + batch_size]
            # One payload per row; SurrealDB INSERT accepts a list of objects.
            rows = [_entity_save_payload(n) for n in batch]
            uuids = [r["uuid"] for r in rows]
            await _run(
                executor,
                tx,
                "DELETE FROM entity WHERE uuid IN $uuids;",
                uuids=uuids,
            )
            await _run(
                executor,
                tx,
                "INSERT INTO entity $rows;",
                rows=rows,
            )

    async def delete(
        self,
        executor: QueryExecutor,
        node: EntityNode,
        tx: Transaction | None = None,
    ) -> None:
        """Delete an entity and cascade any connected relation rows.

        SurrealDB RELATION tables cascade when their endpoints are deleted,
        so no explicit edge cleanup is required here.
        """
        await _run(
            executor,
            tx,
            "DELETE FROM entity WHERE uuid = $uuid;",
            uuid=node.uuid,
        )
        logger.debug("Deleted entity from SurrealDB: %s", node.uuid)

    async def delete_by_group_id(
        self,
        executor: QueryExecutor,
        group_id: str,
        tx: Transaction | None = None,
        batch_size: int = 100,
    ) -> None:
        del batch_size  # SurrealDB deletes atomically; batch size is advisory
        await _run(
            executor,
            tx,
            "DELETE FROM entity WHERE group_id = $group_id;",
            group_id=group_id,
        )

    async def delete_by_uuids(
        self,
        executor: QueryExecutor,
        uuids: list[str],
        tx: Transaction | None = None,
        batch_size: int = 100,
    ) -> None:
        del batch_size
        if not uuids:
            return
        await _run(
            executor,
            tx,
            "DELETE FROM entity WHERE uuid IN $uuids;",
            uuids=uuids,
        )

    async def get_by_uuid(
        self,
        executor: QueryExecutor,
        uuid: str,
    ) -> EntityNode:
        records = normalize_records(
            await executor.execute_query(
                "SELECT * FROM entity WHERE uuid = $uuid LIMIT 1;",
                uuid=uuid,
            )
        )
        if not records:
            raise NodeNotFoundError(uuid)
        return entity_node_from_record(records[0])

    async def get_by_uuids(
        self,
        executor: QueryExecutor,
        uuids: list[str],
    ) -> list[EntityNode]:
        if not uuids:
            return []
        records = normalize_records(
            await executor.execute_query(
                "SELECT * FROM entity WHERE uuid IN $uuids;",
                uuids=uuids,
            )
        )
        return [entity_node_from_record(r) for r in records]

    async def get_by_group_ids(
        self,
        executor: QueryExecutor,
        group_ids: list[str],
        limit: int | None = None,
        uuid_cursor: str | None = None,
    ) -> list[EntityNode]:
        cursor_clause = "AND uuid < $cursor" if uuid_cursor else ""
        limit_clause = f"LIMIT {int(limit)}" if limit is not None else ""
        query = (
            "SELECT * FROM entity "
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
        return [entity_node_from_record(r) for r in records]

    async def load_embeddings(
        self,
        executor: QueryExecutor,
        node: EntityNode,
    ) -> None:
        records = normalize_records(
            await executor.execute_query(
                "SELECT name_embedding FROM entity WHERE uuid = $uuid LIMIT 1;",
                uuid=node.uuid,
            )
        )
        if not records:
            raise NodeNotFoundError(node.uuid)
        node.name_embedding = records[0].get("name_embedding")

    async def load_embeddings_bulk(
        self,
        executor: QueryExecutor,
        nodes: list[EntityNode],
        batch_size: int = 100,
    ) -> None:
        del batch_size
        if not nodes:
            return
        uuids = [n.uuid for n in nodes]
        records = normalize_records(
            await executor.execute_query(
                "SELECT uuid, name_embedding FROM entity WHERE uuid IN $uuids;",
                uuids=uuids,
            )
        )
        embedding_map = {r["uuid"]: r.get("name_embedding") for r in records}
        for node in nodes:
            if node.uuid in embedding_map:
                node.name_embedding = embedding_map[node.uuid]


__all__ = ["SurrealEntityNodeOperations"]
