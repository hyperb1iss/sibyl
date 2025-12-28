"""Auto-tagging for crawled documents and sources.

Extracts tags from document content using heuristics:
- Code languages (from code blocks)
- Technology mentions (frameworks, libraries)
- Topic keywords (from headings and content)
- Content categories (api, tutorial, reference, etc.)
"""

from __future__ import annotations

import re
from collections import Counter
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sibyl.db import CrawledDocument

# =============================================================================
# Technology Detection
# =============================================================================

# Map of technology names to their canonical tag
TECHNOLOGY_PATTERNS: dict[str, list[str]] = {
    "react": ["react", "reactjs", "react.js", "jsx"],
    "nextjs": ["next.js", "nextjs", "next js"],
    "vue": ["vue", "vuejs", "vue.js"],
    "angular": ["angular", "angularjs"],
    "svelte": ["svelte", "sveltekit"],
    "typescript": ["typescript", "ts"],
    "javascript": ["javascript", "js", "ecmascript"],
    "python": ["python", "py"],
    "rust": ["rust", "rustlang"],
    "go": ["golang", "go lang"],
    "kubernetes": ["kubernetes", "k8s"],
    "docker": ["docker", "dockerfile", "container"],
    "aws": ["aws", "amazon web services", "lambda", "s3", "ec2"],
    "gcp": ["gcp", "google cloud", "cloud run"],
    "azure": ["azure", "microsoft azure"],
    "graphql": ["graphql", "gql"],
    "rest": ["rest api", "restful"],
    "postgresql": ["postgresql", "postgres", "psql"],
    "mongodb": ["mongodb", "mongo"],
    "redis": ["redis"],
    "tailwind": ["tailwind", "tailwindcss"],
    "prisma": ["prisma"],
    "fastapi": ["fastapi"],
    "django": ["django"],
    "flask": ["flask"],
    "express": ["express", "expressjs"],
    "node": ["node.js", "nodejs"],
    "deno": ["deno"],
    "bun": ["bun"],
    "vite": ["vite", "vitejs"],
    "webpack": ["webpack"],
    "eslint": ["eslint"],
    "prettier": ["prettier"],
    "git": ["git", "github", "gitlab"],
    "ci-cd": ["ci/cd", "github actions", "gitlab ci", "jenkins"],
    "testing": ["jest", "pytest", "vitest", "playwright", "cypress"],
    "auth": ["oauth", "jwt", "authentication", "authorization"],
    "api": ["api", "endpoint", "route"],
}

# =============================================================================
# Category Detection
# =============================================================================

CATEGORY_PATTERNS: dict[str, list[str]] = {
    "tutorial": ["tutorial", "guide", "how to", "getting started", "introduction", "learn"],
    "reference": ["reference", "api reference", "documentation", "api docs"],
    "examples": ["example", "examples", "sample", "demo"],
    "configuration": ["config", "configuration", "setup", "settings", "environment"],
    "deployment": ["deploy", "deployment", "hosting", "production"],
    "troubleshooting": ["error", "debug", "troubleshoot", "fix", "issue", "problem"],
    "migration": ["migration", "upgrade", "migrate"],
    "security": ["security", "secure", "vulnerability", "auth"],
    "performance": ["performance", "optimization", "speed", "cache"],
    "testing": ["test", "testing", "unit test", "integration test"],
}

# =============================================================================
# Tag Extraction Functions
# =============================================================================


def extract_tags_from_document(doc: CrawledDocument) -> list[str]:
    """Extract tags from a single document.

    Analyzes:
    - Code languages detected in code blocks
    - Technology mentions in content
    - Heading keywords

    Returns:
        List of unique tags (lowercase, deduplicated)
    """
    tags: set[str] = set()

    # 1. Add detected code languages
    if doc.code_languages:
        for lang in doc.code_languages:
            normalized = lang.lower().strip()
            if normalized:
                tags.add(normalized)

    # 2. Detect technologies from content
    content_lower = (doc.content or "").lower()
    title_lower = (doc.title or "").lower()
    combined_text = f"{title_lower} {content_lower}"

    for tag, patterns in TECHNOLOGY_PATTERNS.items():
        for pattern in patterns:
            # Word boundary match to avoid partial matches
            if re.search(rf"\b{re.escape(pattern)}\b", combined_text):
                tags.add(tag)
                break

    # 3. Extract from headings
    if doc.headings:
        headings_text = " ".join(doc.headings).lower()
        for tag, patterns in TECHNOLOGY_PATTERNS.items():
            for pattern in patterns:
                if re.search(rf"\b{re.escape(pattern)}\b", headings_text):
                    tags.add(tag)
                    break

    return sorted(tags)


def extract_categories_from_document(doc: CrawledDocument) -> list[str]:
    """Extract content categories from a document.

    Analyzes URL path, title, and headings to determine content type.

    Returns:
        List of category labels
    """
    categories: set[str] = set()

    # Combine searchable text
    url_path = (doc.url or "").lower()
    title = (doc.title or "").lower()
    headings_text = " ".join(doc.headings or []).lower()
    combined = f"{url_path} {title} {headings_text}"

    for category, patterns in CATEGORY_PATTERNS.items():
        for pattern in patterns:
            if pattern in combined:
                categories.add(category)
                break

    return sorted(categories)


def aggregate_source_tags(documents: list[CrawledDocument]) -> tuple[list[str], list[str]]:
    """Aggregate tags and categories from all documents in a source.

    Uses frequency counting to prioritize common tags.

    Args:
        documents: List of crawled documents from a source

    Returns:
        Tuple of (top_tags, top_categories)
    """
    tag_counter: Counter[str] = Counter()
    category_counter: Counter[str] = Counter()

    for doc in documents:
        doc_tags = extract_tags_from_document(doc)
        doc_categories = extract_categories_from_document(doc)

        tag_counter.update(doc_tags)
        category_counter.update(doc_categories)

    # Return top tags (limit to most common)
    top_tags = [tag for tag, _ in tag_counter.most_common(15)]
    top_categories = [cat for cat, _ in category_counter.most_common(5)]

    return top_tags, top_categories
