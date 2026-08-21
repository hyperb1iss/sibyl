"""Content lineage materialization, backfill, and history queries."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence

from sibyl_core.backends.surreal import SurrealContentClient
from sibyl_core.services import content_client
from sibyl_core.services import content_models as models
from sibyl_core.services.content_models import ContentLineageBackfillResult, SurrealRecord

_DERIVED_FROM_LINEAGE_CANDIDATE_QUERY = """
    SELECT id, uuid, organization_id, raw_memory_ids, created_at
    FROM source_imports
    WHERE organization_id = $organization_id
    ORDER BY created_at ASC, uuid ASC
    LIMIT $page_size START $offset;
"""

_DERIVED_FROM_LINEAGE_RELATE_QUERY = """
FOR $edge_record IN $edges {
    LET $raw = (
        SELECT id, source_id FROM raw_captures
        WHERE organization_id = $edge_record.organization_id
            AND uuid = $edge_record.raw_memory_id
        LIMIT 1
    )[0];
    LET $import_id = (
        SELECT VALUE id FROM source_imports
        WHERE organization_id = $edge_record.organization_id
            AND uuid = $edge_record.source_import_id
        LIMIT 1
    )[0];
    IF $raw != NONE AND $import_id != NONE {
        LET $raw_id = $raw.id;
        LET $edge = type::record($edge_record.edge_ref);
        LET $existing_edge = (SELECT VALUE id FROM derived_from WHERE id = $edge LIMIT 1)[0];
        IF $existing_edge = NONE {
            RELATE $raw_id->$edge->$import_id CONTENT {
                uuid: $edge_record.uuid,
                organization_id: $edge_record.organization_id,
                raw_memory_id: $edge_record.raw_memory_id,
                source_import_id: $edge_record.source_import_id,
                source_id: $raw.source_id,
                created_at: $edge_record.created_at
            };
        } ELSE {
            UPDATE $edge SET
                uuid = $edge_record.uuid,
                organization_id = $edge_record.organization_id,
                raw_memory_id = $edge_record.raw_memory_id,
                source_import_id = $edge_record.source_import_id,
                source_id = $raw.source_id;
        };
    };
};
"""

_CHUNK_OF_LINEAGE_CANDIDATE_QUERY = """
    SELECT id, uuid, organization_id, source_id, document_id, created_at
    FROM document_chunks
    WHERE organization_id = $organization_id
        AND document_id != NONE
        AND document_id != ''
    ORDER BY created_at ASC, uuid ASC
    LIMIT $page_size START $offset;
"""

_CHUNK_OF_LINEAGE_RELATE_QUERY = """
FOR $edge_record IN $edges {
    LET $chunk_id = (
        SELECT VALUE id FROM document_chunks
        WHERE organization_id = $edge_record.organization_id
            AND uuid = $edge_record.chunk_id
        LIMIT 1
    )[0];
    LET $document_id = (
        SELECT VALUE id FROM crawled_documents
        WHERE organization_id = $edge_record.organization_id
            AND uuid = $edge_record.document_id
        LIMIT 1
    )[0];
    IF $chunk_id != NONE AND $document_id != NONE {
        LET $edge = type::record($edge_record.edge_ref);
        LET $existing_edge = (SELECT VALUE id FROM chunk_of WHERE id = $edge LIMIT 1)[0];
        IF $existing_edge = NONE {
            RELATE $chunk_id->$edge->$document_id CONTENT {
                uuid: $edge_record.uuid,
                organization_id: $edge_record.organization_id,
                chunk_id: $edge_record.chunk_id,
                document_id: $edge_record.document_id,
                source_id: $edge_record.source_id,
                created_at: $edge_record.created_at
            };
        } ELSE {
            UPDATE $edge SET
                uuid = $edge_record.uuid,
                organization_id = $edge_record.organization_id,
                chunk_id = $edge_record.chunk_id,
                document_id = $edge_record.document_id,
                source_id = $edge_record.source_id;
        };
    };
};
"""

_SUPERSEDES_LINEAGE_CANDIDATE_QUERY = """
    SELECT id, uuid, organization_id, source_id, metadata, created_at
    FROM raw_captures
    WHERE organization_id = $organization_id
        AND (
            (metadata.supersedes_raw_memory_id != NONE
                AND metadata.supersedes_raw_memory_id != '')
            OR (metadata.superseded_by_source_id != NONE
                AND metadata.superseded_by_source_id != '')
        )
    ORDER BY created_at ASC, uuid ASC
    LIMIT $page_size START $offset;
"""

_SUPERSEDES_LINEAGE_RELATE_QUERY = """
FOR $edge_record IN $edges {
    LET $raw = (
        SELECT id, source_id FROM raw_captures
        WHERE organization_id = $edge_record.organization_id
            AND uuid = $edge_record.raw_memory_id
        LIMIT 1
    )[0];
    LET $superseded_id = (
        SELECT VALUE id FROM raw_captures
        WHERE organization_id = $edge_record.organization_id
            AND uuid = $edge_record.superseded_raw_memory_id
        LIMIT 1
    )[0];
    IF $raw != NONE AND $superseded_id != NONE {
        LET $raw_id = $raw.id;
        LET $edge = type::record($edge_record.edge_ref);
        LET $existing_edge = (SELECT VALUE id FROM supersedes WHERE id = $edge LIMIT 1)[0];
        IF $existing_edge = NONE {
            RELATE $raw_id->$edge->$superseded_id CONTENT {
                uuid: $edge_record.uuid,
                organization_id: $edge_record.organization_id,
                raw_memory_id: $edge_record.raw_memory_id,
                superseded_raw_memory_id: $edge_record.superseded_raw_memory_id,
                source_id: $raw.source_id,
                created_at: $edge_record.created_at
            };
        } ELSE {
            UPDATE $edge SET
                uuid = $edge_record.uuid,
                organization_id = $edge_record.organization_id,
                raw_memory_id = $edge_record.raw_memory_id,
                superseded_raw_memory_id = $edge_record.superseded_raw_memory_id,
                source_id = $raw.source_id;
        };
    };
};
"""

_EXTRACTED_INTO_LINEAGE_CANDIDATE_QUERY = """
    SELECT id, uuid, organization_id, source_id, document_id, entity_ids, created_at
    FROM document_chunks
    WHERE organization_id = $organization_id
        AND entity_ids != NONE
        AND array::len(entity_ids) > 0
    ORDER BY created_at ASC, uuid ASC
    LIMIT $page_size START $offset;
"""

_EXTRACTED_INTO_LINEAGE_RELATE_QUERY = """
FOR $edge_record IN $edges {
    LET $chunk_id = (
        SELECT VALUE id FROM document_chunks
        WHERE organization_id = $edge_record.organization_id
            AND uuid = $edge_record.chunk_id
        LIMIT 1
    )[0];
    IF $chunk_id != NONE {
        LET $entity_id = type::record($edge_record.entity_ref);
        UPSERT $entity_id SET
            uuid = $edge_record.entity_id,
            organization_id = $edge_record.organization_id,
            updated_at = time::now();
        LET $edge = type::record($edge_record.edge_ref);
        LET $existing_edge = (SELECT VALUE id FROM extracted_into WHERE id = $edge LIMIT 1)[0];
        IF $existing_edge = NONE {
            RELATE $entity_id->$edge->$chunk_id CONTENT {
                uuid: $edge_record.uuid,
                organization_id: $edge_record.organization_id,
                entity_id: $edge_record.entity_id,
                chunk_id: $edge_record.chunk_id,
                document_id: $edge_record.document_id,
                source_id: $edge_record.source_id,
                created_at: $edge_record.created_at
            };
        } ELSE {
            UPDATE $edge SET
                uuid = $edge_record.uuid,
                organization_id = $edge_record.organization_id,
                entity_id = $edge_record.entity_id,
                chunk_id = $edge_record.chunk_id,
                document_id = $edge_record.document_id,
                source_id = $edge_record.source_id;
        };
    };
};
"""

_EXTRACTED_INTO_ENTITY_ANCHOR_QUERY = """
FOR $edge_record IN $edges {
    LET $chunk_id = (
        SELECT VALUE id FROM document_chunks
        WHERE organization_id = $edge_record.organization_id
            AND uuid = $edge_record.chunk_id
        LIMIT 1
    )[0];
    IF $chunk_id != NONE {
        LET $entity_id = type::record($edge_record.entity_ref);
        UPSERT $entity_id SET
            uuid = $edge_record.entity_id,
            organization_id = $edge_record.organization_id,
            updated_at = time::now();
    };
};
"""


def lineage_edge_id(prefix: str, organization_id: str, *parts: str) -> str:
    digest = hashlib.sha256(":".join((organization_id, *parts)).encode()).hexdigest()
    return f"{prefix}_{digest}"


def lineage_record_ref(table: str, record_id: str) -> str:
    escaped = record_id.replace("\\", "\\\\").replace("`", "\\`")
    return f"{table}:`{escaped}`"


def _lineage_edge_batches(
    edges: Sequence[SurrealRecord],
    *,
    batch_size: int = content_client.DEFAULT_BATCH_SIZE,
) -> list[list[SurrealRecord]]:
    return [list(edges[index : index + batch_size]) for index in range(0, len(edges), batch_size)]


async def _existing_lineage_edge_ids(
    client: SurrealContentClient,
    table: str,
    edge_ids: Sequence[str],
) -> set[str]:
    existing: set[str] = set()
    for batch in content_client.value_batches(edge_ids):
        rows = await content_client.select_many(
            client,
            f"SELECT uuid FROM {table} WHERE uuid INSIDE $edge_ids;",
            edge_ids=batch,
        )
        existing.update(models.coerce_str(row.get("uuid")) for row in rows)
    return existing


async def _existing_content_uuids(
    client: SurrealContentClient,
    table: str,
    *,
    organization_id: str,
    values: Sequence[str],
) -> set[str]:
    existing: set[str] = set()
    for batch in content_client.value_batches(values):
        rows = await content_client.select_many(
            client,
            f"""
            SELECT uuid FROM {table}
            WHERE organization_id = $organization_id
                AND uuid INSIDE $values;
            """,
            organization_id=organization_id,
            values=batch,
        )
        existing.update(models.coerce_str(row.get("uuid")) for row in rows)
    return existing


async def _pending_lineage_edges(
    client: SurrealContentClient,
    table: str,
    edges: Sequence[SurrealRecord],
) -> list[SurrealRecord]:
    deduped: list[SurrealRecord] = []
    seen: set[str] = set()
    for edge in edges:
        edge_id = models.coerce_str(edge.get("uuid"))
        if not edge_id or edge_id in seen:
            continue
        seen.add(edge_id)
        deduped.append(edge)
    existing = await _existing_lineage_edge_ids(
        client,
        table,
        [models.coerce_str(edge.get("uuid")) for edge in deduped],
    )
    return [edge for edge in deduped if models.coerce_str(edge.get("uuid")) not in existing]


async def _write_lineage_edges(
    client: SurrealContentClient,
    query: str,
    *,
    edges: Sequence[SurrealRecord],
    organization_id: str,
) -> None:
    for batch in _lineage_edge_batches(edges):
        await content_client.select_many_raw(
            client,
            query,
            organization_id=organization_id,
            edges=batch,
        )


async def _lineage_total_count(
    client: SurrealContentClient,
    table: str,
    *,
    organization_id: str,
) -> int:
    rows = await content_client.select_many(
        client,
        f"""
        SELECT count() AS total
        FROM {table}
        WHERE organization_id = $organization_id
        GROUP ALL;
        """,
        organization_id=organization_id,
    )
    if not rows:
        return 0
    return models.coerce_int(rows[0].get("total", rows[0].get("count")))


async def _materialize_derived_from_lineage(
    client: SurrealContentClient,
    *,
    organization_id: str,
    limit: int,
) -> int:
    remaining = limit
    offset = 0
    page_size = min(max(limit, 1), content_client.DEFAULT_BATCH_SIZE)
    while remaining > 0:
        rows = await content_client.select_many(
            client,
            _DERIVED_FROM_LINEAGE_CANDIDATE_QUERY,
            organization_id=organization_id,
            page_size=page_size,
            offset=offset,
        )
        if not rows:
            break
        offset += len(rows)
        edges: list[SurrealRecord] = []
        for row in rows:
            source_import_id = models.coerce_str(row.get("uuid"))
            for raw_memory_id in models.coerce_str_list(row.get("raw_memory_ids")):
                edge_uuid = lineage_edge_id(
                    "derived_from",
                    organization_id,
                    raw_memory_id,
                    source_import_id,
                )
                edges.append(
                    {
                        "uuid": edge_uuid,
                        "edge_ref": lineage_record_ref("derived_from", edge_uuid),
                        "organization_id": organization_id,
                        "raw_memory_id": raw_memory_id,
                        "source_import_id": source_import_id,
                        "source_id": None,
                        "created_at": models.utcnow(),
                    }
                )
        existing_raw_ids = await _existing_content_uuids(
            client,
            "raw_captures",
            organization_id=organization_id,
            values=[models.coerce_str(edge.get("raw_memory_id")) for edge in edges],
        )
        edges = [
            edge
            for edge in edges
            if models.coerce_str(edge.get("raw_memory_id")) in existing_raw_ids
        ]
        pending = await _pending_lineage_edges(client, "derived_from", edges)
        batch = pending[:remaining]
        if batch:
            await _write_lineage_edges(
                client,
                _DERIVED_FROM_LINEAGE_RELATE_QUERY,
                edges=batch,
                organization_id=organization_id,
            )
            remaining -= len(batch)
    return await _lineage_total_count(client, "derived_from", organization_id=organization_id)


async def _materialize_chunk_of_lineage(
    client: SurrealContentClient,
    *,
    organization_id: str,
    limit: int,
) -> int:
    remaining = limit
    offset = 0
    page_size = min(max(limit, 1), content_client.DEFAULT_BATCH_SIZE)
    while remaining > 0:
        rows = await content_client.select_many(
            client,
            _CHUNK_OF_LINEAGE_CANDIDATE_QUERY,
            organization_id=organization_id,
            page_size=page_size,
            offset=offset,
        )
        if not rows:
            break
        offset += len(rows)
        edges: list[SurrealRecord] = [
            {
                "uuid": edge_uuid,
                "edge_ref": lineage_record_ref("chunk_of", edge_uuid),
                "organization_id": organization_id,
                "chunk_id": models.coerce_str(row.get("uuid")),
                "document_id": models.coerce_str(row.get("document_id")),
                "source_id": models.coerce_optional_str(row.get("source_id")),
                "created_at": models.utcnow(),
            }
            for row in rows
            for edge_uuid in [
                lineage_edge_id(
                    "chunk_of",
                    organization_id,
                    models.coerce_str(row.get("uuid")),
                    models.coerce_str(row.get("document_id")),
                )
            ]
        ]
        existing_document_ids = await _existing_content_uuids(
            client,
            "crawled_documents",
            organization_id=organization_id,
            values=[models.coerce_str(edge.get("document_id")) for edge in edges],
        )
        edges = [
            edge
            for edge in edges
            if models.coerce_str(edge.get("document_id")) in existing_document_ids
        ]
        pending = await _pending_lineage_edges(client, "chunk_of", edges)
        batch = pending[:remaining]
        if batch:
            await _write_lineage_edges(
                client,
                _CHUNK_OF_LINEAGE_RELATE_QUERY,
                edges=batch,
                organization_id=organization_id,
            )
            remaining -= len(batch)
    return await _lineage_total_count(client, "chunk_of", organization_id=organization_id)


async def _materialize_supersedes_lineage(
    client: SurrealContentClient,
    *,
    organization_id: str,
    limit: int,
) -> int:
    remaining = limit
    offset = 0
    page_size = min(max(limit, 1), content_client.DEFAULT_BATCH_SIZE)
    while remaining > 0:
        rows = await content_client.select_many(
            client,
            _SUPERSEDES_LINEAGE_CANDIDATE_QUERY,
            organization_id=organization_id,
            page_size=page_size,
            offset=offset,
        )
        if not rows:
            break
        offset += len(rows)
        edges: list[SurrealRecord] = []
        for row in rows:
            metadata = models.coerce_dict(row.get("metadata"))
            superseded_id = models.coerce_optional_str(metadata.get("supersedes_raw_memory_id"))
            raw_memory_id = models.coerce_str(row.get("uuid"))
            if not superseded_id:
                replacement_id = models.coerce_optional_str(metadata.get("superseded_by_source_id"))
                if replacement_id:
                    superseded_id = raw_memory_id
                    raw_memory_id = replacement_id
            if not superseded_id:
                continue
            edge_uuid = lineage_edge_id(
                "supersedes",
                organization_id,
                raw_memory_id,
                superseded_id,
            )
            edges.append(
                {
                    "uuid": edge_uuid,
                    "edge_ref": lineage_record_ref("supersedes", edge_uuid),
                    "organization_id": organization_id,
                    "raw_memory_id": raw_memory_id,
                    "superseded_raw_memory_id": superseded_id,
                    "source_id": models.coerce_optional_str(row.get("source_id")),
                    "created_at": models.utcnow(),
                }
            )
        existing_superseded_ids = await _existing_content_uuids(
            client,
            "raw_captures",
            organization_id=organization_id,
            values=[models.coerce_str(edge.get("superseded_raw_memory_id")) for edge in edges],
        )
        existing_raw_ids = await _existing_content_uuids(
            client,
            "raw_captures",
            organization_id=organization_id,
            values=[models.coerce_str(edge.get("raw_memory_id")) for edge in edges],
        )
        edges = [
            edge
            for edge in edges
            if models.coerce_str(edge.get("superseded_raw_memory_id")) in existing_superseded_ids
            and models.coerce_str(edge.get("raw_memory_id")) in existing_raw_ids
        ]
        pending = await _pending_lineage_edges(client, "supersedes", edges)
        batch = pending[:remaining]
        if batch:
            await _write_lineage_edges(
                client,
                _SUPERSEDES_LINEAGE_RELATE_QUERY,
                edges=batch,
                organization_id=organization_id,
            )
            remaining -= len(batch)
    return await _lineage_total_count(client, "supersedes", organization_id=organization_id)


async def _materialize_extracted_into_lineage(
    client: SurrealContentClient,
    *,
    organization_id: str,
    limit: int,
) -> int:
    remaining = limit
    offset = 0
    page_size = min(max(limit, 1), content_client.DEFAULT_BATCH_SIZE)
    while remaining > 0:
        rows = await content_client.select_many(
            client,
            _EXTRACTED_INTO_LINEAGE_CANDIDATE_QUERY,
            organization_id=organization_id,
            page_size=page_size,
            offset=offset,
        )
        if not rows:
            break
        offset += len(rows)
        edges: list[SurrealRecord] = []
        for row in rows:
            chunk_id = models.coerce_str(row.get("uuid"))
            for entity_id in models.coerce_str_list(row.get("entity_ids")):
                edge_uuid = lineage_edge_id(
                    "extracted_into",
                    organization_id,
                    entity_id,
                    chunk_id,
                )
                edges.append(
                    {
                        "uuid": edge_uuid,
                        "edge_ref": lineage_record_ref("extracted_into", edge_uuid),
                        "entity_ref": lineage_record_ref("entity", entity_id),
                        "organization_id": organization_id,
                        "entity_id": entity_id,
                        "chunk_id": chunk_id,
                        "document_id": models.coerce_str(row.get("document_id")),
                        "source_id": models.coerce_optional_str(row.get("source_id")),
                        "created_at": models.utcnow(),
                    }
                )
        if edges:
            await _write_lineage_edges(
                client,
                _EXTRACTED_INTO_ENTITY_ANCHOR_QUERY,
                edges=edges,
                organization_id=organization_id,
            )
        pending = await _pending_lineage_edges(client, "extracted_into", edges)
        batch = pending[:remaining]
        if batch:
            await _write_lineage_edges(
                client,
                _EXTRACTED_INTO_LINEAGE_RELATE_QUERY,
                edges=batch,
                organization_id=organization_id,
            )
            remaining -= len(batch)
    return await _lineage_total_count(client, "extracted_into", organization_id=organization_id)


async def materialize_content_lineage(
    client: SurrealContentClient,
    *,
    organization_id: str,
    limit: int = 500,
) -> ContentLineageBackfillResult:
    bounded_limit = max(int(limit), 0)
    if not organization_id or bounded_limit <= 0:
        return ContentLineageBackfillResult()

    derived_from_count = await _materialize_derived_from_lineage(
        client,
        organization_id=organization_id,
        limit=bounded_limit,
    )
    chunk_of_count = await _materialize_chunk_of_lineage(
        client,
        organization_id=organization_id,
        limit=bounded_limit,
    )
    supersedes_count = await _materialize_supersedes_lineage(
        client,
        organization_id=organization_id,
        limit=bounded_limit,
    )
    extracted_into_count = await _materialize_extracted_into_lineage(
        client,
        organization_id=organization_id,
        limit=bounded_limit,
    )
    return ContentLineageBackfillResult(
        derived_from=derived_from_count,
        chunk_of=chunk_of_count,
        supersedes=supersedes_count,
        extracted_into=extracted_into_count,
    )


async def backfill_content_lineage(
    *,
    organization_id: str,
    limit: int = 500,
) -> ContentLineageBackfillResult:
    async with content_client.surreal_content_client() as client:
        return await materialize_content_lineage(
            client,
            organization_id=organization_id,
            limit=limit,
        )


async def get_raw_memory_lineage(
    *,
    organization_id: str,
    memory_id: str,
) -> dict[str, list[SurrealRecord]]:
    async with content_client.surreal_content_client() as client:
        derived_from = await content_client.select_many(
            client,
            """
            SELECT uuid, raw_memory_id, source_import_id, source_id, created_at
            FROM derived_from
            WHERE organization_id = $organization_id
                AND raw_memory_id = $memory_id
            ORDER BY created_at ASC, uuid ASC;
            """,
            organization_id=organization_id,
            memory_id=memory_id,
        )
        supersessions = await content_client.select_many(
            client,
            """
            SELECT uuid, raw_memory_id, superseded_raw_memory_id, source_id, created_at
            FROM supersedes
            WHERE organization_id = $organization_id
                AND (
                    raw_memory_id = $memory_id
                    OR superseded_raw_memory_id = $memory_id
                )
            ORDER BY created_at ASC, uuid ASC;
            """,
            organization_id=organization_id,
            memory_id=memory_id,
        )
    return {
        "derived_from": derived_from,
        "supersessions": supersessions,
    }
