"""Source ingestion request/response models."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from .common import MemoryScopeLiteral


class SourceAdapterResponse(BaseModel):
    """Registered source adapter contract."""

    name: str
    version: str
    source_type: str
    display_name: str
    capabilities: list[str] = Field(default_factory=list)
    default_privacy_class: str
    transform_behavior: str
    metadata_schema: dict[str, Any] = Field(default_factory=dict)
    supports_incremental: bool = False


class SourceAdapterListResponse(BaseModel):
    """Registered source adapter list."""

    adapters: list[SourceAdapterResponse]


DocumentImportKindLiteral = Literal["file", "folder", "url", "text"]


class DocumentImportRequest(BaseModel):
    """Start a document import through the source-ingestion runtime."""

    kind: DocumentImportKindLiteral
    source_uri: str | None = Field(default=None, max_length=2000)
    text: str | None = None
    title: str | None = Field(default=None, max_length=1000)
    collection: str | None = Field(default=None, max_length=200)
    target_scope_key: str = Field(..., min_length=1, max_length=500)
    batch_size: int = Field(default=100, ge=1, le=1000)
    promotion_preview_approved: bool = False

    @model_validator(mode="after")
    def validate_source(self) -> DocumentImportRequest:
        if self.kind == "text":
            if not self.text or not self.text.strip():
                msg = "Text document imports require non-empty text"
                raise ValueError(msg)
            return self
        if not self.source_uri or not self.source_uri.strip():
            msg = f"{self.kind} document imports require source_uri"
            raise ValueError(msg)
        return self


class DocumentCollectionResponse(BaseModel):
    """Document import collection summary."""

    name: str
    document_count: int = 0
    updated_at: datetime | None = None


class DocumentCollectionListResponse(BaseModel):
    """Accessible document import collections."""

    collections: list[DocumentCollectionResponse]


SourceImportStatusLiteral = Literal[
    "pending",
    "running",
    "paused",
    "completed",
    "failed",
    "canceled",
]


class SourceImportStartRequest(BaseModel):
    """Start a bounded source import run."""

    source_uri: str = Field(..., min_length=1, max_length=2000)
    adapter_name: str = Field(default="mbox", min_length=1, max_length=80)
    target_memory_scope: MemoryScopeLiteral = Field(default="private")
    target_scope_key: str | None = Field(default=None, max_length=500)
    options: dict[str, Any] = Field(default_factory=dict)
    batch_size: int = Field(default=100, ge=1, le=1000)
    promotion_preview_approved: bool = False


class SourceImportResumeRequest(BaseModel):
    """Resume a bounded source import run from its persisted checkpoint."""

    batch_size: int | None = Field(default=None, ge=1, le=1000)
    promotion_preview_approved: bool | None = None


class SourceImportProgressResponse(BaseModel):
    """Source-safe import progress counters."""

    imported_count: int = 0
    skipped_count: int = 0
    dedupe_count: int = 0
    superseded_count: int = 0
    error_count: int = 0
    attachment_count: int = 0
    extraction_pending_count: int = 0
    raw_memory_count: int = 0


class SourceImportStatusResponse(BaseModel):
    """Source import status without raw source content."""

    import_id: str
    adapter_name: str
    adapter_version: str | None = None
    source_identity: str | None = None
    source_version: str | None = None
    status: SourceImportStatusLiteral
    privacy_class: str | None = None
    target_memory_scope: MemoryScopeLiteral | None = None
    target_scope_key: str | None = None
    checkpoint: dict[str, Any] | None = None
    progress: SourceImportProgressResponse
    raw_memory_ids: list[str] = Field(default_factory=list)
    dedupe_keys: list[str] = Field(default_factory=list)
    duplicate_dedupe_keys: list[str] = Field(default_factory=list)
    skipped_records: list[dict[str, Any]] = Field(default_factory=list)
    errors: list[dict[str, Any]] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None
