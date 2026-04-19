"""Active content runtime adapters for the current relational backend."""

from sibyl.persistence.legacy.crawler import (
    count_remaining_unlinked_chunks,
    get_crawl_stats_payload,
    get_crawled_document_for_org,
    get_org_crawl_source,
    get_source_sync_counts,
    list_crawled_documents_for_org,
    list_document_chunks,
    list_source_chunks,
    list_source_documents,
    list_source_documents_page,
    list_sources_for_graph_linking,
    list_unlinked_source_chunks,
)
from sibyl.persistence.legacy.entities import (
    get_legacy_raw_capture,
    list_legacy_raw_captures,
    resolve_legacy_document_entity,
)
from sibyl.persistence.legacy.rag import (
    get_document_by_url_for_org,
    hybrid_search_chunks,
    list_source_documents_page as list_rag_source_documents_page,
    search_code_example_chunks,
    search_rag_chunks,
)

__all__ = [
    "count_remaining_unlinked_chunks",
    "get_crawl_stats_payload",
    "get_crawled_document_for_org",
    "get_document_by_url_for_org",
    "get_legacy_raw_capture",
    "get_org_crawl_source",
    "get_source_sync_counts",
    "hybrid_search_chunks",
    "list_crawled_documents_for_org",
    "list_document_chunks",
    "list_legacy_raw_captures",
    "list_rag_source_documents_page",
    "list_source_chunks",
    "list_source_documents",
    "list_source_documents_page",
    "list_sources_for_graph_linking",
    "list_unlinked_source_chunks",
    "resolve_legacy_document_entity",
    "search_code_example_chunks",
    "search_rag_chunks",
]
