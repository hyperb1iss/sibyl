"""Native lexical scores depend on raw evidence, never its dream proposals."""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from sibyl_core.backends.surreal import SurrealContentClient
from sibyl_core.backends.surreal.content_schema import bootstrap_content_schema
from sibyl_core.backends.surreal.schema_raw_lexical import migrate_raw_lexical
from sibyl_core.services import content_raw_recall
from sibyl_core.services.content_models import MemoryScope
from sibyl_core.services.surreal_content import remember_raw_memory


@pytest.fixture
async def lexical_store(monkeypatch):
    url = os.environ.get("SIBYL_RAW_LEXICAL_TEST_URL", "memory://")
    client = SurrealContentClient(
        url=url,
        username="root" if url != "memory://" else "",
        password="root" if url != "memory://" else "",
        namespace=f"lexical_{uuid4().hex}",
        pool_size=4,
    )
    await bootstrap_content_schema(client, reset=True)

    @asynccontextmanager
    async def session():
        yield client

    monkeypatch.setattr("sibyl_core.services.content_client.surreal_content_client", session)
    monkeypatch.setattr(
        content_raw_recall, "raw_memory_query_embedding", AsyncMock(return_value=None)
    )
    try:
        yield client
    finally:
        await client.close()


async def _remember(org, source, **kwargs):
    return await remember_raw_memory(
        organization_id=org,
        principal_id=kwargs.pop("principal_id", "owner"),
        source_id=source,
        title=f"Observation {source}",
        raw_content=kwargs.pop("raw_content", "spectral telescope observation"),
        embedding_provider=None,
        **kwargs,
    )


async def _recall(client, org, *, capture_ids=None, principal="owner", limit=100):
    where, params = content_raw_recall._raw_memory_recall_where(
        organization_id=org,
        principal_id=principal,
        memory_scope=MemoryScope.PRIVATE,
        scope_key=None,
        filters=content_raw_recall._RawMemoryRecallFilters(
            capture_ids=tuple(capture_ids) if capture_ids is not None else None,
        ),
    )
    # Exercise actual MATCHES and BM25, with no lexical fallback to hide failures.
    return await content_raw_recall._recall_raw_memory_fulltext(
        client, where_clause=where, params=params, query="spectral", as_of=None, limit=limit
    )


def _scores(memories):
    return [(memory.id, memory.score) for memory in memories]


async def test_original_scores_survive_draft_edits_promotion_and_purge(lexical_store):
    client, org = lexical_store, str(uuid4())
    originals = []
    for index in range(20):
        originals.append(
            await _remember(
                org,
                f"original-{index}",
                raw_content=(
                    "spectral telescope " + "detail " * (index + 1)
                    if index < 4
                    else "unrelated nebula observation"
                ),
            )
        )
    ids = [memory.id for memory in originals]
    before = _scores(await _recall(client, org, capture_ids=ids))
    assert len(before) == 4
    assert all(score > 0 for _, score in before)

    drafts = [
        await _remember(org, f"draft-{index}", capture_surface="reflection_candidate")
        for index in range(76)
    ]
    assert _scores(await _recall(client, org, capture_ids=ids)) == before
    await client.execute_query(
        "UPDATE raw_captures SET raw_content = 'spectral spectral replacement' "
        "WHERE capture_surface = 'reflection_candidate';"
    )
    assert _scores(await _recall(client, org, capture_ids=ids)) == before
    await client.execute_query(
        "UPDATE raw_captures SET review_state = 'promoted' WHERE uuid = $id;", id=drafts[0].id
    )
    assert _scores(await _recall(client, org, capture_ids=ids)) == before
    served = await _recall(client, org)
    assert drafts[0].id in {memory.id for memory in served}
    assert not {memory.id for memory in drafts[1:]} & {memory.id for memory in served}
    await client.execute_query(
        "DELETE raw_captures WHERE capture_surface = 'reflection_candidate';"
    )
    assert _scores(await _recall(client, org, capture_ids=ids)) == before
    assert await client.execute_query("SELECT * FROM raw_lexical_reflections;") == []


async def test_new_originals_reclassification_and_authority_filter_before_limit(lexical_store):
    client, org = lexical_store, str(uuid4())
    retained = await _remember(org, "retained")
    for index in range(30):
        await _remember(org, f"foreign-{index}", principal_id="outsider")
    other = await _remember(str(uuid4()), "other-org")
    await _remember(org, "filler", raw_content="unrelated galaxy evidence")
    result = await _recall(client, org, limit=1)
    assert [memory.id for memory in result] == [retained.id]
    assert await client.execute_query(
        "SELECT VALUE record_id FROM raw_lexical_originals WHERE uuid = $id;", id=retained.id
    ) == await client.execute_query(
        "SELECT VALUE id FROM raw_captures WHERE uuid = $id;", id=retained.id
    )
    assert await _recall(client, org, capture_ids=[other.id]) == []
    await client.execute_query(
        "UPDATE raw_captures SET capture_surface = 'reflection_candidate' WHERE uuid = $id;",
        id=retained.id,
    )
    assert await _recall(client, org) == []
    await client.execute_query(
        "UPDATE raw_captures SET review_state = 'promoted' WHERE uuid = $id;", id=retained.id
    )
    assert [memory.id for memory in await _recall(client, org)] == [retained.id]
    await client.execute_query(
        "UPDATE raw_captures SET capture_surface = 'api', "
        "metadata.capture_surface = 'reflection_candidate' WHERE uuid = $id;",
        id=retained.id,
    )
    assert [memory.id for memory in await _recall(client, org)] == [retained.id]


async def test_upgrade_backfills_without_changing_sources_and_is_idempotent(
    lexical_store, monkeypatch
):
    client, org = lexical_store, str(uuid4())
    await client.execute_query("REMOVE EVENT maintain_raw_lexical ON raw_captures;")
    for index in range(5):
        await _remember(org, f"original-{index}")
    await _remember(org, "filler", raw_content="unrelated galaxy evidence")
    await _remember(org, "draft", capture_surface="reflection_candidate")
    await client.execute_query(
        "CREATE raw_captures:physical_legacy SET uuid = 'logical-legacy', "
        "organization_id = $org, principal_id = 'owner', raw_content = 'spectral legacy';",
        org=org,
    )
    before = await client.execute_query("SELECT * FROM raw_captures ORDER BY id;")
    monkeypatch.setattr("sibyl_core.backends.surreal.schema_raw_lexical._BACKFILL_BATCH_SIZE", 2)
    await client.execute_query("UPDATE schema_version:content SET version = 48;")
    await bootstrap_content_schema(client)
    assert await client.execute_query("SELECT * FROM raw_captures ORDER BY id;") == before
    first = _scores(await _recall(client, org))
    assert len(first) == 6
    legacy = next(memory for memory in await _recall(client, org) if memory.id == "logical-legacy")
    assert legacy.id == "logical-legacy"
    pointers = await client.execute_query(
        "SELECT VALUE type::string(record_id) FROM raw_lexical_originals WHERE uuid = 'logical-legacy';"
    )
    assert pointers == ["raw_captures:physical_legacy"]
    await migrate_raw_lexical(client.execute_query)
    assert _scores(await _recall(client, org)) == first
    assert await client.execute_query("SELECT * FROM raw_captures ORDER BY id;") == before
    fresh = await _remember(org, "new-after-upgrade")
    assert fresh.id in {memory.id for memory in await _recall(client, org)}


async def test_backfill_fetches_current_rows_after_identity_page(lexical_store):
    client, org = lexical_store, str(uuid4())
    retained = await _remember(org, "retained")
    await _remember(org, "filler", raw_content="unrelated galaxy evidence")
    changed = False

    async def execute(statement, **params):
        nonlocal changed
        result = await client.execute_query(statement, **params)
        if statement.startswith("SELECT id, type::string(id)") and not changed:
            changed = True
            await client.execute_query(
                "UPDATE raw_captures SET capture_surface = 'reflection_candidate' "
                "WHERE uuid = $id;",
                id=retained.id,
            )
        return result

    await migrate_raw_lexical(execute)
    assert changed
    assert await _recall(client, org) == []


@pytest.mark.parametrize(
    "surfaces", [("api", "reflection_candidate"), ("reflection_candidate", "api")]
)
async def test_native_backfill_conflicts_with_concurrent_draft_reclassification(
    lexical_store, surfaces, monkeypatch
):
    if not os.environ.get("SIBYL_RAW_LEXICAL_TEST_URL"):
        pytest.skip("write-conflict proof requires the pinned native server")
    client, org = lexical_store, str(uuid4())
    retained = await _remember(org, "retained", capture_surface=surfaces[0])
    entered = asyncio.Event()
    responses = []
    send = client._send_query

    async def observe(*args, **kwargs):
        response = await send(*args, **kwargs)
        if args[1].startswith("BEGIN TRANSACTION;"):
            responses.append(response)
        return response

    monkeypatch.setattr(client, "_send_query", observe)

    async def execute(statement, **params):
        if statement.startswith("BEGIN TRANSACTION;"):
            statement = statement.replace(
                "LET $source = (SELECT * FROM $capture_id)[0];",
                "LET $source = (SELECT * FROM $capture_id)[0]; SLEEP 500ms;",
            )
            entered.set()
        return await client.execute_query(statement, **params)

    async def revise():
        await entered.wait()
        await asyncio.sleep(0.1)
        await client.execute_query(
            "UPDATE raw_captures SET capture_surface = $surface WHERE uuid = $id;",
            id=retained.id,
            surface=surfaces[1],
        )

    migration = asyncio.create_task(migrate_raw_lexical(execute))
    ready = asyncio.create_task(entered.wait())
    try:
        await asyncio.wait([migration, ready], return_when=asyncio.FIRST_COMPLETED)
        if migration.done():
            await migration
            pytest.fail("backfill completed before exercising the overlap")
        results = await asyncio.gather(migration, revise(), return_exceptions=True)
    finally:
        ready.cancel()
        if not migration.done():
            migration.cancel()
        await asyncio.gather(ready, migration, return_exceptions=True)
    assert results[0] is None
    # The existing client retries the entire aborted transaction. Require the
    # actual conflict response and replay, not only a final matching corpus.
    assert len(responses) >= 2
    assert any("conflict" in str(response).lower() for response in responses)
    assert results[1] is None
    expected = 1 if surfaces[1] == "api" else 0
    assert len(await client.execute_query("SELECT * FROM raw_lexical_originals;")) == expected
    await migrate_raw_lexical(client.execute_query)
    assert len(await client.execute_query("SELECT * FROM raw_lexical_originals;")) == expected


async def test_fulltext_reads_leave_capture_and_index_state_unchanged(lexical_store):
    client, org = lexical_store, str(uuid4())
    await _remember(org, "retained")
    await _remember(org, "filler", raw_content="unrelated galaxy evidence")
    sources = await client.execute_query("SELECT * FROM raw_captures ORDER BY id;")
    states = await client.execute_query("SELECT * FROM raw_lexical_states ORDER BY id;")
    assert len(await _recall(client, org)) == 1
    assert await client.execute_query("SELECT * FROM raw_captures ORDER BY id;") == sources
    assert await client.execute_query("SELECT * FROM raw_lexical_states ORDER BY id;") == states
