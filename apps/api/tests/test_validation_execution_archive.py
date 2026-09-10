"""Validation history survives public archive playback without reviving text."""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest

from sibyl.persistence import content_archive
from sibyl_core.backends.surreal import SurrealContentClient, bootstrap_content_schema
from sibyl_core.services import content_client
from sibyl_core.services.content_raw_persistence import remember_raw_memory
from sibyl_core.services.validation_execution import ValidationExecution
from sibyl_core.tasks.procedure_review import review_digest


@pytest.fixture
async def history(monkeypatch):
    client = SurrealContentClient(url="memory://")
    await bootstrap_content_schema(client)

    @asynccontextmanager
    async def session():
        yield client

    monkeypatch.setattr(content_client, "surreal_content_client", session)
    source = await remember_raw_memory(
        organization_id="org",
        principal_id="owner",
        source_id="source",
        raw_content="Original evidence",
        embedding_provider=None,
    )
    states = content_client.normalize_records(
        await client.execute_query("SELECT * FROM source_states;")
    )
    bindings = [{key: states[0][key] for key in ("source_id", "incarnation", "generation")}]
    request = {
        "org": "org",
        "principal": "owner",
        "parent": source.id,
        "source_bindings": bindings,
        "policy": "{}",
    }
    execution = ValidationExecution(review_digest(request), "org", "owner")
    await execution.begin(parent_id=source.id, source_ids=[source.id], policy="{}", request=request)
    await execution.before_dispatch()
    close = client.close
    monkeypatch.setattr(client, "close", AsyncMock())
    monkeypatch.setattr(content_archive, "build_surreal_content_client", lambda: client)
    try:
        yield client, execution
    finally:
        await close()


async def test_validation_archive_exact_unknown_attempt(history, monkeypatch):
    client, _execution = history
    archive = await content_archive.export_content_archive_payload("org")
    before = {
        t: await client.execute_query(f"SELECT * OMIT id FROM {t};")  # noqa: S608
        for t in ("memory_validation_executions", "memory_validation_attempts")
    }
    destination = SurrealContentClient(url="memory://")
    await bootstrap_content_schema(destination)
    close = destination.close
    monkeypatch.setattr(destination, "close", AsyncMock())
    monkeypatch.setattr(content_archive, "build_surreal_content_client", lambda: destination)
    try:
        result = await content_archive.restore_content_archive_payload(archive, clean=True)
        assert result.success, result.errors
        for table, expected in before.items():
            assert await destination.execute_query(f"SELECT * OMIT id FROM {table};") == expected  # noqa: S608
    finally:
        await close()


async def test_validation_archive_preserves_newer_terminal(history):
    client, execution = history
    archive = await content_archive.export_content_archive_payload("org")
    await execution.record_failure(TimeoutError())
    before = await client.execute_query("SELECT * FROM memory_validation_executions;")
    result = await content_archive.restore_content_archive_payload(archive, clean=True)
    assert not result.success
    assert await client.execute_query("SELECT * FROM memory_validation_executions;") == before


async def test_validation_archive_cannot_recover_purged_parent(history):
    client, execution = history
    archive = await content_archive.export_content_archive_payload("org")
    await client.execute_query("DELETE raw_captures;")
    result = await content_archive.restore_content_archive_payload(archive, clean=True)
    assert not result.success
    assert not await client.execute_query("SELECT * FROM raw_captures;")
    assert (await execution.load())["purged"] is True


async def test_validation_archive_accepts_prior_v21_without_history(history):
    _client, execution = history
    archive = await content_archive.export_content_archive_payload("org")
    archive["version"] = "2.1"
    archive.pop("validation_receipts")
    del archive["tables"]["memory_validation_executions"]
    del archive["tables"]["memory_validation_attempts"]
    result = await content_archive.restore_content_archive_payload(archive, clean=True)
    assert result.success, result.errors
    assert await execution.load()


async def test_validation_archive_missing_retention_flag_rejected(history):
    _client, _execution = history
    archive = await content_archive.export_content_archive_payload("org")
    del archive["tables"]["memory_validation_executions"][0]["purged"]
    result = await content_archive.restore_content_archive_payload(archive, clean=True)
    assert not result.success
    assert result.rows_restored == 0
    assert result.errors == ["memory_validation_executions invalid archive row (ValueError)"]
    assert await _execution.load()
