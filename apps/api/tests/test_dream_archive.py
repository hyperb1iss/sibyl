"""Content archives retain dreaming progress without overriding source revocation."""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest

from sibyl.jobs import reflection
from sibyl.persistence import content_archive
from sibyl_core.backends.surreal import SurrealContentClient
from sibyl_core.backends.surreal.content_schema import bootstrap_content_schema
from sibyl_core.services.surreal_content import remember_raw_memory
from tests.test_dream_source_checkpoints import dream_store as dream_store  # noqa: PLC0414


async def populate(store, monkeypatch):
    await remember_raw_memory(
        organization_id="dream-org",
        principal_id="owner",
        source_id="session",
        raw_content="Decision: validate inputs before parsing.",
        embedding_provider=None,
    )
    real_complete = reflection.complete_dream_stage
    monkeypatch.setattr(reflection, "complete_dream_stage", AsyncMock(return_value=False))
    result = await reflection.run_reflection_dream_cycle({}, "dream-org", candidate_limit=0)
    assert result["failed"] == 1
    monkeypatch.setattr(reflection, "complete_dream_stage", real_complete)
    monkeypatch.setattr(content_archive, "build_surreal_content_client", lambda: store)
    monkeypatch.setattr(store, "close", AsyncMock())
    return await content_archive.export_content_archive_payload("dream-org")


@pytest.mark.parametrize("clean", [False, True])
async def test_dream_archive_restart_does_not_repeat_extraction(dream_store, monkeypatch, clean):
    close_source = dream_store.close
    archive = await populate(dream_store, monkeypatch)
    assert archive["version"] == content_archive.CONTENT_ARCHIVE_VERSION
    assert archive["tables"]["dream_source_checkpoints"]
    before = await dream_store.execute_query("SELECT uuid FROM raw_captures ORDER BY uuid;")
    assert len(before) > 1
    checkpoint = await dream_store.execute_query("SELECT * OMIT id FROM dream_source_checkpoints;")
    cursor = await dream_store.execute_query("SELECT * OMIT id FROM dream_source_cursors;")
    destination = SurrealContentClient(url="memory://")
    await bootstrap_content_schema(destination)
    close_destination = destination.close
    monkeypatch.setattr(destination, "close", AsyncMock())
    monkeypatch.setattr(content_archive, "build_surreal_content_client", lambda: destination)

    @asynccontextmanager
    async def session():
        yield destination

    monkeypatch.setattr("sibyl_core.services.content_client.surreal_content_client", session)
    try:
        result = await content_archive.restore_content_archive_payload(archive, clean=clean)
        assert result.success, result.errors
        assert (
            await destination.execute_query("SELECT * OMIT id FROM dream_source_checkpoints;")
            == checkpoint
        )
        assert (
            await destination.execute_query("SELECT * OMIT id FROM dream_source_cursors;") == cursor
        )
        assert (
            await destination.execute_query("SELECT uuid FROM raw_captures ORDER BY uuid;")
            == before
        )
        monkeypatch.setattr(
            "sibyl_core.services.dream_checkpoints.HeuristicReflectionExtractor.extract",
            AsyncMock(side_effect=AssertionError("extraction repeated")),
        )
        resumed = await reflection.run_reflection_dream_cycle({}, "dream-org", candidate_limit=0)
        assert resumed["failed"] == 0
        assert resumed["sources_reflected"] == 1
        assert (
            await destination.execute_query("SELECT uuid FROM raw_captures ORDER BY uuid;")
            == before
        )
    finally:
        await close_destination()
        await close_source()


async def test_dream_archive_purge_cannot_restore_private_stage(dream_store, monkeypatch):
    close = dream_store.close
    archive = await populate(dream_store, monkeypatch)
    try:
        await dream_store.execute_query("DELETE raw_captures;")
        assert not await dream_store.execute_query("SELECT * FROM dream_source_checkpoints;")
        result = await content_archive.restore_content_archive_payload(archive, clean=True)
        assert not result.success
        assert not await dream_store.execute_query("SELECT * FROM dream_source_checkpoints;")
        assert not await dream_store.execute_query("SELECT * FROM raw_captures;")
    finally:
        await close()


async def test_dream_archive_legacy_v2_without_checkpoints_is_accepted(dream_store, monkeypatch):
    close = dream_store.close
    monkeypatch.setattr(dream_store, "close", AsyncMock())
    monkeypatch.setattr(content_archive, "build_surreal_content_client", lambda: dream_store)
    try:
        archive = await content_archive.export_content_archive_payload("dream-org")
        archive["version"] = "2.0"
        del archive["tables"]["dream_source_checkpoints"]
        del archive["tables"]["dream_source_cursors"]
        result = await content_archive.restore_content_archive_payload(archive)
        assert result.success, result.errors
    finally:
        await close()


async def test_dream_archive_preserves_newer_cursor(dream_store, monkeypatch):
    close = dream_store.close
    archive = await populate(dream_store, monkeypatch)
    try:
        await dream_store.execute_query(
            "UPDATE dream_source_cursors SET revision += 1, source_id = 'later';"
        )
        before = await dream_store.execute_query("SELECT * FROM dream_source_cursors;")
        result = await content_archive.restore_content_archive_payload(archive, clean=True)
        assert result.success, result.errors
        assert await dream_store.execute_query("SELECT * FROM dream_source_cursors;") == before
    finally:
        await close()


async def test_dream_archive_old_stage_cannot_overwrite_completion(dream_store, monkeypatch):
    close = dream_store.close
    archive = await populate(dream_store, monkeypatch)
    try:
        await dream_store.execute_query(
            "UPDATE dream_source_checkpoints SET completion_json = $completion;",
            completion='{"done":true}',
        )
        before = await dream_store.execute_query("SELECT * FROM dream_source_checkpoints;")
        result = await content_archive.restore_content_archive_payload(archive, clean=True)
        assert not result.success
        assert await dream_store.execute_query("SELECT * FROM dream_source_checkpoints;") == before
    finally:
        await close()
