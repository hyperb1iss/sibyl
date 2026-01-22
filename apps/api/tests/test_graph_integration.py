"""Integration tests against FalkorDB/Graphiti for entity and relationship handling."""

import uuid
from collections.abc import AsyncGenerator

import pytest

from sibyl.config import settings
from sibyl_core.errors import GraphConnectionError
from sibyl_core.graph.client import GraphClient, get_graph_client, reset_graph_client
from sibyl_core.graph.entities import EntityManager
from sibyl_core.graph.relationships import RelationshipManager
from sibyl_core.models.entities import EntityType, Pattern, Relationship, RelationshipType


@pytest.fixture(scope="module")
def test_org_id() -> str:
    """Generate a unique test organization ID for this module."""
    return f"test_org_{uuid.uuid4().hex[:8]}"


@pytest.fixture(scope="module")
async def graph_client_with_cleanup(test_org_id: str) -> AsyncGenerator[GraphClient]:
    """Get graph client and clean up the test graph after all tests complete."""
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
        pytest.skip("FalkorDB not available for integration test")
        return

    # Cleanup: delete the entire test graph after all tests in module complete
    import contextlib

    with contextlib.suppress(Exception):
        import redis.asyncio as redis

        r = redis.Redis(
            host=settings.falkordb_host,
            port=settings.falkordb_port,
            password=settings.falkordb_password or None,
            decode_responses=True,
        )
        await r.execute_command("GRAPH.DELETE", test_org_id)
        await r.aclose()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_entity_create_get_delete_preserves_id(
    graph_client_with_cleanup: GraphClient, test_org_id: str
) -> None:
    """EntityManager should persist caller-provided IDs and allow CRUD.

    Uses create_direct() for structured data where we need to preserve
    IDs, entity types, and custom metadata. The regular create() method
    uses LLM-powered ingestion which may transform data.
    """
    manager = EntityManager(graph_client_with_cleanup, group_id=test_org_id)

    entity_id = f"test_pattern_{uuid.uuid4().hex[:8]}"
    pattern = Pattern(
        id=entity_id,
        entity_type=EntityType.PATTERN,
        name="Integration Pattern",
        description="Integration test pattern",
        content="Integration test content",
        category="integration",
        languages=["python"],
    )

    # Use create_direct for structured data with preserved metadata
    created_id = await manager.create_direct(pattern)
    assert created_id == entity_id

    fetched = await manager.get(entity_id)
    assert fetched.id == entity_id
    assert fetched.name == pattern.name
    assert fetched.metadata.get("category") == "integration"

    # Cleanup
    deleted = await manager.delete(entity_id)
    assert deleted is True


@pytest.mark.asyncio
@pytest.mark.integration
async def test_relationship_dedup_and_delete(
    graph_client_with_cleanup: GraphClient, test_org_id: str
) -> None:
    """RelationshipManager should deduplicate by (source, target, type) and delete by relationship_id.

    Uses create_direct() for structured data where we need to preserve entity IDs
    for reliable relationship creation and testing.
    """
    entity_manager = EntityManager(graph_client_with_cleanup, group_id=test_org_id)
    rel_manager = RelationshipManager(graph_client_with_cleanup, group_id=test_org_id)

    # Create two entities to relate
    src_id = f"test_rel_src_{uuid.uuid4().hex[:6]}"
    tgt_id = f"test_rel_tgt_{uuid.uuid4().hex[:6]}"
    src = Pattern(
        id=src_id,
        entity_type=EntityType.PATTERN,
        name="Rel Source",
        description="src",
        content="src content",
    )
    tgt = Pattern(
        id=tgt_id,
        entity_type=EntityType.PATTERN,
        name="Rel Target",
        description="tgt",
        content="tgt content",
    )
    # Use create_direct for structured data with preserved IDs
    await entity_manager.create_direct(src)
    await entity_manager.create_direct(tgt)

    rel_id = f"rel_{uuid.uuid4().hex[:10]}"
    relationship = Relationship(
        id=rel_id,
        relationship_type=RelationshipType.RELATED_TO,
        source_id=src_id,
        target_id=tgt_id,
        weight=1.0,
        metadata={"test": True},
    )

    first = await rel_manager.create(relationship)
    second = await rel_manager.create(relationship)  # Should dedupe
    assert first == rel_id
    assert second == rel_id

    rels = await rel_manager.get_for_entity(
        src_id, relationship_types=[RelationshipType.RELATED_TO]
    )
    assert any(
        r.relationship_type == RelationshipType.RELATED_TO and r.target_id == tgt_id for r in rels
    )

    await rel_manager.delete(rel_id)
    rels_after = await rel_manager.get_for_entity(
        src_id, relationship_types=[RelationshipType.RELATED_TO]
    )
    assert all(r.target_id != tgt_id for r in rels_after)

    # Cleanup entities
    await entity_manager.delete(src_id)
    await entity_manager.delete(tgt_id)
