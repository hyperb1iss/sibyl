"""Local file system crawler for markdown directories.

This module provides local file ingestion support, allowing Sibyl to
ingest markdown files from local directories (like ~/dev/conventions).
"""

from collections.abc import AsyncIterator
from hashlib import sha256
from pathlib import Path
from urllib.parse import urlparse

import structlog

from sibyl.db.models import CrawledDocument, CrawlSource
from sibyl.ingestion.parser import ParsedDocument, parse_directory

log = structlog.get_logger()


class LocalFileCrawler:
    """Crawler for local markdown directories.

    Provides the same interface as CrawlerService but reads from the
    local filesystem instead of web URLs.
    """

    def _parse_path(self, url: str) -> Path:
        """Parse a file:// URL or path into a Path object.

        Args:
            url: Either a file:// URL or an absolute/relative path

        Returns:
            Resolved Path object

        Raises:
            ValueError: If path doesn't exist or isn't a directory
        """
        # Handle file:// URLs
        if url.startswith("file://"):
            parsed = urlparse(url)
            path = Path(parsed.path)
        else:
            path = Path(url)

        # Expand ~ and resolve
        path = path.expanduser().resolve()

        if not path.exists():
            raise ValueError(f"Path does not exist: {path}")
        if not path.is_dir():
            raise ValueError(f"Path is not a directory: {path}")

        return path

    def _to_crawled_document(
        self,
        parsed: ParsedDocument,
        source: CrawlSource,
        base_path: Path,
    ) -> CrawledDocument:
        """Convert a ParsedDocument to a CrawledDocument.

        Args:
            parsed: The parsed markdown document
            source: The source this document belongs to
            base_path: Base directory for relative path calculation

        Returns:
            CrawledDocument ready for storage
        """
        # Create a file:// URL for the document
        file_url = f"file://{parsed.file_path}"

        # Calculate relative path for hierarchy
        try:
            relative_path = parsed.file_path.relative_to(base_path)
            parent_dir = relative_path.parent
            parent_url = f"file://{base_path / parent_dir}" if parent_dir != Path(".") else None
        except ValueError:
            parent_url = None

        # Content hash for deduplication
        content_hash = sha256(parsed.raw_content.encode()).hexdigest()

        # Extract metadata from path structure
        # e.g., docs/wisdom/languages/typescript.md -> section_path: [docs, wisdom, languages]
        path_parts = list(parsed.file_path.relative_to(base_path).parts[:-1])

        # Extract headings from sections
        headings = [s.title for s in parsed.all_sections_flat if s.title]

        # Extract code languages from code blocks
        code_languages: list[str] = []
        for section in parsed.all_sections_flat:
            for block in section.code_blocks:
                if block.language and block.language not in code_languages:
                    code_languages.append(block.language)

        return CrawledDocument(
            source_id=source.id,
            url=file_url,
            title=parsed.title or parsed.file_path.stem,
            raw_content=parsed.raw_content,
            content=parsed.raw_content,  # Already markdown, no extraction needed
            content_hash=content_hash,
            parent_url=parent_url,
            section_path=path_parts,
            depth=len(path_parts),
            word_count=parsed.word_count,
            headings=headings,
            code_languages=code_languages,
            has_code=len(code_languages) > 0,
        )

    async def crawl_source(
        self,
        source: CrawlSource,
        *,
        max_pages: int = 100,
        max_depth: int = 3,  # Not used for local, but matches interface
    ) -> AsyncIterator[CrawledDocument]:
        """Crawl a local directory and yield documents.

        Args:
            source: CrawlSource with file:// URL or local path
            max_pages: Maximum files to process
            max_depth: Ignored for local sources (all files included)

        Yields:
            CrawledDocument for each markdown file found
        """
        try:
            base_path = self._parse_path(source.url)
        except ValueError:
            log.exception("invalid_local_path", url=source.url)
            return

        log.info(
            "crawling_local_directory",
            path=str(base_path),
            source_id=str(source.id),
            max_pages=max_pages,
        )

        # Parse all markdown files
        # Include .md and common template extensions
        patterns = ["**/*.md", "**/*.template"]
        all_docs: list[ParsedDocument] = []

        for pattern in patterns:
            docs = parse_directory(base_path, pattern)
            all_docs.extend(docs)

        # Apply include/exclude patterns if configured
        if source.include_patterns:
            import re

            include_regexes = [re.compile(p) for p in source.include_patterns]
            all_docs = [
                d for d in all_docs if any(r.search(str(d.file_path)) for r in include_regexes)
            ]

        if source.exclude_patterns:
            import re

            exclude_regexes = [re.compile(p) for p in source.exclude_patterns]
            all_docs = [
                d for d in all_docs if not any(r.search(str(d.file_path)) for r in exclude_regexes)
            ]

        # Limit to max_pages
        all_docs = all_docs[:max_pages]

        log.info(
            "found_local_documents",
            count=len(all_docs),
            path=str(base_path),
        )

        # Yield documents
        for parsed in all_docs:
            try:
                doc = self._to_crawled_document(parsed, source, base_path)
                yield doc
            except Exception as e:
                log.warning(
                    "failed_to_convert_document",
                    file=str(parsed.file_path),
                    error=str(e),
                )

        log.info(
            "local_crawl_complete",
            source_id=str(source.id),
            documents=len(all_docs),
        )
