"""Content records, lifecycle policy, serialization, and embeddings."""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

from sibyl_core.embeddings import content as content_embeddings
from sibyl_core.embeddings.providers import (
    DeterministicEmbeddingProvider,
    EmbeddingMetadata,
    EmbeddingProvider,
    create_embedding_provider,
)
from sibyl_core.memory_pipeline.lifecycle import raw_memory_lifecycle_recallable
from sibyl_core.memory_pipeline.quality import (
    expand_memory_quality_storage_metadata,
    normalize_memory_quality_metadata,
)
from sibyl_core.memory_pipeline.retrieval import CandidateSourceFailure, CandidateSourceResult
from sibyl_core.models.memory_scope import MemoryScope

_RAW_MEMORY_EMBEDDING_TEXT_MAX_CHARS = 12_000

_RAW_MEMORY_EMBEDDING_TEXT_TRUNCATION_MARKER = "\n...[truncated for raw memory embedding]..."

_RAW_MEMORY_EMBEDDING_TEXT_VERSION = "raw-capture-v1"

_MARK_OPEN = "<mark>"

_MARK_CLOSE = "</mark>"

_SNIPPET_MAX_CHARS = 320

_raw_memory_embedding_provider: EmbeddingProvider | None = None

_raw_memory_embedding_fingerprint: tuple[str, str, int, str] | None = None

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
    revision: int = 1
    captured_at: datetime | None = None
    deleted_at: datetime | None = None
    purge_after: datetime | None = None
    last_recalled_at: datetime | None = None
    last_used_at: datetime | None = None
    retrieval_count: int | None = None
    citation_count: int | None = None
    misled_count: int | None = None
    created_at: datetime | None = None
    score: float = 0.0
    snippet: str | None = None

    observed_revision: int | None = field(default=None, repr=False, compare=False)


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


def raw_memory_recallable(memory: RawMemory) -> bool:
    return raw_memory_lifecycle_recallable(memory)


def raw_memory_currently_recallable(memory: RawMemory) -> bool:
    return raw_memory_recallable(memory) and raw_memory_matches_as_of(memory, datetime.now(UTC))


def raw_memory_capture_surface(memory: RawMemory) -> str:
    metadata_surface = memory.metadata.get("capture_surface")
    value = memory.capture_surface if memory.capture_surface is not None else metadata_surface
    return str(value or "").strip().lower()


def normalize_raw_temporal_datetime(value: object | None) -> datetime | None:
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


def raw_memory_matches_as_of(memory: RawMemory, as_of: datetime | None) -> bool:
    if as_of is None:
        return True
    for value in (memory.created_at, memory.captured_at):
        observed_at = normalize_raw_temporal_datetime(value)
        if observed_at is not None and observed_at > as_of:
            return False
    for key in ("valid_at", "valid_from"):
        valid_at = normalize_raw_temporal_datetime(memory.metadata.get(key))
        if valid_at is not None and valid_at > as_of:
            return False
    for key in ("invalid_at", "valid_to"):
        invalid_at = normalize_raw_temporal_datetime(memory.metadata.get(key))
        if invalid_at is not None and invalid_at <= as_of:
            return False
    return True


def recallable_memories(
    memories: list[RawMemory],
    *,
    limit: int,
    as_of: datetime | None = None,
) -> list[RawMemory]:
    return [
        memory
        for memory in memories
        if raw_memory_recallable(memory) and raw_memory_matches_as_of(memory, as_of)
    ][:limit]


def normalize_records_preserving_id(result: object) -> list[SurrealRecord]:
    if result is None:
        return []
    if isinstance(result, dict):
        payload = {str(key): value for key, value in result.items()}
        if "result" in payload and ("status" in payload or "time" in payload):
            return normalize_records_preserving_id(payload.get("result"))
        statements = payload.get("result")
        if (
            "status" not in payload
            and isinstance(statements, list)
            and statements
            and all(isinstance(statement, dict) for statement in statements)
        ):
            return normalize_records_preserving_id(statements[-1])
        return [payload]
    if not isinstance(result, list):
        return []

    records: list[SurrealRecord] = []
    for item in result:
        records.extend(normalize_records_preserving_id(item))
    return records


def coerce_str(value: object | None, *, default: str = "") -> str:
    return str(value) if value is not None else default


def coerce_optional_str(value: object | None) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None


def coerce_highlight_value(value: object | None) -> str:
    if value is None:
        return ""
    if isinstance(value, list | tuple):
        return " ".join(str(item) for item in value if item is not None)
    return str(value)


def trim_search_snippet(text: str, *, max_chars: int = _SNIPPET_MAX_CHARS) -> str:
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


def search_snippet(
    value: object | None,
    *,
    fallback: object | None = None,
    max_chars: int = _SNIPPET_MAX_CHARS,
) -> str | None:
    highlighted = coerce_highlight_value(value)
    text = highlighted or coerce_highlight_value(fallback)
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return None
    return trim_search_snippet(text, max_chars=max_chars)


def search_snippet_from_values(
    values: Iterable[object | None],
    *,
    fallback: object | None = None,
    max_chars: int = _SNIPPET_MAX_CHARS,
) -> str | None:
    first_text: object | None = None
    for value in values:
        text = coerce_highlight_value(value)
        if not text.strip():
            continue
        first_text = first_text or value
        if _MARK_OPEN in text:
            return search_snippet(value, max_chars=max_chars)
    return search_snippet(first_text, fallback=fallback, max_chars=max_chars)


def coerce_int(value: object | None, *, default: int = 0) -> int:
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


def coerce_bool(value: object | None, *, default: bool = False) -> bool:
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


def coerce_datetime(value: object | None) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    if isinstance(value, str):
        normalized = value.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is not None:
            return parsed.astimezone(UTC).replace(tzinfo=None)
        return parsed
    return None


def coerce_dict(value: object | None) -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    return {str(key): item for key, item in value.items()}


def coerce_float(value: object | None, *, default: float = 0.0) -> float:
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


def coerce_str_list(value: object | None) -> list[str]:
    if isinstance(value, list | tuple):
        return [str(item) for item in value if item is not None]
    return []


def coerce_float_list(value: object | None) -> list[float] | None:
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


def embedding_vector_from_batch(
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


def raw_memory_embedding_metadata(metadata: EmbeddingMetadata) -> dict[str, str | int | bool]:
    payload = metadata.to_dict()
    payload["text_version"] = _RAW_MEMORY_EMBEDDING_TEXT_VERSION
    return payload


def reset_raw_memory_embedding_provider_cache() -> None:
    global _raw_memory_embedding_fingerprint, _raw_memory_embedding_provider
    _raw_memory_embedding_provider = None
    _raw_memory_embedding_fingerprint = None


def configured_raw_memory_embedding_provider() -> EmbeddingProvider | None:
    global _raw_memory_embedding_fingerprint, _raw_memory_embedding_provider
    dimensions = content_embeddings.configured_content_embedding_dimensions()
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

    config = content_embeddings.configured_content_embedding()
    if not config.api_key:
        return None

    if (
        _raw_memory_embedding_provider is None
        or config.fingerprint != _raw_memory_embedding_fingerprint
    ):
        _raw_memory_embedding_provider = create_embedding_provider(
            provider=config.provider,
            model=config.model,
            dimensions=config.dimensions,
            cache_namespace="raw-memory",
            api_key=config.api_key,
            max_cache_size=2000,
            tokenizer_estimate_method="provider-default",
        )
        _raw_memory_embedding_fingerprint = config.fingerprint
    return _raw_memory_embedding_provider


def coerce_memory_scope(value: object | None) -> MemoryScope:
    if isinstance(value, MemoryScope):
        return value
    if value is None:
        return MemoryScope.PRIVATE
    try:
        return MemoryScope(str(value))
    except ValueError:
        return MemoryScope.PRIVATE


def validate_raw_memory_scope(memory_scope: MemoryScope, scope_key: str | None) -> None:
    if memory_scope in _SCOPES_REQUIRING_SCOPE_KEY and not scope_key:
        msg = f"{memory_scope.value} raw memory requires a scope_key"
        raise ValueError(msg)


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def source_from_record(record: Mapping[str, object]) -> ContentSource:
    return ContentSource(
        id=coerce_str(record.get("uuid")),
        organization_id=coerce_str(record.get("organization_id")),
        name=coerce_str(record.get("name")),
        url=coerce_str(record.get("url")),
        source_type=coerce_str(record.get("source_type"), default="website"),
        description=coerce_optional_str(record.get("description")),
        crawl_depth=coerce_int(record.get("crawl_depth"), default=2),
        include_patterns=coerce_str_list(record.get("include_patterns")),
        exclude_patterns=coerce_str_list(record.get("exclude_patterns")),
        crawl_status=coerce_str(record.get("crawl_status"), default="pending"),
        current_job_id=coerce_optional_str(record.get("current_job_id")),
        last_error=coerce_optional_str(record.get("last_error")),
        created_at=coerce_datetime(record.get("created_at")),
        updated_at=coerce_datetime(record.get("updated_at")),
    )


def document_from_record(record: Mapping[str, object]) -> ContentDocument:
    return ContentDocument(
        id=coerce_str(record.get("uuid")),
        source_id=coerce_str(record.get("source_id")),
        url=coerce_str(record.get("url")),
        organization_id=coerce_str(record.get("organization_id")),
        title=coerce_str(record.get("title")),
        content=coerce_str(record.get("content")),
        has_code=coerce_bool(record.get("has_code")),
    )


def chunk_from_record(record: Mapping[str, object]) -> ContentChunk:
    return ContentChunk(
        id=coerce_str(record.get("uuid")),
        document_id=coerce_str(record.get("document_id")),
        organization_id=coerce_str(record.get("organization_id")),
        source_id=coerce_str(record.get("source_id")),
        chunk_index=coerce_int(record.get("chunk_index")),
        chunk_type=coerce_str(record.get("chunk_type"), default="text"),
        content=coerce_str(record.get("content")),
        context=coerce_optional_str(record.get("context")),
        heading_path=coerce_str_list(record.get("heading_path")),
        language=coerce_optional_str(record.get("language")),
        embedding=coerce_float_list(record.get("embedding")),
        has_entities=coerce_bool(record.get("has_entities")),
        entity_ids=coerce_str_list(record.get("entity_ids")),
        snippet=search_snippet(record.get("snippet"), fallback=record.get("content")),
    )


def raw_memory_from_record(record: Mapping[str, object]) -> RawMemory:
    observed_revision = record.get("revision")
    metadata = normalize_memory_quality_metadata(coerce_dict(record.get("metadata")))
    return RawMemory(
        id=coerce_str(record.get("uuid")),
        organization_id=coerce_str(record.get("organization_id")),
        source_id=coerce_str(record.get("source_id")),
        principal_id=coerce_str(record.get("principal_id")),
        memory_scope=coerce_memory_scope(record.get("memory_scope")),
        scope_key=coerce_optional_str(record.get("scope_key")),
        agent_id=coerce_optional_str(record.get("agent_id"))
        or coerce_optional_str(metadata.get("agent_id")),
        project_id=coerce_optional_str(record.get("project_id"))
        or coerce_optional_str(metadata.get("project_id")),
        review_state=coerce_str(
            record.get("review_state") or metadata.get("review_state"), default="pending"
        ),
        entity_id=coerce_optional_str(record.get("entity_id")),
        entity_type=coerce_str(record.get("entity_type"), default="raw_memory"),
        title=coerce_str(record.get("title")),
        raw_content=coerce_str(record.get("raw_content")),
        tags=coerce_str_list(record.get("tags")),
        embedding=coerce_float_list(record.get("embedding")),
        metadata=metadata,
        provenance=coerce_dict(record.get("provenance")),
        capture_surface=coerce_optional_str(record.get("capture_surface")),
        created_by_user_id=coerce_optional_str(record.get("created_by_user_id")),
        revision=max(coerce_int(record.get("revision")), 1),
        observed_revision=observed_revision
        if type(observed_revision) is int and observed_revision > 0
        else None,
        captured_at=coerce_datetime(record.get("captured_at")),
        deleted_at=coerce_datetime(record.get("deleted_at")),
        purge_after=coerce_datetime(record.get("purge_after")),
        last_recalled_at=coerce_datetime(record.get("last_recalled_at")),
        last_used_at=coerce_datetime(record.get("last_used_at")),
        retrieval_count=coerce_int(record.get("retrieval_count")),
        citation_count=coerce_int(record.get("citation_count")),
        misled_count=coerce_int(record.get("misled_count")),
        created_at=coerce_datetime(record.get("created_at")),
        score=coerce_float(record.get("score")),
        snippet=search_snippet_from_values(
            (
                record.get("content_snippet"),
                record.get("title_snippet"),
                record.get("snippet"),
            ),
            fallback=record.get("raw_content") or record.get("title"),
        ),
    )


def source_record(source: ContentSource) -> SurrealRecord:
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


def raw_memory_record(memory: RawMemory) -> SurrealRecord:
    metadata = expand_memory_quality_storage_metadata(memory.metadata)
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
        "revision": memory.revision,
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
    if memory.misled_count is not None:
        record["misled_count"] = memory.misled_count
        metadata["misled_count"] = memory.misled_count
    return record
