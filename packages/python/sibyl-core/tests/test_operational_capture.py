"""Exercise the real content store through the shared capture writer."""

from contextlib import asynccontextmanager
from dataclasses import replace

import pytest

from sibyl_core.backends.surreal import SurrealContentClient
from sibyl_core.backends.surreal.content_schema import EMBEDDING_DIM, bootstrap_content_schema
from sibyl_core.errors import RevisionConflictError
from sibyl_core.models.experience import OperationalExperience
from sibyl_core.services.content_raw_persistence import remember_raw_memory, save_raw_memory
from sibyl_core.services.operational_capture import OperationalSourceWrite, canonical_experience


@pytest.fixture
async def store(monkeypatch):
    client = SurrealContentClient(url="memory://")
    await bootstrap_content_schema(client, reset=True)

    @asynccontextmanager
    async def session():
        yield client

    monkeypatch.setattr("sibyl_core.services.content_client.surreal_content_client", session)
    try:
        yield client
    finally:
        await client.close()


def payload(outcome=""):
    return canonical_experience(
        OperationalExperience.model_validate(
            {
                "source_id": "experience",
                "project_id": "project",
                "goal": "inspect",
                "outcome": outcome,
                "observations": [
                    {
                        "id": "state",
                        "ordinal": 0,
                        "evidence": [{"id": "part", "content": 'original "quoted"\ntext'}],
                    }
                ],
            }
        )
    )


async def capture(
    *,
    outcome="",
    expected=None,
    principal="owner",
    project="project",
    intent=True,
    metadata=None,
    provider=None,
):
    write = OperationalSourceWrite("org", "experience", principal, project, expected)
    return await remember_raw_memory(
        organization_id="org",
        principal_id=principal,
        source_id="experience",
        raw_content=payload(outcome),
        memory_scope="project",
        scope_key=project,
        metadata=metadata or {"project_id": project},
        capture_surface="operational_experience",
        embedding_provider=provider,
        operational_write=write if intent else None,
    )


async def test_operational_create_replay_has_one_source_and_generation(store):
    first = await capture()
    before = await store.execute_query(
        "SELECT * FROM source_states WHERE source_kind='raw_capture';"
    )
    second = await capture()
    assert (
        first.id == second.id == OperationalSourceWrite("org", "experience", "owner", "project").id
    )
    assert first.revision == second.revision == 1
    assert second.raw_content == payload()
    assert len(await store.execute_query("SELECT * FROM raw_captures;")) == 1
    assert (
        await store.execute_query("SELECT * FROM source_states WHERE source_kind='raw_capture';")
        == before
    )


async def test_operational_change_requires_expected_revision_and_preserves_creator(store):
    first = await capture()
    with pytest.raises(RevisionConflictError):
        await capture(outcome="failed")
    changed = await capture(outcome="failed", expected=first.revision)
    assert changed.id == first.id and changed.revision == first.revision + 1
    assert changed.created_at == first.created_at and changed.principal_id == first.principal_id
    with pytest.raises(RevisionConflictError):
        await capture(outcome="old retry", expected=first.revision)


@pytest.mark.parametrize("missing", [False, True])
async def test_operational_retired_source_cannot_be_recreated(store, missing):
    first = await capture()
    if missing:
        await store.execute_query("DELETE raw_captures WHERE uuid=$id;", id=first.id)
    else:
        from sibyl_core.services.content_models import utcnow

        await save_raw_memory(
            replace(first, deleted_at=utcnow()),
            embedding_provider=None,
            expected_revision=first.revision,
        )
    with pytest.raises(ValueError, match=r"retired|unavailable"):
        await capture()


async def test_raw_only_creator_survives_without_graph_manifest(store):
    first = await capture()
    with pytest.raises(ValueError, match="identity or project"):
        await capture(principal="other-contributor")
    assert len(await store.execute_query("SELECT * FROM raw_captures;")) == 1
    assert (await capture()).id == first.id


async def test_generic_metadata_cannot_choose_internal_identity(store):
    generic = await capture(
        intent=False, metadata={"project_id": "project", "operational_write": True}
    )
    internal = await capture()
    assert generic.id != internal.id
    assert len(await store.execute_query("SELECT * FROM raw_captures;")) == 2


async def test_source_change_between_inspection_and_write_is_not_overwritten(store, monkeypatch):
    first = await capture()
    execute = store.execute_query
    changed = False

    async def interleave(query, **kwargs):
        nonlocal changed
        if "Operational source changed during capture" in query and not changed:
            changed = True
            await execute(
                "UPDATE raw_captures SET raw_content='concurrent correction', revision += 1 WHERE uuid=$id;",
                id=first.id,
            )
        return await execute(query, **kwargs)

    monkeypatch.setattr(store, "execute_query", interleave)
    with pytest.raises(Exception, match="Operational source changed"):
        await capture(outcome="late", expected=first.revision)
    assert changed
    rows = await execute("SELECT * FROM raw_captures WHERE uuid=$id;", id=first.id)
    assert rows[0]["raw_content"] == "concurrent correction"


async def test_internal_capture_rejects_arbitrary_row_metadata(store):
    with pytest.raises(ValueError, match="metadata is owned"):
        await capture(metadata={"project_id": "project", "raw_source_ids": ["fake-source"]})
    assert not await store.execute_query("SELECT * FROM raw_captures;")


async def test_replay_preserves_source_metadata_and_creation_dates(store):
    first = await capture()
    await store.execute_query(
        "UPDATE raw_captures SET metadata.last_recalled_at=time::now(), metadata.retrieval_count=3 WHERE uuid=$id;",
        id=first.id,
    )
    before = await store.execute_query("SELECT * FROM raw_captures WHERE uuid=$id;", id=first.id)
    await capture()
    assert (
        await store.execute_query("SELECT * FROM raw_captures WHERE uuid=$id;", id=first.id)
        == before
    )


async def test_project_collision_denied_even_without_graph_manifest(store):
    first = await capture()
    # The identity intentionally does not change with project, so foreign scope
    # cannot create a second row under the same operational source name.
    other = OperationalSourceWrite("org", "experience", "owner", "other")
    assert other.id == first.id
    value = OperationalExperience.model_validate_json(payload()).model_copy(
        update={"project_id": "other"}
    )
    with pytest.raises(ValueError, match="identity or project"):
        await remember_raw_memory(
            organization_id="org",
            principal_id="owner",
            source_id="experience",
            raw_content=canonical_experience(value),
            memory_scope="project",
            scope_key="other",
            metadata={"project_id": "other"},
            capture_surface="operational_experience",
            embedding_provider=None,
            operational_write=other,
        )
    assert len(await store.execute_query("SELECT * FROM raw_captures;")) == 1


async def test_payload_format_revision_does_not_mint_a_new_source(store):
    first = await capture()
    revised = OperationalExperience.model_validate_json(payload()).model_copy(
        update={"metadata": {"serialization_revision": 2}}
    )
    second = await remember_raw_memory(
        organization_id="org",
        principal_id="owner",
        source_id="experience",
        raw_content=canonical_experience(revised),
        memory_scope="project",
        scope_key="project",
        metadata={"project_id": "project"},
        capture_surface="operational_experience",
        embedding_provider=None,
        operational_write=OperationalSourceWrite(
            "org", "experience", "owner", "project", first.revision
        ),
    )
    assert second.id == first.id and second.revision == first.revision + 1
    assert len(await store.execute_query("SELECT * FROM raw_captures;")) == 1


async def test_replayed_or_refused_source_never_embeds(store, monkeypatch):
    from sibyl_core.services import content_raw_persistence as owner

    calls = []
    original = owner._raw_memory_with_embedding

    async def spy(memory, provider):
        calls.append(memory.raw_content)
        return await original(memory, None)

    monkeypatch.setattr(owner, "_raw_memory_with_embedding", spy)
    first = await capture()
    assert len(calls) == 1
    await capture()
    with pytest.raises(RevisionConflictError):
        await capture(outcome="refused without revision")
    with pytest.raises(RevisionConflictError):
        await capture(outcome="stale", expected=first.revision + 1)
    assert len(calls) == 1
    from sibyl_core.services.content_models import utcnow

    await store.execute_query(
        "UPDATE raw_captures SET deleted_at=$now WHERE uuid=$id;", id=first.id, now=utcnow()
    )
    with pytest.raises(ValueError, match="retired"):
        await capture()
    assert len(calls) == 1


async def test_source_fence_survives_async_embedding(store, monkeypatch):
    from sibyl_core.services import content_raw_persistence as owner

    first = await capture()

    async def interleave(memory, provider):
        await store.execute_query(
            "UPDATE raw_captures SET raw_content='during embedding', revision += 1 WHERE uuid=$id;",
            id=first.id,
        )
        return memory

    monkeypatch.setattr(owner, "_raw_memory_with_embedding", interleave)
    with pytest.raises(Exception, match="Operational source changed"):
        await capture(outcome="late", expected=first.revision)
    rows = await store.execute_query("SELECT * FROM raw_captures WHERE uuid=$id;", id=first.id)
    assert rows[0]["raw_content"] == "during embedding"


async def test_actual_embedding_provider_is_not_called_for_replay_or_refusal(store):
    from sibyl_core.embeddings.providers import DeterministicEmbeddingProvider, EmbeddingMetadata

    class Spy(DeterministicEmbeddingProvider):
        calls = 0

        async def embed_texts(self, texts, *, input_kind="document"):
            self.calls += 1
            return await super().embed_texts(texts, input_kind=input_kind)

    provider = Spy(EmbeddingMetadata("test", "one", EMBEDDING_DIM, "test", "bytes"))
    first = await capture(provider=provider)
    assert provider.calls == 1 and first.embedding
    replay = await capture(provider=provider)
    assert provider.calls == 1 and replay.embedding == first.embedding
    with pytest.raises(RevisionConflictError):
        await capture(outcome="changed", provider=provider)
    assert provider.calls == 1
    changed = await capture(outcome="changed", expected=first.revision, provider=provider)
    assert provider.calls == 2 and changed.embedding != first.embedding
    assert changed.metadata["embedding_metadata"] == first.metadata["embedding_metadata"]
    await store.execute_query("DELETE raw_captures WHERE uuid=$id;", id=first.id)
    with pytest.raises(ValueError, match="retired"):
        await capture(provider=provider)
    assert provider.calls == 2


async def test_lost_write_ack_replays_without_embedding_again(store, monkeypatch):
    from unittest.mock import AsyncMock

    from sibyl_core.embeddings.providers import DeterministicEmbeddingProvider, EmbeddingMetadata

    provider = DeterministicEmbeddingProvider(
        EmbeddingMetadata("test", "one", EMBEDDING_DIM, "test", "bytes")
    )
    embed = AsyncMock(wraps=provider.embed_texts)
    monkeypatch.setattr(provider, "embed_texts", embed)
    execute = store.execute_query
    lost = False

    async def lose_ack(query, **kwargs):
        nonlocal lost
        result = await execute(query, **kwargs)
        if "Operational source changed during capture" in query and not lost:
            lost = True
            raise ConnectionError("simulated lost acknowledgment after committed write")
        return result

    monkeypatch.setattr(store, "execute_query", lose_ack)
    with pytest.raises(ConnectionError, match="lost acknowledgment"):
        await capture(provider=provider)
    assert lost and embed.await_count == 1
    replay = await capture(provider=provider)
    assert replay.id == OperationalSourceWrite("org", "experience", "owner", "project").id
    assert embed.await_count == 1
    assert len(await execute("SELECT * FROM raw_captures;")) == 1


async def test_embedding_metadata_tracks_new_provider_and_null_vector(store):
    from sibyl_core.embeddings.providers import DeterministicEmbeddingProvider, EmbeddingMetadata

    old = DeterministicEmbeddingProvider(
        EmbeddingMetadata("test", "v1", EMBEDDING_DIM, "old", "bytes")
    )
    new = DeterministicEmbeddingProvider(
        EmbeddingMetadata("test", "v2", EMBEDDING_DIM, "new", "bytes")
    )
    first = await capture(provider=old)
    before_metadata = dict(first.metadata)
    second = await capture(outcome="changed", expected=first.revision, provider=new)
    assert second.embedding != first.embedding
    assert second.metadata["embedding_metadata"]["model"] == "v2"
    assert {k: v for k, v in second.metadata.items() if k != "embedding_metadata"} == {
        k: v for k, v in before_metadata.items() if k != "embedding_metadata"
    }
    third = await capture(outcome="no vector", expected=second.revision, provider=None)
    assert third.embedding is None
    assert third.metadata.get("embedding_metadata") is None
