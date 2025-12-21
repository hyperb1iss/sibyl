"""Crawl4AI-powered web crawler service for documentation ingestion.

This service handles:
- Single page and deep crawling of documentation sites
- Clean markdown extraction
- Integration with PostgreSQL document storage
- Progress tracking and error handling
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from urllib.parse import urlparse

import structlog
from crawl4ai import AsyncWebCrawler, BrowserConfig, CacheMode, CrawlerRunConfig
from crawl4ai.deep_crawling import BFSDeepCrawlStrategy
from crawl4ai.deep_crawling.filters import FilterChain, URLPatternFilter
from sqlalchemy import select
from sqlmodel import col

from sibyl.api.websocket import broadcast_event
from sibyl.db import CrawledDocument, CrawlSource, CrawlStatus, SourceType, get_session

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from crawl4ai import CrawlResult

log = structlog.get_logger()


class CrawlerService:
    """Service for crawling documentation sites and storing results.

    Uses Crawl4AI's AsyncWebCrawler for efficient async crawling with:
    - Clean markdown extraction
    - Deep crawling with BFS strategy
    - Automatic deduplication via content hashing
    - Progress tracking and error handling
    """

    def __init__(self) -> None:
        """Initialize the crawler service."""
        self._crawler: AsyncWebCrawler | None = None
        self._browser_config = BrowserConfig(
            headless=True,
            verbose=False,
            text_mode=True,  # Faster, text-only mode
        )

    async def start(self) -> None:
        """Start the crawler (initialize browser)."""
        if self._crawler is None:
            self._crawler = AsyncWebCrawler(config=self._browser_config)
            await self._crawler.start()
            log.info("Crawler service started")

    async def stop(self) -> None:
        """Stop the crawler and release resources."""
        if self._crawler is not None:
            await self._crawler.close()
            self._crawler = None
            log.info("Crawler service stopped")

    async def __aenter__(self) -> CrawlerService:
        """Async context manager entry."""
        await self.start()
        return self

    async def __aexit__(self, *args: object) -> None:
        """Async context manager exit."""
        await self.stop()

    def _get_run_config(
        self,
        *,
        cache_mode: CacheMode = CacheMode.ENABLED,
        word_count_threshold: int = 50,
        excluded_tags: list[str] | None = None,
    ) -> CrawlerRunConfig:
        """Create a CrawlerRunConfig with sensible defaults.

        Args:
            cache_mode: Caching strategy (default: ENABLED)
            word_count_threshold: Minimum words per content block
            excluded_tags: HTML tags to exclude

        Returns:
            Configured CrawlerRunConfig
        """
        if excluded_tags is None:
            excluded_tags = ["nav", "footer", "aside", "header", "script", "style"]

        return CrawlerRunConfig(
            cache_mode=cache_mode,
            word_count_threshold=word_count_threshold,
            excluded_tags=excluded_tags,
            remove_forms=True,
            only_text=False,  # Keep structure for markdown
        )

    async def crawl_page(
        self,
        url: str,
        *,
        cache_mode: CacheMode = CacheMode.ENABLED,
    ) -> CrawlResult:
        """Crawl a single page and return the result.

        Args:
            url: URL to crawl
            cache_mode: Caching strategy

        Returns:
            CrawlResult with markdown content
        """
        if self._crawler is None:
            raise RuntimeError("Crawler not started. Use 'async with' or call start()")

        config = self._get_run_config(cache_mode=cache_mode)
        result = await self._crawler.arun(url=url, config=config)

        log.debug(
            "Crawled page",
            url=url,
            success=result.success,
            content_length=len(result.markdown) if result.markdown else 0,
        )

        return result  # type: ignore[return-value]

    async def crawl_source(
        self,
        source: CrawlSource,
        *,
        max_pages: int = 100,
        max_depth: int = 3,
    ) -> AsyncIterator[CrawledDocument]:
        """Deep crawl a documentation source and yield documents.

        Uses BFS strategy for systematic coverage of documentation sites.
        Respects include/exclude patterns from source configuration.

        Args:
            source: CrawlSource to crawl
            max_pages: Maximum pages to crawl
            max_depth: Maximum link depth to follow

        Yields:
            CrawledDocument for each successfully crawled page
        """
        if self._crawler is None:
            raise RuntimeError("Crawler not started. Use 'async with' or call start()")

        # Build filter chain from source patterns
        filters = []
        if source.include_patterns:
            for pattern in source.include_patterns:
                filters.append(URLPatternFilter(patterns=[pattern]))

        # Configure deep crawl strategy
        # Only pass filter_chain if we have filters
        strategy_kwargs = {
            "max_depth": max_depth,
            "include_external": False,
            "max_pages": max_pages,
        }
        if filters:
            strategy_kwargs["filter_chain"] = FilterChain(filters=filters)

        strategy = BFSDeepCrawlStrategy(**strategy_kwargs)

        # Build config with deep crawl strategy and streaming enabled
        config = CrawlerRunConfig(
            cache_mode=CacheMode.WRITE_ONLY,
            word_count_threshold=50,
            excluded_tags=["nav", "footer", "aside", "header", "script", "style"],
            remove_forms=True,
            only_text=False,
            deep_crawl_strategy=strategy,
            stream=True,  # Enable async iteration
        )

        log.info(
            "Starting deep crawl",
            source=source.name,
            url=source.url,
            max_pages=max_pages,
            max_depth=max_depth,
        )

        # Update source status - fetch fresh to avoid detached instance issues
        source_id = source.id
        async with get_session() as session:
            db_source = await session.get(CrawlSource, source_id)
            if db_source:
                db_source.crawl_status = CrawlStatus.IN_PROGRESS

        # Perform deep crawl
        crawled_count = 0
        error_count = 0

        try:
            async for result in await self._crawler.arun(  # type: ignore[union-attr]
                url=source.url,
                config=config,
            ):
                if not result.success:
                    error_count += 1
                    log.warning("Failed to crawl page", url=result.url, error=result.error_message)
                    continue

                # Create document from result
                doc = self.result_to_document(result, source)
                crawled_count += 1

                log.debug(
                    "Crawled document",
                    url=result.url,
                    title=doc.title,
                    words=doc.word_count,
                )

                # Broadcast progress every page
                await broadcast_event(
                    "crawl_progress",
                    {
                        "source_id": str(source_id),
                        "pages_crawled": crawled_count,
                        "max_pages": max_pages,
                        "current_url": result.url,
                        "percentage": min(100, int((crawled_count / max_pages) * 100)),
                    },
                )

                yield doc

        except Exception as e:
            log.error("Deep crawl failed", source=source.name, error=str(e))  # noqa: TRY400
            async with get_session() as session:
                db_source = await session.get(CrawlSource, source_id)
                if db_source:
                    db_source.crawl_status = CrawlStatus.FAILED
                    db_source.current_job_id = None  # Clear job on failure
                    db_source.last_error = str(e)
            raise

        # Update source with results
        async with get_session() as session:
            db_source = await session.get(CrawlSource, source_id)
            if db_source:
                db_source.crawl_status = CrawlStatus.COMPLETED if error_count == 0 else CrawlStatus.PARTIAL
                db_source.current_job_id = None  # Clear job on completion
                db_source.last_crawled_at = datetime.now(UTC)
                db_source.document_count = crawled_count

        log.info(
            "Deep crawl completed",
            source=source.name,
            crawled=crawled_count,
            errors=error_count,
        )

    def result_to_document(
        self,
        result: CrawlResult,
        source: CrawlSource,
    ) -> CrawledDocument:
        """Convert a CrawlResult to a CrawledDocument.

        Args:
            result: Crawl4AI result
            source: Parent source

        Returns:
            CrawledDocument ready for storage
        """
        # Extract content
        content = result.markdown or ""
        raw_content = result.html or ""

        # Compute content hash for deduplication
        content_hash = hashlib.sha256(content.encode()).hexdigest()[:64]

        # Extract metadata
        title = self._extract_title(result)
        headings = self._extract_headings(content)
        links = [link.get("href", "") for link in (result.links or {}).get("internal", [])]
        code_languages = self._detect_code_languages(content)

        # Compute metrics
        word_count = len(content.split()) if content else 0
        token_count = word_count * 4 // 3  # Rough estimate

        # Determine depth from URL path
        parsed = urlparse(result.url)
        depth = len([p for p in parsed.path.split("/") if p])

        return CrawledDocument(
            source_id=source.id,
            url=result.url,
            title=title,
            raw_content=raw_content[:100000],  # Limit raw content size
            content=content,
            content_hash=content_hash,
            depth=depth,
            word_count=word_count,
            token_count=token_count,
            has_code=bool(code_languages),
            is_index=self._is_index_page(result.url, content),
            headings=headings[:50],  # Limit headings
            links=links[:200],  # Limit links
            code_languages=code_languages,
            http_status=200,  # Crawl4AI doesn't expose this directly
        )

    def _extract_title(self, result: CrawlResult) -> str:
        """Extract page title from result."""
        # Try to get from metadata first
        if result.metadata and result.metadata.get("title"):
            return result.metadata["title"][:512]

        # Fall back to first H1 in markdown
        if result.markdown:
            for line in result.markdown.split("\n"):
                if line.startswith("# "):
                    return line[2:].strip()[:512]

        # Use URL path as fallback
        parsed = urlparse(result.url)
        path_parts = [p for p in parsed.path.split("/") if p]
        if path_parts:
            return path_parts[-1].replace("-", " ").replace("_", " ").title()[:512]

        return "Untitled"

    def _extract_headings(self, content: str) -> list[str]:
        """Extract headings from markdown content."""
        headings = []
        for line in content.split("\n"):
            if line.startswith("#"):
                # Remove # prefix and clean
                heading = line.lstrip("#").strip()
                if heading:
                    headings.append(heading[:200])
        return headings

    def _detect_code_languages(self, content: str) -> list[str]:
        """Detect programming languages from code blocks."""
        languages = set()
        in_code_block = False
        for line in content.split("\n"):
            if line.startswith("```"):
                if not in_code_block:
                    # Extract language from opening fence
                    lang = line[3:].strip().split()[0] if line[3:].strip() else ""
                    if lang and lang not in ("", "text", "plaintext"):
                        languages.add(lang.lower())
                in_code_block = not in_code_block
        return list(languages)[:10]

    def _is_index_page(self, url: str, content: str) -> bool:
        """Detect if this is an index/listing page."""
        parsed = urlparse(url)
        path = parsed.path.rstrip("/")

        # Common index paths
        if path.endswith(("/index", "/readme", "")) or path in ("/docs", "/documentation"):
            return True

        # Check for high link-to-content ratio (listing pages)
        if content:
            link_count = content.count("](")
            word_count = len(content.split())
            if word_count > 0 and link_count / word_count > 0.1:
                return True

        return False


async def create_source(
    name: str,
    url: str,
    *,
    source_type: SourceType = SourceType.WEBSITE,
    description: str | None = None,
    crawl_depth: int = 2,
    include_patterns: list[str] | None = None,
    exclude_patterns: list[str] | None = None,
) -> CrawlSource:
    """Create a new crawl source in the database.

    Args:
        name: Human-readable name
        url: Base URL to crawl
        source_type: Type of source
        description: Optional description
        crawl_depth: Maximum depth to follow links
        include_patterns: URL patterns to include (regex)
        exclude_patterns: URL patterns to exclude (regex)

    Returns:
        Created CrawlSource
    """
    async with get_session() as session:
        source = CrawlSource(
            name=name,
            url=url.rstrip("/"),
            source_type=source_type,
            description=description,
            crawl_depth=crawl_depth,
            include_patterns=include_patterns or [],
            exclude_patterns=exclude_patterns or [],
        )
        session.add(source)
        await session.flush()
        await session.refresh(source)
        log.info("Created crawl source", name=name, url=url, id=str(source.id))
        return source


async def get_source_by_url(url: str) -> CrawlSource | None:
    """Get a crawl source by URL."""
    async with get_session() as session:
        result = await session.execute(
            select(CrawlSource).where(col(CrawlSource.url) == url.rstrip("/"))
        )
        return result.scalar_one_or_none()


async def list_sources(
    *,
    status: CrawlStatus | None = None,
    limit: int = 50,
) -> list[CrawlSource]:
    """List crawl sources with optional filtering."""
    async with get_session() as session:
        query = select(CrawlSource)
        if status:
            query = query.where(col(CrawlSource.crawl_status) == status)
        query = query.limit(limit)
        result = await session.execute(query)
        return list(result.scalars().all())
