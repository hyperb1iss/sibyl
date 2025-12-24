"""Stress tests for concurrent FalkorDB write operations.

FalkorDB can crash or corrupt connections with concurrent writes on a single
connection. These tests verify that the write semaphore properly serializes
writes and prevents connection corruption.
"""

import asyncio
import contextlib
import uuid
from collections.abc import AsyncGenerator

import pytest

from sibyl.config import settings
from sibyl.errors import GraphConnectionError
from sibyl.graph.client import GraphClient, get_graph_client, reset_graph_client
from sibyl.graph.entities import EntityManager
from sibyl.models.entities import EntityType, Pattern


@pytest.fixture
async def graph_client() -> AsyncGenerator[GraphClient]:
    """Get a live graph client or skip if unavailable."""
    if not settings.openai_api_key.get_secret_value():
        pytest.skip("SIBYL_OPENAI_API_KEY not set; skipping live graph integration")

    try:
        await reset_graph_client()
    except RuntimeError:
        pass

    try:
        client = await get_graph_client()
        yield client
    except GraphConnectionError:
        pytest.skip("FalkorDB not available for stress test")


@pytest.fixture
def test_group_id() -> str:
    """Generate a unique group ID for test isolation."""
    return f"stress_test_{uuid.uuid4().hex[:8]}"


def _create_test_pattern(index: int, group_id: str) -> Pattern:
    """Create a test pattern entity."""
    return Pattern(
        id=f"stress_{group_id}_{index}",
        entity_type=EntityType.PATTERN,
        name=f"Stress Test Pattern {index}",
        description=f"Pattern created during stress test iteration {index}",
        content=f"Content for pattern {index} in stress test",
        category="stress-test",
        languages=["python"],
    )


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.stress
async def test_concurrent_entity_creates(graph_client: GraphClient, test_group_id: str) -> None:
    """Test many concurrent entity creations don't corrupt the connection.

    Creates 20 entities concurrently - this would crash FalkorDB without
    proper write serialization.
    """
    manager = EntityManager(graph_client, group_id=test_group_id)
    entity_count = 20
    created_ids: list[str] = []

    async def create_entity(index: int) -> str:
        pattern = _create_test_pattern(index, test_group_id)
        return await manager.create_direct(pattern)

    # Launch all creates concurrently
    tasks = [create_entity(i) for i in range(entity_count)]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    # Check results - all should succeed
    errors = [r for r in results if isinstance(r, Exception)]
    successes = [r for r in results if isinstance(r, str)]
    created_ids.extend(successes)

    # Allow some failures but most should succeed
    assert len(errors) == 0, f"Got {len(errors)} errors: {errors[:3]}"
    assert len(successes) == entity_count

    # Verify entities were actually created
    for entity_id in created_ids[:5]:  # Spot check first 5
        entity = await manager.get(entity_id)
        assert entity is not None
        assert entity.id == entity_id

    # Cleanup - ignore errors on teardown
    for entity_id in created_ids:
        with contextlib.suppress(Exception):
            await manager.delete(entity_id)


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.stress
async def test_concurrent_updates(graph_client: GraphClient, test_group_id: str) -> None:
    """Test concurrent updates to different entities don't corrupt data.

    Creates entities first, then updates them all concurrently.
    """
    manager = EntityManager(graph_client, group_id=test_group_id)
    entity_count = 15
    entity_ids: list[str] = []

    # Create entities first (sequentially to ensure they exist)
    for i in range(entity_count):
        pattern = _create_test_pattern(i, test_group_id)
        entity_id = await manager.create_direct(pattern)
        entity_ids.append(entity_id)

    async def update_entity(entity_id: str, iteration: int) -> bool:
        updates = {
            "description": f"Updated in iteration {iteration}",
            "category": f"updated-{iteration}",
        }
        result = await manager.update(entity_id, updates)
        return result is not None  # update() returns Entity | None

    # Launch all updates concurrently
    tasks = [update_entity(eid, i) for i, eid in enumerate(entity_ids)]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    errors = [r for r in results if isinstance(r, Exception)]
    successes = [r for r in results if r is True]

    assert len(errors) == 0, f"Got {len(errors)} errors: {errors[:3]}"
    assert len(successes) == entity_count

    # Verify updates were applied
    for i, entity_id in enumerate(entity_ids[:5]):
        entity = await manager.get(entity_id)
        assert entity.metadata.get("category") == f"updated-{i}"

    # Cleanup - ignore errors on teardown
    for entity_id in entity_ids:
        with contextlib.suppress(Exception):
            await manager.delete(entity_id)


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.stress
async def test_mixed_concurrent_operations(graph_client: GraphClient, test_group_id: str) -> None:
    """Test mixed creates, updates, and deletes running concurrently.

    This simulates real-world load with different operation types.
    """
    manager = EntityManager(graph_client, group_id=test_group_id)

    # Pre-create some entities for updates and deletes
    pre_created_ids: list[str] = []
    for i in range(10):
        pattern = _create_test_pattern(1000 + i, test_group_id)
        entity_id = await manager.create_direct(pattern)
        pre_created_ids.append(entity_id)

    operations_completed = {"create": 0, "update": 0, "delete": 0}
    new_entity_ids: list[str] = []

    async def create_op(index: int) -> None:
        pattern = _create_test_pattern(2000 + index, test_group_id)
        entity_id = await manager.create_direct(pattern)
        new_entity_ids.append(entity_id)
        operations_completed["create"] += 1

    async def update_op(entity_id: str) -> None:
        await manager.update(entity_id, {"description": "Mixed test update"})
        operations_completed["update"] += 1

    async def delete_op(entity_id: str) -> None:
        await manager.delete(entity_id)
        operations_completed["delete"] += 1

    # Build mixed operation list
    tasks = []
    for i in range(10):
        tasks.append(create_op(i))
    for eid in pre_created_ids[:5]:
        tasks.append(update_op(eid))
    for eid in pre_created_ids[5:]:
        tasks.append(delete_op(eid))

    # Shuffle to interleave operation types
    import random

    random.shuffle(tasks)

    # Execute all concurrently
    results = await asyncio.gather(*tasks, return_exceptions=True)

    errors = [r for r in results if isinstance(r, Exception)]
    assert len(errors) == 0, f"Got {len(errors)} errors: {errors[:3]}"

    # Verify operation counts
    assert operations_completed["create"] == 10
    assert operations_completed["update"] == 5
    assert operations_completed["delete"] == 5

    # Cleanup remaining entities - ignore errors on teardown
    for entity_id in new_entity_ids + pre_created_ids[:5]:
        with contextlib.suppress(Exception):
            await manager.delete(entity_id)


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.stress
async def test_rapid_sequential_writes(graph_client: GraphClient, test_group_id: str) -> None:
    """Test rapid sequential writes don't exhaust connections.

    Even without concurrency, rapid writes can cause issues if connections
    aren't properly managed.
    """
    manager = EntityManager(graph_client, group_id=test_group_id)
    entity_ids: list[str] = []

    # Create 50 entities as fast as possible
    for i in range(50):
        pattern = _create_test_pattern(3000 + i, test_group_id)
        entity_id = await manager.create_direct(pattern)
        entity_ids.append(entity_id)
        # Immediately update
        await manager.update(entity_id, {"description": f"Rapid update {i}"})

    # All should succeed
    assert len(entity_ids) == 50

    # Cleanup - ignore errors on teardown
    for entity_id in entity_ids:
        with contextlib.suppress(Exception):
            await manager.delete(entity_id)


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.stress
async def test_connection_recovery_after_heavy_load(
    graph_client: GraphClient, test_group_id: str
) -> None:
    """Test the connection remains usable after heavy concurrent load."""
    manager = EntityManager(graph_client, group_id=test_group_id)
    entity_ids: list[str] = []

    # First, hammer with concurrent creates
    async def create_many(index: int) -> str:
        pattern = _create_test_pattern(4000 + index, test_group_id)
        return await manager.create_direct(pattern)

    tasks = [create_many(i) for i in range(25)]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    entity_ids.extend([r for r in results if isinstance(r, str)])

    # Wait a moment
    await asyncio.sleep(0.1)

    # Now verify the connection still works with simple operations
    test_pattern = _create_test_pattern(9999, test_group_id)
    final_id = await manager.create_direct(test_pattern)
    assert final_id == test_pattern.id

    fetched = await manager.get(final_id)
    assert fetched is not None
    assert fetched.name == test_pattern.name

    # Cleanup - ignore errors on teardown
    entity_ids.append(final_id)
    for entity_id in entity_ids:
        with contextlib.suppress(Exception):
            await manager.delete(entity_id)
