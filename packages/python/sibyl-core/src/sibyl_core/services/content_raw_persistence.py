"""Raw memory capture persistence and revision-aware mutations."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import datetime
from typing import cast
from uuid import uuid4

from sibyl_core.backends.surreal import SurrealContentClient
from sibyl_core.embeddings.providers import (
    EmbeddingProvider,
)
from sibyl_core.errors import RevisionConflictError
from sibyl_core.memory_pipeline.quality import (
    normalize_memory_quality_metadata,
)
from sibyl_core.models.memory_scope import MemoryScope
from sibyl_core.models.reflection import (
    MemoryLifecycle,
    MemoryLifecycleState,
    ReflectionCandidate,
    with_memory_lifecycle_metadata,
)
from sibyl_core.services import content_client
from sibyl_core.services import content_lineage as lineage
from sibyl_core.services import content_models as models
from sibyl_core.services.content_models import RawMemory, RawMemoryWrite

_RAW_MEMORY_EMBEDDING_AUTO = object()

_RAW_MEMORY_BULK_UPSERT_QUERY = """
INSERT INTO raw_captures $rows ON DUPLICATE KEY UPDATE
    uuid = $input.uuid,
    organization_id = $input.organization_id,
    source_id = $input.source_id,
    principal_id = $input.principal_id,
    memory_scope = $input.memory_scope,
    scope_key = $input.scope_key,
    agent_id = $input.agent_id,
    project_id = $input.project_id,
    review_state = $input.review_state,
    entity_id = $input.entity_id,
    title = $input.title,
    raw_content = $input.raw_content,
    entity_type = $input.entity_type,
    tags = $input.tags,
    embedding = $input.embedding,
    metadata = $input.metadata,
    metadata.last_recalled_at = $input.last_recalled_at ?? last_recalled_at,
    metadata.last_used_at = $input.last_used_at ?? last_used_at,
    metadata.retrieval_count = $input.retrieval_count ?? retrieval_count ?? 0,
    metadata.citation_count = $input.citation_count ?? citation_count ?? 0,
    metadata.misled_count = $input.misled_count ?? misled_count ?? 0,
    provenance = $input.provenance,
    capture_surface = $input.capture_surface,
    created_by_user_id = $input.created_by_user_id,
    revision = (revision ?? 0) + 1,
    captured_at = $input.captured_at,
    deleted_at = $input.deleted_at,
    purge_after = $input.purge_after,
    last_recalled_at = $input.last_recalled_at ?? last_recalled_at,
    last_used_at = $input.last_used_at ?? last_used_at,
    retrieval_count = $input.retrieval_count ?? retrieval_count ?? 0,
    citation_count = $input.citation_count ?? citation_count ?? 0,
    misled_count = $input.misled_count ?? misled_count ?? 0,
    created_at = $input.created_at;
"""

_RAW_PROMOTION_VISIBLE_SCOPES = (
    MemoryScope.ORGANIZATION.value,
    MemoryScope.PUBLIC.value,
)


async def replace_raw_memory_records_bulk(
    client: SurrealContentClient,
    records: Sequence[models.SurrealRecord],
) -> list[models.SurrealRecord]:
    if not records:
        return []
    for record in records:
        if record.get("organization_id") is None:
            uuid = record.get("uuid") or "<unknown>"
            raise RuntimeError(f"raw_captures record {uuid} requires organization_id")
    rows = await content_client.select_many(
        client,
        _RAW_MEMORY_BULK_UPSERT_QUERY,
        rows=list(records),
    )
    if len(rows) != len(records):
        raise RuntimeError(
            f"failed to persist raw_captures batch: {len(rows)} of {len(records)} returned"
        )
    return rows


def _order_raw_memory_records_by_input(
    memories: Sequence[RawMemory],
    records: Sequence[models.SurrealRecord],
) -> list[models.SurrealRecord]:
    records_by_uuid: dict[str, models.SurrealRecord] = {}
    for record in records:
        uuid = str(record.get("uuid") or "")
        if not uuid:
            raise RuntimeError("raw_captures bulk returned a record without uuid")
        if uuid in records_by_uuid:
            raise RuntimeError(f"raw_captures bulk returned duplicate uuid {uuid}")
        records_by_uuid[uuid] = record

    ordered_records: list[models.SurrealRecord] = []
    for memory in memories:
        record = records_by_uuid.get(memory.id)
        if record is None:
            raise RuntimeError(f"raw_captures bulk omitted uuid {memory.id}")
        ordered_records.append(record)
    return ordered_records


def _raw_memory_from_write(write: RawMemoryWrite, *, captured_at: datetime) -> RawMemory:
    normalized_scope = models.coerce_memory_scope(write.memory_scope)
    models.validate_raw_memory_scope(normalized_scope, write.scope_key)
    metadata = normalize_memory_quality_metadata(write.metadata or {})
    return RawMemory(
        id=str(uuid4()),
        organization_id=write.organization_id,
        source_id=write.source_id,
        principal_id=write.principal_id,
        memory_scope=normalized_scope,
        scope_key=write.scope_key,
        agent_id=models.coerce_optional_str(metadata.get("agent_id")),
        project_id=models.coerce_optional_str(metadata.get("project_id")),
        review_state=models.coerce_str(metadata.get("review_state"), default="pending"),
        entity_type=write.entity_type,
        title=write.title,
        raw_content=write.raw_content,
        tags=list(write.tags or []),
        metadata=metadata,
        provenance=dict(write.provenance or {}),
        capture_surface=write.capture_surface,
        captured_at=captured_at,
        created_at=captured_at,
    )


async def _raw_memory_with_embedding(
    memory: RawMemory,
    embedding_provider: EmbeddingProvider | None,
) -> RawMemory:
    if embedding_provider is None or memory.embedding is not None:
        return memory
    embeddings = await embedding_provider.embed_texts(
        [
            models.raw_memory_embedding_text(
                title=memory.title,
                raw_content=memory.raw_content,
            )
        ],
        input_kind="document",
    )
    memory.embedding = models.embedding_vector_from_batch(
        embeddings,
        embedding_provider.metadata.dimensions,
    )
    metadata = dict(memory.metadata)
    metadata["embedding_metadata"] = models.raw_memory_embedding_metadata(
        embedding_provider.metadata
    )
    memory.metadata = metadata
    return memory


async def _raw_memories_with_embeddings(
    memories: Sequence[RawMemory],
    embedding_provider: EmbeddingProvider | None,
) -> list[RawMemory]:
    if embedding_provider is None:
        return list(memories)
    pending = [memory for memory in memories if memory.embedding is None]
    if not pending:
        return list(memories)

    embeddings = await embedding_provider.embed_texts(
        [
            models.raw_memory_embedding_text(
                title=memory.title,
                raw_content=memory.raw_content,
            )
            for memory in pending
        ],
        input_kind="document",
    )
    if len(embeddings) != len(pending):
        raise ValueError(
            f"embedding provider returned {len(embeddings)} vectors for {len(pending)} raw memories"
        )

    dimensions = embedding_provider.metadata.dimensions
    embedding_metadata = models.raw_memory_embedding_metadata(embedding_provider.metadata)
    for memory, embedding_values in zip(pending, embeddings, strict=True):
        memory.embedding = models.embedding_vector_from_batch([embedding_values], dimensions)
        metadata = dict(memory.metadata)
        metadata["embedding_metadata"] = embedding_metadata
        memory.metadata = metadata
    return list(memories)


def _raw_memory_embedding_surface(memory: RawMemory) -> str:
    return models.raw_memory_embedding_text(title=memory.title, raw_content=memory.raw_content)


def _raw_memory_without_embedding(memory: RawMemory) -> RawMemory:
    metadata = dict(memory.metadata)
    metadata.pop("embedding_metadata", None)
    return replace(memory, embedding=None, metadata=metadata)


def _raw_memory_with_existing_embedding(memory: RawMemory, existing: RawMemory) -> RawMemory:
    if memory.embedding is not None or existing.embedding is None:
        return memory
    metadata = dict(memory.metadata)
    existing_metadata = existing.metadata.get("embedding_metadata")
    if "embedding_metadata" not in metadata and existing_metadata is not None:
        metadata["embedding_metadata"] = existing_metadata
    return replace(memory, embedding=list(existing.embedding), metadata=metadata)


def _raw_memory_has_replacement_embedding(memory: RawMemory, existing: RawMemory | None) -> bool:
    if memory.embedding is None:
        return False
    if existing is None or existing.embedding is None:
        return True
    return memory.embedding != existing.embedding


async def _raw_memory_with_save_embedding(
    memory: RawMemory,
    embedding_provider: EmbeddingProvider | object | None,
) -> RawMemory:
    provider = (
        models.configured_raw_memory_embedding_provider()
        if embedding_provider is _RAW_MEMORY_EMBEDDING_AUTO
        else cast("EmbeddingProvider | None", embedding_provider)
    )
    return await _raw_memory_with_embedding(_raw_memory_without_embedding(memory), provider)


_PROJECTION_STAMP_KEY = "projected_capture_id"


def _with_projection_stamp(memory: RawMemory, existing_record: Mapping[str, object]) -> RawMemory:
    """Carry the capture queue's projection stamp across a full-row save.

    The API stamps metadata.projected_capture_id on the stored raw row when
    its projection is archived. A job that loaded the RawMemory seconds
    earlier and saves it back wholesale would otherwise erase the stamp,
    and the queue would list the memory twice again with nothing left to
    repair it.
    """
    if memory.metadata.get(_PROJECTION_STAMP_KEY):
        return memory
    existing_metadata = existing_record.get("metadata")
    stamp = (
        existing_metadata.get(_PROJECTION_STAMP_KEY)
        if isinstance(existing_metadata, Mapping)
        else None
    )
    if not stamp:
        return memory
    return replace(memory, metadata={**memory.metadata, _PROJECTION_STAMP_KEY: stamp})


async def _raw_memory_prepared_for_save(
    memory: RawMemory,
    *,
    existing: RawMemory | None,
    embedding_provider: EmbeddingProvider | object | None,
) -> RawMemory:
    if not models.raw_memory_recallable(memory):
        return _raw_memory_without_embedding(memory)
    if _raw_memory_has_replacement_embedding(memory, existing):
        return memory
    if existing is None:
        return await _raw_memory_with_save_embedding(memory, embedding_provider)

    text_changed = _raw_memory_embedding_surface(memory) != _raw_memory_embedding_surface(existing)
    if not text_changed and models.raw_memory_recallable(existing):
        return _raw_memory_with_existing_embedding(memory, existing)
    return await _raw_memory_with_save_embedding(memory, embedding_provider)


async def remember_raw_memory(
    *,
    organization_id: str,
    principal_id: str,
    source_id: str,
    raw_content: str,
    title: str = "",
    memory_scope: MemoryScope | str = MemoryScope.PRIVATE,
    scope_key: str | None = None,
    tags: list[str] | None = None,
    metadata: dict[str, object] | None = None,
    provenance: dict[str, object] | None = None,
    capture_surface: str | None = None,
    entity_type: str = "raw_memory",
    embedding_provider: EmbeddingProvider | object | None = _RAW_MEMORY_EMBEDDING_AUTO,
) -> RawMemory:
    memory = _raw_memory_from_write(
        RawMemoryWrite(
            organization_id=organization_id,
            principal_id=principal_id,
            source_id=source_id,
            raw_content=raw_content,
            title=title,
            memory_scope=memory_scope,
            scope_key=scope_key,
            tags=tags,
            metadata=metadata,
            provenance=provenance,
            capture_surface=capture_surface,
            entity_type=entity_type,
        ),
        captured_at=models.utcnow(),
    )
    provider = (
        models.configured_raw_memory_embedding_provider()
        if embedding_provider is _RAW_MEMORY_EMBEDDING_AUTO
        else cast("EmbeddingProvider | None", embedding_provider)
    )
    memory = await _raw_memory_with_embedding(memory, provider)
    async with content_client.surreal_content_client() as client:
        record = await content_client.replace_record(
            client,
            "raw_captures",
            uuid=memory.id,
            record=models.raw_memory_record(memory),
        )
    return models.raw_memory_from_record(record)


async def remember_raw_memories(
    writes: Sequence[RawMemoryWrite],
    *,
    embedding_provider: EmbeddingProvider | object | None = _RAW_MEMORY_EMBEDDING_AUTO,
) -> list[RawMemory]:
    if not writes:
        return []
    now = models.utcnow()
    memories = [_raw_memory_from_write(write, captured_at=now) for write in writes]
    provider = (
        models.configured_raw_memory_embedding_provider()
        if embedding_provider is _RAW_MEMORY_EMBEDDING_AUTO
        else cast("EmbeddingProvider | None", embedding_provider)
    )
    memories = await _raw_memories_with_embeddings(memories, provider)
    async with content_client.surreal_content_client() as client:
        records = await replace_raw_memory_records_bulk(
            client,
            [models.raw_memory_record(memory) for memory in memories],
        )
    ordered_records = _order_raw_memory_records_by_input(memories, records)
    return [models.raw_memory_from_record(record) for record in ordered_records]


async def remember_reflection_candidate_review(
    *,
    organization_id: str,
    principal_id: str,
    candidate: ReflectionCandidate,
    raw_source_ids: list[str],
    source_id: str | None = None,
    memory_scope: MemoryScope | str = MemoryScope.PRIVATE,
    scope_key: str | None = None,
    suggested_memory_scope: MemoryScope | str | None = None,
    suggested_scope_key: str | None = None,
    extraction_prompt_metadata: dict[str, object] | None = None,
) -> RawMemory:
    normalized_scope = models.coerce_memory_scope(memory_scope)
    suggested_scope = (
        models.coerce_memory_scope(suggested_memory_scope)
        if suggested_memory_scope is not None
        else normalized_scope
    )
    source_ids = list(dict.fromkeys(raw_source_ids))
    resolved_source_id = source_id or (source_ids[0] if source_ids else "reflection:manual")
    metadata: dict[str, object] = {
        **candidate.metadata,
        "capture_mode": "reflect",
        "capture_surface": "reflection_candidate",
        "remember_kind": candidate.kind,
        "reflection_reason": candidate.reason,
        "confidence": candidate.confidence,
        "raw_source_ids": source_ids,
        "source_ids": source_ids,
        "extraction_prompt_metadata": dict(extraction_prompt_metadata or {}),
        "suggested_memory_scope": suggested_scope.value,
        "suggested_scope_key": suggested_scope_key,
        "review_state": "pending",
    }
    metadata = with_memory_lifecycle_metadata(
        metadata,
        MemoryLifecycle(
            state=MemoryLifecycleState.ACTIVE,
            source_id=resolved_source_id,
            action="capture",
            reason="reflection_candidate_pending",
        ),
    )
    return await remember_raw_memory(
        organization_id=organization_id,
        principal_id=principal_id,
        source_id=resolved_source_id,
        raw_content=candidate.content,
        title=candidate.title,
        memory_scope=normalized_scope,
        scope_key=scope_key,
        tags=candidate.tags,
        metadata=metadata,
        provenance={"raw_source_ids": source_ids},
        capture_surface="reflection_candidate",
        entity_type=candidate.kind,
    )


async def get_raw_memory(
    *,
    organization_id: str,
    memory_id: str,
) -> RawMemory | None:
    async with content_client.surreal_content_client() as client:
        record = await content_client.select_one(
            client,
            "SELECT * FROM raw_captures "
            "WHERE uuid = $memory_id AND organization_id = $organization_id LIMIT 1;",
            memory_id=memory_id,
            organization_id=organization_id,
        )
    return models.raw_memory_from_record(record) if record is not None else None


async def get_raw_memory_by_source_id(
    *,
    organization_id: str,
    source_id: str,
    principal_id: str | None = None,
    memory_scope: MemoryScope | str | None = None,
    scope_key: str | None = None,
) -> RawMemory | None:
    filters = [
        "source_id = $source_id",
        "organization_id = $organization_id",
    ]
    params: dict[str, object] = {
        "source_id": source_id,
        "organization_id": organization_id,
    }
    if principal_id is not None:
        filters.append("principal_id = $principal_id")
        params["principal_id"] = principal_id
    if memory_scope is not None:
        filters.append("memory_scope = $memory_scope")
        params["memory_scope"] = models.coerce_memory_scope(memory_scope).value
        if scope_key is None:
            filters.append("scope_key IS NONE")
        else:
            filters.append("scope_key = $scope_key")
            params["scope_key"] = scope_key

    async with content_client.surreal_content_client() as client:
        record = await content_client.select_one(
            client,
            "SELECT * FROM raw_captures "
            f"WHERE {' AND '.join(filters)} "
            "ORDER BY captured_at DESC LIMIT 1;",
            **params,
        )
    return models.raw_memory_from_record(record) if record is not None else None


async def list_raw_memories_by_source_id(
    *,
    organization_id: str,
    source_id: str,
) -> list[RawMemory]:
    async with content_client.surreal_content_client() as client:
        rows = await content_client.select_many(
            client,
            "SELECT * FROM raw_captures "
            "WHERE source_id = $source_id AND organization_id = $organization_id "
            "ORDER BY captured_at DESC LIMIT 100;",
            source_id=source_id,
            organization_id=organization_id,
        )
    return [models.raw_memory_from_record(row) for row in rows]


async def get_raw_memory_by_dedupe_key(
    *,
    organization_id: str,
    dedupe_key: str,
    principal_id: str | None = None,
    memory_scope: MemoryScope | str | None = None,
    scope_key: str | None = None,
) -> RawMemory | None:
    filters = [
        "organization_id = $organization_id",
        "metadata.dedupe_key = $dedupe_key",
    ]
    params: dict[str, object] = {
        "organization_id": organization_id,
        "dedupe_key": dedupe_key,
    }
    if principal_id is not None:
        filters.append("principal_id = $principal_id")
        params["principal_id"] = principal_id
    if memory_scope is not None:
        filters.append("memory_scope = $memory_scope")
        params["memory_scope"] = models.coerce_memory_scope(memory_scope).value
        if scope_key is None:
            filters.append("scope_key IS NONE")
        else:
            filters.append("scope_key = $scope_key")
            params["scope_key"] = scope_key

    async with content_client.surreal_content_client() as client:
        record = await content_client.select_one(
            client,
            f"SELECT * FROM raw_captures WHERE {' AND '.join(filters)} "
            "ORDER BY captured_at DESC LIMIT 1;",
            **params,
        )
    return models.raw_memory_from_record(record) if record is not None else None


async def resolve_raw_memory_prefix(
    *,
    organization_id: str,
    prefix: str,
    limit: int = 20,
) -> list[RawMemory]:
    normalized = prefix.strip()
    if not normalized or limit <= 0:
        return []
    async with content_client.surreal_content_client() as client:
        rows = await content_client.select_many(
            client,
            "SELECT * FROM raw_captures "
            "WHERE organization_id = $organization_id "
            "AND ((uuid >= $prefix AND uuid < $prefix_upper) "
            "OR (source_id >= $prefix AND source_id < $prefix_upper)) "
            "ORDER BY captured_at DESC LIMIT $limit;",
            organization_id=organization_id,
            prefix=normalized,
            prefix_upper=f"{normalized}\uffff",
            limit=limit,
        )
    return [models.raw_memory_from_record(row) for row in rows]


async def list_raw_memories_for_promotion(
    *,
    organization_id: str,
    raw_memory_ids: list[str] | None = None,
    limit: int = 100,
) -> list[RawMemory]:
    if limit <= 0:
        return []
    if raw_memory_ids:
        rows: list[models.SurrealRecord] = []
        for batch in content_client.value_batches(list(dict.fromkeys(raw_memory_ids))):
            async with content_client.surreal_content_client() as client:
                rows.extend(
                    await content_client.select_many(
                        client,
                        "SELECT * FROM raw_captures "
                        "WHERE organization_id = $organization_id AND uuid INSIDE $raw_memory_ids "
                        "AND memory_scope INSIDE $raw_promotion_visible_scopes "
                        "ORDER BY captured_at ASC, uuid ASC;",
                        organization_id=organization_id,
                        raw_memory_ids=batch,
                        raw_promotion_visible_scopes=list(_RAW_PROMOTION_VISIBLE_SCOPES),
                    )
                )
        memories = [models.raw_memory_from_record(row) for row in rows]
        order = {memory_id: index for index, memory_id in enumerate(raw_memory_ids)}
        return [
            memory
            for memory in sorted(memories, key=lambda memory: order.get(memory.id, len(order)))
            if memory.memory_scope.value in _RAW_PROMOTION_VISIBLE_SCOPES
        ][:limit]

    query_limit = limit * content_client.LIFECYCLE_FILTER_OVERFETCH_FACTOR
    async with content_client.surreal_content_client() as client:
        rows = await content_client.select_many(
            client,
            "SELECT * FROM raw_captures "
            "WHERE organization_id = $organization_id "
            "AND memory_scope INSIDE $raw_promotion_visible_scopes "
            "AND deleted_at = NONE "
            "AND (metadata.raw_promotion_state = NONE "
            "OR metadata.raw_promotion_state = '' "
            "OR metadata.raw_promotion_state = 'pending' "
            "OR (metadata.raw_promotion_state = 'promoted' "
            "AND (metadata.raw_promotion_lineage_missing_count > 0 "
            "OR (metadata.raw_promotion_lineage_missing_count = NONE "
            "AND (metadata.source_record_metadata.parent_uuid != NONE "
            "OR metadata.source_record_metadata.forked_from != NONE "
            "OR metadata.source_record_metadata.source_tool_assistant_uuid != NONE "
            "OR metadata.source_record_metadata.is_sidechain = true))))) "
            "ORDER BY captured_at ASC, uuid ASC LIMIT $limit;",
            organization_id=organization_id,
            raw_promotion_visible_scopes=list(_RAW_PROMOTION_VISIBLE_SCOPES),
            limit=query_limit,
        )
    memories = [models.raw_memory_from_record(row) for row in rows]
    return [
        memory
        for memory in memories
        if memory.memory_scope.value in _RAW_PROMOTION_VISIBLE_SCOPES
        and memory.deleted_at is None
        and models.raw_memory_currently_recallable(memory)
    ][:limit]


async def save_raw_memory(
    memory: RawMemory,
    *,
    embedding_provider: EmbeddingProvider | object | None = _RAW_MEMORY_EMBEDDING_AUTO,
    expected_revision: int | None = None,
    superseded_by_memory_id: str | None = None,
) -> RawMemory:
    if expected_revision is not None and expected_revision < 1:
        raise ValueError("expected_revision must be at least 1")
    async with content_client.surreal_content_client() as client:
        existing_record = await content_client.select_one(
            client,
            "SELECT * FROM raw_captures "
            "WHERE organization_id = $organization_id AND uuid = $uuid LIMIT 1;",
            organization_id=memory.organization_id,
            uuid=memory.id,
        )
        memory = await _raw_memory_prepared_for_save(
            memory,
            existing=models.raw_memory_from_record(existing_record) if existing_record else None,
            embedding_provider=embedding_provider,
        )
        if existing_record is None:
            if expected_revision is not None:
                raise RevisionConflictError(memory.id, expected_revision, 0)
            if superseded_by_memory_id is not None:
                raise ValueError("supersession requires an existing raw memory")
            record = await content_client.replace_record(
                client,
                "raw_captures",
                uuid=memory.id,
                record=models.raw_memory_record(memory),
            )
        else:
            memory = _with_projection_stamp(memory, existing_record)
            update_record = models.raw_memory_record(memory)
            update_record.pop("revision", None)
            supersession = None
            if superseded_by_memory_id is not None:
                edge_uuid = lineage.lineage_edge_id(
                    "supersedes",
                    memory.organization_id,
                    superseded_by_memory_id,
                    memory.id,
                )
                supersession = {
                    "uuid": edge_uuid,
                    "edge_ref": lineage.lineage_record_ref("supersedes", edge_uuid),
                    "raw_memory_id": superseded_by_memory_id,
                    "superseded_raw_memory_id": memory.id,
                    "organization_id": memory.organization_id,
                    "created_at": models.utcnow(),
                }
            rows = await content_client.select_many_raw(
                client,
                """
                    BEGIN TRANSACTION;
                    LET $updated = (
                        UPDATE raw_captures MERGE $record
                        WHERE organization_id = $organization_id
                            AND uuid = $uuid
                            AND ($expected_revision = NONE OR revision = $expected_revision)
                        RETURN AFTER
                    );
                    LET $saved = (UPDATE $updated SET revision += 1 RETURN AFTER);
                    IF $supersession != NONE AND array::len($saved) > 0 {
                        LET $replacement = (
                            SELECT id, source_id FROM raw_captures
                            WHERE organization_id = $organization_id
                                AND uuid = $supersession.raw_memory_id
                            LIMIT 1
                        )[0];
                        LET $superseded = (
                            SELECT VALUE id FROM raw_captures
                            WHERE organization_id = $organization_id
                                AND uuid = $supersession.superseded_raw_memory_id
                            LIMIT 1
                        )[0];
                        IF $replacement = NONE OR $superseded = NONE {
                            THROW "supersession_reference_missing";
                        };
                        LET $replacement_id = $replacement.id;
                        LET $edge = type::record($supersession.edge_ref);
                        LET $existing_edge = (
                            SELECT VALUE id FROM supersedes WHERE id = $edge LIMIT 1
                        )[0];
                        IF $existing_edge = NONE {
                            RELATE $replacement_id->$edge->$superseded CONTENT {
                                uuid: $supersession.uuid,
                                organization_id: $organization_id,
                                raw_memory_id: $supersession.raw_memory_id,
                                superseded_raw_memory_id: $supersession.superseded_raw_memory_id,
                                source_id: $replacement.source_id,
                                created_at: $supersession.created_at
                            };
                        };
                    };
                    COMMIT TRANSACTION;
                    RETURN $saved;
                """,
                organization_id=memory.organization_id,
                uuid=memory.id,
                expected_revision=expected_revision,
                record=update_record,
                supersession=supersession,
            )
            if not rows and expected_revision is not None:
                current = await content_client.select_one(
                    client,
                    "SELECT revision FROM raw_captures "
                    "WHERE organization_id = $organization_id AND uuid = $uuid LIMIT 1;",
                    organization_id=memory.organization_id,
                    uuid=memory.id,
                )
                actual_revision = max(models.coerce_int((current or {}).get("revision")), 0)
                raise RevisionConflictError(
                    memory.id,
                    expected_revision,
                    actual_revision,
                )
            if not rows:
                raise RuntimeError(f"failed to persist raw_captures record {memory.id}")
            record = rows[0]
    return models.raw_memory_from_record(record)
