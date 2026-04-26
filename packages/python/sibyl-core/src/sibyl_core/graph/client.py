"""Graphiti client wrapper for Sibyl's active graph runtime."""

import asyncio
import os
from pathlib import Path
from typing import TYPE_CHECKING

import structlog
from dotenv import load_dotenv

from sibyl_core.config import core_config as settings

# Load .env BEFORE graphiti is imported to ensure SEMAPHORE_LIMIT is set
# This prevents FalkorDB race condition crashes by serializing Graphiti operations
_env_path = Path(__file__).parent.parent.parent.parent / ".env"
if _env_path.exists():
    load_dotenv(_env_path)

# Set SEMAPHORE_LIMIT from settings (mostly unused - we patch semaphore_gather below)
# Kept for any code paths that might read the env var directly
if not os.getenv("SEMAPHORE_LIMIT"):
    os.environ["SEMAPHORE_LIMIT"] = str(settings.graphiti_semaphore_limit)

# Graphiti's OpenAI embedder reads EMBEDDING_DIM at import time. If unset, Graphiti
# defaults to 1024, but we pin it explicitly to avoid "mixed-dimension" graphs when
# a different EMBEDDING_DIM leaks in from the shell environment.
if not os.getenv("EMBEDDING_DIM"):
    os.environ["EMBEDDING_DIM"] = str(settings.graph_embedding_dimensions)

# Disable Graphiti's PostHog telemetry (noisy retry errors when offline)
os.environ.setdefault("GRAPHITI_TELEMETRY_ENABLED", "false")


def _patch_semaphore_gather() -> None:
    """Remove Graphiti's semaphore bottleneck by replacing semaphore_gather with asyncio.gather.

    Graphiti uses a global semaphore (SEMAPHORE_LIMIT) for ALL concurrent operations,
    including both LLM calls and FalkorDB queries. This was designed for LLM rate limiting,
    but applying it to DB operations creates artificial serialization.

    Our FalkorDB setup already has proper concurrency controls:
    - BlockingConnectionPool (50 connections, 60s wait timeout)
    - FalkorDB/Redis handles concurrent queries natively

    This patch replaces semaphore_gather with plain asyncio.gather, eliminating the
    Python-level bottleneck while preserving FalkorDB's natural concurrency handling.
    """
    import asyncio
    from collections.abc import Coroutine
    from typing import Any

    import graphiti_core.helpers as helpers

    async def unlimited_gather[T](
        *coroutines: Coroutine[Any, Any, T],
    ) -> list[T]:
        """Execute coroutines concurrently without semaphore throttling."""
        return list(await asyncio.gather(*coroutines))

    # Replace the throttled version with unlimited concurrency
    helpers.semaphore_gather = unlimited_gather


_patch_semaphore_gather()


def _patch_falkordb_driver() -> None:
    """Monkey-patch FalkorDriver FalkorDB behavior we rely on.

    Graphiti's FalkorDriver auto-runs build_indices_and_constraints() on every __init__,
    including when clone() creates a new driver for a different database. This causes
    44+ second blocks on every clone() as FalkorDB re-verifies all indexes.

    This patch replaces clone() with the base class's with_database() approach,
    which uses copy.copy() instead of creating a new instance. This avoids __init__
    entirely, eliminating redundant index rebuilds while preserving the shared connection.

    Upstream PR: https://github.com/getzep/graphiti/pull/XXX
    """
    import copy

    from graphiti_core.driver.falkordb import STOPWORDS
    from graphiti_core.driver.falkordb_driver import FalkorDriver
    from graphiti_core.search.search_utils import validate_group_ids

    def patched_clone(self, database: str):
        """Clone using shallow copy to avoid triggering __init__ and index rebuilding."""
        if database == self._database:
            return self

        # Use copy.copy() like the base class with_database() method
        # This creates a shallow copy without calling __init__, avoiding index rebuilds
        cloned = copy.copy(self)
        cloned._database = database
        return cloned

    def patched_build_fulltext_query(
        self, query: str, group_ids: list[str] | None = None, max_query_length: int = 128
    ) -> str:
        """Build FalkorDB fulltext queries without quoting org UUID filters.

        Graphiti 0.28.2 started quoting FalkorDB group_id filters, producing
        `@group_id:"uuid"`. FalkorDB rejects that syntax for our UUID-backed
        group_ids with `Syntax error at offset 20`, while the unquoted form works.
        """
        validate_group_ids(group_ids)

        group_filter = "" if not group_ids else f"(@group_id:{'|'.join(group_ids)})"

        sanitized_query = self.sanitize(query)
        query_words = sanitized_query.split()
        filtered_words = [word for word in query_words if word and word.lower() not in STOPWORDS]
        sanitized_query = " | ".join(filtered_words)

        if len(sanitized_query.split(" ")) + len(group_ids or []) >= max_query_length:
            return ""

        return group_filter + " (" + sanitized_query + ")"

    FalkorDriver.clone = patched_clone
    FalkorDriver.build_fulltext_query = patched_build_fulltext_query


from sibyl_core.errors import GraphConnectionError  # noqa: E402
from sibyl_core.utils.resilience import GRAPH_RETRY, TIMEOUTS, retry, with_timeout  # noqa: E402

if TYPE_CHECKING:
    from graphiti_core import Graphiti
    from graphiti_core.driver.driver import GraphDriver
    from graphiti_core.llm_client import LLMClient

log = structlog.get_logger()


class GraphClient:
    """Wrapper around Graphiti client for knowledge graph operations.

    This client manages the connection to the active graph runtime and provides
    high-level methods for graph operations.
    """

    def __init__(self) -> None:
        """Initialize the graph client."""
        self._client: Graphiti | None = None
        self._connected = False
        self._store = settings.store
        self._org_drivers: dict[str, GraphDriver] = {}

    def _create_llm_client(self) -> "LLMClient":
        """Create the LLM client based on provider settings.

        Returns:
            Configured LLM client (MockLLMClient, AnthropicClient, or OpenAIClient).
        """
        # Check for mock mode first (for CI/testing without API keys)
        if os.getenv("SIBYL_MOCK_LLM", "").lower() in ("true", "1", "yes"):
            from sibyl_core.graph.mock_llm import MockLLMClient

            log.info("Using MockLLMClient (SIBYL_MOCK_LLM=true)")
            return MockLLMClient()

        from graphiti_core.llm_client.config import LLMConfig

        if settings.llm_provider == "anthropic":
            from graphiti_core.llm_client.anthropic_client import AnthropicClient

            # Get API key from settings or environment
            api_key = settings.anthropic_api_key.get_secret_value()
            if not api_key:
                api_key = os.getenv("ANTHROPIC_API_KEY", "")

            config = LLMConfig(
                api_key=api_key,
                model=settings.llm_model,
            )
            log.debug("Using Anthropic LLM client", model=settings.llm_model)
            return AnthropicClient(config=config)

        # openai
        from graphiti_core.llm_client.openai_client import OpenAIClient

        api_key = settings.openai_api_key.get_secret_value()
        if not api_key:
            api_key = os.getenv("OPENAI_API_KEY", "")

        config = LLMConfig(
            api_key=api_key,
            model=settings.llm_model,
        )
        log.debug("Using OpenAI LLM client", model=settings.llm_model)
        return OpenAIClient(config=config)

    def _prepare_embedder_env(self) -> None:
        """Ensure Graphiti's OpenAI embedder sees the configured API key."""
        openai_key = settings.openai_api_key.get_secret_value()
        if openai_key and not os.getenv("OPENAI_API_KEY"):
            os.environ["OPENAI_API_KEY"] = openai_key

    def _wrap_graphiti_embedder_cache(self) -> None:
        """Wrap Graphiti's embedder with a small LRU cache."""
        if self._client is None:
            return

        from sibyl_core.graph.cached_embedder import wrap_embedder_with_cache

        self._client.embedder = wrap_embedder_with_cache(
            self._client.embedder,
            max_size=2000,
        )

    async def _connect_legacy(self) -> None:
        """Establish the legacy FalkorDB runtime connection."""
        try:
            from falkordb.asyncio import FalkorDB
            from graphiti_core import Graphiti
            from graphiti_core.driver.falkordb_driver import FalkorDriver
            from redis.asyncio import BlockingConnectionPool

            _patch_falkordb_driver()

            log.info(
                "Connecting to FalkorDB",
                host=settings.falkordb_host,
                port=settings.falkordb_port,
                llm_provider=settings.llm_provider,
                llm_model=settings.llm_model,
                max_connections=50,
                semaphore_limit=os.getenv("SEMAPHORE_LIMIT", "20"),
            )

            # Create a BlockingConnectionPool - this is the key to stability!
            # Unlike regular ConnectionPool which errors when exhausted,
            # BlockingConnectionPool waits for a connection to become available.
            # This prevents "connection reset by peer" errors under concurrent load.
            # See: https://redis.io/docs/latest/develop/clients/pools-and-muxing/
            #
            # NOTE: Graphiti's add_episode() can take 60-90s under heavy load
            # (edge_fulltext_search, LLM extraction, embedding). Set socket_timeout
            # high enough to accommodate these operations without timing out.
            connection_pool = BlockingConnectionPool(
                host=settings.falkordb_host,
                port=settings.falkordb_port,
                password=settings.falkordb_password or None,
                max_connections=50,  # Pool size (BlockingConnectionPool default)
                timeout=60,  # Wait up to 60s for a connection from pool
                socket_timeout=120.0,  # 120s timeout for operations (Graphiti needs time)
                socket_connect_timeout=15.0,  # 15s timeout for initial connect
                socket_keepalive=True,  # Keep connections alive
                health_check_interval=15,  # Check connection health every 15s
                decode_responses=True,  # FalkorDB expects decoded responses
            )

            # Create FalkorDB client with the blocking connection pool
            falkor_client = FalkorDB(connection_pool=connection_pool)

            # Create FalkorDB driver with our configured client
            # Note: database is a placeholder - actual graph is set per-operation via group_id (org.id)
            driver = FalkorDriver(
                falkor_db=falkor_client,
                database="default",
            )

            # Inject optimized search interface to avoid O(n²) edge lookups
            # See search_interface.py for details on the performance issue
            from sibyl_core.graph.search_interface import FalkorDBSearchInterface

            driver.search_interface = FalkorDBSearchInterface()

            # Create LLM client based on provider setting
            llm_client = self._create_llm_client()

            self._prepare_embedder_env()

            # Initialize Graphiti with the driver and LLM client
            self._client = Graphiti(graph_driver=driver, llm_client=llm_client)

            self._wrap_graphiti_embedder_cache()

            self._connected = True
            self._store = "legacy"
            log.info("Connected to FalkorDB successfully", llm_provider=settings.llm_provider)

        except Exception as e:
            # Use log.error (not exception) to avoid traceback spam in CLI
            log.error("Failed to connect to FalkorDB", error=str(e))
            raise GraphConnectionError(
                f"Failed to connect to FalkorDB: {e}",
                details={"host": settings.falkordb_host, "port": settings.falkordb_port},
            ) from e

    async def _connect_surreal(self) -> None:
        """Establish the SurrealDB runtime connection."""
        try:
            from graphiti_core import Graphiti

            from sibyl_core.backends.surreal import SurrealDriver

            url = settings.resolved_surreal_url
            log.info(
                "Connecting to SurrealDB",
                url=url,
                namespace_prefix=settings.surreal_namespace_prefix,
                database=settings.surreal_database,
                llm_provider=settings.llm_provider,
                llm_model=settings.llm_model,
            )

            driver = SurrealDriver(
                url,
                username=settings.surreal_username or None,
                password=settings.surreal_password.get_secret_value() or None,
                token=settings.surreal_token.get_secret_value() or None,
                namespace_prefix=settings.surreal_namespace_prefix,
                default_database=settings.surreal_database,
            )
            llm_client = self._create_llm_client()

            self._prepare_embedder_env()
            self._client = Graphiti(graph_driver=driver, llm_client=llm_client)
            self._wrap_graphiti_embedder_cache()

            self._connected = True
            self._store = "surreal"
            log.info("Connected to SurrealDB successfully", url=url)

        except Exception as e:
            log.error("Failed to connect to SurrealDB", error=str(e))
            raise GraphConnectionError(
                f"Failed to connect to SurrealDB: {e}",
                details={"url": settings.resolved_surreal_url},
            ) from e

    async def connect(self) -> None:
        """Establish connection to the configured graph runtime."""
        if settings.store == "surreal":
            await self._connect_surreal()
            return

        await self._connect_legacy()

    async def disconnect(self) -> None:
        """Close the graph database connection."""
        seen_driver_ids: set[int] = set()
        for driver in self._org_drivers.values():
            driver_id = id(driver)
            if driver_id in seen_driver_ids:
                continue
            seen_driver_ids.add(driver_id)
            close = getattr(driver, "close", None)
            if close is not None:
                await close()
        self._org_drivers.clear()
        if self._client is not None:
            await self._client.close()
            self._connected = False
            log.info("Disconnected from graph runtime", store=self._store)

    @property
    def client(self) -> "Graphiti":
        """Get the underlying Graphiti client.

        Raises:
            GraphConnectionError: If not connected.
        """
        if self._client is None or not self._connected:
            raise GraphConnectionError("Not connected to graph database")
        return self._client

    @property
    def is_connected(self) -> bool:
        """Check if the client is connected."""
        return self._connected

    @property
    def driver(self) -> "GraphDriver":
        """Get the underlying graph driver.

        Convenience property to access client.driver directly.

        Returns:
            The active graph driver instance.

        Raises:
            GraphConnectionError: If not connected.
        """
        return self.client.driver

    async def query_with_timeout(
        self,
        query_coro: object,
        operation_name: str = "graph_query",
    ) -> object:
        """Execute a query coroutine with timeout protection.

        Args:
            query_coro: The coroutine to execute
            operation_name: Name for timeout error messages

        Returns:
            Query result
        """
        timeout = TIMEOUTS.get(operation_name, TIMEOUTS["graph_query"])
        return await with_timeout(query_coro, timeout, operation_name)  # type: ignore[arg-type]

    @staticmethod
    def normalize_result(result: object) -> list[dict]:
        """Normalize graph driver query results to a consistent list of dicts.

        FalkorDB returns a tuple, SurrealDB often returns a single dict or list,
        and some call sites expect just a list of row dicts.

        Args:
            result: Raw result from execute_query

        Returns:
            List of result records (possibly empty)
        """
        if result is None:
            return []
        if isinstance(result, tuple):
            # FalkorDB returns (records, header, metadata)
            records = result[0] if len(result) > 0 else []
            return records if records else []  # type: ignore[return-value]
        if isinstance(result, list):
            return result  # type: ignore[return-value]
        if isinstance(result, dict):
            return [result]
        return []

    def get_org_driver(self, organization_id: str) -> "GraphDriver":
        """Get a driver cloned for a specific organization's graph.

        Each organization gets an isolated logical graph. The group ID becomes
        the cloned driver's org-scoped database or namespace.

        Args:
            organization_id: The organization UUID to scope the driver to.

        Returns:
            A graph driver instance scoped to the org's graph.

        Raises:
            ValueError: If organization_id is empty.
        """
        if not organization_id:
            raise ValueError("organization_id is required for org-scoped operations")
        if organization_id not in self._org_drivers:
            self._org_drivers[organization_id] = self.client.driver.clone(organization_id)
        return self._org_drivers[organization_id]

    async def ensure_indexes(self, organization_id: str) -> None:
        """Ensure required indexes exist for an organization's graph.

        Safe to call multiple times. The active runtime handles this idempotently.

        Args:
            organization_id: The organization UUID.
        """
        driver = self.get_org_driver(organization_id)

        if self._store == "surreal":
            await driver.build_indices_and_constraints()
            log.info("Ensured SurrealDB schema", org=organization_id)
            return

        # First, let Graphiti create its standard indexes (range + fulltext)
        # This is idempotent - safe to call if indexes exist
        try:
            await self.client.build_indices_and_constraints()
        except Exception as e:
            log.debug("Graphiti index creation skipped", error=str(e))

        # Create vector index for Entity nodes (required for cosine similarity search)
        # FalkorDB vector index syntax - idempotent (fails silently if exists)
        vector_index_query = """
            CREATE VECTOR INDEX FOR (n:Entity) ON (n.name_embedding)
            OPTIONS {dimension: 1536, similarityFunction: 'cosine'}
        """
        try:
            await driver.execute_query(vector_index_query)
            log.info("Created vector index on Entity.name_embedding", org=organization_id)
        except Exception as e:
            # Index likely already exists - this is fine
            if "already indexed" not in str(e).lower() and "exists" not in str(e).lower():
                log.debug("Vector index creation note", error=str(e))

        # Create composite indexes for common query patterns
        composite_indexes = [
            # Task queries by project and status (most common filter combo)
            "CREATE INDEX FOR (n:Entity) ON (n.project_id, n.status)",
            # Entity type filtering (used in almost every query)
            "CREATE INDEX FOR (n:Entity) ON (n.entity_type)",
            # Episodic node type filtering
            "CREATE INDEX FOR (n:Episodic) ON (n.entity_type)",
        ]
        for idx_query in composite_indexes:
            try:
                await driver.execute_query(idx_query)
            except Exception as e:
                # Index likely already exists - this is fine
                if "already indexed" not in str(e).lower() and "exists" not in str(e).lower():
                    log.debug("Index creation note", query=idx_query, error=str(e))

    async def execute_read(self, query: str, **params: object) -> list[dict]:
        """Execute a read query on the default graph. DEPRECATED for multi-tenant ops.

        WARNING: This uses the default graph, not org-scoped. Use execute_read_org()
        for multi-tenant operations.

        Args:
            query: Cypher query to execute
            **params: Query parameters

        Returns:
            List of result records as dicts
        """
        result = await self.client.driver.execute_query(query, **params)
        return self.normalize_result(result)

    async def execute_write(self, query: str, **params: object) -> list[dict]:
        """Execute a write query on the default graph. DEPRECATED for multi-tenant ops.

        WARNING: This uses the default graph, not org-scoped. Use execute_write_org()
        for multi-tenant operations.

        Uses a semaphore to prevent concurrent writes from corrupting the
        FalkorDB connection. Returns the query results for verification.

        Args:
            query: Cypher query to execute
            **params: Query parameters

        Returns:
            List of result records as dicts

        Raises:
            Exception: If query execution fails
        """
        result = await self.client.driver.execute_query(query, **params)
        return self.normalize_result(result)

    async def execute_read_org(
        self, query: str, organization_id: str, **params: object
    ) -> list[dict]:
        """Execute a read query on an organization's graph.

        This is the preferred method for multi-tenant read operations.

        Args:
            query: Cypher query to execute
            organization_id: The organization UUID to scope the query to.
            **params: Query parameters

        Returns:
            List of result records as dicts
        """
        driver = self.get_org_driver(organization_id)
        result = await driver.execute_query(query, **params)
        return self.normalize_result(result)

    async def execute_write_org(
        self, query: str, organization_id: str, **params: object
    ) -> list[dict]:
        """Execute a write query on an organization's graph.

        This is the preferred method for multi-tenant write operations.

        Args:
            query: Cypher query to execute
            organization_id: The organization UUID to scope the query to.
            **params: Query parameters

        Returns:
            List of result records as dicts

        Raises:
            Exception: If query execution fails
        """
        driver = self.get_org_driver(organization_id)
        result = await driver.execute_query(query, **params)
        return self.normalize_result(result)

    async def __aenter__(self) -> "GraphClient":
        """Async context manager entry."""
        await self.connect()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object,
    ) -> None:
        """Async context manager exit."""
        await self.disconnect()


# Global client instance with thread-safe initialization
_graph_client: GraphClient | None = None
_client_lock = asyncio.Lock()


@retry(config=GRAPH_RETRY)
async def _connect_client() -> GraphClient:
    """Create and connect a new graph client with retry logic."""
    client = GraphClient()
    await client.connect()
    return client


async def get_graph_client() -> GraphClient:
    """Get the global graph client instance.

    Creates and connects a new client if one doesn't exist.
    Thread-safe via asyncio.Lock to prevent race conditions.
    Retries on transient connection failures.
    """
    global _graph_client
    async with _client_lock:
        if _graph_client is None:
            _graph_client = await _connect_client()
    return _graph_client


async def reset_graph_client() -> None:
    """Reset the global client (useful for testing)."""
    global _graph_client
    async with _client_lock:
        if _graph_client is not None:
            await _graph_client.disconnect()
            _graph_client = None
