"""Active content adapters, re-exported from the SurrealDB backend."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from sibyl.persistence.surreal.content import (
    check_relational_backend_health,
    count_remaining_unlinked_chunks,
    create_crawl_source_record,
    delete_crawl_source_record,
    delete_crawled_document_record,
    delete_document_chunks_for_document,
    get_api_idempotency_record,
    get_crawl_source_by_id,
    get_crawl_source_by_url,
    get_crawl_stats_payload,
    get_crawled_document_for_org,
    get_document_by_url_for_org,
    get_link_graph_status_payload,
    get_org_crawl_source,
    get_raw_capture,
    get_source_sync_counts,
    hybrid_search_chunks,
    list_crawl_sources,
    list_crawl_sources_for_org,
    list_crawled_documents_for_org,
    list_document_chunks,
    list_rag_source_documents_page,
    list_raw_captures,
    list_source_chunks,
    list_source_documents,
    list_source_documents_page,
    list_sources_for_graph_linking,
    list_unlinked_source_chunks,
    purge_due_deleted_raw_captures,
    resolve_document_entity,
    save_api_idempotency_record,
    save_crawl_source_record,
    save_crawled_document_record,
    save_document_chunks,
    save_raw_capture_record,
    search_code_example_chunks,
    search_rag_chunks,
    soft_delete_private_raw_captures_for_user,
    update_raw_capture_review_state,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

__all__ = [
    "check_relational_backend_health",
    "count_remaining_unlinked_chunks",
    "create_crawl_source_record",
    "delete_crawl_source_record",
    "delete_crawled_document_record",
    "delete_document_chunks_for_document",
    "get_api_idempotency_record",
    "get_content_read_session",
    "get_content_read_session_dependency",
    "get_crawl_source_by_id",
    "get_crawl_source_by_url",
    "get_crawl_stats_payload",
    "get_crawled_document_for_org",
    "get_document_by_url_for_org",
    "get_link_graph_status_payload",
    "get_org_crawl_source",
    "get_raw_capture",
    "get_source_sync_counts",
    "hybrid_search_chunks",
    "list_crawl_sources",
    "list_crawl_sources_for_org",
    "list_crawled_documents_for_org",
    "list_document_chunks",
    "list_rag_source_documents_page",
    "list_raw_captures",
    "list_source_chunks",
    "list_source_documents",
    "list_source_documents_page",
    "list_sources_for_graph_linking",
    "list_unlinked_source_chunks",
    "purge_due_deleted_raw_captures",
    "resolve_document_entity",
    "save_api_idempotency_record",
    "save_crawl_source_record",
    "save_crawled_document_record",
    "save_document_chunks",
    "save_raw_capture_record",
    "search_code_example_chunks",
    "search_rag_chunks",
    "soft_delete_private_raw_captures_for_user",
    "update_raw_capture_review_state",
]


@asynccontextmanager
async def get_content_read_session() -> AsyncGenerator[object | None]:
    """Yield a relational session only when the active content runtime needs one."""
    yield None


async def get_content_read_session_dependency() -> AsyncGenerator[object | None]:
    """FastAPI dependency wrapper for content reads across runtimes."""
    async with get_content_read_session() as session:
        yield session
