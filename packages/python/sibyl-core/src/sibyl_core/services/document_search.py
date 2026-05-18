"""Document search helpers for unified search."""

import hashlib
import os
from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Protocol, cast
from uuid import UUID

import structlog

from sibyl_core.config import settings
from sibyl_core.embeddings.native import (
    NativeEmbeddingProvider,
    NativeEmbeddingProviderName,
    create_native_embedding_provider,
)
from sibyl_core.retrieval.dedup import cosine_similarity
from sibyl_core.services.surreal_content import (
    lexical_score_from_tokens,
    load_search_scope,
    search_document_chunks,
    tokenize,
    tokenize_fields,
)
from sibyl_core.tools.responses import SearchResult
from sibyl_core.utils.resilience import with_timeout

log = structlog.get_logger()

DOCUMENT_VECTOR_WEIGHT = 0.7
DOCUMENT_LEXICAL_WEIGHT = 0.3
DOCUMENT_EMBEDDING_TIMEOUT_SECONDS = 2.0
DOCUMENT_EMBEDDING_CACHE_SIZE = 1000

_document_embedding_provider: NativeEmbeddingProvider | None = None
_document_embedding_fingerprint: tuple[NativeEmbeddingProviderName, str, int, str] | None = None


def reset_document_embedding_provider_cache() -> None:
    global _document_embedding_fingerprint, _document_embedding_provider
    _document_embedding_provider = None
    _document_embedding_fingerprint = None


async def _embed_text(text: str) -> list[float]:
    provider = _get_document_embedding_provider()
    embeddings = await provider.embed_texts([text], input_kind="query")
    if not embeddings:
        raise ValueError("embedding provider returned no vectors")
    return [float(value) for value in embeddings[0]]


def _get_document_embedding_provider() -> NativeEmbeddingProvider:
    global _document_embedding_fingerprint, _document_embedding_provider
    provider_name = _document_embedding_provider_name()
    model = _document_embedding_model(provider_name)
    dimensions = _document_embedding_dimensions()
    api_key = _document_embedding_api_key(provider_name)
    fingerprint = (provider_name, model, dimensions, _secret_fingerprint(api_key))
    if _document_embedding_provider is None or fingerprint != _document_embedding_fingerprint:
        _document_embedding_provider = create_native_embedding_provider(
            provider=provider_name,
            model=model,
            dimensions=dimensions,
            cache_namespace="document",
            api_key=api_key,
            max_cache_size=DOCUMENT_EMBEDDING_CACHE_SIZE,
        )
        _document_embedding_fingerprint = fingerprint
    assert _document_embedding_provider is not None
    return _document_embedding_provider


def _document_embedding_provider_name() -> NativeEmbeddingProviderName:
    raw_provider = os.getenv("SIBYL_EMBEDDING_PROVIDER") or settings.embedding_provider
    if raw_provider == "openai":
        return "openai"
    if raw_provider == "gemini":
        return "gemini"
    raise ValueError(f"Unsupported embedding provider: {raw_provider}")


def _document_embedding_model(provider: NativeEmbeddingProviderName) -> str:
    env_model = os.getenv("SIBYL_EMBEDDING_MODEL")
    if env_model:
        return env_model
    if provider == "gemini" and settings.embedding_model == "text-embedding-3-small":
        return "gemini-embedding-2"
    return settings.embedding_model


def _document_embedding_dimensions() -> int:
    raw_dimensions = os.getenv("SIBYL_EMBEDDING_DIMENSIONS")
    if raw_dimensions:
        return int(raw_dimensions)
    return settings.embedding_dimensions


def _document_embedding_api_key(provider: NativeEmbeddingProviderName) -> str | None:
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


def _secret_fingerprint(secret: str | None) -> str:
    if not secret:
        return ""
    return hashlib.sha256(secret.encode()).hexdigest()


class DocumentSearchChunk(Protocol):
    @property
    def id(self) -> UUID | str: ...

    @property
    def document_id(self) -> UUID | str: ...

    @property
    def content(self) -> str: ...

    @property
    def context(self) -> str | None: ...

    @property
    def chunk_type(self) -> object: ...

    @property
    def chunk_index(self) -> int: ...

    @property
    def heading_path(self) -> list[str] | None: ...

    @property
    def language(self) -> str | None: ...

    @property
    def has_entities(self) -> bool: ...


class DocumentSearchDocument(Protocol):
    @property
    def id(self) -> UUID | str: ...

    @property
    def source_id(self) -> UUID | str: ...

    @property
    def url(self) -> str: ...

    @property
    def title(self) -> str: ...

    @property
    def content(self) -> str: ...

    @property
    def has_code(self) -> bool: ...


class DocumentSearchSource(Protocol):
    @property
    def id(self) -> UUID | str: ...

    @property
    def name(self) -> str: ...


type DocumentSearchRow = tuple[DocumentSearchChunk, DocumentSearchDocument, str, UUID | str, float]


def _document_result_key(result: SearchResult) -> str:
    document_id = result.metadata.get("document_id")
    return str(document_id or result.id)


def _dedupe_document_rows(
    rows: Sequence[DocumentSearchRow],
) -> list[DocumentSearchRow]:
    best_rows: dict[str, DocumentSearchRow] = {}

    for row in rows:
        chunk, doc, source_name, source_id, score = row
        typed_row = (chunk, doc, source_name, source_id, float(score or 0.0))
        doc_id = str(doc.id)
        score_value = typed_row[4]

        if doc_id not in best_rows or score_value > float(best_rows[doc_id][4] or 0.0):
            best_rows[doc_id] = typed_row

    return sorted(best_rows.values(), key=lambda row: float(row[4] or 0.0), reverse=True)


def _build_document_result(
    chunk: DocumentSearchChunk,
    doc: DocumentSearchDocument,
    source_name: str,
    source_id: UUID | str,
    score: float,
    include_content: bool,
) -> SearchResult:
    if include_content:
        content = chunk.content[:500] if chunk.content else ""
    else:
        content = chunk.content[:200] if chunk.content else ""

    heading_context = " > ".join(chunk.heading_path) if chunk.heading_path else ""
    if heading_context:
        content = f"[{heading_context}] {content}"

    display_url = None
    if doc.url and not doc.url.startswith("file://"):
        display_url = doc.url

    return SearchResult(
        id=str(chunk.id),
        type="document",
        name=doc.title or source_name,
        content=content,
        score=score,
        source=source_name,
        url=display_url,
        result_origin="document",
        metadata={
            "document_id": str(doc.id),
            "source_id": str(source_id),
            "chunk_type": chunk.chunk_type.value
            if hasattr(chunk.chunk_type, "value")
            else str(chunk.chunk_type),
            "chunk_index": chunk.chunk_index,
            "heading_path": chunk.heading_path or [],
            "language": chunk.language,
            "has_code": doc.has_code,
            "hint": "Use 'sibyl entity <id>' or fetch /api/entities/<id> for full content",
        },
    )


def _normalize_document_scores(results: list[SearchResult]) -> dict[str, float]:
    if not results:
        return {}

    max_score = max(result.score for result in results)
    if max_score <= 0:
        return {_document_result_key(result): 1.0 for result in results}

    return {_document_result_key(result): result.score / max_score for result in results}


def _merge_document_results(
    vector_results: list[SearchResult],
    lexical_results: list[SearchResult],
    limit: int,
) -> list[SearchResult]:
    combined_scores: dict[str, float] = {}
    representatives: dict[str, SearchResult] = {}

    for results, weight in (
        (vector_results, DOCUMENT_VECTOR_WEIGHT),
        (lexical_results, DOCUMENT_LEXICAL_WEIGHT),
    ):
        normalized_scores = _normalize_document_scores(results)
        for result in results:
            key = _document_result_key(result)
            representatives.setdefault(key, result)
            combined_scores[key] = combined_scores.get(key, 0.0) + (
                normalized_scores.get(key, 0.0) * weight
            )

    ranked_keys = sorted(combined_scores, key=lambda key: combined_scores[key], reverse=True)
    return [
        replace(representatives[key], score=combined_scores[key]) for key in ranked_keys[:limit]
    ]


def _build_document_results_from_rows(
    rows: Sequence[DocumentSearchRow],
    *,
    limit: int,
    include_content: bool,
) -> list[SearchResult]:
    return [
        _build_document_result(
            chunk=chunk,
            doc=doc,
            source_name=src_name,
            source_id=src_id,
            score=float(score),
            include_content=include_content,
        )
        for chunk, doc, src_name, src_id, score in _dedupe_document_rows(rows)[:limit]
    ]


def _chunk_embedding(chunk: DocumentSearchChunk) -> list[float] | None:
    value = getattr(chunk, "embedding", None)
    if not isinstance(value, Sequence):
        return None
    return [float(item) for item in value]


def _search_documents_from_scope(
    *,
    query: str,
    language: str | None,
    limit: int,
    include_content: bool,
    query_embedding: list[float] | None,
    sources_by_id: Mapping[str, DocumentSearchSource],
    documents_by_id: Mapping[str, DocumentSearchDocument],
    chunks: Sequence[DocumentSearchChunk],
) -> list[SearchResult]:
    query_tokens = tokenize(query)
    document_tokens_by_id: dict[str, set[str]] = {}

    if language:
        language_filter = language.lower()
        chunks = [
            chunk
            for chunk in chunks
            if str(
                chunk.chunk_type.value if hasattr(chunk.chunk_type, "value") else chunk.chunk_type
            ).lower()
            == "code"
            and (chunk.language or "").lower() == language_filter
        ]

    vector_rows_raw: list[DocumentSearchRow] = []
    if query_embedding is not None:
        for chunk in chunks:
            embedding = _chunk_embedding(chunk)
            if not embedding:
                continue
            similarity = cosine_similarity(embedding, query_embedding)
            if similarity < 0.5:
                continue
            document = documents_by_id.get(str(chunk.document_id))
            if document is None:
                continue
            source = sources_by_id.get(str(document.source_id))
            if source is None:
                continue
            vector_rows_raw.append((chunk, document, source.name, source.id, similarity))

    vector_results = _build_document_results_from_rows(
        vector_rows_raw,
        limit=limit,
        include_content=include_content,
    )

    lexical_rows_raw: list[DocumentSearchRow] = []
    for chunk in chunks:
        document = documents_by_id.get(str(chunk.document_id))
        if document is None:
            continue
        source = sources_by_id.get(str(document.source_id))
        if source is None:
            continue
        document_id = str(document.id)
        document_tokens = document_tokens_by_id.get(document_id)
        if document_tokens is None:
            document_tokens = tokenize_fields(document.title, document.content)
            document_tokens_by_id[document_id] = document_tokens
        chunk_tokens = tokenize_fields(chunk.content, chunk.context)
        score = lexical_score_from_tokens(query_tokens, chunk_tokens, document_tokens)
        if score <= 0:
            continue
        lexical_rows_raw.append((chunk, document, source.name, source.id, score))

    lexical_results = _build_document_results_from_rows(
        lexical_rows_raw,
        limit=limit,
        include_content=include_content,
    )

    return _merge_document_results(vector_results, lexical_results, limit)


async def _search_documents_surreal_scan(
    *,
    query: str,
    organization_id: str,
    source_id: str | None,
    source_name: str | None,
    language: str | None,
    limit: int,
    include_content: bool,
    query_embedding: list[float] | None,
) -> list[SearchResult]:
    _, sources_by_id, documents_by_id, chunks = await load_search_scope(
        organization_id=organization_id,
        source_id=source_id,
        source_name=source_name,
    )
    return _search_documents_from_scope(
        query=query,
        language=language,
        limit=limit,
        include_content=include_content,
        query_embedding=query_embedding,
        sources_by_id=sources_by_id,
        documents_by_id=documents_by_id,
        chunks=chunks,
    )


async def _search_documents_runtime_scan(
    *,
    query: str,
    organization_id: str,
    source_id: str | None,
    source_name: str | None,
    language: str | None,
    limit: int,
    include_content: bool,
    query_embedding: list[float] | None,
) -> list[SearchResult]:
    from sibyl.persistence.content_runtime import (
        get_content_read_session,
        list_source_chunks,
        list_source_documents,
        list_sources_for_graph_linking,
    )

    org_uuid = UUID(organization_id)
    requested_source_id = UUID(source_id) if source_id else None

    async with get_content_read_session() as session:
        sources = await list_sources_for_graph_linking(
            session,
            organization_id=org_uuid,
            source_id=requested_source_id,
        )
        if source_name:
            normalized_source_name = source_name.lower()
            sources = [
                source for source in sources if normalized_source_name in source.name.lower()
            ]

        documents_by_id: dict[str, DocumentSearchDocument] = {}
        chunks: list[DocumentSearchChunk] = []
        for source in sources:
            documents = await list_source_documents(session, source_id=source.id)
            for document in documents:
                documents_by_id[str(document.id)] = cast(DocumentSearchDocument, document)
            chunks.extend(
                cast(DocumentSearchChunk, chunk)
                for chunk in await list_source_chunks(session, source_id=source.id)
            )

    return _search_documents_from_scope(
        query=query,
        language=language,
        limit=limit,
        include_content=include_content,
        query_embedding=query_embedding,
        sources_by_id={str(source.id): cast(DocumentSearchSource, source) for source in sources},
        documents_by_id=documents_by_id,
        chunks=chunks,
    )


async def search_documents(
    query: str,
    organization_id: str,
    source_id: str | None = None,
    source_name: str | None = None,
    language: str | None = None,
    limit: int = 10,
    include_content: bool = True,
) -> list[SearchResult]:
    """Search crawled documentation using vector and lexical matching."""

    query_embedding: list[float] | None = None
    try:
        query_embedding = await with_timeout(
            _embed_text(query),
            timeout_seconds=DOCUMENT_EMBEDDING_TIMEOUT_SECONDS,
            operation_name="document_embedding",
        )
    except Exception as exc:
        log.warning("document_vector_embedding_failed", error_type=type(exc).__name__)

    if settings.store == "surreal":
        try:
            vector_rows_raw, lexical_rows_raw = await search_document_chunks(
                organization_id=organization_id,
                query_text=query,
                query_embedding=query_embedding,
                source_id=source_id,
                source_name=source_name,
                language=language,
                limit=limit,
            )
        except RuntimeError as exc:
            log.warning(
                "surreal_document_direct_search_failed",
                error_type=type(exc).__name__,
            )
            return await _search_documents_surreal_scan(
                query=query,
                organization_id=organization_id,
                source_id=source_id,
                source_name=source_name,
                language=language,
                limit=limit,
                include_content=include_content,
                query_embedding=query_embedding,
            )

        vector_results = _build_document_results_from_rows(
            vector_rows_raw,
            limit=limit,
            include_content=include_content,
        )
        lexical_results = _build_document_results_from_rows(
            lexical_rows_raw,
            limit=limit,
            include_content=include_content,
        )

        return _merge_document_results(vector_results, lexical_results, limit)

    return await _search_documents_runtime_scan(
        query=query,
        organization_id=organization_id,
        source_id=source_id,
        source_name=source_name,
        language=language,
        limit=limit,
        include_content=include_content,
        query_embedding=query_embedding,
    )
