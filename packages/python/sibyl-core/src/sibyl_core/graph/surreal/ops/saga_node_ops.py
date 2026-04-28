"""Saga node operations for the SurrealDB driver.

Implements Graphiti's ``SagaNodeOperations`` contract against SurrealDB's
``saga`` table. Sagas are the thinnest node type in the graph: just uuid,
name, group_id, and a created_at timestamp—no embeddings, no summary, no
dynamic attributes. Graphiti ships no shared record parser for sagas, so
we reconstruct the ``SagaNode`` inline from a normalized record dict.
"""

from __future__ import annotations

import logging
from typing import Any

from graphiti_core.driver.operations.saga_node_ops import SagaNodeOperations
from graphiti_core.driver.query_executor import QueryExecutor, Transaction
from graphiti_core.errors import NodeNotFoundError
from graphiti_core.helpers import parse_db_date
from graphiti_core.nodes import SagaNode

from sibyl_core.graph.surreal.ops._common import (
    build_node_bulk_upsert_query,
    build_node_upsert_query,
    normalize_records,
    run_query,
)

logger = logging.getLogger(__name__)

_SAGA_SAVE = build_node_upsert_query(
    "saga",
    ("uuid", "name", "labels", "group_id", "created_at"),
)
_SAGA_SAVE_BULK = build_node_bulk_upsert_query(
    "saga",
    ("uuid", "name", "labels", "group_id", "created_at"),
)


def _saga_save_payload(node: SagaNode) -> dict[str, Any]:
    return {
        "uuid": node.uuid,
        "name": node.name,
        "labels": list(set([*node.labels, "Saga"])),
        "group_id": node.group_id,
        "created_at": node.created_at,
    }


def _saga_from_record(record: dict[str, Any]) -> SagaNode:
    return SagaNode(
        uuid=record["uuid"],
        name=record["name"],
        group_id=record["group_id"],
        created_at=parse_db_date(record["created_at"]),  # type: ignore[arg-type]
    )


class SurrealSagaNodeOperations(SagaNodeOperations):
    """SurrealDB implementation of Graphiti's SagaNodeOperations."""

    async def save(
        self,
        executor: QueryExecutor,
        node: SagaNode,
        tx: Transaction | None = None,
    ) -> None:
        payload = _saga_save_payload(node)
        await run_query(
            executor,
            tx,
            _SAGA_SAVE,
            **payload,
        )
        logger.debug("Saved saga to SurrealDB: %s", node.uuid)

    async def save_bulk(
        self,
        executor: QueryExecutor,
        nodes: list[SagaNode],
        tx: Transaction | None = None,
        batch_size: int = 100,
    ) -> None:
        if not nodes:
            return
        for start in range(0, len(nodes), batch_size):
            batch = nodes[start : start + batch_size]
            rows = [_saga_save_payload(n) for n in batch]
            await run_query(
                executor,
                tx,
                _SAGA_SAVE_BULK,
                rows=rows,
            )

    async def delete(
        self,
        executor: QueryExecutor,
        node: SagaNode,
        tx: Transaction | None = None,
    ) -> None:
        """Delete a saga and cascade any connected relation rows.

        Sagas anchor ``has_episode`` RELATION rows, and SurrealDB removes
        those edges automatically when the saga endpoint is deleted.
        """
        await run_query(
            executor,
            tx,
            "DELETE FROM saga WHERE uuid = $uuid;",
            uuid=node.uuid,
        )
        logger.debug("Deleted saga from SurrealDB: %s", node.uuid)

    async def delete_by_group_id(
        self,
        executor: QueryExecutor,
        group_id: str,
        tx: Transaction | None = None,
        batch_size: int = 100,
    ) -> None:
        del batch_size
        await run_query(
            executor,
            tx,
            "DELETE FROM saga WHERE group_id = $group_id;",
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
        await run_query(
            executor,
            tx,
            "DELETE FROM saga WHERE uuid IN $uuids;",
            uuids=uuids,
        )

    async def get_by_uuid(
        self,
        executor: QueryExecutor,
        uuid: str,
    ) -> SagaNode:
        records = normalize_records(
            await executor.execute_query(
                "SELECT * FROM saga WHERE uuid = $uuid LIMIT 1;",
                uuid=uuid,
            )
        )
        if not records:
            raise NodeNotFoundError(uuid)
        return _saga_from_record(records[0])

    async def get_by_uuids(
        self,
        executor: QueryExecutor,
        uuids: list[str],
    ) -> list[SagaNode]:
        if not uuids:
            return []
        records = normalize_records(
            await executor.execute_query(
                "SELECT * FROM saga WHERE uuid IN $uuids;",
                uuids=uuids,
            )
        )
        return [_saga_from_record(r) for r in records]

    async def get_by_group_ids(
        self,
        executor: QueryExecutor,
        group_ids: list[str],
        limit: int | None = None,
        uuid_cursor: str | None = None,
    ) -> list[SagaNode]:
        cursor_clause = "AND uuid < $cursor" if uuid_cursor else ""
        limit_clause = f"LIMIT {int(limit)}" if limit is not None else ""
        query = (
            "SELECT * FROM saga "
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
        return [_saga_from_record(r) for r in records]


__all__ = ["SurrealSagaNodeOperations"]
