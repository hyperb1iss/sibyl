"""Surreal-backed content helpers shared by core services."""

from __future__ import annotations

import re
from collections.abc import Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from sibyl_core.backends.surreal import SurrealContentClient
from sibyl_core.config import settings

_DEFAULT_BATCH_SIZE = 128
_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_]+")
_DELETE_BY_UUID = {
    "crawl_sources": "DELETE FROM crawl_sources WHERE uuid = $uuid;",
    "crawled_documents": "DELETE FROM crawled_documents WHERE uuid = $uuid;",
    "raw_captures": "DELETE FROM raw_captures WHERE uuid = $uuid;",
}
_CREATE_RECORD = {
    "crawl_sources": "CREATE crawl_sources CONTENT $record;",
    "crawled_documents": "CREATE crawled_documents CONTENT $record;",
    "raw_captures": "CREATE raw_captures CONTENT $record;",
}
AGENT_DIARY_CAPTURE_SURFACE = "agent_diary"


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
    title: str = ""
    content: str = ""
    has_code: bool = False


@dataclass(slots=True)
class ContentChunk:
    id: str
    document_id: str
    chunk_index: int = 0
    chunk_type: str = "text"
    content: str = ""
    context: str | None = None
    heading_path: list[str] = field(default_factory=list)
    language: str | None = None
    embedding: list[float] | None = None
    has_entities: bool = False
    entity_ids: list[str] = field(default_factory=list)


@dataclass(slots=True)
class RawMemory:
    id: str
    organization_id: str
    source_id: str
    principal_id: str
    memory_scope: MemoryScope = MemoryScope.PRIVATE
    scope_key: str | None = None
    title: str = ""
    raw_content: str = ""
    tags: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    provenance: dict[str, Any] = field(default_factory=dict)
    capture_surface: str | None = None
    captured_at: datetime | None = None
    created_at: datetime | None = None
    score: float = 0.0


def build_surreal_content_client() -> SurrealContentClient:
    return SurrealContentClient(
        url=settings.resolved_surreal_url,
        username=settings.surreal_username,
        password=settings.surreal_password.get_secret_value(),
        token=settings.surreal_token.get_secret_value(),
    )


@asynccontextmanager
async def surreal_content_client() -> Any:
    client = build_surreal_content_client()
    try:
        yield client
    finally:
        await client.close()


def _normalize_record(record: Any) -> dict[str, Any] | None:
    if not isinstance(record, dict):
        return None
    if "result" in record and ("status" in record or "time" in record):
        return None
    out = dict(record)
    out.pop("id", None)
    return out


def _normalize_records(result: Any) -> list[dict[str, Any]]:
    if result is None:
        return []
    if isinstance(result, dict):
        if "result" in result and ("status" in result or "time" in result):
            return _normalize_records(result.get("result"))
        record = _normalize_record(result)
        return [record] if record is not None else []
    if not isinstance(result, list):
        return []

    records: list[dict[str, Any]] = []
    for item in result:
        records.extend(_normalize_records(item))
    return records


def _query_error(result: object) -> str | None:
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        payload = {str(key): value for key, value in result.items()}
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


def _coerce_dict(value: object | None) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {str(key): item for key, item in value.items()}


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


def _source_from_record(record: dict[str, Any]) -> ContentSource:
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


def _document_from_record(record: dict[str, Any]) -> ContentDocument:
    return ContentDocument(
        id=_coerce_str(record.get("uuid")),
        source_id=_coerce_str(record.get("source_id")),
        url=_coerce_str(record.get("url")),
        title=_coerce_str(record.get("title")),
        content=_coerce_str(record.get("content")),
        has_code=_coerce_bool(record.get("has_code")),
    )


def _chunk_from_record(record: dict[str, Any]) -> ContentChunk:
    return ContentChunk(
        id=_coerce_str(record.get("uuid")),
        document_id=_coerce_str(record.get("document_id")),
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


def _raw_memory_from_record(record: dict[str, Any]) -> RawMemory:
    return RawMemory(
        id=_coerce_str(record.get("uuid")),
        organization_id=_coerce_str(record.get("organization_id")),
        source_id=_coerce_str(record.get("source_id")),
        principal_id=_coerce_str(record.get("principal_id")),
        memory_scope=_coerce_memory_scope(record.get("memory_scope")),
        scope_key=_coerce_optional_str(record.get("scope_key")),
        title=_coerce_str(record.get("title")),
        raw_content=_coerce_str(record.get("raw_content")),
        tags=_coerce_str_list(record.get("tags")),
        metadata=_coerce_dict(record.get("metadata")),
        provenance=_coerce_dict(record.get("provenance")),
        capture_surface=_coerce_optional_str(record.get("capture_surface")),
        captured_at=_coerce_datetime(record.get("captured_at")),
        created_at=_coerce_datetime(record.get("created_at")),
        score=float(record.get("score") or 0.0),
    )


def _source_record(source: ContentSource) -> dict[str, Any]:
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


def _raw_memory_record(memory: RawMemory) -> dict[str, Any]:
    return {
        "uuid": memory.id,
        "organization_id": memory.organization_id,
        "source_id": memory.source_id,
        "principal_id": memory.principal_id,
        "memory_scope": memory.memory_scope.value,
        "scope_key": memory.scope_key,
        "title": memory.title,
        "raw_content": memory.raw_content,
        "entity_type": "raw_memory",
        "tags": list(memory.tags),
        "metadata": dict(memory.metadata),
        "provenance": dict(memory.provenance),
        "capture_surface": memory.capture_surface,
        "created_by_user_id": memory.principal_id,
        "captured_at": memory.captured_at,
        "created_at": memory.created_at,
    }


async def _select_many(
    client: SurrealContentClient, query: str, **params: Any
) -> list[dict[str, Any]]:
    result = await client.execute_query(query, **params)
    error = _query_error(result)
    if error is not None:
        raise RuntimeError(error)
    return _normalize_records(result)


async def _select_one(
    client: SurrealContentClient, query: str, **params: Any
) -> dict[str, Any] | None:
    rows = await _select_many(client, query, **params)
    return rows[0] if rows else None


async def _delete_record(client: SurrealContentClient, table: str, *, uuid: str) -> None:
    result = await client.execute_query(_DELETE_BY_UUID[table], uuid=uuid)
    error = _query_error(result)
    if error is not None:
        raise RuntimeError(error)


async def _replace_record(
    client: SurrealContentClient,
    table: str,
    *,
    uuid: str,
    record: dict[str, Any],
) -> dict[str, Any]:
    await _delete_record(client, table, uuid=uuid)
    rows = await _select_many(client, _CREATE_RECORD[table], record=record)
    if rows:
        return rows[0]
    created = await _select_one(
        client, f"SELECT * FROM {table} WHERE uuid = $uuid LIMIT 1;", uuid=uuid
    )
    if created is None:
        raise RuntimeError(f"failed to persist {table} record {uuid}")
    return created


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


async def _load_documents_for_source_ids(
    client: SurrealContentClient,
    source_ids: list[str],
) -> list[ContentDocument]:
    rows: list[dict[str, Any]] = []
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


async def _load_chunks_for_document_ids(
    client: SurrealContentClient,
    document_ids: list[str],
) -> list[ContentChunk]:
    rows: list[dict[str, Any]] = []
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
) -> tuple[str, dict[str, Any]]:
    _validate_raw_memory_scope(memory_scope, scope_key)
    clauses = [
        "organization_id = $organization_id",
        "memory_scope = $memory_scope",
    ]
    params: dict[str, Any] = {
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
        clauses.append("metadata.agent_id = $agent_id")
        params["agent_id"] = agent_id
    else:
        clauses.append("(capture_surface != $agent_diary_surface OR capture_surface = NONE)")
        params["agent_diary_surface"] = AGENT_DIARY_CAPTURE_SURFACE
    if project_id:
        clauses.append("metadata.project_id = $project_id")
        params["project_id"] = project_id
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
    limit: int,
) -> list[RawMemory]:
    where_clause, params = _memory_scope_where(
        organization_id=organization_id,
        principal_id=principal_id,
        memory_scope=memory_scope,
        scope_key=scope_key,
        agent_id=agent_id,
        project_id=project_id,
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
        if memory.score > 0:
            scored.append(memory)
    return sorted(scored, key=lambda memory: (-memory.score, memory.captured_at or datetime.min))[
        :limit
    ]


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
    metadata: dict[str, Any] | None = None,
    provenance: dict[str, Any] | None = None,
    capture_surface: str | None = None,
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
        title=title,
        raw_content=raw_content,
        tags=list(tags or []),
        metadata=dict(metadata or {}),
        provenance=dict(provenance or {}),
        capture_surface=capture_surface,
        captured_at=now,
        created_at=now,
    )
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
    limit: int = 10,
) -> list[RawMemory]:
    normalized_query = query.strip()
    if not normalized_query or limit <= 0:
        return []

    normalized_scope = _coerce_memory_scope(memory_scope)
    where_clause, params = _memory_scope_where(
        organization_id=organization_id,
        principal_id=principal_id,
        memory_scope=normalized_scope,
        scope_key=scope_key,
        agent_id=agent_id,
        project_id=project_id,
    )
    async with surreal_content_client() as client:
        try:
            rows = await _select_many(
                client,
                "SELECT *, math::max([search::score(0), search::score(1)]) AS score "
                f"FROM raw_captures WHERE {where_clause} "
                "AND (title @0@ $search_query OR raw_content @1@ $search_query) "
                "ORDER BY score DESC, captured_at DESC LIMIT $limit;",
                **params,
                search_query=normalized_query,
                limit=limit,
            )
        except RuntimeError:
            return await _recall_raw_memory_lexical(
                client,
                organization_id=organization_id,
                principal_id=principal_id,
                query=normalized_query,
                memory_scope=normalized_scope,
                scope_key=scope_key,
                agent_id=agent_id,
                project_id=project_id,
                limit=limit,
            )
    return [_raw_memory_from_record(row) for row in rows]


async def get_or_create_source(
    url: str,
    depth: int,
    data: dict[str, Any],
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
        sources = await _load_sources_for_org(client, organization_id=organization_id)
        if source_id is not None:
            sources = [source for source in sources if source.id == source_id]
        elif source_name:
            needle = source_name.strip().lower()
            sources = [source for source in sources if needle in source.name.lower()]

        source_ids = [source.id for source in sources]
        documents = await _load_documents_for_source_ids(client, source_ids)
        chunks = await _load_chunks_for_document_ids(
            client, [document.id for document in documents]
        )

    sources_by_id = {source.id: source for source in sources}
    documents_by_id = {document.id: document for document in documents}
    return sources, sources_by_id, documents_by_id, chunks


async def list_unlinked_document_chunks(
    *,
    organization_id: str,
    source_id: str | None = None,
    limit: int = 1000,
) -> list[ContentChunk]:
    _, _, _, chunks = await load_search_scope(
        organization_id=organization_id,
        source_id=source_id,
        source_name=None,
    )
    return [chunk for chunk in chunks if not chunk.has_entities][:limit]


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
    "ContentSource",
    "MemoryScope",
    "RawMemory",
    "build_surreal_content_client",
    "get_or_create_source",
    "lexical_score",
    "lexical_score_from_tokens",
    "list_source_ids_for_org",
    "list_unlinked_document_chunks",
    "load_search_scope",
    "recall_raw_memory",
    "remember_raw_memory",
    "set_source_job_state",
    "source_exists",
    "surreal_content_client",
    "tokenize",
    "tokenize_fields",
]
