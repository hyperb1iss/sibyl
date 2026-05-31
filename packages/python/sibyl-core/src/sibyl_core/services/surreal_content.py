"""Surreal-backed content helpers shared by core services."""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import AsyncIterator, Iterable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol, cast
from uuid import uuid4

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
_RAW_MEMORY_EMBEDDING_TEXT_MAX_CHARS = 12_000
_RAW_MEMORY_EMBEDDING_TEXT_TRUNCATION_MARKER = "\n...[truncated for raw memory embedding]..."
_RAW_MEMORY_EMBEDDING_TEXT_VERSION = "raw-capture-v1"
_RAW_MEMORY_EMBEDDING_AUTO = object()
_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_]+")
type _RawMemoryProviderCacheKey = tuple[EmbeddingProviderName, str, int, str]
_raw_memory_embedding_provider: EmbeddingProvider | None = None
_raw_memory_embedding_fingerprint: _RawMemoryProviderCacheKey | None = None
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
AGENT_DIARY_CAPTURE_SURFACE = "agent_diary"
type SurrealRecord = dict[str, object]


class RawExecuteQuery(Protocol):
    async def __call__(self, query: str, **params: object) -> object: ...


class MemoryScope(StrEnum):
    PRIVATE = "private"
    DELEGATED = "delegated"
    PROJECT = "project"
    TEAM = "team"
    ORGANIZATION = "organization"
    SHARED = "shared"
    PUBLIC = "public"


_SCOPES_REQUIRING_SCOPE_KEY = {
    MemoryScope.DELEGATED,
    MemoryScope.PROJECT,
    MemoryScope.TEAM,
    MemoryScope.SHARED,
}


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
    created_at: datetime | None = None
    score: float = 0.0


@dataclass(frozen=True, slots=True)
class _RawMemoryRecallFilters:
    participants: tuple[str, ...] = ()
    labels: tuple[str, ...] = ()
    thread_id: str | None = None
    occurred_after: str | None = None
    occurred_before: str | None = None


_RECALL_EXCLUDED_REVIEW_STATES = frozenset(
    {
        "archived",
        "deleted",
        "hidden",
        "redacted",
        "superseded",
    }
)
_RECALL_EXCLUDED_LIFECYCLE_STATES = frozenset(
    {
        "deleted",
        "duplicate",
        "hidden",
        "redacted",
        "sensitive",
        "stale",
        "superseded",
        "wrong",
    }
)


def raw_memory_recallable(memory: RawMemory) -> bool:
    review_state = str(memory.review_state or "").strip().lower()
    lifecycle_state = str(memory.metadata.get("lifecycle_state") or "").strip().lower()
    if review_state in _RECALL_EXCLUDED_REVIEW_STATES:
        return False
    if lifecycle_state in _RECALL_EXCLUDED_LIFECYCLE_STATES:
        return False
    if memory.metadata.get("superseded_by_raw_memory_id"):
        return False
    if memory.metadata.get("superseded_by_source_id"):
        return False
    return not memory.metadata.get("duplicate_of_source_id")


def _raw_memory_capture_surface(memory: RawMemory) -> str:
    metadata_surface = memory.metadata.get("capture_surface")
    value = memory.capture_surface if memory.capture_surface is not None else metadata_surface
    return str(value or "").strip().lower()


def _recallable_memories(memories: list[RawMemory], *, limit: int) -> list[RawMemory]:
    return [memory for memory in memories if raw_memory_recallable(memory)][:limit]


def build_surreal_content_client() -> SurrealContentClient:
    return SurrealContentClient(
        url=settings.resolved_surreal_url,
        username=settings.surreal_username,
        password=settings.surreal_password.get_secret_value(),
        token=settings.surreal_token.get_secret_value(),
    )


@asynccontextmanager
async def surreal_content_client() -> AsyncIterator[SurrealContentClient]:
    client = build_surreal_content_client()
    try:
        yield client
    finally:
        await client.close()


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
        created_at=_coerce_datetime(record.get("created_at")),
        score=_coerce_float(record.get("score")),
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
    return {
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
        "metadata": dict(memory.metadata),
        "provenance": dict(memory.provenance),
        "capture_surface": memory.capture_surface,
        "created_by_user_id": memory.created_by_user_id or memory.principal_id,
        "captured_at": memory.captured_at,
        "deleted_at": memory.deleted_at,
        "purge_after": memory.purge_after,
        "created_at": memory.created_at,
    }


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
    created = await _select_one(
        client, f"SELECT * FROM {table} WHERE uuid = $uuid LIMIT 1;", uuid=uuid
    )
    if created is None:
        raise RuntimeError(f"failed to persist {table} record {uuid}")
    return created


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
                "SELECT * FROM document_chunks WHERE document_id INSIDE $document_ids;",
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
        f"SELECT * FROM raw_captures WHERE {where_clause} ORDER BY captured_at DESC LIMIT $limit;",
        **params,
        limit=max(limit * 4, limit),
    )
    scored: list[RawMemory] = []
    for row in rows:
        memory = _raw_memory_from_record(row)
        memory.score = lexical_score(query, memory.title, memory.raw_content)
        if memory.score > 0 and raw_memory_recallable(memory):
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
    limit: int,
) -> list[RawMemory]:
    rows = await with_timeout(
        _select_many_raw(
            client,
            "SELECT *, math::max([search::score(0), search::score(1)]) AS score "
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
    return _recallable_memories([_raw_memory_from_record(row) for row in rows], limit=limit)


async def _recall_raw_memory_vector(
    client: SurrealContentClient,
    *,
    where_clause: str,
    params: Mapping[str, object],
    query_embedding: list[float],
    limit: int,
) -> list[RawMemory]:
    candidate_limit = max(limit * _LIFECYCLE_FILTER_OVERFETCH_FACTOR, limit)
    rows = await with_timeout(
        _select_many_raw(
            client,
            "SELECT * FROM ("
            "SELECT *, (1 - vector::distance::knn()) AS score "
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
    return _recallable_memories([_raw_memory_from_record(row) for row in rows], limit=limit)


async def _raw_memory_query_embedding(query: str) -> list[float] | None:
    provider = _configured_raw_memory_embedding_provider()
    if provider is None:
        return None
    try:
        embeddings = await provider.embed_texts([query], input_kind="query")
        return _embedding_vector_from_batch(embeddings, provider.metadata.dimensions)
    except Exception:
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
    participants: Sequence[str] | None,
    labels: Sequence[str] | None,
    thread_id: str | None,
    occurred_after: datetime | str | None,
    occurred_before: datetime | str | None,
) -> _RawMemoryRecallFilters:
    return _RawMemoryRecallFilters(
        participants=tuple(_normalized_filter_values(participants)),
        labels=tuple(_normalized_filter_values(labels)),
        thread_id=_coerce_optional_str(thread_id),
        occurred_after=_datetime_filter_value(occurred_after),
        occurred_before=_datetime_filter_value(occurred_before),
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
    now = _utcnow()
    normalized_scope = _coerce_memory_scope(memory_scope)
    _validate_raw_memory_scope(normalized_scope, scope_key)
    memory = RawMemory(
        id=str(uuid4()),
        organization_id=organization_id,
        source_id=source_id,
        principal_id=principal_id,
        memory_scope=normalized_scope,
        scope_key=scope_key,
        agent_id=_coerce_optional_str((metadata or {}).get("agent_id")),
        project_id=_coerce_optional_str((metadata or {}).get("project_id")),
        review_state=_coerce_str((metadata or {}).get("review_state"), default="pending"),
        entity_type=entity_type,
        title=title,
        raw_content=raw_content,
        tags=list(tags or []),
        metadata=dict(metadata or {}),
        provenance=dict(provenance or {}),
        capture_surface=capture_surface,
        captured_at=now,
        created_at=now,
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
            state=MemoryLifecycleState.PENDING,
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


async def get_raw_memory_by_dedupe_key(
    *,
    organization_id: str,
    dedupe_key: str,
) -> RawMemory | None:
    async with surreal_content_client() as client:
        record = await _select_one(
            client,
            "SELECT * FROM raw_captures "
            "WHERE organization_id = $organization_id "
            "AND metadata.dedupe_key = $dedupe_key "
            "ORDER BY captured_at DESC LIMIT 1;",
            organization_id=organization_id,
            dedupe_key=dedupe_key,
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
                        "ORDER BY captured_at ASC, uuid ASC;",
                        organization_id=organization_id,
                        raw_memory_ids=batch,
                    )
                )
        memories = [_raw_memory_from_record(row) for row in rows]
        order = {memory_id: index for index, memory_id in enumerate(raw_memory_ids)}
        return sorted(memories, key=lambda memory: order.get(memory.id, len(order)))[:limit]

    query_limit = limit * _LIFECYCLE_FILTER_OVERFETCH_FACTOR
    async with surreal_content_client() as client:
        rows = await _select_many(
            client,
            "SELECT * FROM raw_captures "
            "WHERE organization_id = $organization_id "
            "AND deleted_at = NONE "
            "AND (metadata.raw_promotion_state = NONE "
            "OR metadata.raw_promotion_state = '' "
            "OR metadata.raw_promotion_state = 'pending') "
            "ORDER BY captured_at ASC, uuid ASC LIMIT $limit;",
            organization_id=organization_id,
            limit=query_limit,
        )
    memories = [_raw_memory_from_record(row) for row in rows]
    return [
        memory for memory in memories if memory.deleted_at is None and raw_memory_recallable(memory)
    ][:limit]


async def save_raw_memory(memory: RawMemory) -> RawMemory:
    async with surreal_content_client() as client:
        record = await _replace_record(
            client,
            "raw_captures",
            uuid=memory.id,
            record=_raw_memory_record(memory),
        )
    return _raw_memory_from_record(record)


async def recall_raw_memory(
    *,
    organization_id: str,
    principal_id: str,
    query: str,
    memory_scope: MemoryScope | str = MemoryScope.PRIVATE,
    scope_key: str | None = None,
    agent_id: str | None = None,
    project_id: str | None = None,
    participants: Sequence[str] | None = None,
    labels: Sequence[str] | None = None,
    thread_id: str | None = None,
    occurred_after: datetime | str | None = None,
    occurred_before: datetime | str | None = None,
    limit: int = 10,
) -> list[RawMemory]:
    normalized_query = query.strip()
    if not normalized_query or limit <= 0:
        return []

    normalized_scope = _coerce_memory_scope(memory_scope)
    filters = _raw_recall_filters(
        participants=participants,
        labels=labels,
        thread_id=thread_id,
        occurred_after=occurred_after,
        occurred_before=occurred_before,
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
    query_embedding = await _raw_memory_query_embedding(normalized_query)
    async with surreal_content_client() as client:
        fulltext_memories: list[RawMemory] = []
        vector_memories: list[RawMemory] = []
        try:
            fulltext_memories = await _recall_raw_memory_fulltext(
                client,
                where_clause=where_clause,
                params=params,
                query=normalized_query,
                limit=limit,
            )
        except (RuntimeError, TimeoutError):
            fulltext_memories = []
        if query_embedding is not None:
            try:
                vector_memories = await _recall_raw_memory_vector(
                    client,
                    where_clause=where_clause,
                    params=params,
                    query_embedding=query_embedding,
                    limit=limit,
                )
            except (RuntimeError, TimeoutError):
                vector_memories = []
        memories = await _fuse_raw_memory_results(
            client,
            [fulltext_memories, vector_memories],
            limit=limit,
        )
        if memories:
            return memories
        return await _recall_raw_memory_lexical(
            client,
            organization_id=organization_id,
            principal_id=principal_id,
            query=normalized_query,
            memory_scope=normalized_scope,
            scope_key=scope_key,
            agent_id=agent_id,
            project_id=project_id,
            filters=filters,
            limit=limit,
        )


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
    return _recallable_memories(memories, limit=limit)


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
            "ORDER BY captured_at ASC LIMIT $limit;",
            organization_id=organization_id,
            candidate_surface="reflection_candidate",
            source_surface="reflection_source",
            limit=query_limit,
        )
    memories = [_raw_memory_from_record(row) for row in rows]
    return [
        memory
        for memory in memories
        if raw_memory_recallable(memory)
        and _raw_memory_capture_surface(memory) not in {"reflection_candidate", "reflection_source"}
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
                errors.append(str(exc))

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
                        "search::score(0) AS score "
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
    "ContentSearchRow",
    "ContentSource",
    "MemoryScope",
    "RawMemory",
    "build_surreal_content_client",
    "get_or_create_source",
    "get_raw_memory",
    "get_raw_memory_by_dedupe_key",
    "get_raw_memory_by_source_id",
    "lexical_score",
    "lexical_score_from_tokens",
    "list_raw_memories_for_promotion",
    "list_raw_memories_for_scope",
    "list_reflection_candidate_reviews",
    "list_reflection_dream_source_memories",
    "list_source_ids_for_org",
    "list_unlinked_document_chunks",
    "load_search_scope",
    "raw_memory_embedding_text",
    "raw_memory_recallable",
    "recall_raw_memory",
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
