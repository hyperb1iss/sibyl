"""Content source, document, passage, import, and search operations."""

from __future__ import annotations

import re
from uuid import uuid4

from sibyl_core.backends.surreal import SurrealContentClient
from sibyl_core.backends.surreal.fulltext import (
    build_fulltext_query,
)
from sibyl_core.backends.surreal.knn import knn_search_effort
from sibyl_core.services import content_client
from sibyl_core.services import content_models as models
from sibyl_core.services.content_models import ContentChunk, ContentDocument, ContentSource
from sibyl_core.utils.resilience import with_timeout

_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_]+")

_DOCUMENT_CHUNK_SELECT = (
    "uuid, organization_id, source_id, document_id, chunk_index, chunk_type, content, context, "
    "heading_path, language, has_entities, entity_ids"
)

ContentSearchRow = tuple[ContentChunk, ContentDocument, str, str, float]


async def load_sources_for_org(
    client: SurrealContentClient,
    *,
    organization_id: str,
) -> list[ContentSource]:
    rows = await content_client.select_many(
        client,
        "SELECT * FROM crawl_sources WHERE organization_id = $organization_id;",
        organization_id=organization_id,
    )
    sources = [models.source_from_record(row) for row in rows]
    return sorted(sources, key=lambda source: (source.name.lower(), source.id))


async def load_sources_for_search_scope(
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
    rows = await content_client.select_many(
        client,
        f"SELECT * FROM crawl_sources WHERE {where_clause};",
        **params,
    )
    sources = [models.source_from_record(row) for row in rows]
    return sorted(sources, key=lambda source: (source.name.lower(), source.id))


async def load_documents_for_source_ids(
    client: SurrealContentClient,
    source_ids: list[str],
) -> list[ContentDocument]:
    rows: list[models.SurrealRecord] = []
    for batch in content_client.value_batches(source_ids):
        rows.extend(
            await content_client.select_many(
                client,
                "SELECT * FROM crawled_documents WHERE source_id INSIDE $source_ids;",
                source_ids=batch,
            )
        )
    documents = [models.document_from_record(row) for row in rows]
    return sorted(documents, key=lambda document: (document.source_id, document.id))


async def load_search_documents_by_ids(
    client: SurrealContentClient,
    document_ids: list[str],
) -> list[ContentDocument]:
    rows: list[models.SurrealRecord] = []
    for batch in content_client.value_batches(document_ids):
        rows.extend(
            await content_client.select_many(
                client,
                "SELECT uuid, organization_id, source_id, url, title, has_code "
                "FROM crawled_documents WHERE uuid INSIDE $document_ids;",
                document_ids=batch,
            )
        )
    documents = [models.document_from_record(row) for row in rows]
    return sorted(documents, key=lambda document: (document.source_id, document.id))


async def load_chunks_for_document_ids(
    client: SurrealContentClient,
    document_ids: list[str],
) -> list[ContentChunk]:
    rows: list[models.SurrealRecord] = []
    for batch in content_client.value_batches(document_ids):
        rows.extend(
            await content_client.select_many(
                client,
                f"SELECT {_DOCUMENT_CHUNK_SELECT} "
                "FROM document_chunks WHERE document_id INSIDE $document_ids;",
                document_ids=batch,
            )
        )
    chunks = [models.chunk_from_record(row) for row in rows]
    return sorted(chunks, key=lambda chunk: (chunk.document_id, chunk.chunk_index, chunk.id))


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
    include_patterns = models.coerce_str_list(data.get("include_patterns") or data.get("patterns"))
    exclude_patterns = models.coerce_str_list(data.get("exclude_patterns") or data.get("exclude"))

    async with content_client.surreal_content_client() as client:
        existing = await content_client.select_one(
            client,
            "SELECT * FROM crawl_sources "
            "WHERE organization_id = $organization_id AND url = $url LIMIT 1;",
            organization_id=organization_id,
            url=normalized_url,
        )
        if existing is not None:
            return models.source_from_record(existing), False

        now = models.utcnow()
        source = ContentSource(
            id=str(uuid4()),
            organization_id=organization_id,
            name=source_name,
            url=normalized_url,
            source_type=source_type,
            description=models.coerce_optional_str(data.get("description")),
            crawl_depth=max(0, min(int(depth), 10)),
            include_patterns=include_patterns,
            exclude_patterns=exclude_patterns,
            created_at=now,
            updated_at=now,
        )
        record = await content_client.replace_record(
            client,
            "crawl_sources",
            uuid=source.id,
            record=models.source_record(source),
        )
    return models.source_from_record(record), True


async def source_exists(source_id: str, organization_id: str) -> bool:
    async with content_client.surreal_content_client() as client:
        record = await content_client.select_one(
            client,
            "SELECT * FROM crawl_sources "
            "WHERE uuid = $source_id AND organization_id = $organization_id LIMIT 1;",
            source_id=source_id,
            organization_id=organization_id,
        )
    return record is not None


async def list_source_ids_for_org(organization_id: str) -> list[str]:
    async with content_client.surreal_content_client() as client:
        sources = await load_sources_for_org(client, organization_id=organization_id)
    return [source.id for source in sources]


async def set_source_job_state(
    source_id: str,
    *,
    organization_id: str,
    job_id: str | None,
    crawl_status: str,
    last_error: str | None,
) -> ContentSource | None:
    async with content_client.surreal_content_client() as client:
        record = await content_client.select_one(
            client,
            "SELECT * FROM crawl_sources "
            "WHERE uuid = $source_id AND organization_id = $organization_id LIMIT 1;",
            source_id=source_id,
            organization_id=organization_id,
        )
        if record is None:
            return None

        source = models.source_from_record(record)
        source.current_job_id = job_id
        source.crawl_status = crawl_status
        source.last_error = last_error
        source.updated_at = models.utcnow()
        saved = await content_client.replace_record(
            client,
            "crawl_sources",
            uuid=source.id,
            record=models.source_record(source),
        )
    return models.source_from_record(saved)


async def load_search_scope(
    *,
    organization_id: str,
    source_id: str | None,
    source_name: str | None,
) -> tuple[
    list[ContentSource],
    dict[str, ContentSource],
    dict[str, ContentDocument],
    list[ContentChunk],
]:
    async with content_client.surreal_content_client() as client:
        sources = await load_sources_for_search_scope(
            client,
            organization_id=organization_id,
            source_id=source_id,
            source_name=source_name,
        )
        source_ids = [source.id for source in sources]
        documents = await load_documents_for_source_ids(client, source_ids)
        chunks = await load_chunks_for_document_ids(client, [document.id for document in documents])

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
    rows: list[models.SurrealRecord],
    *,
    documents_by_id: dict[str, ContentDocument],
    sources_by_id: dict[str, ContentSource],
) -> list[ContentSearchRow]:
    hydrated: list[ContentSearchRow] = []
    for row in rows:
        chunk = models.chunk_from_record(row)
        document = documents_by_id.get(chunk.document_id)
        if document is None:
            continue
        source = sources_by_id.get(document.source_id)
        if source is None:
            continue
        hydrated.append(
            (chunk, document, source.name, source.id, models.coerce_float(row.get("score")))
        )
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
    rows = await content_client.select_many(
        client,
        "SELECT uuid, organization_id, name, url, source_type, description, crawl_status "
        f"FROM crawl_sources WHERE {where_clause} ORDER BY name ASC, uuid ASC;",
        **params,
    )
    return [models.source_from_record(row) for row in rows]


def _document_ids_from_search_rows(
    *row_groups: list[models.SurrealRecord],
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
    knn_effort = knn_search_effort(candidate_limit, content_client.CONTENT_KNN_EF_FLOOR)
    language_clause, language_params = _document_language_clause(language)
    lexical_query_text = build_fulltext_query(query_text)
    errors: list[str] = []

    async with content_client.surreal_content_client() as client:
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

        vector_rows: list[models.SurrealRecord] = []
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
                    content_client.select_many_raw(
                        client,
                        "SELECT * FROM ("
                        "SELECT uuid, organization_id, source_id, document_id, chunk_index, "
                        "chunk_type, content, context, heading_path, language, "
                        "has_entities, entity_ids, "
                        "(1 - vector::distance::knn()) AS score "
                        "FROM document_chunks WHERE organization_id = $organization_id "
                        "AND source_id INSIDE $source_ids"
                        f"{language_clause} "
                        f"AND embedding <|{candidate_limit}, {knn_effort}|> $query_embedding"
                        ") WHERE score >= $similarity_threshold "
                        "ORDER BY score DESC LIMIT $candidate_limit;",
                        **vector_params,
                    ),
                    timeout_seconds=content_client.DIRECT_SEARCH_QUERY_TIMEOUT_SECONDS,
                    operation_name="surreal_document_vector_search",
                )
            except (RuntimeError, TimeoutError) as exc:
                vector_errors.append(str(exc))

        lexical_rows: list[models.SurrealRecord] = []
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
                    content_client.select_many_raw(
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
                    timeout_seconds=content_client.DIRECT_SEARCH_QUERY_TIMEOUT_SECONDS,
                    operation_name="surreal_document_lexical_search",
                )
            except (RuntimeError, TimeoutError) as exc:
                errors.append(str(exc))

        document_ids = _document_ids_from_search_rows(vector_rows, lexical_rows)
        documents = await load_search_documents_by_ids(client, document_ids) if document_ids else []
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
    async with content_client.surreal_content_client() as client:
        rows = await content_client.select_many(
            client,
            f"SELECT * FROM document_chunks WHERE {' AND '.join(clauses)} "
            "ORDER BY document_id ASC, chunk_index ASC, uuid ASC LIMIT $limit;",
            **params,
        )
    return [models.chunk_from_record(row) for row in rows]


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
