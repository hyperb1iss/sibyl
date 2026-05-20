from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from sibyl_core.models.entities import Entity, EntityType, RelationshipType
from sibyl_core.models.memory_extraction import ExtractedMemoryEntity
from sibyl_core.projection import (
    extract_projected_memory_entities,
    project_extracted_memory_entities,
    project_memory_entities,
    project_memory_entity,
)


def _session(
    content: str,
    *,
    entity_id: str = "session_source",
    metadata: dict[str, object] | None = None,
) -> Entity:
    merged_metadata = {"valid_at": "2026/01/03 12:00", **(metadata or {})}
    return Entity(
        id=entity_id,
        entity_type=EntityType.SESSION,
        name="Memory session",
        content=content,
        organization_id="org-123",
        metadata=merged_metadata,
    )


def test_extract_projected_memory_entities_finds_memory_handles() -> None:
    source = _session(
        "I bought a Samsung TV for the den. "
        "I prefer espresso beans from the market. "
        "My sister Maya visited yesterday."
    )

    projected = extract_projected_memory_entities(source, max_entities=8)
    names = {entity.name for entity in projected}

    assert "Samsung TV" in names
    assert "espresso beans" in names
    assert "Maya" in names


@pytest.mark.asyncio
async def test_project_memory_entity_creates_projected_entities_and_mentions() -> None:
    source = _session("I bought a Samsung TV for the den.")
    entity_manager = SimpleNamespace(
        create_direct_bulk=AsyncMock(
            side_effect=lambda entities, **_: [entity.id for entity in entities]
        )
    )
    relationship_manager = SimpleNamespace(create_bulk=AsyncMock(return_value=(1, 0)))

    result = await project_memory_entity(
        entity_manager=entity_manager,
        relationship_manager=relationship_manager,
        source=source,
        group_id="org-123",
        generate_embeddings=False,
    )

    assert result.extracted >= 1
    assert result.projected_entities >= 1
    assert result.relationships == 1

    entity_manager.create_direct_bulk.assert_awaited_once()
    created_entities = entity_manager.create_direct_bulk.await_args.args[0]
    assert created_entities[0].entity_type == EntityType.TOPIC
    assert "I bought a Samsung TV" in created_entities[0].content

    relationship_manager.create_bulk.assert_awaited_once()
    relationships = relationship_manager.create_bulk.await_args.args[0]
    assert relationships[0].relationship_type == RelationshipType.MENTIONS
    assert relationships[0].source_id == source.id
    assert relationships[0].metadata["auto_projected"] is True
    assert relationships[0].metadata["valid_at"] == "2026/01/03 12:00"


@pytest.mark.asyncio
async def test_project_memory_entities_batches_and_dedupes_targets() -> None:
    sources = [
        _session("I bought a Samsung TV for the den.", entity_id="session_one"),
        _session("I watched the Samsung TV yesterday.", entity_id="session_two"),
    ]
    entity_manager = SimpleNamespace(
        create_direct_bulk=AsyncMock(
            side_effect=lambda entities, **_: [entity.id for entity in entities]
        )
    )
    relationship_manager = SimpleNamespace(create_bulk=AsyncMock(return_value=(2, 0)))

    result = await project_memory_entities(
        entity_manager=entity_manager,
        relationship_manager=relationship_manager,
        sources=sources,
        group_id="org-123",
        generate_embeddings=False,
    )

    assert result.sources == 2
    assert result.projected_entities == 1
    assert result.relationships == 2

    created_entities = entity_manager.create_direct_bulk.await_args.args[0]
    assert [entity.name for entity in created_entities] == ["Samsung TV"]


@pytest.mark.asyncio
async def test_project_memory_entities_inherits_scope_metadata() -> None:
    source = _session(
        "I bought a Samsung TV for the den.",
        metadata={
            "project_id": "project-123",
            "memory_scope": "project",
            "scope_key": "project-123",
        },
    )
    entity_manager = SimpleNamespace(
        create_direct_bulk=AsyncMock(
            side_effect=lambda entities, **_: [entity.id for entity in entities]
        )
    )
    relationship_manager = SimpleNamespace(create_bulk=AsyncMock(return_value=(1, 0)))

    await project_memory_entity(
        entity_manager=entity_manager,
        relationship_manager=relationship_manager,
        source=source,
        group_id="org-123",
        generate_embeddings=False,
    )

    created_entities = entity_manager.create_direct_bulk.await_args.args[0]
    assert created_entities[0].metadata["project_id"] == "project-123"
    assert created_entities[0].metadata["memory_scope"] == "project"
    assert created_entities[0].metadata["scope_key"] == "project-123"

    relationships = relationship_manager.create_bulk.await_args.args[0]
    assert relationships[0].metadata["project_id"] == "project-123"
    assert relationships[0].metadata["memory_scope"] == "project"
    assert relationships[0].metadata["scope_key"] == "project-123"


@pytest.mark.asyncio
async def test_project_memory_entities_keeps_existing_projected_entity() -> None:
    source = _session("I bought a Samsung TV for the den.")
    entity_manager = SimpleNamespace(
        get=AsyncMock(
            return_value=Entity(
                id="topic_existing",
                entity_type=EntityType.TOPIC,
                name="Samsung TV",
            )
        ),
        create_direct_bulk=AsyncMock(return_value=[]),
    )
    relationship_manager = SimpleNamespace(create_bulk=AsyncMock(return_value=(1, 0)))

    result = await project_memory_entity(
        entity_manager=entity_manager,
        relationship_manager=relationship_manager,
        source=source,
        group_id="org-123",
        generate_embeddings=False,
    )

    assert result.projected_entities == 0
    assert result.relationships == 1
    entity_manager.create_direct_bulk.assert_not_awaited()
    relationship_manager.create_bulk.assert_awaited_once()


@pytest.mark.asyncio
async def test_project_extracted_memory_entities_materializes_llm_mentions() -> None:
    source = _session(
        "SurrealDB 3.0 RRF helped the LongMemEval retrieval gap.",
        metadata={
            "project_id": "project-123",
            "memory_scope": "project",
            "scope_key": "project-123",
        },
    )
    entity_manager = SimpleNamespace(
        create_direct_bulk=AsyncMock(
            side_effect=lambda entities, **_: [entity.id for entity in entities]
        )
    )
    relationship_manager = SimpleNamespace(create_bulk=AsyncMock(return_value=(1, 0)))

    result = await project_extracted_memory_entities(
        entity_manager=entity_manager,
        relationship_manager=relationship_manager,
        sources=[source],
        group_id="org-123",
        created_source_ids=["created-session"],
        extractions_by_source_id={
            "created-session": [
                ExtractedMemoryEntity(
                    name="SurrealDB",
                    entity_type="tool",
                    summary="Native graph database",
                    evidence="SurrealDB 3.0 RRF helped retrieval.",
                    confidence=0.92,
                )
            ]
        },
        generate_embeddings=False,
    )

    assert result.extracted == 1
    assert result.projected_entities == 1
    assert result.relationships == 1

    created_entities = entity_manager.create_direct_bulk.await_args.args[0]
    assert created_entities[0].entity_type == EntityType.TOOL
    assert created_entities[0].metadata["projection_extractor"] == "llm"
    assert created_entities[0].metadata["projection_kind"] == "llm_mention"
    assert created_entities[0].metadata["source_entity_id"] == "created-session"
    assert created_entities[0].metadata["scope_key"] == "project-123"

    relationships = relationship_manager.create_bulk.await_args.args[0]
    assert relationships[0].relationship_type == RelationshipType.MENTIONS
    assert relationships[0].source_id == "created-session"
    assert relationships[0].metadata["projection_extractor"] == "llm"
    assert relationships[0].metadata["fact"].endswith(
        "SurrealDB: SurrealDB 3.0 RRF helped retrieval."
    )
