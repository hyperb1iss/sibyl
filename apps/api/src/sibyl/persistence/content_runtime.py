"""Active content adapters for the current persistence runtime."""

from __future__ import annotations

from collections.abc import Awaitable
from contextlib import asynccontextmanager
from importlib import import_module
from typing import TYPE_CHECKING, Protocol, cast


class RuntimeExport(Protocol):
    def __call__(self, *args: object, **kwargs: object) -> Awaitable[object]: ...


if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Sequence
    from uuid import UUID

    from sibyl.persistence.content_common import (
        ApiIdempotencyRecord,
        CodeExampleSearchRow,
        ContentSession,
        CrawledDocumentRecord,
        CrawlSourceRecord,
        CrawlStats,
        DocumentChunkRecord,
        DocumentEntityRecord,
        HybridSearchRow,
        RAGSearchRow,
        RawCaptureRecord,
    )
    from sibyl_core.models import CrawlStatus, SourceType
    from sibyl_core.services.link_graph_status import LinkGraphStatusData

    class CheckRelationalBackendHealth(Protocol):
        def __call__(self) -> Awaitable[dict[str, str | None]]: ...

    class GetCrawlSourceById(Protocol):
        def __call__(
            self, session: ContentSession, *, source_id: UUID
        ) -> Awaitable[CrawlSourceRecord | None]: ...

    class GetCrawlSourceByUrl(Protocol):
        def __call__(
            self, session: ContentSession, *, url: str
        ) -> Awaitable[CrawlSourceRecord | None]: ...

    class GetOrgCrawlSource(Protocol):
        def __call__(
            self,
            session: ContentSession,
            *,
            source_id: UUID,
            organization_id: UUID,
        ) -> Awaitable[CrawlSourceRecord | None]: ...

    class ListCrawlSourcesForOrg(Protocol):
        def __call__(
            self,
            session: ContentSession,
            *,
            organization_id: UUID,
            status: CrawlStatus | None,
            limit: int,
        ) -> Awaitable[tuple[list[CrawlSourceRecord], int]]: ...

    class ListCrawlSources(Protocol):
        def __call__(
            self,
            session: ContentSession,
            *,
            status: CrawlStatus | None = None,
            limit: int | None = 50,
        ) -> Awaitable[list[CrawlSourceRecord]]: ...

    class CreateCrawlSourceRecord(Protocol):
        def __call__(
            self,
            session: ContentSession,
            *,
            name: str,
            url: str,
            organization_id: UUID,
            source_type: SourceType,
            description: str | None,
            crawl_depth: int,
            include_patterns: list[str] | None,
            exclude_patterns: list[str] | None,
        ) -> Awaitable[CrawlSourceRecord]: ...

    class SaveCrawlSourceRecord(Protocol):
        def __call__(
            self, session: ContentSession, *, source: CrawlSourceRecord
        ) -> Awaitable[CrawlSourceRecord]: ...

    class GetCrawlStatsPayload(Protocol):
        def __call__(
            self, session: ContentSession, *, organization_id: UUID
        ) -> Awaitable[CrawlStats]: ...

    class GetLinkGraphStatusPayload(Protocol):
        def __call__(
            self, session: ContentSession, *, organization_id: UUID
        ) -> Awaitable[LinkGraphStatusData]: ...

    class ListCrawledDocumentsForOrg(Protocol):
        def __call__(
            self,
            session: ContentSession,
            *,
            organization_id: UUID,
            limit: int,
            offset: int,
        ) -> Awaitable[tuple[list[CrawledDocumentRecord], int]]: ...

    class GetCrawledDocumentForOrg(Protocol):
        def __call__(
            self,
            session: ContentSession,
            *,
            document_id: UUID,
            organization_id: UUID,
        ) -> Awaitable[CrawledDocumentRecord | None]: ...

    class SaveCrawledDocumentRecord(Protocol):
        def __call__(
            self, session: ContentSession, *, document: object
        ) -> Awaitable[CrawledDocumentRecord]: ...

    class SaveDocumentChunks(Protocol):
        def __call__(
            self, session: ContentSession, *, chunks: Sequence[object]
        ) -> Awaitable[list[DocumentChunkRecord]]: ...

    class DeleteCrawledDocumentRecord(Protocol):
        def __call__(
            self,
            session: ContentSession,
            *,
            document_id: UUID,
            organization_id: UUID,
        ) -> Awaitable[tuple[CrawledDocumentRecord, int] | None]: ...

    class ListDocumentChunks(Protocol):
        def __call__(
            self, session: ContentSession, *, document_id: UUID
        ) -> Awaitable[list[DocumentChunkRecord]]: ...

    class ListSourceDocumentsPage(Protocol):
        def __call__(
            self,
            session: ContentSession,
            *,
            source_id: UUID,
            limit: int,
            offset: int,
            has_code: bool | None = None,
            is_index: bool | None = None,
        ) -> Awaitable[tuple[list[CrawledDocumentRecord], int]]: ...

    class ListSourceChunks(Protocol):
        def __call__(
            self, session: ContentSession, *, source_id: UUID
        ) -> Awaitable[list[DocumentChunkRecord]]: ...

    class ListSourceDocuments(Protocol):
        def __call__(
            self, session: ContentSession, *, source_id: UUID
        ) -> Awaitable[list[CrawledDocumentRecord]]: ...

    class DeleteCrawlSourceRecord(Protocol):
        def __call__(
            self, session: ContentSession, *, source_id: UUID, organization_id: UUID
        ) -> Awaitable[CrawlSourceRecord | None]: ...

    class GetSourceSyncCounts(Protocol):
        def __call__(
            self, session: ContentSession, *, source_id: UUID
        ) -> Awaitable[tuple[int, int]]: ...

    class ListSourcesForGraphLinking(Protocol):
        def __call__(
            self,
            session: ContentSession,
            *,
            organization_id: UUID,
            source_id: UUID | None,
        ) -> Awaitable[list[CrawlSourceRecord]]: ...

    class ListUnlinkedSourceChunks(Protocol):
        def __call__(
            self, session: ContentSession, *, source_id: UUID, limit: int
        ) -> Awaitable[list[DocumentChunkRecord]]: ...

    class CountRemainingUnlinkedChunks(Protocol):
        def __call__(
            self,
            session: ContentSession,
            *,
            organization_id: UUID,
            source_id: UUID | None,
        ) -> Awaitable[int]: ...

    class ListRawCaptures(Protocol):
        def __call__(
            self,
            session: ContentSession,
            *,
            organization_id: UUID,
            entity_type: str | None,
            capture_surface: str | None,
            review_state: str | None,
            limit: int,
            offset: int,
        ) -> Awaitable[tuple[list[RawCaptureRecord], bool]]: ...

    class GetRawCapture(Protocol):
        def __call__(
            self, session: ContentSession, *, organization_id: UUID, capture_id: UUID
        ) -> Awaitable[RawCaptureRecord | None]: ...

    class SaveRawCaptureRecord(Protocol):
        def __call__(
            self, session: ContentSession, *, capture: RawCaptureRecord
        ) -> Awaitable[RawCaptureRecord]: ...

    class UpdateRawCaptureReviewState(Protocol):
        def __call__(
            self,
            session: ContentSession,
            *,
            organization_id: UUID,
            capture_id: UUID,
            review_state: str,
        ) -> Awaitable[RawCaptureRecord | None]: ...

    class GetApiIdempotencyRecord(Protocol):
        def __call__(
            self,
            session: ContentSession,
            *,
            organization_id: UUID,
            principal_id: str,
            idempotency_key: str,
            method: str,
            path: str,
        ) -> Awaitable[ApiIdempotencyRecord | None]: ...

    class SaveApiIdempotencyRecord(Protocol):
        def __call__(
            self, session: ContentSession, *, record: ApiIdempotencyRecord
        ) -> Awaitable[ApiIdempotencyRecord]: ...

    class ResolveDocumentEntity(Protocol):
        def __call__(
            self, session: ContentSession, *, organization_id: UUID, entity_id: str
        ) -> Awaitable[DocumentEntityRecord | None]: ...

    class GetDocumentByUrlForOrg(Protocol):
        def __call__(
            self,
            session: ContentSession,
            *,
            url: str,
            organization_id: UUID | str,
        ) -> Awaitable[CrawledDocumentRecord | None]: ...

    class SearchRAGChunks(Protocol):
        def __call__(
            self,
            session: ContentSession,
            *,
            query_embedding: list[float],
            organization_id: UUID | str,
            similarity_threshold: float,
            match_count: int,
            source_id: UUID | None = None,
            source_name: str | None = None,
        ) -> Awaitable[list[RAGSearchRow]]: ...

    class SearchCodeExampleChunks(Protocol):
        def __call__(
            self,
            session: ContentSession,
            *,
            query_embedding: list[float],
            organization_id: UUID | str,
            match_count: int,
            source_id: UUID | None = None,
            language: str | None = None,
        ) -> Awaitable[list[CodeExampleSearchRow]]: ...

    class HybridSearchChunks(Protocol):
        def __call__(
            self,
            session: ContentSession,
            *,
            query_text: str,
            query_embedding: list[float],
            organization_id: UUID | str,
            similarity_threshold: float,
            match_count: int,
            source_id: UUID | None = None,
            source_name: str | None = None,
        ) -> Awaitable[list[HybridSearchRow]]: ...

    check_relational_backend_health: CheckRelationalBackendHealth
    count_remaining_unlinked_chunks: CountRemainingUnlinkedChunks
    create_crawl_source_record: CreateCrawlSourceRecord
    delete_crawl_source_record: DeleteCrawlSourceRecord
    delete_crawled_document_record: DeleteCrawledDocumentRecord
    get_crawl_source_by_id: GetCrawlSourceById
    get_crawl_source_by_url: GetCrawlSourceByUrl
    get_crawl_stats_payload: GetCrawlStatsPayload
    get_crawled_document_for_org: GetCrawledDocumentForOrg
    get_document_by_url_for_org: GetDocumentByUrlForOrg
    get_link_graph_status_payload: GetLinkGraphStatusPayload
    get_org_crawl_source: GetOrgCrawlSource
    get_raw_capture: GetRawCapture
    get_api_idempotency_record: GetApiIdempotencyRecord
    get_source_sync_counts: GetSourceSyncCounts
    hybrid_search_chunks: HybridSearchChunks
    list_crawl_sources: ListCrawlSources
    list_crawl_sources_for_org: ListCrawlSourcesForOrg
    list_crawled_documents_for_org: ListCrawledDocumentsForOrg
    list_document_chunks: ListDocumentChunks
    list_rag_source_documents_page: ListSourceDocumentsPage
    list_raw_captures: ListRawCaptures
    list_source_chunks: ListSourceChunks
    list_source_documents: ListSourceDocuments
    list_source_documents_page: ListSourceDocumentsPage
    list_sources_for_graph_linking: ListSourcesForGraphLinking
    list_unlinked_source_chunks: ListUnlinkedSourceChunks
    resolve_document_entity: ResolveDocumentEntity
    save_crawl_source_record: SaveCrawlSourceRecord
    save_crawled_document_record: SaveCrawledDocumentRecord
    save_document_chunks: SaveDocumentChunks
    save_raw_capture_record: SaveRawCaptureRecord
    save_api_idempotency_record: SaveApiIdempotencyRecord
    search_code_example_chunks: SearchCodeExampleChunks
    search_rag_chunks: SearchRAGChunks
    update_raw_capture_review_state: UpdateRawCaptureReviewState

_BACKEND_MODULE = "sibyl.persistence.surreal.content"

_BACKEND_EXPORTS = [
    "create_crawl_source_record",
    "count_remaining_unlinked_chunks",
    "check_relational_backend_health",
    "delete_crawl_source_record",
    "delete_crawled_document_record",
    "get_crawl_source_by_id",
    "get_crawl_source_by_url",
    "get_crawl_stats_payload",
    "get_crawled_document_for_org",
    "get_document_by_url_for_org",
    "get_link_graph_status_payload",
    "get_raw_capture",
    "get_api_idempotency_record",
    "get_org_crawl_source",
    "get_source_sync_counts",
    "hybrid_search_chunks",
    "list_crawl_sources_for_org",
    "list_crawl_sources",
    "list_crawled_documents_for_org",
    "list_document_chunks",
    "list_raw_captures",
    "list_rag_source_documents_page",
    "list_source_chunks",
    "list_source_documents",
    "list_source_documents_page",
    "list_sources_for_graph_linking",
    "list_unlinked_source_chunks",
    "resolve_document_entity",
    "save_crawl_source_record",
    "save_crawled_document_record",
    "save_document_chunks",
    "save_raw_capture_record",
    "save_api_idempotency_record",
    "search_code_example_chunks",
    "search_rag_chunks",
    "update_raw_capture_review_state",
]

__all__ = [
    "get_content_read_session",
    "get_content_read_session_dependency",
    "create_crawl_source_record",
    "count_remaining_unlinked_chunks",
    "check_relational_backend_health",
    "delete_crawl_source_record",
    "delete_crawled_document_record",
    "get_crawl_source_by_id",
    "get_crawl_source_by_url",
    "get_crawl_stats_payload",
    "get_crawled_document_for_org",
    "get_document_by_url_for_org",
    "get_link_graph_status_payload",
    "get_raw_capture",
    "get_api_idempotency_record",
    "get_org_crawl_source",
    "get_source_sync_counts",
    "hybrid_search_chunks",
    "list_crawl_sources_for_org",
    "list_crawl_sources",
    "list_crawled_documents_for_org",
    "list_document_chunks",
    "list_raw_captures",
    "list_rag_source_documents_page",
    "list_source_chunks",
    "list_source_documents",
    "list_source_documents_page",
    "list_sources_for_graph_linking",
    "list_unlinked_source_chunks",
    "resolve_document_entity",
    "save_crawl_source_record",
    "save_crawled_document_record",
    "save_document_chunks",
    "save_raw_capture_record",
    "save_api_idempotency_record",
    "search_code_example_chunks",
    "search_rag_chunks",
    "update_raw_capture_review_state",
]


def _resolve_backend_export(name: str) -> RuntimeExport:
    module = import_module(_BACKEND_MODULE)
    if hasattr(module, name):
        return cast("RuntimeExport", getattr(module, name))
    msg = f"{name} is not implemented for the Surreal content runtime"
    raise AttributeError(msg)


@asynccontextmanager
async def get_content_read_session() -> AsyncGenerator[object | None]:
    """Yield a relational session only when the active content runtime needs one."""
    yield None


async def get_content_read_session_dependency() -> AsyncGenerator[object | None]:
    """FastAPI dependency wrapper for content reads across runtimes."""
    async with get_content_read_session() as session:
        yield session


def _make_runtime_proxy(name: str) -> RuntimeExport:
    async def _proxy(*args: object, **kwargs: object) -> object:
        export = _resolve_backend_export(name)
        return await export(*args, **kwargs)

    _proxy.__name__ = name
    return cast("RuntimeExport", _proxy)


for _export_name in _BACKEND_EXPORTS:
    if _export_name not in globals():
        globals()[_export_name] = _make_runtime_proxy(_export_name)

del _export_name
