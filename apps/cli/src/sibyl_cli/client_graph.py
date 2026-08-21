"""Entity, graph, search, exploration, and temporal client operations."""

from typing import Any
from urllib.parse import quote


class ClientGraphMixin:
    """Entity, graph, search, exploration, and temporal client operations."""

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

    async def get_entity(
        self,
        entity_id: str,
        *,
        include_summary: bool = True,
        related_limit: int = 5,
    ) -> dict[str, Any]:
        """Get a single entity by ID with related context."""
        return await self._request(
            "GET",
            f"/entities/{entity_id}",
            params={
                "include_summary": include_summary,
                "related_limit": related_limit,
            },
        )

    async def list_raw_captures(
        self,
        *,
        entity_type: str | None = None,
        capture_surface: str | None = None,
        review_state: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        """List archived raw quick captures."""
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if entity_type:
            params["entity_type"] = entity_type
        if capture_surface:
            params["capture_surface"] = capture_surface
        if review_state:
            params["review_state"] = review_state
        return await self._request("GET", "/entities/captures", params=params)

    async def get_raw_capture(self, capture_id: str) -> dict[str, Any]:
        """Get a single archived raw quick capture."""
        return await self._request("GET", f"/entities/captures/{capture_id}")

    async def create_entity(
        self,
        name: str,
        content: str,
        entity_type: str = "episode",
        description: str | None = None,
        category: str | None = None,
        languages: list[str] | None = None,
        tags: list[str] | None = None,
        related_to: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        sync: bool = False,
        skip_conflicts: bool = False,
        retrieval_keys: list[str] | None = None,
        spans: list[dict[str, Any]] | None = None,
        atomic: bool = False,
        probes: list[str] | None = None,
    ) -> dict[str, Any]:
        """Create a new entity.

        Args:
            sync: If True, wait for entity creation to complete (slower but
                  entity is immediately available for operations like task start).
            retrieval_keys: Exact-match identifiers this entity answers to.
            spans: Agent-authored cut plan tiling the stored content exactly.
            atomic: Declare the body one retrievable unit that must not be cut.
            probes: Questions the memory must answer, rehearsed at write time.
        """
        data: dict[str, Any] = {
            "name": name,
            "content": content,
            "entity_type": entity_type,
        }
        if spans is not None:
            data["spans"] = spans
        if atomic:
            data["atomic"] = True
        if probes is not None:
            data["probes"] = probes
        if description:
            data["description"] = description
        if category:
            data["category"] = category
        if languages:
            data["languages"] = languages
        if tags:
            data["tags"] = tags
        if related_to:
            data["related_to"] = related_to
        if metadata:
            data["metadata"] = metadata
        if skip_conflicts:
            data["skip_conflicts"] = True
        if retrieval_keys:
            data["retrieval_keys"] = retrieval_keys

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

    async def resolve_id_prefix(
        self,
        prefix: str,
        *,
        entity_type: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        """Resolve a short graph ID prefix to matching full IDs."""
        params: dict[str, Any] = {"limit": limit}
        if entity_type:
            params["entity_type"] = entity_type
        return await self._request("GET", f"/resolve/{quote(prefix, safe='')}", params=params)

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
        content_max_chars: int = 500,
        include_documents: bool = True,
        include_graph: bool = True,
        as_of: str | None = None,
    ) -> dict[str, Any]:
        """Semantic search across the knowledge graph."""
        data: dict[str, Any] = {
            "query": query,
            "limit": limit,
            "offset": offset,
            "include_content": include_content,
            "content_max_chars": content_max_chars,
            "include_documents": include_documents,
            "include_graph": include_graph,
        }
        if types:
            data["types"] = types
        if language:
            data["language"] = language
        if category:
            data["category"] = category
        if project:
            data["project"] = project
        if as_of:
            data["as_of"] = as_of

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

    async def link_graph(
        self,
        source_id: str | None = None,
        batch_size: int = 50,
        dry_run: bool = False,
        create_new_entities: bool = False,
    ) -> dict[str, Any]:
        """Link document chunks to knowledge graph via entity extraction.

        Args:
            source_id: Specific source ID, or None for all sources
            batch_size: Chunks per batch
            dry_run: Preview without processing
            create_new_entities: Create graph entities for unlinked extractions

        Returns:
            LinkGraphResponse with stats
        """
        data = {
            "batch_size": batch_size,
            "dry_run": dry_run,
            "create_new_entities": create_new_entities,
        }
        if source_id:
            return await self._request("POST", f"/sources/{source_id}/link-graph", json=data)
        return await self._request("POST", "/sources/link-graph", json=data)

    async def link_graph_status(self) -> dict[str, Any]:
        """Get status of pending graph linking work.

        Returns:
            LinkGraphStatusResponse with pending chunk counts per source
        """
        return await self._request("GET", "/sources/link-graph/status")
