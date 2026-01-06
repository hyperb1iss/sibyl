"""HTTP client for CLI to communicate with Sibyl REST API.

The CLI is a thin client - all operations go through the REST API,
ensuring consistent event broadcasting and state management.
"""

import os
from typing import Any

import httpx

from sibyl_cli.auth_store import (
    get_access_token,
    get_refresh_token,
    is_access_token_expired,
    normalize_api_url,
    set_tokens,
)

# Default server port (matches sibyl-server default)
DEFAULT_SERVER_PORT = 3334


def _get_default_api_url(context_name: str | None = None) -> str:
    """Get API URL from context, config file, env var, or default.

    Priority:
    1. Explicit context (if provided)
    2. Active context's server_url
    3. Environment variable (SIBYL_API_URL)
    4. Legacy config file (server.url)
    5. Default (http://localhost:3334/api)

    Args:
        context_name: Optional context name to use instead of active context.
    """
    # Lazy import to avoid circular dependency
    from sibyl_cli import config_store

    # 1. If explicit context provided, use that
    if context_name:
        ctx = config_store.get_context(context_name)
        if ctx:
            return f"{ctx.server_url}/api"
        # Context not found - fall through to other options

    # 2. Try active context
    ctx = config_store.get_active_context()
    if ctx:
        return f"{ctx.server_url}/api"

    # 3. Try env var
    env_url = os.environ.get("SIBYL_API_URL", "").strip()
    if env_url:
        return env_url

    # 4. Try legacy config file
    if config_store.config_exists():
        url = config_store.get_server_url()
        if url:
            return f"{url}/api"

    # 5. Default
    return f"http://localhost:{DEFAULT_SERVER_PORT}/api"


def _load_default_auth_token(api_base_url: str) -> str | None:
    """Load auth token for the given API URL.

    Priority:
    1. SIBYL_AUTH_TOKEN environment variable
    2. Stored access token for the specific server
    """
    env_token = os.environ.get("SIBYL_AUTH_TOKEN", "").strip()
    if env_token:
        return env_token

    return get_access_token(api_base_url)


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

    def __init__(
        self,
        base_url: str | None = None,
        timeout: float = 30.0,
        auth_token: str | None = None,
        context_name: str | None = None,
    ):
        """Initialize the client.

        Args:
            base_url: API base URL. Defaults to context, then env var, then localhost.
            timeout: Request timeout in seconds.
            auth_token: Optional bearer token or API key to send as Authorization header.
            context_name: Optional context name to use for URL and auth resolution.
        """
        self.context_name = context_name
        self.base_url = normalize_api_url(base_url or _get_default_api_url(context_name))
        self.timeout = timeout
        self.auth_token = auth_token or _load_default_auth_token(self.base_url)
        self._client: httpx.AsyncClient | None = None
        # Load insecure setting from context
        self.insecure = self._get_insecure_from_context(context_name)

    def _get_insecure_from_context(self, context_name: str | None) -> bool:
        """Get insecure setting from context config."""
        from sibyl_cli import config_store

        if context_name:
            ctx = config_store.get_context(context_name)
            if ctx:
                return ctx.insecure
        # Check active context
        ctx = config_store.get_active_context()
        if ctx:
            return ctx.insecure
        return False

    def _default_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self.auth_token:
            headers["Authorization"] = f"Bearer {self.auth_token}"
        return headers

    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create async HTTP client."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=self.timeout,
                headers=self._default_headers(),
                verify=not self.insecure,
            )
        return self._client

    async def close(self) -> None:
        """Close the HTTP client."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self) -> "SibylClient":
        """Async context manager entry."""
        return self

    async def __aexit__(self, *args: object) -> None:
        """Async context manager exit."""
        await self.close()

    async def _refresh_token(self) -> bool:
        """Attempt to refresh the access token using stored refresh token.

        Returns:
            True if refresh succeeded, False otherwise
        """
        refresh_token = get_refresh_token(self.base_url)
        if not refresh_token:
            return False

        try:
            # Use a fresh client without auth header for the refresh call
            async with httpx.AsyncClient(
                base_url=self.base_url,
                timeout=self.timeout,
                verify=not self.insecure,
            ) as client:
                response = await client.post(
                    "/auth/refresh",
                    json={"refresh_token": refresh_token},
                )

                if response.status_code != 200:
                    return False

                data = response.json()
                new_access_token = data.get("access_token")
                new_refresh_token = data.get("refresh_token")
                expires_in = data.get("expires_in")

                if not new_access_token:
                    return False

                # Store new tokens for this server
                set_tokens(
                    self.base_url,
                    new_access_token,
                    refresh_token=new_refresh_token,
                    expires_in=expires_in,
                )

                # Update client state
                self.auth_token = new_access_token

                # Recreate HTTP client with new token
                if self._client and not self._client.is_closed:
                    await self._client.aclose()
                self._client = None

                return True

        except Exception:
            return False

    async def _request(
        self,
        method: str,
        path: str,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        *,
        _retry_on_401: bool = True,
    ) -> dict[str, Any]:
        """Make an HTTP request to the API.

        Args:
            method: HTTP method (GET, POST, PATCH, DELETE)
            path: API path (e.g., /entities, /tasks/123/start)
            json: JSON body for POST/PATCH requests
            params: Query parameters
            _retry_on_401: Internal flag to prevent infinite retry loops

        Returns:
            Response JSON as dict

        Raises:
            SibylClientError: On API errors or connection issues
        """
        # Proactively refresh if token is about to expire
        if self.auth_token and is_access_token_expired(self.base_url):
            await self._refresh_token()

        client = await self._get_client()

        try:
            response = await client.request(
                method=method,
                url=path,
                json=json,
                params=params,
            )

            # Handle 401 - try to refresh token and retry once
            if response.status_code == 401 and _retry_on_401 and await self._refresh_token():
                # Retry with new token
                return await self._request(
                    method, path, json=json, params=params, _retry_on_401=False
                )

            # Handle error responses
            if response.status_code >= 400:
                try:
                    error_data = response.json()
                    detail = error_data.get("detail", response.text)
                except Exception:
                    detail = response.text

                if response.status_code in {401, 403}:
                    detail = (
                        f"{detail}\n\n"
                        "Auth required. Run 'sibyl auth login' or set SIBYL_AUTH_TOKEN."
                    )

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
    # Generic HTTP Methods
    # =========================================================================

    async def get(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Generic GET request."""
        return await self._request("GET", path, params=params)

    async def post(
        self,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Generic POST request."""
        return await self._request("POST", path, json=json, params=params)

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

    # =========================================================================
    # Auth Operations
    # =========================================================================

    async def list_api_keys(self) -> dict[str, Any]:
        return await self._request("GET", "/auth/api-keys")

    async def create_api_key(
        self,
        *,
        name: str,
        live: bool = True,
        scopes: list[str] | None = None,
        expires_days: int | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"name": name, "live": live}
        if scopes is not None:
            payload["scopes"] = scopes
        if expires_days is not None:
            payload["expires_days"] = expires_days
        return await self._request("POST", "/auth/api-keys", json=payload)

    async def revoke_api_key(self, api_key_id: str) -> dict[str, Any]:
        return await self._request("POST", f"/auth/api-keys/{api_key_id}/revoke")

    async def local_signup(
        self,
        *,
        email: str,
        password: str,
        name: str,
        redirect: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"email": email, "password": password, "name": name}
        if redirect is not None:
            payload["redirect"] = redirect
        return await self._request("POST", "/auth/local/signup", json=payload)

    async def local_login(
        self,
        *,
        email: str,
        password: str,
        redirect: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"email": email, "password": password}
        if redirect is not None:
            payload["redirect"] = redirect
        return await self._request("POST", "/auth/local/login", json=payload)

    async def list_orgs(self) -> dict[str, Any]:
        return await self._request("GET", "/orgs")

    async def create_org(self, name: str, slug: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"name": name}
        if slug:
            payload["slug"] = slug
        return await self._request("POST", "/orgs", json=payload)

    async def switch_org(self, slug: str) -> dict[str, Any]:
        return await self._request("POST", f"/orgs/{slug}/switch")

    # -------------------------------------------------------------------------
    # Org Members
    # -------------------------------------------------------------------------

    async def list_org_members(self, slug: str) -> dict[str, Any]:
        """List all members of an organization."""
        return await self._request("GET", f"/orgs/{slug}/members")

    async def add_org_member(self, slug: str, user_id: str, role: str = "member") -> dict[str, Any]:
        """Add a member to an organization."""
        return await self._request(
            "POST", f"/orgs/{slug}/members", json={"user_id": user_id, "role": role}
        )

    async def update_org_member_role(self, slug: str, user_id: str, role: str) -> dict[str, Any]:
        """Update a member's role in an organization."""
        return await self._request("PATCH", f"/orgs/{slug}/members/{user_id}", json={"role": role})

    async def remove_org_member(self, slug: str, user_id: str) -> dict[str, Any]:
        """Remove a member from an organization."""
        return await self._request("DELETE", f"/orgs/{slug}/members/{user_id}")

    async def get_entity(self, entity_id: str) -> dict[str, Any]:
        """Get a single entity by ID with related context."""
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
        sync: bool = False,
    ) -> dict[str, Any]:
        """Create a new entity.

        Args:
            sync: If True, wait for entity creation to complete (slower but
                  entity is immediately available for operations like task start).
        """
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

        params = {"sync": "true"} if sync else None
        return await self._request("POST", "/entities", json=data, params=params)

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
        complexity: str | None = None,
        title: str | None = None,
        description: str | None = None,
        assignees: list[str] | None = None,
        epic_id: str | None = None,
        feature: str | None = None,
        tags: list[str] | None = None,
        technologies: list[str] | None = None,
    ) -> dict[str, Any]:
        """Update task fields."""
        data: dict[str, Any] = {}
        if status:
            data["status"] = status
        if priority:
            data["priority"] = priority
        if complexity:
            data["complexity"] = complexity
        if title:
            data["title"] = title
        if description:
            data["description"] = description
        if assignees:
            data["assignees"] = assignees
        if epic_id:
            data["epic_id"] = epic_id
        if feature:
            data["feature"] = feature
        if tags:
            data["tags"] = tags
        if technologies:
            data["technologies"] = technologies

        return await self._request("PATCH", f"/tasks/{task_id}", json=data)

    # =========================================================================
    # Task Notes Operations
    # =========================================================================

    async def create_note(
        self,
        task_id: str,
        content: str,
        author_type: str = "user",
        author_name: str = "",
    ) -> dict[str, Any]:
        """Create a note on a task."""
        data = {
            "content": content,
            "author_type": author_type,
            "author_name": author_name,
        }
        return await self._request("POST", f"/tasks/{task_id}/notes", json=data)

    async def list_notes(
        self,
        task_id: str,
        limit: int = 50,
    ) -> dict[str, Any]:
        """List notes for a task."""
        params = {"limit": limit}
        return await self._request("GET", f"/tasks/{task_id}/notes", params=params)

    # =========================================================================
    # Search Operations
    # =========================================================================

    async def search(
        self,
        query: str,
        types: list[str] | None = None,
        language: str | None = None,
        category: str | None = None,
        project: str | None = None,
        limit: int = 10,
        offset: int = 0,
        include_content: bool = True,
    ) -> dict[str, Any]:
        """Semantic search across the knowledge graph."""
        data: dict[str, Any] = {
            "query": query,
            "limit": limit,
            "offset": offset,
            "include_content": include_content,
        }
        if types:
            data["types"] = types
        if language:
            data["language"] = language
        if category:
            data["category"] = category
        if project:
            data["project"] = project

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
        epic: str | None = None,
        no_epic: bool = False,
        status: str | None = None,
        priority: str | None = None,
        complexity: str | None = None,
        feature: str | None = None,
        tags: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Explore and traverse the knowledge graph."""
        data: dict[str, Any] = {"mode": mode, "limit": limit, "offset": offset, "depth": depth}
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
        if epic:
            data["epic"] = epic
        if no_epic:
            data["no_epic"] = True
        if status:
            data["status"] = status
        if priority:
            data["priority"] = priority
        if complexity:
            data["complexity"] = complexity
        if feature:
            data["feature"] = feature
        if tags:
            data["tags"] = tags

        return await self._request("POST", "/search/explore", json=data)

    async def temporal_query(
        self,
        mode: str = "history",
        entity_id: str | None = None,
        as_of: str | None = None,
        include_expired: bool = False,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Query bi-temporal history of edges.

        Modes:
        - history: Edges as they existed at a point in time
        - timeline: All versions of edges over time
        - conflicts: Find invalidated/superseded facts
        """
        data: dict[str, Any] = {"mode": mode, "limit": limit}
        if entity_id:
            data["entity_id"] = entity_id
        if as_of:
            data["as_of"] = as_of
        if include_expired:
            data["include_expired"] = True

        return await self._request("POST", "/search/temporal", json=data)

    # =========================================================================
    # Admin Operations
    # =========================================================================

    async def health(self) -> dict[str, Any]:
        """Get server health status."""
        return await self._request("GET", "/admin/health")

    async def stats(self) -> dict[str, Any]:
        """Get knowledge graph statistics."""
        return await self._request("GET", "/admin/stats")

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
    ) -> dict[str, Any]:
        """Add knowledge to the graph (via create_entity with knowledge semantics)."""
        return await self.create_entity(
            name=title,
            content=content,
            entity_type=entity_type,
            category=category,
            languages=languages,
            tags=tags,
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

    async def link_graph(
        self,
        source_id: str | None = None,
        batch_size: int = 50,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Link document chunks to knowledge graph via entity extraction.

        Args:
            source_id: Specific source ID, or None for all sources
            batch_size: Chunks per batch
            dry_run: Preview without processing

        Returns:
            LinkGraphResponse with stats
        """
        data = {"batch_size": batch_size, "dry_run": dry_run}
        if source_id:
            return await self._request("POST", f"/sources/{source_id}/link-graph", json=data)
        return await self._request("POST", "/sources/link-graph", json=data)

    async def link_graph_status(self) -> dict[str, Any]:
        """Get status of pending graph linking work.

        Returns:
            LinkGraphStatusResponse with pending chunk counts per source
        """
        return await self._request("GET", "/sources/link-graph/status")


# Client cache by context name (None = default/active context)
_clients: dict[str | None, SibylClient] = {}


def get_client(context_name: str | None = None) -> SibylClient:
    """Get a client instance for the given context.

    Clients are cached by context name. Passing None uses the global override,
    then falls back to active context.

    Priority for context resolution:
    1. Explicit context_name parameter
    2. Global --context flag override
    3. SIBYL_CONTEXT environment variable
    4. Active context from config

    Args:
        context_name: Optional context name. None = use override or active context.

    Returns:
        SibylClient configured for the specified context.
    """
    global _clients

    # Check for global override if no explicit context provided
    if context_name is None:
        from sibyl_cli.state import get_context_override

        context_name = get_context_override()

    cache_key = context_name

    if cache_key not in _clients:
        _clients[cache_key] = SibylClient(context_name=context_name)

    return _clients[cache_key]


def clear_client_cache() -> None:
    """Clear the client cache. Useful when context settings change."""
    global _clients
    _clients.clear()
