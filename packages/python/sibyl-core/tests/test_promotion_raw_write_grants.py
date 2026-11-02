"""Promotion retires someone else's memory only with the authority a correction needs.

Superseding or contradicting a capture stamps ``invalid_at`` on it and recall
stops serving it, which is what ``mark_stale`` does. So the rule is the
correction rule: the author may, given project write access, and anyone else
needs project maintainer on the capture's project.
"""

import pytest

from sibyl_core.models.entities import Entity, EntityType
from sibyl_core.models.reflection import ReflectionCandidate
from sibyl_core.services.memory_reflection import persist_reflection_candidate, promote_raw_memory
from sibyl_core.services.memory_sharing import share_memory
from sibyl_core.services.surreal_content import get_raw_memory, remember_raw_memory
from tests.test_reflection_identity import content_store as content_store
from tests.test_reflection_identity import runtime as runtime

_PROJECT = "project_b"
_PROMOTER = "owner"

# (target author, contributor grants, maintainer grants, may retire)
_CORRECTION_CASES = [
    pytest.param("source-author", set(), set(), False, id="no-write-grant"),
    pytest.param("source-author", {_PROJECT}, set(), False, id="contributor"),
    pytest.param("source-author", {_PROJECT}, {"project_a"}, False, id="maintainer-elsewhere"),
    pytest.param("source-author", {_PROJECT}, {_PROJECT}, True, id="maintainer"),
    pytest.param(_PROMOTER, {_PROJECT}, set(), True, id="author"),
    pytest.param(_PROMOTER, set(), set(), False, id="author-without-write"),
]


async def _project_entity(runtime, *, owner: str, entity_id: str) -> Entity:
    entity = Entity(
        id=entity_id,
        entity_type=EntityType.DECISION,
        name="Prior project decision",
        content="Use the original project convention.",
        created_by=owner,
        metadata={"memory_scope": "project", "scope_key": _PROJECT, "project_id": _PROJECT},
    )
    await runtime.entity_manager.create_direct(entity)
    return entity


async def _project_capture(runtime, *, author: str, promoted_entity_id: str | None = None):
    metadata = {"project_id": _PROJECT}
    if promoted_entity_id:
        metadata["promoted_entity_id"] = promoted_entity_id
    return await remember_raw_memory(
        organization_id=runtime.client.group_id,
        principal_id=author,
        source_id=f"project-decision-{author}",
        title="Existing project guidance",
        raw_content="Use the original project convention.",
        memory_scope="project",
        scope_key=_PROJECT,
        metadata=metadata,
        embedding_provider=None,
    )


def _replacement(metadata: dict[str, object]) -> ReflectionCandidate:
    return ReflectionCandidate(
        kind="decision",
        title="Updated personal guidance",
        content="Use the revised convention.",
        reason="A new conclusion",
        confidence=1.0,
        metadata=metadata,
    )


@pytest.mark.parametrize("key", ["supersedes_source_ids", "contradicts_source_ids"])
@pytest.mark.parametrize(("author", "writable", "maintained", "retired"), _CORRECTION_CASES)
async def test_retiring_a_project_capture_takes_correction_authority(
    runtime, content_store, key, author, writable, maintained, retired
):
    prior = await _project_entity(runtime, owner=author, entity_id="decision_prior")
    source = await _project_capture(runtime, author=author, promoted_entity_id=prior.id)
    result = await persist_reflection_candidate(
        candidate=_replacement({key: [source.id]}),
        organization_id=runtime.client.group_id,
        principal_id=_PROMOTER,
        accessible_projects={_PROJECT},
        writable_projects=writable,
        maintained_projects=maintained,
        memory_scope="private",
    )

    assert result.response.success
    current = await get_raw_memory(organization_id=source.organization_id, memory_id=source.id)
    stored_prior = await runtime.entity_manager.get(prior.id)
    assert current is not None
    if retired:
        assert current.metadata["invalidated_by_entity_id"] == result.response.id
        assert result.metadata["invalidated_source_ids"] == [source.id]
        assert result.metadata["invalidated_entity_ids"] == [prior.id]
        assert stored_prior.metadata["invalidated_by_entity_id"] == result.response.id
    else:
        assert current == source
        assert result.metadata["invalidation_skipped_source_ids"] == [source.id]
        assert result.metadata["invalidated_entity_ids"] == []
        assert "invalid_at" not in stored_prior.metadata


@pytest.mark.parametrize(("owner", "writable", "maintained", "retired"), _CORRECTION_CASES)
async def test_superseding_a_project_entity_takes_correction_authority(
    runtime, content_store, owner, writable, maintained, retired
):
    prior = await _project_entity(runtime, owner=owner, entity_id="decision_prior")
    result = await persist_reflection_candidate(
        candidate=_replacement({"supersedes_entity_ids": [prior.id]}),
        organization_id=runtime.client.group_id,
        principal_id=_PROMOTER,
        accessible_projects={_PROJECT},
        writable_projects=writable,
        maintained_projects=maintained,
        memory_scope="private",
    )

    assert result.response.success
    created = await runtime.entity_manager.get(result.response.id)
    stored_prior = await runtime.entity_manager.get(prior.id)
    if retired:
        assert result.metadata["invalidated_entity_ids"] == [prior.id]
        assert created.metadata["supersedes_entity_ids"] == [prior.id]
        assert stored_prior.metadata["invalidated_by_entity_id"] == result.response.id
    else:
        assert result.metadata["invalidated_entity_ids"] == []
        assert created.metadata["supersedes_entity_ids"] == []
        assert "invalid_at" not in stored_prior.metadata


@pytest.mark.parametrize(("maintained", "retired"), [(set(), False), ({_PROJECT}, True)])
async def test_promotion_hands_maintainer_grants_to_invalidation(
    runtime, content_store, maintained, retired
):
    """The promotion services forward the reviewer's maintainer grants to the write."""
    target = await _project_capture(runtime, author="source-author")
    replacement = await remember_raw_memory(
        organization_id=runtime.client.group_id,
        principal_id=_PROMOTER,
        source_id="personal-replacement",
        title="Revised guidance",
        raw_content="Use the revised convention.",
        metadata={"supersedes_source_ids": [target.id]},
        embedding_provider=None,
    )
    result = await promote_raw_memory(
        raw_memory_id=replacement.id,
        organization_id=replacement.organization_id,
        principal_id=_PROMOTER,
        promote_to_scope="private",
        accessible_projects={_PROJECT},
        writable_projects={_PROJECT},
        maintained_projects=maintained,
    )

    assert result.success, result.reason
    current = await get_raw_memory(organization_id=target.organization_id, memory_id=target.id)
    assert current is not None
    assert ("invalid_at" in current.metadata) is retired
    key = "invalidated_source_ids" if retired else "invalidation_skipped_source_ids"
    assert result.metadata[key] == [target.id]


@pytest.mark.parametrize("grant_iterator", [False, True])
async def test_project_promotion_preserves_write_grants_across_stages(
    runtime, content_store, grant_iterator
):
    source = await remember_raw_memory(
        organization_id=runtime.client.group_id,
        principal_id="owner",
        source_id="personal-promotion-source",
        title="Reusable guidance",
        raw_content="Apply the project convention consistently.",
        embedding_provider=None,
    )
    grants = iter(["project_b"]) if grant_iterator else {"project_b"}
    result = await promote_raw_memory(
        raw_memory_id=source.id,
        organization_id=source.organization_id,
        principal_id="owner",
        promote_to_scope="project",
        promote_to_scope_key="project_b",
        accessible_projects={"project_b"},
        writable_projects=grants,
    )
    assert result.success, result.reason


@pytest.mark.parametrize("can_write", [False, True])
async def test_sharing_requires_project_write_grants(runtime, content_store, can_write):
    source = await remember_raw_memory(
        organization_id=runtime.client.group_id,
        principal_id="owner",
        source_id="personal-share-source",
        title="Reusable shared guidance",
        raw_content="Apply the shared convention consistently.",
        embedding_provider=None,
    )
    result = await share_memory(
        source_ids=[source.id],
        organization_id=source.organization_id,
        principal_id="owner",
        target_scope="project",
        target_scope_key="project_b",
        accessible_projects={"project_b"},
        writable_projects={"project_b"} if can_write else set(),
    )
    assert result.applied is can_write
    if can_write:
        assert result.reason == "shared"
    else:
        assert result.reason == "unverified_membership"
