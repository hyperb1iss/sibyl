"""Promotion keeps source read access separate from target mutation authority."""

from unittest.mock import AsyncMock

import pytest

from sibyl_core.models.entities import Entity, EntityType
from sibyl_core.models.reflection import ReflectionCandidate
from sibyl_core.services.memory_reflection import persist_reflection_candidate
from tests.test_reflection_identity import content_store as content_store
from tests.test_reflection_identity import runtime as runtime


@pytest.mark.parametrize(
    ("writable", "maintained", "superseded"),
    [
        pytest.param([], [], False, id="no-write-grant"),
        pytest.param(["project_b"], [], False, id="contributor"),
        pytest.param(["project_b"], ["project_b"], True, id="maintainer"),
    ],
)
async def test_project_supersession_takes_correction_authority(
    runtime, content_store, monkeypatch, writable, maintained, superseded
):
    """A row with no recorded owner is someone else's, so superseding it takes maintainer."""
    await runtime.entity_manager.create_direct(
        Entity(
            id="project-target",
            name="Existing decision",
            entity_type=EntityType.DECISION,
            metadata={
                "memory_scope": "project",
                "scope_key": "project_b",
                "project_id": "project_b",
            },
        ),
        generate_embedding=False,
    )
    before = await runtime.entity_manager.get("project-target")
    writes = AsyncMock(wraps=runtime.relationship_manager.create_bulk)
    monkeypatch.setattr(runtime.relationship_manager, "create_bulk", writes)
    result = await persist_reflection_candidate(
        candidate=ReflectionCandidate(
            kind="decision",
            title="New personal decision",
            content="New private guidance",
            reason="Personal update",
            confidence=1.0,
            metadata={"supersedes": ["project-target"]},
        ),
        organization_id=runtime.client.group_id,
        principal_id="owner",
        accessible_projects={"project_b"},
        memory_scope="private",
        writable_projects=(value for value in writable),
        maintained_projects=(value for value in maintained),
        related_to=["project-target"],
    )
    assert result.response.success
    target = await runtime.entity_manager.get("project-target")
    relationships = [edge for call in writes.call_args_list for edge in call.args[0]]
    assert any(
        edge.target_id == "project-target" and edge.relationship_type.value == "RELATED_TO"
        for edge in relationships
    )
    if superseded:
        assert target.observed_revision > before.observed_revision
        assert target.metadata["invalidated_by_entity_id"] == result.response.id
    else:
        assert target.observed_revision == before.observed_revision
        assert "invalidated_by_entity_id" not in target.metadata
        assert all(edge.relationship_type.value != "SUPERSEDES" for edge in relationships)
