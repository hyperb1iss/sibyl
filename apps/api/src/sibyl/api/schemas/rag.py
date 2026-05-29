"""RAG search, code-example, and document detail request/response models."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from .crawler import CrawlDocumentResponse


class RAGSearchRequest(BaseModel):
    """RAG search request for document chunks."""

    query: str = Field(..., min_length=1, description="Natural language search query")
    source_id: str | None = Field(default=None, description="Filter by source ID")
    source_name: str | None = Field(
        default=None, description="Filter by source name (partial match)"
    )
    match_count: int = Field(default=10, ge=1, le=100, description="Number of results")
    similarity_threshold: float = Field(
        default=0.5, ge=0.0, le=1.0, description="Minimum similarity score"
    )
    return_mode: Literal["chunks", "pages"] = Field(
        default="chunks", description="Return chunks or full pages"
    )
    include_context: bool = Field(default=True, description="Include contextual prefix in results")


class RAGChunkResult(BaseModel):
    """Single chunk result from RAG search."""

    chunk_id: str
    document_id: str
    source_id: str
    source_name: str
    url: str
    title: str
    content: str
    context: str | None = None
    similarity: float
    chunk_type: str
    chunk_index: int
    heading_path: list[str] = Field(default_factory=list)
    language: str | None = None


class RAGPageResult(BaseModel):
    """Full page result from RAG search."""

    document_id: str
    source_id: str
    source_name: str
    url: str
    title: str
    content: str
    word_count: int
    has_code: bool
    headings: list[str] = Field(default_factory=list)
    code_languages: list[str] = Field(default_factory=list)
    best_chunk_similarity: float


class RAGSearchResponse(BaseModel):
    """RAG search response."""

    results: list[RAGChunkResult | RAGPageResult]
    total: int
    query: str
    source_filter: str | None = None
    return_mode: str


class CodeExampleRequest(BaseModel):
    """Search for code examples."""

    query: str = Field(..., min_length=1, description="Search query for code")
    language: str | None = Field(default=None, description="Filter by programming language")
    source_id: str | None = Field(default=None, description="Filter by source")
    match_count: int = Field(default=10, ge=1, le=50, description="Number of results")


class CodeExampleResult(BaseModel):
    """Code example result."""

    chunk_id: str
    document_id: str
    source_id: str
    source_name: str
    url: str
    title: str
    code: str
    context: str | None = None
    language: str | None = None
    similarity: float
    heading_path: list[str] = Field(default_factory=list)


class CodeExampleResponse(BaseModel):
    """Code example search response."""

    examples: list[CodeExampleResult]
    total: int
    query: str
    language_filter: str | None = None


class FullPageResponse(BaseModel):
    """Full page content response."""

    document_id: str
    source_id: str
    source_name: str
    url: str
    title: str
    content: str
    raw_content: str | None = None
    word_count: int
    token_count: int
    has_code: bool
    headings: list[str] = Field(default_factory=list)
    code_languages: list[str] = Field(default_factory=list)
    links: list[str] = Field(default_factory=list)
    crawled_at: datetime


class SourcePagesResponse(BaseModel):
    """List of pages for a source."""

    source_id: str
    source_name: str
    pages: list[CrawlDocumentResponse]
    total: int
    has_more: bool


class DocumentUpdateRequest(BaseModel):
    """Update a crawled document's content."""

    title: str | None = Field(default=None, max_length=512, description="New document title")
    content: str | None = Field(default=None, max_length=500000, description="New document content")


class DocumentRelatedEntity(BaseModel):
    """An entity related to a document through extraction."""

    id: str
    name: str
    entity_type: str
    description: str = ""
    chunk_count: int = Field(default=1, description="Number of chunks mentioning this entity")


class DocumentRelatedEntitiesResponse(BaseModel):
    """Related entities for a document."""

    document_id: str
    entities: list[DocumentRelatedEntity]
    total: int
