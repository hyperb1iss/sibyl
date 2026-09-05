from __future__ import annotations

import json
from datetime import UTC, datetime
from itertools import count
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from sibyl_core.models import reflection as reflection_models
from sibyl_core.models.entities import Entity, EntityType
from sibyl_core.services.surreal_content import MemoryScope, RawMemory
from sibyl_core.tools.reflect import (
    reflect_memory,
    reflection_pack_to_dict,
    reflection_pack_to_markdown,
)


@pytest.fixture
def native_reflection_runtime(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Keep the native orchestration real while recording its storage boundary."""
    entities: list[Entity] = []
    relationships = []

    async def create_direct_if_absent(entity):
        entities.append(entity)
        return entity, True

    async def update(entity_id, updates, **kwargs):
        entity = next(entity for entity in entities if entity.id == entity_id)
        entity.metadata.update(updates["metadata"])
        return entity

    async def create_bulk(items):
        relationships.extend(items)
        return len(items), 0

    runtime = SimpleNamespace(
        entity_manager=SimpleNamespace(
            create_direct_if_absent=AsyncMock(side_effect=create_direct_if_absent),
            update=AsyncMock(side_effect=update),
            get=AsyncMock(return_value=None),
        ),
        relationship_manager=SimpleNamespace(create_bulk=AsyncMock(side_effect=create_bulk)),
        entities=entities,
        relationships=relationships,
    )
    monkeypatch.setattr(
        "sibyl_core.services.memory_reflection.get_surreal_graph_runtime",
        AsyncMock(return_value=runtime),
    )
    monkeypatch.setattr(
        "sibyl_core.tools.reflect._load_reflection_decision_memories", AsyncMock(return_value=[])
    )
    return runtime


@pytest.mark.asyncio
@pytest.mark.parametrize("retired_mode", [None, "enabled", "disabled", "typo"])
async def test_native_reflection_frozen_contract(
    native_reflection_runtime: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    retired_mode: str | None,
) -> None:
    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 9, 4, 12, tzinfo=UTC)

    ids = count(1)
    monkeypatch.setattr(reflection_models, "datetime", FrozenDatetime)
    monkeypatch.setattr("sibyl_core.services.memory_lifecycle.datetime", FrozenDatetime)
    monkeypatch.setattr(reflection_models, "uuid4", lambda: UUID(int=next(ids)))
    if retired_mode is None:
        monkeypatch.delenv("SIBYL_NATIVE_WRITE", raising=False)
    else:
        monkeypatch.setenv("SIBYL_NATIVE_WRITE", retired_mode)
    pack = await reflect_memory(
        "We decided to preserve source evidence before publishing decisions.",
        source_title="Evidence review",
        intent="build",
        domain="sibyl",
        project="project_123",
        organization_id="org_123",
        principal_id="user_123",
        accessible_projects={"project_123"},
        persist=True,
    )
    assert pack.persisted_count == 1
    assert len(native_reflection_runtime.entities) == 2
    actual = {
        "pack": reflection_pack_to_dict(pack),
        "markdown": reflection_pack_to_markdown(pack),
        "entities": [
            entity.model_dump(mode="json", exclude={"created_at", "updated_at"})
            for entity in native_reflection_runtime.entities
        ],
        "relationships": [
            relationship.model_dump(mode="json", exclude={"created_at"})
            for relationship in native_reflection_runtime.relationships
        ],
    }
    fixture = Path(__file__).parent / "fixtures" / "reflection_native_contract.json"
    assert actual == json.loads(fixture.read_text())


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["private", "project"])
@pytest.mark.parametrize("existing_source_id", [None, "raw-existing-source"])
@pytest.mark.parametrize("persist_source", [False, True])
async def test_native_reflection_keeps_scope_and_provenance(
    native_reflection_runtime: SimpleNamespace,
    scope: str,
    existing_source_id: str | None,
    persist_source: bool,
) -> None:
    from sibyl_core.services.reflection import ephemeral_reflection_source_id

    content = "We decided to preserve source evidence before publishing decisions."
    project = "project_123" if scope == "project" else None
    pack = await reflect_memory(
        content,
        organization_id="org_123",
        principal_id="user_123",
        project=project,
        accessible_projects={"project_123"},
        memory_scope=scope,
        scope_key=project,
        persist=True,
        persist_source=persist_source,
        existing_source_id=existing_source_id,
    )
    source_created = persist_source and existing_source_id is None
    assert len(native_reflection_runtime.entities) == (2 if source_created else 1)
    entity = native_reflection_runtime.entities[-1]
    assert entity.metadata["memory_scope"] == scope
    assert entity.metadata.get("scope_key") == project
    assert entity.metadata["principal_id"] == "user_123"
    assert entity.organization_id == "org_123"
    if source_created:
        source = native_reflection_runtime.entities[0]
        assert source.entity_type is EntityType.SESSION
        assert source.content == content
        assert source.metadata["reflection_source"] is True
        assert source.metadata["memory_scope"] == scope
        assert source.metadata.get("scope_key") == project
        assert source.metadata["principal_id"] == "user_123"
        assert pack.source_id == source.id
    else:
        assert pack.source_id == existing_source_id
    source_ids = [pack.source_id or ephemeral_reflection_source_id(content)]
    assert pack.candidates[0].raw_source_ids == source_ids
    assert entity.metadata["raw_source_ids"] == source_ids
    assert pack.persisted_count == 1


@pytest.mark.asyncio
async def test_native_reflection_returns_partial_relationship_receipt(
    native_reflection_runtime: SimpleNamespace,
) -> None:
    native_reflection_runtime.relationship_manager.create_bulk.side_effect = None
    native_reflection_runtime.relationship_manager.create_bulk.return_value = (0, 1)
    pack = await reflect_memory(
        "We decided to preserve source evidence before publishing decisions.",
        organization_id="org_123",
        principal_id="user_123",
        project="project_123",
        accessible_projects={"project_123"},
        persist=True,
        persist_source=False,
    )
    assert pack.persisted_count == 1
    metadata = reflection_pack_to_dict(pack)["candidates"][0]["metadata"]
    assert metadata["native_relationship_failed_count"] == 1
    assert metadata["promotion_state"] == "partial"
    assert metadata["promotion_errors"] == ["1 promotion relationships failed"]


@pytest.mark.asyncio
async def test_reflect_memory_extracts_domain_general_candidates() -> None:
    pack = await reflect_memory(
        "We decided to keep one Hyperbliss Technologies org. "
        "Next we will build reflect so agents remember planning sessions. "
        "Maybe context packs should score decisions above loose notes. "
        "Validated docs/architecture/SURREALDB_NATIVE_GOAL_STATE.md as the source.",
        source_title="Surreal planning",
        intent="plan",
        domain="sibyl",
        project="project_123",
        organization_id="org_123",
    )

    kinds = {candidate.kind for candidate in pack.candidates}

    assert {"decision", "plan", "idea", "artifact"} <= kinds
    assert pack.project == "project_123"
    assert pack.total_candidates == len(pack.candidates)
    assert all(candidate.metadata["project_id"] == "project_123" for candidate in pack.candidates)


@pytest.mark.asyncio
async def test_reflect_memory_dedupes_repeated_candidates_by_kind_and_content() -> None:
    pack = await reflect_memory(
        "We decided reflection should dedupe repeated findings. "
        "We decided reflection should dedupe repeated findings.",
        source_title="Repeated reflection",
        intent="build",
        domain="sibyl",
        project="project_123",
        organization_id="org_123",
    )

    decisions = [candidate for candidate in pack.candidates if candidate.kind == "decision"]

    assert len(decisions) == 1
    assert decisions[0].content == "We decided reflection should dedupe repeated findings."


@pytest.mark.asyncio
async def test_reflect_memory_persist_denies_unverified_project(
    native_reflection_runtime: SimpleNamespace,
) -> None:

    pack = await reflect_memory(
        "We decided unauthorized reflection writes must fail before persistence.",
        source_title="Reflection denial",
        intent="build",
        domain="sibyl",
        project="project_123",
        organization_id="org_123",
        principal_id="user_123",
        accessible_projects={"project_other"},
        memory_scope="project",
        scope_key="project_123",
        persist=True,
        persist_review=False,
    )

    native_reflection_runtime.entity_manager.create_direct_if_absent.assert_not_awaited()
    native_reflection_runtime.relationship_manager.create_bulk.assert_not_awaited()
    assert pack.source_id is None
    assert pack.persisted_count == 0
    assert pack.candidates[0].metadata["policy_allowed"] is False
    assert pack.candidates[0].metadata["policy_reasons"] == [
        "unverified_membership",
        "unverified_membership",
    ]


@pytest.mark.asyncio
async def test_reflect_memory_can_persist_review_queue(
    monkeypatch: pytest.MonkeyPatch,
    native_reflection_runtime: SimpleNamespace,
) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    async def fake_remember_source(**kwargs: Any) -> SimpleNamespace:
        calls.append(("source", kwargs))
        return SimpleNamespace(id="raw-source-1", title=kwargs["title"])

    async def fake_candidate_review(**kwargs: Any) -> RawMemory:
        calls.append(("candidate", kwargs))
        candidate = kwargs["candidate"]
        return RawMemory(
            id="raw-candidate-1",
            organization_id=kwargs["organization_id"],
            source_id=kwargs["source_id"],
            principal_id=kwargs["principal_id"],
            memory_scope=MemoryScope.PROJECT,
            scope_key="project_123",
            project_id="project_123",
            review_state="pending",
            entity_type=candidate.kind,
            title=candidate.title,
            raw_content=candidate.content,
            tags=list(candidate.tags),
            metadata=dict(candidate.metadata),
            capture_surface="reflection_candidate",
        )

    monkeypatch.setattr(
        "sibyl_core.services.surreal_content.remember_raw_memory",
        fake_remember_source,
    )
    monkeypatch.setattr(
        "sibyl_core.tools.reflect._persist_reflection_candidate_review",
        fake_candidate_review,
    )

    pack = await reflect_memory(
        "We decided reflection candidates should be reviewed before promotion.",
        source_title="Reflection queue",
        intent="build",
        domain="sibyl",
        project="project_123",
        organization_id="org_123",
        principal_id="user_123",
        accessible_projects={"project_123"},
        memory_scope="project",
        scope_key="project_123",
        suggested_memory_scope="team",
        persist=True,
        persist_review=True,
    )

    assert [kind for kind, _ in calls] == ["source", "candidate"]
    assert pack.source_id == "raw-source-1"
    assert pack.persisted_count == 1
    assert pack.candidates[0].persisted_id == "raw-candidate-1"
    assert pack.candidates[0].raw_source_ids == ["raw-source-1"]
    assert pack.candidates[0].suggested_memory_scope == "team"
    assert pack.candidates[0].suggested_scope_key is None
    assert pack.candidates[0].metadata["raw_source_ids"] == ["raw-source-1"]
    assert pack.candidates[0].metadata["review_state"] == "pending"
    assert pack.candidates[0].metadata["policy_allowed"] is True
    assert pack.candidates[0].metadata["policy_reasons"] == [
        "same_scope_reflect_allowed",
        "same_scope_write_allowed",
    ]
    assert calls[0][1]["metadata"]["policy_reasons"] == [
        "same_scope_reflect_allowed",
        "same_scope_write_allowed",
    ]
    assert calls[0][1]["tags"] == ["reflection", "session", "sibyl"]
    assert calls[0][1]["memory_scope"] is MemoryScope.PROJECT
    assert calls[0][1]["scope_key"] == "project_123"
    assert calls[1][1]["memory_scope"] is MemoryScope.PROJECT
    assert calls[1][1]["suggested_memory_scope"] is MemoryScope.TEAM
    assert calls[1][1]["suggested_scope_key"] is None
    assert calls[1][1]["extraction_prompt_metadata"]["extractor"] == ("sibyl_reflection_extractor")

    native_reflection_runtime.entity_manager.create_direct_if_absent.assert_not_awaited()
    native_reflection_runtime.relationship_manager.create_bulk.assert_not_awaited()


@pytest.mark.asyncio
async def test_reflect_memory_review_persistence_denies_unverified_project(
    monkeypatch: pytest.MonkeyPatch,
    native_reflection_runtime: SimpleNamespace,
) -> None:
    source_review = AsyncMock(side_effect=AssertionError("source review should not persist"))
    candidate_review = AsyncMock(side_effect=AssertionError("candidate review should not persist"))

    monkeypatch.setattr(
        "sibyl_core.tools.reflect._persist_reflection_source_review",
        source_review,
    )
    monkeypatch.setattr(
        "sibyl_core.tools.reflect._persist_reflection_candidate_review",
        candidate_review,
    )

    pack = await reflect_memory(
        "We decided project review writes need verified membership.",
        source_title="Reflection denial",
        intent="build",
        domain="sibyl",
        project="project_123",
        organization_id="org_123",
        principal_id="user_123",
        accessible_projects={"project_other"},
        memory_scope="project",
        scope_key="project_123",
        persist=True,
        persist_review=True,
    )

    assert pack.source_id is None
    assert pack.persisted_count == 0
    assert pack.candidates[0].metadata["policy_allowed"] is False
    assert pack.candidates[0].metadata["policy_reasons"] == [
        "unverified_membership",
        "unverified_membership",
    ]
    source_review.assert_not_awaited()
    candidate_review.assert_not_awaited()

    native_reflection_runtime.entity_manager.create_direct_if_absent.assert_not_awaited()
    native_reflection_runtime.relationship_manager.create_bulk.assert_not_awaited()


@pytest.mark.asyncio
async def test_reflect_memory_native_write_uses_policy_and_direct_graph(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created_entities = []
    created_relationships = []

    class FakeEntityManager:
        async def create_direct_if_absent(self, entity):
            created_entities.append(entity)
            return entity, True

        async def update(self, entity_id, updates, **kwargs):
            entity = next(entity for entity in created_entities if entity.id == entity_id)
            entity.metadata.update(updates["metadata"])
            return entity

        # Promotion links only targets it can resolve and the writer can see,
        # so the declared link has to exist for the RELATED_TO edge to appear.
        async def get(self, entity_id):
            return Entity(
                id=entity_id,
                entity_type=EntityType.TASK,
                name="Linked task",
                description="",
                content="",
                metadata={"project_id": "project_123"},
            )

    class FakeRelationshipManager:
        async def create_bulk(self, relationships):
            created_relationships.extend(relationships)
            return len(relationships), 0

    async def fake_get_graph_runtime(_organization_id: str):
        return type(
            "Runtime",
            (),
            {
                "entity_manager": FakeEntityManager(),
                "relationship_manager": FakeRelationshipManager(),
            },
        )()

    monkeypatch.setattr(
        "sibyl_core.services.memory_reflection.get_surreal_graph_runtime",
        fake_get_graph_runtime,
    )

    pack = await reflect_memory(
        "We decided native reflection writes should bypass Graphiti add_episode.",
        source_title="Native reflection",
        intent="build",
        domain="sibyl",
        project="project_123",
        related_to=["task_123"],
        organization_id="org_123",
        principal_id="user_123",
        accessible_projects={"project_123"},
        persist=True,
    )

    assert pack.source_id is not None
    assert pack.persisted_count == len(pack.candidates)
    assert len(created_entities) == 2
    assert {entity.entity_type.value for entity in created_entities} == {"session", "decision"}
    assert created_entities[1].metadata["policy_allowed"] is True
    assert created_entities[1].metadata["policy_reasons"] == [
        "same_scope_reflect_allowed",
        "same_scope_write_allowed",
    ]
    assert created_entities[1].metadata["raw_source_ids"] == [pack.source_id]
    assert {relationship.relationship_type.value for relationship in created_relationships} >= {
        "BELONGS_TO",
        "DERIVED_FROM",
        "RELATED_TO",
    }


@pytest.mark.asyncio
async def test_reflect_memory_native_write_denies_unverified_project(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    create_direct = AsyncMock()

    async def fake_get_graph_runtime(_organization_id: str):
        return type(
            "Runtime",
            (),
            {
                "entity_manager": type("EntityManager", (), {"create_direct": create_direct})(),
                "relationship_manager": type(
                    "RelationshipManager",
                    (),
                    {"create_bulk": AsyncMock()},
                )(),
            },
        )()

    monkeypatch.setattr(
        "sibyl_core.services.memory_reflection.get_surreal_graph_runtime",
        fake_get_graph_runtime,
    )

    pack = await reflect_memory(
        "We decided unauthorized project writes must fail closed.",
        source_title="Denied reflection",
        intent="build",
        domain="sibyl",
        project="project_123",
        organization_id="org_123",
        principal_id="user_123",
        accessible_projects={"project_other"},
        persist=True,
    )

    assert pack.source_id is None
    assert pack.persisted_count == 0
    create_direct.assert_not_awaited()
    assert pack.candidates[0].metadata["policy_allowed"] is False
    assert pack.candidates[0].metadata["policy_reasons"] == [
        "unverified_membership",
        "unverified_membership",
    ]


@pytest.mark.asyncio
async def test_reflect_memory_requires_content_and_org_when_persisting() -> None:
    with pytest.raises(ValueError, match="content is required"):
        await reflect_memory("")

    with pytest.raises(ValueError, match="organization_id is required"):
        await reflect_memory("We decided this matters.", persist=True)


@pytest.mark.asyncio
async def test_reflection_pack_serializes_and_renders_markdown() -> None:
    pack = await reflect_memory("We decided to build reflect.", source_title="Planning")

    payload = reflection_pack_to_dict(pack)
    markdown = reflection_pack_to_markdown(pack)

    assert payload["source_title"] == "Planning"
    assert payload["candidates"][0]["kind"] == "decision"
    assert "# Sibyl Reflection: Planning" in markdown
    assert "Source:" not in markdown
    assert "## Decision:" in markdown
