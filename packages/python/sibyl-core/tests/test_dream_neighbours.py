"""The dream job fills a page with the seed's nearest sources that could share its cohort."""

from contextlib import asynccontextmanager

import pytest

from sibyl_core.backends.surreal import SurrealContentClient
from sibyl_core.backends.surreal.content_schema import EMBEDDING_DIM, bootstrap_content_schema
from sibyl_core.models.memory_scope import MemoryScope
from sibyl_core.services.content_raw_persistence import remember_raw_memory
from sibyl_core.services.content_raw_recall import list_reflection_dream_neighbours


@pytest.fixture
async def store(monkeypatch):
    from sibyl_core.services import content_models

    monkeypatch.setattr(content_models, "configured_raw_memory_embedding_provider", lambda: None)
    client = SurrealContentClient(url="memory://")
    try:
        await bootstrap_content_schema(client, reset=True)

        @asynccontextmanager
        async def session():
            yield client

        monkeypatch.setattr("sibyl_core.services.content_client.surreal_content_client", session)
        yield client
    finally:
        await client.close()


def _vector(components):
    vector = [0.0] * EMBEDDING_DIM
    for axis, value in components.items():
        vector[axis] = value
    return vector


async def _source(store, name, components=None, *, principal="owner", space="model-a", **fields):
    memory = await remember_raw_memory(
        organization_id="org",
        principal_id=principal,
        source_id=name,
        raw_content=f"{name}: repair evidence.",
        embedding_provider=None,
        **fields,
    )
    if components is not None:
        await store.execute_query(
            "UPDATE raw_captures SET embedding=$vector, metadata.embedding_metadata=$space "
            "WHERE uuid=$uuid;",
            vector=_vector(components),
            space={"model": space},
            uuid=memory.id,
        )
    return memory


async def _ids(seed, limit, **kwargs):
    neighbours = await list_reflection_dream_neighbours(
        organization_id="org", seed=seed, limit=limit, **kwargs
    )
    return [memory.id for memory in neighbours]


async def test_neighbours_rank_by_similarity_to_the_seed(store):
    seed = await _source(store, "seed", {0: 1.0})
    far = await _source(store, "far", {1: 1.0})
    mid = await _source(store, "mid", {0: 1.0, 1: 0.5})
    near = await _source(store, "near", {0: 1.0, 1: 0.1})
    assert await _ids(seed, 3) == [near.id, mid.id, far.id]
    assert await _ids(seed, 2) == [near.id, mid.id]
    assert await _ids(seed, 0) == []


async def test_neighbours_break_exact_ties_on_identifier(store):
    seed = await _source(store, "seed", {0: 1.0})
    twins = [await _source(store, f"twin-{index}", {0: 1.0, 1: 0.2}) for index in range(4)]
    assert await _ids(seed, 4) == sorted(twin.id for twin in twins)


async def test_neighbours_share_the_seed_owner_scope_and_embedding_space(store):
    seed = await _source(store, "seed", {0: 1.0})
    sibling = await _source(store, "sibling", {0: 1.0, 1: 0.4})
    # Each of these is nearer the seed than its sibling but could never join
    # the seed's cohort, so none of them may take a place on its page.
    await _source(store, "other-owner", {0: 1.0}, principal="someone-else")
    await _source(
        store, "other-scope", {0: 1.0}, memory_scope=MemoryScope.SHARED, scope_key="team-a"
    )
    await _source(store, "other-space", {0: 1.0}, space="model-b")
    await _source(store, "unembedded")
    await _source(store, "derived", {0: 1.0}, capture_surface="synthesis_artifact")
    assert await _ids(seed, 10) == [sibling.id]


async def test_neighbours_pass_over_sources_that_are_not_pending(store):
    seed = await _source(store, "seed", {0: 1.0})
    near = await _source(store, "near", {0: 1.0, 1: 0.1})
    mid = await _source(store, "mid", {0: 1.0, 1: 0.5})
    far = await _source(store, "far", {1: 1.0})
    asked = []

    async def pending(memory):
        asked.append(memory.id)
        return memory.id != near.id

    assert await _ids(seed, 2, is_pending=pending) == [mid.id, far.id]
    assert asked == [near.id, mid.id, far.id]


async def test_a_seed_without_a_vector_has_no_neighbours(store):
    seed = await _source(store, "seed")
    await _source(store, "near", {0: 1.0})
    assert await _ids(seed, 5) == []
