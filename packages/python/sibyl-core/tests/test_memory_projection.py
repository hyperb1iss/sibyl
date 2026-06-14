from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from sibyl_core.models.entities import Entity, EntityType, RelationshipType
from sibyl_core.models.memory_extraction import ExtractedMemoryEntity
from sibyl_core.projection import (
    extract_projected_memory_entities,
    extract_projected_memory_facts,
    project_extracted_memory_entities,
    project_memory_entities,
    project_memory_entity,
)


class HnswResolutionClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def execute_query(self, query: str, **params: object) -> list[dict[str, object]]:
        self.calls.append((query, params))
        return [
            {
                "status": "OK",
                "result": [
                    {
                        "seed_id": params["seed_id_0"],
                        "uuid": "topic_existing_samsung",
                        "name": "Samsung Television",
                        "entity_type": "topic",
                        "score": 0.98,
                    }
                ],
            }
        ]


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


def _document(
    content: str,
    *,
    entity_id: str = "document_source",
    metadata: dict[str, object] | None = None,
) -> Entity:
    merged_metadata = {"raw_memory_id": "raw-1", **(metadata or {})}
    return Entity(
        id=entity_id,
        entity_type=EntityType.DOCUMENT,
        name="Imported document",
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


def test_extract_projected_memory_facts_materializes_typed_evidence() -> None:
    source = _session(
        "I've been watching documentaries lately, especially on Netflix.",
        metadata={"valid_at": "2026/01/08 09:00"},
    )

    facts = extract_projected_memory_facts(source)

    assert facts
    fact = facts[0]
    assert fact.entity_type == EntityType.EVENT
    assert {"service", "media"} <= set(fact.categories)
    assert "recency" in fact.relations
    assert "Netflix" in fact.span
    assert "Valid at: 2026/01/08 09:00" in fact.content


def test_extract_projected_memory_facts_uses_fact_confidence_floor() -> None:
    source = _session("Watching documentaries on Netflix can be relaxing.")

    assert not extract_projected_memory_facts(source)


@pytest.mark.asyncio
async def test_project_memory_entity_creates_projected_entities_and_mentions() -> None:
    source = _session(
        "I bought a Samsung TV for the den.",
        metadata={"source_id": "raw-session-source"},
    )
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
    assert result.relationships >= 1

    entity_manager.create_direct_bulk.assert_awaited_once()
    created_entities = entity_manager.create_direct_bulk.await_args.args[0]
    assert created_entities[0].entity_type == EntityType.TOPIC
    assert "I bought a Samsung TV" in created_entities[0].content
    fact_entities = [
        entity
        for entity in created_entities
        if entity.metadata.get("category") == "memory_fact_projection"
    ]
    assert fact_entities
    assert fact_entities[0].entity_type == EntityType.EVENT
    assert fact_entities[0].metadata["projection_kind"] == "memory_fact"
    assert fact_entities[0].metadata["valid_at"] == "2026/01/03 12:00"
    assert result.created_projected_entities
    assert result.created_projection_relationships
    assert fact_entities[0].metadata["source_id"] == "raw-session-source"
    assert fact_entities[0].metadata["source_entity_id"] == source.id

    relationship_manager.create_bulk.assert_awaited_once()
    relationships = relationship_manager.create_bulk.await_args.args[0]
    assert all(
        relationship.relationship_type == RelationshipType.MENTIONS
        for relationship in relationships
    )
    assert all(relationship.source_id == source.id for relationship in relationships)
    assert all(relationship.metadata["auto_projected"] is True for relationship in relationships)
    assert all(
        relationship.metadata["valid_at"] == "2026/01/03 12:00" for relationship in relationships
    )


@pytest.mark.asyncio
async def test_project_memory_entity_defers_projection_relationship_embeddings() -> None:
    source = _session("I bought a Samsung TV for the den.")
    entity_manager = SimpleNamespace(
        create_direct_bulk=AsyncMock(
            side_effect=lambda entities, **_: [entity.id for entity in entities]
        )
    )
    relationship_manager = SimpleNamespace(
        create_direct_bulk=AsyncMock(
            side_effect=lambda relationships, **_: [
                relationship.id for relationship in relationships
            ]
        ),
        create_bulk=AsyncMock(return_value=(0, 0)),
    )

    result = await project_memory_entity(
        entity_manager=entity_manager,
        relationship_manager=relationship_manager,
        source=source,
        group_id="org-123",
        generate_embeddings=False,
    )

    assert result.created_projected_entities
    assert result.created_projection_relationships
    assert entity_manager.create_direct_bulk.await_args.kwargs["generate_embeddings"] is False
    assert relationship_manager.create_direct_bulk.await_args.kwargs["generate_embeddings"] is False
    relationship_manager.create_bulk.assert_not_awaited()


@pytest.mark.asyncio
async def test_project_memory_entity_marks_partial_relationship_writes() -> None:
    source = _session("I bought a Samsung TV for the den.")
    entity_manager = SimpleNamespace(
        create_direct_bulk=AsyncMock(
            side_effect=lambda entities, **_: [entity.id for entity in entities]
        )
    )
    relationship_manager = SimpleNamespace(create_bulk=AsyncMock(return_value=(1, 1)))

    result = await project_memory_entity(
        entity_manager=entity_manager,
        relationship_manager=relationship_manager,
        source=source,
        group_id="org-123",
        generate_embeddings=False,
    )

    assert result.relationships == 1
    assert result.projection_state == "partial"
    assert result.errors == ("1 projection relationships failed",)


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
    assert result.projected_entities >= 1
    assert result.relationships >= 2

    created_entities = entity_manager.create_direct_bulk.await_args.args[0]
    topic_entities = [
        entity
        for entity in created_entities
        if entity.metadata.get("category") == "memory_projection"
    ]
    assert [entity.name for entity in topic_entities] == ["Samsung TV"]


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
    assert (
        relationships[0].metadata["fact"].endswith("SurrealDB: SurrealDB 3.0 RRF helped retrieval.")
    )


@pytest.mark.asyncio
async def test_project_extracted_memory_entities_resolves_hnsw_duplicate_target() -> None:
    source = _session("I bought a Samsung TV for the den.")
    client = HnswResolutionClient()

    async def prepare_entities(
        entities: list[Entity],
        *,
        generate_embeddings: bool,
    ) -> list[Entity]:
        assert generate_embeddings is True
        return [entity.model_copy(update={"embedding": [1.0, 0.0]}) for entity in entities]

    entity_manager = SimpleNamespace(
        _client=client,
        _group_id="org-123",
        prepare_entities_for_write=prepare_entities,
        create_direct_bulk=AsyncMock(return_value=[]),
    )
    relationship_manager = SimpleNamespace(create_bulk=AsyncMock(return_value=(1, 0)))

    result = await project_extracted_memory_entities(
        entity_manager=entity_manager,
        relationship_manager=relationship_manager,
        sources=[source],
        group_id="org-123",
        extractions_by_source_id={
            source.id: [
                ExtractedMemoryEntity(
                    name="Samsung TV",
                    entity_type="topic",
                    summary="A television in the den",
                    evidence="I bought a Samsung TV for the den.",
                    confidence=0.92,
                )
            ]
        },
        generate_embeddings=True,
    )

    assert result.projected_entities == 0
    assert result.relationships == 1
    entity_manager.create_direct_bulk.assert_not_awaited()
    assert client.calls
    query, params = client.calls[0]
    assert query.count("name_embedding <|") == 1
    assert "attributes.memory_scope = NONE" in query
    assert "attributes.project_id = NONE" in query
    assert params["seed_embedding_0"] == [1.0, 0.0]

    relationships = relationship_manager.create_bulk.await_args.args[0]
    assert relationships[0].target_id == "topic_existing_samsung"


@pytest.mark.asyncio
async def test_project_extracted_memory_entities_does_not_resolve_across_private_scopes() -> None:
    source = _document(
        "The Apollo dossier mentions Samsung TV preferences.",
        metadata={"memory_scope": "private", "principal_id": "attacker-user"},
    )

    class ScopedHnswClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, object]]] = []

        async def execute_query(self, query: str, **params: object) -> list[dict[str, object]]:
            self.calls.append((query, params))
            if params.get("scope_0_principal_id") == "attacker-user":
                return [{"status": "OK", "result": []}]
            return [
                {
                    "status": "OK",
                    "result": [
                        {
                            "seed_id": params["seed_id_0"],
                            "uuid": "victim_private_projected",
                            "name": "Samsung TV",
                            "entity_type": "topic",
                            "score": 0.99,
                        }
                    ],
                }
            ]

    client = ScopedHnswClient()

    async def prepare_entities(
        entities: list[Entity],
        *,
        generate_embeddings: bool,
    ) -> list[Entity]:
        assert generate_embeddings is True
        return [entity.model_copy(update={"embedding": [1.0, 0.0]}) for entity in entities]

    entity_manager = SimpleNamespace(
        _client=client,
        _group_id="org-123",
        prepare_entities_for_write=prepare_entities,
        create_direct_bulk=AsyncMock(
            side_effect=lambda entities, **_: [entity.id for entity in entities]
        ),
    )
    relationship_manager = SimpleNamespace(create_bulk=AsyncMock(return_value=(1, 0)))

    result = await project_extracted_memory_entities(
        entity_manager=entity_manager,
        relationship_manager=relationship_manager,
        sources=[source],
        group_id="org-123",
        extractions_by_source_id={
            source.id: [
                ExtractedMemoryEntity(
                    name="Samsung TV",
                    entity_type="topic",
                    summary="A television in a private dossier",
                    evidence="The Apollo dossier mentions Samsung TV preferences.",
                    confidence=0.92,
                )
            ]
        },
        generate_embeddings=True,
    )

    assert result.projected_entities == 1
    entity_manager.create_direct_bulk.assert_awaited_once()
    query, params = client.calls[0]
    assert "attributes.memory_scope = $scope_0_memory_scope" in query
    assert "attributes.principal_id = $scope_0_principal_id" in query
    assert params["scope_0_memory_scope"] == "private"
    assert params["scope_0_principal_id"] == "attacker-user"

    relationships = relationship_manager.create_bulk.await_args.args[0]
    assert relationships[0].target_id != "victim_private_projected"


@pytest.mark.asyncio
async def test_project_extracted_memory_entities_accepts_imported_documents() -> None:
    source = _document(
        "An imported email says SurrealDB powers the memory graph.",
        metadata={"memory_scope": "private", "principal_id": "user-1"},
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
        created_source_ids=["raw-doc"],
        extractions_by_source_id={
            "raw-doc": [
                ExtractedMemoryEntity(
                    name="SurrealDB",
                    entity_type="tool",
                    summary="Native graph database",
                    evidence="SurrealDB powers the memory graph.",
                    confidence=0.9,
                )
            ]
        },
        generate_embeddings=False,
    )

    assert result.extracted == 1
    assert result.projected_entities == 1
    assert result.relationships == 1
    created_entities = entity_manager.create_direct_bulk.await_args.args[0]
    assert created_entities[0].metadata["source_entity_type"] == "document"
    assert created_entities[0].metadata["principal_id"] == "user-1"
    relationships = relationship_manager.create_bulk.await_args.args[0]
    assert relationships[0].source_id == "raw-doc"


@pytest.mark.asyncio
async def test_project_extracted_memory_entities_scopes_private_documents_by_principal() -> None:
    sources = [
        _document(
            "An imported email says SurrealDB powers the memory graph.",
            entity_id="document-user-1",
            metadata={
                "raw_memory_id": "raw-1",
                "memory_scope": "private",
                "principal_id": "user-1",
            },
        ),
        _document(
            "Another imported email says SurrealDB powers the memory graph.",
            entity_id="document-user-2",
            metadata={
                "raw_memory_id": "raw-2",
                "memory_scope": "private",
                "principal_id": "user-2",
            },
        ),
    ]
    entity_manager = SimpleNamespace(
        create_direct_bulk=AsyncMock(
            side_effect=lambda entities, **_: [entity.id for entity in entities]
        )
    )
    relationship_manager = SimpleNamespace(
        create_bulk=AsyncMock(side_effect=lambda relationships: (len(relationships), 0))
    )

    result = await project_extracted_memory_entities(
        entity_manager=entity_manager,
        relationship_manager=relationship_manager,
        sources=sources,
        group_id="org-123",
        created_source_ids=["raw-doc-1", "raw-doc-2"],
        extractions_by_source_id={
            "raw-doc-1": [
                ExtractedMemoryEntity(
                    name="SurrealDB",
                    entity_type="tool",
                    summary="Native graph database",
                    evidence="SurrealDB powers the memory graph.",
                    confidence=0.9,
                )
            ],
            "raw-doc-2": [
                ExtractedMemoryEntity(
                    name="SurrealDB",
                    entity_type="tool",
                    summary="Native graph database",
                    evidence="SurrealDB powers the memory graph.",
                    confidence=0.9,
                )
            ],
        },
        generate_embeddings=False,
    )

    assert result.projected_entities == 2
    assert result.relationships == 2
    created_entities = entity_manager.create_direct_bulk.await_args.args[0]
    assert len({entity.id for entity in created_entities}) == 2
    assert {entity.metadata["principal_id"] for entity in created_entities} == {
        "user-1",
        "user-2",
    }


@pytest.mark.asyncio
async def test_project_memory_entities_skips_private_scope() -> None:
    source = _session(
        "My private notes mention Samsung TV purchase details.",
        metadata={"memory_scope": "private", "principal_id": "user-1"},
    )
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

    assert result.skipped is True
    assert result.extracted == 0
    assert result.projected_entities == 0
    assert result.relationships == 0
    entity_manager.create_direct_bulk.assert_not_awaited()
    relationship_manager.create_bulk.assert_not_awaited()
