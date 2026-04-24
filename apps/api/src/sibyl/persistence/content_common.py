"""Shared content runtime DTOs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class CrawlStats:
    total_sources: int
    total_documents: int
    total_chunks: int
    chunks_with_embeddings: int
    sources_by_status: dict[str, int]


@dataclass(frozen=True)
class DocumentEntityRecord:
    """Resolved document-backed entity payload for entity routes."""

    chunk: Any
    document: Any
    source: Any
    content: str


LegacyCrawlStats = CrawlStats
LegacyDocumentEntityRecord = DocumentEntityRecord
