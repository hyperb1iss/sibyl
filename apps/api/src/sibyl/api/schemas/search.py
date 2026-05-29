"""Unified search request/response models."""

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
    source_id: str | None = Field(default=None, description="Filter documents by source ID")
    source_name: str | None = Field(default=None, description="Filter documents by source name")
    assignee: str | None = Field(default=None, description="Filter tasks by assignee name")
    since: str | None = Field(
        default=None, description="Filter by creation date (ISO: 2024-03-15 or relative: 7d, 2w)"
    )
    reference_time: str | None = Field(
        default=None,
        description="Query as-of timestamp for relative temporal ranking",
    )
    limit: int = Field(default=10, ge=1, le=50, description="Maximum results")
    offset: int = Field(default=0, ge=0, description="Offset for pagination")
    include_content: bool = Field(default=True, description="Include full content in results")
    include_documents: bool = Field(
        default=True, description="Include crawled documentation in search"
    )
    include_graph: bool = Field(default=True, description="Include knowledge graph entities")
    use_enhanced: bool = Field(
        default=True, description="Use enhanced hybrid retrieval (vector + graph fusion)"
    )
    boost_recent: bool = Field(default=True, description="Boost recent results in ranking")


class SearchResult(BaseModel):
    """Single search result - unified across graph entities and documents."""

    id: str = Field(..., description="Entity or chunk ID")
    type: str = Field(..., description="Entity type (pattern, rule, episode, etc.) or 'document'")
    name: str = Field(..., description="Entity name or document title")
    content: str = Field(..., description="Matched content")
    score: float = Field(..., description="Relevance score (0-1)")
    source: str | None = Field(default=None, description="Source file path or documentation source")
    url: str | None = Field(default=None, description="URL for documents")
    result_origin: Literal["graph", "document"] = Field(
        default="graph", description="Whether result is from knowledge graph or documents"
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
    limit: int = Field(default=10, description="Results per page")
    offset: int = Field(default=0, description="Current offset")
    has_more: bool = Field(default=False, description="Whether more results exist")
