"""Publication bookkeeping preserves source identity without hiding lifecycle changes."""

from dataclasses import replace
from uuid import uuid4

import pytest

from sibyl_core.memory_pipeline.observations import SourceIdentity, SourceKind
from sibyl_core.services import content_client
from sibyl_core.services.source_state_store import load_source_snapshot
from sibyl_core.services.surreal_content import remember_raw_memory, save_raw_memory
from tests.test_reflection_identity import content_store as content_store


async def source_snapshot(memory):
    async with content_client.surreal_content_client() as client:
        return await load_source_snapshot(
            SourceIdentity(memory.organization_id, SourceKind.RAW_CAPTURE, memory.id),
            organization_id=memory.organization_id,
            execute_query=client.execute_query,
        )


async def test_ordinary_publication_bookkeeping_preserves_source_epoch(content_store):
    memory = await remember_raw_memory(
        organization_id=str(uuid4()),
        principal_id="owner",
        source_id="source",
        raw_content="Keep the blue approval.",
        embedding_provider=None,
    )
    before = await source_snapshot(memory)
    promoted = await save_raw_memory(
        replace(
            memory,
            review_state="promoted",
            metadata={
                **memory.metadata,
                "review_state": "promoted",
                "lifecycle_state": "active",
                "lifecycle_flags": [],
                "memory_lifecycle": {
                    "state": "active",
                    "flags": [],
                    "action": "promote",
                    "reason": "published",
                    "source_id": memory.id,
                    "derived_ids": ["target"],
                },
            },
        ),
        expected_revision=memory.revision,
    )
    after = await source_snapshot(promoted)
    assert after.observation.same_evidence(before.observation)
    assert after.observation.revision > before.observation.revision


@pytest.mark.parametrize(
    "updates",
    [
        {"review_state": "archived"},
        {"review_state": "deferred"},
        {"metadata": {"review_state": "rejected"}},
        {"metadata": {"lifecycle_state": "archived"}},
        {"metadata": {"memory_lifecycle": {"state": "active", "flags": ["hidden"]}}},
        {"principal_id": "other"},
    ],
)
async def test_nonbookkeeping_transition_advances_source_epoch(content_store, updates):
    memory = await remember_raw_memory(
        organization_id=str(uuid4()),
        principal_id="owner",
        source_id="source",
        raw_content="Keep the blue approval.",
        embedding_provider=None,
    )
    before = await source_snapshot(memory)
    changed = await save_raw_memory(replace(memory, **updates), expected_revision=memory.revision)
    after = await source_snapshot(changed)
    assert after.observation.generation > before.observation.generation


async def test_upgrade_preserves_existing_source_and_high_water(monkeypatch):
    from contextlib import asynccontextmanager

    from sibyl_core.backends.surreal import SurrealContentClient, bootstrap_content_schema
    from sibyl_core.backends.surreal.content_schema import (
        CONTENT_SCHEMA_NAME,
        _content_schema_migrations,
    )
    from sibyl_core.backends.surreal.schema_version import (
        apply_schema_migrations,
        get_schema_version,
    )

    client = SurrealContentClient(url="memory://")
    try:
        await apply_schema_migrations(
            client.execute_query,
            [
                migration
                for migration in _content_schema_migrations(url=client._url)
                if migration.version <= 32
            ],
            name=CONTENT_SCHEMA_NAME,
        )

        @asynccontextmanager
        async def session():
            yield client

        monkeypatch.setattr(content_client, "surreal_content_client", session)
        memory = await remember_raw_memory(
            organization_id=str(uuid4()),
            principal_id="owner",
            source_id="source",
            raw_content="Keep the original signed content.",
            metadata={"note": "unchanged"},
            embedding_provider=None,
        )
        before = await source_snapshot(memory)
        await bootstrap_content_schema(client)
        assert await get_schema_version(client.execute_query, name=CONTENT_SCHEMA_NAME) == 33
        after = await source_snapshot(memory)
        assert after == before
        promoted = await save_raw_memory(
            replace(memory, review_state="promoted"), expected_revision=memory.revision
        )
        assert (await source_snapshot(promoted)).observation.same_evidence(before.observation)
    finally:
        await client.close()
