"""Source import, document import, and crawler client operations."""

from typing import Any
from urllib.parse import quote


class ClientSourcesMixin:
    """Source import, document import, and crawler client operations."""

    async def source_import_status(self, import_id: str) -> dict[str, Any]:
        """Inspect a source import receipt from the memory surface."""
        encoded_import_id = quote(import_id, safe="")
        return await self._request("GET", f"/memory/source-imports/{encoded_import_id}")

    async def start_source_import(
        self,
        *,
        source_uri: str,
        adapter_name: str,
        target_memory_scope: str = "private",
        target_scope_key: str | None = None,
        options: dict[str, Any] | None = None,
        batch_size: int = 100,
        promotion_preview_approved: bool = False,
    ) -> dict[str, Any]:
        """Create a source import run through the ingestion surface."""
        data: dict[str, Any] = {
            "source_uri": source_uri,
            "adapter_name": adapter_name,
            "target_memory_scope": target_memory_scope,
            "target_scope_key": target_scope_key,
            "options": options or {},
            "batch_size": batch_size,
            "promotion_preview_approved": promotion_preview_approved,
        }
        return await self._request("POST", "/ingestion/imports", json=data)

    async def start_document_import(
        self,
        *,
        kind: str,
        source_uri: str | None = None,
        text: str | None = None,
        title: str | None = None,
        collection: str | None = None,
        target_scope_key: str,
        batch_size: int = 100,
        promotion_preview_approved: bool = False,
        allow_private_network: bool = False,
    ) -> dict[str, Any]:
        """Create a document import run through the ingestion surface."""
        data: dict[str, Any] = {
            "kind": kind,
            "source_uri": source_uri,
            "text": text,
            "title": title,
            "collection": collection,
            "target_scope_key": target_scope_key,
            "batch_size": batch_size,
            "promotion_preview_approved": promotion_preview_approved,
            "allow_private_network": allow_private_network,
        }
        return await self._request("POST", "/ingestion/documents", json=data)

    async def list_document_collections(self) -> dict[str, Any]:
        """List accessible document import collections."""
        return await self._request("GET", "/ingestion/collections")

    async def ingestion_source_import_status(self, import_id: str) -> dict[str, Any]:
        """Inspect a source import receipt from the ingestion surface."""
        encoded_import_id = quote(import_id, safe="")
        return await self._request("GET", f"/ingestion/imports/{encoded_import_id}")

    async def resume_source_import(
        self,
        import_id: str,
        *,
        batch_size: int | None = None,
        promotion_preview_approved: bool | None = None,
    ) -> dict[str, Any]:
        """Resume a source import drain through the ingestion surface."""
        encoded_import_id = quote(import_id, safe="")
        data: dict[str, Any] = {
            "batch_size": batch_size,
            "promotion_preview_approved": promotion_preview_approved,
        }
        return await self._request(
            "POST",
            f"/ingestion/imports/{encoded_import_id}/resume",
            json=data,
        )

    async def cancel_source_import(self, import_id: str) -> dict[str, Any]:
        """Cancel a source import drain through the ingestion surface."""
        encoded_import_id = quote(import_id, safe="")
        return await self._request("POST", f"/ingestion/imports/{encoded_import_id}/cancel")

    async def create_crawl_source(
        self,
        name: str,
        url: str,
        source_type: str = "website",
        description: str | None = None,
        crawl_depth: int = 2,
        include_patterns: list[str] | None = None,
        exclude_patterns: list[str] | None = None,
    ) -> dict[str, Any]:
        """Create a new crawl source."""
        data: dict[str, Any] = {
            "name": name,
            "url": url,
            "source_type": source_type,
            "crawl_depth": crawl_depth,
        }
        if description:
            data["description"] = description
        if include_patterns:
            data["include_patterns"] = include_patterns
        if exclude_patterns:
            data["exclude_patterns"] = exclude_patterns

        return await self._request("POST", "/sources", json=data)

    async def list_crawl_sources(
        self,
        status: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """List crawl sources."""
        params: dict[str, Any] = {"limit": limit}
        if status:
            params["status"] = status
        return await self._request("GET", "/sources", params=params)

    async def get_crawl_source(self, source_id: str) -> dict[str, Any]:
        """Get a crawl source by ID."""
        return await self._request("GET", f"/sources/{source_id}")

    async def delete_crawl_source(self, source_id: str) -> dict[str, Any]:
        """Delete a crawl source."""
        return await self._request("DELETE", f"/sources/{source_id}")

    async def start_crawl(
        self,
        source_id: str,
        max_pages: int = 50,
        max_depth: int = 3,
        generate_embeddings: bool = True,
    ) -> dict[str, Any]:
        """Start crawling a source."""
        data = {
            "max_pages": max_pages,
            "max_depth": max_depth,
            "generate_embeddings": generate_embeddings,
        }
        return await self._request("POST", f"/sources/{source_id}/ingest", json=data)

    async def get_crawl_status(self, source_id: str) -> dict[str, Any]:
        """Get status of a crawl job."""
        return await self._request("GET", f"/sources/{source_id}/status")

    async def list_crawl_documents(
        self,
        source_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        """List crawled documents."""
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if source_id:
            return await self._request("GET", f"/sources/{source_id}/documents", params=params)
        return await self._request("GET", "/sources/documents", params=params)

    async def get_crawl_document(self, document_id: str) -> dict[str, Any]:
        """Get a crawled document by ID."""
        return await self._request("GET", f"/sources/documents/{document_id}")

    async def crawler_stats(self) -> dict[str, Any]:
        """Get crawler statistics."""
        return await self._request("GET", "/sources/stats")

    async def crawler_health(self) -> dict[str, Any]:
        """Get crawler health status."""
        return await self._request("GET", "/sources/health")
