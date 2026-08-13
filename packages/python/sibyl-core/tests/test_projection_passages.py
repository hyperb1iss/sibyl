from __future__ import annotations

from typing import Any

import pytest

from sibyl_core.memory_pipeline.spans import (
    AGENT_ATOMIC_METADATA_KEY,
    AGENT_SPANS_METADATA_KEY,
)
from sibyl_core.models.entities import Entity, EntityType, Relationship, RelationshipType
from sibyl_core.projection.passages import (
    MAX_PASSAGE_CONTENT_CHARS,
    MAX_PASSAGES_PER_SOURCE,
    PASSAGE_COVERS_PARENT_KEY,
    PASSAGE_MIN_SOURCE_CHARS,
    PASSAGE_PLAN_AGENT,
    PASSAGE_PLAN_KEY,
    PASSAGE_PLAN_MECHANICAL,
    PassageProjectionResult,
    passage_entity_id,
    plan_entity_passages,
    project_entity_passages,
    reproject_entity_passages,
    restamp_entity_passages,
    retire_entity_passages,
    scope_bearing_entity_update,
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


def test_passages_do_not_inflate_what_a_reader_is_served() -> None:
    """Spans replace the parent in a pack, so together they must not cost more.

    Each passage repeats a locator header and its breadcrumb trail, which is
    real overhead. It has to stay small: if the projection served materially
    more text than the body it replaced, slicing would be a regression dressed
    as precision rather than the point of the substrate.
    """
    body = _prose(sections=8)
    entities, _ = plan_entity_passages(_source(content=body), source_id=_SOURCE_ID, group_id=_GROUP)

    served = sum(len(entity.content) for entity in entities)

    assert len(entities) > 2
    # The body itself is always fully covered; only framing is added.
    assert served >= len(body.strip())
    assert served <= len(body) * 1.35


def test_every_line_of_the_body_survives_into_some_passage() -> None:
    """The parent keeps the body, but a span reader must not silently lose lines."""
    body = _prose(sections=8)
    entities, _ = plan_entity_passages(_source(content=body), source_id=_SOURCE_ID, group_id=_GROUP)

    joined = "\n".join(entity.content for entity in entities)
    missing = [line for line in body.split("\n") if line.strip() and line.strip() not in joined]

    assert missing == []


def test_a_parent_from_another_tenant_is_refused() -> None:
    """Namespace-per-org should make this unreachable; fail loudly if it is not."""
    foreign = Entity(
        id=_SOURCE_ID,
        entity_type=EntityType.DECISION,
        name="someone else's decision",
        content=_prose(),
        organization_id="org-somebody-else",
    )

    with pytest.raises(ValueError, match="not org-passages"):
        plan_entity_passages(foreign, source_id=_SOURCE_ID, group_id=_GROUP)


def test_a_complete_projection_may_stand_in_for_its_parent() -> None:
    entities, _ = plan_entity_passages(_source(), source_id=_SOURCE_ID, group_id=_GROUP)

    assert entities
    assert all(entity.metadata[PASSAGE_COVERS_PARENT_KEY] is True for entity in entities)


def test_a_capped_projection_is_marked_as_not_covering_its_parent() -> None:
    """Above the cap some spans have no row, so the survivors cannot hide the body."""
    entities, _ = plan_entity_passages(
        _source(content=_prose(sections=400)),
        source_id=_SOURCE_ID,
        group_id=_GROUP,
    )

    assert len(entities) == MAX_PASSAGES_PER_SOURCE
    assert all(entity.metadata[PASSAGE_COVERS_PARENT_KEY] is False for entity in entities)


def test_a_skipped_oversize_span_marks_every_sibling_incomplete() -> None:
    """Coverage is only known after the whole body is walked, including the tail."""
    giant_line = "x" * (MAX_PASSAGE_CONTENT_CHARS + 500)
    body = "\n".join(["# Doc", "", _prose(sections=4), "", "## Tail", "", giant_line])

    entities, _ = plan_entity_passages(_source(content=body), source_id=_SOURCE_ID, group_id=_GROUP)

    assert entities
    assert all(entity.metadata[PASSAGE_COVERS_PARENT_KEY] is False for entity in entities)


class _ReprojectEntityManager(_RecordingEntityManager):
    """An entity manager that remembers a previous, longer projection."""

    def __init__(self, existing_indices: set[int]) -> None:
        super().__init__()
        self.existing = set(existing_indices)
        self.deleted: list[str] = []

    async def delete(self, entity_id: str) -> bool:
        for index in sorted(self.existing):
            if passage_entity_id(_SOURCE_ID, index) == entity_id:
                self.existing.discard(index)
                self.deleted.append(entity_id)
                return True
        return False


@pytest.mark.asyncio
async def test_reprojection_retires_spans_the_new_cut_no_longer_uses() -> None:
    """A shortened body would otherwise strand old spans still serving old text."""
    entity_manager = _ReprojectEntityManager(existing_indices={0, 1, 2, 3, 4, 5})
    relationship_manager = _RecordingRelationshipManager()

    result = await reproject_entity_passages(
        entity_manager=entity_manager,
        relationship_manager=relationship_manager,
        source=_source(content=_prose(sections=6)),
        group_id=_GROUP,
        created_source_id=_SOURCE_ID,
    )

    assert 2 <= result.passages < 6
    expected_retired = [passage_entity_id(_SOURCE_ID, index) for index in range(result.passages, 6)]
    assert entity_manager.deleted == expected_retired


@pytest.mark.asyncio
async def test_reprojection_keeps_going_when_there_is_nothing_stale() -> None:
    entity_manager = _ReprojectEntityManager(existing_indices=set())
    relationship_manager = _RecordingRelationshipManager()

    result = await reproject_entity_passages(
        entity_manager=entity_manager,
        relationship_manager=relationship_manager,
        source=_source(),
        group_id=_GROUP,
        created_source_id=_SOURCE_ID,
    )

    assert result.passages > 1
    assert entity_manager.deleted == []


@pytest.mark.asyncio
async def test_shrinking_a_memory_below_the_threshold_retires_all_its_spans() -> None:
    """Otherwise a now-short memory keeps serving spans of the body it used to have."""
    entity_manager = _ReprojectEntityManager(existing_indices={0, 1, 2})
    relationship_manager = _RecordingRelationshipManager()

    result = await reproject_entity_passages(
        entity_manager=entity_manager,
        relationship_manager=relationship_manager,
        source=_source(content="a short body now"),
        group_id=_GROUP,
        created_source_id=_SOURCE_ID,
    )

    assert result.passages == 0
    assert result.skipped is True
    assert entity_manager.deleted == [passage_entity_id(_SOURCE_ID, i) for i in range(3)]
    assert entity_manager.existing == set()


@pytest.mark.asyncio
async def test_deleting_a_memory_retires_every_span_cut_from_it() -> None:
    """A span outliving its parent keeps serving text the caller deleted."""
    entity_manager = _ReprojectEntityManager(existing_indices={0, 1, 2, 3})

    retired = await retire_entity_passages(
        entity_manager=entity_manager,
        source_id=_SOURCE_ID,
    )

    assert retired == 4
    assert entity_manager.existing == set()
    assert entity_manager.deleted == [passage_entity_id(_SOURCE_ID, i) for i in range(4)]


@pytest.mark.asyncio
async def test_deleting_a_memory_retires_spans_sitting_past_a_hole() -> None:
    """The deleted memory's text must stop being served even when index 0 is absent.

    An unbreakable oversize line is emitted as a slice and then skipped, so a
    previous projection can leave the run starting at 1. A sweep that reads the
    absence at 0 as the end of the run deletes nothing at all, and every span of
    the deleted memory keeps answering searches under a current id.
    """
    entity_manager = _ReprojectEntityManager(existing_indices={1, 2, 3})

    retired = await retire_entity_passages(
        entity_manager=entity_manager,
        source_id=_SOURCE_ID,
    )

    assert retired == 3
    assert entity_manager.existing == set()


@pytest.mark.asyncio
async def test_reprojection_retires_stale_spans_sitting_past_a_hole() -> None:
    """A body too short to project must still take its old spans down with it."""
    entity_manager = _ReprojectEntityManager(existing_indices={1, 2})
    relationship_manager = _RecordingRelationshipManager()

    result = await reproject_entity_passages(
        entity_manager=entity_manager,
        relationship_manager=relationship_manager,
        source=_source(content="a short body now"),
        group_id=_GROUP,
        created_source_id=_SOURCE_ID,
    )

    assert result.passages == 0
    assert entity_manager.existing == set()


@pytest.mark.asyncio
async def test_a_failed_delete_does_not_strand_the_rest_of_the_sweep() -> None:
    """One transport blip must not leave later spans serving a deleted memory."""

    class _FlakyEntityManager(_ReprojectEntityManager):
        async def delete(self, entity_id: str) -> bool:
            if entity_id == passage_entity_id(_SOURCE_ID, 1):
                raise RuntimeError("transport blip")
            return await super().delete(entity_id)

    entity_manager = _FlakyEntityManager(existing_indices={0, 1, 2})

    retired = await retire_entity_passages(
        entity_manager=entity_manager,
        source_id=_SOURCE_ID,
    )

    assert retired == 2
    assert entity_manager.existing == {1}


@pytest.mark.asyncio
async def test_retiring_a_memory_that_never_had_spans_is_a_no_op() -> None:
    entity_manager = _ReprojectEntityManager(existing_indices=set())

    retired = await retire_entity_passages(
        entity_manager=entity_manager,
        source_id=_SOURCE_ID,
    )

    assert retired == 0
    assert entity_manager.deleted == []


# ---------------------------------------------------------------------------
# Agent-authored cut plans
# ---------------------------------------------------------------------------


def _agent_metadata(*pairs: tuple[int, int, str | None]) -> dict[str, Any]:
    return {
        AGENT_SPANS_METADATA_KEY: [
            {"start": start, "end": end, **({"label": label} if label else {})}
            for start, end, label in pairs
        ]
    }


def test_agent_spans_are_honored_verbatim_and_differ_from_the_cutter() -> None:
    """The writer's seams win, and the passage text is the parent's own bytes."""
    body = _prose()
    third = len(body) // 3
    source = _source(
        content=body,
        metadata=_agent_metadata(
            (0, third, "Opening"),
            (third, third * 2, "Middle"),
            (third * 2, len(body), "Close"),
        ),
    )

    entities, relationships = plan_entity_passages(source, source_id=_SOURCE_ID, group_id=_GROUP)

    assert len(entities) == 3
    assert len(relationships) == 3
    expected = [body[0:third], body[third : third * 2], body[third * 2 :]]
    for entity, span_text in zip(entities, expected, strict=True):
        assert entity.content.endswith(span_text)
        assert span_text in entity.content
    assert "".join(expected) == body

    mechanical, _ = plan_entity_passages(
        _source(content=body), source_id=_SOURCE_ID, group_id=_GROUP
    )
    assert [entity.content for entity in entities] != [entity.content for entity in mechanical]


def test_agent_passages_stamp_the_same_contract_as_mechanical_ones() -> None:
    body = _prose()
    half = len(body) // 2
    source = _source(
        content=body, metadata=_agent_metadata((0, half, None), (half, len(body), None))
    )

    entities, _ = plan_entity_passages(source, source_id=_SOURCE_ID, group_id=_GROUP)

    assert [entity.metadata["passage_index"] for entity in entities] == [0, 1]
    assert {entity.metadata["passage_total"] for entity in entities} == {2}
    assert all(entity.metadata[PASSAGE_COVERS_PARENT_KEY] is True for entity in entities)
    assert all(entity.metadata[PASSAGE_PLAN_KEY] == PASSAGE_PLAN_AGENT for entity in entities)
    assert all(entity.metadata["passage_cut_reason"] == "agent-span" for entity in entities)


def test_mechanical_passages_say_so_in_their_plan_stamp() -> None:
    entities, _ = plan_entity_passages(_source(), source_id=_SOURCE_ID, group_id=_GROUP)

    assert all(entity.metadata[PASSAGE_PLAN_KEY] == PASSAGE_PLAN_MECHANICAL for entity in entities)


def test_a_span_label_is_carried_in_the_passage_body() -> None:
    """Labels are indexed text, so they have to reach the stored content."""
    body = _prose()
    half = len(body) // 2
    source = _source(
        content=body,
        metadata=_agent_metadata((0, half, "Root cause"), (half, len(body), "Remediation")),
    )

    entities, _ = plan_entity_passages(source, source_id=_SOURCE_ID, group_id=_GROUP)

    assert "Root cause" in entities[0].content
    assert "Remediation" in entities[1].content
    assert entities[0].metadata["passage_breadcrumb"] == "Root cause"


def test_an_atomic_memory_is_never_cut_however_long_it_is() -> None:
    source = _source(content=_prose(sections=12), metadata={AGENT_ATOMIC_METADATA_KEY: True})

    assert should_project_passages(source) is False
    assert plan_entity_passages(source, source_id=_SOURCE_ID, group_id=_GROUP) == ([], [])


def test_agent_spans_outrank_the_size_threshold() -> None:
    """The threshold exists because the cutter guesses; here it does not."""
    body = "one two three four five six seven eight"
    assert len(body) < PASSAGE_MIN_SOURCE_CHARS
    source = _source(content=body, metadata=_agent_metadata((0, 15, None), (15, len(body), None)))

    assert should_project_passages(source) is True
    entities, _ = plan_entity_passages(source, source_id=_SOURCE_ID, group_id=_GROUP)
    assert len(entities) == 2


def test_a_stored_plan_that_no_longer_tiles_the_body_falls_back_to_the_cutter() -> None:
    """A body rewritten behind the plan must not be cut on stale offsets."""
    body = _prose()
    source = _source(content=body, metadata=_agent_metadata((0, 40, None), (40, 90, None)))

    entities, _ = plan_entity_passages(source, source_id=_SOURCE_ID, group_id=_GROUP)

    assert entities
    assert all(entity.metadata[PASSAGE_PLAN_KEY] == PASSAGE_PLAN_MECHANICAL for entity in entities)


def test_passages_do_not_inherit_their_parents_plan_or_probes() -> None:
    body = _prose()
    half = len(body) // 2
    source = _source(
        content=body,
        metadata={
            **_agent_metadata((0, half, None), (half, len(body), None)),
            AGENT_ATOMIC_METADATA_KEY: False,
            "memory_probes": ["why"],
            "memory_scope": "private",
        },
    )

    entities, _ = plan_entity_passages(source, source_id=_SOURCE_ID, group_id=_GROUP)

    for entity in entities:
        assert AGENT_SPANS_METADATA_KEY not in entity.metadata
        assert "memory_probes" not in entity.metadata
        assert entity.metadata["memory_scope"] == "private"


@pytest.mark.asyncio
async def test_reprojection_keeps_a_span_written_past_a_skipped_index() -> None:
    """A hole in the index run must not make the retire sweep eat a live span.

    An unbroken line past the row ceiling is emitted as its own slice and then
    skipped, so the written indices are not contiguous. Retiring by count would
    treat the highest written index as stale and delete the span that had just
    been minted, leaving that text reachable only through the parent.
    """
    giant_line = "x" * (MAX_PASSAGE_CONTENT_CHARS + 500)
    body = "\n".join(
        [
            "# Root",
            "",
            "## First",
            "",
            " ".join(["alpha"] * 200),
            "",
            "## Huge",
            "",
            giant_line,
            "",
            "## Last",
            "",
            " ".join(["omega"] * 200),
            "",
        ]
    )
    source = _source(content=body)
    entities, _ = plan_entity_passages(source, source_id=_SOURCE_ID, group_id=_GROUP)
    written = [entity.metadata["passage_index"] for entity in entities]
    assert written != list(range(len(written))), "probe needs a skipped index to be meaningful"

    entity_manager = _ReprojectEntityManager(existing_indices=set(written))
    result = await reproject_entity_passages(
        entity_manager=entity_manager,
        relationship_manager=_RecordingRelationshipManager(),
        source=source,
        group_id=_GROUP,
        created_source_id=_SOURCE_ID,
    )

    assert result.passages == len(written)
    survivors = {
        entity.metadata["passage_index"]
        for entity in entity_manager.created
        if passage_entity_id(_SOURCE_ID, entity.metadata["passage_index"])
        not in entity_manager.deleted
    }
    assert survivors == set(written)


def test_a_withdrawn_plan_in_a_stale_snapshot_is_not_read_back() -> None:
    """A row keeps its metadata twice; only the flattened copy may speak here."""
    import json

    from sibyl_core.services.graph import entity_from_surreal_row

    body = _prose()
    stale_plan = [{"start": 0, "end": 40, "label": "Old"}, {"start": 40, "end": len(body)}]
    row = {
        "uuid": _SOURCE_ID,
        "name": "A long decision",
        "entity_type": "decision",
        "description": "short blurb",
        "content": body,
        "group_id": _GROUP,
        "attributes": {
            # The flattened copy no longer carries the plan; the snapshot still does.
            "memory_scope": "private",
            "metadata": json.dumps(
                {
                    "memory_scope": "private",
                    "agent_spans": stale_plan,
                    "probe_rehearsal": {"retrievable": 1},
                    "keep_me": "kept",
                }
            ),
        },
    }

    entity = entity_from_surreal_row(row)

    assert entity.metadata["keep_me"] == "kept"
    assert "agent_spans" not in entity.metadata
    assert "probe_rehearsal" not in entity.metadata
    entities, _ = plan_entity_passages(entity, source_id=_SOURCE_ID, group_id=_GROUP)
    assert entities
    assert all(
        passage.metadata[PASSAGE_PLAN_KEY] == PASSAGE_PLAN_MECHANICAL for passage in entities
    )


class _ScopedStoreEntityManager:
    """Get/update over a dict of entities, mirroring update MERGE semantics."""

    def __init__(self, entities: dict[str, Entity]) -> None:
        self.entities = dict(entities)
        self.updates: list[tuple[str, dict[str, Any]]] = []

    async def get(self, entity_id: str) -> Entity:
        try:
            return self.entities[entity_id]
        except KeyError:
            raise KeyError(entity_id) from None

    async def update(self, entity_id: str, updates: dict[str, Any]) -> Entity:
        self.updates.append((entity_id, updates))
        entity = self.entities[entity_id]
        metadata = dict(entity.metadata or {})
        for key, value in (updates.get("metadata") or {}).items():
            if value is None:
                metadata.pop(key, None)
            else:
                metadata[key] = value
        updated = entity.model_copy(update={"metadata": metadata})
        self.entities[entity_id] = updated
        return updated


def _project_scoped_source() -> Entity:
    return _source(
        metadata={
            "memory_scope": "project",
            "scope_key": "project_x",
            "project_id": "project_x",
            "principal_id": "user-1",
        }
    )


async def _spans_in_store(source: Entity) -> _ScopedStoreEntityManager:
    entities, _ = plan_entity_passages(source, source_id=_SOURCE_ID, group_id=_GROUP)
    assert entities
    return _ScopedStoreEntityManager({entity.id: entity for entity in entities})


def test_scope_bearing_update_detection() -> None:
    assert scope_bearing_entity_update({"memory_scope": "private"}) is True
    assert scope_bearing_entity_update({"metadata": {"scope_key": "p"}}) is True
    assert scope_bearing_entity_update({"metadata": {"project_id": None}}) is True
    assert scope_bearing_entity_update({"name": "renamed"}) is False
    assert scope_bearing_entity_update({"metadata": {"category": "note"}}) is False
    assert scope_bearing_entity_update({"content": "new body"}) is False


@pytest.mark.asyncio
async def test_a_tightened_parent_restamps_its_spans_private() -> None:
    """Tighten parent to private: a stranger's search gate must refuse its spans."""
    from sibyl_core.auth.memory_policy import memory_metadata_read_allowed

    manager = await _spans_in_store(_project_scoped_source())
    tightened = _source(
        metadata={
            "memory_scope": "private",
            "principal_id": "user-1",
        }
    )

    restamped = await restamp_entity_passages(
        entity_manager=manager,
        source=tightened,
        created_source_id=_SOURCE_ID,
    )

    assert restamped == len(manager.entities)
    for span in manager.entities.values():
        assert span.metadata["memory_scope"] == "private"
        assert span.metadata["principal_id"] == "user-1"
        # The project audience the parent left behind must leave the spans too.
        assert "scope_key" not in span.metadata
        assert "project_id" not in span.metadata
        # The gate search and traversal share: owner reads, a stranger does not.
        assert (
            memory_metadata_read_allowed(
                span.metadata,
                principal_id="user-1",
                private_scope_granted=True,
                accessible_projects={"project_x"},
            )
            is True
        )
        assert (
            memory_metadata_read_allowed(
                span.metadata,
                principal_id="user-2",
                private_scope_granted=True,
                accessible_projects={"project_x"},
            )
            is False
        )


@pytest.mark.asyncio
async def test_matching_stamps_are_left_unwritten() -> None:
    source = _project_scoped_source()
    manager = await _spans_in_store(source)

    restamped = await restamp_entity_passages(
        entity_manager=manager,
        source=source,
        created_source_id=_SOURCE_ID,
    )

    assert restamped == 0
    assert manager.updates == []


@pytest.mark.asyncio
async def test_restamp_reaches_spans_past_an_index_gap() -> None:
    """The oversize-leaf branch can skip an index; stale rows past it still count."""
    manager = await _spans_in_store(_project_scoped_source())
    dropped = passage_entity_id(_SOURCE_ID, 0)
    del manager.entities[dropped]
    survivors = len(manager.entities)
    tightened = _source(metadata={"memory_scope": "private", "principal_id": "user-1"})

    restamped = await restamp_entity_passages(
        entity_manager=manager,
        source=tightened,
        created_source_id=_SOURCE_ID,
    )

    assert restamped == survivors


@pytest.mark.asyncio
async def test_a_failed_restamp_write_recovers_through_reprojection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A partial restamp can never re-fire, so recovery must happen now.

    The route trigger diffs pre/post stamp projections; once the parent
    carries the new stamps that diff is empty on every later edit, and a span
    the failed write left behind would serve the old audience indefinitely.
    """
    from sibyl_core.projection import passages as passages_module

    manager = await _spans_in_store(_project_scoped_source())

    first_span_id = sorted(manager.entities)[0]
    original_update = manager.update

    async def failing_update(entity_id: str, updates: dict[str, Any]) -> Entity:
        if entity_id == first_span_id:
            raise RuntimeError("surreal write refused")
        return await original_update(entity_id, updates)

    manager.update = failing_update  # type: ignore[method-assign]

    reproject_calls: list[dict[str, Any]] = []

    async def fake_reproject(**kwargs: Any) -> PassageProjectionResult:
        reproject_calls.append(kwargs)
        return PassageProjectionResult(source_id=kwargs["created_source_id"], passages=4)

    monkeypatch.setattr(passages_module, "reproject_entity_passages", fake_reproject)

    tightened = _source(metadata={"memory_scope": "private", "principal_id": "user-1"})
    relationship_manager = _RecordingRelationshipManager()

    restamped = await restamp_entity_passages(
        entity_manager=manager,
        source=tightened,
        created_source_id=_SOURCE_ID,
        relationship_manager=relationship_manager,
        group_id=_GROUP,
    )

    assert len(reproject_calls) == 1
    recovery = reproject_calls[0]
    assert recovery["source"] is tightened
    assert recovery["group_id"] == _GROUP
    assert recovery["created_source_id"] == _SOURCE_ID
    assert recovery["relationship_manager"] is relationship_manager
    assert restamped == 4


@pytest.mark.asyncio
async def test_a_failed_restamp_without_recovery_managers_keeps_walking() -> None:
    """Callers that cannot reproject still get every span the walk can fix."""
    manager = await _spans_in_store(_project_scoped_source())

    first_span_id = sorted(manager.entities)[0]
    original_update = manager.update

    async def failing_update(entity_id: str, updates: dict[str, Any]) -> Entity:
        if entity_id == first_span_id:
            raise RuntimeError("surreal write refused")
        return await original_update(entity_id, updates)

    manager.update = failing_update  # type: ignore[method-assign]

    tightened = _source(metadata={"memory_scope": "private", "principal_id": "user-1"})

    restamped = await restamp_entity_passages(
        entity_manager=manager,
        source=tightened,
        created_source_id=_SOURCE_ID,
    )

    assert restamped == len(manager.entities) - 1
