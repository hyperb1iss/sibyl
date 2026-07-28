from __future__ import annotations

from typing import Any

import pytest

from sibyl_core.models.entities import Entity, EntityType, Relationship, RelationshipType
from sibyl_core.projection.passages import (
    MAX_PASSAGES_PER_SOURCE,
    PASSAGE_MIN_SOURCE_CHARS,
    passage_entity_id,
    plan_entity_passages,
    project_entity_passages,
    should_project_passages,
)

_GROUP = "org-passages"
_SOURCE_ID = "decision_abc123"


def _prose(sections: int = 6, words: int = 60) -> str:
    lines: list[str] = ["# Root", ""]
    for index in range(sections):
        lines.extend(
            [
                f"## Section {index}",
                "",
                " ".join([f"word{index}"] * words),
                "",
            ]
        )
    return "\n".join(lines)


def _source(
    *,
    content: str | None = None,
    entity_type: EntityType = EntityType.DECISION,
    metadata: dict[str, Any] | None = None,
) -> Entity:
    return Entity(
        id=_SOURCE_ID,
        entity_type=entity_type,
        name="A long decision",
        description="short blurb",
        content=_prose() if content is None else content,
        organization_id=_GROUP,
        created_by="user-1",
        metadata=metadata if metadata is not None else {},
    )


class _RecordingEntityManager:
    def __init__(self, *, fail: bool = False) -> None:
        self.created: list[Entity] = []
        self.calls: list[str] = []
        self._fail = fail

    async def create_direct_bulk(
        self,
        entities: list[Entity],
        *,
        generate_embeddings: bool = True,
    ) -> list[str]:
        self.calls.append("entities")
        if self._fail:
            msg = "surreal is unhappy"
            raise RuntimeError(msg)
        self.created.extend(entities)
        return [entity.id for entity in entities]


class _RecordingRelationshipManager:
    def __init__(self, *, fail: bool = False) -> None:
        self.created: list[Relationship] = []
        self.calls: list[str] = []
        self._fail = fail

    async def create_direct_bulk(
        self,
        relationships: list[Relationship],
        *,
        generate_embeddings: bool = True,
    ) -> list[str]:
        self.calls.append("relationships")
        if self._fail:
            msg = "edge write refused"
            raise RuntimeError(msg)
        self.created.extend(relationships)
        return [relationship.id for relationship in relationships]


def test_a_body_inside_the_band_is_left_whole() -> None:
    source = _source(content="short body")

    assert should_project_passages(source) is False
    assert plan_entity_passages(source, source_id=_SOURCE_ID, group_id=_GROUP) == ([], [])


def test_the_threshold_is_the_packers_own_ceiling() -> None:
    under = _source(content="x" * PASSAGE_MIN_SOURCE_CHARS)
    over = _source(content=_prose())

    assert should_project_passages(under) is False
    assert should_project_passages(over) is True


def test_work_items_are_not_sliced() -> None:
    """A task's content mirrors a short description; it has no body to cut."""
    task = _source(entity_type=EntityType.TASK)

    assert should_project_passages(task) is False


def test_a_passage_is_never_sliced_again() -> None:
    passage = _source(entity_type=EntityType.PASSAGE)

    assert should_project_passages(passage) is False


def test_a_body_that_yields_one_slice_earns_no_passage() -> None:
    """One passage would just be the parent again, with an embedding to pay for."""
    single = "word " * 500

    entities, relationships = plan_entity_passages(
        _source(content=single),
        source_id=_SOURCE_ID,
        group_id=_GROUP,
    )

    assert entities == []
    assert relationships == []


def test_passages_carry_distinct_bodies_and_descriptions() -> None:
    entities, _ = plan_entity_passages(_source(), source_id=_SOURCE_ID, group_id=_GROUP)

    assert len(entities) > 1
    assert len({entity.content for entity in entities}) == len(entities)
    # A shared blurb across every passage is what made them indistinguishable
    # to description-reading resolvers before 07921a5e.
    assert len({entity.description for entity in entities}) == len(entities)


def test_every_passage_points_back_at_its_parent() -> None:
    entities, relationships = plan_entity_passages(_source(), source_id=_SOURCE_ID, group_id=_GROUP)

    assert len(relationships) == len(entities)
    assert {relationship.target_id for relationship in relationships} == {_SOURCE_ID}
    assert {relationship.source_id for relationship in relationships} == {
        entity.id for entity in entities
    }
    assert all(
        relationship.relationship_type is RelationshipType.PART_OF for relationship in relationships
    )


def test_passage_ids_are_stable_across_replanning() -> None:
    first, _ = plan_entity_passages(_source(), source_id=_SOURCE_ID, group_id=_GROUP)
    second, _ = plan_entity_passages(_source(), source_id=_SOURCE_ID, group_id=_GROUP)

    assert [entity.id for entity in first] == [entity.id for entity in second]
    assert first[0].id == passage_entity_id(_SOURCE_ID, 0)


def test_passages_inherit_the_parents_scope() -> None:
    """A passage that loses its scope either leaks or vanishes from packs."""
    source = _source(
        metadata={
            "memory_scope": "project",
            "scope_key": "project_xyz",
            "project_id": "project_xyz",
            "principal_id": "user-1",
            "unrelated": "dropped",
        }
    )

    entities, relationships = plan_entity_passages(source, source_id=_SOURCE_ID, group_id=_GROUP)

    for entity in entities:
        assert entity.metadata["memory_scope"] == "project"
        assert entity.metadata["scope_key"] == "project_xyz"
        assert entity.metadata["project_id"] == "project_xyz"
        assert entity.metadata["principal_id"] == "user-1"
        assert "unrelated" not in entity.metadata
    for relationship in relationships:
        assert relationship.metadata["memory_scope"] == "project"
        assert relationship.metadata["scope_key"] == "project_xyz"


def test_a_private_parent_yields_private_passages() -> None:
    source = _source(metadata={"memory_scope": "private", "principal_id": "user-1"})

    entities, _ = plan_entity_passages(source, source_id=_SOURCE_ID, group_id=_GROUP)

    assert entities
    assert all(entity.metadata["memory_scope"] == "private" for entity in entities)


def test_passage_count_is_bounded() -> None:
    entities, _ = plan_entity_passages(
        _source(content=_prose(sections=400)),
        source_id=_SOURCE_ID,
        group_id=_GROUP,
    )

    assert 0 < len(entities) <= MAX_PASSAGES_PER_SOURCE
    assert all(entity.metadata["passage_total"] == len(entities) for entity in entities)


@pytest.mark.asyncio
async def test_entities_are_written_before_the_edges_that_point_at_them() -> None:
    """A PART_OF edge to a row that is not there yet is dropped and never heals."""
    entity_manager = _RecordingEntityManager()
    relationship_manager = _RecordingRelationshipManager()

    result = await project_entity_passages(
        entity_manager=entity_manager,
        relationship_manager=relationship_manager,
        source=_source(),
        group_id=_GROUP,
        created_source_id=_SOURCE_ID,
    )

    assert result.passages > 1
    assert result.relationships == result.passages
    assert entity_manager.calls + relationship_manager.calls == ["entities", "relationships"]


@pytest.mark.asyncio
async def test_a_failed_passage_write_does_not_lose_the_memory() -> None:
    """The parent already holds the whole body; the loss is retrievability."""
    entity_manager = _RecordingEntityManager(fail=True)
    relationship_manager = _RecordingRelationshipManager()

    result = await project_entity_passages(
        entity_manager=entity_manager,
        relationship_manager=relationship_manager,
        source=_source(),
        group_id=_GROUP,
        created_source_id=_SOURCE_ID,
    )

    assert result.passages == 0
    assert result.errors
    assert result.reason == "entity_write_failed"
    assert relationship_manager.created == []


@pytest.mark.asyncio
async def test_a_failed_edge_write_keeps_the_passages_and_reports_it() -> None:
    entity_manager = _RecordingEntityManager()
    relationship_manager = _RecordingRelationshipManager(fail=True)

    result = await project_entity_passages(
        entity_manager=entity_manager,
        relationship_manager=relationship_manager,
        source=_source(),
        group_id=_GROUP,
        created_source_id=_SOURCE_ID,
    )

    assert result.passages > 1
    assert result.relationships == 0
    assert result.errors


@pytest.mark.asyncio
async def test_a_short_memory_skips_the_projection_entirely() -> None:
    entity_manager = _RecordingEntityManager()
    relationship_manager = _RecordingRelationshipManager()

    result = await project_entity_passages(
        entity_manager=entity_manager,
        relationship_manager=relationship_manager,
        source=_source(content="short"),
        group_id=_GROUP,
        created_source_id=_SOURCE_ID,
    )

    assert result.skipped is True
    assert result.reason == "below_threshold"
    assert entity_manager.calls == []
    assert relationship_manager.calls == []
