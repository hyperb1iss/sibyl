"""Surreal-backed content query helpers."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Iterable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sibyl import config as config_module
from sibyl.db.models import (
    CrawledDocument,
    CrawlSource,
    DocumentChunk,
    RawCapture,
)
from sibyl.persistence.content_common import CrawlStats, DocumentEntityRecord
from sibyl_core.backends.surreal import SurrealContentClient
from sibyl_core.backends.surreal.fulltext import build_fulltext_query
from sibyl_core.models import ChunkType, CrawlStatus, SourceType
from sibyl_core.services.link_graph_status import LinkGraphSourceStatusData, LinkGraphStatusData

_DEFAULT_BATCH_SIZE = 128
_DELETE_BY_UUID = {
    "crawl_sources": "DELETE FROM crawl_sources WHERE uuid = $uuid;",
    "crawled_documents": "DELETE FROM crawled_documents WHERE uuid = $uuid;",
    "document_chunks": "DELETE FROM document_chunks WHERE uuid = $uuid;",
    "raw_captures": "DELETE FROM raw_captures WHERE uuid = $uuid;",
}
_UPSERT_RECORD = {
    "crawl_sources": "UPSERT crawl_sources CONTENT $record WHERE uuid = $uuid;",
    "crawled_documents": "UPSERT crawled_documents CONTENT $record WHERE uuid = $uuid;",
    "document_chunks": "UPSERT document_chunks CONTENT $record WHERE uuid = $uuid;",
    "raw_captures": "UPSERT raw_captures CONTENT $record WHERE uuid = $uuid;",
}


@dataclass(slots=True)
class _SharedContentClientState:
    client: SurrealContentClient | None = None


_shared_content_client_state = _SharedContentClientState()
_shared_content_client_lock = asyncio.Lock()


async def check_relational_backend_health() -> dict[str, str | None]:
    return {"status": "disabled", "postgres_version": None, "pgvector_version": None}


def build_surreal_content_client() -> SurrealContentClient:
    """Build a Surreal content client from application settings."""

    return SurrealContentClient(
        url=config_module.settings.resolved_surreal_url,
        username=config_module.settings.surreal_username,
        password=config_module.settings.surreal_password.get_secret_value(),
        token=config_module.settings.surreal_token.get_secret_value(),
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
async def surreal_content_client() -> AsyncGenerator[SurrealContentClient]:
    yield await get_shared_surreal_content_client()


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


def _serialize_value(value: object) -> object:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value
    if hasattr(value, "value"):
        return _serialize_value(value.value)
    if isinstance(value, dict):
        return {str(key): _serialize_value(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_serialize_value(item) for item in value]
    return value


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _coerce_uuid(value: object | None, *, field_name: str) -> UUID:
    if isinstance(value, UUID):
        return value
    if isinstance(value, str) and value:
        return UUID(value)
    msg = f"{field_name} is required"
    raise TypeError(msg)


def _coerce_optional_uuid(value: object | None) -> UUID | None:
    if value is None or value == "":
        return None
    if isinstance(value, UUID):
        return value
    if isinstance(value, str):
        return UUID(value)
    return None


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


def _coerce_dict(value: object | None) -> dict[str, Any]:
    if isinstance(value, dict):
        return {str(key): item for key, item in value.items()}
    return {}


def _coerce_source_type(value: object | None) -> SourceType:
    try:
        return SourceType(_coerce_str(value, default=SourceType.WEBSITE.value))
    except ValueError:
        return SourceType.WEBSITE


def _coerce_crawl_status(value: object | None) -> CrawlStatus:
    try:
        return CrawlStatus(_coerce_str(value, default=CrawlStatus.PENDING.value))
    except ValueError:
        return CrawlStatus.PENDING


def _coerce_chunk_type(value: object | None) -> ChunkType:
    try:
        return ChunkType(_coerce_str(value, default=ChunkType.TEXT.value))
    except ValueError:
        return ChunkType.TEXT


def _sort_key(dt: datetime | None) -> tuple[int, str]:
    return (0 if dt is None else 1, dt.isoformat() if dt is not None else "")


def _source_record(source: CrawlSource) -> dict[str, Any]:
    return {
        "uuid": str(source.id),
        "organization_id": str(source.organization_id),
        "name": source.name,
        "url": source.url,
        "source_type": _serialize_value(source.source_type),
        "description": source.description,
        "crawl_depth": source.crawl_depth,
        "include_patterns": list(source.include_patterns or []),
        "exclude_patterns": list(source.exclude_patterns or []),
        "respect_robots": source.respect_robots,
        "crawl_status": _serialize_value(source.crawl_status),
        "current_job_id": source.current_job_id,
        "last_crawled_at": source.last_crawled_at,
        "last_error": source.last_error,
        "document_count": source.document_count,
        "chunk_count": source.chunk_count,
        "total_tokens": source.total_tokens,
        "tags": list(source.tags or []),
        "categories": list(source.categories or []),
        "favicon_url": source.favicon_url,
        "created_at": source.created_at,
        "updated_at": source.updated_at,
    }


def _source_from_record(record: dict[str, Any]) -> CrawlSource:
    now = datetime.now(UTC).replace(tzinfo=None)
    return CrawlSource(
        id=_coerce_uuid(record.get("uuid"), field_name="crawl_sources.uuid"),
        organization_id=_coerce_uuid(
            record.get("organization_id"),
            field_name="crawl_sources.organization_id",
        ),
        name=_coerce_str(record.get("name")),
        url=_coerce_str(record.get("url")),
        source_type=_coerce_source_type(record.get("source_type")),
        description=_coerce_optional_str(record.get("description")),
        crawl_depth=_coerce_int(record.get("crawl_depth"), default=2),
        include_patterns=_coerce_str_list(record.get("include_patterns")),
        exclude_patterns=_coerce_str_list(record.get("exclude_patterns")),
        respect_robots=_coerce_bool(record.get("respect_robots"), default=True),
        crawl_status=_coerce_crawl_status(record.get("crawl_status")),
        current_job_id=_coerce_optional_str(record.get("current_job_id")),
        last_crawled_at=_coerce_datetime(record.get("last_crawled_at")),
        last_error=_coerce_optional_str(record.get("last_error")),
        document_count=_coerce_int(record.get("document_count")),
        chunk_count=_coerce_int(record.get("chunk_count")),
        total_tokens=_coerce_int(record.get("total_tokens")),
        tags=_coerce_str_list(record.get("tags")),
        categories=_coerce_str_list(record.get("categories")),
        favicon_url=_coerce_optional_str(record.get("favicon_url")),
        created_at=_coerce_datetime(record.get("created_at")) or now,
        updated_at=_coerce_datetime(record.get("updated_at")) or now,
    )


def _document_from_record(record: dict[str, Any]) -> CrawledDocument:
    now = datetime.now(UTC).replace(tzinfo=None)
    return CrawledDocument(
        id=_coerce_uuid(record.get("uuid"), field_name="crawled_documents.uuid"),
        source_id=_coerce_uuid(record.get("source_id"), field_name="crawled_documents.source_id"),
        url=_coerce_str(record.get("url")),
        title=_coerce_str(record.get("title")),
        raw_content=_coerce_str(record.get("raw_content")),
        content=_coerce_str(record.get("content")),
        content_hash=_coerce_str(record.get("content_hash")),
        parent_url=_coerce_optional_str(record.get("parent_url")),
        section_path=_coerce_str_list(record.get("section_path")),
        depth=_coerce_int(record.get("depth")),
        language=_coerce_optional_str(record.get("language")),
        word_count=_coerce_int(record.get("word_count")),
        token_count=_coerce_int(record.get("token_count")),
        has_code=_coerce_bool(record.get("has_code")),
        is_index=_coerce_bool(record.get("is_index")),
        headings=_coerce_str_list(record.get("headings")),
        links=_coerce_str_list(record.get("links")),
        code_languages=_coerce_str_list(record.get("code_languages")),
        crawled_at=_coerce_datetime(record.get("crawled_at")) or now,
        http_status=_coerce_int(record.get("http_status")) if record.get("http_status") else None,
        created_at=_coerce_datetime(record.get("created_at")) or now,
        updated_at=_coerce_datetime(record.get("updated_at")) or now,
    )


def _document_record(document: CrawledDocument) -> dict[str, Any]:
    return {
        "uuid": str(document.id),
        "source_id": str(document.source_id),
        "url": document.url,
        "title": document.title,
        "raw_content": document.raw_content,
        "content": document.content,
        "content_hash": document.content_hash,
        "parent_url": document.parent_url,
        "section_path": list(document.section_path or []),
        "depth": document.depth,
        "language": document.language,
        "word_count": document.word_count,
        "token_count": document.token_count,
        "has_code": document.has_code,
        "is_index": document.is_index,
        "headings": list(document.headings or []),
        "links": list(document.links or []),
        "code_languages": list(document.code_languages or []),
        "crawled_at": document.crawled_at,
        "http_status": document.http_status,
        "created_at": document.created_at,
        "updated_at": document.updated_at,
    }


def _chunk_from_record(record: dict[str, Any]) -> DocumentChunk:
    now = datetime.now(UTC).replace(tzinfo=None)
    return DocumentChunk(
        id=_coerce_uuid(record.get("uuid"), field_name="document_chunks.uuid"),
        document_id=_coerce_uuid(
            record.get("document_id"), field_name="document_chunks.document_id"
        ),
        chunk_index=_coerce_int(record.get("chunk_index")),
        chunk_type=_coerce_chunk_type(record.get("chunk_type")),
        content=_coerce_str(record.get("content")),
        context=_coerce_optional_str(record.get("context")),
        token_count=_coerce_int(record.get("token_count")),
        start_char=_coerce_int(record.get("start_char")),
        end_char=_coerce_int(record.get("end_char")),
        heading_path=_coerce_str_list(record.get("heading_path")),
        embedding=_coerce_float_list(record.get("embedding")),
        language=_coerce_optional_str(record.get("language")),
        is_complete=_coerce_bool(record.get("is_complete"), default=True),
        has_entities=_coerce_bool(record.get("has_entities")),
        entity_ids=_coerce_str_list(record.get("entity_ids")),
        created_at=_coerce_datetime(record.get("created_at")) or now,
        updated_at=_coerce_datetime(record.get("updated_at")) or now,
    )


def _chunk_record(chunk: DocumentChunk) -> dict[str, Any]:
    return {
        "uuid": str(chunk.id),
        "document_id": str(chunk.document_id),
        "chunk_index": chunk.chunk_index,
        "chunk_type": _serialize_value(chunk.chunk_type),
        "content": chunk.content,
        "context": chunk.context,
        "token_count": chunk.token_count,
        "start_char": chunk.start_char,
        "end_char": chunk.end_char,
        "heading_path": list(chunk.heading_path or []),
        "embedding": _serialize_value(chunk.embedding),
        "language": chunk.language,
        "is_complete": chunk.is_complete,
        "has_entities": chunk.has_entities,
        "entity_ids": list(chunk.entity_ids or []),
        "created_at": chunk.created_at,
        "updated_at": chunk.updated_at,
    }


def _raw_capture_from_record(record: dict[str, Any]) -> RawCapture:
    now = datetime.now(UTC).replace(tzinfo=None)
    return RawCapture(
        id=_coerce_uuid(record.get("uuid"), field_name="raw_captures.uuid"),
        organization_id=_coerce_uuid(
            record.get("organization_id"),
            field_name="raw_captures.organization_id",
        ),
        entity_id=_coerce_optional_str(record.get("entity_id")),
        title=_coerce_str(record.get("title")),
        raw_content=_coerce_str(record.get("raw_content")),
        entity_type=_coerce_str(record.get("entity_type")),
        tags=_coerce_str_list(record.get("tags")),
        metadata_=_coerce_dict(record.get("metadata") or record.get("metadata_")),
        capture_surface=_coerce_optional_str(record.get("capture_surface")),
        created_by_user_id=_coerce_optional_uuid(record.get("created_by_user_id")),
        created_at=_coerce_datetime(record.get("created_at")) or now,
    )


def _raw_capture_record(capture: RawCapture) -> dict[str, Any]:
    return {
        "uuid": str(capture.id),
        "organization_id": str(capture.organization_id),
        "entity_id": capture.entity_id,
        "title": capture.title,
        "raw_content": capture.raw_content,
        "entity_type": capture.entity_type,
        "tags": list(capture.tags or []),
        "metadata": dict(capture.metadata_ or {}),
        "capture_surface": capture.capture_surface,
        "created_by_user_id": str(capture.created_by_user_id)
        if capture.created_by_user_id
        else None,
        "created_at": capture.created_at,
    }


def _page[T](items: Sequence[T], *, limit: int, offset: int) -> tuple[list[T], int]:
    total = len(items)
    return list(items[offset : offset + limit]), total


def _rrf_score(rank: int, *, k: float = 60.0) -> float:
    return 1.0 / (k + rank)


def _combined_hybrid_score(vector_rank: int | None, lexical_rank: int | None) -> float:
    score = 0.0
    if vector_rank is not None:
        score += _rrf_score(vector_rank)
    if lexical_rank is not None:
        score += _rrf_score(lexical_rank)
    return score


def _search_candidate_limit(limit: int) -> int:
    return min(max(limit * 5, limit, 1), 100)


def _code_chunk_clause(language: str | None) -> tuple[str, dict[str, Any]]:
    if not language:
        return " AND chunk_type = 'code' ", {}
    return (
        " AND chunk_type = 'code' AND string::lowercase(language ?? '') = $language ",
        {"language": language.lower()},
    )


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


def _where_equals(field: str, values: Sequence[str], *, prefix: str) -> tuple[str, dict[str, str]]:
    clauses: list[str] = []
    params: dict[str, str] = {}
    for index, value in enumerate(values):
        key = f"{prefix}_{index}"
        clauses.append(f"{field} = ${key}")
        params[key] = value
    return " OR ".join(clauses), params


async def _select_many(
    client: SurrealContentClient, query: str, **params: Any
) -> list[dict[str, Any]]:
    result = await client.execute_query(query, **params)
    error = _query_error(result)
    if error is not None:
        raise RuntimeError(error)
    return _normalize_records(result)


def _normalize_raw_statement_records(result: object, *, statement_index: int) -> list[dict[str, Any]]:
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
    *,
    statement_index: int = -1,
    **params: Any,
) -> list[dict[str, Any]]:
    execute_query_raw = getattr(client, "execute_query_raw", None)
    if execute_query_raw is not None:
        result = await execute_query_raw(query, **params)
    else:
        result = await client.execute_query(query, **params)
    error = _query_error(result)
    if error is not None:
        raise RuntimeError(error)
    return _normalize_raw_statement_records(result, statement_index=statement_index)


async def _select_one(
    client: SurrealContentClient, query: str, **params: Any
) -> dict[str, Any] | None:
    rows = await _select_many(client, query, **params)
    return rows[0] if rows else None


async def _replace_record(
    client: SurrealContentClient,
    table: str,
    *,
    uuid: UUID | str,
    record: dict[str, Any],
) -> dict[str, Any]:
    result = await client.execute_query(_UPSERT_RECORD[table], uuid=str(uuid), record=record)
    error = _query_error(result)
    if error is not None:
        raise RuntimeError(error)
    created = _normalize_records(result)
    if not created:
        msg = f"Failed to write {table} record {uuid}"
        raise RuntimeError(msg)
    return created[0]


async def _delete_record(
    client: SurrealContentClient,
    table: str,
    *,
    uuid: UUID | str,
) -> None:
    delete_result = await client.execute_query(_DELETE_BY_UUID[table], uuid=str(uuid))
    delete_error = _query_error(delete_result)
    if delete_error is not None:
        raise RuntimeError(delete_error)


async def _load_sources_for_org(
    client: SurrealContentClient,
    *,
    organization_id: UUID | str,
) -> list[CrawlSource]:
    rows = await _select_many(
        client,
        "SELECT * FROM crawl_sources WHERE organization_id = $organization_id;",
        organization_id=str(organization_id),
    )
    sources = [_source_from_record(row) for row in rows]
    return sorted(sources, key=lambda source: _sort_key(source.created_at), reverse=True)


async def _load_all_sources(client: SurrealContentClient) -> list[CrawlSource]:
    rows = await _select_many(client, "SELECT * FROM crawl_sources;")
    sources = [_source_from_record(row) for row in rows]
    return sorted(sources, key=lambda source: _sort_key(source.created_at), reverse=True)


async def _load_documents_for_source_ids(
    client: SurrealContentClient,
    source_ids: Sequence[str],
) -> list[CrawledDocument]:
    if not source_ids:
        return []

    rows: list[dict[str, Any]] = []
    for batch_index, batch in enumerate(_value_batches(source_ids), start=1):
        where_clause, params = _where_equals("source_id", batch, prefix=f"source_{batch_index}")
        rows.extend(
            await _select_many(
                client,
                f"SELECT * FROM crawled_documents WHERE {where_clause};",  # noqa: S608
                **params,
            )
        )
    return [_document_from_record(row) for row in rows]


async def _load_search_documents_by_ids(
    client: SurrealContentClient,
    document_ids: Sequence[str],
) -> list[CrawledDocument]:
    if not document_ids:
        return []

    rows: list[dict[str, Any]] = []
    for batch in _value_batches(document_ids):
        rows.extend(
            await _select_many(
                client,
                "SELECT uuid, source_id, url, title, has_code "
                "FROM crawled_documents WHERE uuid INSIDE $document_ids;",
                document_ids=batch,
            )
        )
    return [_document_from_record(row) for row in rows]


async def _load_chunks_for_document_ids(
    client: SurrealContentClient,
    document_ids: Sequence[str],
) -> list[DocumentChunk]:
    if not document_ids:
        return []

    rows: list[dict[str, Any]] = []
    for batch_index, batch in enumerate(_value_batches(document_ids), start=1):
        where_clause, params = _where_equals(
            "document_id",
            batch,
            prefix=f"document_{batch_index}",
        )
        rows.extend(
            await _select_many(
                client,
                f"SELECT * FROM document_chunks WHERE {where_clause};",  # noqa: S608
                **params,
            )
        )
    return [_chunk_from_record(row) for row in rows]


def _source_search_scope_clause(
    *,
    organization_id: UUID | str,
    source_id: UUID | None,
    source_name: str | None,
) -> tuple[str, dict[str, Any]]:
    clauses = ["organization_id = $organization_id"]
    params: dict[str, Any] = {"organization_id": str(organization_id)}
    if source_id is not None:
        clauses.append("uuid = $source_id")
        params["source_id"] = str(source_id)
    elif source_name is not None:
        normalized_source_name = build_fulltext_query(source_name.lower())
        if normalized_source_name:
            clauses.append("name @0@ $source_name")
            params["source_name"] = normalized_source_name
        else:
            clauses.append("uuid = $source_name_empty_sentinel")
            params["source_name_empty_sentinel"] = "__sibyl_empty_source_name__"
    return " AND ".join(clauses), params


def _document_ids_from_search_rows(
    *row_groups: Sequence[dict[str, Any]],
) -> list[str]:
    document_ids: set[str] = set()
    for rows in row_groups:
        for row in rows:
            document_id = row.get("document_id")
            if document_id is not None:
                document_ids.add(str(document_id))
    return sorted(document_ids)


def _hydrate_search_rows(
    rows: Sequence[dict[str, Any]],
    *,
    documents_by_id: dict[str, CrawledDocument],
    sources_by_id: dict[str, CrawlSource],
) -> list[tuple[DocumentChunk, CrawledDocument, str, UUID, float]]:
    hydrated: list[tuple[DocumentChunk, CrawledDocument, str, UUID, float]] = []
    for row in rows:
        chunk = _chunk_from_record(row)
        document = documents_by_id.get(str(chunk.document_id))
        if document is None:
            continue
        source = sources_by_id.get(str(document.source_id))
        if source is None:
            continue
        hydrated.append((chunk, document, source.name, source.id, float(row.get("score") or 0.0)))
    return hydrated


async def _load_sources_for_search_scope(
    client: SurrealContentClient,
    *,
    organization_id: UUID | str,
    source_id: UUID | None,
    source_name: str | None,
) -> list[CrawlSource]:
    where_clause, params = _source_search_scope_clause(
        organization_id=organization_id,
        source_id=source_id,
        source_name=source_name,
    )
    rows = await _select_many(
        client,
        f"SELECT * FROM crawl_sources WHERE {where_clause};",  # noqa: S608
        **params,
    )
    sources = [_source_from_record(row) for row in rows]
    return sorted(sources, key=lambda source: _sort_key(source.created_at), reverse=True)


async def list_crawl_sources_for_org(
    _session: Any,
    *,
    organization_id: UUID,
    status: CrawlStatus | None = None,
    limit: int,
) -> tuple[list[CrawlSource], int]:
    async with surreal_content_client() as client:
        sources = await _load_sources_for_org(client, organization_id=organization_id)

    if status is not None:
        sources = [source for source in sources if source.crawl_status == status]
    return list(sources[:limit]), len(sources)


async def get_org_crawl_source(
    _session: Any,
    *,
    source_id: UUID,
    organization_id: UUID,
) -> CrawlSource | None:
    async with surreal_content_client() as client:
        record = await _select_one(
            client,
            "SELECT * FROM crawl_sources "
            "WHERE uuid = $source_id AND organization_id = $organization_id LIMIT 1;",
            source_id=str(source_id),
            organization_id=str(organization_id),
        )
    return _source_from_record(record) if record is not None else None


async def get_crawl_source_by_id(
    _session: Any,
    *,
    source_id: UUID,
) -> CrawlSource | None:
    async with surreal_content_client() as client:
        record = await _select_one(
            client,
            "SELECT * FROM crawl_sources WHERE uuid = $source_id LIMIT 1;",
            source_id=str(source_id),
        )
    return _source_from_record(record) if record is not None else None


async def get_crawl_source_by_url(
    _session: Any,
    *,
    url: str,
) -> CrawlSource | None:
    async with surreal_content_client() as client:
        record = await _select_one(
            client,
            "SELECT * FROM crawl_sources WHERE url = $url LIMIT 1;",
            url=url.rstrip("/"),
        )
    return _source_from_record(record) if record is not None else None


async def get_crawl_stats_payload(
    _session: Any,
    *,
    organization_id: UUID,
) -> CrawlStats:
    async with surreal_content_client() as client:
        sources = await _load_sources_for_org(client, organization_id=organization_id)
        documents = await _load_documents_for_source_ids(
            client, [str(source.id) for source in sources]
        )
        chunks = await _load_chunks_for_document_ids(client, [str(doc.id) for doc in documents])

    status_counts: dict[str, int] = {}
    for source in sources:
        key = (
            source.crawl_status.value
            if hasattr(source.crawl_status, "value")
            else str(source.crawl_status)
        )
        status_counts[key] = status_counts.get(key, 0) + 1

    return CrawlStats(
        total_sources=len(sources),
        total_documents=len(documents),
        total_chunks=len(chunks),
        chunks_with_embeddings=sum(1 for chunk in chunks if chunk.embedding),
        sources_by_status=status_counts,
    )


async def list_crawled_documents_for_org(
    _session: Any,
    *,
    organization_id: UUID,
    limit: int,
    offset: int,
) -> tuple[list[CrawledDocument], int]:
    async with surreal_content_client() as client:
        sources = await _load_sources_for_org(client, organization_id=organization_id)
        documents = await _load_documents_for_source_ids(
            client, [str(source.id) for source in sources]
        )

    documents = sorted(documents, key=lambda doc: _sort_key(doc.crawled_at), reverse=True)
    return _page(documents, limit=limit, offset=offset)


async def list_crawl_sources(
    _session: Any,
    *,
    status: CrawlStatus | None = None,
    limit: int | None = 50,
) -> list[CrawlSource]:
    async with surreal_content_client() as client:
        sources = await _load_all_sources(client)

    if status is not None:
        sources = [source for source in sources if source.crawl_status == status]
    if limit is None:
        return list(sources)
    return list(sources[:limit])


async def create_crawl_source_record(
    _session: Any,
    *,
    name: str,
    url: str,
    organization_id: UUID,
    source_type: SourceType,
    description: str | None,
    crawl_depth: int,
    include_patterns: list[str] | None,
    exclude_patterns: list[str] | None,
) -> CrawlSource:
    from sibyl.crawler.service import SourceAlreadyExistsError

    normalized_url = url.rstrip("/")
    now = _utcnow()

    async with surreal_content_client() as client:
        existing = await _select_one(
            client,
            "SELECT * FROM crawl_sources "
            "WHERE url = $url AND organization_id = $organization_id LIMIT 1;",
            url=normalized_url,
            organization_id=str(organization_id),
        )
        if existing is not None:
            raise SourceAlreadyExistsError(normalized_url)

        source = CrawlSource(
            id=uuid4(),
            organization_id=organization_id,
            name=name,
            url=normalized_url,
            source_type=source_type,
            description=description,
            crawl_depth=crawl_depth,
            include_patterns=include_patterns or [],
            exclude_patterns=exclude_patterns or [],
            created_at=now,
            updated_at=now,
        )

        try:
            record = await _replace_record(
                client,
                "crawl_sources",
                uuid=source.id,
                record=_source_record(source),
            )
        except Exception as exc:
            duplicate = await _select_one(
                client,
                "SELECT * FROM crawl_sources WHERE url = $url LIMIT 1;",
                url=normalized_url,
            )
            if duplicate is not None:
                raise SourceAlreadyExistsError(normalized_url) from exc
            raise

    return _source_from_record(record)


async def get_crawled_document_for_org(
    _session: Any,
    *,
    document_id: UUID,
    organization_id: UUID,
) -> CrawledDocument | None:
    async with surreal_content_client() as client:
        record = await _select_one(
            client,
            "SELECT * FROM crawled_documents WHERE uuid = $document_id LIMIT 1;",
            document_id=str(document_id),
        )
        if record is None:
            return None
        document = _document_from_record(record)
        source = await _select_one(
            client,
            "SELECT * FROM crawl_sources "
            "WHERE uuid = $source_id AND organization_id = $organization_id LIMIT 1;",
            source_id=str(document.source_id),
            organization_id=str(organization_id),
        )

    return document if source is not None else None


async def save_crawl_source_record(
    _session: Any,
    *,
    source: CrawlSource,
) -> CrawlSource:
    source.updated_at = _utcnow()
    async with surreal_content_client() as client:
        record = await _replace_record(
            client,
            "crawl_sources",
            uuid=source.id,
            record=_source_record(source),
        )
    return _source_from_record(record)


async def list_document_chunks(
    _session: Any,
    *,
    document_id: UUID,
) -> list[DocumentChunk]:
    async with surreal_content_client() as client:
        rows = await _select_many(
            client,
            "SELECT * FROM document_chunks WHERE document_id = $document_id;",
            document_id=str(document_id),
        )
    chunks = [_chunk_from_record(row) for row in rows]
    return sorted(chunks, key=lambda chunk: chunk.chunk_index)


async def list_source_documents_page(
    _session: Any,
    *,
    source_id: UUID,
    limit: int,
    offset: int,
    has_code: bool | None = None,
    is_index: bool | None = None,
) -> tuple[list[CrawledDocument], int]:
    async with surreal_content_client() as client:
        rows = await _select_many(
            client,
            "SELECT * FROM crawled_documents WHERE source_id = $source_id;",
            source_id=str(source_id),
        )
    documents = [_document_from_record(row) for row in rows]
    if has_code is not None:
        documents = [doc for doc in documents if doc.has_code is has_code]
    if is_index is not None:
        documents = [doc for doc in documents if doc.is_index is is_index]
    documents = sorted(documents, key=lambda doc: _sort_key(doc.crawled_at), reverse=True)
    return _page(documents, limit=limit, offset=offset)


async def list_rag_source_documents_page(
    _session: Any,
    *,
    source_id: UUID,
    limit: int,
    offset: int,
    has_code: bool | None = None,
    is_index: bool | None = None,
) -> tuple[list[CrawledDocument], int]:
    async with surreal_content_client() as client:
        rows = await _select_many(
            client,
            "SELECT * FROM crawled_documents WHERE source_id = $source_id;",
            source_id=str(source_id),
        )
    documents = [_document_from_record(row) for row in rows]
    if has_code is not None:
        documents = [doc for doc in documents if doc.has_code is has_code]
    if is_index is not None:
        documents = [doc for doc in documents if doc.is_index is is_index]
    documents = sorted(documents, key=lambda doc: (_coerce_str(doc.title).lower(), str(doc.id)))
    return _page(documents, limit=limit, offset=offset)


async def list_source_chunks(
    _session: Any,
    *,
    source_id: UUID,
) -> list[DocumentChunk]:
    async with surreal_content_client() as client:
        documents = await _load_documents_for_source_ids(client, [str(source_id)])
        chunks = await _load_chunks_for_document_ids(client, [str(doc.id) for doc in documents])
    return sorted(
        chunks,
        key=lambda chunk: (str(chunk.document_id), chunk.chunk_index, str(chunk.id)),
    )


async def list_source_documents(
    _session: Any,
    *,
    source_id: UUID,
) -> list[CrawledDocument]:
    async with surreal_content_client() as client:
        rows = await _select_many(
            client,
            "SELECT * FROM crawled_documents WHERE source_id = $source_id;",
            source_id=str(source_id),
        )
    documents = [_document_from_record(row) for row in rows]
    return sorted(documents, key=lambda doc: _sort_key(doc.crawled_at), reverse=True)


async def get_link_graph_status_payload(
    _session: Any,
    *,
    organization_id: UUID,
) -> LinkGraphStatusData:
    async with surreal_content_client() as client:
        sources = await _load_sources_for_org(client, organization_id=organization_id)
        documents = await _load_documents_for_source_ids(
            client, [str(source.id) for source in sources]
        )
        chunks = await _load_chunks_for_document_ids(
            client, [str(document.id) for document in documents]
        )

    document_source_ids = {str(document.id): str(document.source_id) for document in documents}
    pending_by_source = {str(source.id): 0 for source in sources}
    chunks_with_entities = 0

    for chunk in chunks:
        if chunk.has_entities:
            chunks_with_entities += 1
            continue
        source_id = document_source_ids.get(str(chunk.document_id))
        if source_id is not None:
            pending_by_source[source_id] = pending_by_source.get(source_id, 0) + 1

    return LinkGraphStatusData(
        total_chunks=len(chunks),
        chunks_with_entities=chunks_with_entities,
        sources=[
            LinkGraphSourceStatusData(
                source_id=str(source.id),
                name=source.name,
                pending=pending_by_source[str(source.id)],
            )
            for source in sources
            if pending_by_source[str(source.id)] > 0
        ],
    )


async def get_source_sync_counts(
    session: Any,
    *,
    source_id: UUID,
) -> tuple[int, int]:
    documents = await list_source_documents(session, source_id=source_id)
    chunks = await list_source_chunks(session, source_id=source_id)
    return len(documents), len(chunks)


async def list_sources_for_graph_linking(
    _session: Any,
    *,
    organization_id: UUID,
    source_id: UUID | None = None,
) -> list[CrawlSource]:
    if source_id is not None:
        async with surreal_content_client() as client:
            record = await _select_one(
                client,
                "SELECT * FROM crawl_sources "
                "WHERE uuid = $source_id AND organization_id = $organization_id LIMIT 1;",
                source_id=str(source_id),
                organization_id=str(organization_id),
            )
        source = _source_from_record(record) if record is not None else None
        return [source] if source is not None else []

    async with surreal_content_client() as client:
        return await _load_sources_for_org(client, organization_id=organization_id)


async def list_unlinked_source_chunks(
    _session: Any,
    *,
    source_id: UUID,
    limit: int,
) -> list[DocumentChunk]:
    chunks = await list_source_chunks(None, source_id=source_id)
    pending = [chunk for chunk in chunks if not chunk.has_entities]
    return pending[:limit]


async def count_remaining_unlinked_chunks(
    _session: Any,
    *,
    organization_id: UUID,
    source_id: UUID | None = None,
) -> int:
    async with surreal_content_client() as client:
        if source_id is not None:
            documents = await _load_documents_for_source_ids(client, [str(source_id)])
        else:
            sources = await _load_sources_for_org(client, organization_id=organization_id)
            documents = await _load_documents_for_source_ids(
                client, [str(source.id) for source in sources]
            )
        chunks = await _load_chunks_for_document_ids(client, [str(doc.id) for doc in documents])
    return sum(1 for chunk in chunks if not chunk.has_entities)


async def list_raw_captures(
    _session: Any,
    *,
    organization_id: UUID,
    entity_type: str | None,
    capture_surface: str | None,
    review_state: str | None,
    limit: int,
    offset: int,
) -> tuple[list[RawCapture], bool]:
    async with surreal_content_client() as client:
        rows = await _select_many(
            client,
            "SELECT * FROM raw_captures WHERE organization_id = $organization_id;",
            organization_id=str(organization_id),
        )

    captures = [_raw_capture_from_record(row) for row in rows]
    if entity_type:
        captures = [capture for capture in captures if capture.entity_type == entity_type]
    if capture_surface:
        captures = [
            capture for capture in captures if (capture.capture_surface or "") == capture_surface
        ]
    if review_state:
        if review_state == "pending":
            captures = [
                capture
                for capture in captures
                if str((capture.metadata_ or {}).get("review_state") or "pending") == "pending"
            ]
        else:
            captures = [
                capture
                for capture in captures
                if str((capture.metadata_ or {}).get("review_state") or "") == review_state
            ]
    captures = sorted(captures, key=lambda capture: _sort_key(capture.created_at), reverse=True)
    paged = captures[offset : offset + limit + 1]
    return paged[:limit], len(paged) > limit


async def get_raw_capture(
    _session: Any,
    *,
    organization_id: UUID,
    capture_id: UUID,
) -> RawCapture | None:
    async with surreal_content_client() as client:
        record = await _select_one(
            client,
            "SELECT * FROM raw_captures "
            "WHERE uuid = $capture_id AND organization_id = $organization_id LIMIT 1;",
            capture_id=str(capture_id),
            organization_id=str(organization_id),
        )
    return _raw_capture_from_record(record) if record is not None else None


async def save_raw_capture_record(
    _session: Any,
    *,
    capture: RawCapture,
) -> RawCapture:
    async with surreal_content_client() as client:
        record = await _replace_record(
            client,
            "raw_captures",
            uuid=capture.id,
            record=_raw_capture_record(capture),
        )
    return _raw_capture_from_record(record)


async def resolve_document_entity(
    _session: Any,
    *,
    organization_id: UUID,
    entity_id: str,
) -> DocumentEntityRecord | None:
    async with surreal_content_client() as client:
        sources = await _load_sources_for_org(client, organization_id=organization_id)
        source_ids = {str(source.id) for source in sources}
        documents = await _load_documents_for_source_ids(client, list(source_ids))
        documents_by_id = {str(document.id): document for document in documents}
        sources_by_id = {str(source.id): source for source in sources}
        chunks = await _load_chunks_for_document_ids(client, list(documents_by_id))

    chunk: DocumentChunk | None = None
    try:
        chunk_uuid = UUID(entity_id)
        chunk = next((item for item in chunks if item.id == chunk_uuid), None)
    except ValueError:
        normalized_prefix = entity_id.lower().replace("-", "")
        if len(normalized_prefix) >= 4 and all(
            char in "0123456789abcdef" for char in normalized_prefix
        ):
            chunk = next(
                (
                    item
                    for item in chunks
                    if str(item.id).replace("-", "").lower().startswith(normalized_prefix)
                ),
                None,
            )

    if chunk is None:
        return None

    document = documents_by_id.get(str(chunk.document_id))
    if document is None:
        return None
    source = sources_by_id.get(str(document.source_id))
    if source is None:
        return None

    content = chunk.content or ""
    if chunk.chunk_type == ChunkType.HEADING:
        following_chunks = sorted(
            [
                item
                for item in chunks
                if item.document_id == chunk.document_id and item.chunk_index > chunk.chunk_index
            ],
            key=lambda item: item.chunk_index,
        )
        section_parts = [content]
        for following_chunk in following_chunks:
            if following_chunk.chunk_type == ChunkType.HEADING:
                break
            section_parts.append(following_chunk.content or "")
        content = "\n\n".join(section_parts)

    return DocumentEntityRecord(
        chunk=chunk,
        document=document,
        source=source,
        content=content,
    )


async def get_document_by_url_for_org(
    _session: Any,
    *,
    url: str,
    organization_id: UUID | str,
) -> CrawledDocument | None:
    async with surreal_content_client() as client:
        record = await _select_one(
            client,
            "SELECT * FROM crawled_documents WHERE url = $url LIMIT 1;",
            url=url,
        )
        if record is None:
            return None
        document = _document_from_record(record)
        source = await _select_one(
            client,
            "SELECT * FROM crawl_sources "
            "WHERE uuid = $source_id AND organization_id = $organization_id LIMIT 1;",
            source_id=str(document.source_id),
            organization_id=str(organization_id),
        )
    return document if source is not None else None


async def save_crawled_document_record(
    _session: Any,
    *,
    document: CrawledDocument,
) -> CrawledDocument:
    document.updated_at = _utcnow()
    async with surreal_content_client() as client:
        record = await _replace_record(
            client,
            "crawled_documents",
            uuid=document.id,
            record=_document_record(document),
        )
    return _document_from_record(record)


async def save_document_chunks(
    _session: Any,
    *,
    chunks: list[DocumentChunk],
) -> list[DocumentChunk]:
    async with surreal_content_client() as client:
        saved: list[DocumentChunk] = []
        for chunk in chunks:
            chunk.updated_at = _utcnow()
            record = await _replace_record(
                client,
                "document_chunks",
                uuid=chunk.id,
                record=_chunk_record(chunk),
            )
            saved.append(_chunk_from_record(record))
    return saved


async def delete_crawled_document_record(
    _session: Any,
    *,
    document_id: UUID,
    organization_id: UUID,
) -> tuple[CrawledDocument, int] | None:
    async with surreal_content_client() as client:
        record = await _select_one(
            client,
            "SELECT * FROM crawled_documents WHERE uuid = $document_id LIMIT 1;",
            document_id=str(document_id),
        )
        if record is None:
            return None

        document = _document_from_record(record)
        source_record = await _select_one(
            client,
            "SELECT * FROM crawl_sources "
            "WHERE uuid = $source_id AND organization_id = $organization_id LIMIT 1;",
            source_id=str(document.source_id),
            organization_id=str(organization_id),
        )
        if source_record is None:
            return None

        source = _source_from_record(source_record)
        chunk_rows = await _select_many(
            client,
            "SELECT * FROM document_chunks WHERE document_id = $document_id;",
            document_id=str(document_id),
        )
        chunks_deleted = len(chunk_rows)

        chunk_delete_result = await client.execute_query(
            "DELETE FROM document_chunks WHERE document_id = $document_id;",
            document_id=str(document_id),
        )
        chunk_delete_error = _query_error(chunk_delete_result)
        if chunk_delete_error is not None:
            raise RuntimeError(chunk_delete_error)
        await _delete_record(client, "crawled_documents", uuid=document.id)

        source.document_count = max(0, source.document_count - 1)
        source.chunk_count = max(0, source.chunk_count - chunks_deleted)
        source.updated_at = _utcnow()
        await _replace_record(
            client,
            "crawl_sources",
            uuid=source.id,
            record=_source_record(source),
        )

    return document, chunks_deleted


async def delete_crawl_source_record(
    _session: Any,
    *,
    source_id: UUID,
    organization_id: UUID,
) -> CrawlSource | None:
    async with surreal_content_client() as client:
        source_record = await _select_one(
            client,
            "SELECT * FROM crawl_sources "
            "WHERE uuid = $source_id AND organization_id = $organization_id LIMIT 1;",
            source_id=str(source_id),
            organization_id=str(organization_id),
        )
        if source_record is None:
            return None

        source = _source_from_record(source_record)
        document_rows = await _select_many(
            client,
            "SELECT * FROM crawled_documents WHERE source_id = $source_id;",
            source_id=str(source_id),
        )
        document_ids = [
            str(document_row["uuid"])
            for document_row in document_rows
            if document_row.get("uuid") is not None
        ]
        if document_ids:
            chunk_delete_result = await client.execute_query(
                "DELETE FROM document_chunks WHERE document_id IN $document_ids;",
                document_ids=document_ids,
            )
            chunk_delete_error = _query_error(chunk_delete_result)
            if chunk_delete_error is not None:
                raise RuntimeError(chunk_delete_error)

            document_delete_result = await client.execute_query(
                "DELETE FROM crawled_documents WHERE source_id = $source_id;",
                source_id=str(source_id),
            )
            document_delete_error = _query_error(document_delete_result)
            if document_delete_error is not None:
                raise RuntimeError(document_delete_error)

        await _delete_record(client, "crawl_sources", uuid=source.id)

    return source


async def _load_search_scope(
    *,
    organization_id: UUID | str,
    source_id: UUID | None,
    source_name: str | None,
) -> tuple[
    list[CrawlSource], dict[str, CrawlSource], dict[str, CrawledDocument], list[DocumentChunk]
]:
    async with surreal_content_client() as client:
        sources = await _load_sources_for_search_scope(
            client,
            organization_id=organization_id,
            source_id=source_id,
            source_name=source_name,
        )
        source_ids = [str(source.id) for source in sources]
        documents = await _load_documents_for_source_ids(client, source_ids)
        chunks = await _load_chunks_for_document_ids(
            client, [str(document.id) for document in documents]
        )

    sources_by_id = {str(source.id): source for source in sources}
    documents_by_id = {str(document.id): document for document in documents}
    return sources, sources_by_id, documents_by_id, chunks


async def search_rag_chunks(
    _session: Any,
    *,
    query_embedding: list[float],
    organization_id: UUID | str,
    similarity_threshold: float,
    match_count: int,
    source_id: UUID | None = None,
    source_name: str | None = None,
) -> list[Any]:
    if match_count <= 0:
        return []

    candidate_limit = _search_candidate_limit(match_count)
    async with surreal_content_client() as client:
        sources = await _load_sources_for_search_scope(
            client,
            organization_id=organization_id,
            source_id=source_id,
            source_name=source_name,
        )
        if not sources:
            return []

        source_ids = [str(source.id) for source in sources]
        sources_by_id = {str(source.id): source for source in sources}
        rows = await _select_many_raw(
            client,
            "LET $document_ids = ("  # noqa: S608
            "SELECT VALUE uuid FROM crawled_documents WHERE source_id INSIDE $source_ids"
            ");"
            "SELECT * FROM ("
            "SELECT uuid, document_id, chunk_index, chunk_type, content, context, "
            "heading_path, language, has_entities, entity_ids, "
            "(1 - vector::distance::knn()) AS score "
            "FROM document_chunks WHERE document_id INSIDE $document_ids "
            f"AND embedding <|{candidate_limit}, 40|> $query_embedding"
            ") WHERE score >= $similarity_threshold "
            "ORDER BY score DESC LIMIT $candidate_limit;",
            source_ids=source_ids,
            query_embedding=query_embedding,
            similarity_threshold=similarity_threshold,
            candidate_limit=candidate_limit,
        )
        documents = await _load_search_documents_by_ids(
            client, _document_ids_from_search_rows(rows)
        )
        documents_by_id = {str(document.id): document for document in documents}

    return _hydrate_search_rows(
        rows,
        documents_by_id=documents_by_id,
        sources_by_id=sources_by_id,
    )[:match_count]


async def search_code_example_chunks(
    _session: Any,
    *,
    query_embedding: list[float],
    organization_id: UUID | str,
    match_count: int,
    source_id: UUID | None = None,
    language: str | None = None,
) -> list[Any]:
    if match_count <= 0:
        return []

    candidate_limit = _search_candidate_limit(match_count)
    language_clause, language_params = _code_chunk_clause(language)
    async with surreal_content_client() as client:
        sources = await _load_sources_for_search_scope(
            client,
            organization_id=organization_id,
            source_id=source_id,
            source_name=None,
        )
        if not sources:
            return []

        source_ids = [str(source.id) for source in sources]
        sources_by_id = {str(source.id): source for source in sources}
        rows = await _select_many_raw(
            client,
            "LET $document_ids = ("  # noqa: S608
            "SELECT VALUE uuid FROM crawled_documents WHERE source_id INSIDE $source_ids"
            ");"
            "SELECT * FROM ("
            "SELECT uuid, document_id, chunk_index, chunk_type, content, context, "
            "heading_path, language, has_entities, entity_ids, "
            "(1 - vector::distance::knn()) AS score "
            "FROM document_chunks WHERE document_id INSIDE $document_ids"
            f"{language_clause} "
            f"AND embedding <|{candidate_limit}, 40|> $query_embedding "
            ") "
            "ORDER BY score DESC LIMIT $candidate_limit;",
            source_ids=source_ids,
            query_embedding=query_embedding,
            candidate_limit=candidate_limit,
            **language_params,
        )
        documents = await _load_search_documents_by_ids(
            client, _document_ids_from_search_rows(rows)
        )
        documents_by_id = {str(document.id): document for document in documents}

    return [
        (chunk, document, source_id, source_name, score)
        for chunk, document, source_name, source_id, score in _hydrate_search_rows(
            rows,
            documents_by_id=documents_by_id,
            sources_by_id=sources_by_id,
        )
    ][:match_count]


async def hybrid_search_chunks(
    _session: Any,
    *,
    query_text: str,
    query_embedding: list[float],
    organization_id: UUID | str,
    similarity_threshold: float,
    match_count: int,
    source_id: UUID | None = None,
    source_name: str | None = None,
) -> list[Any]:
    if match_count <= 0:
        return []

    candidate_limit = _search_candidate_limit(match_count)
    async with surreal_content_client() as client:
        sources = await _load_sources_for_search_scope(
            client,
            organization_id=organization_id,
            source_id=source_id,
            source_name=source_name,
        )
        if not sources:
            return []

        source_ids = [str(source.id) for source in sources]
        sources_by_id = {str(source.id): source for source in sources}
        vector_rows = await _select_many_raw(
            client,
            "LET $document_ids = ("  # noqa: S608
            "SELECT VALUE uuid FROM crawled_documents WHERE source_id INSIDE $source_ids"
            ");"
            "SELECT * FROM ("
            "SELECT uuid, document_id, chunk_index, chunk_type, content, context, "
            "heading_path, language, has_entities, entity_ids, "
            "(1 - vector::distance::knn()) AS score "
            "FROM document_chunks WHERE document_id INSIDE $document_ids "
            f"AND embedding <|{candidate_limit}, 40|> $query_embedding"
            ") WHERE score >= $similarity_threshold "
            "ORDER BY score DESC LIMIT $candidate_limit;",
            source_ids=source_ids,
            query_embedding=query_embedding,
            similarity_threshold=similarity_threshold,
            candidate_limit=candidate_limit,
        )
        lexical_rows = await _select_many_raw(
            client,
            "LET $document_ids = ("
            "SELECT VALUE uuid FROM crawled_documents WHERE source_id INSIDE $source_ids"
            ");"
            "SELECT uuid, document_id, chunk_index, chunk_type, content, context, "
            "heading_path, language, has_entities, entity_ids, search::score(0) AS score "
            "FROM document_chunks WHERE document_id INSIDE $document_ids "
            "AND content @0@ $search_query "
            "ORDER BY score DESC LIMIT $candidate_limit;",
            source_ids=source_ids,
            search_query=query_text.strip(),
            candidate_limit=candidate_limit,
        )
        document_ids = _document_ids_from_search_rows(vector_rows, lexical_rows)
        documents = await _load_search_documents_by_ids(client, document_ids)
        documents_by_id = {str(document.id): document for document in documents}

    vector_ranks = {
        str(row.get("uuid")): index for index, row in enumerate(vector_rows, start=1)
    }
    lexical_ranks = {
        str(row.get("uuid")): index for index, row in enumerate(lexical_rows, start=1)
    }
    vector_by_id = {str(row.get("uuid")): row for row in vector_rows}
    lexical_by_id = {str(row.get("uuid")): row for row in lexical_rows}

    combined: list[tuple[DocumentChunk, CrawledDocument, str, UUID, float, float]] = []
    for chunk_id in set(vector_by_id) | set(lexical_by_id):
        row = vector_by_id.get(chunk_id) or lexical_by_id.get(chunk_id)
        if row is None:
            continue
        chunk = _chunk_from_record(row)
        document = documents_by_id.get(str(chunk.document_id))
        if document is None:
            continue
        source = sources_by_id.get(str(document.source_id))
        if source is None:
            continue
        similarity = float(vector_by_id.get(chunk_id, {}).get("score") or 0.0)
        lexical = float(lexical_by_id.get(chunk_id, {}).get("score") or 0.0)
        combined.append((chunk, document, source.name, source.id, similarity, lexical))

    combined.sort(
        key=lambda row: (
            _combined_hybrid_score(
                vector_ranks.get(str(row[0].id)), lexical_ranks.get(str(row[0].id))
            ),
            row[4],
            row[5],
        ),
        reverse=True,
    )
    return combined[:match_count]
