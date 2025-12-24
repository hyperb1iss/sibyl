"""Document ingestion pipeline - crawl, chunk, embed, store, integrate.

Orchestrates the full ingestion flow:
1. Crawl documentation source
2. Store raw documents
3. Chunk documents into retrievable segments
4. Generate embeddings
5. Store chunks with embeddings
6. Extract entities and link to knowledge graph (Graph-RAG integration)

Supports both single-document and bulk source ingestion.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import structlog
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlmodel import col

from sibyl.crawler.chunker import ChunkStrategy, DocumentChunker
from sibyl.crawler.embedder import EmbeddingService
from sibyl.crawler.graph_integration import GraphIntegrationService
from sibyl.crawler.local import LocalFileCrawler
from sibyl.crawler.service import CrawlerService
from sibyl.db import (
    CrawledDocument,
    CrawlSource,
    DocumentChunk,
    SourceType,
    get_session,
)
from sibyl.graph.entities import EntityManager
from sibyl.models.entities import Entity, EntityType

if TYPE_CHECKING:
    from uuid import UUID

log = structlog.get_logger()


@dataclass
class IngestionStats:
    """Statistics from an ingestion run."""

    source_id: UUID
    source_name: str
    documents_crawled: int = 0
    documents_stored: int = 0
    chunks_created: int = 0
    embeddings_generated: int = 0
    entities_extracted: int = 0
    entities_linked: int = 0
    errors: int = 0
    duration_seconds: float = 0.0

    def __str__(self) -> str:
        base = (
            f"Ingested {self.source_name}: "
            f"{self.documents_stored} docs, "
            f"{self.chunks_created} chunks, "
            f"{self.embeddings_generated} embeddings"
        )
        if self.entities_extracted > 0:
            base += f", {self.entities_extracted} entities ({self.entities_linked} linked)"
        base += f" in {self.duration_seconds:.1f}s"
        return base


class IngestionPipeline:
    """Full document ingestion pipeline.

    Coordinates crawling, chunking, embedding, storage, and graph integration.
    Handles batching and error recovery.
    """

    def __init__(
        self,
        organization_id: str,
        *,
        chunk_strategy: ChunkStrategy = ChunkStrategy.SEMANTIC,
        generate_embeddings: bool = True,
        embedding_batch_size: int = 50,
        integrate_with_graph: bool = True,
    ) -> None:
        """Initialize the ingestion pipeline.

        Args:
            organization_id: Organization ID for graph operations
            chunk_strategy: Strategy for chunking documents
            generate_embeddings: Whether to generate embeddings
            embedding_batch_size: Batch size for embedding generation
            integrate_with_graph: Whether to extract entities and link to graph
        """
        self.organization_id = organization_id
        self.chunk_strategy = chunk_strategy
        self.generate_embeddings = generate_embeddings
        self.embedding_batch_size = embedding_batch_size
        self.integrate_with_graph = integrate_with_graph

        self._crawler: CrawlerService | None = None
        self._embedder: EmbeddingService | None = None
        self._graph_integration: GraphIntegrationService | None = None
        self._entity_manager: EntityManager | None = None
        self._chunker = DocumentChunker()

    async def start(self) -> None:
        """Start pipeline services."""
        self._crawler = CrawlerService()
        await self._crawler.start()

        if self.generate_embeddings:
            self._embedder = EmbeddingService(batch_size=self.embedding_batch_size)

        if self.integrate_with_graph:
            try:
                from sibyl.graph.client import get_graph_client

                graph_client = await get_graph_client()
                self._graph_integration = GraphIntegrationService(
                    graph_client,
                    self.organization_id,
                    extract_entities=True,
                    create_new_entities=False,  # Only link to existing entities for now
                )
                self._entity_manager = EntityManager(graph_client, group_id=self.organization_id)
                log.info(
                    "Graph integration enabled",
                    extract_entities=True,
                    entity_manager=bool(self._entity_manager),
                )
            except Exception as e:
                log.warning("Graph integration unavailable", error=str(e), exc_info=True)
                self._graph_integration = None
                self._entity_manager = None

        log.info("Ingestion pipeline started")

    async def stop(self) -> None:
        """Stop pipeline services."""
        if self._crawler:
            await self._crawler.stop()
            self._crawler = None

        log.info("Ingestion pipeline stopped")

    async def __aenter__(self) -> IngestionPipeline:
        """Async context manager entry."""
        await self.start()
        return self

    async def __aexit__(self, *args: object) -> None:
        """Async context manager exit."""
        await self.stop()

    async def ingest_source(
        self,
        source: CrawlSource,
        *,
        max_pages: int = 100,
        max_depth: int = 3,
    ) -> IngestionStats:
        """Ingest a full documentation source.

        Crawls all pages, chunks content, generates embeddings,
        and stores everything in the database.

        Args:
            source: CrawlSource to ingest
            max_pages: Maximum pages to crawl
            max_depth: Maximum link depth

        Returns:
            IngestionStats with results
        """
        if not self._crawler:
            raise RuntimeError("Pipeline not started. Use 'async with' or call start()")

        start_time = datetime.now(UTC)
        stats = IngestionStats(
            source_id=source.id,
            source_name=source.name,
        )

        log.info(
            "Starting source ingestion",
            source=source.name,
            url=source.url,
            max_pages=max_pages,
        )

        try:
            # Select crawler based on source type
            if source.source_type == SourceType.LOCAL:
                crawler: CrawlerService | LocalFileCrawler = LocalFileCrawler()
            else:
                if not self._crawler:
                    raise RuntimeError("Web crawler not started")
                crawler = self._crawler

            # Crawl and process documents
            async for doc in crawler.crawl_source(
                source,
                max_pages=max_pages,
                max_depth=max_depth,
            ):
                stats.documents_crawled += 1

                try:
                    # Store document and create chunks
                    await self._process_document(doc, stats, source.source_type)
                    stats.documents_stored += 1

                except Exception as e:
                    stats.errors += 1
                    log.error(  # noqa: TRY400
                        "Failed to process document",
                        url=doc.url,
                        error=str(e),
                    )

        except Exception as e:
            log.error("Source ingestion failed", source=source.name, error=str(e))  # noqa: TRY400
            stats.errors += 1

        stats.duration_seconds = (datetime.now(UTC) - start_time).total_seconds()

        log.info(
            "Source ingestion complete",
            source=source.name,
            stats=str(stats),
        )

        return stats

    async def _create_convention_entity(
        self,
        document: CrawledDocument,
    ) -> str | None:
        """Create a convention entity in the knowledge graph for a local file.

        Args:
            document: The crawled document from a local directory

        Returns:
            Entity ID if created, None otherwise
        """
        if not self._entity_manager:
            log.debug("Skipping convention entity - entity manager not available")
            return None

        try:
            # Extract filename from file:// URL for the entity name
            file_path = document.url.replace("file://", "")
            file_name = file_path.split("/")[-1].replace(".md", "").replace("-", " ").title()

            # Create a summary from the first ~500 chars of content
            description = document.content[:500] if document.content else document.title
            if len(description) > 500:
                description = description[:497] + "..."

            entity = Entity(
                id=f"convention:{document.id!s}",
                name=file_name,
                entity_type=EntityType.CONVENTION,
                description=description,
                content=document.content,
                source_file=file_path,
                metadata={
                    "source_id": str(document.source_id),
                    "url": document.url,
                    "word_count": document.word_count,
                    "headings": document.headings[:10] if document.headings else [],
                },
            )

            entity_id = await self._entity_manager.create_direct(entity)
            log.debug("Created convention entity", entity_id=entity_id, name=file_name)
            return entity_id

        except Exception as e:
            log.warning(
                "Failed to create convention entity",
                url=document.url,
                error=str(e),
            )
            return None

    async def _process_document(
        self,
        document: CrawledDocument,
        stats: IngestionStats,
        source_type: SourceType | None = None,
    ) -> None:
        """Process a single document - store, chunk, embed, integrate with graph.

        Args:
            document: Document to process
            stats: Stats to update
            source_type: Type of source (LOCAL for convention entities)
        """
        db_chunks: list[DocumentChunk] = []

        async with get_session() as session:
            # Check for existing document (deduplication)
            existing = await session.execute(
                select(CrawledDocument).where(col(CrawledDocument.url) == document.url)
            )
            if existing.scalar_one_or_none():
                log.debug("Document already exists, skipping", url=document.url)
                return

            # Store document - handle race condition with concurrent crawls
            try:
                session.add(document)
                await session.flush()
                await session.refresh(document)
            except IntegrityError:
                # Another concurrent crawl already inserted this URL
                log.debug("Document inserted by concurrent crawl, skipping", url=document.url)
                await session.rollback()
                return

            # Chunk document
            chunks = self._chunker.chunk_document(
                document,
                strategy=self.chunk_strategy,
            )
            stats.chunks_created += len(chunks)

            if not chunks:
                log.debug("No chunks created for document", url=document.url)
                return

            # Generate embeddings if enabled
            embeddings = None
            if self.generate_embeddings and self._embedder:
                try:
                    embeddings = await self._embedder.embed_chunks(chunks)
                    stats.embeddings_generated += len(embeddings)
                except Exception as e:
                    log.warning(
                        "Failed to generate embeddings",
                        url=document.url,
                        error=str(e),
                    )

            # Store chunks
            for i, chunk in enumerate(chunks):
                db_chunk = DocumentChunk(
                    document_id=document.id,
                    chunk_index=chunk.chunk_index,
                    chunk_type=chunk.chunk_type,
                    content=chunk.content,
                    context=chunk.context,
                    token_count=chunk.token_count,
                    start_char=chunk.start_char,
                    end_char=chunk.end_char,
                    heading_path=chunk.heading_path,
                    language=chunk.language,
                    embedding=embeddings[i] if embeddings and i < len(embeddings) else None,
                    is_complete=True,
                    has_entities=False,
                    entity_ids=[],
                )
                session.add(db_chunk)
                db_chunks.append(db_chunk)

            # Flush to get chunk IDs for graph integration
            await session.flush()

        # Graph integration: extract entities and link to knowledge graph
        if self._graph_integration and db_chunks:
            try:
                integration_stats = await self._graph_integration.process_chunks(
                    db_chunks,
                    source_name=document.url,
                )
                stats.entities_extracted += integration_stats.entities_extracted
                stats.entities_linked += integration_stats.entities_linked

                # Create DOCUMENTED_IN relationships for linked entities
                entity_uuids = []
                for chunk in db_chunks:
                    if chunk.entity_ids:
                        entity_uuids.extend(chunk.entity_ids)

                if entity_uuids:
                    await self._graph_integration.create_doc_relationships(
                        document.id,
                        list(set(entity_uuids)),  # Dedupe
                    )

            except Exception as e:
                log.warning(
                    "Graph integration failed for document",
                    url=document.url,
                    error=str(e),
                )

        # Create convention entity for local sources
        if source_type == SourceType.LOCAL:
            await self._create_convention_entity(document)

        log.debug(
            "Processed document",
            url=document.url,
            chunks=len(chunks),
            embeddings=len(embeddings) if embeddings else 0,
            entities=stats.entities_extracted,
        )

    async def ingest_url(
        self,
        url: str,
        source: CrawlSource,
    ) -> CrawledDocument | None:
        """Ingest a single URL.

        Args:
            url: URL to crawl
            source: Parent source

        Returns:
            Created document or None if failed
        """
        if not self._crawler:
            raise RuntimeError("Pipeline not started")

        result = await self._crawler.crawl_page(url)
        if not result.success:
            log.warning("Failed to crawl URL", url=url, error=result.error_message)
            return None

        # Create document
        doc = self._crawler.result_to_document(result, source)

        # Store and process
        stats = IngestionStats(source_id=source.id, source_name=source.name)
        await self._process_document(doc, stats, source.source_type)

        return doc


async def ingest_documentation(
    name: str,
    url: str,
    *,
    organization_id: str,
    max_pages: int = 100,
    max_depth: int = 3,
    include_patterns: list[str] | None = None,
) -> IngestionStats:
    """Convenience function to ingest a documentation site.

    Creates a source if needed and runs full ingestion.

    Args:
        name: Human-readable name for the source
        url: Base URL to crawl
        organization_id: Organization UUID for multi-tenant isolation.
        max_pages: Maximum pages to crawl
        max_depth: Maximum link depth
        include_patterns: URL patterns to include

    Returns:
        IngestionStats with results
    """
    from sibyl.crawler.service import create_source, get_source_by_url

    # Get or create source
    source = await get_source_by_url(url)
    if source is None:
        source = await create_source(
            name=name,
            url=url,
            organization_id=organization_id,
            include_patterns=include_patterns,
        )

    # Run ingestion
    async with IngestionPipeline(organization_id) as pipeline:
        return await pipeline.ingest_source(
            source,
            max_pages=max_pages,
            max_depth=max_depth,
        )


async def reingest_source(source_id: UUID, organization_id: str) -> IngestionStats:
    """Re-ingest an existing source.

    Useful for refreshing stale documentation.

    Args:
        source_id: UUID of source to re-ingest
        organization_id: Organization ID for graph operations

    Returns:
        IngestionStats with results
    """
    async with get_session() as session:
        source = await session.get(CrawlSource, source_id)
        if source is None:
            raise ValueError(f"Source not found: {source_id}")

    async with IngestionPipeline(organization_id) as pipeline:
        return await pipeline.ingest_source(source)
