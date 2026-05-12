from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

import sibyl_core.retrieval.native as native_retrieval
from sibyl_core.backends.surreal import SurrealDriver
from sibyl_core.backends.surreal.content_client import SurrealContentClient
from sibyl_core.backends.surreal.content_schema import bootstrap_content_schema
from sibyl_core.backends.surreal.schema import EMBEDDING_DIM
from sibyl_core.graph.entities import EntityManager
from sibyl_core.graph.relationships import RelationshipManager
from sibyl_core.graph.surreal.ops._common import normalize_records
from sibyl_core.models.entities import Entity, EntityType
from sibyl_core.services import native_memory
from sibyl_core.services.surreal_content import get_raw_memory, save_raw_memory
from sibyl_core.tools.context import compile_context, context_pack_to_markdown
from sibyl_core.tools.reflect import reflect_memory, reflection_pack_to_dict


class FakeEmbedder:
    async def create(self, _input_data: object) -> list[float]:
        return [0.1] * EMBEDDING_DIM


class FakeGraphClient:
    def __init__(self, driver: SurrealDriver) -> None:
        self.add_episode = AsyncMock(side_effect=AssertionError("Graphiti add_episode forbidden"))
        self.client = SimpleNamespace(embedder=FakeEmbedder(), add_episode=self.add_episode)
        self._driver = driver

    def get_org_driver(self, _organization_id: str) -> SurrealDriver:
        return self._driver


async def _empty_raw_memory_recall(**_kwargs: Any) -> list[Any]:
    return []


async def _unexpected_graphiti_search(**_kwargs: Any) -> Any:
    raise AssertionError("Graphiti fallback search should not run in native mode")


async def _seed_scope_entities(
    graph_client: FakeGraphClient,
    *,
    group_id: str,
) -> None:
    manager = EntityManager(graph_client, group_id=group_id)
    await manager.create_direct(
        Entity(
            id="project_native",
            entity_type=EntityType.PROJECT,
            name="Native Contract Project",
            description="Project scope anchor",
            organization_id=group_id,
            metadata={"project_id": "project_native"},
        ),
        generate_embedding=False,
    )
    await manager.create_direct(
        Entity(
            id="task_native",
            entity_type=EntityType.TASK,
            name="Native Contract Task",
            description="Related task anchor",
            organization_id=group_id,
            metadata={"project_id": "project_native", "status": "doing"},
        ),
        generate_embedding=False,
    )


@pytest.mark.asyncio
async def test_native_reflection_write_contract_renders_context_pack(
    surreal_schema: SurrealDriver,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    group_id = surreal_schema.group_id
    graph_client = FakeGraphClient(surreal_schema)
    runtime = SimpleNamespace(
        client=graph_client,
        entity_manager=EntityManager(graph_client, group_id=group_id),
        relationship_manager=RelationshipManager(graph_client, group_id=group_id),
    )

    async def fake_get_graph_runtime(organization_id: str) -> SimpleNamespace:
        assert organization_id == group_id
        return runtime

    monkeypatch.setenv("SIBYL_NATIVE_WRITE", "enabled")
    monkeypatch.setattr(native_memory, "get_native_graph_runtime", fake_get_graph_runtime)
    monkeypatch.setattr(native_retrieval, "get_graph_runtime", fake_get_graph_runtime)

    await _seed_scope_entities(graph_client, group_id=group_id)

    compatibility_add = AsyncMock(
        side_effect=AssertionError("compatibility add path should not run")
    )
    reflection = await reflect_memory(
        (
            "We decided native reflection promotion should bypass Graphiti add_episode "
            "and write directly to Surreal graph records for context packs."
        ),
        source_title="Native Reflection Contract",
        intent="build",
        domain="sibyl",
        project="project_native",
        related_to=["task_native"],
        organization_id=group_id,
        principal_id="user-native",
        accessible_projects={"project_native"},
        persist=True,
        add_fn=compatibility_add,
    )

    compatibility_add.assert_not_awaited()
    graph_client.add_episode.assert_not_called()
    assert reflection.source_id == "session_e9790facb5f9"
    assert reflection.persisted_count == 1
    candidate = reflection.candidates[0]
    assert candidate.persisted_id == "decision_0a054f09f2ae"

    source_node = await surreal_schema.entity_node_ops.get_by_uuid(
        surreal_schema,
        reflection.source_id,
    )
    candidate_node = await surreal_schema.entity_node_ops.get_by_uuid(
        surreal_schema,
        candidate.persisted_id,
    )
    candidate_metadata = json.loads(candidate_node.attributes["metadata"])

    assert source_node.attributes["entity_type"] == "session"
    assert candidate_node.attributes["entity_type"] == "decision"
    assert candidate_node.attributes["source_file"] == reflection.source_id
    assert candidate_metadata["raw_source_ids"] == [reflection.source_id]
    assert candidate_metadata["source_ids"] == [reflection.source_id]
    assert candidate_metadata["reflection_source_id"] == reflection.source_id
    assert candidate_metadata["native_write_path"] == "reflection_promotion"
    assert candidate_metadata["policy_reasons"] == [
        "same_scope_reflect_allowed",
        "same_scope_write_allowed",
    ]

    relationship_rows = normalize_records(
        await surreal_schema.execute_query(
            """
            SELECT uuid, name, in.uuid AS source_uuid, out.uuid AS target_uuid
            FROM relates_to
            WHERE group_id = $group_id;
            """,
            group_id=group_id,
        )
    )
    relationship_keys = {
        (row["source_uuid"], row["name"], row["target_uuid"]) for row in relationship_rows
    }
    assert (candidate.persisted_id, "BELONGS_TO", "project_native") in relationship_keys
    assert (candidate.persisted_id, "DERIVED_FROM", reflection.source_id) in relationship_keys
    assert (candidate.persisted_id, "RELATED_TO", "task_native") in relationship_keys

    context_pack = await compile_context(
        "native reflection promotion Surreal graph records",
        intent="build",
        project="project_native",
        accessible_projects={"project_native"},
        organization_id=group_id,
        principal_id="user-native",
        search_fn=_unexpected_graphiti_search,
        raw_memory_recall_fn=_empty_raw_memory_recall,
        limit=6,
        related_limit=0,
        retrieval_mode="native",
    )
    markdown = context_pack_to_markdown(context_pack, max_items=8)
    context_ids = {item.id for section in context_pack.sections for item in section.items}

    graph_client.add_episode.assert_not_called()
    assert candidate.persisted_id in context_ids
    assert reflection.source_id in context_ids
    assert "native reflection promotion should bypass Graphiti add_episode" in markdown
    assert f"src={reflection.source_id}" in markdown

    response_snapshot = reflection_pack_to_dict(reflection)
    assert response_snapshot == {
        "source_title": "Native Reflection Contract",
        "source_id": "session_e9790facb5f9",
        "intent": "build",
        "domain": "sibyl",
        "project": "project_native",
        "candidates": [
            {
                "kind": "decision",
                "title": (
                    "Decision: We decided native reflection promotion should bypass "
                    "Graphiti add_episode an"
                ),
                "content": (
                    "We decided native reflection promotion should bypass Graphiti "
                    "add_episode and write directly to Surreal graph records for "
                    "context packs."
                ),
                "reason": "captures a choice or direction future agents should preserve",
                "confidence": 0.86,
                "tags": ["reflection", "decision", "sibyl"],
                "metadata": {
                    "reflection_source_title": "Native Reflection Contract",
                    "reflection_intent": "build",
                    "reflection_index": 0,
                    "project_id": "project_native",
                    "organization_id": group_id,
                    "capture_mode": "reflect",
                    "capture_surface": "reflection",
                    "remember_kind": "decision",
                    "reflection_reason": (
                        "captures a choice or direction future agents should preserve"
                    ),
                    "reflection_confidence": 0.86,
                    "raw_source_ids": ["session_e9790facb5f9"],
                    "source_ids": ["session_e9790facb5f9"],
                    "suggested_memory_scope": "project",
                    "suggested_scope_key": "project_native",
                    "review_state": "pending",
                    "extraction_prompt_metadata": {
                        "extractor": "sibyl_reflect_heuristic",
                        "extractor_version": "v0.7",
                        "intent": "build",
                        "domain": "sibyl",
                        "project": "project_native",
                        "limit": 12,
                    },
                    "domain": "sibyl",
                    "reflection_source_id": "session_e9790facb5f9",
                    "native_write_mode": "enabled",
                    "memory_scope": "project",
                    "scope_key": "project_native",
                    "policy_allowed": True,
                    "policy_reasons": [
                        "same_scope_reflect_allowed",
                        "same_scope_write_allowed",
                    ],
                    "policy_actions": ["reflect", "write"],
                    "native_write_path": "reflection_promotion",
                    "native_relationship_count": 3,
                },
                "raw_source_ids": ["session_e9790facb5f9"],
                "suggested_memory_scope": "project",
                "suggested_scope_key": "project_native",
                "review_state": "pending",
                "persisted_id": "decision_0a054f09f2ae",
            }
        ],
        "total_candidates": 1,
        "persisted_count": 1,
        "usage_hint": (
            "Review candidates, persist the durable ones, and keep raw session "
            "source as provenance."
        ),
    }


@pytest.mark.asyncio
async def test_post_reflection_recall_promotes_review_candidate_into_native_context(
    surreal_schema: SurrealDriver,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    group_id = surreal_schema.group_id
    content_client = SurrealContentClient(url="memory://")
    await bootstrap_content_schema(content_client, reset=True)

    @asynccontextmanager
    async def client_session() -> AsyncIterator[SurrealContentClient]:
        yield content_client

    from sibyl_core.services import surreal_content as content_service

    monkeypatch.setattr(content_service, "surreal_content_client", client_session)

    graph_client = FakeGraphClient(surreal_schema)
    runtime = SimpleNamespace(
        client=graph_client,
        entity_manager=EntityManager(graph_client, group_id=group_id),
        relationship_manager=RelationshipManager(graph_client, group_id=group_id),
    )

    async def fake_get_graph_runtime(organization_id: str) -> SimpleNamespace:
        assert organization_id == group_id
        return runtime

    monkeypatch.setenv("SIBYL_NATIVE_WRITE", "enabled")
    monkeypatch.setattr(native_memory, "get_native_graph_runtime", fake_get_graph_runtime)
    monkeypatch.setattr(native_retrieval, "get_graph_runtime", fake_get_graph_runtime)

    try:
        await _seed_scope_entities(graph_client, group_id=group_id)
        await runtime.entity_manager.create_direct(
            Entity(
                id="decision_legacy_recall",
                entity_type=EntityType.DECISION,
                name="Decision: Legacy recall depended on raw review shortcuts",
                description="Old reflection recall path stayed in the raw review queue.",
                content="Old reflection recall depended on raw-only shortcuts.",
                organization_id=group_id,
                metadata={"project_id": "project_native"},
            ),
            generate_embedding=False,
        )

        compatibility_add = AsyncMock(
            side_effect=AssertionError("compatibility add path should not run")
        )
        reflection = await reflect_memory(
            (
                "We decided post-reflection recall should promote reviewed Surreal "
                "candidates into native graph records so context packs do not depend "
                "on raw-only shortcuts."
            ),
            source_title="Post Reflection Recall Fixture",
            intent="build",
            domain="sibyl",
            project="project_native",
            related_to=["task_native"],
            organization_id=group_id,
            principal_id="user-native",
            accessible_projects={"project_native"},
            memory_scope="project",
            scope_key="project_native",
            persist=True,
            persist_review=True,
            add_fn=compatibility_add,
        )
        compatibility_add.assert_not_awaited()
        graph_client.add_episode.assert_not_called()

        candidate_id = reflection.candidates[0].persisted_id
        assert candidate_id is not None
        candidate_memory = await get_raw_memory(
            organization_id=group_id,
            memory_id=candidate_id,
        )
        assert candidate_memory is not None
        await save_raw_memory(
            replace(
                candidate_memory,
                metadata={
                    **candidate_memory.metadata,
                    "supersedes_ids": ["decision_legacy_recall"],
                },
            )
        )

        promotion = await native_memory.promote_reflection_candidate_review(
            candidate_id=candidate_id,
            organization_id=group_id,
            principal_id="user-native",
            promote_to_scope="project",
            promote_to_scope_key="project_native",
            domain="sibyl",
            project="project_native",
            related_to=["task_native"],
            accessible_projects={"project_native"},
        )

        assert promotion.success
        assert promotion.promoted_id is not None
        promoted_raw = await get_raw_memory(
            organization_id=group_id,
            memory_id=candidate_id,
        )
        assert promoted_raw is not None
        assert promoted_raw.review_state == "promoted"
        assert promoted_raw.metadata["promoted_entity_id"] == promotion.promoted_id
        assert promoted_raw.metadata["native_relationship_count"] == 3

        promoted_node = await surreal_schema.entity_node_ops.get_by_uuid(
            surreal_schema,
            promotion.promoted_id,
        )
        promoted_metadata = json.loads(promoted_node.attributes["metadata"])
        assert reflection.source_id in promoted_metadata["raw_source_ids"]
        assert promoted_metadata["source_ids"] == promoted_metadata["raw_source_ids"]
        assert promoted_metadata["review_capture_id"] == candidate_id
        assert promoted_metadata["supersedes_ids"] == ["decision_legacy_recall"]

        relationship_rows = normalize_records(
            await surreal_schema.execute_query(
                """
                SELECT uuid, name, attributes, in.uuid AS source_uuid, out.uuid AS target_uuid
                FROM relates_to
                WHERE group_id = $group_id;
                """,
                group_id=group_id,
            )
        )
        relationship_keys = {
            (row["source_uuid"], row["name"], row["target_uuid"]) for row in relationship_rows
        }
        assert (promotion.promoted_id, "BELONGS_TO", "project_native") in relationship_keys
        assert (promotion.promoted_id, "RELATED_TO", "task_native") in relationship_keys
        assert (promotion.promoted_id, "SUPERSEDES", "decision_legacy_recall") in relationship_keys
        supersedes_row = next(row for row in relationship_rows if row["name"] == "SUPERSEDES")
        assert supersedes_row["attributes"]["source_id"] == reflection.source_id
        assert supersedes_row["attributes"]["raw_source_ids"] == [reflection.source_id]
        assert supersedes_row["attributes"]["replacement_reason"] == "accepted_reflection_candidate"
        assert supersedes_row["attributes"]["valid_from"]
        assert (
            promotion.promoted_id,
            "DERIVED_FROM",
            reflection.source_id,
        ) not in relationship_keys

        context_pack = await compile_context(
            "post-reflection recall promote reviewed Surreal candidates native graph",
            intent="build",
            project="project_native",
            accessible_projects={"project_native"},
            organization_id=group_id,
            principal_id="user-native",
            search_fn=_unexpected_graphiti_search,
            raw_memory_recall_fn=_empty_raw_memory_recall,
            limit=6,
            related_limit=0,
            retrieval_mode="native",
        )
        markdown = context_pack_to_markdown(context_pack, max_items=8)
        context_ids = {item.id for section in context_pack.sections for item in section.items}

        graph_client.add_episode.assert_not_called()
        assert promotion.promoted_id in context_ids
        assert all(not item_id.startswith("raw_memory:") for item_id in context_ids)
        assert "post-reflection recall should promote reviewed Surreal candidates" in markdown
    finally:
        await content_client.close()
