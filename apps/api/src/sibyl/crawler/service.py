"""Crawl4AI-powered web crawler service for documentation ingestion.

This service handles:
- Single page and deep crawling of documentation sites
- llms.txt discovery and parsing for AI-friendly content
- Clean markdown extraction
- Integration with runtime document storage
- Progress tracking and error handling
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import AsyncIterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast
from urllib.parse import urljoin, urlparse
from uuid import UUID

import httpx
import structlog
from crawl4ai import AsyncWebCrawler, BrowserConfig, CacheMode, CrawlerRunConfig
from crawl4ai.deep_crawling import BFSDeepCrawlStrategy
from crawl4ai.deep_crawling.filters import FilterChain, URLPatternFilter

from sibyl.api.event_types import WSEvent
from sibyl.api.websocket import broadcast_event
from sibyl.crawler.discovery import DiscoveryResult, DiscoveryService
from sibyl.crawler.llms_parser import LLMsSection, parse_llms_full
from sibyl.persistence.content_common import (
    CrawledDocumentRecord,
    CrawlSourceRecord,
    utcnow_naive,
)
from sibyl.persistence.content_runtime import (
    create_crawl_source_record,
    get_content_read_session,
    get_crawl_source_by_id,
    get_crawl_source_by_url,
    list_crawl_sources,
    list_crawl_sources_for_org,
    save_crawl_source_record,
)
from sibyl_core.models import CrawlStatus, SourceType

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from crawl4ai import CrawlResult

log = structlog.get_logger()

# Patterns to strip from crawled markdown content (navigation cruft)
_NAV_CRUFT_PATTERNS = [
    # Skip to content links (Docusaurus, etc.)
    re.compile(r"^\[Skip to (?:main )?content\]\([^)]+\)\s*", re.MULTILINE | re.IGNORECASE),
    # "On this page" sidebar headers
    re.compile(r"^On this page\s*$", re.MULTILINE | re.IGNORECASE),
    # Breadcrumb navigation like "Docs > Getting Started > Installation"
    re.compile(r"^(?:Docs|Documentation)\s*[>›]\s*.+$", re.MULTILINE),
    # Common nav links at top/bottom
    re.compile(r"^\[(?:Previous|Next|Edit this page|Report an issue)\]\([^)]+\)\s*$", re.MULTILINE),
    # Table of contents markers
    re.compile(r"^(?:Table of [Cc]ontents|Contents)\s*$", re.MULTILINE),
]


class SourceAlreadyExistsError(ValueError):
    """Raised when an org already has a source for the requested URL."""


@dataclass(slots=True)
class CrawlSourcePage:
    """Org-scoped crawl-source listing with total count."""

    sources: list[CrawlSourceRecord]
    total: int


def _clean_nav_cruft(content: str) -> str:
    """Remove common navigation cruft from markdown content.

    Strips:
    - Skip to content links
    - On this page sidebar text
    - Previous/Next navigation links
    - Edit this page links
    """
    for pattern in _NAV_CRUFT_PATTERNS:
        content = pattern.sub("", content)
    # Clean up multiple blank lines left behind
    content = re.sub(r"\n{3,}", "\n\n", content)
    return content.strip()


async def _save_source_update(source_id: UUID, **updates: object) -> CrawlSourceRecord | None:
    async with get_content_read_session() as session:
        source = await get_crawl_source_by_id(session, source_id=source_id)
        if source is None:
            return None
        for field, value in updates.items():
            setattr(source, field, value)
        return await save_crawl_source_record(session, source=source)


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

    async def restart(self) -> None:
        """Restart the crawler (useful after browser crashes)."""
        log.warning("Restarting crawler after browser failure")
        await self.stop()
        await self.start()

    def _is_browser_death(self, error: Exception) -> bool:
        """Check if an error indicates the browser has died."""
        error_msg = str(error).lower()
        return any(
            pattern in error_msg
            for pattern in [
                "'nonetype' object has no attribute 'new_context'",
                "target page, context or browser has been closed",
                "browser has been closed",
                "connection closed",
            ]
        )

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
        source: CrawlSourceRecord,
        *,
        max_pages: int = 100,
        max_depth: int = 3,
    ) -> AsyncIterator[CrawledDocumentRecord]:
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
        filters: list[URLPatternFilter] = [
            # Always exclude localhost/loopback URLs (common in docs as examples)
            URLPatternFilter(
                patterns=["*localhost*", "*127.0.0.1*", "*0.0.0.0*"],
                reverse=True,  # reverse=True means EXCLUDE matching URLs
                use_glob=True,
            ),
        ]
        if source.include_patterns:
            filters.extend(
                URLPatternFilter(patterns=[pattern]) for pattern in source.include_patterns
            )

        # Configure deep crawl strategy with filter chain
        strategy_kwargs = {
            "max_depth": max_depth,
            "include_external": False,
            "max_pages": max_pages,
            "filter_chain": FilterChain(filters=filters),
        }

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
            semaphore_count=3,  # Reduce concurrency to avoid browser context exhaustion
            page_timeout=90000,  # 90s timeout for slow pages
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
        await _save_source_update(source_id, crawl_status=CrawlStatus.IN_PROGRESS)

        # Perform deep crawl
        crawled_count = 0
        error_count = 0

        async def process_result(result: CrawlResult) -> CrawledDocumentRecord | None:
            nonlocal crawled_count, error_count

            if not result.success:
                error_count += 1
                log.warning("Failed to crawl page", url=result.url, error=result.error_message)
                return None

            doc = self.result_to_document(result, source)
            crawled_count += 1

            log.debug(
                "Crawled document",
                url=result.url,
                title=doc.title,
                words=doc.word_count,
            )

            await broadcast_event(
                WSEvent.CRAWL_PROGRESS,
                {
                    "source_id": str(source_id),
                    "pages_crawled": crawled_count,
                    "max_pages": max_pages,
                    "current_url": result.url,
                    "percentage": min(100, int((crawled_count / max_pages) * 100)),
                },
            )

            return doc

        try:
            crawl_results = await self._crawler.arun(
                url=source.url,
                config=config,
            )
            if isinstance(crawl_results, AsyncIterable):
                async for raw_result in crawl_results:
                    doc = await process_result(cast("CrawlResult", raw_result))
                    if doc is not None:
                        yield doc
            else:
                doc = await process_result(cast("CrawlResult", crawl_results))
                if doc is not None:
                    yield doc

        except Exception as e:
            # Check if this is a browser death - handle gracefully
            if self._is_browser_death(e):
                log.warning(
                    "Browser died during crawl - marking as partial",
                    source=source.name,
                    crawled=crawled_count,
                    error=str(e),
                )
                error_count += 1
                await _save_source_update(
                    source_id,
                    crawl_status=CrawlStatus.PARTIAL,
                    current_job_id=None,
                    last_crawled_at=utcnow_naive(),
                    document_count=crawled_count,
                    last_error=f"Browser crashed after {crawled_count} pages",
                )

                # Restart browser for next source
                await self.restart()

                # Don't raise - we already yielded some documents
                log.info(
                    "Partial crawl completed after browser recovery",
                    source=source.name,
                    crawled=crawled_count,
                    errors=error_count,
                )
                return

            # Other errors - fail as before
            log.error("Deep crawl failed", source=source.name, error=str(e))  # noqa: TRY400
            await _save_source_update(
                source_id,
                crawl_status=CrawlStatus.FAILED,
                current_job_id=None,
                last_error=str(e),
            )
            raise

        # Update source with results
        await _save_source_update(
            source_id,
            crawl_status=CrawlStatus.COMPLETED if error_count == 0 else CrawlStatus.PARTIAL,
            current_job_id=None,
            last_crawled_at=utcnow_naive(),
            document_count=crawled_count,
        )

        log.info(
            "Deep crawl completed",
            source=source.name,
            crawled=crawled_count,
            errors=error_count,
        )

    def result_to_document(
        self,
        result: CrawlResult,
        source: CrawlSourceRecord,
    ) -> CrawledDocumentRecord:
        """Convert a CrawlResult to a CrawledDocument.

        Args:
            result: Crawl4AI result
            source: Parent source

        Returns:
            CrawledDocument ready for storage
        """
        # Extract and clean content
        content = _clean_nav_cruft(result.markdown or "")
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

        return CrawledDocumentRecord(
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

    async def fetch_favicon(self, base_url: str) -> str | None:
        """Attempt to find a favicon for a website.

        Tries common favicon locations and HTML meta tags.

        Args:
            base_url: Base URL of the site

        Returns:
            Favicon URL if found, None otherwise
        """
        parsed = urlparse(base_url)
        origin = f"{parsed.scheme}://{parsed.netloc}"

        # Common favicon locations to try (in order of preference)
        favicon_paths = [
            "/favicon.ico",
            "/favicon.png",
            "/apple-touch-icon.png",
            "/apple-touch-icon-precomposed.png",
        ]

        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            # Try common paths first
            for path in favicon_paths:
                url = urljoin(origin, path)
                try:
                    response = await client.head(url)
                    if response.status_code == 200:
                        content_type = response.headers.get("content-type", "")
                        if "image" in content_type or path.endswith((".ico", ".png")):
                            log.debug("Found favicon", url=url)
                            return url
                except Exception:
                    continue

            # Try parsing HTML for link tags
            try:
                response = await client.get(origin, timeout=15.0)
                if response.status_code == 200:
                    html = response.text
                    favicon_url = self._extract_favicon_from_html(html, origin)
                    if favicon_url:
                        log.debug("Found favicon from HTML", url=favicon_url)
                        return favicon_url
            except Exception as e:
                log.debug("Failed to fetch HTML for favicon", error=str(e))

        return None

    def _extract_favicon_from_html(self, html: str, base_url: str) -> str | None:
        """Extract favicon URL from HTML link tags."""
        # Match <link rel="icon" href="..."> or <link rel="shortcut icon" href="...">
        # Also match apple-touch-icon
        patterns = [
            r'<link[^>]*rel=["\'](?:shortcut )?icon["\'][^>]*href=["\']([^"\']+)["\']',
            r'<link[^>]*href=["\']([^"\']+)["\'][^>]*rel=["\'](?:shortcut )?icon["\']',
            r'<link[^>]*rel=["\']apple-touch-icon["\'][^>]*href=["\']([^"\']+)["\']',
        ]

        for pattern in patterns:
            match = re.search(pattern, html, re.IGNORECASE)
            if match:
                href = match.group(1)
                # Resolve relative URLs
                if href.startswith("//"):
                    return f"https:{href}"
                if href.startswith("/"):
                    return urljoin(base_url, href)
                if not href.startswith("http"):
                    return urljoin(base_url, href)
                return href

        return None

    # =========================================================================
    # llms.txt Discovery and Processing
    # =========================================================================

    def section_to_document(
        self,
        section: LLMsSection,
        source: CrawlSourceRecord,
    ) -> CrawledDocumentRecord:
        """Convert an llms-full.txt section to a CrawledDocument.

        Args:
            section: Parsed section from llms-full.txt
            source: Parent source

        Returns:
            CrawledDocument ready for storage
        """
        content = _clean_nav_cruft(section.content)
        content_hash = hashlib.sha256(content.encode()).hexdigest()[:64]
        headings = self._extract_headings(content)
        code_languages = self._detect_code_languages(content)

        return CrawledDocumentRecord(
            source_id=source.id,
            url=section.url,
            title=section.title,
            raw_content=content,  # For llms.txt, raw == markdown
            content=content,
            content_hash=content_hash,
            depth=0,  # Sections are top-level
            word_count=section.word_count,
            token_count=section.word_count * 4 // 3,
            has_code=bool(code_languages),
            is_index=False,
            headings=headings[:50],
            links=[],
            code_languages=code_languages,
            http_status=200,
        )

    async def crawl_with_discovery(
        self,
        source: CrawlSourceRecord,
        *,
        max_pages: int = 100,
        max_depth: int = 3,
    ) -> AsyncIterator[CrawledDocumentRecord]:
        """Crawl a source with llms.txt discovery.

        First probes for llms.txt, llms-full.txt, etc. If found:
        - llms-full.txt: Parse into sections, yield as documents
        - llms.txt with links: Follow links to crawl referenced pages
        - Otherwise: Fall back to normal deep crawling

        Args:
            source: CrawlSource to crawl
            max_pages: Maximum pages to crawl
            max_depth: Maximum link depth

        Yields:
            CrawledDocument for each successfully processed page/section
        """
        log.info(
            "Starting crawl with discovery",
            source=source.name,
            url=source.url,
        )

        # Run discovery first
        discovery_result: DiscoveryResult | None = None
        try:
            async with DiscoveryService() as discovery:
                discovery_result = await discovery.discover(source.url)
        except Exception as e:
            log.warning("Discovery failed, falling back to deep crawl", error=str(e))

        if discovery_result:
            log.info(
                "Discovery found AI-friendly file",
                file_type=discovery_result.file_type,
                url=discovery_result.url,
                links=len(discovery_result.links),
                is_link_collection=discovery_result.is_link_collection,
            )

            # Handle llms-full.txt - parse into sections
            if discovery_result.file_type == "llms-full":
                log.info("Processing llms-full.txt sections")
                sections = parse_llms_full(
                    discovery_result.content,
                    discovery_result.url,
                )

                for section in sections:
                    doc = self.section_to_document(section, source)
                    log.debug(
                        "Yielding llms-full section",
                        title=section.title,
                        words=section.word_count,
                    )
                    yield doc

                # Also follow any links in the llms-full.txt
                if discovery_result.links:
                    async for doc in self._crawl_discovered_links(
                        source,
                        discovery_result.links,
                        max_pages=max_pages - len(sections),
                    ):
                        yield doc
                return

            # Handle llms.txt link collection - follow links
            if discovery_result.is_link_collection and discovery_result.links:
                log.info(
                    "Following llms.txt links",
                    link_count=len(discovery_result.links),
                )
                async for doc in self._crawl_discovered_links(
                    source,
                    discovery_result.links,
                    max_pages=max_pages,
                ):
                    yield doc
                return

            # Handle llms.txt with content - yield as document, then follow links
            if discovery_result.file_type == "llms" and not discovery_result.is_link_collection:
                # Create document from llms.txt content itself
                llms_doc = CrawledDocumentRecord(
                    source_id=source.id,
                    url=discovery_result.url,
                    title="LLMs Documentation Guide",
                    raw_content=discovery_result.content,
                    content=discovery_result.content,
                    content_hash=hashlib.sha256(discovery_result.content.encode()).hexdigest()[:64],
                    depth=0,
                    word_count=len(discovery_result.content.split()),
                    token_count=len(discovery_result.content.split()) * 4 // 3,
                    has_code=bool(self._detect_code_languages(discovery_result.content)),
                    is_index=True,
                    headings=self._extract_headings(discovery_result.content)[:50],
                    links=discovery_result.links[:200],
                    code_languages=self._detect_code_languages(discovery_result.content),
                    http_status=200,
                )
                yield llms_doc

                # Follow links if present
                if discovery_result.links:
                    async for doc in self._crawl_discovered_links(
                        source,
                        discovery_result.links,
                        max_pages=max_pages - 1,
                    ):
                        yield doc
                return

        # Fall back to normal deep crawling
        log.info("No usable llms.txt found, using deep crawl")
        async for doc in self.crawl_source(
            source,
            max_pages=max_pages,
            max_depth=max_depth,
        ):
            yield doc

    async def _crawl_discovered_links(
        self,
        source: CrawlSourceRecord,
        links: list[str],
        *,
        max_pages: int = 100,
    ) -> AsyncIterator[CrawledDocumentRecord]:
        """Crawl specific URLs from llms.txt links.

        Args:
            source: Parent source
            links: List of URLs to crawl
            max_pages: Maximum pages to crawl

        Yields:
            CrawledDocument for each successfully crawled page
        """
        if self._crawler is None:
            raise RuntimeError("Crawler not started")

        crawled = 0
        for url in links[:max_pages]:
            try:
                result = await self.crawl_page(url)
                if result.success:
                    doc = self.result_to_document(result, source)
                    crawled += 1
                    log.debug(
                        "Crawled linked page",
                        url=url,
                        title=doc.title,
                        count=crawled,
                    )

                    await broadcast_event(
                        WSEvent.CRAWL_PROGRESS,
                        {
                            "source_id": str(source.id),
                            "pages_crawled": crawled,
                            "max_pages": min(len(links), max_pages),
                            "current_url": url,
                            "percentage": min(
                                100,
                                int((crawled / min(len(links), max_pages)) * 100),
                            ),
                        },
                    )

                    yield doc
                else:
                    log.warning("Failed to crawl linked page", url=url)
            except Exception as e:
                log.warning("Error crawling linked page", url=url, error=str(e))
                continue

        log.info("Finished crawling discovered links", crawled=crawled, total=len(links))


async def create_source(
    name: str,
    url: str,
    *,
    organization_id: str,
    source_type: SourceType = SourceType.WEBSITE,
    description: str | None = None,
    crawl_depth: int = 2,
    include_patterns: list[str] | None = None,
    exclude_patterns: list[str] | None = None,
) -> CrawlSourceRecord:
    """Create a new crawl source in the database.

    Args:
        name: Human-readable name
        url: Base URL to crawl
        organization_id: Organization UUID for multi-tenant isolation.
        source_type: Type of source
        description: Optional description
        crawl_depth: Maximum depth to follow links
        include_patterns: URL patterns to include (regex)
        exclude_patterns: URL patterns to exclude (regex)

    Returns:
        Created CrawlSource
    """
    async with get_content_read_session() as session:
        return await create_crawl_source_record(
            session,
            name=name,
            url=url,
            organization_id=UUID(organization_id),
            source_type=source_type,
            description=description,
            crawl_depth=crawl_depth,
            include_patterns=include_patterns,
            exclude_patterns=exclude_patterns,
        )


async def create_org_source(
    name: str,
    url: str,
    *,
    organization_id: UUID,
    source_type: SourceType = SourceType.WEBSITE,
    description: str | None = None,
    crawl_depth: int = 2,
    include_patterns: list[str] | None = None,
    exclude_patterns: list[str] | None = None,
) -> CrawlSourceRecord:
    """Create a crawl source for an organization, rejecting same-org duplicates."""
    async with get_content_read_session() as session:
        return await create_crawl_source_record(
            session,
            name=name,
            url=url,
            organization_id=organization_id,
            source_type=source_type,
            description=description,
            crawl_depth=crawl_depth,
            include_patterns=include_patterns,
            exclude_patterns=exclude_patterns,
        )


async def get_source_by_url(url: str) -> CrawlSourceRecord | None:
    """Get a crawl source by URL."""
    async with get_content_read_session() as session:
        return await get_crawl_source_by_url(session, url=_normalize_source_url(url))


async def list_sources(
    *,
    status: CrawlStatus | None = None,
    limit: int = 50,
) -> list[CrawlSourceRecord]:
    """List crawl sources with optional filtering."""
    async with get_content_read_session() as session:
        return await list_crawl_sources(session, status=status, limit=limit)


async def list_org_sources(
    *,
    organization_id: UUID,
    status: CrawlStatus | None = None,
    limit: int = 50,
) -> CrawlSourcePage:
    """List crawl sources for a single organization."""
    async with get_content_read_session() as session:
        sources, total = await list_crawl_sources_for_org(
            session,
            organization_id=organization_id,
            status=status,
            limit=limit,
        )
    return CrawlSourcePage(sources=sources, total=total)


def _normalize_source_url(url: str) -> str:
    return url.rstrip("/")
