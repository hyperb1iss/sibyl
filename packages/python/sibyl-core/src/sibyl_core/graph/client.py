"""Graphiti client wrapper for FalkorDB."""

import asyncio
import os
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

import structlog
from dotenv import load_dotenv

from sibyl_core.config import core_config as settings

# Load .env BEFORE graphiti is imported to ensure SEMAPHORE_LIMIT is set
# This prevents FalkorDB race condition crashes by serializing Graphiti operations
_env_path = Path(__file__).parent.parent.parent.parent / ".env"
if _env_path.exists():
    load_dotenv(_env_path)

# Set SEMAPHORE_LIMIT from settings if not already set (controls Graphiti LLM concurrency)
if not os.getenv("SEMAPHORE_LIMIT"):
    os.environ["SEMAPHORE_LIMIT"] = str(settings.graphiti_semaphore_limit)

# Graphiti's OpenAI embedder reads EMBEDDING_DIM at import time. If unset, Graphiti
# defaults to 1024, but we pin it explicitly to avoid "mixed-dimension" graphs when
# a different EMBEDDING_DIM leaks in from the shell environment.
if not os.getenv("EMBEDDING_DIM"):
    os.environ["EMBEDDING_DIM"] = str(settings.graph_embedding_dimensions)

# Disable Graphiti's PostHog telemetry (noisy retry errors when offline)
os.environ.setdefault("GRAPHITI_TELEMETRY_ENABLED", "false")


def _patch_falkordb_driver() -> None:
    """Monkey-patch FalkorDriver to skip index rebuilding on clone().

    Graphiti's FalkorDriver auto-runs build_indices_and_constraints() on every __init__,
    including when clone() creates a new driver for a different database. This causes
    44+ second blocks on every clone() as FalkorDB re-verifies all indexes.

    This patch replaces clone() with the base class's with_database() approach,
    which uses copy.copy() instead of creating a new instance. This avoids __init__
    entirely, eliminating redundant index rebuilds while preserving the shared connection.

    Upstream PR: https://github.com/getzep/graphiti/pull/XXX
    """
    import copy

    from graphiti_core.driver.falkordb_driver import FalkorDriver

    def patched_clone(self, database: str):
        """Clone using shallow copy to avoid triggering __init__ and index rebuilding."""
        if database == self._database:
            return self

        # Use copy.copy() like the base class with_database() method
        # This creates a shallow copy without calling __init__, avoiding index rebuilds
        cloned = copy.copy(self)
        cloned._database = database
        return cloned

    FalkorDriver.clone = patched_clone


_patch_falkordb_driver()

from sibyl_core.errors import GraphConnectionError  # noqa: E402
from sibyl_core.utils.resilience import GRAPH_RETRY, TIMEOUTS, retry, with_timeout  # noqa: E402

if TYPE_CHECKING:
    from graphiti_core import Graphiti
    from graphiti_core.driver.falkordb_driver import FalkorDriver
    from graphiti_core.llm_client import LLMClient

log = structlog.get_logger()


class GraphClient:
    """Wrapper around Graphiti client for knowledge graph operations.

    This client manages the connection to FalkorDB and provides
    high-level methods for graph operations.

    Uses per-org semaphores to serialize write operations, preventing connection
    contention while allowing different organizations to write in parallel.
    """

    # Limit concurrent DB operations to prevent connection contention
    # Higher values improve bulk performance; lower values reduce contention risk
    _MAX_CONCURRENT_WRITES = 20  # High parallelism for bulk operations

    # Global semaphore (deprecated - use per-org semaphores via get_org_write_lock)
    _write_semaphore: asyncio.Semaphore | None = None

    # Per-org write semaphores: org_id -> Semaphore
    # Each org gets its own semaphore to prevent cross-org blocking
    _org_semaphores: ClassVar[dict[str, asyncio.Semaphore]] = {}
    _semaphore_lock: ClassVar[asyncio.Lock | None] = None  # Protects _org_semaphores access

    def __init__(self) -> None:
        """Initialize the graph client."""
        self._client: Graphiti | None = None
        self._connected = False
        # Initialize global semaphore lazily (for backward compat with write_lock property)
        if GraphClient._write_semaphore is None:
            GraphClient._write_semaphore = asyncio.Semaphore(self._MAX_CONCURRENT_WRITES)
        # Initialize lock for semaphore dictionary access
        if GraphClient._semaphore_lock is None:
            GraphClient._semaphore_lock = asyncio.Lock()

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

    async def connect(self) -> None:
        """Establish connection to FalkorDB via Graphiti.

        Raises:
            GraphConnectionError: If connection fails.
        """
        try:
            from falkordb.asyncio import FalkorDB
            from graphiti_core import Graphiti
            from graphiti_core.driver.falkordb_driver import FalkorDriver
            from redis.asyncio import BlockingConnectionPool

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
            connection_pool = BlockingConnectionPool(
                host=settings.falkordb_host,
                port=settings.falkordb_port,
                password=settings.falkordb_password or None,
                max_connections=50,  # Pool size (BlockingConnectionPool default)
                timeout=30,  # Wait up to 30s for a connection
                socket_timeout=30.0,  # 30s timeout for operations
                socket_connect_timeout=10.0,  # 10s timeout for connect
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

            # Ensure OpenAI API key is set for embeddings (Graphiti still uses OpenAI for embeddings)
            openai_key = settings.openai_api_key.get_secret_value()
            if openai_key and not os.getenv("OPENAI_API_KEY"):
                os.environ["OPENAI_API_KEY"] = openai_key

            # Initialize Graphiti with the driver and LLM client
            self._client = Graphiti(graph_driver=driver, llm_client=llm_client)
            self._connected = True
            log.info("Connected to FalkorDB successfully", llm_provider=settings.llm_provider)

        except Exception as e:
            # Use log.error (not exception) to avoid traceback spam in CLI
            log.error("Failed to connect to FalkorDB", error=str(e))
            raise GraphConnectionError(
                f"Failed to connect to FalkorDB: {e}",
                details={"host": settings.falkordb_host, "port": settings.falkordb_port},
            ) from e

    async def disconnect(self) -> None:
        """Close the graph database connection."""
        if self._client is not None:
            await self._client.close()
            self._connected = False
            log.info("Disconnected from FalkorDB")

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
    def driver(self) -> "FalkorDriver":
        """Get the underlying FalkorDB driver.

        Convenience property to access client.driver directly.

        Returns:
            The FalkorDB driver instance.

        Raises:
            GraphConnectionError: If not connected.
        """
        return self.client.driver  # type: ignore[return-value]

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

    @property
    def write_lock(self) -> asyncio.Semaphore:
        """Get the global write semaphore for serializing DB operations.

        DEPRECATED: Use get_org_write_lock() for per-org write locking.
        This global lock is retained for backward compatibility with
        non-org-scoped operations like ensure_indexes().

        Returns:
            Semaphore that limits concurrent writes to prevent connection contention.
        """
        if GraphClient._write_semaphore is None:
            GraphClient._write_semaphore = asyncio.Semaphore(self._MAX_CONCURRENT_WRITES)
        return GraphClient._write_semaphore

    async def get_org_write_lock(self, organization_id: str) -> asyncio.Semaphore:
        """Get a write semaphore for a specific organization.

        Each organization gets its own semaphore, allowing writes to different
        orgs to proceed in parallel while serializing writes within the same org.
        This prevents one busy org from blocking writes to other orgs.

        Args:
            organization_id: The organization UUID to get a lock for.

        Returns:
            Semaphore for the specified organization.

        Raises:
            ValueError: If organization_id is empty.
        """
        if not organization_id:
            raise ValueError("organization_id is required for per-org write locking")

        # Ensure lock is initialized
        if GraphClient._semaphore_lock is None:
            GraphClient._semaphore_lock = asyncio.Lock()

        # Thread-safe access to semaphore dictionary
        async with GraphClient._semaphore_lock:
            if organization_id not in GraphClient._org_semaphores:
                GraphClient._org_semaphores[organization_id] = asyncio.Semaphore(
                    self._MAX_CONCURRENT_WRITES
                )
                log.debug(
                    "Created org write semaphore",
                    org_id=organization_id,
                    max_concurrent=self._MAX_CONCURRENT_WRITES,
                )
            return GraphClient._org_semaphores[organization_id]

    def org_write_context(self, organization_id: str) -> "OrgWriteContext":
        """Get a context manager for org-scoped writes.

        Usage:
            async with client.org_write_context(org_id) as lock:
                await driver.execute_query(query)

        Args:
            organization_id: The organization UUID.

        Returns:
            Async context manager that acquires org-specific write lock.
        """
        return OrgWriteContext(self, organization_id)

    @staticmethod
    def normalize_result(result: object) -> list[dict]:  # type: ignore[type-arg]
        """Normalize FalkorDB query results to a consistent list of dicts.

        FalkorDB driver returns (records, header, metadata) tuple, but some
        code paths expect just a list. This helper ensures consistent handling.

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
        return []

    def get_org_driver(self, organization_id: str) -> "FalkorDriver":
        """Get a driver cloned for a specific organization's graph.

        Each organization has its own isolated graph in FalkorDB.
        The organization_id becomes the graph/database name.

        Args:
            organization_id: The organization UUID to scope the driver to.

        Returns:
            A FalkorDB driver instance scoped to the org's graph.

        Raises:
            ValueError: If organization_id is empty.
        """
        if not organization_id:
            raise ValueError("organization_id is required for org-scoped operations")
        return self.client.driver.clone(organization_id)  # type: ignore[return-value]

    async def ensure_indexes(self, organization_id: str) -> None:
        """Ensure required indexes exist for an organization's graph.

        Creates Graphiti's standard indexes plus vector index for semantic search.
        Safe to call multiple times - indexes are created idempotently.

        Args:
            organization_id: The organization UUID.
        """
        driver = self.get_org_driver(organization_id)

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
            async with self.write_lock:
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
                async with self.write_lock:
                    await driver.execute_query(idx_query)
            except Exception as e:
                # Index likely already exists - this is fine
                if "already indexed" not in str(e).lower() and "exists" not in str(e).lower():
                    log.debug("Index creation note", query=idx_query, error=str(e))

    async def execute_read(self, query: str, **params: object) -> list[dict]:  # type: ignore[type-arg]
        """Execute a read query on the default graph. DEPRECATED for multi-tenant ops.

        WARNING: This uses the default graph, not org-scoped. Use execute_read_org()
        for multi-tenant operations.

        Args:
            query: Cypher query to execute
            **params: Query parameters

        Returns:
            List of result records as dicts
        """
        # type: ignore[arg-type] - dynamic query strings
        result = await self.client.driver.execute_query(query, **params)  # type: ignore[arg-type]
        return self.normalize_result(result)

    async def execute_write(self, query: str, **params: object) -> list[dict]:  # type: ignore[type-arg]
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
        async with self.write_lock:
            # type: ignore[arg-type] - dynamic query strings
            result = await self.client.driver.execute_query(query, **params)  # type: ignore[arg-type]
            return self.normalize_result(result)

    async def execute_read_org(
        self, query: str, organization_id: str, **params: object
    ) -> list[dict]:  # type: ignore[type-arg]
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
        result = await driver.execute_query(query, **params)  # type: ignore[arg-type]
        return self.normalize_result(result)

    async def execute_write_org(
        self, query: str, organization_id: str, **params: object
    ) -> list[dict]:  # type: ignore[type-arg]
        """Execute a write query on an organization's graph.

        This is the preferred method for multi-tenant write operations.
        Uses a per-org semaphore to serialize writes within the same org
        while allowing writes to different orgs in parallel.

        Args:
            query: Cypher query to execute
            organization_id: The organization UUID to scope the query to.
            **params: Query parameters

        Returns:
            List of result records as dicts

        Raises:
            Exception: If query execution fails
        """
        org_lock = await self.get_org_write_lock(organization_id)
        async with org_lock:
            driver = self.get_org_driver(organization_id)
            result = await driver.execute_query(query, **params)  # type: ignore[arg-type]
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


class OrgWriteContext:
    """Async context manager for organization-scoped write locking.

    Acquires the per-org semaphore on entry and releases on exit.
    Allows convenient usage with async with syntax.

    Usage:
        async with client.org_write_context(org_id):
            await driver.execute_query(query)
    """

    def __init__(self, client: GraphClient, organization_id: str) -> None:
        """Initialize the context manager.

        Args:
            client: The GraphClient instance.
            organization_id: The organization UUID.
        """
        self._client = client
        self._organization_id = organization_id
        self._semaphore: asyncio.Semaphore | None = None

    async def __aenter__(self) -> asyncio.Semaphore:
        """Acquire the org-specific write lock.

        Returns:
            The acquired semaphore (for inspection if needed).
        """
        self._semaphore = await self._client.get_org_write_lock(self._organization_id)
        await self._semaphore.acquire()
        return self._semaphore

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object,
    ) -> None:
        """Release the org-specific write lock."""
        if self._semaphore is not None:
            self._semaphore.release()


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
    """Reset the global client and per-org semaphores (useful for testing)."""
    global _graph_client
    async with _client_lock:
        if _graph_client is not None:
            await _graph_client.disconnect()
            _graph_client = None
        # Clear per-org semaphores to prevent state leakage between tests
        GraphClient._org_semaphores.clear()
