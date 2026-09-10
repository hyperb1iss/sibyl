"""API privacy purge and archive restore retain source-state high water."""

from sibyl_core.memory_pipeline.observations import SourceIdentity, SourceKind
from sibyl_core.services.source_state_store import load_source_snapshot


async def test_raw_source_generation_survives_actual_purge_and_archive_restore(monkeypatch) -> None:
    from contextlib import asynccontextmanager
    from datetime import UTC, datetime, timedelta
    from unittest.mock import AsyncMock
    from uuid import uuid4

    from sibyl.persistence import content_archive
    from sibyl.persistence.surreal import content as api_content
    from sibyl_core.backends.surreal import SurrealContentClient
    from sibyl_core.backends.surreal.content_schema import bootstrap_content_schema
    from sibyl_core.services import content_client
    from sibyl_core.services.content_raw_persistence import remember_raw_memory
    from sibyl_core.services.source_state_store import RawSourceSnapshot

    client = SurrealContentClient(url="memory://")
    await bootstrap_content_schema(client)
    close = client.close
    monkeypatch.setattr(client, "close", AsyncMock())

    @asynccontextmanager
    async def session():
        yield client

    monkeypatch.setattr(content_client, "surreal_content_client", session)
    monkeypatch.setattr(api_content, "surreal_content_client", session)
    monkeypatch.setattr(content_archive, "build_surreal_content_client", lambda: client)
    org, user = str(uuid4()), str(uuid4())
    try:
        raw = await remember_raw_memory(
            organization_id=org,
            principal_id=user,
            source_id="capture",
            raw_content="Original raw evidence",
            embedding_provider=None,
        )
        source = SourceIdentity(org, SourceKind.RAW_CAPTURE, raw.id)

        async def snapshot():
            return await load_source_snapshot(
                source, organization_id=org, execute_query=client.execute_query
            )

        original = await snapshot()
        assert isinstance(original, RawSourceSnapshot)
        await client.execute_query(
            "UPDATE raw_captures SET retrieval_count += 1, revision += 1 WHERE uuid=$uuid;",
            uuid=raw.id,
        )
        used = await snapshot()
        assert isinstance(used, RawSourceSnapshot)
        assert used.observation.same_evidence(original.observation)
        await client.execute_query(
            "UPDATE raw_captures SET metadata.memory_lifecycle = {state:'hidden'} WHERE uuid=$uuid;",
            uuid=raw.id,
        )
        hidden = await snapshot()
        assert isinstance(hidden, RawSourceSnapshot)
        assert hidden.observation.generation > used.observation.generation
        await client.execute_query(
            "UPDATE raw_captures SET metadata.memory_lifecycle = NONE WHERE uuid=$uuid;",
            uuid=raw.id,
        )
        archive = await content_archive.export_content_archive_payload(org)
        await api_content.soft_delete_private_raw_captures_for_user(
            user_id=user, purge_after=datetime.now(UTC) - timedelta(seconds=1)
        )
        assert await snapshot() is None
        await api_content.purge_due_deleted_raw_captures(now=datetime.now(UTC))
        assert not await client.execute_query(
            "SELECT * FROM raw_captures WHERE uuid=$uuid;", uuid=raw.id
        )
        retired = await client.execute_query(
            "SELECT * FROM source_states WHERE source_id=$uuid;", uuid=raw.id
        )
        assert retired[0]["deleted"] is True
        result = await content_archive.restore_content_archive_payload(archive)
        assert not result.errors
        assert await snapshot() is None
        assert any(
            row["source_id"] == raw.id and row["reason"] == "retained_tombstone"
            for row in result.integrity_conflicts
        )
        result = await content_archive.restore_content_archive_payload(archive, clean=True)
        assert not result.errors
        assert await snapshot() is None
        retained = await client.execute_query(
            "SELECT * FROM source_states WHERE source_id=$uuid;", uuid=raw.id
        )
        assert retained == retired
    finally:
        await close()
