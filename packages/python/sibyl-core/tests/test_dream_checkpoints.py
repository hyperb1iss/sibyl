"""Observation checkpoints survive restart without consuming unseen corrections."""

from contextlib import asynccontextmanager
from dataclasses import replace

import pytest

from sibyl_core.backends.surreal import SurrealContentClient
from sibyl_core.backends.surreal.content_schema import bootstrap_content_schema
from sibyl_core.memory_pipeline.observations import SourceIdentity, SourceKind
from sibyl_core.services import content_client
from sibyl_core.services.content_raw_persistence import remember_raw_memory, save_raw_memory
from sibyl_core.services.content_raw_recall import list_reflection_dream_source_memories
from sibyl_core.services.dream_checkpoints import (
    CheckpointReflectionExtractor,
    DreamCandidateWrite,
    DreamSourceWork,
    complete_dream_stage,
    load_dream_stage,
)
from sibyl_core.services.reflection import ReflectionExtractionRequest
from sibyl_core.services.source_state_store import RawSourceSnapshot, load_source_snapshot


@pytest.fixture
async def store(monkeypatch):
    client = SurrealContentClient(url="memory://")
    await bootstrap_content_schema(client, reset=True)

    @asynccontextmanager
    async def session():
        yield client

    monkeypatch.setattr(content_client, "surreal_content_client", session)
    try:
        yield client
    finally:
        await client.close()


async def source():
    return await remember_raw_memory(
        organization_id="dream-org",
        principal_id="owner",
        source_id="session",
        raw_content="Decision: always validate the input before parsing.",
        embedding_provider=None,
    )


async def work(store, memory, *, projects=frozenset()):
    snapshot = await load_source_snapshot(
        SourceIdentity(memory.organization_id, SourceKind.RAW_CAPTURE, memory.id),
        organization_id=memory.organization_id,
        execute_query=store.execute_query,
    )
    assert isinstance(snapshot, RawSourceSnapshot)
    return DreamSourceWork(snapshot, projects, projects)


def request(memory):
    return ReflectionExtractionRequest(
        content=memory.raw_content,
        source_title="session",
        intent="maintenance",
        domain=None,
        project=None,
        limit=12,
    )


async def test_dream_stage_replay_and_changed_obligation(store, monkeypatch):
    memory = await source()
    first = await work(store, memory)
    extracted = await CheckpointReflectionExtractor(first).extract(request(memory))
    assert await complete_dream_stage(first, {"candidate_ids": []})

    async def forbidden(*args, **kwargs):
        raise AssertionError("replay must use persisted extraction")

    monkeypatch.setattr(
        "sibyl_core.services.dream_checkpoints.HeuristicReflectionExtractor.extract", forbidden
    )
    assert await CheckpointReflectionExtractor(first).extract(request(memory)) == extracted
    assert (await load_dream_stage(first))["completion_json"]
    updated = await save_raw_memory(
        replace(memory, raw_content="Corrected advice"),
        expected_revision=memory.revision,
        embedding_provider=None,
    )
    second = await work(store, updated)
    assert second.key != first.key
    assert await load_dream_stage(second) is None
    assert not await complete_dream_stage(first, {})


async def test_dream_bookkeeping_does_not_make_new_obligation(store):
    memory = await source()
    first = await work(store, memory)
    await store.execute_query(
        "UPDATE raw_captures SET revision += 1, metadata.retrieval_count = 4 WHERE uuid=$uuid;",
        uuid=memory.id,
    )
    second = await work(store, memory)
    assert second.snapshot.memory.revision != first.snapshot.memory.revision
    assert second.key == first.key
    assert not await complete_dream_stage(first, {})
    assert (await work(store, memory, projects=frozenset({"project"}))).key != second.key


async def test_dream_candidate_replay_preserves_one_row_and_fails_on_tamper(store):
    memory = await source()
    observed = await work(store, memory)
    await CheckpointReflectionExtractor(observed).extract(request(memory))
    kwargs = dict(
        organization_id=memory.organization_id,
        principal_id=memory.principal_id,
        source_id=memory.id,
        raw_content="A durable candidate",
        title="Advice",
        capture_surface="reflection_candidate",
        embedding_provider=None,
        source_memories=[memory],
        dream_write=DreamCandidateWrite(observed, 0),
    )
    first = await remember_raw_memory(**kwargs)
    again = await remember_raw_memory(**kwargs)
    assert again.id == first.id
    assert again.revision == first.revision
    await store.execute_query(
        "UPDATE raw_captures SET review_state='archived', revision += 1 WHERE uuid=$uuid;",
        uuid=first.id,
    )
    archived = await remember_raw_memory(**kwargs)
    assert archived.review_state == "archived"
    assert archived.revision == first.revision + 1
    await store.execute_query(
        "UPDATE raw_captures SET raw_content='tampered', revision += 1 WHERE uuid=$uuid;",
        uuid=first.id,
    )
    with pytest.raises(Exception, match="immutable result changed"):
        await remember_raw_memory(**kwargs)


async def test_dream_keyset_pages_past_completed_and_hidden_rows(store):
    for index in range(65):
        await store.execute_query(
            "CREATE raw_captures CONTENT $row;",
            row={
                "uuid": f"source-{index:03}",
                "organization_id": "dream-org",
                "principal_id": "owner",
                "source_id": str(index),
                "raw_content": "source",
                "revision": 1,
                "metadata": {"reflection_dream_processed_at": "legacy timestamp"},
            },
        )

    async def pending(memory):
        return memory.id == "source-064"

    selected = await list_reflection_dream_source_memories(
        organization_id="dream-org", limit=1, is_pending=pending
    )
    assert [memory.id for memory in selected] == ["source-064"]


async def test_dream_purge_removes_private_stage_and_recreation_is_new_work(store):
    memory = await source()
    first = await work(store, memory)
    await CheckpointReflectionExtractor(first).extract(request(memory))
    await store.execute_query("DELETE raw_captures WHERE uuid=$uuid;", uuid=memory.id)
    assert await load_dream_stage(first) is None
    with pytest.raises(Exception, match="source changed"):
        await CheckpointReflectionExtractor(first).extract(request(memory))
    from sibyl_core.services.content_models import raw_memory_record

    await store.execute_query("CREATE raw_captures CONTENT $row;", row=raw_memory_record(memory))
    recreated = await work(store, memory)
    assert recreated.key != first.key


async def test_dream_concurrent_workers_keep_one_logical_candidate(store):
    import asyncio

    memory = await source()
    observed = await work(store, memory)
    extracted = await asyncio.gather(
        *[CheckpointReflectionExtractor(observed).extract(request(memory)) for _ in range(4)]
    )
    assert all(value == extracted[0] for value in extracted)

    async def write():
        return await remember_raw_memory(
            organization_id=memory.organization_id,
            principal_id=memory.principal_id,
            source_id=memory.id,
            raw_content="candidate",
            embedding_provider=None,
            source_memories=[memory],
            dream_write=DreamCandidateWrite(observed, 0),
        )

    rows = await asyncio.gather(*[write() for _ in range(4)])
    assert len({row.id for row in rows}) == 1
    assert len({row.revision for row in rows}) == 1
    stored = await store.execute_query(
        "SELECT revision FROM raw_captures WHERE uuid=$uuid;", uuid=rows[0].id
    )
    assert stored[0]["revision"] == rows[0].revision


async def test_dream_source_change_between_extraction_and_stage_does_not_acknowledge(
    store, monkeypatch
):
    memory = await source()
    observed = await work(store, memory)
    from sibyl_core.services.reflection import HeuristicReflectionExtractor

    extract = HeuristicReflectionExtractor.extract

    async def changed(extractor, extraction_request):
        result = await extract(extractor, extraction_request)
        await save_raw_memory(
            replace(memory, raw_content="A correction"),
            expected_revision=memory.revision,
            embedding_provider=None,
        )
        return result

    monkeypatch.setattr(HeuristicReflectionExtractor, "extract", changed)
    with pytest.raises(Exception, match="source changed"):
        await CheckpointReflectionExtractor(observed).extract(request(memory))
    assert await load_dream_stage(observed) is None


async def test_dream_replay_rejects_different_requested_candidate_and_purged_candidate(store):
    memory = await source()
    observed = await work(store, memory)
    await CheckpointReflectionExtractor(observed).extract(request(memory))
    kwargs = dict(
        organization_id=memory.organization_id,
        principal_id=memory.principal_id,
        source_id=memory.id,
        raw_content="original candidate",
        embedding_provider=None,
        source_memories=[memory],
        dream_write=DreamCandidateWrite(observed, 0),
    )
    original = await remember_raw_memory(**kwargs)
    with pytest.raises(Exception, match="incoming result changed"):
        await remember_raw_memory(**{**kwargs, "raw_content": "different candidate"})
    await store.execute_query("DELETE raw_captures WHERE uuid=$uuid;", uuid=original.id)
    with pytest.raises(Exception, match="purged"):
        await remember_raw_memory(**kwargs)


async def test_dream_checkpoint_upgrade_preserves_existing_raw_source(store):
    memory = await source()
    before = await store.execute_query(
        "SELECT * FROM raw_captures WHERE uuid=$uuid;", uuid=memory.id
    )
    await store.execute_query("REMOVE TABLE dream_source_checkpoints;")
    await store.execute_query("UPDATE schema_version SET version=35 WHERE name='content';")
    from sibyl_core.backends.surreal.schema_version import get_schema_version

    assert await get_schema_version(store.execute_query, name="content") == 35
    await bootstrap_content_schema(store)
    assert await get_schema_version(store.execute_query, name="content") == 36
    after = await store.execute_query(
        "SELECT * FROM raw_captures WHERE uuid=$uuid;", uuid=memory.id
    )
    assert before == after
    observed = await work(store, memory)
    await CheckpointReflectionExtractor(observed).extract(request(memory))
    assert await load_dream_stage(observed) is not None


async def test_dream_cursor_is_durable_and_stale_worker_cannot_rewind(store):
    from sibyl_core.services.dream_checkpoints import advance_dream_cursor, load_dream_cursor

    assert await load_dream_cursor("dream-org") == ("", 0)
    assert await advance_dream_cursor("dream-org", "source-b", 0)
    assert not await advance_dream_cursor("dream-org", "source-a", 0)
    assert await load_dream_cursor("dream-org") == ("source-b", 1)
    assert await load_dream_cursor("another-org") == ("", 0)
