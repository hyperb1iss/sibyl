"""Promote raw captures into chunked documents and graph anchors."""

from __future__ import annotations

import hashlib
import time
from datetime import UTC, datetime
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

import structlog

from sibyl.config import settings
from sibyl.crawler.chunker import ChunkStrategy, DocumentChunker
from sibyl.crawler.embedder import EmbeddingService
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
from sibyl_core.models.entities import Entity, EntityType
from sibyl_core.observability import elapsed_ms
from sibyl_core.services.surreal_content import (
    MemoryScope,
    RawMemory,
    list_raw_memories_for_promotion,
    raw_memory_recallable,
    save_raw_memory,
)

log = structlog.get_logger()


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
        "skipped_scoped_count": 0,
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
        elif outcome["status"] == "skipped_scoped":
            result["skipped_scoped_count"] += 1
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
        if skip_status in {"skipped_deleted", "skipped_superseded", "skipped_scoped"}:
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
    if not settings.auto_extract_entities:
        metadata["source_extraction_state"] = "disabled"
    memory.metadata = metadata
    memory.entity_id = entity_id
    await save_raw_memory(memory)
    return {
        "status": "promoted",
        "document_id": str(stored_document.id),
        "chunk_count": len(saved_chunks),
        "entity_id": entity_id,
    }


def _organization_visible_for_promotion(memory: RawMemory) -> bool:
    return memory.memory_scope in {MemoryScope.ORGANIZATION, MemoryScope.PUBLIC}


def _scope_metadata(memory: RawMemory) -> dict[str, str]:
    metadata = {
        "memory_scope": memory.memory_scope.value,
        "principal_id": memory.principal_id,
    }
    if memory.scope_key is not None:
        metadata["scope_key"] = memory.scope_key
    if memory.project_id is not None:
        metadata["project_id"] = memory.project_id
    return metadata


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
    if not _organization_visible_for_promotion(memory):
        return "skipped_scoped"
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
                **_scope_metadata(memory),
                "raw_memory_id": memory.id,
                "source_id": memory.source_id,
                "document_id": str(document.id),
                "chunk_count": chunk_count,
            },
        ),
        generate_embedding=False,
    )
    return entity_id


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
