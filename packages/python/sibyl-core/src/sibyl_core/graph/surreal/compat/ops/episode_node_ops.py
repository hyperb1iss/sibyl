"""Episode node operations for the SurrealDB driver.

Implements Graphiti's ``EpisodeNodeOperations`` contract against SurrealDB's
``episode`` table. Episodes are immutable raw records—no dynamic attribute
bag like entities—so the save payload maps 1:1 onto the schema columns.
"""

from __future__ import annotations

import logging
from datetime import datetime

from sibyl_core.graph.surreal.compat.models import EpisodeType, EpisodicNode, NodeNotFoundError
from sibyl_core.graph.surreal.compat.ops._common import (
    QueryExecutor,
    SurrealRecord,
    Transaction,
    build_node_bulk_upsert_query,
    build_node_upsert_query,
    normalize_records,
    parse_db_date,
    run_query,
)

logger = logging.getLogger(__name__)

_EPISODE_SAVE = build_node_upsert_query(
    "episode",
    (
        "uuid",
        "name",
        "source",
        "source_description",
        "content",
        "labels",
        "group_id",
        "created_at",
        "valid_at",
        "entity_edges",
    ),
)
_EPISODE_SAVE_BULK = build_node_bulk_upsert_query(
    "episode",
    (
        "uuid",
        "name",
        "source",
        "source_description",
        "content",
        "labels",
        "group_id",
        "created_at",
        "valid_at",
        "entity_edges",
    ),
)


def _ensure_episode_fields(record: SurrealRecord) -> SurrealRecord:
    """Backfill option<> fields SurrealDB omits when they are NONE.

    The compat parser uses strict indexing; any missing option<> field would
    raise KeyError before hitting the None check.
    """
    record.setdefault("source_description", None)
    record.setdefault("valid_at", None)
    record.setdefault("entity_edges", [])
    return record


def episodic_node_from_record(record: SurrealRecord) -> EpisodicNode:
    created_at = parse_db_date(record["created_at"])
    valid_at = parse_db_date(record["valid_at"])

    if created_at is None:
        raise ValueError(f"created_at cannot be None for episode {record.get('uuid', 'unknown')}")
    if valid_at is None:
        raise ValueError(f"valid_at cannot be None for episode {record.get('uuid', 'unknown')}")

    return EpisodicNode.model_validate(
        {
            "content": record["content"],
            "created_at": created_at,
            "valid_at": valid_at,
            "uuid": record["uuid"],
            "group_id": record["group_id"],
            "source": EpisodeType.from_str(str(record["source"])),
            "name": record["name"],
            "source_description": record["source_description"],
            "entity_edges": record["entity_edges"],
        }
    )


def _episode_save_payload(node: EpisodicNode) -> SurrealRecord:
    return {
        "uuid": node.uuid,
        "name": node.name,
        "source": node.source.value,
        "source_description": node.source_description,
        "content": node.content,
        "labels": list(set([*node.labels, "Episodic"])),
        "group_id": node.group_id,
        "created_at": node.created_at,
        "valid_at": node.valid_at,
        "entity_edges": list(node.entity_edges or []),
    }


class SurrealEpisodeNodeOperations:
    """SurrealDB implementation of Graphiti's EpisodeNodeOperations."""

    async def save(
        self,
        executor: QueryExecutor,
        node: EpisodicNode,
        tx: Transaction | None = None,
    ) -> None:
        payload = _episode_save_payload(node)
        await run_query(
            executor,
            tx,
            _EPISODE_SAVE,
            **payload,
        )
        logger.debug("Saved episode to SurrealDB: %s", node.uuid)

    async def save_bulk(
        self,
        executor: QueryExecutor,
        nodes: list[EpisodicNode],
        tx: Transaction | None = None,
        batch_size: int = 100,
    ) -> None:
        if not nodes:
            return
        for start in range(0, len(nodes), batch_size):
            batch = nodes[start : start + batch_size]
            rows = [_episode_save_payload(n) for n in batch]
            await run_query(
                executor,
                tx,
                _EPISODE_SAVE_BULK,
                rows=rows,
            )

    async def delete(
        self,
        executor: QueryExecutor,
        node: EpisodicNode,
        tx: Transaction | None = None,
    ) -> None:
        """Delete an episode and cascade any connected relation rows.

        Episodes sit at the tail of ``mentions``/``has_episode``/
        ``next_episode`` RELATION tables, and SurrealDB removes those
        edges automatically when the endpoint is deleted.
        """
        await run_query(
            executor,
            tx,
            "DELETE FROM episode WHERE uuid = $uuid;",
            uuid=node.uuid,
        )
        logger.debug("Deleted episode from SurrealDB: %s", node.uuid)

    async def delete_by_group_id(
        self,
        executor: QueryExecutor,
        group_id: str,
        tx: Transaction | None = None,
        batch_size: int = 100,
    ) -> None:
        del batch_size  # SurrealDB deletes atomically; batch size is advisory
        await run_query(
            executor,
            tx,
            "DELETE FROM episode WHERE group_id = $group_id;",
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
            "DELETE FROM episode WHERE uuid IN $uuids;",
            uuids=uuids,
        )

    async def get_by_uuid(
        self,
        executor: QueryExecutor,
        uuid: str,
    ) -> EpisodicNode:
        records = normalize_records(
            await executor.execute_query(
                "SELECT * FROM episode WHERE uuid = $uuid LIMIT 1;",
                uuid=uuid,
            )
        )
        if not records:
            raise NodeNotFoundError(uuid)
        return episodic_node_from_record(_ensure_episode_fields(records[0]))

    async def get_by_uuids(
        self,
        executor: QueryExecutor,
        uuids: list[str],
    ) -> list[EpisodicNode]:
        if not uuids:
            return []
        records = normalize_records(
            await executor.execute_query(
                "SELECT * FROM episode WHERE uuid IN $uuids;",
                uuids=uuids,
            )
        )
        return [episodic_node_from_record(r) for r in records]

    async def get_by_group_ids(
        self,
        executor: QueryExecutor,
        group_ids: list[str],
        limit: int | None = None,
        uuid_cursor: str | None = None,
    ) -> list[EpisodicNode]:
        cursor_clause = "AND uuid < $cursor" if uuid_cursor else ""
        limit_clause = f"LIMIT {int(limit)}" if limit is not None else ""
        query = (
            "SELECT * FROM episode "
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
        return [episodic_node_from_record(r) for r in records]

    async def get_by_entity_node_uuid(
        self,
        executor: QueryExecutor,
        entity_node_uuid: str,
    ) -> list[EpisodicNode]:
        records = normalize_records(
            await executor.execute_query(
                """
                SELECT * FROM episode
                WHERE id IN (
                    SELECT VALUE in FROM mentions
                    WHERE out IN (SELECT id FROM entity WHERE uuid = $entity_node_uuid)
                );
                """,
                entity_node_uuid=entity_node_uuid,
            )
        )
        return [episodic_node_from_record(r) for r in records]

    async def retrieve_episodes(
        self,
        executor: QueryExecutor,
        reference_time: datetime,
        last_n: int = 3,
        group_ids: list[str] | None = None,
        source: str | None = None,
        saga: str | None = None,
    ) -> list[EpisodicNode]:
        if saga is not None and group_ids:
            # Saga-scoped: pull episodes attached to a named saga in the
            # first group_id (Graphiti's FalkorDB impl only uses [0]).
            source_clause = "AND source = $source" if source else ""
            query = (
                "SELECT * FROM episode "
                "WHERE id IN ("
                "    SELECT VALUE out FROM has_episode "
                "    WHERE in IN (SELECT id FROM saga "
                "                 WHERE name = $saga_name "
                "                 AND group_id = $group_id)"
                ") "
                "AND valid_at <= $reference_time "
                f"{source_clause} "
                "ORDER BY valid_at DESC "
                "LIMIT $num_episodes;"
            )
            records = normalize_records(
                await executor.execute_query(
                    query,
                    saga_name=saga,
                    group_id=group_ids[0],
                    reference_time=reference_time,
                    source=source,
                    num_episodes=last_n,
                )
            )
        else:
            source_clause = "AND source = $source" if source else ""
            group_clause = "AND group_id IN $group_ids" if group_ids else ""
            query = (
                "SELECT * FROM episode "
                "WHERE valid_at <= $reference_time "
                f"{group_clause} "
                f"{source_clause} "
                "ORDER BY valid_at DESC "
                "LIMIT $num_episodes;"
            )
            records = normalize_records(
                await executor.execute_query(
                    query,
                    reference_time=reference_time,
                    group_ids=group_ids,
                    source=source,
                    num_episodes=last_n,
                )
            )
        return [episodic_node_from_record(r) for r in records]


__all__ = ["SurrealEpisodeNodeOperations"]
