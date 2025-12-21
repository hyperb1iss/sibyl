"""RAG (Retrieval-Augmented Generation) search endpoints.

Provides semantic search over crawled documentation:
- Vector similarity search on document chunks
- Source-filtered search
- Code example search
- Full page retrieval
- Archon-compatible API surface
"""

import hashlib
import re
from uuid import UUID

import structlog
from fastapi import APIRouter, HTTPException
from sqlalchemy import func, select, text
from sqlmodel import col

from sibyl.api.schemas import (
    CodeExampleRequest,
    CodeExampleResponse,
    CodeExampleResult,
    CrawlDocumentResponse,
    DocumentRelatedEntitiesResponse,
    DocumentRelatedEntity,
    DocumentUpdateRequest,
    FullPageResponse,
    RAGChunkResult,
    RAGPageResult,
    RAGSearchRequest,
    RAGSearchResponse,
    SourcePagesResponse,
)
from sibyl.crawler.embedder import embed_text
from sibyl.db import (
    CrawledDocument,
    CrawlSource,
    DocumentChunk,
    get_session,
)
from sibyl.db.models import ChunkType

log = structlog.get_logger()
router = APIRouter(prefix="/rag", tags=["rag"])


# =============================================================================
# RAG Search - Vector Similarity on Chunks
# =============================================================================


@router.post("/search", response_model=RAGSearchResponse)
async def rag_search(request: RAGSearchRequest) -> RAGSearchResponse:
    """Semantic search over document chunks.

    Uses pgvector for similarity search with optional source filtering.
    Supports returning chunks or grouping by page.
    """
    # Generate query embedding
    try:
        query_embedding = await embed_text(request.query)
    except Exception as e:
        log.error("Failed to generate query embedding", error=str(e))
        raise HTTPException(status_code=500, detail=f"Embedding error: {e}") from e

    async with get_session() as session:
        # Build base query with cosine similarity
        # Using 1 - cosine_distance for similarity (pgvector uses distance)
        similarity_expr = 1 - DocumentChunk.embedding.cosine_distance(query_embedding)

        query = (
            select(
                DocumentChunk,
                CrawledDocument,
                CrawlSource.name.label("source_name"),
                CrawlSource.id.label("source_id"),
                similarity_expr.label("similarity"),
            )
            .join(CrawledDocument, DocumentChunk.document_id == CrawledDocument.id)
            .join(CrawlSource, CrawledDocument.source_id == CrawlSource.id)
            .where(col(DocumentChunk.embedding).is_not(None))
        )

        # Apply source filters
        source_filter_name = None
        if request.source_id:
            query = query.where(col(CrawlSource.id) == UUID(request.source_id))
            source_filter_name = request.source_id
        elif request.source_name:
            query = query.where(col(CrawlSource.name).ilike(f"%{request.source_name}%"))
            source_filter_name = request.source_name

        # Apply similarity threshold and ordering
        query = (
            query.where(similarity_expr >= request.similarity_threshold)
            .order_by(similarity_expr.desc())
            .limit(request.match_count)
        )

        result = await session.execute(query)
        rows = result.all()

        if request.return_mode == "pages":
            # Group by document, return best chunk per doc
            page_results: dict[str, RAGPageResult] = {}
            for chunk, doc, source_name, source_id, similarity in rows:
                doc_id = str(doc.id)
                if doc_id not in page_results or similarity > page_results[doc_id].best_chunk_similarity:
                    page_results[doc_id] = RAGPageResult(
                        document_id=doc_id,
                        source_id=str(source_id),
                        source_name=source_name,
                        url=doc.url,
                        title=doc.title,
                        content=doc.content,
                        word_count=doc.word_count,
                        has_code=doc.has_code,
                        headings=doc.headings or [],
                        code_languages=doc.code_languages or [],
                        best_chunk_similarity=similarity,
                    )
            results: list[RAGChunkResult | RAGPageResult] = list(page_results.values())
        else:
            # Return individual chunks
            results = [
                RAGChunkResult(
                    chunk_id=str(chunk.id),
                    document_id=str(doc.id),
                    source_id=str(source_id),
                    source_name=source_name,
                    url=doc.url,
                    title=doc.title,
                    content=chunk.content,
                    context=chunk.context if request.include_context else None,
                    similarity=similarity,
                    chunk_type=chunk.chunk_type.value if hasattr(chunk.chunk_type, "value") else str(chunk.chunk_type),
                    chunk_index=chunk.chunk_index,
                    heading_path=chunk.heading_path or [],
                    language=chunk.language,
                )
                for chunk, doc, source_name, source_id, similarity in rows
            ]

    log.debug(
        "RAG search completed",
        query=request.query[:50],
        results=len(results),
        mode=request.return_mode,
    )

    return RAGSearchResponse(
        results=results,
        total=len(results),
        query=request.query,
        source_filter=source_filter_name,
        return_mode=request.return_mode,
    )


# =============================================================================
# Code Example Search
# =============================================================================


@router.post("/code-examples", response_model=CodeExampleResponse)
async def search_code_examples(request: CodeExampleRequest) -> CodeExampleResponse:
    """Search for code examples with optional language filtering.

    Only searches chunks with chunk_type = 'code'.
    """
    # Generate query embedding
    try:
        query_embedding = await embed_text(request.query)
    except Exception as e:
        log.error("Failed to generate query embedding", error=str(e))
        raise HTTPException(status_code=500, detail=f"Embedding error: {e}") from e

    async with get_session() as session:
        similarity_expr = 1 - DocumentChunk.embedding.cosine_distance(query_embedding)

        query = (
            select(
                DocumentChunk,
                CrawledDocument,
                CrawlSource.name.label("source_name"),
                similarity_expr.label("similarity"),
            )
            .join(CrawledDocument, DocumentChunk.document_id == CrawledDocument.id)
            .join(CrawlSource, CrawledDocument.source_id == CrawlSource.id)
            .where(col(DocumentChunk.embedding).is_not(None))
            .where(col(DocumentChunk.chunk_type) == ChunkType.CODE)
        )

        # Apply filters
        if request.source_id:
            query = query.where(col(CrawlSource.id) == UUID(request.source_id))

        if request.language:
            query = query.where(col(DocumentChunk.language).ilike(request.language))

        query = query.order_by(similarity_expr.desc()).limit(request.match_count)

        result = await session.execute(query)
        rows = result.all()

        examples = [
            CodeExampleResult(
                chunk_id=str(chunk.id),
                document_id=str(doc.id),
                source_name=source_name,
                url=doc.url,
                title=doc.title,
                code=chunk.content,
                context=chunk.context,
                language=chunk.language,
                similarity=similarity,
                heading_path=chunk.heading_path or [],
            )
            for chunk, doc, source_name, similarity in rows
        ]

    log.debug(
        "Code example search completed",
        query=request.query[:50],
        language=request.language,
        results=len(examples),
    )

    return CodeExampleResponse(
        examples=examples,
        total=len(examples),
        query=request.query,
        language_filter=request.language,
    )


# =============================================================================
# Page Listing and Full Page Retrieval
# =============================================================================


@router.get("/sources/{source_id}/pages", response_model=SourcePagesResponse)
async def list_source_pages(
    source_id: str,
    limit: int = 50,
    offset: int = 0,
    has_code: bool | None = None,
    is_index: bool | None = None,
) -> SourcePagesResponse:
    """List all pages for a source with optional filtering."""
    try:
        source_uuid = UUID(source_id)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid source ID format: {source_id}")

    async with get_session() as session:
        # Get source info
        source = await session.get(CrawlSource, source_uuid)
        if not source:
            raise HTTPException(status_code=404, detail=f"Source not found: {source_id}")

        # Build query
        query = (
            select(CrawledDocument)
            .where(col(CrawledDocument.source_id) == source_uuid)
        )

        if has_code is not None:
            query = query.where(col(CrawledDocument.has_code) == has_code)

        if is_index is not None:
            query = query.where(col(CrawledDocument.is_index) == is_index)

        # Get total count
        count_query = select(func.count()).select_from(query.subquery())
        count_result = await session.execute(count_query)
        total = count_result.scalar() or 0

        # Apply pagination
        query = query.order_by(col(CrawledDocument.title)).offset(offset).limit(limit)
        result = await session.execute(query)
        documents = list(result.scalars().all())

        pages = [
            CrawlDocumentResponse(
                id=str(doc.id),
                source_id=source_id,
                url=doc.url,
                title=doc.title,
                word_count=doc.word_count,
                has_code=doc.has_code,
                is_index=doc.is_index,
                depth=doc.depth,
                crawled_at=doc.crawled_at,
                headings=doc.headings or [],
                code_languages=doc.code_languages or [],
            )
            for doc in documents
        ]

    return SourcePagesResponse(
        source_id=source_id,
        source_name=source.name,
        pages=pages,
        total=total,
        has_more=offset + len(pages) < total,
    )


@router.get("/pages/{document_id}", response_model=FullPageResponse)
async def get_full_page(document_id: str) -> FullPageResponse:
    """Get full page content by document ID."""
    try:
        doc_uuid = UUID(document_id)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid document ID format: {document_id}")

    async with get_session() as session:
        doc = await session.get(CrawledDocument, doc_uuid)
        if not doc:
            raise HTTPException(status_code=404, detail=f"Document not found: {document_id}")

        # Get source name
        source = await session.get(CrawlSource, doc.source_id)
        source_name = source.name if source else "Unknown"

    return FullPageResponse(
        document_id=str(doc.id),
        source_id=str(doc.source_id),
        source_name=source_name,
        url=doc.url,
        title=doc.title,
        content=doc.content,
        raw_content=doc.raw_content if len(doc.raw_content) < 100000 else None,
        word_count=doc.word_count,
        token_count=doc.token_count,
        has_code=doc.has_code,
        headings=doc.headings or [],
        code_languages=doc.code_languages or [],
        links=doc.links or [],
        crawled_at=doc.crawled_at,
    )


@router.get("/pages/by-url")
async def get_page_by_url(url: str) -> FullPageResponse:
    """Get full page content by URL."""
    async with get_session() as session:
        result = await session.execute(
            select(CrawledDocument).where(col(CrawledDocument.url) == url)
        )
        doc = result.scalar_one_or_none()

        if not doc:
            raise HTTPException(status_code=404, detail=f"Document not found for URL: {url}")

        # Get source name
        source = await session.get(CrawlSource, doc.source_id)
        source_name = source.name if source else "Unknown"

    return FullPageResponse(
        document_id=str(doc.id),
        source_id=str(doc.source_id),
        source_name=source_name,
        url=doc.url,
        title=doc.title,
        content=doc.content,
        raw_content=doc.raw_content if len(doc.raw_content) < 100000 else None,
        word_count=doc.word_count,
        token_count=doc.token_count,
        has_code=doc.has_code,
        headings=doc.headings or [],
        code_languages=doc.code_languages or [],
        links=doc.links or [],
        crawled_at=doc.crawled_at,
    )


# =============================================================================
# Hybrid Search (Vector + Full-Text)
# =============================================================================


@router.post("/hybrid-search", response_model=RAGSearchResponse)
async def hybrid_search(request: RAGSearchRequest) -> RAGSearchResponse:
    """Hybrid search combining vector similarity and full-text search.

    Uses RRF (Reciprocal Rank Fusion) to combine results from:
    - Vector similarity (pgvector cosine distance)
    - Full-text search (PostgreSQL tsvector)
    """
    # Generate query embedding
    try:
        query_embedding = await embed_text(request.query)
    except Exception as e:
        log.error("Failed to generate query embedding", error=str(e))
        raise HTTPException(status_code=500, detail=f"Embedding error: {e}") from e

    async with get_session() as session:
        # Build hybrid query with RRF
        # RRF score = sum(1 / (k + rank)) for each retriever
        k = 60  # RRF constant

        # Vector similarity score
        similarity_expr = 1 - DocumentChunk.embedding.cosine_distance(query_embedding)

        # Full-text relevance using ts_rank
        ts_query = func.plainto_tsquery("english", request.query)
        ts_vector = func.to_tsvector("english", DocumentChunk.content)
        fts_rank = func.ts_rank(ts_vector, ts_query)

        query = (
            select(
                DocumentChunk,
                CrawledDocument,
                CrawlSource.name.label("source_name"),
                CrawlSource.id.label("source_id"),
                similarity_expr.label("similarity"),
                fts_rank.label("fts_rank"),
            )
            .join(CrawledDocument, DocumentChunk.document_id == CrawledDocument.id)
            .join(CrawlSource, CrawledDocument.source_id == CrawlSource.id)
            .where(col(DocumentChunk.embedding).is_not(None))
        )

        # Apply source filters
        source_filter_name = None
        if request.source_id:
            query = query.where(col(CrawlSource.id) == UUID(request.source_id))
            source_filter_name = request.source_id
        elif request.source_name:
            query = query.where(col(CrawlSource.name).ilike(f"%{request.source_name}%"))
            source_filter_name = request.source_name

        # Combine with RRF-style scoring (simplified: weighted combination)
        # Higher weight for vector similarity since it's more reliable
        combined_score = similarity_expr * 0.7 + fts_rank * 0.3

        query = (
            query.where(similarity_expr >= request.similarity_threshold)
            .order_by(combined_score.desc())
            .limit(request.match_count)
        )

        result = await session.execute(query)
        rows = result.all()

        results: list[RAGChunkResult | RAGPageResult] = [
            RAGChunkResult(
                chunk_id=str(chunk.id),
                document_id=str(doc.id),
                source_id=str(source_id),
                source_name=source_name,
                url=doc.url,
                title=doc.title,
                content=chunk.content,
                context=chunk.context if request.include_context else None,
                similarity=similarity,
                chunk_type=chunk.chunk_type.value if hasattr(chunk.chunk_type, "value") else str(chunk.chunk_type),
                chunk_index=chunk.chunk_index,
                heading_path=chunk.heading_path or [],
                language=chunk.language,
            )
            for chunk, doc, source_name, source_id, similarity, fts_rank in rows
        ]

    log.debug(
        "Hybrid search completed",
        query=request.query[:50],
        results=len(results),
    )

    return RAGSearchResponse(
        results=results,
        total=len(results),
        query=request.query,
        source_filter=source_filter_name,
        return_mode="chunks",
    )


# =============================================================================
# Document Update
# =============================================================================


def _extract_headings(content: str) -> list[str]:
    """Extract markdown headings from content."""
    headings: list[str] = []
    for line in content.split("\n"):
        match = re.match(r"^(#{1,6})\s+(.+)$", line.strip())
        if match:
            headings.append(match.group(2).strip())
    return headings


def _detect_code_presence(content: str) -> bool:
    """Check if content contains code blocks."""
    return "```" in content or content.count("    ") > 5


def _estimate_token_count(content: str) -> int:
    """Rough token estimate (~4 chars per token)."""
    return len(content) // 4


@router.patch("/pages/{document_id}", response_model=FullPageResponse)
async def update_document(document_id: str, request: DocumentUpdateRequest) -> FullPageResponse:
    """Update a document's title and/or content.

    When content is updated, recalculates derived fields:
    - word_count, token_count, content_hash
    - has_code, headings
    """
    if request.title is None and request.content is None:
        raise HTTPException(status_code=400, detail="At least one of title or content must be provided")

    try:
        doc_uuid = UUID(document_id)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid document ID format: {document_id}")

    async with get_session() as session:
        doc = await session.get(CrawledDocument, doc_uuid)
        if not doc:
            raise HTTPException(status_code=404, detail=f"Document not found: {document_id}")

        # Update title if provided
        if request.title is not None:
            doc.title = request.title

        # Update content and recalculate derived fields
        if request.content is not None:
            doc.content = request.content
            doc.word_count = len(request.content.split())
            doc.token_count = _estimate_token_count(request.content)
            doc.content_hash = hashlib.sha256(request.content.encode()).hexdigest()
            doc.has_code = _detect_code_presence(request.content)
            doc.headings = _extract_headings(request.content)

        session.add(doc)
        await session.commit()
        await session.refresh(doc)

        # Get source name
        source = await session.get(CrawlSource, doc.source_id)
        source_name = source.name if source else "Unknown"

    log.info("Document updated", document_id=document_id, title_updated=request.title is not None, content_updated=request.content is not None)

    return FullPageResponse(
        document_id=str(doc.id),
        source_id=str(doc.source_id),
        source_name=source_name,
        url=doc.url,
        title=doc.title,
        content=doc.content,
        raw_content=doc.raw_content if len(doc.raw_content) < 100000 else None,
        word_count=doc.word_count,
        token_count=doc.token_count,
        has_code=doc.has_code,
        headings=doc.headings or [],
        code_languages=doc.code_languages or [],
        links=doc.links or [],
        crawled_at=doc.crawled_at,
    )


# =============================================================================
# Document Related Entities
# =============================================================================


@router.get("/pages/{document_id}/entities", response_model=DocumentRelatedEntitiesResponse)
async def get_document_related_entities(document_id: str) -> DocumentRelatedEntitiesResponse:
    """Get knowledge graph entities related to a document.

    Uses semantic search to find entities (tasks, patterns, episodes, etc.)
    that are relevant to this document's content based on its title.
    """
    try:
        doc_uuid = UUID(document_id)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid document ID format: {document_id}")

    async with get_session() as session:
        # Get the document
        doc = await session.get(CrawledDocument, doc_uuid)
        if not doc:
            raise HTTPException(status_code=404, detail=f"Document not found: {document_id}")

        doc_title = doc.title

    # Search the knowledge graph using document title as query
    entities: list[DocumentRelatedEntity] = []
    try:
        from sibyl.graph.client import get_graph_client
        from sibyl.graph.entities import EntityManager

        client = await get_graph_client()
        entity_manager = EntityManager(client)

        # Semantic search using document title
        search_results = await entity_manager.search(
            query=doc_title,
            limit=15,
        )

        for entity, score in search_results:
            # Skip very low relevance matches
            if score < 0.1:
                continue

            entities.append(
                DocumentRelatedEntity(
                    id=entity.id,
                    name=entity.name,
                    entity_type=entity.entity_type.value,
                    description=entity.description or "",
                    chunk_count=int(score * 100),  # Use score as relevance indicator
                )
            )

    except Exception as e:
        log.warning("graph_search_failed", error=str(e), document_id=document_id)
        # Return empty if graph is unavailable
        return DocumentRelatedEntitiesResponse(
            document_id=document_id,
            entities=[],
            total=0,
        )

    log.debug("document_entities_found", document_id=document_id, title=doc_title, count=len(entities))

    return DocumentRelatedEntitiesResponse(
        document_id=document_id,
        entities=entities,
        total=len(entities),
    )
