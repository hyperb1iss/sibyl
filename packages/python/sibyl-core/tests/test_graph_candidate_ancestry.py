"""Candidate batches retain current stored ancestor authority."""

import pytest

from sibyl_core.models.entities import Entity, EntityType
from sibyl_core.retrieval.candidates import CandidateScope, RetrievalCandidate
from sibyl_core.services.graph_capture_availability import available_capture_projection_rows
from sibyl_core.tools.helpers import memory_scope_guard
from tests.test_capture_corrections import captured_note
from tests.test_reflection_identity import content_store as content_store
from tests.test_reflection_identity import runtime as runtime


def as_candidate(entity):
    return RetrievalCandidate(
        id=entity.id,
        type=entity.entity_type.value,
        name=entity.name,
        content=entity.content,
        score=1,
        source=None,
        metadata=dict(entity.metadata),
        scope=CandidateScope(organization_id=entity.organization_id),
        source_revision=entity.observed_revision,
    )


@pytest.mark.parametrize("include_child", [False, True])
@pytest.mark.parametrize("row_kind", ["candidate", "entity"])
@pytest.mark.parametrize("change", ["current", "owner", "hidden", "source"])
async def test_candidates_use_current_source_rows_after_policy_change(
    runtime, content_store, monkeypatch, row_kind, change, include_child
):
    memory, parent = await captured_note(runtime, monkeypatch)
    child = Entity(
        id="candidate-child",
        entity_type=EntityType.PASSAGE,
        name="Retained child",
        content=memory.raw_content,
        metadata={"projection_kind": "passage", "parent_entity_id": parent.id},
    )
    ordinary = Entity(id="candidate-ordinary", entity_type=EntityType.NOTE, name="Resource")
    await runtime.entity_manager.create_direct_bulk([child, ordinary], generate_embeddings=False)
    ids = [parent.id, ordinary.id]
    if include_child:
        ids.append(child.id)
    stored = await runtime.entity_manager.get_many(ids)
    rows = {row.id: as_candidate(row) if row_kind == "candidate" else row for row in stored}
    visible = memory_scope_guard(
        principal_id="user_a",
        accessible_projects=set(),
        accessible_teams=set(),
        accessible_delegations=set(),
        allowed_memory_scope_keys=None,
    )

    async def read():
        return await available_capture_projection_rows(
            runtime.client.group_id,
            rows,
            graph_client=runtime.client,
            source_visible=visible,
        )

    assert set(await read()) == set(rows)
    if change == "current":
        return
    updates = {
        "owner": {"principal_id": "other-owner"},
        "hidden": {"excluded_from_recall": True},
        "source": {"raw_memory_id": "missing-canonical-capture"},
    }
    await runtime.entity_manager.update(parent.id, {"metadata": updates[change]})
    available = await read()
    assert parent.id not in available
    assert child.id not in available
    assert ordinary.id in available
    await runtime.entity_manager.update(
        parent.id,
        {
            "metadata": {
                "principal_id": "user_a",
                "excluded_from_recall": False,
                "raw_memory_id": memory.id,
            }
        },
    )
    assert set(await read()) == set(rows)
