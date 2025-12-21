"""HTTP client for CLI to communicate with Sibyl REST API.

The CLI is a thin client - all operations go through the REST API,
ensuring consistent event broadcasting and state management.
"""

from typing import Any

import httpx

from sibyl.config import settings


class SibylClientError(Exception):
    """Error from Sibyl API."""

    def __init__(self, message: str, status_code: int | None = None, detail: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.detail = detail


class SibylClient:
    """HTTP client for Sibyl REST API.

    Provides typed methods for all API operations.
    Handles connection errors, retries, and error responses.
    """

    def __init__(self, base_url: str | None = None, timeout: float = 30.0):
        """Initialize the client.

        Args:
            base_url: API base URL. Defaults to http://localhost:{server_port}/api
            timeout: Request timeout in seconds.
        """
        self.base_url = base_url or f"http://localhost:{settings.server_port}/api"
        self.timeout = timeout
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create async HTTP client."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=self.timeout,
                headers={"Content-Type": "application/json"},
            )
        return self._client

    async def close(self) -> None:
        """Close the HTTP client."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    async def _request(
        self,
        method: str,
        path: str,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Make an HTTP request to the API.

        Args:
            method: HTTP method (GET, POST, PATCH, DELETE)
            path: API path (e.g., /entities, /tasks/123/start)
            json: JSON body for POST/PATCH requests
            params: Query parameters

        Returns:
            Response JSON as dict

        Raises:
            SibylClientError: On API errors or connection issues
        """
        client = await self._get_client()

        try:
            response = await client.request(
                method=method,
                url=path,
                json=json,
                params=params,
            )

            # Handle error responses
            if response.status_code >= 400:
                try:
                    error_data = response.json()
                    detail = error_data.get("detail", response.text)
                except Exception:
                    detail = response.text

                raise SibylClientError(
                    f"API error: {detail}",
                    status_code=response.status_code,
                    detail=detail,
                )

            # Return empty dict for 204 No Content
            if response.status_code == 204:
                return {}

            return response.json()

        except httpx.ConnectError as e:
            raise SibylClientError(
                f"Cannot connect to Sibyl API at {self.base_url}. Is the server running?",
                detail=str(e),
            ) from e
        except httpx.TimeoutException as e:
            raise SibylClientError(
                f"Request timed out after {self.timeout}s",
                detail=str(e),
            ) from e

    # =========================================================================
    # Entity Operations
    # =========================================================================

    async def list_entities(
        self,
        entity_type: str | None = None,
        language: str | None = None,
        category: str | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> dict[str, Any]:
        """List entities with optional filters."""
        params: dict[str, Any] = {"page": page, "page_size": page_size}
        if entity_type:
            params["entity_type"] = entity_type
        if language:
            params["language"] = language
        if category:
            params["category"] = category

        return await self._request("GET", "/entities", params=params)

    async def get_entity(self, entity_id: str) -> dict[str, Any]:
        """Get a single entity by ID."""
        return await self._request("GET", f"/entities/{entity_id}")

    async def create_entity(
        self,
        name: str,
        content: str,
        entity_type: str = "episode",
        description: str | None = None,
        category: str | None = None,
        languages: list[str] | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create a new entity."""
        data: dict[str, Any] = {
            "name": name,
            "content": content,
            "entity_type": entity_type,
        }
        if description:
            data["description"] = description
        if category:
            data["category"] = category
        if languages:
            data["languages"] = languages
        if tags:
            data["tags"] = tags
        if metadata:
            data["metadata"] = metadata

        return await self._request("POST", "/entities", json=data)

    async def update_entity(
        self,
        entity_id: str,
        **updates: Any,
    ) -> dict[str, Any]:
        """Update an entity."""
        return await self._request("PATCH", f"/entities/{entity_id}", json=updates)

    async def delete_entity(self, entity_id: str) -> dict[str, Any]:
        """Delete an entity."""
        return await self._request("DELETE", f"/entities/{entity_id}")

    # =========================================================================
    # Task Workflow Operations
    # =========================================================================

    async def start_task(self, task_id: str, assignee: str | None = None) -> dict[str, Any]:
        """Start working on a task."""
        data = {"assignee": assignee} if assignee else None
        return await self._request("POST", f"/tasks/{task_id}/start", json=data)

    async def block_task(self, task_id: str, reason: str) -> dict[str, Any]:
        """Block a task with a reason."""
        return await self._request("POST", f"/tasks/{task_id}/block", json={"reason": reason})

    async def unblock_task(self, task_id: str) -> dict[str, Any]:
        """Unblock a task."""
        return await self._request("POST", f"/tasks/{task_id}/unblock")

    async def submit_review(
        self,
        task_id: str,
        pr_url: str | None = None,
        commit_shas: list[str] | None = None,
    ) -> dict[str, Any]:
        """Submit a task for review."""
        data: dict[str, Any] = {}
        if pr_url:
            data["pr_url"] = pr_url
        if commit_shas:
            data["commit_shas"] = commit_shas
        return await self._request("POST", f"/tasks/{task_id}/review", json=data or None)

    async def complete_task(
        self,
        task_id: str,
        actual_hours: float | None = None,
        learnings: str | None = None,
    ) -> dict[str, Any]:
        """Complete a task."""
        data: dict[str, Any] = {}
        if actual_hours is not None:
            data["actual_hours"] = actual_hours
        if learnings:
            data["learnings"] = learnings
        return await self._request("POST", f"/tasks/{task_id}/complete", json=data or None)

    async def archive_task(self, task_id: str, reason: str | None = None) -> dict[str, Any]:
        """Archive a task."""
        data = {"reason": reason} if reason else None
        return await self._request("POST", f"/tasks/{task_id}/archive", json=data)

    async def update_task(
        self,
        task_id: str,
        status: str | None = None,
        priority: str | None = None,
        title: str | None = None,
        description: str | None = None,
        assignees: list[str] | None = None,
        feature: str | None = None,
    ) -> dict[str, Any]:
        """Update task fields."""
        data: dict[str, Any] = {}
        if status:
            data["status"] = status
        if priority:
            data["priority"] = priority
        if title:
            data["title"] = title
        if description:
            data["description"] = description
        if assignees:
            data["assignees"] = assignees
        if feature:
            data["feature"] = feature

        return await self._request("PATCH", f"/tasks/{task_id}", json=data)

    # =========================================================================
    # Search Operations
    # =========================================================================

    async def search(
        self,
        query: str,
        types: list[str] | None = None,
        language: str | None = None,
        category: str | None = None,
        limit: int = 10,
        include_content: bool = True,
    ) -> dict[str, Any]:
        """Semantic search across the knowledge graph."""
        data: dict[str, Any] = {
            "query": query,
            "limit": limit,
            "include_content": include_content,
        }
        if types:
            data["types"] = types
        if language:
            data["language"] = language
        if category:
            data["category"] = category

        return await self._request("POST", "/search", json=data)

    async def explore(
        self,
        mode: str = "list",
        types: list[str] | None = None,
        entity_id: str | None = None,
        relationship_types: list[str] | None = None,
        depth: int = 1,
        language: str | None = None,
        category: str | None = None,
        project: str | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Explore and traverse the knowledge graph."""
        data: dict[str, Any] = {"mode": mode, "limit": limit, "depth": depth}
        if types:
            data["types"] = types
        if entity_id:
            data["entity_id"] = entity_id
        if relationship_types:
            data["relationship_types"] = relationship_types
        if language:
            data["language"] = language
        if category:
            data["category"] = category
        if project:
            data["project"] = project
        if status:
            data["status"] = status

        return await self._request("POST", "/search/explore", json=data)

    # =========================================================================
    # Admin Operations
    # =========================================================================

    async def health(self) -> dict[str, Any]:
        """Get server health status."""
        return await self._request("GET", "/admin/health")

    async def stats(self) -> dict[str, Any]:
        """Get knowledge graph statistics."""
        return await self._request("GET", "/admin/stats")

    async def ingest(self, path: str | None = None, force: bool = False) -> dict[str, Any]:
        """Trigger document ingestion."""
        data: dict[str, Any] = {"force": force}
        if path:
            data["path"] = path
        return await self._request("POST", "/admin/ingest", json=data)

    # =========================================================================
    # Knowledge Operations
    # =========================================================================

    async def add_knowledge(
        self,
        title: str,
        content: str,
        entity_type: str = "episode",
        category: str | None = None,
        languages: list[str] | None = None,
        tags: list[str] | None = None,
        auto_link: bool = False,
    ) -> dict[str, Any]:
        """Add knowledge to the graph (via create_entity with knowledge semantics)."""
        metadata: dict[str, Any] = {}
        if category:
            metadata["category"] = category
        if tags:
            metadata["tags"] = tags
        if auto_link:
            metadata["auto_link"] = auto_link

        return await self.create_entity(
            name=title,
            content=content,
            entity_type=entity_type,
            languages=languages,
            metadata=metadata if metadata else None,
        )

    # =========================================================================
    # Crawler Operations
    # =========================================================================

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
            return await self._request(
                "GET", f"/sources/{source_id}/documents", params=params
            )
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


# Singleton client instance
_client: SibylClient | None = None


def get_client() -> SibylClient:
    """Get the singleton client instance."""
    global _client  # noqa: PLW0603
    if _client is None:
        _client = SibylClient()
    return _client
