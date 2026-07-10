"""Surreal-backed content helpers shared by core services."""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
from collections.abc import AsyncIterator, Iterable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Protocol, cast
from uuid import uuid4

import structlog

from sibyl_core.backends.surreal import SurrealContentClient
from sibyl_core.backends.surreal.fulltext import build_fulltext_query
from sibyl_core.config import settings
from sibyl_core.embeddings.providers import (
    DeterministicEmbeddingProvider,
    EmbeddingMetadata,
    EmbeddingProvider,
    EmbeddingProviderName,
    create_embedding_provider,
)
from sibyl_core.memory_pipeline.lifecycle import raw_memory_lifecycle_recallable
from sibyl_core.memory_pipeline.retrieval import CandidateSourceFailure, CandidateSourceResult
from sibyl_core.models.memory_scope import MemoryScope
from sibyl_core.models.reflection import (
    MemoryLifecycle,
    MemoryLifecycleState,
    ReflectionCandidate,
    with_memory_lifecycle_metadata,
)
from sibyl_core.utils.resilience import with_timeout

_DEFAULT_BATCH_SIZE = 128
_DIRECT_SEARCH_QUERY_TIMEOUT_SECONDS = 3.0
_LIFECYCLE_FILTER_OVERFETCH_FACTOR = 4
_REFLECTION_DREAM_EXCLUDED_CAPTURE_SURFACES = frozenset(
    {
        "reflection",
        "reflection_candidate",
        "reflection_source",
        "synthesis_artifact",
    }
)
_RAW_MEMORY_EMBEDDING_TEXT_MAX_CHARS = 12_000
_RAW_MEMORY_EMBEDDING_TEXT_TRUNCATION_MARKER = "\n...[truncated for raw memory embedding]..."
_RAW_MEMORY_EMBEDDING_TEXT_VERSION = "raw-capture-v1"
_RAW_MEMORY_EMBEDDING_AUTO = object()
_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_]+")
_MARK_OPEN = "<mark>"
_MARK_CLOSE = "</mark>"
_SNIPPET_MAX_CHARS = 320
_EMBEDDED_SURREAL_SCHEMES = ("memory://", "surrealkv://", "rocksdb://", "file://")
log = structlog.get_logger()
type _RawMemoryProviderCacheKey = tuple[EmbeddingProviderName, str, int, str]
_raw_memory_embedding_provider: EmbeddingProvider | None = None
_raw_memory_embedding_fingerprint: _RawMemoryProviderCacheKey | None = None


@dataclass(slots=True)
class _SharedContentClientState:
    client: SurrealContentClient | None = None


_shared_content_client_state = _SharedContentClientState()
_shared_content_client_lock = asyncio.Lock()
_UPSERT_RECORD = {
    "crawl_sources": (
        "UPSERT crawl_sources CONTENT $record "
        "WHERE uuid = $uuid AND organization_id = $organization_id;"
    ),
    "crawled_documents": (
        "UPSERT crawled_documents CONTENT $record "
        "WHERE uuid = $uuid AND organization_id = $organization_id;"
    ),
    "raw_captures": (
        "UPSERT raw_captures CONTENT $record "
        "WHERE uuid = $uuid AND organization_id = $organization_id;"
    ),
}
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
    provenance = $input.provenance,
    capture_surface = $input.capture_surface,
    created_by_user_id = $input.created_by_user_id,
    captured_at = $input.captured_at,
    deleted_at = $input.deleted_at,
    purge_after = $input.purge_after,
    last_recalled_at = $input.last_recalled_at ?? last_recalled_at,
    last_used_at = $input.last_used_at ?? last_used_at,
    retrieval_count = $input.retrieval_count ?? retrieval_count ?? 0,
    citation_count = $input.citation_count ?? citation_count ?? 0,
    created_at = $input.created_at;
"""
_RAW_MEMORY_RECALL_FIELDS = ", ".join(
    (
        "id AS record_id",
        "uuid",
        "organization_id",
        "source_id",
        "principal_id",
        "memory_scope",
        "scope_key",
        "agent_id",
        "project_id",
        "review_state",
        "entity_id",
        "entity_type",
        "title",
        "raw_content",
        "tags",
        "metadata",
        "provenance",
        "capture_surface",
        "created_by_user_id",
        "captured_at",
        "deleted_at",
        "purge_after",
        "last_recalled_at",
        "last_used_at",
        "retrieval_count",
        "citation_count",
        "created_at",
    )
)
_DOCUMENT_CHUNK_SELECT = (
    "uuid, organization_id, source_id, document_id, chunk_index, chunk_type, content, context, "
    "heading_path, language, has_entities, entity_ids"
)
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
        AND metadata.supersedes_raw_memory_id != NONE
        AND metadata.supersedes_raw_memory_id != ''
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
AGENT_DIARY_CAPTURE_SURFACE = "agent_diary"
type SurrealRecord = dict[str, object]


class RawExecuteQuery(Protocol):
    async def __call__(self, query: str, **params: object) -> object: ...


_SCOPES_REQUIRING_SCOPE_KEY = {
    MemoryScope.DELEGATED,
    MemoryScope.PROJECT,
    MemoryScope.TEAM,
    MemoryScope.SHARED,
}
_RAW_PROMOTION_VISIBLE_SCOPES = (
    MemoryScope.ORGANIZATION.value,
    MemoryScope.PUBLIC.value,
)


@dataclass(slots=True)
class ContentSource:
    id: str
    organization_id: str
    name: str
    url: str
    source_type: str = "website"
    description: str | None = None
    crawl_depth: int = 2
    include_patterns: list[str] = field(default_factory=list)
    exclude_patterns: list[str] = field(default_factory=list)
    crawl_status: str = "pending"
    current_job_id: str | None = None
    last_error: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(slots=True)
class ContentDocument:
    id: str
    source_id: str
    url: str
    organization_id: str = ""
    title: str = ""
    content: str = ""
    has_code: bool = False


@dataclass(slots=True)
class ContentChunk:
    id: str
    document_id: str
    organization_id: str = ""
    source_id: str = ""
    chunk_index: int = 0
    chunk_type: str = "text"
    content: str = ""
    context: str | None = None
    heading_path: list[str] = field(default_factory=list)
    language: str | None = None
    embedding: list[float] | None = None
    has_entities: bool = False
    entity_ids: list[str] = field(default_factory=list)
    snippet: str | None = None


ContentSearchRow = tuple[ContentChunk, ContentDocument, str, str, float]


@dataclass(slots=True)
class RawMemory:
    id: str
    organization_id: str
    source_id: str
    principal_id: str
    memory_scope: MemoryScope = MemoryScope.PRIVATE
    scope_key: str | None = None
    agent_id: str | None = None
    project_id: str | None = None
    review_state: str = "pending"
    entity_id: str | None = None
    entity_type: str = "raw_memory"
    title: str = ""
    raw_content: str = ""
    tags: list[str] = field(default_factory=list)
    embedding: list[float] | None = None
    metadata: dict[str, object] = field(default_factory=dict)
    provenance: dict[str, object] = field(default_factory=dict)
    capture_surface: str | None = None
    created_by_user_id: str | None = None
    captured_at: datetime | None = None
    deleted_at: datetime | None = None
    purge_after: datetime | None = None
    last_recalled_at: datetime | None = None
    last_used_at: datetime | None = None
    retrieval_count: int | None = None
    citation_count: int | None = None
    created_at: datetime | None = None
    score: float = 0.0
    snippet: str | None = None


@dataclass(frozen=True, slots=True)
class RawMemoryRecallResult:
    memories: tuple[RawMemory, ...]
    sources: tuple[CandidateSourceResult[RawMemory], ...] = ()

    @property
    def failures(self) -> tuple[CandidateSourceFailure, ...]:
        failures: list[CandidateSourceFailure] = []
        for source in self.sources:
            if source.failure is not None:
                failures.append(source.failure)
        return tuple(failures)

    @property
    def degraded(self) -> bool:
        return bool(self.failures)

    def as_metadata(self) -> dict[str, object]:
        failures = [failure.as_metadata() for failure in self.failures]
        metadata: dict[str, object] = {
            "raw_recall_degraded": bool(failures),
            "raw_recall_failure_count": len(failures),
        }
        if failures:
            metadata["raw_recall_failures"] = failures
        return metadata


@dataclass(frozen=True, slots=True)
class RawMemoryWrite:
    organization_id: str
    principal_id: str
    source_id: str
    raw_content: str
    title: str = ""
    memory_scope: MemoryScope | str = MemoryScope.PRIVATE
    scope_key: str | None = None
    tags: Sequence[str] | None = None
    metadata: Mapping[str, object] | None = None
    provenance: Mapping[str, object] | None = None
    capture_surface: str | None = None
    entity_type: str = "raw_memory"


@dataclass(frozen=True, slots=True)
class ContentLineageBackfillResult:
    derived_from: int = 0
    chunk_of: int = 0
    supersedes: int = 0
    extracted_into: int = 0


@dataclass(frozen=True, slots=True)
class _RawMemoryRecallFilters:
    source_ids: tuple[str, ...] = ()
    participants: tuple[str, ...] = ()
    labels: tuple[str, ...] = ()
    thread_id: str | None = None
    occurred_after: str | None = None
    occurred_before: str | None = None
    as_of: datetime | None = None
    as_of_text: str | None = None


def raw_memory_recallable(memory: RawMemory) -> bool:
    return raw_memory_lifecycle_recallable(memory)


def raw_memory_currently_recallable(memory: RawMemory) -> bool:
    return raw_memory_recallable(memory) and _raw_memory_matches_as_of(memory, datetime.now(UTC))


def _raw_memory_capture_surface(memory: RawMemory) -> str:
    metadata_surface = memory.metadata.get("capture_surface")
    value = memory.capture_surface if memory.capture_surface is not None else metadata_surface
    return str(value or "").strip().lower()


def _normalize_raw_temporal_datetime(value: object | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _raw_memory_matches_as_of(memory: RawMemory, as_of: datetime | None) -> bool:
    if as_of is None:
        return True
    for value in (memory.created_at, memory.captured_at):
        observed_at = _normalize_raw_temporal_datetime(value)
        if observed_at is not None and observed_at > as_of:
            return False
    for key in ("valid_at", "valid_from"):
        valid_at = _normalize_raw_temporal_datetime(memory.metadata.get(key))
        if valid_at is not None and valid_at > as_of:
            return False
    for key in ("invalid_at", "valid_to"):
        invalid_at = _normalize_raw_temporal_datetime(memory.metadata.get(key))
        if invalid_at is not None and invalid_at <= as_of:
            return False
    return True


def _recallable_memories(
    memories: list[RawMemory],
    *,
    limit: int,
    as_of: datetime | None = None,
) -> list[RawMemory]:
    return [
        memory
        for memory in memories
        if raw_memory_recallable(memory) and _raw_memory_matches_as_of(memory, as_of)
    ][:limit]


def build_surreal_content_client() -> SurrealContentClient:
    return SurrealContentClient(
        url=settings.resolved_surreal_url,
        username=settings.surreal_username,
        password=settings.surreal_password.get_secret_value(),
        token=settings.surreal_token.get_secret_value(),
        pool_size=settings.surreal_client_pool_size("content"),
    )


async def get_shared_surreal_content_client() -> SurrealContentClient:
    if _shared_content_client_state.client is not None:
        return _shared_content_client_state.client

    async with _shared_content_client_lock:
        if _shared_content_client_state.client is None:
            _shared_content_client_state.client = build_surreal_content_client()
        return _shared_content_client_state.client


async def close_shared_surreal_content_client() -> None:
    async with _shared_content_client_lock:
        client = _shared_content_client_state.client
        _shared_content_client_state.client = None
        if client is not None:
            await client.close()


@asynccontextmanager
async def surreal_content_client() -> AsyncIterator[SurrealContentClient]:
    yield await get_shared_surreal_content_client()


def _normalize_record(record: object) -> SurrealRecord | None:
    if not isinstance(record, dict):
        return None
    out = {str(key): value for key, value in record.items()}
    if "result" in out and ("status" in out or "time" in out):
        return None
    out.pop("id", None)
    return out


def _normalize_records(result: object) -> list[SurrealRecord]:
    if result is None:
        return []
    if isinstance(result, dict):
        payload = {str(key): value for key, value in result.items()}
        if "result" in payload and ("status" in payload or "time" in payload):
            return _normalize_records(payload.get("result"))
        record = _normalize_record(payload)
        return [record] if record is not None else []
    if not isinstance(result, list):
        return []

    records: list[SurrealRecord] = []
    for item in result:
        records.extend(_normalize_records(item))
    return records


def _normalize_records_preserving_id(result: object) -> list[SurrealRecord]:
    if result is None:
        return []
    if isinstance(result, dict):
        payload = {str(key): value for key, value in result.items()}
        if "result" in payload and ("status" in payload or "time" in payload):
            return _normalize_records_preserving_id(payload.get("result"))
        statements = payload.get("result")
        if (
            "status" not in payload
            and isinstance(statements, list)
            and statements
            and all(isinstance(statement, dict) for statement in statements)
        ):
            return _normalize_records_preserving_id(statements[-1])
        return [payload]
    if not isinstance(result, list):
        return []

    records: list[SurrealRecord] = []
    for item in result:
        records.extend(_normalize_records_preserving_id(item))
    return records


def _query_error(result: object) -> str | None:
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        payload = {str(key): value for key, value in result.items()}
        if (
            "result" in payload
            and "status" not in payload
            and isinstance(payload.get("result"), list)
        ):
            return _query_error(payload["result"])
        status = payload.get("status")
        if isinstance(status, str) and status.upper() == "ERR":
            detail = payload.get("detail") or payload.get("result") or payload
            return str(detail)
        return None
    if not isinstance(result, list):
        return None
    for item in result:
        error = _query_error(item)
        if error is not None:
            return error
    return None


def _coerce_str(value: object | None, *, default: str = "") -> str:
    return str(value) if value is not None else default


def _coerce_optional_str(value: object | None) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None


def _coerce_highlight_value(value: object | None) -> str:
    if value is None:
        return ""
    if isinstance(value, list | tuple):
        return " ".join(str(item) for item in value if item is not None)
    return str(value)


def _trim_search_snippet(text: str, *, max_chars: int = _SNIPPET_MAX_CHARS) -> str:
    if len(text) <= max_chars:
        return text

    mark_index = text.find(_MARK_OPEN)
    if mark_index < 0:
        return text[:max_chars].rstrip() + "..."

    window_start = max(mark_index - max_chars // 3, 0)
    window_end = min(window_start + max_chars, len(text))
    mark_close = text.find(_MARK_CLOSE, mark_index)
    if mark_close >= 0:
        window_end = max(window_end, min(mark_close + len(_MARK_CLOSE), len(text)))

    snippet = text[window_start:window_end].strip()
    if window_start > 0:
        snippet = "..." + snippet.lstrip()
    if window_end < len(text):
        snippet = snippet.rstrip() + "..."
    return snippet


def _search_snippet(
    value: object | None,
    *,
    fallback: object | None = None,
    max_chars: int = _SNIPPET_MAX_CHARS,
) -> str | None:
    highlighted = _coerce_highlight_value(value)
    text = highlighted or _coerce_highlight_value(fallback)
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return None
    return _trim_search_snippet(text, max_chars=max_chars)


def _search_snippet_from_values(
    values: Iterable[object | None],
    *,
    fallback: object | None = None,
    max_chars: int = _SNIPPET_MAX_CHARS,
) -> str | None:
    first_text: object | None = None
    for value in values:
        text = _coerce_highlight_value(value)
        if not text.strip():
            continue
        first_text = first_text or value
        if _MARK_OPEN in text:
            return _search_snippet(value, max_chars=max_chars)
    return _search_snippet(first_text, fallback=fallback, max_chars=max_chars)


def _coerce_int(value: object | None, *, default: int = 0) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str) and value:
        try:
            return int(value)
        except ValueError:
            return default
    return default


def _coerce_bool(value: object | None, *, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "on"}:
            return True
        if lowered in {"false", "0", "no", "off", ""}:
            return False
    return default


def _coerce_datetime(value: object | None) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    if isinstance(value, str):
        normalized = value.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is not None:
            return parsed.astimezone(UTC).replace(tzinfo=None)
        return parsed
    return None


def _coerce_dict(value: object | None) -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    return {str(key): item for key, item in value.items()}


def _coerce_float(value: object | None, *, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str) and value:
        try:
            return float(value)
        except ValueError:
            return default
    return default


def _coerce_str_list(value: object | None) -> list[str]:
    if isinstance(value, list | tuple):
        return [str(item) for item in value if item is not None]
    return []


def _coerce_float_list(value: object | None) -> list[float] | None:
    if not isinstance(value, list | tuple):
        return None
    out: list[float] = []
    for item in value:
        if isinstance(item, bool):
            out.append(float(item))
            continue
        if isinstance(item, int | float):
            out.append(float(item))
            continue
        if isinstance(item, str) and item:
            try:
                out.append(float(item))
            except ValueError:
                return None
            continue
        return None
    return out


def _embedding_vector_from_batch(
    embeddings: Iterable[Iterable[float]],
    dimensions: int,
) -> list[float]:
    first = next(iter(embeddings), None)
    if first is None:
        raise ValueError("embedding provider returned no vectors")
    embedding = [float(value) for value in first]
    if len(embedding) != dimensions:
        raise ValueError(
            f"embedding provider returned {len(embedding)} dimensions, expected {dimensions}"
        )
    return embedding


def raw_memory_embedding_text(
    *,
    title: str,
    raw_content: str,
    max_chars: int = _RAW_MEMORY_EMBEDDING_TEXT_MAX_CHARS,
) -> str:
    title_text = title.strip()
    content_text = raw_content.strip()
    sections: list[str] = []
    if title_text:
        sections.append(f"Title: {title_text}")
    if content_text:
        sections.append(content_text)
    text = "\n\n".join(sections).strip() or "[empty]"
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    marker = _RAW_MEMORY_EMBEDDING_TEXT_TRUNCATION_MARKER
    if max_chars <= len(marker):
        return text[:max_chars]
    return f"{text[: max_chars - len(marker)]}{marker}"


def _raw_memory_embedding_metadata(metadata: EmbeddingMetadata) -> dict[str, str | int | bool]:
    payload = metadata.to_dict()
    payload["text_version"] = _RAW_MEMORY_EMBEDDING_TEXT_VERSION
    return payload


def reset_raw_memory_embedding_provider_cache() -> None:
    global _raw_memory_embedding_fingerprint, _raw_memory_embedding_provider
    _raw_memory_embedding_provider = None
    _raw_memory_embedding_fingerprint = None


def _configured_raw_memory_embedding_provider() -> EmbeddingProvider | None:
    global _raw_memory_embedding_fingerprint, _raw_memory_embedding_provider
    dimensions = _raw_memory_embedding_dimensions()
    if os.getenv("SIBYL_MOCK_LLM", "").strip().lower() in {"1", "true", "yes", "on"}:
        return DeterministicEmbeddingProvider(
            EmbeddingMetadata(
                provider="deterministic",
                model="mock-llm-v1",
                dimensions=dimensions,
                cache_namespace="raw-memory-mock",
                tokenizer_estimate_method="sha256",
                text_version=_RAW_MEMORY_EMBEDDING_TEXT_VERSION,
            )
        )

    provider_name = _raw_memory_embedding_provider_name()
    model = _raw_memory_embedding_model(provider_name)
    api_key = _raw_memory_embedding_api_key(provider_name)
    if not api_key:
        return None

    fingerprint: _RawMemoryProviderCacheKey = (
        provider_name,
        model,
        dimensions,
        hashlib.sha256(api_key.encode()).hexdigest(),
    )
    if _raw_memory_embedding_provider is None or fingerprint != _raw_memory_embedding_fingerprint:
        _raw_memory_embedding_provider = create_embedding_provider(
            provider=provider_name,
            model=model,
            dimensions=dimensions,
            cache_namespace="raw-memory",
            api_key=api_key,
            max_cache_size=2000,
            tokenizer_estimate_method="provider-default",
        )
        _raw_memory_embedding_fingerprint = fingerprint
    return _raw_memory_embedding_provider


def _raw_memory_embedding_provider_name() -> EmbeddingProviderName:
    provider = (os.getenv("SIBYL_EMBEDDING_PROVIDER") or settings.embedding_provider).strip()
    if provider == "openai":
        return "openai"
    if provider == "gemini":
        return "gemini"
    raise ValueError(f"unsupported raw memory embedding provider: {provider}")


def _raw_memory_embedding_model(provider: EmbeddingProviderName) -> str:
    model = os.getenv("SIBYL_EMBEDDING_MODEL", "").strip()
    if model:
        return model
    if provider == "gemini" and settings.embedding_model == "text-embedding-3-small":
        return "gemini-embedding-2"
    return settings.embedding_model


def _raw_memory_embedding_dimensions() -> int:
    dimensions = os.getenv("SIBYL_EMBEDDING_DIMENSIONS", "").strip()
    if dimensions:
        return int(dimensions)
    return settings.embedding_dimensions


def _raw_memory_embedding_api_key(provider: EmbeddingProviderName) -> str | None:
    if provider == "gemini":
        return (
            os.getenv("SIBYL_GEMINI_API_KEY", "")
            or os.getenv("GEMINI_API_KEY", "")
            or os.getenv("GOOGLE_API_KEY", "")
            or settings.gemini_api_key.get_secret_value()
            or None
        )
    return (
        os.getenv("SIBYL_OPENAI_API_KEY", "")
        or os.getenv("OPENAI_API_KEY", "")
        or settings.openai_api_key.get_secret_value()
        or None
    )


def _coerce_memory_scope(value: object | None) -> MemoryScope:
    if isinstance(value, MemoryScope):
        return value
    if value is None:
        return MemoryScope.PRIVATE
    try:
        return MemoryScope(str(value))
    except ValueError:
        return MemoryScope.PRIVATE


def _validate_raw_memory_scope(memory_scope: MemoryScope, scope_key: str | None) -> None:
    if memory_scope in _SCOPES_REQUIRING_SCOPE_KEY and not scope_key:
        msg = f"{memory_scope.value} raw memory requires a scope_key"
        raise ValueError(msg)


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _source_from_record(record: Mapping[str, object]) -> ContentSource:
    return ContentSource(
        id=_coerce_str(record.get("uuid")),
        organization_id=_coerce_str(record.get("organization_id")),
        name=_coerce_str(record.get("name")),
        url=_coerce_str(record.get("url")),
        source_type=_coerce_str(record.get("source_type"), default="website"),
        description=_coerce_optional_str(record.get("description")),
        crawl_depth=_coerce_int(record.get("crawl_depth"), default=2),
        include_patterns=_coerce_str_list(record.get("include_patterns")),
        exclude_patterns=_coerce_str_list(record.get("exclude_patterns")),
        crawl_status=_coerce_str(record.get("crawl_status"), default="pending"),
        current_job_id=_coerce_optional_str(record.get("current_job_id")),
        last_error=_coerce_optional_str(record.get("last_error")),
        created_at=_coerce_datetime(record.get("created_at")),
        updated_at=_coerce_datetime(record.get("updated_at")),
    )


def _document_from_record(record: Mapping[str, object]) -> ContentDocument:
    return ContentDocument(
        id=_coerce_str(record.get("uuid")),
        source_id=_coerce_str(record.get("source_id")),
        url=_coerce_str(record.get("url")),
        organization_id=_coerce_str(record.get("organization_id")),
        title=_coerce_str(record.get("title")),
        content=_coerce_str(record.get("content")),
        has_code=_coerce_bool(record.get("has_code")),
    )


def _chunk_from_record(record: Mapping[str, object]) -> ContentChunk:
    return ContentChunk(
        id=_coerce_str(record.get("uuid")),
        document_id=_coerce_str(record.get("document_id")),
        organization_id=_coerce_str(record.get("organization_id")),
        source_id=_coerce_str(record.get("source_id")),
        chunk_index=_coerce_int(record.get("chunk_index")),
        chunk_type=_coerce_str(record.get("chunk_type"), default="text"),
        content=_coerce_str(record.get("content")),
        context=_coerce_optional_str(record.get("context")),
        heading_path=_coerce_str_list(record.get("heading_path")),
        language=_coerce_optional_str(record.get("language")),
        embedding=_coerce_float_list(record.get("embedding")),
        has_entities=_coerce_bool(record.get("has_entities")),
        entity_ids=_coerce_str_list(record.get("entity_ids")),
        snippet=_search_snippet(record.get("snippet"), fallback=record.get("content")),
    )


def _raw_memory_from_record(record: Mapping[str, object]) -> RawMemory:
    metadata = _coerce_dict(record.get("metadata"))
    return RawMemory(
        id=_coerce_str(record.get("uuid")),
        organization_id=_coerce_str(record.get("organization_id")),
        source_id=_coerce_str(record.get("source_id")),
        principal_id=_coerce_str(record.get("principal_id")),
        memory_scope=_coerce_memory_scope(record.get("memory_scope")),
        scope_key=_coerce_optional_str(record.get("scope_key")),
        agent_id=_coerce_optional_str(record.get("agent_id"))
        or _coerce_optional_str(metadata.get("agent_id")),
        project_id=_coerce_optional_str(record.get("project_id"))
        or _coerce_optional_str(metadata.get("project_id")),
        review_state=_coerce_str(
            record.get("review_state") or metadata.get("review_state"), default="pending"
        ),
        entity_id=_coerce_optional_str(record.get("entity_id")),
        entity_type=_coerce_str(record.get("entity_type"), default="raw_memory"),
        title=_coerce_str(record.get("title")),
        raw_content=_coerce_str(record.get("raw_content")),
        tags=_coerce_str_list(record.get("tags")),
        embedding=_coerce_float_list(record.get("embedding")),
        metadata=metadata,
        provenance=_coerce_dict(record.get("provenance")),
        capture_surface=_coerce_optional_str(record.get("capture_surface")),
        created_by_user_id=_coerce_optional_str(record.get("created_by_user_id")),
        captured_at=_coerce_datetime(record.get("captured_at")),
        deleted_at=_coerce_datetime(record.get("deleted_at")),
        purge_after=_coerce_datetime(record.get("purge_after")),
        last_recalled_at=_coerce_datetime(record.get("last_recalled_at")),
        last_used_at=_coerce_datetime(record.get("last_used_at")),
        retrieval_count=_coerce_int(record.get("retrieval_count")),
        citation_count=_coerce_int(record.get("citation_count")),
        created_at=_coerce_datetime(record.get("created_at")),
        score=_coerce_float(record.get("score")),
        snippet=_search_snippet_from_values(
            (
                record.get("content_snippet"),
                record.get("title_snippet"),
                record.get("snippet"),
            ),
            fallback=record.get("raw_content") or record.get("title"),
        ),
    )


def _source_record(source: ContentSource) -> SurrealRecord:
    return {
        "uuid": source.id,
        "organization_id": source.organization_id,
        "name": source.name,
        "url": source.url,
        "source_type": source.source_type,
        "description": source.description,
        "crawl_depth": source.crawl_depth,
        "include_patterns": list(source.include_patterns),
        "exclude_patterns": list(source.exclude_patterns),
        "crawl_status": source.crawl_status,
        "current_job_id": source.current_job_id,
        "last_error": source.last_error,
        "created_at": source.created_at,
        "updated_at": source.updated_at,
    }


def _raw_memory_record(memory: RawMemory) -> SurrealRecord:
    metadata = dict(memory.metadata)
    record: SurrealRecord = {
        "uuid": memory.id,
        "organization_id": memory.organization_id,
        "source_id": memory.source_id,
        "principal_id": memory.principal_id,
        "memory_scope": memory.memory_scope.value,
        "scope_key": memory.scope_key,
        "agent_id": memory.agent_id,
        "project_id": memory.project_id,
        "review_state": memory.review_state,
        "entity_id": memory.entity_id,
        "title": memory.title,
        "raw_content": memory.raw_content,
        "entity_type": memory.entity_type,
        "tags": list(memory.tags),
        "embedding": list(memory.embedding) if memory.embedding is not None else None,
        "metadata": metadata,
        "provenance": dict(memory.provenance),
        "capture_surface": memory.capture_surface,
        "created_by_user_id": memory.created_by_user_id or memory.principal_id,
        "captured_at": memory.captured_at,
        "deleted_at": memory.deleted_at,
        "purge_after": memory.purge_after,
        "created_at": memory.created_at,
    }
    if memory.last_recalled_at is not None:
        record["last_recalled_at"] = memory.last_recalled_at
        metadata["last_recalled_at"] = memory.last_recalled_at
    if memory.last_used_at is not None:
        record["last_used_at"] = memory.last_used_at
        metadata["last_used_at"] = memory.last_used_at
    if memory.retrieval_count is not None:
        record["retrieval_count"] = memory.retrieval_count
        metadata["retrieval_count"] = memory.retrieval_count
    if memory.citation_count is not None:
        record["citation_count"] = memory.citation_count
        metadata["citation_count"] = memory.citation_count
    return record


async def _select_many(
    client: SurrealContentClient, query: str, **params: object
) -> list[SurrealRecord]:
    result = await client.execute_query(query, **params)
    error = _query_error(result)
    if error is not None:
        raise RuntimeError(error)
    return _normalize_records(result)


def _normalize_raw_statement_records(
    result: object, *, statement_index: int
) -> list[SurrealRecord]:
    if isinstance(result, dict):
        payload = {str(key): value for key, value in result.items()}
        statements = payload.get("result")
        if (
            "status" not in payload
            and isinstance(statements, list)
            and statements
            and all(isinstance(statement, dict) for statement in statements)
        ):
            return _normalize_records(statements[statement_index])
    return _normalize_records(result)


async def _select_many_raw(
    client: SurrealContentClient,
    query: str,
    **params: object,
) -> list[SurrealRecord]:
    execute_query_raw = getattr(client, "execute_query_raw", None)
    if callable(execute_query_raw):
        result = await cast("RawExecuteQuery", execute_query_raw)(query, **params)
    else:
        result = await client.execute_query(query, **params)
    error = _query_error(result)
    if error is not None:
        raise RuntimeError(error)
    return _normalize_raw_statement_records(result, statement_index=-1)


async def _select_one(
    client: SurrealContentClient, query: str, **params: object
) -> SurrealRecord | None:
    rows = await _select_many(client, query, **params)
    return rows[0] if rows else None


def _lineage_edge_id(prefix: str, organization_id: str, *parts: str) -> str:
    digest = hashlib.sha256(":".join((organization_id, *parts)).encode()).hexdigest()
    return f"{prefix}_{digest}"


def _lineage_record_ref(table: str, record_id: str) -> str:
    escaped = record_id.replace("\\", "\\\\").replace("`", "\\`")
    return f"{table}:`{escaped}`"


def _lineage_edge_batches(
    edges: Sequence[SurrealRecord],
    *,
    batch_size: int = _DEFAULT_BATCH_SIZE,
) -> list[list[SurrealRecord]]:
    return [list(edges[index : index + batch_size]) for index in range(0, len(edges), batch_size)]


async def _existing_lineage_edge_ids(
    client: SurrealContentClient,
    table: str,
    edge_ids: Sequence[str],
) -> set[str]:
    existing: set[str] = set()
    for batch in _value_batches(edge_ids):
        rows = await _select_many(
            client,
            f"SELECT uuid FROM {table} WHERE uuid INSIDE $edge_ids;",
            edge_ids=batch,
        )
        existing.update(_coerce_str(row.get("uuid")) for row in rows)
    return existing


async def _existing_content_uuids(
    client: SurrealContentClient,
    table: str,
    *,
    organization_id: str,
    values: Sequence[str],
) -> set[str]:
    existing: set[str] = set()
    for batch in _value_batches(values):
        rows = await _select_many(
            client,
            f"""
            SELECT uuid FROM {table}
            WHERE organization_id = $organization_id
                AND uuid INSIDE $values;
            """,
            organization_id=organization_id,
            values=batch,
        )
        existing.update(_coerce_str(row.get("uuid")) for row in rows)
    return existing


async def _pending_lineage_edges(
    client: SurrealContentClient,
    table: str,
    edges: Sequence[SurrealRecord],
) -> list[SurrealRecord]:
    deduped: list[SurrealRecord] = []
    seen: set[str] = set()
    for edge in edges:
        edge_id = _coerce_str(edge.get("uuid"))
        if not edge_id or edge_id in seen:
            continue
        seen.add(edge_id)
        deduped.append(edge)
    existing = await _existing_lineage_edge_ids(
        client,
        table,
        [_coerce_str(edge.get("uuid")) for edge in deduped],
    )
    return [edge for edge in deduped if _coerce_str(edge.get("uuid")) not in existing]


async def _write_lineage_edges(
    client: SurrealContentClient,
    query: str,
    *,
    edges: Sequence[SurrealRecord],
    organization_id: str,
) -> None:
    for batch in _lineage_edge_batches(edges):
        await _select_many_raw(
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
    rows = await _select_many(
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
    return _coerce_int(rows[0].get("total", rows[0].get("count")))


async def _materialize_derived_from_lineage(
    client: SurrealContentClient,
    *,
    organization_id: str,
    limit: int,
) -> int:
    remaining = limit
    offset = 0
    page_size = min(max(limit, 1), _DEFAULT_BATCH_SIZE)
    while remaining > 0:
        rows = await _select_many(
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
            source_import_id = _coerce_str(row.get("uuid"))
            for raw_memory_id in _coerce_str_list(row.get("raw_memory_ids")):
                edge_uuid = _lineage_edge_id(
                    "derived_from",
                    organization_id,
                    raw_memory_id,
                    source_import_id,
                )
                edges.append(
                    {
                        "uuid": edge_uuid,
                        "edge_ref": _lineage_record_ref("derived_from", edge_uuid),
                        "organization_id": organization_id,
                        "raw_memory_id": raw_memory_id,
                        "source_import_id": source_import_id,
                        "source_id": None,
                        "created_at": _utcnow(),
                    }
                )
        existing_raw_ids = await _existing_content_uuids(
            client,
            "raw_captures",
            organization_id=organization_id,
            values=[_coerce_str(edge.get("raw_memory_id")) for edge in edges],
        )
        edges = [
            edge for edge in edges if _coerce_str(edge.get("raw_memory_id")) in existing_raw_ids
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
    page_size = min(max(limit, 1), _DEFAULT_BATCH_SIZE)
    while remaining > 0:
        rows = await _select_many(
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
                "edge_ref": _lineage_record_ref("chunk_of", edge_uuid),
                "organization_id": organization_id,
                "chunk_id": _coerce_str(row.get("uuid")),
                "document_id": _coerce_str(row.get("document_id")),
                "source_id": _coerce_optional_str(row.get("source_id")),
                "created_at": _utcnow(),
            }
            for row in rows
            for edge_uuid in [
                _lineage_edge_id(
                    "chunk_of",
                    organization_id,
                    _coerce_str(row.get("uuid")),
                    _coerce_str(row.get("document_id")),
                )
            ]
        ]
        existing_document_ids = await _existing_content_uuids(
            client,
            "crawled_documents",
            organization_id=organization_id,
            values=[_coerce_str(edge.get("document_id")) for edge in edges],
        )
        edges = [
            edge for edge in edges if _coerce_str(edge.get("document_id")) in existing_document_ids
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
    page_size = min(max(limit, 1), _DEFAULT_BATCH_SIZE)
    while remaining > 0:
        rows = await _select_many(
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
            metadata = _coerce_dict(row.get("metadata"))
            superseded_id = _coerce_optional_str(metadata.get("supersedes_raw_memory_id"))
            if not superseded_id:
                continue
            raw_memory_id = _coerce_str(row.get("uuid"))
            edge_uuid = _lineage_edge_id(
                "supersedes",
                organization_id,
                raw_memory_id,
                superseded_id,
            )
            edges.append(
                {
                    "uuid": edge_uuid,
                    "edge_ref": _lineage_record_ref("supersedes", edge_uuid),
                    "organization_id": organization_id,
                    "raw_memory_id": raw_memory_id,
                    "superseded_raw_memory_id": superseded_id,
                    "source_id": _coerce_optional_str(row.get("source_id")),
                    "created_at": _utcnow(),
                }
            )
        existing_superseded_ids = await _existing_content_uuids(
            client,
            "raw_captures",
            organization_id=organization_id,
            values=[_coerce_str(edge.get("superseded_raw_memory_id")) for edge in edges],
        )
        edges = [
            edge
            for edge in edges
            if _coerce_str(edge.get("superseded_raw_memory_id")) in existing_superseded_ids
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
    page_size = min(max(limit, 1), _DEFAULT_BATCH_SIZE)
    while remaining > 0:
        rows = await _select_many(
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
            chunk_id = _coerce_str(row.get("uuid"))
            for entity_id in _coerce_str_list(row.get("entity_ids")):
                edge_uuid = _lineage_edge_id(
                    "extracted_into",
                    organization_id,
                    entity_id,
                    chunk_id,
                )
                edges.append(
                    {
                        "uuid": edge_uuid,
                        "edge_ref": _lineage_record_ref("extracted_into", edge_uuid),
                        "entity_ref": _lineage_record_ref("entity", entity_id),
                        "organization_id": organization_id,
                        "entity_id": entity_id,
                        "chunk_id": chunk_id,
                        "document_id": _coerce_str(row.get("document_id")),
                        "source_id": _coerce_optional_str(row.get("source_id")),
                        "created_at": _utcnow(),
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
    async with surreal_content_client() as client:
        return await materialize_content_lineage(
            client,
            organization_id=organization_id,
            limit=limit,
        )


async def _replace_record(
    client: SurrealContentClient,
    table: str,
    *,
    uuid: str,
    record: SurrealRecord,
) -> SurrealRecord:
    organization_id = record.get("organization_id")
    if organization_id is None:
        raise RuntimeError(f"{table} record {uuid} requires organization_id")
    rows = await _select_many(
        client,
        _UPSERT_RECORD[table],
        uuid=uuid,
        organization_id=str(organization_id),
        record=record,
    )
    if rows:
        return rows[0]
    try:
        rows = await _select_many(client, f"CREATE {table} CONTENT $record;", record=record)
    except Exception as exc:
        rows = await _select_many(
            client,
            _UPSERT_RECORD[table],
            uuid=uuid,
            organization_id=str(organization_id),
            record=record,
        )
        if rows:
            return rows[0]
        raise RuntimeError(f"failed to persist {table} record {uuid}") from exc
    if rows:
        return rows[0]
    raise RuntimeError(f"failed to persist {table} record {uuid}")


async def _replace_raw_memory_records_bulk(
    client: SurrealContentClient,
    records: Sequence[SurrealRecord],
) -> list[SurrealRecord]:
    if not records:
        return []
    for record in records:
        if record.get("organization_id") is None:
            uuid = record.get("uuid") or "<unknown>"
            raise RuntimeError(f"raw_captures record {uuid} requires organization_id")
    rows = await _select_many(
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
    records: Sequence[SurrealRecord],
) -> list[SurrealRecord]:
    records_by_uuid: dict[str, SurrealRecord] = {}
    for record in records:
        uuid = str(record.get("uuid") or "")
        if not uuid:
            raise RuntimeError("raw_captures bulk returned a record without uuid")
        if uuid in records_by_uuid:
            raise RuntimeError(f"raw_captures bulk returned duplicate uuid {uuid}")
        records_by_uuid[uuid] = record

    ordered_records: list[SurrealRecord] = []
    for memory in memories:
        record = records_by_uuid.get(memory.id)
        if record is None:
            raise RuntimeError(f"raw_captures bulk omitted uuid {memory.id}")
        ordered_records.append(record)
    return ordered_records


def _raw_memory_from_write(write: RawMemoryWrite, *, captured_at: datetime) -> RawMemory:
    normalized_scope = _coerce_memory_scope(write.memory_scope)
    _validate_raw_memory_scope(normalized_scope, write.scope_key)
    metadata = dict(write.metadata or {})
    return RawMemory(
        id=str(uuid4()),
        organization_id=write.organization_id,
        source_id=write.source_id,
        principal_id=write.principal_id,
        memory_scope=normalized_scope,
        scope_key=write.scope_key,
        agent_id=_coerce_optional_str(metadata.get("agent_id")),
        project_id=_coerce_optional_str(metadata.get("project_id")),
        review_state=_coerce_str(metadata.get("review_state"), default="pending"),
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
            raw_memory_embedding_text(
                title=memory.title,
                raw_content=memory.raw_content,
            )
        ],
        input_kind="document",
    )
    memory.embedding = _embedding_vector_from_batch(
        embeddings,
        embedding_provider.metadata.dimensions,
    )
    metadata = dict(memory.metadata)
    metadata["embedding_metadata"] = _raw_memory_embedding_metadata(embedding_provider.metadata)
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
            raw_memory_embedding_text(
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
    embedding_metadata = _raw_memory_embedding_metadata(embedding_provider.metadata)
    for memory, embedding_values in zip(pending, embeddings, strict=True):
        memory.embedding = _embedding_vector_from_batch([embedding_values], dimensions)
        metadata = dict(memory.metadata)
        metadata["embedding_metadata"] = embedding_metadata
        memory.metadata = metadata
    return list(memories)


def _raw_memory_embedding_surface(memory: RawMemory) -> str:
    return raw_memory_embedding_text(title=memory.title, raw_content=memory.raw_content)


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
    embedding_provider: EmbeddingProvider | None | object,
) -> RawMemory:
    provider = (
        _configured_raw_memory_embedding_provider()
        if embedding_provider is _RAW_MEMORY_EMBEDDING_AUTO
        else cast("EmbeddingProvider | None", embedding_provider)
    )
    return await _raw_memory_with_embedding(_raw_memory_without_embedding(memory), provider)


async def _raw_memory_prepared_for_save(
    memory: RawMemory,
    *,
    existing: RawMemory | None,
    embedding_provider: EmbeddingProvider | None | object,
) -> RawMemory:
    if not raw_memory_recallable(memory):
        return _raw_memory_without_embedding(memory)
    if _raw_memory_has_replacement_embedding(memory, existing):
        return memory
    if existing is None:
        return await _raw_memory_with_save_embedding(memory, embedding_provider)

    text_changed = _raw_memory_embedding_surface(memory) != _raw_memory_embedding_surface(existing)
    if not text_changed and raw_memory_recallable(existing):
        return _raw_memory_with_existing_embedding(memory, existing)
    return await _raw_memory_with_save_embedding(memory, embedding_provider)


def _value_batches(
    values: Iterable[str], *, batch_size: int = _DEFAULT_BATCH_SIZE
) -> list[list[str]]:
    batch: list[str] = []
    batches: list[list[str]] = []
    for value in values:
        batch.append(value)
        if len(batch) >= batch_size:
            batches.append(batch)
            batch = []
    if batch:
        batches.append(batch)
    return batches


async def _load_sources_for_org(
    client: SurrealContentClient,
    *,
    organization_id: str,
) -> list[ContentSource]:
    rows = await _select_many(
        client,
        "SELECT * FROM crawl_sources WHERE organization_id = $organization_id;",
        organization_id=organization_id,
    )
    sources = [_source_from_record(row) for row in rows]
    return sorted(sources, key=lambda source: (source.name.lower(), source.id))


async def _load_sources_for_search_scope(
    client: SurrealContentClient,
    *,
    organization_id: str,
    source_id: str | None,
    source_name: str | None,
) -> list[ContentSource]:
    where_clause, params = _source_search_scope_clause(
        organization_id=organization_id,
        source_id=source_id,
        source_name=source_name,
    )
    rows = await _select_many(
        client,
        f"SELECT * FROM crawl_sources WHERE {where_clause};",
        **params,
    )
    sources = [_source_from_record(row) for row in rows]
    return sorted(sources, key=lambda source: (source.name.lower(), source.id))


async def _load_documents_for_source_ids(
    client: SurrealContentClient,
    source_ids: list[str],
) -> list[ContentDocument]:
    rows: list[SurrealRecord] = []
    for batch in _value_batches(source_ids):
        rows.extend(
            await _select_many(
                client,
                "SELECT * FROM crawled_documents WHERE source_id INSIDE $source_ids;",
                source_ids=batch,
            )
        )
    documents = [_document_from_record(row) for row in rows]
    return sorted(documents, key=lambda document: (document.source_id, document.id))


async def _load_search_documents_by_ids(
    client: SurrealContentClient,
    document_ids: list[str],
) -> list[ContentDocument]:
    rows: list[SurrealRecord] = []
    for batch in _value_batches(document_ids):
        rows.extend(
            await _select_many(
                client,
                "SELECT uuid, organization_id, source_id, url, title, has_code "
                "FROM crawled_documents WHERE uuid INSIDE $document_ids;",
                document_ids=batch,
            )
        )
    documents = [_document_from_record(row) for row in rows]
    return sorted(documents, key=lambda document: (document.source_id, document.id))


async def _load_chunks_for_document_ids(
    client: SurrealContentClient,
    document_ids: list[str],
) -> list[ContentChunk]:
    rows: list[SurrealRecord] = []
    for batch in _value_batches(document_ids):
        rows.extend(
            await _select_many(
                client,
                f"SELECT {_DOCUMENT_CHUNK_SELECT} "
                "FROM document_chunks WHERE document_id INSIDE $document_ids;",
                document_ids=batch,
            )
        )
    chunks = [_chunk_from_record(row) for row in rows]
    return sorted(chunks, key=lambda chunk: (chunk.document_id, chunk.chunk_index, chunk.id))


def _memory_scope_where(
    *,
    organization_id: str,
    principal_id: str,
    memory_scope: MemoryScope,
    scope_key: str | None,
    agent_id: str | None = None,
    project_id: str | None = None,
) -> tuple[str, dict[str, object]]:
    _validate_raw_memory_scope(memory_scope, scope_key)
    clauses = [
        "organization_id = $organization_id",
        "memory_scope = $memory_scope",
    ]
    params: dict[str, object] = {
        "organization_id": organization_id,
        "memory_scope": memory_scope.value,
    }
    if memory_scope is MemoryScope.PRIVATE:
        clauses.append("principal_id = $principal_id")
        params["principal_id"] = principal_id
    elif scope_key is not None:
        clauses.append("scope_key = $scope_key")
        params["scope_key"] = scope_key
    if agent_id:
        clauses.append("agent_id = $agent_id")
        params["agent_id"] = agent_id
    else:
        clauses.append("(capture_surface != $agent_diary_surface OR capture_surface = NONE)")
        params["agent_diary_surface"] = AGENT_DIARY_CAPTURE_SURFACE
    if project_id:
        clauses.append("project_id = $project_id")
        params["project_id"] = project_id
    return " AND ".join(clauses), params


def _surreal_type_is_string(field: str) -> str:
    if settings.resolved_surreal_url.startswith(_EMBEDDED_SURREAL_SCHEMES):
        return f"type::is::string({field})"
    return f"type::is_string({field})"


def _surreal_type_is_datetime(field: str) -> str:
    if settings.resolved_surreal_url.startswith(_EMBEDDED_SURREAL_SCHEMES):
        return f"type::is::datetime({field})"
    return f"type::is_datetime({field})"


def _raw_memory_recall_where(
    *,
    organization_id: str,
    principal_id: str,
    memory_scope: MemoryScope,
    scope_key: str | None,
    agent_id: str | None = None,
    project_id: str | None = None,
    filters: _RawMemoryRecallFilters | None = None,
) -> tuple[str, dict[str, object]]:
    where_clause, params = _memory_scope_where(
        organization_id=organization_id,
        principal_id=principal_id,
        memory_scope=memory_scope,
        scope_key=scope_key,
        agent_id=agent_id,
        project_id=project_id,
    )
    clauses = [where_clause]
    filters = filters or _RawMemoryRecallFilters()
    if filters.source_ids:
        clauses.append("source_id IN $source_ids")
        params["source_ids"] = list(filters.source_ids)
    if filters.participants:
        clauses.append("metadata.participants CONTAINSANY $participants")
        params["participants"] = list(filters.participants)
    if filters.labels:
        clauses.append("(tags CONTAINSANY $labels OR metadata.labels CONTAINSANY $labels)")
        params["labels"] = list(filters.labels)
    if filters.thread_id:
        clauses.append(
            "(metadata.thread_id = $thread_id "
            "OR metadata.source_record_metadata.thread_id = $thread_id)"
        )
        params["thread_id"] = filters.thread_id
    if filters.occurred_after:
        clauses.append("metadata.occurred_at >= $occurred_after")
        params["occurred_after"] = filters.occurred_after
    if filters.occurred_before:
        clauses.append("metadata.occurred_at <= $occurred_before")
        params["occurred_before"] = filters.occurred_before
    if filters.as_of:
        created_at_is_string = _surreal_type_is_string("created_at")
        created_at_is_datetime = _surreal_type_is_datetime("created_at")
        captured_at_is_string = _surreal_type_is_string("captured_at")
        captured_at_is_datetime = _surreal_type_is_datetime("captured_at")
        valid_at_is_string = _surreal_type_is_string("metadata.valid_at")
        valid_at_is_datetime = _surreal_type_is_datetime("metadata.valid_at")
        valid_from_is_string = _surreal_type_is_string("metadata.valid_from")
        valid_from_is_datetime = _surreal_type_is_datetime("metadata.valid_from")
        invalid_at_is_string = _surreal_type_is_string("metadata.invalid_at")
        invalid_at_is_datetime = _surreal_type_is_datetime("metadata.invalid_at")
        valid_to_is_string = _surreal_type_is_string("metadata.valid_to")
        valid_to_is_datetime = _surreal_type_is_datetime("metadata.valid_to")
        clauses.extend(
            [
                "(created_at = NONE "
                f"OR ({created_at_is_datetime} AND created_at <= $as_of) "
                f"OR ({created_at_is_string} AND created_at <= $as_of_text))",
                "(captured_at = NONE "
                f"OR ({captured_at_is_datetime} AND captured_at <= $as_of) "
                f"OR ({captured_at_is_string} AND captured_at <= $as_of_text))",
                "(metadata.valid_at = NONE "
                f"OR ({valid_at_is_datetime} AND metadata.valid_at <= $as_of) "
                f"OR ({valid_at_is_string} AND metadata.valid_at <= $as_of_text))",
                "(metadata.valid_from = NONE "
                f"OR ({valid_from_is_datetime} AND metadata.valid_from <= $as_of) "
                f"OR ({valid_from_is_string} AND metadata.valid_from <= $as_of_text))",
                "(metadata.invalid_at = NONE "
                f"OR ({invalid_at_is_datetime} AND metadata.invalid_at > $as_of) "
                f"OR ({invalid_at_is_string} AND metadata.invalid_at > $as_of_text))",
                "(metadata.valid_to = NONE "
                f"OR ({valid_to_is_datetime} AND metadata.valid_to > $as_of) "
                f"OR ({valid_to_is_string} AND metadata.valid_to > $as_of_text))",
            ]
        )
        params["as_of"] = filters.as_of
        params["as_of_text"] = filters.as_of_text or filters.as_of.isoformat()
    return " AND ".join(clauses), params


async def _recall_raw_memory_lexical(
    client: SurrealContentClient,
    *,
    organization_id: str,
    principal_id: str,
    query: str,
    memory_scope: MemoryScope,
    scope_key: str | None,
    agent_id: str | None,
    project_id: str | None,
    filters: _RawMemoryRecallFilters | None = None,
    as_of: datetime | None = None,
    limit: int,
) -> list[RawMemory]:
    where_clause, params = _raw_memory_recall_where(
        organization_id=organization_id,
        principal_id=principal_id,
        memory_scope=memory_scope,
        scope_key=scope_key,
        agent_id=agent_id,
        project_id=project_id,
        filters=filters,
    )
    rows = await _select_many(
        client,
        f"SELECT {_RAW_MEMORY_RECALL_FIELDS} FROM raw_captures "
        f"WHERE {where_clause} ORDER BY captured_at DESC LIMIT $limit;",
        **params,
        limit=max(limit * 4, limit),
    )
    scored: list[RawMemory] = []
    for row in rows:
        memory = _raw_memory_from_record(row)
        memory.score = lexical_score(query, memory.title, memory.raw_content)
        if (
            memory.score > 0
            and raw_memory_recallable(memory)
            and _raw_memory_matches_as_of(memory, as_of)
        ):
            scored.append(memory)
    return sorted(scored, key=lambda memory: (-memory.score, memory.captured_at or datetime.min))[
        :limit
    ]


async def _recall_raw_memory_fulltext(
    client: SurrealContentClient,
    *,
    where_clause: str,
    params: Mapping[str, object],
    query: str,
    as_of: datetime | None,
    limit: int,
) -> list[RawMemory]:
    rows = await with_timeout(
        _select_many_raw(
            client,
            f"SELECT {_RAW_MEMORY_RECALL_FIELDS}, "
            "math::max([search::score(0), search::score(1)]) AS score, "
            "search::highlight('<mark>', '</mark>', 0) AS title_snippet, "
            "search::highlight('<mark>', '</mark>', 1) AS content_snippet "
            f"FROM raw_captures WHERE {where_clause} "
            "AND (title @0@ $search_query OR raw_content @1@ $search_query) "
            "ORDER BY score DESC, captured_at DESC LIMIT $limit;",
            **params,
            search_query=query,
            limit=limit * _LIFECYCLE_FILTER_OVERFETCH_FACTOR,
        ),
        timeout_seconds=_DIRECT_SEARCH_QUERY_TIMEOUT_SECONDS,
        operation_name="surreal_raw_memory_fulltext_recall",
    )
    return _recallable_memories(
        [_raw_memory_from_record(row) for row in rows],
        limit=limit,
        as_of=as_of,
    )


async def _recall_raw_memory_vector(
    client: SurrealContentClient,
    *,
    where_clause: str,
    params: Mapping[str, object],
    query_embedding: list[float],
    as_of: datetime | None,
    limit: int,
) -> list[RawMemory]:
    candidate_limit = max(limit * _LIFECYCLE_FILTER_OVERFETCH_FACTOR, limit)
    rows = await with_timeout(
        _select_many_raw(
            client,
            "SELECT * FROM ("
            f"SELECT {_RAW_MEMORY_RECALL_FIELDS}, "
            "(1 - vector::distance::knn()) AS score "
            f"FROM raw_captures WHERE {where_clause} "
            f"AND embedding <|{candidate_limit}, 40|> $query_embedding"
            ") ORDER BY score DESC, captured_at DESC LIMIT $candidate_limit;",
            **params,
            query_embedding=query_embedding,
            candidate_limit=candidate_limit,
        ),
        timeout_seconds=_DIRECT_SEARCH_QUERY_TIMEOUT_SECONDS,
        operation_name="surreal_raw_memory_vector_recall",
    )
    return _recallable_memories(
        [_raw_memory_from_record(row) for row in rows],
        limit=limit,
        as_of=as_of,
    )


async def _raw_memory_query_embedding(query: str) -> list[float] | None:
    provider: EmbeddingProvider | None = None
    try:
        provider = _configured_raw_memory_embedding_provider()
        if provider is None:
            return None
        embeddings = await provider.embed_texts([query], input_kind="query")
        return _embedding_vector_from_batch(embeddings, provider.metadata.dimensions)
    except Exception as exc:
        metadata = provider.metadata if provider is not None else None
        log.warning(
            "raw_memory_query_embedding_failed",
            provider=metadata.provider if metadata is not None else None,
            model=metadata.model if metadata is not None else None,
            dimensions=metadata.dimensions if metadata is not None else None,
            query_length=len(query),
            error_type=type(exc).__name__,
        )
        return None


def _python_raw_memory_rrf_scores(
    result_lists: Sequence[Sequence[RawMemory]],
    *,
    k: float = 60.0,
) -> dict[str, float]:
    scores: dict[str, float] = {}
    for memories in result_lists:
        for rank, memory in enumerate(memories, start=1):
            scores[memory.id] = scores.get(memory.id, 0.0) + (1.0 / (k + rank))
    return scores


async def _surreal_raw_memory_rrf_scores(
    client: SurrealContentClient,
    result_lists: Sequence[Sequence[RawMemory]],
    *,
    limit: int,
    k: float = 60.0,
) -> dict[str, float]:
    rrf_inputs = [
        [{"id": memory.id, "score": memory.score} for memory in memories]
        for memories in result_lists
    ]
    if not any(rrf_inputs):
        return {}
    unique_count = len({memory.id for memories in result_lists for memory in memories})
    try:
        result = await client.execute_query(
            "RETURN search::rrf($lists, $limit, $k);",
            lists=rrf_inputs,
            limit=max(int(limit), unique_count, 1),
            k=k,
        )
    except Exception:
        return {}
    if _query_error(result) is not None:
        return {}

    scores: dict[str, float] = {}
    for row in _normalize_records_preserving_id(result):
        memory_id = _coerce_optional_str(row.get("id") or row.get("uuid") or row.get("record_id"))
        raw_score = row.get("rrf_score", row.get("rff_score", row.get("fuse_score")))
        if memory_id and isinstance(raw_score, int | float):
            scores[memory_id] = float(raw_score)
    return scores


async def _fuse_raw_memory_results(
    client: SurrealContentClient,
    result_lists: Sequence[Sequence[RawMemory]],
    *,
    limit: int,
) -> list[RawMemory]:
    raw_lists = [list(results) for results in result_lists if results]
    if not raw_lists:
        return []
    if len(raw_lists) == 1:
        return raw_lists[0][:limit]

    memory_by_id: dict[str, RawMemory] = {}
    first_seen: dict[str, tuple[int, int]] = {}
    for list_index, memories in enumerate(raw_lists):
        for rank, memory in enumerate(memories, start=1):
            memory_by_id.setdefault(memory.id, memory)
            first_seen.setdefault(memory.id, (list_index, rank))

    scores = await _surreal_raw_memory_rrf_scores(client, raw_lists, limit=limit)
    if set(scores) != set(memory_by_id):
        fallback_scores = _python_raw_memory_rrf_scores(raw_lists)
        for memory_id, score in fallback_scores.items():
            scores.setdefault(memory_id, score)

    fused: list[RawMemory] = []
    ranked_ids = sorted(
        memory_by_id,
        key=lambda memory_id: (-scores.get(memory_id, 0.0), first_seen[memory_id]),
    )
    for memory_id in ranked_ids[:limit]:
        memory = memory_by_id[memory_id]
        score = scores.get(memory_id, 0.0)
        memory.score = score
        fused.append(memory)
    return fused


def _raw_recall_filters(
    *,
    source_ids: Sequence[str] | None,
    participants: Sequence[str] | None,
    labels: Sequence[str] | None,
    thread_id: str | None,
    occurred_after: datetime | str | None,
    occurred_before: datetime | str | None,
    as_of: datetime | str | None,
) -> _RawMemoryRecallFilters:
    as_of_datetime = _as_of_filter_value(as_of)
    return _RawMemoryRecallFilters(
        source_ids=tuple(_normalized_filter_values(source_ids)),
        participants=tuple(_normalized_filter_values(participants)),
        labels=tuple(_normalized_filter_values(labels)),
        thread_id=_coerce_optional_str(thread_id),
        occurred_after=_datetime_filter_value(occurred_after),
        occurred_before=_datetime_filter_value(occurred_before),
        as_of=as_of_datetime,
        as_of_text=as_of_datetime.isoformat() if as_of_datetime else None,
    )


def _normalized_filter_values(values: Sequence[str] | None) -> list[str]:
    if values is None:
        return []
    return [value for item in values if (value := str(item).strip())]


def _datetime_filter_value(value: datetime | str | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    text = str(value).strip()
    return text or None


def _as_of_filter_value(value: datetime | str | None) -> datetime | None:
    if value is None:
        return None
    parsed = _normalize_raw_temporal_datetime(value)
    return parsed


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
    embedding_provider: EmbeddingProvider | None | object = _RAW_MEMORY_EMBEDDING_AUTO,
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
        captured_at=_utcnow(),
    )
    provider = (
        _configured_raw_memory_embedding_provider()
        if embedding_provider is _RAW_MEMORY_EMBEDDING_AUTO
        else cast("EmbeddingProvider | None", embedding_provider)
    )
    memory = await _raw_memory_with_embedding(memory, provider)
    async with surreal_content_client() as client:
        record = await _replace_record(
            client,
            "raw_captures",
            uuid=memory.id,
            record=_raw_memory_record(memory),
        )
    return _raw_memory_from_record(record)


async def remember_raw_memories(
    writes: Sequence[RawMemoryWrite],
    *,
    embedding_provider: EmbeddingProvider | None | object = _RAW_MEMORY_EMBEDDING_AUTO,
) -> list[RawMemory]:
    if not writes:
        return []
    now = _utcnow()
    memories = [_raw_memory_from_write(write, captured_at=now) for write in writes]
    provider = (
        _configured_raw_memory_embedding_provider()
        if embedding_provider is _RAW_MEMORY_EMBEDDING_AUTO
        else cast("EmbeddingProvider | None", embedding_provider)
    )
    memories = await _raw_memories_with_embeddings(memories, provider)
    async with surreal_content_client() as client:
        records = await _replace_raw_memory_records_bulk(
            client,
            [_raw_memory_record(memory) for memory in memories],
        )
    ordered_records = _order_raw_memory_records_by_input(memories, records)
    return [_raw_memory_from_record(record) for record in ordered_records]


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
    normalized_scope = _coerce_memory_scope(memory_scope)
    suggested_scope = (
        _coerce_memory_scope(suggested_memory_scope)
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
        "reflection_confidence": candidate.confidence,
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
    async with surreal_content_client() as client:
        record = await _select_one(
            client,
            "SELECT * FROM raw_captures "
            "WHERE uuid = $memory_id AND organization_id = $organization_id LIMIT 1;",
            memory_id=memory_id,
            organization_id=organization_id,
        )
    return _raw_memory_from_record(record) if record is not None else None


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
        params["memory_scope"] = _coerce_memory_scope(memory_scope).value
        if scope_key is None:
            filters.append("scope_key IS NONE")
        else:
            filters.append("scope_key = $scope_key")
            params["scope_key"] = scope_key

    async with surreal_content_client() as client:
        record = await _select_one(
            client,
            "SELECT * FROM raw_captures "
            f"WHERE {' AND '.join(filters)} "
            "ORDER BY captured_at DESC LIMIT 1;",
            **params,
        )
    return _raw_memory_from_record(record) if record is not None else None


async def list_raw_memories_by_source_id(
    *,
    organization_id: str,
    source_id: str,
) -> list[RawMemory]:
    async with surreal_content_client() as client:
        rows = await _select_many(
            client,
            "SELECT * FROM raw_captures "
            "WHERE source_id = $source_id AND organization_id = $organization_id "
            "ORDER BY captured_at DESC LIMIT 100;",
            source_id=source_id,
            organization_id=organization_id,
        )
    return [_raw_memory_from_record(row) for row in rows]


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
        params["memory_scope"] = _coerce_memory_scope(memory_scope).value
        if scope_key is None:
            filters.append("scope_key IS NONE")
        else:
            filters.append("scope_key = $scope_key")
            params["scope_key"] = scope_key

    async with surreal_content_client() as client:
        record = await _select_one(
            client,
            f"SELECT * FROM raw_captures WHERE {' AND '.join(filters)} "
            "ORDER BY captured_at DESC LIMIT 1;",
            **params,
        )
    return _raw_memory_from_record(record) if record is not None else None


async def resolve_raw_memory_prefix(
    *,
    organization_id: str,
    prefix: str,
    limit: int = 20,
) -> list[RawMemory]:
    normalized = prefix.strip()
    if not normalized or limit <= 0:
        return []
    async with surreal_content_client() as client:
        rows = await _select_many(
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
    return [_raw_memory_from_record(row) for row in rows]


async def list_raw_memories_for_promotion(
    *,
    organization_id: str,
    raw_memory_ids: list[str] | None = None,
    limit: int = 100,
) -> list[RawMemory]:
    if limit <= 0:
        return []
    if raw_memory_ids:
        rows: list[SurrealRecord] = []
        for batch in _value_batches(list(dict.fromkeys(raw_memory_ids))):
            async with surreal_content_client() as client:
                rows.extend(
                    await _select_many(
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
        memories = [_raw_memory_from_record(row) for row in rows]
        order = {memory_id: index for index, memory_id in enumerate(raw_memory_ids)}
        return [
            memory
            for memory in sorted(memories, key=lambda memory: order.get(memory.id, len(order)))
            if memory.memory_scope.value in _RAW_PROMOTION_VISIBLE_SCOPES
        ][:limit]

    query_limit = limit * _LIFECYCLE_FILTER_OVERFETCH_FACTOR
    async with surreal_content_client() as client:
        rows = await _select_many(
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
    memories = [_raw_memory_from_record(row) for row in rows]
    return [
        memory
        for memory in memories
        if memory.memory_scope.value in _RAW_PROMOTION_VISIBLE_SCOPES
        and memory.deleted_at is None
        and raw_memory_currently_recallable(memory)
    ][:limit]


async def save_raw_memory(
    memory: RawMemory,
    *,
    embedding_provider: EmbeddingProvider | None | object = _RAW_MEMORY_EMBEDDING_AUTO,
) -> RawMemory:
    async with surreal_content_client() as client:
        existing_record = await _select_one(
            client,
            "SELECT * FROM raw_captures "
            "WHERE organization_id = $organization_id AND uuid = $uuid LIMIT 1;",
            organization_id=memory.organization_id,
            uuid=memory.id,
        )
        memory = await _raw_memory_prepared_for_save(
            memory,
            existing=_raw_memory_from_record(existing_record) if existing_record else None,
            embedding_provider=embedding_provider,
        )
        record = await _replace_record(
            client,
            "raw_captures",
            uuid=memory.id,
            record=_raw_memory_record(memory),
        )
    return _raw_memory_from_record(record)


async def _recall_raw_memory_result(
    *,
    organization_id: str,
    principal_id: str,
    query: str,
    memory_scope: MemoryScope | str = MemoryScope.PRIVATE,
    scope_key: str | None = None,
    agent_id: str | None = None,
    project_id: str | None = None,
    source_ids: Sequence[str] | None = None,
    participants: Sequence[str] | None = None,
    labels: Sequence[str] | None = None,
    thread_id: str | None = None,
    occurred_after: datetime | str | None = None,
    occurred_before: datetime | str | None = None,
    as_of: datetime | str | None = None,
    limit: int = 10,
    raise_on_source_failure: bool,
) -> RawMemoryRecallResult:
    normalized_query = query.strip()
    if not normalized_query or limit <= 0:
        return RawMemoryRecallResult(())

    normalized_scope = _coerce_memory_scope(memory_scope)
    filters = _raw_recall_filters(
        source_ids=source_ids,
        participants=participants,
        labels=labels,
        thread_id=thread_id,
        occurred_after=occurred_after,
        occurred_before=occurred_before,
        as_of=as_of,
    )
    effective_as_of = filters.as_of or datetime.now(UTC)
    filters = replace(
        filters,
        as_of=effective_as_of,
        as_of_text=filters.as_of_text or effective_as_of.isoformat(),
    )
    where_clause, params = _raw_memory_recall_where(
        organization_id=organization_id,
        principal_id=principal_id,
        memory_scope=normalized_scope,
        scope_key=scope_key,
        agent_id=agent_id,
        project_id=project_id,
        filters=filters,
    )
    source_results: list[CandidateSourceResult[RawMemory]] = []
    query_embedding: list[float] | None = None
    try:
        query_embedding = await _raw_memory_query_embedding(normalized_query)
    except Exception as exc:
        source_results.append(CandidateSourceResult.failed("raw_vector", type(exc).__name__))
    async with surreal_content_client() as client:
        fulltext_memories: list[RawMemory] = []
        vector_memories: list[RawMemory] = []
        try:
            fulltext_memories = await _recall_raw_memory_fulltext(
                client,
                where_clause=where_clause,
                params=params,
                query=normalized_query,
                as_of=effective_as_of,
                limit=limit,
            )
        except Exception as exc:
            log.warning(
                "raw_memory_fulltext_recall_failed",
                organization_id=organization_id,
                memory_scope=normalized_scope.value,
                has_scope_key=scope_key is not None,
                error_type=type(exc).__name__,
            )
            fulltext_memories = []
            source_results.append(CandidateSourceResult.failed("raw_fulltext", type(exc).__name__))
        else:
            source_results.append(CandidateSourceResult.success("raw_fulltext", fulltext_memories))
        if query_embedding is not None:
            try:
                vector_memories = await _recall_raw_memory_vector(
                    client,
                    where_clause=where_clause,
                    params=params,
                    query_embedding=query_embedding,
                    as_of=effective_as_of,
                    limit=limit,
                )
            except Exception as exc:
                log.warning(
                    "raw_memory_vector_recall_failed",
                    organization_id=organization_id,
                    memory_scope=normalized_scope.value,
                    has_scope_key=scope_key is not None,
                    error_type=type(exc).__name__,
                )
                vector_memories = []
                source_results.append(
                    CandidateSourceResult.failed("raw_vector", type(exc).__name__)
                )
            else:
                source_results.append(CandidateSourceResult.success("raw_vector", vector_memories))
        memories = await _fuse_raw_memory_results(
            client,
            [fulltext_memories, vector_memories],
            limit=limit,
        )
        if memories:
            return RawMemoryRecallResult(tuple(memories), tuple(source_results))
        try:
            lexical_memories = await _recall_raw_memory_lexical(
                client,
                organization_id=organization_id,
                principal_id=principal_id,
                query=normalized_query,
                memory_scope=normalized_scope,
                scope_key=scope_key,
                agent_id=agent_id,
                project_id=project_id,
                filters=filters,
                as_of=effective_as_of,
                limit=limit,
            )
        except (RuntimeError, TimeoutError) as exc:
            source_results.append(CandidateSourceResult.failed("raw_lexical", type(exc).__name__))
            if raise_on_source_failure:
                raise
            lexical_memories = []
        else:
            source_results.append(CandidateSourceResult.success("raw_lexical", lexical_memories))
        return RawMemoryRecallResult(tuple(lexical_memories), tuple(source_results))


async def recall_raw_memory_with_sources(
    *,
    organization_id: str,
    principal_id: str,
    query: str,
    memory_scope: MemoryScope | str = MemoryScope.PRIVATE,
    scope_key: str | None = None,
    agent_id: str | None = None,
    project_id: str | None = None,
    source_ids: Sequence[str] | None = None,
    participants: Sequence[str] | None = None,
    labels: Sequence[str] | None = None,
    thread_id: str | None = None,
    occurred_after: datetime | str | None = None,
    occurred_before: datetime | str | None = None,
    as_of: datetime | str | None = None,
    limit: int = 10,
) -> RawMemoryRecallResult:
    return await _recall_raw_memory_result(
        organization_id=organization_id,
        principal_id=principal_id,
        query=query,
        memory_scope=memory_scope,
        scope_key=scope_key,
        agent_id=agent_id,
        project_id=project_id,
        source_ids=source_ids,
        participants=participants,
        labels=labels,
        thread_id=thread_id,
        occurred_after=occurred_after,
        occurred_before=occurred_before,
        as_of=as_of,
        limit=limit,
        raise_on_source_failure=False,
    )


async def recall_raw_memory(
    *,
    organization_id: str,
    principal_id: str,
    query: str,
    memory_scope: MemoryScope | str = MemoryScope.PRIVATE,
    scope_key: str | None = None,
    agent_id: str | None = None,
    project_id: str | None = None,
    source_ids: Sequence[str] | None = None,
    participants: Sequence[str] | None = None,
    labels: Sequence[str] | None = None,
    thread_id: str | None = None,
    occurred_after: datetime | str | None = None,
    occurred_before: datetime | str | None = None,
    as_of: datetime | str | None = None,
    limit: int = 10,
) -> list[RawMemory]:
    result = await _recall_raw_memory_result(
        organization_id=organization_id,
        principal_id=principal_id,
        query=query,
        memory_scope=memory_scope,
        scope_key=scope_key,
        agent_id=agent_id,
        project_id=project_id,
        source_ids=source_ids,
        participants=participants,
        labels=labels,
        thread_id=thread_id,
        occurred_after=occurred_after,
        occurred_before=occurred_before,
        as_of=as_of,
        limit=limit,
        raise_on_source_failure=True,
    )
    return list(result.memories)


async def list_raw_memories_for_scope(
    *,
    organization_id: str,
    principal_id: str,
    memory_scope: MemoryScope | str = MemoryScope.PRIVATE,
    scope_key: str | None = None,
    agent_id: str | None = None,
    project_id: str | None = None,
    limit: int = 50,
    include_lifecycle_hidden: bool = False,
) -> list[RawMemory]:
    if limit <= 0:
        return []
    normalized_scope = _coerce_memory_scope(memory_scope)
    query_limit = limit if include_lifecycle_hidden else limit * _LIFECYCLE_FILTER_OVERFETCH_FACTOR
    where_clause, params = _memory_scope_where(
        organization_id=organization_id,
        principal_id=principal_id,
        memory_scope=normalized_scope,
        scope_key=scope_key,
        agent_id=agent_id,
        project_id=project_id,
    )
    async with surreal_content_client() as client:
        rows = await _select_many(
            client,
            f"SELECT * FROM raw_captures WHERE {where_clause} "
            "ORDER BY captured_at DESC LIMIT $limit;",
            **params,
            limit=query_limit,
        )
    memories = [_raw_memory_from_record(row) for row in rows]
    if include_lifecycle_hidden:
        return memories[:limit]
    return _recallable_memories(memories, limit=limit, as_of=datetime.now(UTC))


async def list_reflection_candidate_reviews(
    *,
    organization_id: str,
    review_state: str = "pending",
    limit: int = 50,
) -> list[RawMemory]:
    if limit <= 0:
        return []
    target_review_state = review_state.strip().lower()
    async with surreal_content_client() as client:
        rows = await _select_many(
            client,
            "SELECT * FROM raw_captures "
            "WHERE organization_id = $organization_id "
            "AND capture_surface = $capture_surface "
            "AND review_state = $review_state "
            "ORDER BY captured_at ASC LIMIT $limit;",
            organization_id=organization_id,
            capture_surface="reflection_candidate",
            review_state=target_review_state,
            limit=limit,
        )
    memories = [_raw_memory_from_record(row) for row in rows]
    memories = [
        memory
        for memory in memories
        if str(memory.review_state or "pending").strip().lower() == target_review_state
    ]
    memories = sorted(
        memories,
        key=lambda memory: (
            memory.captured_at or memory.created_at or datetime.min.replace(tzinfo=UTC)
        ),
    )
    return memories[:limit]


async def list_reflection_dream_source_memories(
    *,
    organization_id: str,
    limit: int = 50,
) -> list[RawMemory]:
    if limit <= 0:
        return []
    query_limit = limit * _LIFECYCLE_FILTER_OVERFETCH_FACTOR
    async with surreal_content_client() as client:
        rows = await _select_many(
            client,
            "SELECT * FROM raw_captures "
            "WHERE organization_id = $organization_id "
            "AND (capture_surface != $candidate_surface OR capture_surface = NONE) "
            "AND (capture_surface != $source_surface OR capture_surface = NONE) "
            "AND (capture_surface != $reflection_surface OR capture_surface = NONE) "
            "AND (capture_surface != $synthesis_surface OR capture_surface = NONE) "
            "ORDER BY captured_at ASC LIMIT $limit;",
            organization_id=organization_id,
            candidate_surface="reflection_candidate",
            source_surface="reflection_source",
            reflection_surface="reflection",
            synthesis_surface="synthesis_artifact",
            limit=query_limit,
        )
    memories = [_raw_memory_from_record(row) for row in rows]
    return [
        memory
        for memory in memories
        if raw_memory_currently_recallable(memory)
        and _raw_memory_capture_surface(memory) not in _REFLECTION_DREAM_EXCLUDED_CAPTURE_SURFACES
        and not memory.metadata.get("reflection_dream_processed_at")
    ][:limit]


async def get_or_create_source(
    url: str,
    depth: int,
    data: dict[str, object],
    *,
    organization_id: str,
) -> tuple[ContentSource, bool]:
    normalized_url = url.rstrip("/")
    source_name = str(data.get("name") or normalized_url.split("//")[-1].split("/")[0])
    source_type = str(data.get("source_type") or "website").lower()
    include_patterns = _coerce_str_list(data.get("include_patterns") or data.get("patterns"))
    exclude_patterns = _coerce_str_list(data.get("exclude_patterns") or data.get("exclude"))

    async with surreal_content_client() as client:
        existing = await _select_one(
            client,
            "SELECT * FROM crawl_sources "
            "WHERE organization_id = $organization_id AND url = $url LIMIT 1;",
            organization_id=organization_id,
            url=normalized_url,
        )
        if existing is not None:
            return _source_from_record(existing), False

        now = _utcnow()
        source = ContentSource(
            id=str(uuid4()),
            organization_id=organization_id,
            name=source_name,
            url=normalized_url,
            source_type=source_type,
            description=_coerce_optional_str(data.get("description")),
            crawl_depth=max(0, min(int(depth), 10)),
            include_patterns=include_patterns,
            exclude_patterns=exclude_patterns,
            created_at=now,
            updated_at=now,
        )
        record = await _replace_record(
            client,
            "crawl_sources",
            uuid=source.id,
            record=_source_record(source),
        )
    return _source_from_record(record), True


async def source_exists(source_id: str, organization_id: str) -> bool:
    async with surreal_content_client() as client:
        record = await _select_one(
            client,
            "SELECT * FROM crawl_sources "
            "WHERE uuid = $source_id AND organization_id = $organization_id LIMIT 1;",
            source_id=source_id,
            organization_id=organization_id,
        )
    return record is not None


async def list_source_ids_for_org(organization_id: str) -> list[str]:
    async with surreal_content_client() as client:
        sources = await _load_sources_for_org(client, organization_id=organization_id)
    return [source.id for source in sources]


async def set_source_job_state(
    source_id: str,
    *,
    organization_id: str,
    job_id: str | None,
    crawl_status: str,
    last_error: str | None,
) -> ContentSource | None:
    async with surreal_content_client() as client:
        record = await _select_one(
            client,
            "SELECT * FROM crawl_sources "
            "WHERE uuid = $source_id AND organization_id = $organization_id LIMIT 1;",
            source_id=source_id,
            organization_id=organization_id,
        )
        if record is None:
            return None

        source = _source_from_record(record)
        source.current_job_id = job_id
        source.crawl_status = crawl_status
        source.last_error = last_error
        source.updated_at = _utcnow()
        saved = await _replace_record(
            client,
            "crawl_sources",
            uuid=source.id,
            record=_source_record(source),
        )
    return _source_from_record(saved)


async def load_search_scope(
    *,
    organization_id: str,
    source_id: str | None,
    source_name: str | None,
) -> tuple[
    list[ContentSource], dict[str, ContentSource], dict[str, ContentDocument], list[ContentChunk]
]:
    async with surreal_content_client() as client:
        sources = await _load_sources_for_search_scope(
            client,
            organization_id=organization_id,
            source_id=source_id,
            source_name=source_name,
        )
        source_ids = [source.id for source in sources]
        documents = await _load_documents_for_source_ids(client, source_ids)
        chunks = await _load_chunks_for_document_ids(
            client, [document.id for document in documents]
        )

    sources_by_id = {source.id: source for source in sources}
    documents_by_id = {document.id: document for document in documents}
    return sources, sources_by_id, documents_by_id, chunks


def _document_search_candidate_limit(limit: int) -> int:
    return min(max(limit * 5, limit, 1), 100)


def _document_language_clause(language: str | None) -> tuple[str, dict[str, object]]:
    if not language:
        return "", {}
    return (
        " AND chunk_type = 'code' AND string::lowercase(language ?? '') = $language",
        {"language": language.lower()},
    )


def _hydrate_document_search_rows(
    rows: list[SurrealRecord],
    *,
    documents_by_id: dict[str, ContentDocument],
    sources_by_id: dict[str, ContentSource],
) -> list[ContentSearchRow]:
    hydrated: list[ContentSearchRow] = []
    for row in rows:
        chunk = _chunk_from_record(row)
        document = documents_by_id.get(chunk.document_id)
        if document is None:
            continue
        source = sources_by_id.get(document.source_id)
        if source is None:
            continue
        hydrated.append((chunk, document, source.name, source.id, _coerce_float(row.get("score"))))
    return hydrated


def _source_search_scope_clause(
    *,
    organization_id: str,
    source_id: str | None,
    source_name: str | None,
) -> tuple[str, dict[str, object]]:
    clauses = ["organization_id = $organization_id"]
    params: dict[str, object] = {"organization_id": organization_id}
    if source_id is not None:
        clauses.append("uuid = $source_id")
        params["source_id"] = source_id
    elif source_name is not None:
        normalized_source_name = build_fulltext_query(source_name.lower())
        if normalized_source_name:
            clauses.append("name @0@ $source_name")
            params["source_name"] = normalized_source_name
        else:
            clauses.append("uuid = $source_name_empty_sentinel")
            params["source_name_empty_sentinel"] = "__sibyl_empty_source_name__"
    return " AND ".join(clauses), params


async def _load_search_sources(
    client: SurrealContentClient,
    *,
    organization_id: str,
    source_id: str | None,
    source_name: str | None,
) -> list[ContentSource]:
    where_clause, params = _source_search_scope_clause(
        organization_id=organization_id,
        source_id=source_id,
        source_name=source_name,
    )
    rows = await _select_many(
        client,
        "SELECT uuid, organization_id, name, url, source_type, description, crawl_status "
        f"FROM crawl_sources WHERE {where_clause} ORDER BY name ASC, uuid ASC;",
        **params,
    )
    return [_source_from_record(row) for row in rows]


def _document_ids_from_search_rows(
    *row_groups: list[SurrealRecord],
) -> list[str]:
    document_ids: set[str] = set()
    for rows in row_groups:
        for row in rows:
            document_id = row.get("document_id")
            if document_id is not None:
                document_ids.add(str(document_id))
    return sorted(document_ids)


async def search_document_chunks(
    *,
    organization_id: str,
    query_text: str,
    query_embedding: list[float] | None,
    source_id: str | None = None,
    source_name: str | None = None,
    language: str | None = None,
    limit: int = 10,
    similarity_threshold: float = 0.5,
) -> tuple[list[ContentSearchRow], list[ContentSearchRow]]:
    if limit <= 0:
        return [], []

    candidate_limit = _document_search_candidate_limit(limit)
    language_clause, language_params = _document_language_clause(language)
    lexical_query_text = build_fulltext_query(query_text)
    errors: list[str] = []

    async with surreal_content_client() as client:
        sources = await _load_search_sources(
            client,
            organization_id=organization_id,
            source_id=source_id,
            source_name=source_name,
        )
        if not sources:
            return [], []

        source_ids = [source.id for source in sources]
        sources_by_id = {source.id: source for source in sources}

        vector_rows: list[SurrealRecord] = []
        vector_errors: list[str] = []
        if query_embedding is not None:
            vector_params: dict[str, object] = {
                "organization_id": organization_id,
                "source_ids": source_ids,
                "query_embedding": query_embedding,
                "similarity_threshold": similarity_threshold,
                "candidate_limit": candidate_limit,
                **language_params,
            }
            try:
                vector_rows = await with_timeout(
                    _select_many_raw(
                        client,
                        "SELECT * FROM ("
                        "SELECT uuid, organization_id, source_id, document_id, chunk_index, "
                        "chunk_type, content, context, heading_path, language, "
                        "has_entities, entity_ids, "
                        "(1 - vector::distance::knn()) AS score "
                        "FROM document_chunks WHERE organization_id = $organization_id "
                        "AND source_id INSIDE $source_ids"
                        f"{language_clause} "
                        f"AND embedding <|{candidate_limit}, 40|> $query_embedding"
                        ") WHERE score >= $similarity_threshold "
                        "ORDER BY score DESC LIMIT $candidate_limit;",
                        **vector_params,
                    ),
                    timeout_seconds=_DIRECT_SEARCH_QUERY_TIMEOUT_SECONDS,
                    operation_name="surreal_document_vector_search",
                )
            except (RuntimeError, TimeoutError) as exc:
                vector_errors.append(str(exc))

        lexical_rows: list[SurrealRecord] = []
        if lexical_query_text:
            lexical_params: dict[str, object] = {
                "organization_id": organization_id,
                "source_ids": source_ids,
                "search_query": lexical_query_text,
                "candidate_limit": candidate_limit,
                **language_params,
            }
            try:
                lexical_rows = await with_timeout(
                    _select_many_raw(
                        client,
                        "SELECT uuid, organization_id, source_id, document_id, chunk_index, "
                        "chunk_type, content, context, heading_path, language, "
                        "has_entities, entity_ids, "
                        "search::score(0) AS score, "
                        "search::highlight('<mark>', '</mark>', 0) AS snippet "
                        "FROM document_chunks WHERE organization_id = $organization_id "
                        "AND source_id INSIDE $source_ids"
                        f"{language_clause} "
                        "AND content @0@ $search_query "
                        "ORDER BY score DESC LIMIT $candidate_limit;",
                        **lexical_params,
                    ),
                    timeout_seconds=_DIRECT_SEARCH_QUERY_TIMEOUT_SECONDS,
                    operation_name="surreal_document_lexical_search",
                )
            except (RuntimeError, TimeoutError) as exc:
                errors.append(str(exc))

        document_ids = _document_ids_from_search_rows(vector_rows, lexical_rows)
        documents = (
            await _load_search_documents_by_ids(client, document_ids) if document_ids else []
        )
        documents_by_id = {document.id: document for document in documents}

    if vector_errors:
        raise RuntimeError("; ".join([*vector_errors, *errors]))
    if errors and not vector_rows and not lexical_rows:
        raise RuntimeError("; ".join(errors))

    return (
        _hydrate_document_search_rows(
            vector_rows,
            documents_by_id=documents_by_id,
            sources_by_id=sources_by_id,
        ),
        _hydrate_document_search_rows(
            lexical_rows,
            documents_by_id=documents_by_id,
            sources_by_id=sources_by_id,
        ),
    )


async def list_unlinked_document_chunks(
    *,
    organization_id: str,
    source_id: str | None = None,
    limit: int = 1000,
) -> list[ContentChunk]:
    clauses = ["organization_id = $organization_id", "has_entities = false"]
    params: dict[str, object] = {
        "organization_id": organization_id,
        "limit": max(limit, 0),
    }
    if source_id is not None:
        clauses.append("source_id = $source_id")
        params["source_id"] = source_id
    async with surreal_content_client() as client:
        rows = await _select_many(
            client,
            f"SELECT * FROM document_chunks WHERE {' AND '.join(clauses)} "
            "ORDER BY document_id ASC, chunk_index ASC, uuid ASC LIMIT $limit;",
            **params,
        )
    return [_chunk_from_record(row) for row in rows]


def tokenize(text: str) -> set[str]:
    return {match.group(0).lower() for match in _TOKEN_PATTERN.finditer(text)}


def tokenize_fields(*fields: str | None) -> set[str]:
    tokens: set[str] = set()
    for value in fields:
        if value:
            tokens.update(match.group(0).lower() for match in _TOKEN_PATTERN.finditer(value))
    return tokens


def lexical_score_from_tokens(query_tokens: set[str], *field_token_sets: set[str]) -> float:
    if not query_tokens:
        return 0.0
    matched: set[str] = set()
    for tokens in field_token_sets:
        matched.update(query_tokens & tokens)
    return len(matched) / len(query_tokens)


def lexical_score(query_text: str, *fields: str | None) -> float:
    return lexical_score_from_tokens(tokenize(query_text), tokenize_fields(*fields))


__all__ = [
    "AGENT_DIARY_CAPTURE_SURFACE",
    "ContentChunk",
    "ContentDocument",
    "ContentLineageBackfillResult",
    "ContentSearchRow",
    "ContentSource",
    "MemoryScope",
    "RawMemory",
    "RawMemoryRecallResult",
    "RawMemoryWrite",
    "backfill_content_lineage",
    "build_surreal_content_client",
    "close_shared_surreal_content_client",
    "get_or_create_source",
    "get_raw_memory",
    "get_raw_memory_by_dedupe_key",
    "get_raw_memory_by_source_id",
    "get_shared_surreal_content_client",
    "lexical_score",
    "lexical_score_from_tokens",
    "list_raw_memories_by_source_id",
    "list_raw_memories_for_promotion",
    "list_raw_memories_for_scope",
    "list_reflection_candidate_reviews",
    "list_reflection_dream_source_memories",
    "list_source_ids_for_org",
    "list_unlinked_document_chunks",
    "load_search_scope",
    "materialize_content_lineage",
    "raw_memory_currently_recallable",
    "raw_memory_embedding_text",
    "raw_memory_recallable",
    "recall_raw_memory",
    "recall_raw_memory_with_sources",
    "remember_raw_memories",
    "remember_raw_memory",
    "remember_reflection_candidate_review",
    "reset_raw_memory_embedding_provider_cache",
    "resolve_raw_memory_prefix",
    "save_raw_memory",
    "search_document_chunks",
    "set_source_job_state",
    "source_exists",
    "surreal_content_client",
    "tokenize",
    "tokenize_fields",
]
