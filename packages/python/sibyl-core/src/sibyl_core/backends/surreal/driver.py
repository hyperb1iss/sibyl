"""SurrealDB implementation of Graphiti's GraphDriver interface.

Each organization's knowledge graph lives in its own SurrealDB namespace
(``org_{hex_uuid}``) under database ``graph``. ``clone(group_id)`` returns a
driver scoped to that org's namespace with a fresh client connection.

Connection model:
    A single AsyncSurreal client per driver instance. clone() creates a new
    driver with its own client so per-org namespace switching is safe under
    concurrent use. Embedded connections (``memory://`` / ``surrealkv://``)
    skip authentication; remote connections (``ws://`` / ``http://``) call
    signin() on first use.

Transaction model:
    Embedded SurrealDB has no transaction support. ``execute_write`` runs
    without BEGIN/COMMIT and relies on caller-side idempotency. WebSocket
    connections could wrap in BEGIN/COMMIT in a later iteration; Phase 1
    ships with the permissive embedded behavior.

Provider tag:
    Graphiti's ``GraphProvider`` enum does not yet include a SurrealDB
    variant. We reuse ``NEO4J`` as the closest semantic neighbor (openCypher
    family, property-graph model). Sibyl's custom operations interfaces
    intercept save/delete dispatch before any provider-specific Cypher is
    emitted, so the tag only affects fallback paths we override.
"""

from __future__ import annotations

import asyncio
import copy
import logging
from functools import cached_property
from typing import Any

from graphiti_core.driver.driver import (
    GraphDriver,
    GraphDriverSession,
    GraphProvider,
)

logger = logging.getLogger(__name__)

# See module docstring "Provider tag" for rationale.
_SURREAL_PROVIDER_TAG: GraphProvider = GraphProvider.NEO4J


def _raise_if_surreal_error(query: str, result: Any) -> None:
    """Raise a SurrealQueryError when the SDK returned an error envelope."""
    if isinstance(result, str):
        if query.lstrip().upper().startswith("RETURN "):
            return
        raise SurrealQueryError(query, result)
    if isinstance(result, list):
        for entry in result:
            if isinstance(entry, dict) and entry.get("status") == "ERR":
                raise SurrealQueryError(query, str(entry.get("result", entry)))


class SurrealQueryError(RuntimeError):
    """Raised when SurrealDB returns an error envelope instead of result rows."""

    def __init__(self, query: str, message: str) -> None:
        snippet = (query[:120] + "…") if len(query) > 120 else query
        super().__init__(f"SurrealDB query failed: {message} (query: {snippet!r})")
        self.query = query
        self.surreal_message = message


def _namespace_for_group(prefix: str, group_id: str) -> str:
    """Translate a Sibyl group_id (UUID) into a SurrealDB namespace name.

    SurrealDB namespace names must be alphanumeric plus underscores; UUID
    hyphens are stripped. An empty group_id yields a placeholder namespace
    used only for root-level operations (eg. ``INFO FOR KV``).
    """
    sanitized = group_id.replace("-", "").lower() if group_id else "default"
    return f"{prefix}{sanitized}"


class SurrealDriverSession(GraphDriverSession):
    """GraphDriverSession wrapper for SurrealDB.

    The session delegates to the owning driver's client and provides the
    async context manager interface Graphiti expects. Closing the session
    does not close the underlying driver connection (that belongs to the
    driver lifecycle).
    """

    provider: GraphProvider = _SURREAL_PROVIDER_TAG

    def __init__(self, driver: SurrealDriver) -> None:
        self._driver = driver

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        await self.close()

    async def run(self, query: str, **kwargs: Any) -> Any:
        return await self._driver.execute_query(query, **kwargs)

    async def close(self) -> None:
        return None

    async def execute_write(self, func: Any, *args: Any, **kwargs: Any) -> Any:
        return await func(self, *args, **kwargs)


class SurrealDriver(GraphDriver):
    """Graphiti ``GraphDriver`` backed by SurrealDB."""

    provider: GraphProvider = _SURREAL_PROVIDER_TAG
    fulltext_syntax: str = ""

    def __init__(
        self,
        url: str,
        *,
        username: str | None = None,
        password: str | None = None,
        token: str | None = None,
        namespace_prefix: str = "org_",
        default_database: str = "graph",
    ) -> None:
        self._url = url
        self._username = username
        self._password = password
        self._token = token
        self._namespace_prefix = namespace_prefix
        self._default_database = default_database
        self._database: str = ""
        self._client: Any | None = None
        self._query_lock = asyncio.Lock()

    @property
    def namespace_prefix(self) -> str:
        return self._namespace_prefix

    @property
    def default_database(self) -> str:
        return self._default_database

    @property
    def group_id(self) -> str:
        return self._database

    @cached_property
    def _entity_node_ops(self):
        from sibyl_core.graph.surreal.ops.entity_node_ops import SurrealEntityNodeOperations

        return SurrealEntityNodeOperations()

    @property
    def entity_node_ops(self):
        return self._entity_node_ops

    @cached_property
    def _episode_node_ops(self):
        from sibyl_core.graph.surreal.ops.episode_node_ops import SurrealEpisodeNodeOperations

        return SurrealEpisodeNodeOperations()

    @property
    def episode_node_ops(self):
        return self._episode_node_ops

    @cached_property
    def _community_node_ops(self):
        from sibyl_core.graph.surreal.ops.community_node_ops import SurrealCommunityNodeOperations

        return SurrealCommunityNodeOperations()

    @property
    def community_node_ops(self):
        return self._community_node_ops

    @cached_property
    def _saga_node_ops(self):
        from sibyl_core.graph.surreal.ops.saga_node_ops import SurrealSagaNodeOperations

        return SurrealSagaNodeOperations()

    @property
    def saga_node_ops(self):
        return self._saga_node_ops

    @cached_property
    def _entity_edge_ops(self):
        from sibyl_core.graph.surreal.ops.entity_edge_ops import SurrealEntityEdgeOperations

        return SurrealEntityEdgeOperations()

    @property
    def entity_edge_ops(self):
        return self._entity_edge_ops

    @cached_property
    def _episodic_edge_ops(self):
        from sibyl_core.graph.surreal.ops.episodic_edge_ops import SurrealEpisodicEdgeOperations

        return SurrealEpisodicEdgeOperations()

    @property
    def episodic_edge_ops(self):
        return self._episodic_edge_ops

    @cached_property
    def _community_edge_ops(self):
        from sibyl_core.graph.surreal.ops.community_edge_ops import SurrealCommunityEdgeOperations

        return SurrealCommunityEdgeOperations()

    @property
    def community_edge_ops(self):
        return self._community_edge_ops

    @cached_property
    def _has_episode_edge_ops(self):
        from sibyl_core.graph.surreal.ops.has_episode_edge_ops import (
            SurrealHasEpisodeEdgeOperations,
        )

        return SurrealHasEpisodeEdgeOperations()

    @property
    def has_episode_edge_ops(self):
        return self._has_episode_edge_ops

    @cached_property
    def _next_episode_edge_ops(self):
        from sibyl_core.graph.surreal.ops.next_episode_edge_ops import (
            SurrealNextEpisodeEdgeOperations,
        )

        return SurrealNextEpisodeEdgeOperations()

    @property
    def next_episode_edge_ops(self):
        return self._next_episode_edge_ops

    @cached_property
    def _graph_ops(self):
        from sibyl_core.graph.surreal.ops.graph_ops import SurrealGraphMaintenanceOperations

        return SurrealGraphMaintenanceOperations()

    @property
    def graph_ops(self):
        return self._graph_ops

    async def _ensure_client(self) -> Any:
        if self._client is not None:
            return self._client

        from surrealdb import AsyncSurreal

        client = AsyncSurreal(self._url)

        if self._requires_auth():
            if self._token:
                await client.authenticate(self._token)
            elif self._username and self._password:
                await client.signin({"username": self._username, "password": self._password})

        namespace = _namespace_for_group(self._namespace_prefix, self._database)
        await client.use(namespace, self._default_database)

        self._client = client
        return client

    def _requires_auth(self) -> bool:
        return not self._url.startswith(("memory://", "surrealkv://"))

    async def execute_query(self, cypher_query_: str, **kwargs: Any) -> Any:
        async with self._query_lock:
            client = await self._ensure_client()
            result = await client.query(cypher_query_, kwargs if kwargs else None)
        # The surrealdb SDK does not raise on per-statement errors — it returns
        # the error message as a string (single statement) or as `{"status":
        # "ERR", "result": "..."}` (multi-statement). Detect both and raise so
        # bulk paths can't falsely report success on a rejected insert.
        _raise_if_surreal_error(cypher_query_, result)
        return result

    def session(self, database: str | None = None) -> GraphDriverSession:
        if database is not None and database != self._database:
            return self.clone(database).session()
        return SurrealDriverSession(self)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None

    def clone(self, database: str) -> SurrealDriver:
        if database == self._database and self._client is not None:
            return self
        cloned = copy.copy(self)
        cloned._database = database
        cloned._client = None
        cloned._query_lock = asyncio.Lock()
        return cloned

    async def delete_all_indexes(self) -> None:
        from sibyl_core.backends.surreal.schema import drop_all_indexes

        await drop_all_indexes(self)

    async def build_indices_and_constraints(self, delete_existing: bool = False) -> None:
        if not self._database:
            logger.debug(
                "build_indices_and_constraints called without group_id; "
                "use driver.clone(org_id) first",
            )
            return

        from sibyl_core.backends.surreal.schema import bootstrap_schema

        await bootstrap_schema(self, reset=delete_existing)

    def build_fulltext_query(
        self,
        query: str,
        group_ids: list[str] | None = None,
        max_query_length: int = 128,
    ) -> str:
        del group_ids
        sanitized = "".join(c for c in query if c.isprintable() and c not in ('"', "'")).strip()
        if not sanitized:
            return ""
        if len(sanitized) > max_query_length:
            sanitized = sanitized[:max_query_length]
        return sanitized


__all__ = ["SurrealDriver", "SurrealDriverSession", "_namespace_for_group"]
