"""Application content archive imports preserve content without granting old trust."""

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from sibyl.persistence import content_archive
from sibyl_core.backends.surreal import SurrealContentClient
from sibyl_core.backends.surreal.content_schema import bootstrap_content_schema
from sibyl_core.services import content_client
from sibyl_core.services.content_raw_persistence import remember_raw_memory


async def test_legacy_content_archive_quarantines_imported_memory_with_exact_metadata(monkeypatch):
    source = SurrealContentClient(url="memory://")
    destination = SurrealContentClient(url="memory://")
    await bootstrap_content_schema(source)
    await bootstrap_content_schema(destination)
    source_close, destination_close = source.close, destination.close
    monkeypatch.setattr(source, "close", AsyncMock())
    monkeypatch.setattr(destination, "close", AsyncMock())

    @asynccontextmanager
    async def session():
        yield source

    monkeypatch.setattr(content_client, "surreal_content_client", session)
    monkeypatch.setattr(content_archive, "build_surreal_content_client", lambda: source)
    org = str(uuid4())
    try:
        raw = await remember_raw_memory(
            organization_id=org,
            principal_id="owner",
            source_id="legacy",
            raw_content="Do not lose these words",
            metadata={"literal": "2026-09-09T00:00:00Z"},
            embedding_provider=None,
        )
        payload = await content_archive.export_content_archive_payload(org)
        payload["version"] = "1.0"
        payload.pop("source_integrity")
        monkeypatch.setattr(content_archive, "build_surreal_content_client", lambda: destination)
        result = await content_archive.restore_content_archive_payload(payload, clean=True)
        assert not result.errors, result.errors
        assert result.quarantined == [
            {
                "source_id": raw.id,
                "reason": "unverifiable_legacy_lineage",
                "repair": "reauthor_under_new_capture_identity",
            }
        ]
        rows = await destination.execute_query(
            "SELECT * FROM raw_captures WHERE uuid=$uuid;", uuid=raw.id
        )
        assert len(rows) == 1
        assert rows[0]["raw_content"] == raw.raw_content
        assert rows[0]["metadata"]["literal"] == "2026-09-09T00:00:00Z"
        assert rows[0]["derivation_required"] is True
        assert not await destination.execute_query("SELECT * FROM memory_derivations;")

        from sibyl_core.services.content_models import raw_memory_from_record
        from sibyl_core.services.memory_derivations import raw_derivation_current
        from sibyl_core.services.memory_source_validation import SourceReadAuthority

        @asynccontextmanager
        async def restored_session():
            yield destination

        monkeypatch.setattr(content_client, "surreal_content_client", restored_session)
        authority = SourceReadAuthority(principal_id="owner", projects=frozenset())
        quarantined_memory = raw_memory_from_record(rows[0])
        assert not await raw_derivation_current(quarantined_memory, authority)
        reauthored = await remember_raw_memory(
            organization_id=org,
            principal_id="owner",
            source_id="reviewed-new-identity",
            raw_content=quarantined_memory.raw_content,
            embedding_provider=None,
        )
        assert reauthored.id != quarantined_memory.id
        assert await raw_derivation_current(reauthored, authority)
        assert not await raw_derivation_current(quarantined_memory, authority)

    finally:
        await source_close()
        await destination_close()


async def test_global_restore_requires_explicit_operator_scope_before_client(monkeypatch):
    from unittest.mock import Mock

    import pytest

    factory = Mock(side_effect=AssertionError("database must not be opened"))
    monkeypatch.setattr(content_archive, "build_surreal_content_client", factory)
    with pytest.raises(ValueError, match="explicit operator scope"):
        await content_archive.restore_content_archive_payload(
            {"version": "1.0", "tables": {}}, clean=True
        )
    factory.assert_not_called()


async def test_explicit_global_clean_retains_omitted_org_highwater_and_fences_new_org(monkeypatch):

    client = SurrealContentClient(url="memory://")
    await bootstrap_content_schema(client)
    close = client.close
    monkeypatch.setattr(client, "close", AsyncMock())

    @asynccontextmanager
    async def session():
        yield client

    monkeypatch.setattr(content_client, "surreal_content_client", session)
    monkeypatch.setattr(content_archive, "build_surreal_content_client", lambda: client)

    async def remember(org, source):
        return await remember_raw_memory(
            organization_id=org,
            principal_id="owner",
            source_id=source,
            raw_content=source,
            embedding_provider=None,
        )

    try:
        original = await remember("retained-org", "Original")
        payload = await content_archive.export_content_archive_payload()
        omitted = await remember("omitted-org", "Added after archive")
        result = await content_archive.restore_content_archive_payload(
            payload, clean=True, global_scope=True
        )
        assert not result.errors
        rows = await client.execute_query("SELECT * FROM raw_captures;")
        assert {row["uuid"] for row in rows} == {original.id}
        states = await client.execute_query(
            "SELECT * FROM source_states WHERE source_id=$uuid;", uuid=omitted.id
        )
        assert len(states) == 1
        assert states[0]["deleted"] is True
        highwater = states[0]
        execute = client.execute_query
        injected = False
        new_source = None

        async def race(statement, **params):
            nonlocal injected, new_source
            if "archive destination changed before restore" in statement and not injected:
                injected = True
                new_source = await remember("new-concurrent-org", "Concurrent source")
            return await execute(statement, **params)

        monkeypatch.setattr(client, "execute_query", race)
        failed = await content_archive.restore_content_archive_payload(
            payload, clean=True, global_scope=True
        )
        assert not failed.success
        assert failed.rows_restored == 0
        assert any("destination changed" in error for error in failed.errors)
        assert injected
        assert new_source is not None
        rows = await execute("SELECT * FROM raw_captures;")
        assert {row["uuid"] for row in rows} == {original.id, new_source.id}
        states = await execute(
            "SELECT * FROM source_states WHERE source_id=$uuid;", uuid=omitted.id
        )
        assert states == [highwater]
    finally:
        await close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "table", ["content_changefeed_cursors", "source_imports", "api_idempotency_records"]
)
@pytest.mark.parametrize("clean", [False, True])
@pytest.mark.parametrize("foreign", [False, True])
async def test_scoped_archive_does_not_replace_foreign_auxiliary_identity(
    monkeypatch, clean, foreign, table
):
    source = SurrealContentClient(url="memory://")
    destination = SurrealContentClient(url="memory://")
    source_close, destination_close = source.close, destination.close
    try:
        await bootstrap_content_schema(source)
        await bootstrap_content_schema(destination)
        monkeypatch.setattr(source, "close", AsyncMock())
        monkeypatch.setattr(destination, "close", AsyncMock())
        org_a, org_b, identity = str(uuid4()), str(uuid4()), str(uuid4())
        fields = {
            "content_changefeed_cursors": {
                "table_name": "raw_captures",
                "consumer_name": "fixture",
                "versionstamp": 7,
            },
            "source_imports": {"principal_id": "owner"},
            "api_idempotency_records": {
                "principal_id": "owner",
                "idempotency_key": "fixture",
                "method": "POST",
                "path": "/fixture",
                "request_hash": "fixture",
                "response_status_code": 200,
                "created_at": datetime.now(UTC),
            },
        }[table]
        for client, org in [(source, org_a), (destination, org_b if foreign else org_a)]:
            await client.execute_query(
                f"CREATE {table} CONTENT $record;",
                record={"uuid": identity, "organization_id": org, **fields},
            )
        before = await destination.execute_query(f"SELECT * FROM {table};")  # noqa: S608
        monkeypatch.setattr(content_archive, "build_surreal_content_client", lambda: source)
        payload = await content_archive.export_content_archive_payload(org_a)
        monkeypatch.setattr(content_archive, "build_surreal_content_client", lambda: destination)
        result = await content_archive.restore_content_archive_payload(payload, clean=clean)
        after = await destination.execute_query(f"SELECT * FROM {table};")  # noqa: S608
        if foreign:
            assert not result.success
            assert after == before, result.errors
        else:
            assert result.success, result.errors
            assert len(after) == 1
            assert after[0]["organization_id"] == org_a
    finally:
        await source_close()
        await destination_close()
