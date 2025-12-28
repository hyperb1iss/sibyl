"""Sibyl crawler module - Crawl4AI-powered documentation ingestion.

This module provides:
- Web crawling with Crawl4AI
- llms.txt discovery and parsing for AI-friendly content
- Smart chunking for RAG retrieval
- Embedding generation with OpenAI
- Full ingestion pipeline

Usage:
    from sibyl.crawler import ingest_documentation

    stats = await ingest_documentation(
        name="FastAPI Docs",
        url="https://fastapi.tiangolo.com",
        max_pages=50,
    )
    print(f"Ingested {stats.documents_stored} documents")
"""

from sibyl.crawler.chunker import (
    Chunk,
    ChunkStrategy,
    DocumentChunker,
    chunk_document,
)
from sibyl.crawler.discovery import (
    DiscoveryResult,
    DiscoveryService,
    is_llms_variant,
)
from sibyl.crawler.embedder import (
    EmbeddingService,
    embed_chunks,
    embed_text,
    get_embedding_service,
)
from sibyl.crawler.llms_parser import (
    LLMsSection,
    parse_llms_full,
)
from sibyl.crawler.local import LocalFileCrawler
from sibyl.crawler.pipeline import (
    IngestionPipeline,
    IngestionStats,
    ingest_documentation,
    reingest_source,
)
from sibyl.crawler.service import (
    CrawlerService,
    create_source,
    get_source_by_url,
    list_sources,
)

__all__ = [
    # Pipeline
    "IngestionPipeline",
    "IngestionStats",
    "ingest_documentation",
    "reingest_source",
    # Crawler
    "CrawlerService",
    "LocalFileCrawler",
    "create_source",
    "get_source_by_url",
    "list_sources",
    # Discovery
    "DiscoveryService",
    "DiscoveryResult",
    "is_llms_variant",
    # llms.txt Parser
    "LLMsSection",
    "parse_llms_full",
    # Chunker
    "Chunk",
    "ChunkStrategy",
    "DocumentChunker",
    "chunk_document",
    # Embedder
    "EmbeddingService",
    "embed_chunks",
    "embed_text",
    "get_embedding_service",
]
