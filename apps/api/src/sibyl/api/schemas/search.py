"""Unified search request/response models."""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class SearchRequest(BaseModel):
    """Unified search request - searches both knowledge graph AND documentation.

    By default, searches both stores and merges results by relevance.
    Use filters to narrow scope.
    """

    query: str = Field(..., min_length=1, description="Natural language search query")
    types: list[str] | None = Field(
        default=None,
        description="Filter by entity types. Options: pattern, rule, template, topic, "
        "episode, task, project, document. 'document' searches crawled docs.",
    )
    language: str | None = Field(default=None, description="Filter by programming language")
    category: str | None = Field(default=None, description="Filter by category")
    status: str | None = Field(default=None, description="Filter tasks by status")
    project: str | None = Field(default=None, description="Filter tasks by project ID")
    source: str | None = Field(default=None, description="Alias for source_name")
    source_id: str | None = Field(
        default=None, description="Filter documents/raw memory by source ID"
    )
    source_name: str | None = Field(default=None, description="Filter documents by source name")
    assignee: str | None = Field(default=None, description="Filter tasks by assignee name")
    since: str | None = Field(
        default=None, description="Filter by creation date (ISO: 2024-03-15 or relative: 7d, 2w)"
    )
    reference_time: str | None = Field(
        default=None,
        description="Query as-of timestamp for relative temporal ranking",
    )
    as_of: str | None = Field(
        default=None,
        description="Point-in-time validity filter for graph results",
    )
    limit: int = Field(default=10, ge=1, le=50, description="Maximum results")
    offset: int = Field(default=0, ge=0, description="Offset for pagination")
    include_content: bool = Field(default=True, description="Include content in results")
    content_max_chars: int = Field(
        default=500,
        ge=0,
        le=50_000,
        description="Maximum content characters returned per result",
    )
    include_documents: bool = Field(
        default=True, description="Include crawled documentation in search"
    )
    include_graph: bool = Field(default=True, description="Include knowledge graph entities")
    include_raw_memory: bool = Field(default=True, description="Include raw memories in search")
    memory_scope: Literal[
        "private",
        "delegated",
        "project",
        "team",
        "organization",
        "shared",
        "public",
    ] = Field(default="private", description="Raw memory scope to search")
    scope_key: str | None = Field(default=None, description="Raw memory scope key")
    participants: list[str] = Field(
        default_factory=list,
        description="Participant identifiers for imported raw memories",
    )
    labels: list[str] = Field(
        default_factory=list,
        description="Adapter labels/tags for imported raw memories",
    )
    thread_id: str | None = Field(default=None, description="Imported raw-memory thread ID")
    occurred_after: datetime | None = Field(
        default=None,
        description="Earliest source occurrence timestamp for raw memories",
    )
    occurred_before: datetime | None = Field(
        default=None,
        description="Latest source occurrence timestamp for raw memories",
    )
    use_enhanced: bool = Field(
        default=True, description="Use enhanced hybrid retrieval (vector + graph fusion)"
    )
    boost_recent: bool = Field(default=True, description="Boost recent results in ranking")
    include_retrieval_diagnostics: bool = Field(
        default=False,
        description="Include authorized candidate ranking diagnostics in response filters",
    )


class SearchResult(BaseModel):
    """Single search result - unified across graph entities and documents."""

    id: str = Field(..., description="Entity or chunk ID")
    type: str = Field(..., description="Entity type (pattern, rule, episode, etc.) or 'document'")
    name: str = Field(..., description="Entity name or document title")
    content: str = Field(..., description="Matched content")
    score: float = Field(..., description="Relevance score (0-1)")
    source: str | None = Field(default=None, description="Source file path or documentation source")
    url: str | None = Field(default=None, description="URL for documents")
    result_origin: Literal["graph", "document", "raw_memory"] = Field(
        default="graph", description="Whether result is from graph, documents, or raw memory"
    )
    metadata: dict[str, Any] = Field(default_factory=dict)


class SearchResponse(BaseModel):
    """Unified search results response."""

    results: list[SearchResult]
    total: int
    query: str
    filters: dict[str, Any]
    graph_count: int = Field(default=0, description="Number of results from knowledge graph")
    document_count: int = Field(default=0, description="Number of results from documents")
    raw_memory_count: int = Field(default=0, description="Number of results from raw memories")
    limit: int = Field(default=10, description="Results per page")
    offset: int = Field(default=0, description="Current offset")
    has_more: bool = Field(default=False, description="Whether more results exist")
