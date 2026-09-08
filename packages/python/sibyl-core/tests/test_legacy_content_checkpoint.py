"""Legacy content epochs are stable across every raw storage write shape."""

from dataclasses import replace

from sibyl_core.memory_pipeline.source_lifecycle import correction_event
from sibyl_core.services import content_client
from sibyl_core.services.content_models import raw_memory_record
from sibyl_core.services.content_raw_persistence import (
    get_raw_memory,
    remember_raw_memory,
    replace_raw_memory_records_bulk,
    save_raw_memory,
)
from tests.test_reflection_identity import content_store as content_store


async def test_legacy_checkpoint_single_bulk_and_import(content_store):
    source = await remember_raw_memory(
        organization_id="checkpoint",
        principal_id="owner",
        source_id="source",
        raw_content="old",
        embedding_provider=None,
    )
    revised = await save_raw_memory(
        replace(source, metadata={"correction_history": [{"action": "revise"}]}),
        expected_revision=source.revision,
        embedding_provider=None,
    )
    assert revised.revision == source.revision + 1
    assert correction_event(revised, blocking=False).content_revision == revised.revision
    again = await save_raw_memory(
        replace(revised, metadata={**revised.metadata, "bookkeeping": True}),
        expected_revision=revised.revision,
        embedding_provider=None,
    )
    assert correction_event(again, blocking=False).content_revision == revised.revision
    async with content_client.surreal_content_client() as client:
        rows = await replace_raw_memory_records_bulk(client, [raw_memory_record(again)])
        assert rows[0]["revision"] == again.revision + 1
        await content_client.select_many(
            client,
            "CREATE raw_captures CONTENT $record;",
            record={
                **raw_memory_record(again),
                "uuid": "restored",
                "legacy_content_checkpoint": None,
            },
        )
    stored = await get_raw_memory(organization_id="checkpoint", memory_id=source.id)
    assert correction_event(stored, blocking=False).content_revision == revised.revision
    restored = await get_raw_memory(organization_id="checkpoint", memory_id="restored")
    assert correction_event(restored, blocking=False).content_revision == again.revision


async def test_legacy_checkpoint_advances_only_with_appended_history(content_store):
    source = await remember_raw_memory(
        organization_id="checkpoint",
        principal_id="owner",
        source_id="epochs",
        raw_content="before",
        embedding_provider=None,
    )
    original = source
    for text in ("first legacy revision", "second legacy revision"):
        old_revision = source.revision
        source = await save_raw_memory(
            replace(
                source,
                raw_content=text,
                metadata={
                    **source.metadata,
                    "correction_history": [
                        *source.metadata.get("correction_history", []),
                        {"action": "revise", "reason": text},
                    ],
                },
            ),
            expected_revision=old_revision,
            embedding_provider=None,
        )
        assert source.revision == old_revision + 1
        assert correction_event(source, blocking=False).content_revision == source.revision
        epoch = source.revision
        source = await save_raw_memory(
            replace(source, metadata={**source.metadata, "bookkeeping": text}),
            expected_revision=source.revision,
            embedding_provider=None,
        )
        assert correction_event(source, blocking=False).content_revision == epoch
    legacy_history = source.metadata["correction_history"]
    legacy_checkpoint = source.legacy_content_checkpoint
    prior = source.revision
    modern = await save_raw_memory(
        replace(
            source,
            raw_content="modern revision",
            metadata={
                **source.metadata,
                "correction_history": [
                    *legacy_history,
                    {"action": "revise", "prior_revision": prior},
                ],
            },
        ),
        expected_revision=prior,
        embedding_provider=None,
    )
    assert modern.legacy_content_checkpoint == legacy_checkpoint
    assert correction_event(modern, blocking=False).content_revision == prior + 1
    assert modern.metadata["correction_history"][:2] == legacy_history
    assert original.metadata == {}


async def test_legacy_checkpoint_cas_preserves_winner(content_store):
    import asyncio

    from sibyl_core.errors import RevisionConflictError

    source = await remember_raw_memory(
        organization_id="checkpoint",
        principal_id="owner",
        source_id="race",
        raw_content="before",
        embedding_provider=None,
    )
    writes = [
        save_raw_memory(
            replace(
                source,
                raw_content=text,
                metadata={
                    "correction_history": [
                        {"action": "revise", "reason": text},
                    ]
                },
            ),
            expected_revision=source.revision,
            embedding_provider=None,
        )
        for text in ("winner a", "winner b")
    ]
    results = await asyncio.gather(*writes, return_exceptions=True)
    assert sum(isinstance(result, RevisionConflictError) for result in results) == 1
    stored = await get_raw_memory(organization_id="checkpoint", memory_id=source.id)
    assert stored.revision == source.revision + 1
    assert correction_event(stored, blocking=False).content_revision == stored.revision
    assert stored.metadata["correction_history"][0]["reason"] == stored.raw_content


async def test_legacy_checkpoint_upgrade_preserves_observed_binding(content_store):
    from sibyl_core.backends.surreal.content_schema import (
        CONTENT_LEGACY_CONTENT_CHECKPOINT_BACKFILL,
        CONTENT_LEGACY_CONTENT_CHECKPOINT_DEFINITIONS,
    )
    from sibyl_core.services.content_models import raw_memory_from_record

    async with content_client.surreal_content_client() as client:
        await client.execute_query("REMOVE FIELD legacy_content_checkpoint ON raw_captures;")
        await client.execute_query(
            "CREATE raw_captures CONTENT $record;",
            record={
                "uuid": "upgrade",
                "organization_id": "checkpoint",
                "revision": 7,
                "metadata": {"correction_history": [{"action": "revise"}]},
            },
        )
        await client.execute_query(
            "CREATE raw_captures CONTENT {uuid: 'ordinary', organization_id: 'checkpoint', revision: 5};"
        )
        await client.execute_query(CONTENT_LEGACY_CONTENT_CHECKPOINT_DEFINITIONS)
        await client.execute_query(CONTENT_LEGACY_CONTENT_CHECKPOINT_BACKFILL)
        first = (
            await content_client.select_many(
                client, "SELECT * FROM raw_captures WHERE uuid = 'upgrade';"
            )
        )[0]
        await client.execute_query(CONTENT_LEGACY_CONTENT_CHECKPOINT_BACKFILL)
        second = (
            await content_client.select_many(
                client, "SELECT * FROM raw_captures WHERE uuid = 'upgrade';"
            )
        )[0]
        ordinary = (
            await content_client.select_many(
                client, "SELECT revision FROM raw_captures WHERE uuid = 'ordinary';"
            )
        )[0]
    assert first == second
    assert ordinary["revision"] == 5
    memory = raw_memory_from_record(first)
    assert memory.revision == 8
    assert correction_event(memory, blocking=False).content_revision == 7
    assert memory.metadata["correction_history"] == [{"action": "revise"}]


async def test_legacy_checkpoint_batch_advances_existing_and_initializes_new(content_store):
    from sibyl_core.services.content_models import raw_memory_from_record

    source = await remember_raw_memory(
        organization_id="checkpoint",
        principal_id="owner",
        source_id="batch",
        raw_content="before",
        embedding_provider=None,
    )
    source = await save_raw_memory(
        replace(source, metadata={"correction_history": [{"action": "revise", "reason": "first"}]}),
        expected_revision=source.revision,
        embedding_provider=None,
    )
    record = raw_memory_record(
        replace(
            source,
            raw_content="new legacy body",
            metadata={
                **source.metadata,
                "correction_history": [
                    *source.metadata["correction_history"],
                    {"action": "revise", "reason": "second"},
                ],
            },
        )
    )
    async with content_client.surreal_content_client() as client:
        rows = await replace_raw_memory_records_bulk(
            client,
            [
                record,
                {**record, "uuid": "new-batch-row", "revision": 99},
            ],
        )
    stored = {row["uuid"]: raw_memory_from_record(row) for row in rows}
    assert stored[source.id].revision == source.revision + 1
    assert (
        correction_event(stored[source.id], blocking=False).content_revision == source.revision + 1
    )
    assert stored["new-batch-row"].revision == 1
    assert correction_event(stored["new-batch-row"], blocking=False).content_revision == 1


async def test_legacy_checkpoint_stale_history_and_tenant_collision_are_atomic(content_store):
    import pytest

    source = await remember_raw_memory(
        organization_id="checkpoint",
        principal_id="owner",
        source_id="protected",
        raw_content="current",
        embedding_provider=None,
    )
    record = raw_memory_record(source)
    record["metadata"] = {"correction_history": [{"action": "revise", "reason": "first"}]}
    async with content_client.surreal_content_client() as client:
        first = (await replace_raw_memory_records_bulk(client, [record]))[0]
        record["metadata"] = {
            "correction_history": [
                *first["metadata"]["correction_history"],
                {"action": "revise", "reason": "second"},
            ]
        }
        second = (await replace_raw_memory_records_bulk(client, [record]))[0]
        with pytest.raises(RuntimeError, match=r"legacy correction history|failed transaction"):
            await replace_raw_memory_records_bulk(
                client,
                [
                    {**record, "uuid": "would-be-created"},
                    first,
                ],
            )
        with pytest.raises(RuntimeError, match=r"organization cannot change|failed transaction"):
            await replace_raw_memory_records_bulk(
                client,
                [
                    {**record, "uuid": "would-be-created"},
                    {**record, "organization_id": "different-tenant"},
                ],
            )
        stored = (
            await content_client.select_many(
                client, "SELECT * FROM raw_captures WHERE uuid = $uuid;", uuid=source.id
            )
        )[0]
        absent = await content_client.select_many(
            client, "SELECT * FROM raw_captures WHERE uuid = 'would-be-created';"
        )
    assert stored == second
    assert absent == []


async def test_legacy_checkpoint_nonstring_actions_survive_upgrade_and_writes(content_store):
    from sibyl_core.backends.surreal.content_schema import (
        CONTENT_LEGACY_CONTENT_CHECKPOINT_BACKFILL,
        CONTENT_LEGACY_CONTENT_CHECKPOINT_DEFINITIONS,
    )

    history = [{"action": value} for value in (5, False, {}, [], None)] + [{"action": " ReViSe "}]
    async with content_client.surreal_content_client() as client:
        await client.execute_query("REMOVE FIELD legacy_content_checkpoint ON raw_captures;")
        await client.execute_query(
            "CREATE raw_captures CONTENT $record;",
            record={
                "uuid": "mixed-history",
                "organization_id": "checkpoint",
                "revision": 7,
                "metadata": {"correction_history": history},
            },
        )
        before = (
            await content_client.select_many(
                client, "SELECT metadata FROM raw_captures WHERE uuid = 'mixed-history';"
            )
        )[0]
        await client.execute_query(CONTENT_LEGACY_CONTENT_CHECKPOINT_DEFINITIONS)
        await client.execute_query(CONTENT_LEGACY_CONTENT_CHECKPOINT_BACKFILL)
        stored = (
            await content_client.select_many(
                client, "SELECT * FROM raw_captures WHERE uuid = 'mixed-history';"
            )
        )[0]
        assert stored["legacy_content_checkpoint"] == {
            "entries": [history[-1]],
            "observed_revision": 7,
        }
        assert stored["metadata"]["correction_history"] == before["metadata"]["correction_history"]
        await client.execute_query(
            "CREATE raw_captures CONTENT $record;",
            record={
                "uuid": "mixed-import",
                "organization_id": "checkpoint",
                "revision": 9,
                "metadata": {"correction_history": history},
            },
        )
        imported = (
            await content_client.select_many(
                client, "SELECT * FROM raw_captures WHERE uuid = 'mixed-import';"
            )
        )[0]
        assert imported["legacy_content_checkpoint"] == {
            "entries": [history[-1]],
            "observed_revision": 9,
        }
        assert (
            imported["metadata"]["correction_history"] == before["metadata"]["correction_history"]
        )
