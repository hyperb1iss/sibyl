from __future__ import annotations

import builtins
from typing import Any, cast

import pytest

from sibyl_core.backends.surreal.schema import EMBEDDING_DIM
from sibyl_core.models.entities import Entity, EntityType, Relationship, RelationshipType
from sibyl_core.retrieval.dedup import EntityDeduplicator
from sibyl_core.services.native_graph import (
    NativeEntityManager,
    NativeRelationshipManager,
    NativeSurrealGraphClient,
    normalize_records,
    prepare_native_graph_schema,
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
                    metadata={"native_write_path": "test"},
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
                SELECT uuid, name, in.uuid AS source_uuid, out.uuid AS target_uuid, attributes
                FROM relates_to
                WHERE uuid = "rel_decision_project";
                """
            )
        )
        assert relationships == [
            {
                "uuid": "rel_decision_project",
                "name": "BELONGS_TO",
                "source_uuid": "decision_native",
                "target_uuid": "project_native",
                "attributes": {"native_write_path": "test"},
            }
        ]

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
            "task_native_two",
            "task_native_archived",
        }

        visible_ids = {
            entity.id for entity in await entity_manager.list_all(include_archived=False)
        }
        assert "decision_native" in visible_ids
        assert "task_native_archived" not in visible_ids

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
