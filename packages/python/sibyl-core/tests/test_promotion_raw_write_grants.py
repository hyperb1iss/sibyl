"""Temporal invalidation uses write authority independently of source visibility."""

import pytest

from sibyl_core.models.reflection import ReflectionCandidate
from sibyl_core.services.memory_reflection import persist_reflection_candidate, promote_raw_memory
from sibyl_core.services.memory_sharing import share_memory
from sibyl_core.services.surreal_content import get_raw_memory, remember_raw_memory
from tests.test_reflection_identity import content_store as content_store
from tests.test_reflection_identity import runtime as runtime


@pytest.mark.parametrize("can_write", [False, True])
async def test_project_source_invalidation_requires_write_grants(runtime, content_store, can_write):
    source = await remember_raw_memory(
        organization_id=runtime.client.group_id,
        principal_id="source-author",
        source_id="project-decision-source",
        title="Existing project guidance",
        raw_content="Use the original project convention.",
        memory_scope="project",
        scope_key="project_b",
        metadata={"project_id": "project_b"},
        embedding_provider=None,
    )
    result = await persist_reflection_candidate(
        candidate=ReflectionCandidate(
            kind="decision",
            title="Updated personal guidance",
            content="Use the revised convention.",
            reason="A new conclusion",
            confidence=1.0,
            metadata={"supersedes_source_ids": [source.id]},
        ),
        organization_id=runtime.client.group_id,
        principal_id="owner",
        accessible_projects={"project_b"},
        writable_projects={"project_b"} if can_write else set(),
        memory_scope="private",
    )
    assert result.response.success
    current = await get_raw_memory(organization_id=source.organization_id, memory_id=source.id)
    assert current is not None
    if can_write:
        assert current.metadata["invalidated_by_entity_id"] == result.response.id
        assert source.id in result.metadata["invalidated_source_ids"]
    else:
        assert current == source
        assert source.id in result.metadata["invalidation_skipped_source_ids"]


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
