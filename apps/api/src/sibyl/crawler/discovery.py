"""Discovery service for llms.txt, sitemap.xml, and related files.

Auto-discovers AI-friendly documentation files at domain roots to guide crawling.
Priority order: llms.txt > llms-full.txt > sitemap.xml > robots.txt > .well-known/*
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

import httpx
import structlog

log = structlog.get_logger()

# Discovery priority - try in order, use first found
DISCOVERY_FILES = [
    "llms.txt",
    "llms-full.txt",
    "sitemap.xml",
    "robots.txt",
    ".well-known/llms.txt",
    ".well-known/ai.txt",
    ".well-known/sitemap.xml",
]


@dataclass
class DiscoveryResult:
    """Result of file discovery."""

    url: str
    file_type: str  # "llms", "llms-full", "sitemap", "robots"
    content: str
    links: list[str] = field(default_factory=list)
    is_link_collection: bool = False


class DiscoveryService:
    """Discovers llms.txt, sitemap.xml, and related files for a domain."""

    def __init__(self, timeout: float = 10.0) -> None:
        self.timeout = timeout
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> DiscoveryService:
        self._client = httpx.AsyncClient(
            timeout=self.timeout,
            follow_redirects=True,
            headers={"User-Agent": "Sibyl/1.0 (AI Documentation Crawler)"},
        )
        return self

    async def __aexit__(self, *args: object) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    def _get_base_url(self, url: str) -> str:
        """Extract base URL (scheme + host) from a URL."""
        parsed = urlparse(url)
        return f"{parsed.scheme}://{parsed.netloc}"

    def _classify_file(self, filename: str) -> str:
        """Classify a discovered file by type."""
        filename = filename.lower()
        if "llms-full" in filename:
            return "llms-full"
        if "llms" in filename:
            return "llms"
        if "sitemap" in filename:
            return "sitemap"
        if "robots" in filename:
            return "robots"
        if "ai.txt" in filename:
            return "llms"  # Treat ai.txt as llms variant
        return "unknown"

    async def discover(self, base_url: str) -> DiscoveryResult | None:
        """Discover the best AI-friendly file for a domain.

        Probes for llms.txt, sitemap.xml, etc. in priority order.
        Returns the first successful result.

        Args:
            base_url: URL to discover files for (uses domain root)

        Returns:
            DiscoveryResult if found, None otherwise
        """
        if not self._client:
            raise RuntimeError("DiscoveryService not started. Use 'async with'.")

        root = self._get_base_url(base_url)

        for filename in DISCOVERY_FILES:
            probe_url = urljoin(root + "/", filename)

            try:
                response = await self._client.get(probe_url)

                if response.status_code == 200:
                    # Validate content type - must be text, not HTML
                    content_type = response.headers.get("content-type", "").lower()
                    if "text/html" in content_type:
                        log.debug(
                            "Skipping - got HTML instead of text file",
                            url=probe_url,
                            content_type=content_type,
                        )
                        continue

                    content = response.text

                    # Also reject if content looks like HTML
                    if content.strip().startswith(("<!DOCTYPE", "<html", "<!doctype")):
                        log.debug(
                            "Skipping - content is HTML",
                            url=probe_url,
                        )
                        continue

                    file_type = self._classify_file(filename)

                    # Validate content is useful
                    if not self._is_useful_content(content, probe_url):
                        log.debug(
                            "Discovered file is placeholder/stub",
                            url=probe_url,
                        )
                        continue

                    # Extract links if it's an llms.txt variant
                    links: list[str] = []
                    is_link_collection = False

                    if file_type in ("llms", "llms-full"):
                        links = self.extract_markdown_links(content, probe_url)
                        is_link_collection = self._is_link_collection(content, links)

                    log.info(
                        "Discovered AI-friendly file",
                        url=probe_url,
                        file_type=file_type,
                        links_found=len(links),
                        is_link_collection=is_link_collection,
                    )

                    return DiscoveryResult(
                        url=probe_url,
                        file_type=file_type,
                        content=content,
                        links=links,
                        is_link_collection=is_link_collection,
                    )

            except httpx.HTTPError as e:
                log.debug("Discovery probe failed", url=probe_url, error=str(e))
                continue

        log.debug("No AI-friendly files discovered", base_url=base_url)
        return None

    def _is_useful_content(self, content: str, url: str) -> bool:
        """Check if content is useful vs a placeholder/stub.

        Criteria:
        - Has more than 3 non-empty, non-comment lines
        - Has more than 100 characters of meaningful text
        - OR has at least 1 extractable link
        """
        if not content:
            return False

        content = content.strip()

        # Check for links first
        links = self.extract_markdown_links(content, url)
        if links:
            return True

        # Count meaningful lines (non-empty, non-comment)
        lines = [
            line.strip()
            for line in content.split("\n")
            if line.strip() and not line.strip().startswith("#")
        ]

        if len(lines) <= 3:
            return False

        total_chars = sum(len(line) for line in lines)
        return total_chars >= 100

    def _is_link_collection(self, content: str, links: list[str]) -> bool:
        """Check if file is primarily a collection of links.

        Files with >2% link density and >3 links are link collections.
        These should be followed but not indexed as content.
        """
        if len(links) <= 3:
            return False

        content_length = len(content.strip())
        if content_length == 0:
            return False

        link_density = (len(links) * 100) / content_length
        return link_density > 2.0

    def extract_markdown_links(self, content: str, base_url: str | None = None) -> list[str]:
        """Extract URLs from markdown content.

        Handles:
        - [text](url) - markdown links
        - <https://...> - autolinks
        - https://... - bare URLs
        - //example.com - protocol-relative
        - www.example.com - www URLs

        Args:
            content: Text content to extract links from
            base_url: Base URL to resolve relative links

        Returns:
            List of absolute URLs (deduplicated, order preserved)
        """
        if not content:
            return []

        # Combined pattern for all URL formats
        combined_pattern = re.compile(
            r"\[(?P<text>[^\]]*)\]\((?P<md>[^)]+)\)"  # markdown links
            r"|<\s*(?P<auto>https?://[^>\s]+)\s*>"  # autolinks
            r"|(?P<bare>https?://[^\s<>()\[\]\"]+)"  # bare URLs
            r"|(?P<proto>//[^\s<>()\[\]\"]+)"  # protocol-relative
            r"|(?P<www>www\.[^\s<>()\[\]\"]+)"  # www URLs
        )

        links: list[str] = []
        seen: set[str] = set()

        for match in re.finditer(combined_pattern, content):
            url = (
                match.group("md")
                or match.group("auto")
                or match.group("bare")
                or match.group("proto")
                or match.group("www")
            )
            if not url:
                continue

            # Clean URL
            url = url.strip().rstrip(".,;:)]>")

            # Skip anchors and mailto
            if not url or url.startswith("#") or url.startswith("mailto:"):
                continue

            # Normalize URL format
            if url.startswith("//"):
                url = f"https:{url}"
            elif url.startswith("www."):
                url = f"https://{url}"

            # Resolve relative URLs
            if base_url and not url.startswith(("http://", "https://")):
                try:
                    url = urljoin(base_url, url)
                except Exception:
                    continue

            # Only include HTTP/HTTPS
            if url.startswith(("http://", "https://")) and url not in seen:
                seen.add(url)
                links.append(url)

        return links


def is_llms_variant(url: str) -> bool:
    """Check if a URL is an llms.txt variant.

    Matches:
    - llms.txt, llms-full.txt
    - Files in /llms/ directories
    - .well-known/llms.txt, .well-known/ai.txt
    """
    parsed = urlparse(url)
    path = parsed.path.lower()
    filename = path.split("/")[-1] if "/" in path else path

    # Exact matches
    if filename in ("llms.txt", "llms-full.txt", "ai.txt"):
        return True

    # Files in /llms/ directory
    if "/llms/" in path and path.endswith(".txt"):
        return True

    # .well-known variants
    return bool("/.well-known/" in path and filename in ("llms.txt", "ai.txt"))
