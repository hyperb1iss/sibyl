"""Source admission preserves both membership and credential restrictions."""

from contextlib import asynccontextmanager
from uuid import uuid4

import pytest

from sibyl_core.auth.memory_policy import memory_scope_policy_key
from sibyl_core.backends.surreal import SurrealContentClient
from sibyl_core.backends.surreal.content_schema import bootstrap_content_schema
from sibyl_core.services import content_client, content_raw_persistence
from sibyl_core.services.content_models import RawMemoryWrite, raw_memory_recallable


@pytest.fixture
async def content_store(monkeypatch):
    client = SurrealContentClient(url="memory://")
    try:
        await bootstrap_content_schema(client, reset=True)

        @asynccontextmanager
        async def session():
            yield client

        monkeypatch.setattr(content_client, "surreal_content_client", session)
        yield str(uuid4())
    finally:
        await client.close()


@pytest.mark.parametrize("bulk", [False, True])
@pytest.mark.parametrize("source_access", ["allowed", "credential_denied", "membership_denied"])
async def test_source_admission_requires_membership_and_credential_grant(
    content_store, bulk, source_access
):
    org = content_store
    source = await content_raw_persistence.remember_raw_memory(
        organization_id=org,
        principal_id="owner",
        source_id="source",
        raw_content="Project source material",
        memory_scope="project",
        scope_key="source-project",
        embedding_provider=None,
    )
    projects = ["target-project"]
    if source_access != "membership_denied":
        projects.append("source-project")
    grants = [memory_scope_policy_key("project", "target-project")]
    if source_access != "credential_denied":
        grants.append(memory_scope_policy_key("project", "source-project"))
    context = {
        "accessible_projects": iter(projects),
        "allowed_memory_scope_keys": iter(grants),
        "embedding_provider": None,
    }
    common = {
        "organization_id": org,
        "principal_id": "owner",
        "raw_content": "Derived project material",
        "memory_scope": "project",
        "scope_key": "target-project",
        "metadata": {"raw_source_ids": [source.id]},
    }
    if bulk:
        children = await content_raw_persistence.remember_raw_memories(
            [RawMemoryWrite(source_id=f"derived-{index}", **common) for index in range(2)],
            **context,
        )
    else:
        children = [
            await content_raw_persistence.remember_raw_memory(
                source_id="derived", **common, **context
            )
        ]
    for child in children:
        stored = await content_raw_persistence.get_raw_memory(
            organization_id=org, memory_id=child.id
        )
        allowed = source_access == "allowed"
        assert raw_memory_recallable(stored) is allowed
        assert stored.metadata["source_validation_pending"] is not allowed
        if not allowed:
            assert source.id not in stored.metadata.get("correction_blockers", {})
            assert source.id not in stored.metadata.get("source_bindings", {})


async def test_admission_rechecks_target_authority_after_a_conflict(content_store, monkeypatch):
    from dataclasses import replace

    from sibyl_core.services.memory_source_validation import reconcile_raw_source_lifecycle

    org = content_store
    source = await content_raw_persistence.remember_raw_memory(
        organization_id=org,
        principal_id="owner",
        source_id="project-source",
        raw_content="Source material",
        memory_scope="project",
        scope_key="project-a",
        embedding_provider=None,
    )
    child = await content_raw_persistence.remember_raw_memory(
        organization_id=org,
        principal_id="owner",
        source_id="derived",
        raw_content="Original content",
        metadata={"raw_source_ids": [source.id]},
        embedding_provider=None,
    )
    assert child.metadata["source_validation_pending"] is True
    original_save = content_raw_persistence.save_raw_memory
    raced = False

    async def race(memory, **kwargs):
        nonlocal raced
        if not raced:
            raced = True
            await original_save(
                replace(child, principal_id="someone-else", raw_content="New private content"),
                expected_revision=child.observed_revision,
                embedding_provider=None,
            )
        return await original_save(memory, **kwargs)

    monkeypatch.setattr(content_raw_persistence, "save_raw_memory", race)
    with pytest.raises(PermissionError, match=r"^Memory capture is no longer accessible\.$"):
        await reconcile_raw_source_lifecycle(
            child,
            principal_id="owner",
            accessible_projects={"project-a"},
            embedding_provider=None,
        )
    assert raced
    stored = await content_raw_persistence.get_raw_memory(organization_id=org, memory_id=child.id)
    assert stored.principal_id == "someone-else"
    assert stored.raw_content == "New private content"
    assert stored.metadata["source_validation_pending"] is True


async def test_admission_refuses_a_normalized_but_unobserved_revision(content_store, monkeypatch):
    from unittest.mock import AsyncMock

    from sibyl_core.services.content_models import raw_memory_from_record, raw_memory_record
    from sibyl_core.services.memory_source_validation import reconcile_raw_source_lifecycle

    child = await content_raw_persistence.remember_raw_memory(
        organization_id=content_store,
        principal_id="owner",
        source_id="derived",
        raw_content="Original content",
        metadata={"raw_source_ids": ["missing-source"]},
        embedding_provider=None,
    )
    record = raw_memory_record(child)
    record["revision"] = None
    legacy = raw_memory_from_record(record)
    assert legacy.revision == 1
    assert legacy.observed_revision is None
    save = AsyncMock()
    monkeypatch.setattr(content_raw_persistence, "save_raw_memory", save)
    with pytest.raises(ValueError, match=r"^Memory capture revision is unavailable\.$"):
        await reconcile_raw_source_lifecycle(legacy, principal_id="owner", embedding_provider=None)
    save.assert_not_awaited()
