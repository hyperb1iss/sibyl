"""Community node operations for the SurrealDB driver.

Implements Graphiti's ``CommunityNodeOperations`` contract against
SurrealDB's ``community`` table. Communities carry a ``name_embedding``
(not ``summary_embedding``) matching Graphiti's upstream field name.
"""

from __future__ import annotations

import logging
from typing import Any

from graphiti_core.driver.operations.community_node_ops import CommunityNodeOperations
from graphiti_core.driver.query_executor import QueryExecutor, Transaction
from graphiti_core.driver.record_parsers import community_node_from_record
from graphiti_core.errors import NodeNotFoundError
from graphiti_core.nodes import CommunityNode

from sibyl_core.graph.surreal.ops._common import normalize_records

logger = logging.getLogger(__name__)


def _ensure_community_fields(record: dict[str, Any]) -> dict[str, Any]:
    """Backfill option<> fields SurrealDB omits when they are NONE.

    ``community_node_from_record`` uses strict ``record['name_embedding']``
    indexing, but SurrealDB drops unset option fields from SELECT output.
    """
    record.setdefault("name_embedding", None)
    record.setdefault("summary", "")
    return record


def _community_save_payload(node: CommunityNode) -> dict[str, Any]:
    return {
        "uuid": node.uuid,
        "name": node.name,
        "summary": node.summary,
        "labels": list(set([*node.labels, "Community"])),
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


class SurrealCommunityNodeOperations(CommunityNodeOperations):
    """SurrealDB implementation of Graphiti's CommunityNodeOperations."""

    async def save(
        self,
        executor: QueryExecutor,
        node: CommunityNode,
        tx: Transaction | None = None,
    ) -> None:
        payload = _community_save_payload(node)
        await _run(
            executor,
            tx,
            "DELETE FROM community WHERE uuid = $uuid;",
            uuid=payload["uuid"],
        )
        await _run(
            executor,
            tx,
            """
            CREATE community SET
                uuid = $uuid,
                name = $name,
                summary = $summary,
                labels = $labels,
                group_id = $group_id,
                created_at = $created_at,
                name_embedding = $name_embedding;
            """,
            **payload,
        )
        logger.debug("Saved community to SurrealDB: %s", node.uuid)

    async def save_bulk(
        self,
        executor: QueryExecutor,
        nodes: list[CommunityNode],
        tx: Transaction | None = None,
        batch_size: int = 100,
    ) -> None:
        if not nodes:
            return
        for start in range(0, len(nodes), batch_size):
            batch = nodes[start : start + batch_size]
            rows = [_community_save_payload(n) for n in batch]
            uuids = [r["uuid"] for r in rows]
            await _run(
                executor,
                tx,
                "DELETE FROM community WHERE uuid IN $uuids;",
                uuids=uuids,
            )
            await _run(
                executor,
                tx,
                "INSERT INTO community $rows;",
                rows=rows,
            )

    async def delete(
        self,
        executor: QueryExecutor,
        node: CommunityNode,
        tx: Transaction | None = None,
    ) -> None:
        """Delete a community and cascade any connected relation rows.

        Communities anchor ``has_member`` RELATION rows on both sides
        (community -> entity | community), and SurrealDB removes those
        edges automatically when the endpoint disappears.
        """
        await _run(
            executor,
            tx,
            "DELETE FROM community WHERE uuid = $uuid;",
            uuid=node.uuid,
        )
        logger.debug("Deleted community from SurrealDB: %s", node.uuid)

    async def delete_by_group_id(
        self,
        executor: QueryExecutor,
        group_id: str,
        tx: Transaction | None = None,
        batch_size: int = 100,
    ) -> None:
        del batch_size
        await _run(
            executor,
            tx,
            "DELETE FROM community WHERE group_id = $group_id;",
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
            "DELETE FROM community WHERE uuid IN $uuids;",
            uuids=uuids,
        )

    async def get_by_uuid(
        self,
        executor: QueryExecutor,
        uuid: str,
    ) -> CommunityNode:
        records = normalize_records(
            await executor.execute_query(
                "SELECT * FROM community WHERE uuid = $uuid LIMIT 1;",
                uuid=uuid,
            )
        )
        if not records:
            raise NodeNotFoundError(uuid)
        return community_node_from_record(_ensure_community_fields(records[0]))

    async def get_by_uuids(
        self,
        executor: QueryExecutor,
        uuids: list[str],
    ) -> list[CommunityNode]:
        if not uuids:
            return []
        records = normalize_records(
            await executor.execute_query(
                "SELECT * FROM community WHERE uuid IN $uuids;",
                uuids=uuids,
            )
        )
        return [community_node_from_record(_ensure_community_fields(r)) for r in records]

    async def get_by_group_ids(
        self,
        executor: QueryExecutor,
        group_ids: list[str],
        limit: int | None = None,
        uuid_cursor: str | None = None,
    ) -> list[CommunityNode]:
        cursor_clause = "AND uuid < $cursor" if uuid_cursor else ""
        limit_clause = f"LIMIT {int(limit)}" if limit is not None else ""
        query = (
            "SELECT * FROM community "
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
        return [community_node_from_record(_ensure_community_fields(r)) for r in records]

    async def load_name_embedding(
        self,
        executor: QueryExecutor,
        node: CommunityNode,
    ) -> None:
        records = normalize_records(
            await executor.execute_query(
                "SELECT name_embedding FROM community WHERE uuid = $uuid LIMIT 1;",
                uuid=node.uuid,
            )
        )
        if not records:
            raise NodeNotFoundError(node.uuid)
        node.name_embedding = records[0].get("name_embedding")


__all__ = ["SurrealCommunityNodeOperations"]
