from __future__ import annotations

import builtins
import json
from datetime import UTC, datetime
from typing import Any, cast

import pytest

from sibyl_core.backends.surreal.schema import EMBEDDING_DIM
from sibyl_core.embeddings.native import (
    DeterministicNativeEmbeddingProvider,
    NativeEmbeddingMetadata,
)
from sibyl_core.models.entities import Entity, EntityType, Procedure, Relationship, RelationshipType
from sibyl_core.retrieval.dedup import EntityDeduplicator
from sibyl_core.services.native_graph import (
    NativeEntityManager,
    NativeRelationshipManager,
    NativeSurrealGraphClient,
    _validate_native_embedding_dimensions,
    entity_from_surreal_row,
    normalize_records,
    prepare_native_graph_schema,
    relationship_from_surreal_row,
)


def _block_graphiti_imports(monkeypatch: pytest.MonkeyPatch) -> None:
    real_import = builtins.__import__

    def guarded_import(
        name: str,
        globals: dict[str, object] | None = None,
        locals: dict[str, object] | None = None,
        fromlist: tuple[str, ...] = (),
        level: int = 0,
    ) -> Any:
        if name == "graphiti_core" or name.startswith("graphiti_core."):
            raise AssertionError(f"Graphiti import forbidden: {name}")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", guarded_import)


class _EmbeddingWriteClient:
    group_id = "org-native"

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def execute_query(self, query: str, **params: object) -> list[dict[str, object]]:
        self.calls.append((query, params))
        if "SELECT id AS record_id FROM entity" in query:
            return [{"record_id": f"entity:{params['uuid']}"}]
        if "UPSERT entity" in query:
            return [{"uuid": params["uuid"], "name_embedding": params["name_embedding"]}]
        if "RELATE $src->$rel->$tgt" in query:
            return [{"uuid": params["uuid"], "fact_embedding": params["fact_embedding"]}]
        return []


def _deterministic_provider() -> DeterministicNativeEmbeddingProvider:
    return DeterministicNativeEmbeddingProvider(
        NativeEmbeddingMetadata(
            provider="deterministic",
            model="unit-test",
            dimensions=4,
            cache_namespace="native-graph-test",
            tokenizer_estimate_method="utf8-byte-length",
        )
    )


def test_native_embedding_dimension_validation_requires_schema_match() -> None:
    _validate_native_embedding_dimensions(
        DeterministicNativeEmbeddingProvider(
            NativeEmbeddingMetadata(
                provider="deterministic",
                model="unit-test",
                dimensions=EMBEDDING_DIM,
                cache_namespace="native-graph-test",
                tokenizer_estimate_method="utf8-byte-length",
            )
        )
    )
    with pytest.raises(ValueError, match="must match Surreal graph schema"):
        _validate_native_embedding_dimensions(
            DeterministicNativeEmbeddingProvider(
                NativeEmbeddingMetadata(
                    provider="deterministic",
                    model="unit-test",
                    dimensions=EMBEDDING_DIM + 1,
                    cache_namespace="native-graph-test",
                    tokenizer_estimate_method="utf8-byte-length",
                )
            )
        )


def test_entity_from_surreal_row_preserves_native_policy_metadata() -> None:
    entity = entity_from_surreal_row(
        {
            "id": "entity:procedure_native",
            "uuid": "procedure_native",
            "name": "Native Procedure",
            "entity_type": "procedure",
            "description": "Top-level description",
            "content": "Top-level content",
            "group_id": "org-native",
            "created_by": "stef",
            "modified_by": "nova",
            "project_id": "project_native",
            "source_id": "raw_1",
            "source_ids": ["raw_1", "raw_2"],
            "confidence": 0.94,
            "valid_at": "2026-05-13T12:00:00+00:00",
            "valid_from": "2026-05-13T12:00:00+00:00",
            "invalid_at": "2026-05-14T12:00:00+00:00",
            "tags": ["native", "procedure"],
            "attributes": {
                "metadata": json.dumps(
                    {
                        "category": None,
                        "required_tools": ["moon"],
                        "steps": [
                            {
                                "order": 1,
                                "title": "Verify",
                                "success_criteria": "Targeted tests pass",
                            }
                        ],
                    }
                ),
                "source_file": "docs/native.md",
            },
            "created_at": "2026-05-13T12:00:00+00:00",
            "updated_at": "2026-05-13T12:30:00+00:00",
            "name_embedding": [0.5] * EMBEDDING_DIM,
        }
    )

    assert isinstance(entity, Procedure)
    assert entity.id == "procedure_native"
    assert entity.category == ""
    assert entity.required_tools == ["moon"]
    assert entity.steps[0].title == "Verify"
    assert entity.created_by == "stef"
    assert entity.modified_by == "nova"
    assert entity.source_file == "docs/native.md"
    assert entity.embedding == [0.5] * EMBEDDING_DIM
    assert entity.metadata["project_id"] == "project_native"
    assert entity.metadata["source_id"] == "raw_1"
    assert entity.metadata["source_ids"] == ["raw_1", "raw_2"]
    assert entity.metadata["confidence"] == 0.94
    assert entity.metadata["valid_at"] == "2026-05-13T12:00:00+00:00"
    assert entity.metadata["valid_from"] == "2026-05-13T12:00:00+00:00"
    assert entity.metadata["invalid_at"] == "2026-05-14T12:00:00+00:00"
    assert entity.metadata["record_id"] == "entity:procedure_native"
    assert "category" not in entity.metadata


@pytest.mark.asyncio
async def test_native_entity_manager_generates_embeddings_with_native_provider() -> None:
    client = _EmbeddingWriteClient()
    provider = _deterministic_provider()
    manager = NativeEntityManager(
        cast(NativeSurrealGraphClient, client),
        group_id=client.group_id,
        embedding_provider=provider,
    )

    created_id = await manager.create_direct(
        Entity(
            id="entity_embed",
            entity_type=EntityType.PATTERN,
            name="Native Embedding Pattern",
            description="Generated without Graphiti.",
            organization_id=client.group_id,
        ),
        generate_embedding=True,
    )

    assert created_id == "entity_embed"
    write_params = client.calls[0][1]
    assert len(cast(list[float], write_params["name_embedding"])) == 4
    attributes = cast(dict[str, object], write_params["attributes"])
    assert attributes["embedding_metadata"] == provider.metadata.to_dict()


@pytest.mark.asyncio
async def test_native_relationship_manager_generates_fact_embeddings() -> None:
    client = _EmbeddingWriteClient()
    provider = _deterministic_provider()
    manager = NativeRelationshipManager(
        cast(NativeSurrealGraphClient, client),
        group_id=client.group_id,
        embedding_provider=provider,
    )

    created_id = await manager.create(
        Relationship(
            id="rel_embed",
            source_id="source_entity",
            target_id="target_entity",
            relationship_type=RelationshipType.RELATED_TO,
            metadata={
                "fact": "Source relates to target through native embeddings.",
                "fact_embedding": [],
            },
        )
    )

    assert created_id == "rel_embed"
    write_params = client.calls[2][1]
    assert len(cast(list[float], write_params["fact_embedding"])) == 4
    attributes = cast(dict[str, object], write_params["attributes"])
    assert attributes["embedding_metadata"] == provider.metadata.to_dict()
    assert "fact_embedding" not in attributes


def test_entity_from_surreal_row_hydrates_legacy_shaped_rows() -> None:
    entity = entity_from_surreal_row(
        {
            "id": "legacy_procedure",
            "name": "Legacy Procedure",
            "labels": ["Entity", "Procedure"],
            "summary": "Legacy summary",
            "attributes": {
                "metadata": {
                    "category": None,
                    "project_id": "project_legacy",
                    "source_ids": ["graphiti:episode_1"],
                },
                "created_by": "legacy-agent",
            },
            "created_at": "2026-05-13T12:00:00+00:00",
        }
    )

    assert isinstance(entity, Procedure)
    assert entity.id == "legacy_procedure"
    assert entity.entity_type == EntityType.PROCEDURE
    assert entity.description == "Legacy summary"
    assert entity.content == "Legacy summary"
    assert entity.category == ""
    assert entity.created_by == "legacy-agent"
    assert entity.metadata["project_id"] == "project_legacy"
    assert entity.metadata["source_ids"] == ["graphiti:episode_1"]


def test_normalize_records_preserves_surreal_record_id_without_leaking_id() -> None:
    records = normalize_records(
        {
            "id": "entity:procedure_native",
            "uuid": "procedure_native",
            "name": "Native Procedure",
        }
    )

    assert records == [
        {
            "record_id": "entity:procedure_native",
            "uuid": "procedure_native",
            "name": "Native Procedure",
        }
    ]


def test_relationship_from_surreal_row_preserves_temporal_provenance() -> None:
    relationship = relationship_from_surreal_row(
        {
            "id": "relates_to:rel_native",
            "uuid": "rel_native",
            "name": "SUPPORTS",
            "fact": "Task supports the plan",
            "group_id": "org-native",
            "source_uuid": "task_native",
            "target_uuid": "plan_native",
            "fact_embedding": [0.25] * EMBEDDING_DIM,
            "project_id": "project_native",
            "source_ids": ["raw_1", "raw_2"],
            "confidence": 0.87,
            "valid_at": "2026-05-13T12:00:00+00:00",
            "invalid_at": "2026-05-14T12:00:00+00:00",
            "expired_at": "2026-05-15T12:00:00+00:00",
            "created_by": "stef",
            "modified_by": "nova",
            "direction": "outgoing",
            "episodes": ["episode_1"],
            "attributes": {
                "metadata": json.dumps({"weight": 0.42, "project_id": "nested_project"}),
            },
            "created_at": "2026-05-13T12:30:00+00:00",
        }
    )

    assert relationship.id == "rel_native"
    assert relationship.relationship_type is RelationshipType.SUPPORTS
    assert relationship.source_id == "task_native"
    assert relationship.target_id == "plan_native"
    assert relationship.weight == 0.42
    assert relationship.metadata["fact"] == "Task supports the plan"
    assert relationship.metadata["fact_embedding"] == [0.25] * EMBEDDING_DIM
    assert relationship.metadata["project_id"] == "nested_project"
    assert relationship.metadata["source_ids"] == ["raw_1", "raw_2"]
    assert relationship.metadata["confidence"] == 0.87
    assert relationship.metadata["valid_at"] == "2026-05-13T12:00:00+00:00"
    assert relationship.metadata["invalid_at"] == "2026-05-14T12:00:00+00:00"
    assert relationship.metadata["expired_at"] == "2026-05-15T12:00:00+00:00"
    assert relationship.metadata["created_by"] == "stef"
    assert relationship.metadata["modified_by"] == "nova"
    assert relationship.metadata["direction"] == "outgoing"
    assert relationship.metadata["episodes"] == ["episode_1"]
    assert relationship.metadata["record_id"] == "relates_to:rel_native"


@pytest.mark.asyncio
async def test_native_entity_lists_order_by_updated_at_before_created_at() -> None:
    client = NativeSurrealGraphClient(group_id="org-native-ordering", url="memory://")
    try:
        await prepare_native_graph_schema(client)
        entity_manager = NativeEntityManager(client, group_id=client.group_id)

        await entity_manager.create_direct(
            Entity(
                id="task_created_newer",
                entity_type=EntityType.TASK,
                name="Created newer",
                organization_id=client.group_id,
                created_at=datetime(2026, 5, 14, tzinfo=UTC),
                metadata={
                    "status": "todo",
                    "updated_at": "2026-05-13T12:00:00+00:00",
                },
            )
        )
        await entity_manager.create_direct(
            Entity(
                id="task_updated_newer",
                entity_type=EntityType.TASK,
                name="Updated newer",
                organization_id=client.group_id,
                created_at=datetime(2026, 5, 13, tzinfo=UTC),
                metadata={
                    "status": "todo",
                    "updated_at": "2026-05-15T12:00:00+00:00",
                },
            )
        )

        typed = await entity_manager.list_by_type(
            EntityType.TASK,
            include_archived=True,
            limit=2,
        )
        all_entities = await entity_manager.list_all(include_archived=True, limit=2)

        assert [entity.id for entity in typed] == [
            "task_updated_newer",
            "task_created_newer",
        ]
        assert [entity.id for entity in all_entities] == [
            "task_updated_newer",
            "task_created_newer",
        ]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_native_graph_writes_entities_and_relationships_without_graphiti(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_graphiti_imports(monkeypatch)
    client = NativeSurrealGraphClient(group_id="org-native-graph", url="memory://")
    try:
        await prepare_native_graph_schema(client)
        entity_manager = NativeEntityManager(client, group_id=client.group_id)
        relationship_manager = NativeRelationshipManager(client, group_id=client.group_id)

        await entity_manager.create_direct(
            Entity(
                id="project_native",
                entity_type=EntityType.PROJECT,
                name="Native Project",
                description="Project anchor",
                organization_id="org-native-graph",
                metadata={"project_id": "project_native", "tags": ["native"]},
            )
        )
        await entity_manager.create_direct(
            Entity(
                id="decision_native",
                entity_type=EntityType.DECISION,
                name="Native Decision",
                description="Graphiti-free decision",
                content="Native graph writes should not import Graphiti.",
                organization_id="org-native-graph",
                source_file="raw_123",
                metadata={
                    "project_id": "project_native",
                    "source_ids": ["raw_123"],
                    "status": "doing",
                },
            )
        )
        created, failed = await relationship_manager.create_bulk(
            [
                Relationship(
                    id="rel_decision_project",
                    source_id="decision_native",
                    target_id="project_native",
                    relationship_type=RelationshipType.BELONGS_TO,
                    metadata={
                        "native_write_path": "test",
                        "source_ids": ["raw_123"],
                        "confidence": 0.82,
                        "valid_at": "2026-05-13T12:00:00+00:00",
                        "invalid_at": "2026-05-14T12:00:00+00:00",
                    },
                )
            ]
        )

        assert (created, failed) == (1, 0)
        rows = normalize_records(
            await client.execute_query(
                """
                SELECT uuid, name, entity_type, project_id, attributes
                FROM entity
                WHERE uuid = "decision_native";
                """
            )
        )
        assert rows[0]["project_id"] == "project_native"
        attributes = cast("dict[str, object]", rows[0]["attributes"])
        assert attributes["source_file"] == "raw_123"
        assert attributes["metadata"]

        relationships = normalize_records(
            await client.execute_query(
                """
                SELECT uuid,
                       name,
                       in.uuid AS source_uuid,
                       out.uuid AS target_uuid,
                       attributes,
                       valid_at,
                       invalid_at
                FROM relates_to
                WHERE uuid = "rel_decision_project";
                """
            )
        )
        assert len(relationships) == 1
        relationship_row = relationships[0]
        assert relationship_row["uuid"] == "rel_decision_project"
        assert relationship_row["name"] == "BELONGS_TO"
        assert relationship_row["source_uuid"] == "decision_native"
        assert relationship_row["target_uuid"] == "project_native"
        assert relationship_row["attributes"] == {
            "native_write_path": "test",
            "source_ids": ["raw_123"],
            "confidence": 0.82,
            "valid_at": "2026-05-13T12:00:00+00:00",
            "invalid_at": "2026-05-14T12:00:00+00:00",
        }
        assert relationship_row["valid_at"] == datetime(2026, 5, 13, 12, tzinfo=UTC)
        assert relationship_row["invalid_at"] == datetime(2026, 5, 14, 12, tzinfo=UTC)

        updated = await entity_manager.update(
            "decision_native",
            {"status": "done", "title": "Updated Native Decision"},
        )
        assert updated is not None
        assert updated.name == "Updated Native Decision"
        assert updated.metadata["status"] == "done"

        fetched_relationships = await relationship_manager.get_for_entity(
            "decision_native",
            relationship_types=[RelationshipType.BELONGS_TO],
            direction="outgoing",
        )
        assert [rel.target_id for rel in fetched_relationships] == ["project_native"]
        assert fetched_relationships[0].metadata["source_ids"] == ["raw_123"]
        assert fetched_relationships[0].metadata["confidence"] == 0.82
        assert fetched_relationships[0].metadata["direction"] == "outgoing"
        assert fetched_relationships[0].metadata["valid_at"]
        assert fetched_relationships[0].metadata["invalid_at"]
        matched_relationships = await relationship_manager.find_between(
            "decision_native",
            "project_native",
            relationship_type=RelationshipType.BELONGS_TO,
        )
        assert [rel.id for rel in matched_relationships] == ["rel_decision_project"]
        assert matched_relationships[0].metadata["source_ids"] == ["raw_123"]

        deleted = await relationship_manager.delete_between(
            "decision_native",
            "project_native",
            RelationshipType.BELONGS_TO,
        )
        assert deleted == 1

        search_results = await entity_manager.search(
            query="Updated Native Decision",
            entity_types=[EntityType.DECISION],
        )
        assert search_results
        assert all(0.0 <= score <= 1.0 for _, score in search_results)

        await entity_manager.create_direct(
            Entity(
                id="epic_native",
                entity_type=EntityType.EPIC,
                name="Native epic",
                description="Epic with task progress",
                organization_id="org-native-graph",
                metadata={
                    "project_id": "project_native",
                    "status": "planning",
                    "total_tasks": 0,
                    "completed_tasks": 0,
                },
            )
        )
        await entity_manager.create_direct(
            Entity(
                id="epic_native_two",
                entity_type=EntityType.EPIC,
                name="Native blocked epic",
                description="Epic with non-terminal task progress",
                organization_id="org-native-graph",
                metadata={
                    "project_id": "project_native",
                    "status": "blocked",
                    "total_tasks": 0,
                    "completed_tasks": 0,
                },
            )
        )
        await entity_manager.create_direct(
            Entity(
                id="epic_native_empty",
                entity_type=EntityType.EPIC,
                name="Native empty epic",
                description="Epic with no tasks",
                organization_id="org-native-graph",
                metadata={
                    "project_id": "project_native",
                    "status": "completed",
                    "total_tasks": 9,
                    "completed_tasks": 9,
                },
            )
        )
        await entity_manager.create_direct(
            Entity(
                id="task_native_one",
                entity_type=EntityType.TASK,
                name="Native filtered task",
                description="Task with every native list filter",
                organization_id="org-native-graph",
                metadata={
                    "project_id": "project_native",
                    "epic_id": "epic_native",
                    "status": "todo",
                    "priority": "high",
                    "complexity": "complex",
                    "feature": "surreal",
                    "tags": ["native", "graph"],
                },
            )
        )
        await entity_manager.create_direct(
            Entity(
                id="task_native_done",
                entity_type=EntityType.TASK,
                name="Native completed task",
                description="Done task attached to an epic",
                organization_id="org-native-graph",
                metadata={
                    "project_id": "project_native",
                    "epic_id": "epic_native",
                    "status": "done",
                    "priority": "medium",
                    "complexity": "simple",
                    "feature": "surreal",
                    "tags": ["native"],
                },
            )
        )
        await entity_manager.create_direct(
            Entity(
                id="task_native_doing",
                entity_type=EntityType.TASK,
                name="Native active task",
                description="Doing task attached to an epic",
                organization_id="org-native-graph",
                metadata={
                    "project_id": "project_native",
                    "epic_id": "epic_native",
                    "status": "doing",
                    "priority": "medium",
                    "complexity": "simple",
                    "feature": "surreal",
                    "tags": ["native"],
                },
            )
        )
        await entity_manager.create_direct(
            Entity(
                id="task_native_review",
                entity_type=EntityType.TASK,
                name="Native review task",
                description="Review task attached to another epic",
                organization_id="org-native-graph",
                metadata={
                    "project_id": "project_native",
                    "epic_id": "epic_native_two",
                    "status": "review",
                    "priority": "medium",
                    "complexity": "simple",
                    "feature": "surreal",
                    "tags": ["native"],
                },
            )
        )
        await entity_manager.create_direct(
            Entity(
                id="task_native_blocked",
                entity_type=EntityType.TASK,
                name="Native blocked task",
                description="Blocked task attached to another epic",
                organization_id="org-native-graph",
                metadata={
                    "project_id": "project_native",
                    "epic_id": "epic_native_two",
                    "status": "blocked",
                    "priority": "medium",
                    "complexity": "simple",
                    "feature": "surreal",
                    "tags": ["native"],
                },
            )
        )
        await entity_manager.create_direct(
            Entity(
                id="task_native_archived_epic",
                entity_type=EntityType.TASK,
                name="Native archived epic task",
                description="Archived task attached to another epic",
                organization_id="org-native-graph",
                metadata={
                    "project_id": "project_native",
                    "epic_id": "epic_native_two",
                    "status": "archived",
                    "priority": "medium",
                    "complexity": "simple",
                    "feature": "surreal",
                    "tags": ["native"],
                },
            )
        )
        await entity_manager.create_direct(
            Entity(
                id="task_native_two",
                entity_type=EntityType.TASK,
                name="Native unepic task",
                description="Task without an epic",
                organization_id="org-native-graph",
                metadata={
                    "project_id": "project_native",
                    "status": "doing",
                    "priority": "medium",
                    "complexity": "simple",
                    "feature": "surreal",
                    "tags": ["native"],
                },
            )
        )
        await entity_manager.create_direct(
            Entity(
                id="task_native_archived",
                entity_type=EntityType.TASK,
                name="Native archived task",
                description="Archived task hidden by default",
                organization_id="org-native-graph",
                metadata={
                    "project_id": "project_native",
                    "status": "archived",
                    "tags": ["native"],
                },
            )
        )

        fetched_task = await entity_manager.get("task_native_one")
        assert "metadata" not in fetched_task.metadata
        assert fetched_task.metadata["status"] == "todo"
        assert fetched_task.metadata["project_id"] == "project_native"

        filtered = await entity_manager.list_by_type(
            EntityType.TASK,
            project_id="project_native",
            epic_id="epic_native",
            status="todo,done",
            priority="high",
            complexity="complex",
            feature="surreal",
            tags=["graph"],
            include_archived=False,
        )
        assert [entity.id for entity in filtered] == ["task_native_one"]

        no_epic = await entity_manager.list_by_type(
            EntityType.TASK,
            project_id="project_native",
            no_epic=True,
        )
        assert [entity.id for entity in no_epic] == ["task_native_two"]

        archived = await entity_manager.list_by_type(
            EntityType.TASK,
            project_id="project_native",
            include_archived=True,
        )
        assert {entity.id for entity in archived} == {
            "task_native_one",
            "task_native_done",
            "task_native_doing",
            "task_native_review",
            "task_native_blocked",
            "task_native_archived_epic",
            "task_native_two",
            "task_native_archived",
        }

        progress = await entity_manager.get_epic_progress("epic_native")
        assert progress["total_tasks"] == 3
        assert progress["completed_tasks"] == 1
        assert progress["in_progress_tasks"] == 1
        assert progress["completion_pct"] == 33.3

        epics = await entity_manager.list_by_type(
            EntityType.EPIC,
            project_id="project_native",
            enrich_epic_progress=True,
        )
        epics_by_id = {epic.id: epic for epic in epics}
        assert epics_by_id["epic_native"].metadata["total_tasks"] == 3
        assert epics_by_id["epic_native"].metadata["completed_tasks"] == 1
        assert epics_by_id["epic_native"].metadata["in_progress_tasks"] == 1
        assert epics_by_id["epic_native"].metadata["completion_pct"] == 33.3
        assert epics_by_id["epic_native_two"].metadata["total_tasks"] == 3
        assert epics_by_id["epic_native_two"].metadata["completed_tasks"] == 0
        assert epics_by_id["epic_native_two"].metadata["blocked_tasks"] == 1
        assert epics_by_id["epic_native_two"].metadata["in_review_tasks"] == 1
        assert epics_by_id["epic_native_two"].metadata["completion_pct"] == 0.0
        assert epics_by_id["epic_native_empty"].metadata["total_tasks"] == 0
        assert epics_by_id["epic_native_empty"].metadata["completed_tasks"] == 0
        assert epics_by_id["epic_native_empty"].metadata["completion_pct"] == 0

        planning_epics = await entity_manager.list_by_type(
            EntityType.EPIC,
            project_id="project_native",
            status="planning",
            enrich_epic_progress=True,
        )
        assert [epic.id for epic in planning_epics] == ["epic_native"]

        summary = await entity_manager.get_project_summary("project_native", epic_limit=10)
        summary_epics = {epic["id"]: epic for epic in summary["epics"]}
        assert summary_epics["epic_native"]["total_tasks"] == 3
        assert summary_epics["epic_native"]["progress_pct"] == 33.3
        assert summary_epics["epic_native_two"]["total_tasks"] == 3
        assert summary_epics["epic_native_empty"]["total_tasks"] == 0

        visible_ids = {
            entity.id for entity in await entity_manager.list_all(include_archived=False)
        }
        assert "decision_native" in visible_ids
        assert "task_native_archived" not in visible_ids
        assert "task_native_archived_epic" not in visible_ids

        embedding = [0.2] * EMBEDDING_DIM
        await entity_manager.create_direct(
            Entity(
                id="dedup_keep",
                entity_type=EntityType.PATTERN,
                name="Native duplicate pattern",
                description="Kept native dedup entity",
                organization_id="org-native-graph",
                metadata={"source": "keep"},
                embedding=embedding,
            )
        )
        await entity_manager.create_direct(
            Entity(
                id="dedup_remove",
                entity_type=EntityType.PATTERN,
                name="Native duplicate pattern",
                description="Removed native dedup entity",
                organization_id="org-native-graph",
                metadata={"source": "remove", "merged": True},
                embedding=embedding,
            )
        )
        await entity_manager.create_direct(
            Entity(
                id="dedup_neighbor",
                entity_type=EntityType.DECISION,
                name="Native dedup neighbor",
                description="Relationship target for merge redirect",
                organization_id="org-native-graph",
            )
        )
        await relationship_manager.create(
            Relationship(
                id="rel_dedup_remove_neighbor",
                source_id="dedup_remove",
                target_id="dedup_neighbor",
                relationship_type=RelationshipType.RELATED_TO,
            )
        )

        deduplicator = EntityDeduplicator(
            client=client,
            entity_manager=entity_manager,
        )
        assert await deduplicator.merge_entities(
            keep_id="dedup_keep",
            remove_id="dedup_remove",
        )

        with pytest.raises(KeyError):
            await entity_manager.get("dedup_remove")

        redirected = await relationship_manager.get_for_entity(
            "dedup_keep",
            relationship_types=[RelationshipType.RELATED_TO],
            direction="outgoing",
        )
        assert [rel.target_id for rel in redirected] == ["dedup_neighbor"]
        assert await relationship_manager.get_for_entity("dedup_remove", direction="both") == []
    finally:
        await client.close()
