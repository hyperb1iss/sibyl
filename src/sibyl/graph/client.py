"""Graphiti client wrapper for FalkorDB."""

import asyncio
from typing import TYPE_CHECKING

import structlog

from sibyl.config import settings
from sibyl.errors import GraphConnectionError
from sibyl.utils.resilience import GRAPH_RETRY, TIMEOUTS, retry, with_timeout

if TYPE_CHECKING:
    from graphiti_core import Graphiti

log = structlog.get_logger()


class GraphClient:
    """Wrapper around Graphiti client for knowledge graph operations.

    This client manages the connection to FalkorDB and provides
    high-level methods for graph operations.
    """

    def __init__(self) -> None:
        """Initialize the graph client."""
        self._client: Graphiti | None = None
        self._connected = False

    async def connect(self) -> None:
        """Establish connection to FalkorDB via Graphiti.

        Raises:
            GraphConnectionError: If connection fails.
        """
        try:
            from graphiti_core import Graphiti
            from graphiti_core.driver.falkordb_driver import FalkorDriver

            log.info(
                "Connecting to FalkorDB",
                host=settings.falkordb_host,
                port=settings.falkordb_port,
                graph=settings.falkordb_graph_name,
            )

            # Create FalkorDB driver with connection details
            driver = FalkorDriver(
                host=settings.falkordb_host,
                port=settings.falkordb_port,
                password=settings.falkordb_password or None,
                database=settings.falkordb_graph_name,
            )

            # Initialize Graphiti with the driver
            self._client = Graphiti(graph_driver=driver)
            self._connected = True
            log.info("Connected to FalkorDB successfully")

        except Exception as e:
            # Use log.error (not exception) to avoid traceback spam in CLI
            log.error("Failed to connect to FalkorDB", error=str(e))  # noqa: TRY400
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
    global _graph_client  # noqa: PLW0603
    async with _client_lock:
        if _graph_client is None:
            _graph_client = await _connect_client()
    return _graph_client


async def reset_graph_client() -> None:
    """Reset the global client (useful for testing)."""
    global _graph_client  # noqa: PLW0603
    async with _client_lock:
        if _graph_client is not None:
            await _graph_client.disconnect()
            _graph_client = None
