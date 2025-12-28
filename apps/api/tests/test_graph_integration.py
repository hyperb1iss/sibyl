"""Integration tests against FalkorDB/Graphiti for entity and relationship handling."""

import uuid

import pytest

from sibyl.config import settings
from sibyl.errors import GraphConnectionError
from sibyl.graph.client import get_graph_client, reset_graph_client
from sibyl.graph.entities import EntityManager
from sibyl.graph.relationships import RelationshipManager
from sibyl.models.entities import EntityType, Pattern, Relationship, RelationshipType

# Test organization ID for multi-tenancy
TEST_ORG_ID = f"test_org_{uuid.uuid4().hex[:8]}"


async def _ensure_graph_client():
    """Reset and obtain a live graph client or skip if unavailable."""
    # Require OpenAI API key for Graphiti ingestion; skip if not configured
    if not settings.openai_api_key.get_secret_value():
        pytest.skip("SIBYL_OPENAI_API_KEY not set; skipping live graph integration")
    try:
        await reset_graph_client()
    except RuntimeError:
        # Event loop closed from prior run; ignore and attempt fresh client
        pass
    try:
        return await get_graph_client()
    except GraphConnectionError:
        pytest.skip("FalkorDB not available for integration test")


@pytest.mark.asyncio
@pytest.mark.integration
async def test_entity_create_get_delete_preserves_id() -> None:
    """EntityManager should persist caller-provided IDs and allow CRUD.

    Uses create_direct() for structured data where we need to preserve
    IDs, entity types, and custom metadata. The regular create() method
    uses LLM-powered ingestion which may transform data.
    """
    client = await _ensure_graph_client()
    manager = EntityManager(client, group_id=TEST_ORG_ID)

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
async def test_relationship_dedup_and_delete() -> None:
    """RelationshipManager should deduplicate by (source, target, type) and delete by relationship_id.

    Uses create_direct() for structured data where we need to preserve entity IDs
    for reliable relationship creation and testing.
    """
    client = await _ensure_graph_client()
    entity_manager = EntityManager(client, group_id=TEST_ORG_ID)
    rel_manager = RelationshipManager(client, group_id=TEST_ORG_ID)

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
