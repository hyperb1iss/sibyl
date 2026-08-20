"""Relationship reads and writes for the native graph."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

from surrealdb import RecordID

from sibyl_core.backends.surreal.connection import _is_transient_connection_error
from sibyl_core.embeddings.providers import EmbeddingProvider
from sibyl_core.memory_pipeline.quality import expand_memory_quality_storage_metadata
from sibyl_core.models.entities import Entity, Relationship, RelationshipType
from sibyl_core.services.graph_client import (
    SurrealGraphClient,
    mark_graph_schema_dirty,
    prepare_graph_schema,
)
from sibyl_core.services.graph_common import (
    SurrealRecord,
)
from sibyl_core.services.graph_common import (
    execute_graph_transaction as _execute_graph_transaction,
)
from sibyl_core.services.graph_common import (
    normalize_graph_records as normalize_records,
)
from sibyl_core.services.graph_common import (
    select_one as _select_one,
)
from sibyl_core.services.graph_embeddings import (
    _relationship_with_native_embedding,
    _relationships_with_native_embeddings,
)
from sibyl_core.services.graph_records import (
    _metadata_datetime,
    _metadata_float_list,
    _metadata_str,
    _metadata_str_list,
    _related_entity_from_row,
    _related_entity_projection,
    _relationship_fact,
    _relationship_from_row,
)

_RELATIONSHIP_BULK_UPSERT_QUERY = """
BEGIN TRANSACTION;
-- The planner never serves `uuid IN $list` from idx_relates_uuid (TableScan
-- for every statement type, seconds per capture batch once the table is
-- large), so the endpoint-move cleanup iterates the batch and addresses each
-- row through the unique index instead. The endpoint guard stays on the
-- DELETE itself so it evaluates atomically on the addressed record.
FOR $edge IN $edges {
    DELETE (SELECT VALUE id FROM relates_to WHERE uuid = $edge.uuid LIMIT 1)
    WHERE in != $edge.src OR out != $edge.tgt;
};
INSERT RELATION INTO relates_to $rows ON DUPLICATE KEY UPDATE
    name = $input.name,
    fact = $input.fact,
    fact_embedding = $input.fact_embedding,
    group_id = $input.group_id,
    source_id = $input.source_id,
    target_id = $input.target_id,
    episodes = $input.episodes ?? [],
    attributes = $input.attributes ?? {},
    created_at = $input.created_at,
    expired_at = $input.expired_at,
    valid_at = $input.valid_at,
    invalid_at = $input.invalid_at;
COMMIT TRANSACTION;
"""


class RelationshipManager:
    def __init__(
        self,
        client: SurrealGraphClient,
        *,
        group_id: str,
        embedding_provider: EmbeddingProvider | None = None,
    ) -> None:
        self._client = client
        self._group_id = group_id
        self._embedding_provider = embedding_provider

    async def create_bulk(self, relationships: Sequence[Relationship]) -> tuple[int, int]:
        prepared = list(relationships)
        if not prepared:
            return 0, 0
        try:
            created_ids = await self.create_direct_bulk(prepared, generate_embeddings=True)
        except Exception:
            return 0, len(prepared)
        created = len(created_ids)
        return created, len(prepared) - created

    async def create_direct_bulk(
        self,
        relationships: Sequence[Relationship],
        *,
        generate_embeddings: bool = False,
        embedding_batch_size: int = 64,
        write_batch_size: int = 128,
    ) -> list[str]:
        prepared = list(relationships)
        if not prepared:
            return []
        if generate_embeddings:
            prepared = await _relationships_with_native_embeddings(
                prepared,
                self._embedding_provider,
                batch_size=embedding_batch_size,
            )

        created_ids: list[str] = []
        batch_size = max(int(write_batch_size), 1)
        for index in range(0, len(prepared), batch_size):
            batch = prepared[index : index + batch_size]
            written = await _replace_relationships_bulk(
                self._client, batch, group_id=self._group_id
            )
            created_ids.extend(written)
        return created_ids

    async def create(self, relationship: Relationship) -> str:
        relationship = await _relationship_with_native_embedding(
            relationship,
            self._embedding_provider,
        )
        await _replace_relationship(self._client, relationship, group_id=self._group_id)
        return relationship.id

    async def delete(self, relationship_id: str) -> bool:
        rows = await _execute_graph_transaction(
            self._client,
            """
            BEGIN TRANSACTION;
            DELETE FROM relates_to
            WHERE group_id = $group_id AND uuid = $uuid
            RETURN BEFORE;
            DELETE FROM mentions
            WHERE group_id = $group_id AND uuid = $uuid
            RETURN BEFORE;
            COMMIT TRANSACTION;
            """,
            group_id=self._group_id,
            uuid=relationship_id,
        )
        return any(row.get("uuid") == relationship_id for row in rows)

    async def delete_bulk(self, relationship_ids: Sequence[str]) -> int:
        unique_ids = list(
            dict.fromkeys(
                relationship_id for relationship_id in relationship_ids if relationship_id
            )
        )
        if not unique_ids:
            return 0
        rows = await _execute_graph_transaction(
            self._client,
            # `uuid IN $list` is never index-served, so the targets resolve
            # through per-uuid indexed lookups first and the DELETE addresses
            # records directly; RETURN BEFORE still yields the deleted rows.
            # The closures reference nothing but their own argument (any other
            # binding silently evaluates to nothing on at least one engine),
            # so the group guard lives on the DELETE, where it still evaluates
            # per addressed record.
            """
            BEGIN TRANSACTION;
            LET $edge_targets = $uuids.map(|$u|
                (SELECT VALUE id FROM relates_to WHERE uuid = $u LIMIT 1)[0]);
            DELETE array::filter($edge_targets, |$t| $t != NONE)
                WHERE group_id = $group_id RETURN BEFORE;
            LET $mention_targets = $uuids.map(|$u|
                (SELECT VALUE id FROM mentions WHERE uuid = $u LIMIT 1)[0]);
            DELETE array::filter($mention_targets, |$t| $t != NONE)
                WHERE group_id = $group_id RETURN BEFORE;
            COMMIT TRANSACTION;
            """,
            group_id=self._group_id,
            uuids=unique_ids,
        )
        deleted = {str(row.get("uuid")) for row in rows if row.get("uuid") is not None}
        return len(deleted & set(unique_ids))

    async def get(self, relationship_id: str) -> Relationship:
        row = await _select_one(
            self._client,
            """
            SELECT id AS record_id,
                   uuid,
                   name,
                   fact,
                   group_id,
                   episodes,
                   attributes,
                   created_at,
                   expired_at,
                   valid_at,
                   invalid_at,
                   source_id AS source_uuid,
                   target_id AS target_uuid
            FROM relates_to
            WHERE group_id = $group_id AND uuid = $uuid
            LIMIT 1;
            """,
            group_id=self._group_id,
            uuid=relationship_id,
        )
        if row is None:
            raise KeyError(relationship_id)
        return _relationship_from_row(row)

    async def get_for_entity(
        self,
        entity_id: str,
        relationship_types: Sequence[RelationshipType] | None = None,
        direction: str = "both",
    ) -> list[Relationship]:
        type_values = [rel_type.value for rel_type in relationship_types or ()]
        type_clause = " AND name IN $relationship_types" if type_values else ""
        if direction == "outgoing":
            direction_clause = " AND source_id = $entity_id"
        elif direction == "incoming":
            direction_clause = " AND target_id = $entity_id"
        else:
            direction_clause = """
                AND (
                    source_id = $entity_id
                    OR target_id = $entity_id
                )
            """

        rows = normalize_records(
            await self._client.execute_query(
                """
                SELECT id AS record_id,
                       uuid,
                       name,
                       fact,
                       group_id,
                       episodes,
                       attributes,
                       created_at,
                       expired_at,
                       valid_at,
                       invalid_at,
                       source_id AS source_uuid,
                       target_id AS target_uuid
                FROM relates_to
                WHERE group_id = $group_id
                """
                + direction_clause
                + type_clause
                + """
                ORDER BY created_at DESC, uuid DESC;
                """,
                group_id=self._group_id,
                entity_id=entity_id,
                relationship_types=type_values,
            )
        )
        for row in rows:
            if row.get("source_uuid") == entity_id:
                row["direction"] = "outgoing"
            elif row.get("target_uuid") == entity_id:
                row["direction"] = "incoming"
        return [_relationship_from_row(row) for row in rows]

    async def get_related_entities(
        self,
        entity_id: str,
        relationship_types: Sequence[RelationshipType] | None = None,
        max_depth: int = 1,
        limit: int = 50,
    ) -> list[tuple[Entity, Relationship]]:
        del max_depth
        related_by_seed = await self.get_related_entities_batch(
            [entity_id],
            relationship_types=relationship_types,
            limit_per_entity=limit,
        )
        return related_by_seed.get(entity_id, [])

    async def get_related_entities_batch(
        self,
        entity_ids: Sequence[str],
        relationship_types: Sequence[RelationshipType] | None = None,
        limit_per_entity: int = 50,
    ) -> dict[str, list[tuple[Entity, Relationship]]]:
        seed_ids = list(dict.fromkeys(str(entity_id) for entity_id in entity_ids if entity_id))
        if not seed_ids:
            return {}

        type_values = [rel_type.value for rel_type in relationship_types or ()]
        type_clause = "AND name IN $relationship_types" if type_values else ""
        per_seed_limit = max(int(limit_per_entity), 1)
        related_rows = await self._get_native_related_entity_rows(
            seed_ids,
            type_clause=type_clause,
            type_values=type_values,
            limit=per_seed_limit,
        )

        results: dict[str, list[tuple[Entity, Relationship]]] = {
            seed_id: [] for seed_id in seed_ids
        }
        seen_by_seed: dict[str, set[tuple[str, object]]] = {seed_id: set() for seed_id in seed_ids}
        for row in related_rows:
            seed_id = row.get("seed_uuid")
            if not isinstance(seed_id, str) or seed_id not in results:
                continue
            seed_results = results[seed_id]
            if len(seed_results) >= per_seed_limit:
                continue
            entity = _related_entity_from_row(row)
            if entity is None:
                continue
            relationship = _relationship_from_row(row)
            key = (relationship.id, row.get("direction"))
            if key in seen_by_seed[seed_id]:
                continue
            seen_by_seed[seed_id].add(key)
            seed_results.append((entity, relationship))
        return results

    async def _get_native_related_entity_rows(
        self,
        seed_ids: Sequence[str],
        *,
        type_clause: str,
        type_values: Sequence[str],
        limit: int,
    ) -> list[SurrealRecord]:
        outgoing_rows, incoming_rows = await asyncio.gather(
            self._get_native_related_entity_direction_rows(
                seed_ids,
                endpoint_field="source_id",
                endpoint_alias="source_uuid",
                related_side="out",
                direction="outgoing",
                type_clause=type_clause,
                type_values=type_values,
                limit=limit,
            ),
            self._get_native_related_entity_direction_rows(
                seed_ids,
                endpoint_field="target_id",
                endpoint_alias="target_uuid",
                related_side="in",
                direction="incoming",
                type_clause=type_clause,
                type_values=type_values,
                limit=limit,
            ),
        )

        rows: list[SurrealRecord] = []
        seen: set[str] = set()
        for row in [*outgoing_rows, *incoming_rows]:
            key = ":".join(
                str(value)
                for value in (
                    row.get("seed_uuid"),
                    row.get("direction"),
                    row.get("uuid") or row.get("record_id") or id(row),
                )
            )
            if key in seen:
                continue
            seen.add(key)
            rows.append(row)
        return rows

    async def _get_native_related_entity_direction_rows(
        self,
        seed_ids: Sequence[str],
        *,
        endpoint_field: str,
        endpoint_alias: str,
        related_side: str,
        direction: str,
        type_clause: str,
        type_values: Sequence[str],
        limit: int,
    ) -> list[SurrealRecord]:
        async def get_seed_rows(seed_id: str) -> list[SurrealRecord]:
            return normalize_records(
                await self._client.execute_query(
                    f"""
                SELECT id AS record_id,
                       uuid,
                       name,
                       fact,
                       group_id,
                       episodes,
                       attributes,
                       created_at,
                       expired_at,
                       valid_at,
                       invalid_at,
                       source_id AS source_uuid,
                       target_id AS target_uuid,
                       {endpoint_field} AS seed_uuid,
                       {_related_entity_projection(related_side)}
                FROM relates_to
                WHERE group_id = $group_id
                  AND {endpoint_field} = $entity_id
                  AND {related_side}.group_id = $group_id
                """
                    + type_clause
                    + """
                ORDER BY created_at DESC, uuid DESC
                LIMIT $limit;
                """,
                    group_id=self._group_id,
                    entity_id=seed_id,
                    relationship_types=type_values,
                    limit=limit,
                )
            )

        rows = [
            row
            for seed_rows in await asyncio.gather(*(get_seed_rows(seed_id) for seed_id in seed_ids))
            for row in seed_rows
        ]
        for row in rows:
            row["direction"] = direction
            row.setdefault("seed_uuid", row.get(endpoint_alias))
        return rows

    async def list_all(
        self,
        relationship_types: Sequence[RelationshipType] | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Relationship]:
        if limit <= 0:
            return []
        type_values = [rel_type.value for rel_type in relationship_types or ()]
        type_clause = "AND name IN $relationship_types" if type_values else ""
        rows = normalize_records(
            await self._client.execute_query(
                """
                SELECT id AS record_id,
                       uuid,
                       name,
                       fact,
                       group_id,
                       episodes,
                       attributes,
                       created_at,
                       expired_at,
                       valid_at,
                       invalid_at,
                       source_id AS source_uuid,
                       target_id AS target_uuid
                FROM relates_to
                WHERE group_id = $group_id
                """
                + type_clause
                + """
                ORDER BY created_at DESC, uuid DESC
                LIMIT $limit START $offset;
                """,
                group_id=self._group_id,
                relationship_types=type_values,
                limit=max(int(limit), 1),
                offset=max(int(offset), 0),
            )
        )
        return [_relationship_from_row(row) for row in rows]

    async def find_between(
        self,
        source_id: str,
        target_id: str,
        *,
        relationship_type: RelationshipType | None = None,
    ) -> list[Relationship]:
        type_clause = "AND name = $relationship_type" if relationship_type else ""
        rows = normalize_records(
            await self._client.execute_query(
                """
                SELECT id AS record_id,
                       uuid,
                       name,
                       fact,
                       group_id,
                       episodes,
                       attributes,
                       created_at,
                       expired_at,
                       valid_at,
                       invalid_at,
                       source_id AS source_uuid,
                       target_id AS target_uuid
                FROM relates_to
                WHERE group_id = $group_id
                  AND (
                    (source_id = $source_id AND target_id = $target_id)
                    OR (source_id = $target_id AND target_id = $source_id)
                  )
                """
                + type_clause
                + """
                ORDER BY created_at DESC, uuid DESC;
                """,
                group_id=self._group_id,
                source_id=source_id,
                target_id=target_id,
                relationship_type=relationship_type.value if relationship_type else None,
            )
        )
        return [_relationship_from_row(row) for row in rows]

    async def delete_between(
        self,
        source_id: str,
        target_id: str,
        relationship_type: RelationshipType,
    ) -> int:
        rows = normalize_records(
            await self._client.execute_query(
                """
                DELETE FROM relates_to
                WHERE group_id = $group_id
                  AND (
                    (source_id = $source_id AND target_id = $target_id)
                  )
                  AND name = $relationship_type
                RETURN BEFORE;
                """,
                group_id=self._group_id,
                source_id=source_id,
                target_id=target_id,
                relationship_type=relationship_type.value,
            )
        )
        return len(rows)


async def _replace_relationship(
    client: SurrealGraphClient,
    relationship: Relationship,
    *,
    group_id: str,
) -> None:
    src = await _record_id(client, relationship.source_id)
    tgt = await _record_id(client, relationship.target_id)
    if src is None or tgt is None:
        raise ValueError(
            "relates_to endpoint not found: "
            f"{relationship.source_id!r} -> {relationship.target_id!r}"
        )
    payload = _relationship_record(relationship, group_id=group_id)
    await client.execute_query(
        """
        DELETE FROM relates_to WHERE uuid = $uuid AND (in != $src OR out != $tgt);
        LET $updated = (UPDATE relates_to SET
            in = $src,
            out = $tgt,
            uuid = $uuid,
            name = $name,
            fact = $fact,
            fact_embedding = $fact_embedding,
            group_id = $group_id,
            source_id = $source_id,
            target_id = $target_id,
            episodes = $episodes,
            attributes = $attributes,
            created_at = $created_at,
            expired_at = $expired_at,
            valid_at = $valid_at,
            invalid_at = $invalid_at
        WHERE uuid = $uuid RETURN id);
        IF array::len($updated) = 0 THEN
            RELATE $src->$rel->$tgt SET
                uuid = $uuid,
                name = $name,
                fact = $fact,
                fact_embedding = $fact_embedding,
                group_id = $group_id,
                source_id = $source_id,
                target_id = $target_id,
                episodes = $episodes,
                attributes = $attributes,
                created_at = $created_at,
                expired_at = $expired_at,
                valid_at = $valid_at,
                invalid_at = $invalid_at;
        END;
        """,
        src=src,
        tgt=tgt,
        rel=RecordID("relates_to", relationship.id),
        **payload,
    )


async def _record_id(client: SurrealGraphClient, uuid: str) -> object | None:
    row = await _select_one(
        client,
        "SELECT id AS record_id FROM entity WHERE uuid = $uuid LIMIT 1;",
        uuid=uuid,
    )
    return row.get("record_id") if row else None


async def _record_ids(
    client: SurrealGraphClient,
    uuids: Sequence[str],
    *,
    group_id: str,
) -> dict[str, object]:
    unique = list(dict.fromkeys(uuid for uuid in uuids if uuid))
    if not unique:
        return {}
    rows = normalize_records(
        await client.execute_query(
            # `uuid IN $list` is never index-served (a whole-table scan per
            # capture batch), so each uuid gets its own indexed lookup. The
            # closure body may reference nothing but its own argument: any
            # other binding silently evaluates to nothing on at least one
            # engine, so the group guard is applied to the returned rows
            # instead (uuid is unique table-wide). A missing uuid yields a
            # NONE entry, which normalize_records drops.
            """
            RETURN $uuids.map(|$u|
                (SELECT uuid, group_id, id AS record_id FROM entity
                 WHERE uuid = $u LIMIT 1)[0]
            );
            """,
            uuids=unique,
        )
    )
    resolved: dict[str, object] = {}
    for row in rows:
        uuid = row.get("uuid")
        record_id = row.get("record_id")
        if isinstance(uuid, str) and record_id is not None and row.get("group_id") == group_id:
            resolved[uuid] = record_id
    return resolved


async def _replace_relationships_bulk(
    client: SurrealGraphClient,
    relationships: Sequence[Relationship],
    *,
    group_id: str,
) -> list[str]:
    if not relationships:
        return []
    endpoint_uuids = [
        endpoint
        for relationship in relationships
        for endpoint in (relationship.source_id, relationship.target_id)
    ]
    record_ids = await _record_ids(client, endpoint_uuids, group_id=group_id)

    rows: list[SurrealRecord] = []
    written_ids: list[str] = []
    for relationship in relationships:
        src = record_ids.get(relationship.source_id)
        tgt = record_ids.get(relationship.target_id)
        if src is None or tgt is None:
            continue
        payload = _relationship_record(relationship, group_id=group_id)
        payload["in"] = src
        payload["out"] = tgt
        rows.append(payload)
        written_ids.append(relationship.id)

    if not rows:
        return []

    edges = [{"uuid": str(row["uuid"]), "src": row["in"], "tgt": row["out"]} for row in rows]
    try:
        await client.execute_query(
            _RELATIONSHIP_BULK_UPSERT_QUERY,
            rows=rows,
            edges=edges,
        )
    except Exception as exc:
        if not _is_transient_connection_error(exc):
            raise
        mark_graph_schema_dirty(client.group_id)
        await prepare_graph_schema(client)
        await client.execute_query(
            _RELATIONSHIP_BULK_UPSERT_QUERY,
            rows=rows,
            edges=edges,
        )
    return written_ids


def _relationship_record(relationship: Relationship, *, group_id: str) -> SurrealRecord:
    metadata = expand_memory_quality_storage_metadata(relationship.metadata or {})
    fact = _metadata_str(metadata, "fact") or _relationship_fact(relationship)
    fact_embedding = _metadata_float_list(
        metadata.get("fact_embedding") or metadata.get("embedding")
    )
    attributes = {
        key: value for key, value in metadata.items() if key not in {"fact_embedding", "embedding"}
    }
    return {
        "uuid": relationship.id,
        "name": relationship.relationship_type.value,
        "fact": fact,
        "fact_embedding": fact_embedding,
        "group_id": group_id,
        "source_id": relationship.source_id,
        "target_id": relationship.target_id,
        "episodes": _metadata_str_list(metadata.get("episodes")),
        "attributes": attributes,
        "created_at": relationship.created_at,
        "expired_at": _metadata_datetime(metadata.get("expired_at")),
        "valid_at": _metadata_datetime(metadata.get("valid_at") or metadata.get("valid_from")),
        "invalid_at": _metadata_datetime(metadata.get("invalid_at") or metadata.get("valid_to")),
    }


__all__ = ["RelationshipManager"]
