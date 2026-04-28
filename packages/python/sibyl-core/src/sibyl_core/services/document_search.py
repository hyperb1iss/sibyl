"""Relational document search helpers for unified search."""

from collections.abc import Sequence
from dataclasses import replace
from typing import Any
from uuid import UUID

import structlog

from sibyl_core.config import settings
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


def _document_result_key(result: SearchResult) -> str:
    document_id = result.metadata.get("document_id")
    return str(document_id or result.id)


def _document_language_predicates(
    *,
    language: str | None,
    chunk_type_column: Any,
    language_column: Any,
    code_chunk_type: Any,
) -> tuple[Any, ...]:
    if not language:
        return ()

    return (
        chunk_type_column == code_chunk_type,
        language_column.ilike(language),
    )


def _dedupe_document_rows(
    rows: Sequence[Any],
) -> list[tuple[Any, Any, str, Any, float]]:
    best_rows: dict[str, tuple[Any, Any, str, Any, float]] = {}

    for row in rows:
        chunk, doc, source_name, source_id, score = row
        typed_row = (chunk, doc, source_name, source_id, float(score or 0.0))
        doc_id = str(doc.id)
        score_value = typed_row[4]

        if doc_id not in best_rows or score_value > float(best_rows[doc_id][4] or 0.0):
            best_rows[doc_id] = typed_row

    return sorted(best_rows.values(), key=lambda row: float(row[4] or 0.0), reverse=True)


def _build_document_result(
    chunk: Any,
    doc: Any,
    source_name: str,
    source_id: Any,
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
    rows: Sequence[Any],
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
    query_tokens = tokenize(query)
    document_tokens_by_id: dict[str, set[str]] = {}

    if language:
        language_filter = language.lower()
        chunks = [
            chunk
            for chunk in chunks
            if chunk.chunk_type.lower() == "code"
            and (chunk.language or "").lower() == language_filter
        ]

    vector_rows_raw: list[tuple[Any, Any, str, Any, float]] = []
    if query_embedding is not None:
        for chunk in chunks:
            if not chunk.embedding:
                continue
            similarity = cosine_similarity(chunk.embedding, query_embedding)
            if similarity < 0.5:
                continue
            document = documents_by_id.get(chunk.document_id)
            if document is None:
                continue
            source = sources_by_id.get(document.source_id)
            if source is None:
                continue
            vector_rows_raw.append((chunk, document, source.name, source.id, similarity))

    vector_results = _build_document_results_from_rows(
        vector_rows_raw,
        limit=limit,
        include_content=include_content,
    )

    lexical_rows_raw: list[tuple[Any, Any, str, Any, float]] = []
    for chunk in chunks:
        document = documents_by_id.get(chunk.document_id)
        if document is None:
            continue
        source = sources_by_id.get(document.source_id)
        if source is None:
            continue
        document_tokens = document_tokens_by_id.get(document.id)
        if document_tokens is None:
            document_tokens = tokenize_fields(document.title, document.content)
            document_tokens_by_id[document.id] = document_tokens
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

    from sibyl.crawler.embedder import embed_text

    query_embedding: list[float] | None = None
    try:
        query_embedding = await with_timeout(
            embed_text(query),
            timeout_seconds=DOCUMENT_EMBEDDING_TIMEOUT_SECONDS,
            operation_name="document_embedding",
        )
    except Exception as exc:
        log.warning("document_vector_embedding_failed", error=str(exc))

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
            log.warning("surreal_document_direct_search_failed", error=str(exc))
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

    from sibyl.db import CrawledDocument, CrawlSource, DocumentChunk, get_session
    from sibyl.db.models import ChunkType
    from sqlalchemy import func, select
    from sqlmodel import col

    async with get_session() as session:
        base_query = (
            select(
                DocumentChunk,
                CrawledDocument,
                CrawlSource.name.label("source_name"),  # type: ignore[attr-defined]
                CrawlSource.id.label("source_id"),  # type: ignore[attr-defined]
            )
            .join(CrawledDocument, DocumentChunk.document_id == CrawledDocument.id)  # type: ignore[arg-type]
            .join(CrawlSource, CrawledDocument.source_id == CrawlSource.id)  # type: ignore[arg-type]
        )

        base_query = base_query.where(col(CrawlSource.organization_id) == UUID(organization_id))

        if source_id:
            base_query = base_query.where(col(CrawlSource.id) == UUID(source_id))
        if source_name:
            base_query = base_query.where(col(CrawlSource.name).ilike(f"%{source_name}%"))

        for predicate in _document_language_predicates(
            language=language,
            chunk_type_column=DocumentChunk.chunk_type,
            language_column=DocumentChunk.language,
            code_chunk_type=ChunkType.CODE,
        ):
            base_query = base_query.where(predicate)

        vector_results: list[SearchResult] = []
        if query_embedding is not None:
            similarity_expr = 1 - DocumentChunk.embedding.cosine_distance(query_embedding)
            vector_query = (
                base_query.add_columns(similarity_expr.label("score"))
                .where(col(DocumentChunk.embedding).is_not(None))
                .where(similarity_expr >= 0.5)
                .order_by(similarity_expr.desc())
                .limit(limit * 5)
            )
            vector_rows = _dedupe_document_rows((await session.execute(vector_query)).all())[:limit]
            vector_results = [
                _build_document_result(
                    chunk=chunk,
                    doc=doc,
                    source_name=src_name,
                    source_id=src_id,
                    score=float(score),
                    include_content=include_content,
                )
                for chunk, doc, src_name, src_id, score in vector_rows
            ]

        ts_query = func.plainto_tsquery("english", query)
        ts_vector = func.to_tsvector("english", DocumentChunk.content)
        fts_rank = func.ts_rank(ts_vector, ts_query)
        lexical_query = (
            base_query.add_columns(fts_rank.label("score"))
            .where(fts_rank > 0)
            .order_by(fts_rank.desc())
            .limit(limit * 5)
        )
        lexical_rows = _dedupe_document_rows((await session.execute(lexical_query)).all())[:limit]
        lexical_results = [
            _build_document_result(
                chunk=chunk,
                doc=doc,
                source_name=src_name,
                source_id=src_id,
                score=float(score),
                include_content=include_content,
            )
            for chunk, doc, src_name, src_id, score in lexical_rows
        ]

    return _merge_document_results(vector_results, lexical_results, limit)
