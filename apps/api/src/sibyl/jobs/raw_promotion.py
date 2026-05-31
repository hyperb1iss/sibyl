"""Promote raw captures into chunked documents and graph anchors."""

from __future__ import annotations

import hashlib
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

import structlog

from sibyl.config import settings
from sibyl.crawler.chunker import ChunkStrategy, DocumentChunker
from sibyl.crawler.embedder import EmbeddingService
from sibyl.jobs.memory_extraction import enqueue_memory_extraction_batches
from sibyl.persistence.content_common import (
    CrawledDocumentRecord,
    DocumentChunkRecord,
    utcnow_naive,
)
from sibyl.persistence.content_runtime import (
    delete_document_chunks_for_document,
    get_content_read_session,
    save_crawled_document_record,
    save_document_chunks,
)
from sibyl.persistence.graph_runtime import get_entity_graph_runtime
from sibyl_core.models.entities import Entity, EntityType, Relationship, RelationshipType
from sibyl_core.models.sources import SourceImportManifest
from sibyl_core.observability import elapsed_ms
from sibyl_core.services.source_adapters import build_source_record_id
from sibyl_core.services.surreal_content import (
    RawMemory,
    get_raw_memory_by_source_id,
    list_raw_memories_for_promotion,
    raw_memory_recallable,
    save_raw_memory,
)

log = structlog.get_logger()


@dataclass(frozen=True, slots=True)
class _LineageReference:
    relationship_type: RelationshipType
    reference: str
    metadata_key: str
    adapter_record_id: str | None = None
    parent_to_current: bool = False


async def promote_raw_captures(
    ctx: dict[str, Any],  # noqa: ARG001
    organization_id: str,
    *,
    raw_memory_ids: list[str] | None = None,
    limit: int = 100,
    force: bool = False,
) -> dict[str, Any]:
    started_at = time.perf_counter()
    memories = await list_raw_memories_for_promotion(
        organization_id=organization_id,
        raw_memory_ids=raw_memory_ids,
        limit=limit,
    )
    result: dict[str, Any] = {
        "organization_id": organization_id,
        "selected_count": len(memories),
        "promoted_count": 0,
        "skipped_deleted_count": 0,
        "skipped_superseded_count": 0,
        "skipped_existing_count": 0,
        "skipped_review_count": 0,
        "failed_count": 0,
        "chunk_count": 0,
        "raw_memory_ids": [],
        "document_ids": [],
        "failed_records": [],
    }
    if not memories:
        result["duration_ms"] = elapsed_ms(started_at)
        return result

    chunker = DocumentChunker()
    embedder = EmbeddingService()
    for memory in memories:
        try:
            outcome = await _promote_one(memory, chunker=chunker, embedder=embedder, force=force)
        except Exception as exc:
            result["failed_count"] += 1
            result["failed_records"].append(
                {
                    "raw_memory_id": memory.id,
                    "error": str(exc),
                    "type": type(exc).__name__,
                }
            )
            await _mark_failed(memory, error=str(exc))
            log.warning("raw_capture_promotion_failed", raw_memory_id=memory.id, error=str(exc))
            continue

        if outcome["status"] == "promoted":
            result["promoted_count"] += 1
            result["chunk_count"] += outcome["chunk_count"]
            result["raw_memory_ids"].append(memory.id)
            result["document_ids"].append(outcome["document_id"])
        elif outcome["status"] == "skipped_deleted":
            result["skipped_deleted_count"] += 1
        elif outcome["status"] == "skipped_superseded":
            result["skipped_superseded_count"] += 1
        elif outcome["status"] == "skipped_existing":
            result["skipped_existing_count"] += 1
        else:
            result["skipped_review_count"] += 1

    result["duration_ms"] = elapsed_ms(started_at)
    return result


async def _promote_one(
    memory: RawMemory,
    *,
    chunker: DocumentChunker,
    embedder: EmbeddingService,
    force: bool,
) -> dict[str, Any]:
    skip_status = _promotion_skip_status(memory, force=force)
    if skip_status is not None:
        if skip_status == "skipped_existing" and memory.entity_id:
            lineage = await _safe_lineage_metadata(memory, entity_id=memory.entity_id)
            if lineage:
                memory.metadata = {**dict(memory.metadata), **lineage}
                await save_raw_memory(memory)
        if skip_status in {"skipped_deleted", "skipped_superseded"}:
            await _mark_skipped(memory, skip_status)
        return {"status": skip_status, "chunk_count": 0}

    document = _document_from_raw_memory(memory)
    chunks = chunker.chunk_document(document, strategy=_chunk_strategy(memory))
    if not chunks:
        raise ValueError("raw capture produced no chunks")

    embeddings = await embedder.embed_chunks(chunks)
    if len(embeddings) != len(chunks):
        msg = f"embedding count mismatch: {len(embeddings)} for {len(chunks)} chunks"
        raise ValueError(msg)

    db_chunks = [
        DocumentChunkRecord(
            id=uuid5(NAMESPACE_URL, f"sibyl.raw_promotion:{memory.id}:{chunk.chunk_index}"),
            document_id=document.id,
            organization_id=document.organization_id,
            source_id=memory.source_id,
            chunk_index=chunk.chunk_index,
            chunk_type=chunk.chunk_type,
            content=chunk.content,
            context=chunk.context,
            token_count=chunk.token_count,
            start_char=chunk.start_char,
            end_char=chunk.end_char,
            heading_path=chunk.heading_path,
            language=chunk.language,
            embedding=embeddings[index],
            is_complete=True,
            has_entities=False,
            entity_ids=[],
        )
        for index, chunk in enumerate(chunks)
    ]

    async with get_content_read_session() as session:
        stored_document = await save_crawled_document_record(session, document=document)
        await delete_document_chunks_for_document(
            session,
            document_id=stored_document.id,
            organization_id=stored_document.organization_id,
        )
        saved_chunks = await save_document_chunks(session, chunks=db_chunks)

    entity_id = await _upsert_document_entity(
        memory, stored_document, chunk_count=len(saved_chunks)
    )
    promoted_at = datetime.now(UTC).isoformat()
    metadata = dict(memory.metadata)
    metadata.update(
        {
            "raw_promotion_state": "promoted",
            "raw_promotion_document_id": str(stored_document.id),
            "raw_promotion_chunk_count": len(saved_chunks),
            "raw_promotion_promoted_at": promoted_at,
        }
    )
    metadata.update(
        await _source_extraction_metadata(
            memory,
            stored_document,
            entity_id=entity_id,
        )
    )
    metadata.update(await _safe_lineage_metadata(memory, entity_id=entity_id))
    memory.metadata = metadata
    memory.entity_id = entity_id
    await save_raw_memory(memory)
    return {
        "status": "promoted",
        "document_id": str(stored_document.id),
        "chunk_count": len(saved_chunks),
        "entity_id": entity_id,
    }


def _document_from_raw_memory(memory: RawMemory) -> CrawledDocumentRecord:
    now = utcnow_naive()
    document_id = UUID(memory.id)
    organization_id = UUID(memory.organization_id)
    raw_content = memory.raw_content or ""
    return CrawledDocumentRecord(
        id=document_id,
        organization_id=organization_id,
        source_id=memory.source_id,
        url=_raw_memory_url(memory),
        title=memory.title or memory.source_id or str(document_id),
        raw_content=raw_content,
        content=raw_content,
        content_hash=_content_hash(memory),
        language=_raw_memory_language(memory),
        word_count=len(raw_content.split()),
        token_count=max(1, len(raw_content) // 4) if raw_content else 0,
        has_code=_raw_memory_has_code(memory),
        crawled_at=memory.captured_at or now,
        created_at=memory.created_at or now,
        updated_at=now,
    )


def _content_hash(memory: RawMemory) -> str:
    existing = memory.metadata.get("content_hash")
    if existing:
        return str(existing)
    return hashlib.sha256((memory.raw_content or "").encode()).hexdigest()


def _raw_memory_url(memory: RawMemory) -> str:
    for key in ("source_uri", "url", "canonical_url"):
        value = memory.metadata.get(key) or memory.provenance.get(key)
        if value:
            return str(value)
    return f"raw-memory://{memory.id}"


def _raw_memory_language(memory: RawMemory) -> str | None:
    language = memory.metadata.get("language") or memory.provenance.get("language")
    return str(language) if language else None


def _raw_memory_has_code(memory: RawMemory) -> bool:
    media_type = str(memory.metadata.get("media_type") or "").lower()
    source_type = str(memory.metadata.get("source_type") or "").lower()
    return (
        "code" in media_type
        or source_type in {"code", "git", "github"}
        or "```" in (memory.raw_content or "")
    )


def _chunk_strategy(memory: RawMemory) -> ChunkStrategy:
    media_type = str(memory.metadata.get("media_type") or "").lower()
    source_type = str(memory.metadata.get("source_type") or "").lower()
    if _raw_memory_has_code(memory):
        return ChunkStrategy.CODE
    if "transcript" in media_type or source_type in {"transcript", "audio", "meeting"}:
        return ChunkStrategy.SLIDING
    return ChunkStrategy.SEMANTIC


def _promotion_skip_status(memory: RawMemory, *, force: bool) -> str | None:
    if memory.deleted_at is not None:
        return "skipped_deleted"
    if _raw_memory_superseded(memory):
        return "skipped_superseded"
    if not force and memory.metadata.get("raw_promotion_state") == "promoted" and memory.entity_id:
        return "skipped_existing"
    if not raw_memory_recallable(memory):
        return "skipped_review"
    return None


def _raw_memory_superseded(memory: RawMemory) -> bool:
    if str(memory.review_state or "").lower() == "superseded":
        return True
    lifecycle_state = str(memory.metadata.get("lifecycle_state") or "").lower()
    return lifecycle_state == "superseded" or bool(
        memory.metadata.get("superseded_by_raw_memory_id")
        or memory.metadata.get("superseded_by_source_id")
    )


async def _upsert_document_entity(
    memory: RawMemory,
    document: CrawledDocumentRecord,
    *,
    chunk_count: int,
) -> str:
    entity_id = memory.entity_id or str(document.id)
    runtime = await get_entity_graph_runtime(memory.organization_id)
    await runtime.entity_manager.create_direct(
        Entity(
            id=entity_id,
            entity_type=EntityType.DOCUMENT,
            name=document.title or document.url,
            description=document.title or "",
            content=document.url,
            organization_id=memory.organization_id,
            created_by=memory.created_by_user_id or memory.principal_id,
            metadata={
                "created_by": "raw_promotion",
                "raw_memory_id": memory.id,
                "source_id": memory.source_id,
                "document_id": str(document.id),
                "chunk_count": chunk_count,
            },
        ),
        generate_embedding=False,
    )
    return entity_id


async def _lineage_metadata(memory: RawMemory, *, entity_id: str) -> dict[str, object]:
    references = _lineage_references(memory)
    if not references:
        return {}

    runtime = await get_entity_graph_runtime(memory.organization_id)
    relationship_manager = getattr(runtime, "relationship_manager", None)
    create_direct_bulk = getattr(relationship_manager, "create_direct_bulk", None)
    if not callable(create_direct_bulk):
        return {
            "raw_promotion_lineage_edge_count": 0,
            "raw_promotion_lineage_missing_count": len(references),
            "raw_promotion_lineage_missing_reasons": ["relationship_manager_unavailable"],
        }

    relationships: list[Relationship] = []
    missing_reasons: list[str] = []
    for reference in references:
        parent_source_id, parent_adapter_record_id = _lineage_parent_source_id(memory, reference)
        if not parent_source_id:
            missing_reasons.append(f"{reference.metadata_key}:missing_source_identity")
            continue
        parent = await _lineage_parent_memory(memory, source_id=parent_source_id)
        if parent is None or not parent.entity_id:
            missing_reasons.append(f"{reference.metadata_key}:missing_parent_entity")
            continue
        if parent.entity_id == entity_id:
            missing_reasons.append(f"{reference.metadata_key}:self_reference")
            continue
        relationships.append(
            _lineage_relationship(
                memory,
                entity_id=entity_id,
                reference=reference,
                parent=parent,
                parent_source_id=parent_source_id,
                parent_adapter_record_id=parent_adapter_record_id,
            )
        )

    written_ids = await create_direct_bulk(relationships, generate_embeddings=False)
    if len(written_ids) < len(relationships):
        missing_reasons.append("relationship_endpoint_missing")
    missing_count = len(references) - len(written_ids)
    metadata: dict[str, object] = {
        "raw_promotion_lineage_edge_count": len(written_ids),
        "raw_promotion_lineage_missing_count": missing_count,
    }
    if written_ids:
        metadata["raw_promotion_lineage_relationship_ids"] = list(written_ids)
    if missing_reasons or len(written_ids) < len(relationships):
        metadata["raw_promotion_lineage_missing_reasons"] = missing_reasons
    return metadata


async def _safe_lineage_metadata(memory: RawMemory, *, entity_id: str) -> dict[str, object]:
    try:
        return await _lineage_metadata(memory, entity_id=entity_id)
    except Exception as exc:
        log.warning("raw_capture_lineage_failed", raw_memory_id=memory.id, error=str(exc))
        references = _lineage_references(memory)
        return {
            "raw_promotion_lineage_edge_count": 0,
            "raw_promotion_lineage_missing_count": len(references),
            "raw_promotion_lineage_error": str(exc),
            "raw_promotion_lineage_failed_at": datetime.now(UTC).isoformat(),
        }


def _lineage_references(memory: RawMemory) -> list[_LineageReference]:
    record_metadata = _source_record_metadata(memory)
    references: list[_LineageReference] = []
    parent_uuid = _metadata_text(record_metadata, "parent_uuid")
    if parent_uuid:
        references.append(
            _LineageReference(
                relationship_type=RelationshipType.REPLIES_TO,
                reference=parent_uuid,
                metadata_key="parent_uuid",
                adapter_record_id=_metadata_text(record_metadata, "parent_adapter_record_id"),
            )
        )
    forked_from = _metadata_text(record_metadata, "forked_from")
    if forked_from:
        references.append(
            _LineageReference(
                relationship_type=RelationshipType.FORKED_FROM,
                reference=forked_from,
                metadata_key="forked_from",
                adapter_record_id=_metadata_text(record_metadata, "forked_from_adapter_record_id"),
            )
        )
    source_tool_assistant_uuid = _metadata_text(record_metadata, "source_tool_assistant_uuid")
    if source_tool_assistant_uuid:
        references.append(
            _LineageReference(
                relationship_type=RelationshipType.SPAWNED_SUBAGENT,
                reference=source_tool_assistant_uuid,
                metadata_key="source_tool_assistant_uuid",
                adapter_record_id=_metadata_text(
                    record_metadata, "source_tool_assistant_adapter_record_id"
                ),
                parent_to_current=True,
            )
        )
    elif bool(record_metadata.get("is_sidechain")) and parent_uuid:
        references.append(
            _LineageReference(
                relationship_type=RelationshipType.SPAWNED_SUBAGENT,
                reference=parent_uuid,
                metadata_key="is_sidechain",
                adapter_record_id=_metadata_text(record_metadata, "parent_adapter_record_id"),
                parent_to_current=True,
            )
        )

    unique: dict[tuple[RelationshipType, str, bool], _LineageReference] = {}
    for reference in references:
        unique[(reference.relationship_type, reference.reference, reference.parent_to_current)] = (
            reference
        )
    return list(unique.values())


def _lineage_parent_source_id(
    memory: RawMemory,
    reference: _LineageReference,
) -> tuple[str | None, str | None]:
    if reference.reference.startswith("source-record:"):
        return reference.reference, None

    adapter_name = _metadata_text(memory.metadata, "adapter_name") or _metadata_text(
        memory.provenance, "source_adapter"
    )
    source_identity = _metadata_text(memory.metadata, "source_identity") or _metadata_text(
        memory.provenance, "source_identity"
    )
    if not adapter_name or not source_identity:
        return None, None

    adapter_record_id = reference.adapter_record_id or _parent_adapter_record_id(
        memory, reference.reference
    )
    manifest = SourceImportManifest(
        adapter_name=adapter_name,
        adapter_version=_metadata_text(memory.metadata, "adapter_version")
        or _metadata_text(memory.provenance, "source_adapter_version")
        or "unknown",
        source_identity=source_identity,
    )
    return (
        build_source_record_id(manifest=manifest, adapter_record_id=adapter_record_id),
        adapter_record_id,
    )


async def _lineage_parent_memory(memory: RawMemory, *, source_id: str) -> RawMemory | None:
    return await get_raw_memory_by_source_id(
        organization_id=memory.organization_id,
        source_id=source_id,
        principal_id=memory.principal_id if memory.memory_scope.value == "private" else None,
        memory_scope=memory.memory_scope,
        scope_key=memory.scope_key,
    )


def _parent_adapter_record_id(memory: RawMemory, reference: str) -> str:
    adapter_record_id = _metadata_text(memory.metadata, "adapter_record_id") or _metadata_text(
        memory.provenance, "adapter_record_id"
    )
    if not adapter_record_id:
        return reference
    file_key = _adapter_file_key(adapter_record_id)
    if file_key and reference.startswith(f"{file_key}:"):
        return reference
    if ".jsonl:" in reference:
        return reference
    if file_key:
        return f"{file_key}:{reference}"
    return reference


def _adapter_file_key(adapter_record_id: str) -> str | None:
    marker = ".jsonl:"
    if marker in adapter_record_id:
        return adapter_record_id.split(marker, 1)[0] + ".jsonl"
    if ":" not in adapter_record_id:
        return None
    return adapter_record_id.split(":", 1)[0]


def _lineage_relationship(
    memory: RawMemory,
    *,
    entity_id: str,
    reference: _LineageReference,
    parent: RawMemory,
    parent_source_id: str,
    parent_adapter_record_id: str | None,
) -> Relationship:
    source_id, target_id = (
        (parent.entity_id, entity_id)
        if reference.parent_to_current
        else (entity_id, parent.entity_id)
    )
    assert source_id is not None
    assert target_id is not None
    return Relationship(
        id=str(
            uuid5(
                NAMESPACE_URL,
                f"sibyl.raw_promotion.lineage:{memory.id}:"
                f"{reference.relationship_type.value}:{parent_source_id}",
            )
        ),
        relationship_type=reference.relationship_type,
        source_id=source_id,
        target_id=target_id,
        metadata={
            "adapter_record_id": memory.metadata.get("adapter_record_id"),
            "lineage_key": reference.metadata_key,
            "lineage_reference": reference.reference,
            "parent_adapter_record_id": parent_adapter_record_id,
            "parent_raw_memory_id": parent.id,
            "parent_source_id": parent_source_id,
            "raw_memory_id": memory.id,
            "source_id": memory.source_id,
        },
    )


def _source_record_metadata(memory: RawMemory) -> dict[str, object]:
    value = memory.metadata.get("source_record_metadata")
    if isinstance(value, Mapping):
        return {str(key): item for key, item in value.items()}
    return {}


def _metadata_text(metadata: Mapping[str, object], key: str) -> str | None:
    value = metadata.get(key)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


async def _source_extraction_metadata(
    memory: RawMemory,
    document: CrawledDocumentRecord,
    *,
    entity_id: str,
) -> dict[str, object]:
    if not settings.auto_extract_entities:
        return {"source_extraction_state": "disabled"}
    try:
        result = await enqueue_memory_extraction_batches(
            [_memory_extraction_source_payload(memory, document, entity_id=entity_id)],
            memory.organization_id,
            created_source_ids=[entity_id],
        )
    except Exception as exc:
        return {
            "source_extraction_state": "failed",
            "source_extraction_error": str(exc),
            "source_extraction_failed_at": datetime.now(UTC).isoformat(),
        }

    metadata: dict[str, object] = {
        "source_extraction_enqueue_status": result.status,
        "source_extraction_updated_at": datetime.now(UTC).isoformat(),
    }
    if result.job_ids:
        metadata["source_extraction_job_ids"] = list(result.job_ids)
    if result.reason:
        metadata["source_extraction_reason"] = result.reason
    if result.status in {"queued", "partial"}:
        metadata["source_extraction_state"] = "queued"
    elif result.reason == "disabled":
        metadata["source_extraction_state"] = "disabled"
    elif result.reason == "no_projectable_sources":
        metadata["source_extraction_state"] = "not_projectable"
    else:
        metadata["source_extraction_state"] = "failed"
    return metadata


def _memory_extraction_source_payload(
    memory: RawMemory,
    document: CrawledDocumentRecord,
    *,
    entity_id: str,
) -> dict[str, object]:
    metadata = {
        **dict(memory.metadata),
        "raw_memory_id": memory.id,
        "source_id": memory.source_id,
        "document_id": str(document.id),
        "memory_scope": memory.memory_scope.value,
        "scope_key": memory.scope_key,
        "principal_id": memory.principal_id,
        "capture_surface": memory.capture_surface,
    }
    return {
        "id": entity_id,
        "entity_type": EntityType.DOCUMENT.value,
        "name": document.title or memory.title or entity_id,
        "description": document.title or "",
        "content": memory.raw_content,
        "organization_id": memory.organization_id,
        "created_by": memory.created_by_user_id or memory.principal_id,
        "created_by_user_id": memory.created_by_user_id or memory.principal_id,
        "principal_id": memory.principal_id,
        "metadata": metadata,
    }


async def _mark_skipped(memory: RawMemory, status: str) -> None:
    metadata = dict(memory.metadata)
    metadata["raw_promotion_state"] = status
    metadata["raw_promotion_skipped_at"] = datetime.now(UTC).isoformat()
    memory.metadata = metadata
    await save_raw_memory(memory)


async def _mark_failed(memory: RawMemory, *, error: str) -> None:
    metadata = dict(memory.metadata)
    metadata["raw_promotion_state"] = "failed"
    metadata["raw_promotion_error"] = error
    metadata["raw_promotion_failed_at"] = datetime.now(UTC).isoformat()
    memory.metadata = metadata
    await save_raw_memory(memory)


__all__ = ["promote_raw_captures"]
