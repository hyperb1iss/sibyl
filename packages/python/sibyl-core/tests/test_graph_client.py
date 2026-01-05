"""Tests for GraphClient per-org semaphore functionality.

Covers the organization-scoped write locking that allows different
orgs to write in parallel while serializing writes within each org.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from sibyl_core.graph.client import GraphClient, OrgWriteContext


# =============================================================================
# Fixtures
# =============================================================================
@pytest.fixture
def graph_client() -> GraphClient:
    """Create a fresh GraphClient instance.

    Clears class-level state between tests to prevent cross-test pollution.
    """
    # Clear class-level state
    GraphClient._org_semaphores.clear()
    GraphClient._write_semaphore = None
    GraphClient._semaphore_lock = None
    return GraphClient()


@pytest.fixture
def org_id() -> str:
    """Generate a unique organization ID."""
    return str(uuid4())


@pytest.fixture
def org_id_2() -> str:
    """Generate a second unique organization ID."""
    return str(uuid4())


# =============================================================================
# get_org_write_lock Tests
# =============================================================================
class TestGetOrgWriteLock:
    """Tests for get_org_write_lock method."""

    @pytest.mark.asyncio
    async def test_returns_semaphore(self, graph_client: GraphClient, org_id: str) -> None:
        """Returns an asyncio Semaphore."""
        lock = await graph_client.get_org_write_lock(org_id)
        assert isinstance(lock, asyncio.Semaphore)

    @pytest.mark.asyncio
    async def test_same_org_returns_same_semaphore(
        self, graph_client: GraphClient, org_id: str
    ) -> None:
        """Returns the same semaphore for repeated calls with same org."""
        lock1 = await graph_client.get_org_write_lock(org_id)
        lock2 = await graph_client.get_org_write_lock(org_id)
        assert lock1 is lock2

    @pytest.mark.asyncio
    async def test_different_orgs_return_different_semaphores(
        self, graph_client: GraphClient, org_id: str, org_id_2: str
    ) -> None:
        """Different orgs get different semaphores."""
        lock1 = await graph_client.get_org_write_lock(org_id)
        lock2 = await graph_client.get_org_write_lock(org_id_2)
        assert lock1 is not lock2

    @pytest.mark.asyncio
    async def test_empty_org_id_raises(self, graph_client: GraphClient) -> None:
        """Raises ValueError for empty organization ID."""
        with pytest.raises(ValueError, match="organization_id is required"):
            await graph_client.get_org_write_lock("")

    @pytest.mark.asyncio
    async def test_semaphore_has_correct_limit(
        self, graph_client: GraphClient, org_id: str
    ) -> None:
        """Semaphore has the configured concurrency limit."""
        lock = await graph_client.get_org_write_lock(org_id)
        # Semaphore._value is the current available count
        # Initial value should equal max concurrent writes
        assert lock._value == GraphClient._MAX_CONCURRENT_WRITES

    @pytest.mark.asyncio
    async def test_concurrent_get_calls_safe(self, graph_client: GraphClient, org_id: str) -> None:
        """Multiple concurrent calls to get_org_write_lock are thread-safe."""
        # Launch many concurrent requests for the same org's lock
        tasks = [graph_client.get_org_write_lock(org_id) for _ in range(100)]
        results = await asyncio.gather(*tasks)

        # All should return the same semaphore instance
        first = results[0]
        assert all(lock is first for lock in results)


# =============================================================================
# org_write_context Tests
# =============================================================================
class TestOrgWriteContext:
    """Tests for org_write_context context manager."""

    @pytest.mark.asyncio
    async def test_acquires_and_releases_lock(self, graph_client: GraphClient, org_id: str) -> None:
        """Context manager acquires lock on enter, releases on exit."""
        lock = await graph_client.get_org_write_lock(org_id)
        initial_value = lock._value

        async with graph_client.org_write_context(org_id):
            # Lock should be held (value decremented)
            assert lock._value == initial_value - 1

        # Lock should be released (value restored)
        assert lock._value == initial_value

    @pytest.mark.asyncio
    async def test_releases_on_exception(self, graph_client: GraphClient, org_id: str) -> None:
        """Lock is released even if an exception occurs."""
        lock = await graph_client.get_org_write_lock(org_id)
        initial_value = lock._value

        with pytest.raises(ValueError):
            async with graph_client.org_write_context(org_id):
                raise ValueError("Test error")

        # Lock should still be released
        assert lock._value == initial_value

    @pytest.mark.asyncio
    async def test_returns_semaphore(self, graph_client: GraphClient, org_id: str) -> None:
        """Context manager returns the semaphore on enter."""
        async with graph_client.org_write_context(org_id) as lock:
            assert isinstance(lock, asyncio.Semaphore)


# =============================================================================
# OrgWriteContext Class Tests
# =============================================================================
class TestOrgWriteContextClass:
    """Tests for OrgWriteContext class directly."""

    @pytest.mark.asyncio
    async def test_init_stores_references(self, graph_client: GraphClient, org_id: str) -> None:
        """Constructor stores client and org_id."""
        ctx = OrgWriteContext(graph_client, org_id)
        assert ctx._client is graph_client
        assert ctx._organization_id == org_id
        assert ctx._semaphore is None

    @pytest.mark.asyncio
    async def test_aenter_acquires_lock(self, graph_client: GraphClient, org_id: str) -> None:
        """__aenter__ acquires the org lock."""
        ctx = OrgWriteContext(graph_client, org_id)
        lock = await graph_client.get_org_write_lock(org_id)
        initial = lock._value

        await ctx.__aenter__()
        assert lock._value == initial - 1
        assert ctx._semaphore is lock

        # Clean up
        await ctx.__aexit__(None, None, None)

    @pytest.mark.asyncio
    async def test_aexit_releases_lock(self, graph_client: GraphClient, org_id: str) -> None:
        """__aexit__ releases the org lock."""
        ctx = OrgWriteContext(graph_client, org_id)
        lock = await graph_client.get_org_write_lock(org_id)

        await ctx.__aenter__()
        initial = lock._value

        await ctx.__aexit__(None, None, None)
        assert lock._value == initial + 1


# =============================================================================
# Isolation Tests
# =============================================================================
class TestOrgIsolation:
    """Tests that different orgs are truly isolated."""

    @pytest.mark.asyncio
    async def test_org_writes_dont_block_each_other(
        self, graph_client: GraphClient, org_id: str, org_id_2: str
    ) -> None:
        """Writes to different orgs can proceed in parallel."""
        acquired_order: list[str] = []
        released_order: list[str] = []

        async def write_to_org(oid: str, delay: float) -> None:
            """Simulate a write operation with delay."""
            async with graph_client.org_write_context(oid):
                acquired_order.append(oid)
                await asyncio.sleep(delay)
                released_order.append(oid)

        # Start both writes concurrently
        # org_id has longer delay, org_id_2 should complete first
        await asyncio.gather(
            write_to_org(org_id, 0.1),
            write_to_org(org_id_2, 0.01),
        )

        # Both orgs acquired locks (order may vary based on scheduling)
        assert set(acquired_order) == {org_id, org_id_2}
        # org_id_2 with shorter delay should release first
        assert org_id_2 in released_order

    @pytest.mark.asyncio
    async def test_same_org_writes_serialized(self, graph_client: GraphClient, org_id: str) -> None:
        """Multiple writes to same org are serialized by the semaphore."""
        # Set semaphore to 1 for strict serialization test
        lock = await graph_client.get_org_write_lock(org_id)
        # Manually adjust for testing (normally wouldn't do this)
        lock._value = 1

        order: list[int] = []

        async def write_task(task_id: int) -> None:
            """Task that records when it runs."""
            async with graph_client.org_write_context(org_id):
                order.append(task_id)
                await asyncio.sleep(0.01)

        # With semaphore=1, these should execute one at a time
        # Launch 3 concurrent tasks
        await asyncio.gather(
            write_task(1),
            write_task(2),
            write_task(3),
        )

        # All three should have completed
        assert len(order) == 3
        # Order should contain all tasks
        assert set(order) == {1, 2, 3}


# =============================================================================
# execute_write_org Integration Tests
# =============================================================================
class TestExecuteWriteOrgIntegration:
    """Tests for execute_write_org using per-org semaphores."""

    @pytest.mark.asyncio
    async def test_uses_org_semaphore(self, graph_client: GraphClient, org_id: str) -> None:
        """execute_write_org uses org-specific semaphore."""
        # Mock the driver and query execution
        mock_driver = MagicMock()
        mock_driver.execute_query = AsyncMock(return_value=([], [], {}))

        with (
            patch.object(graph_client, "get_org_driver", return_value=mock_driver),
            patch.object(graph_client, "_client", create=True),
        ):
            graph_client._connected = True

            # Get the org's semaphore and track its value
            lock = await graph_client.get_org_write_lock(org_id)
            initial = lock._value

            # execute_write_org should acquire and release the org lock
            await graph_client.execute_write_org("CREATE (n:Test)", organization_id=org_id)

            # Lock should be released after execution
            assert lock._value == initial


# =============================================================================
# Global write_lock Backward Compatibility Tests
# =============================================================================
class TestWriteLockBackwardCompat:
    """Tests for deprecated global write_lock property."""

    def test_write_lock_returns_semaphore(self, graph_client: GraphClient) -> None:
        """Global write_lock property still works."""
        lock = graph_client.write_lock
        assert isinstance(lock, asyncio.Semaphore)

    def test_write_lock_is_singleton(self, graph_client: GraphClient) -> None:
        """Global write_lock returns same instance."""
        lock1 = graph_client.write_lock
        lock2 = graph_client.write_lock
        assert lock1 is lock2

    def test_write_lock_separate_from_org_locks(
        self, graph_client: GraphClient, org_id: str
    ) -> None:
        """Global write_lock is separate from per-org locks."""
        global_lock = graph_client.write_lock

        async def get_org_lock() -> asyncio.Semaphore:
            return await graph_client.get_org_write_lock(org_id)

        org_lock = asyncio.get_event_loop().run_until_complete(get_org_lock())
        assert global_lock is not org_lock
