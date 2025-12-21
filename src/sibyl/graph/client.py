"""Graphiti client wrapper for FalkorDB."""

import asyncio
import os
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

    def _create_llm_client(self) -> object:
        """Create the LLM client based on provider settings.

        Returns:
            Configured LLM client (AnthropicClient or OpenAIClient).
        """
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
            from graphiti_core import Graphiti
            from graphiti_core.driver.falkordb_driver import FalkorDriver

            log.info(
                "Connecting to FalkorDB",
                host=settings.falkordb_host,
                port=settings.falkordb_port,
                graph=settings.falkordb_graph_name,
                llm_provider=settings.llm_provider,
                llm_model=settings.llm_model,
            )

            # Create FalkorDB driver with connection details
            driver = FalkorDriver(
                host=settings.falkordb_host,
                port=settings.falkordb_port,
                password=settings.falkordb_password or None,
                database=settings.falkordb_graph_name,
            )

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

    @property
    def driver(self) -> object:
        """Get the underlying FalkorDB driver.

        Convenience property to access client.driver directly.

        Returns:
            The FalkorDB driver instance.

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
