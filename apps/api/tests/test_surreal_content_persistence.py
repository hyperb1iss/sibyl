from __future__ import annotations

import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
import pytest_asyncio

from sibyl.db.models import (
    Backup,
    BackupSettings,
    ChunkType,
    CrawledDocument,
    RawCapture,
    SourceType,
    SystemSetting,
)
from sibyl.persistence import content_archive
from sibyl.persistence.content_archive import restore_content_archive_payload
from sibyl.persistence.surreal import backups as surreal_backups, content as surreal_content
from sibyl.persistence.surreal.backups import (
    attach_backup_job,
    create_backup_record,
    delete_backup_record,
    get_backup,
    get_backup_retention,
    get_backup_settings,
    list_backups,
    list_enabled_backup_settings,
    update_backup_record,
    update_backup_settings,
)
from sibyl.persistence.surreal.content import (
    create_crawl_source_record,
    delete_crawl_source_record,
    delete_crawled_document_record,
    get_link_graph_status_payload,
    save_crawl_source_record,
    save_crawled_document_record,
    save_raw_capture_record,
)
from sibyl.persistence.surreal.system_settings import (
    delete_system_setting,
    get_system_setting,
    list_system_settings,
    save_system_setting,
)
from sibyl_core.backends.surreal import SurrealContentClient, bootstrap_content_schema

pytest.importorskip("surrealdb")


def _normalize_records(result: object) -> list[dict[str, object]]:
    if result is None:
        return []
    if isinstance(result, dict):
        return [result]
    if not isinstance(result, list):
        return []

    records: list[dict[str, object]] = []
    for item in result:
        if isinstance(item, dict):
            records.append(item)
            continue
        if not isinstance(item, list):
            continue
        for nested in item:
            if isinstance(nested, dict):
                records.append(nested)
    return records


class _RecordingContentClient:
    def __init__(self, response: object) -> None:
        self.response = response
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def execute_query(self, query: str, **kwargs: object) -> object:
        self.calls.append((query, kwargs))
        return self.response


class _SequencedContentClient:
    def __init__(self, responses: list[object]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def execute_query(self, query: str, **kwargs: object) -> object:
        self.calls.append((query, kwargs))
        if query.strip().startswith("UPSERT ") and "CONTENT $record" in query:
            return [kwargs["record"]]
        return self.responses.pop(0)


@pytest_asyncio.fixture
async def surreal_content_client() -> SurrealContentClient:
    client = SurrealContentClient(url="memory://")
    await bootstrap_content_schema(client, reset=True)
    try:
        yield client
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_surreal_content_replace_record_uses_single_upsert_statement() -> None:
    source_id = uuid4()
    record = {
        "uuid": str(source_id),
        "organization_id": str(uuid4()),
        "name": "Docs",
    }
    client = _RecordingContentClient([record])

    saved = await surreal_content._replace_record(
        client,
        "crawl_sources",
        uuid=source_id,
        record=record,
    )

    assert saved["uuid"] == str(source_id)
    assert len(client.calls) == 1
    query, params = client.calls[0]
    assert "UPSERT crawl_sources CONTENT $record WHERE uuid = $uuid" in query
    assert "DELETE FROM crawl_sources" not in query
    assert params == {"uuid": str(source_id), "record": record}


@pytest.mark.asyncio
async def test_surreal_backup_settings_save_uses_upsert_statement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    org_id = uuid4()
    settings_id = uuid4()
    existing = {"uuid": str(settings_id), "organization_id": str(org_id)}
    updated = {
        **existing,
        "enabled": True,
        "schedule": "0 3 * * *",
        "retention_days": 14,
        "include_database_dump": False,
        "include_graph": True,
    }
    client = _SequencedContentClient([[existing], [existing], [updated]])

    @asynccontextmanager
    async def client_scope():
        yield client

    monkeypatch.setattr(surreal_backups, "surreal_content_client", client_scope)

    saved = await surreal_backups.update_backup_settings(
        org_id,
        schedule="0 3 * * *",
        retention_days=14,
        include_database_dump=False,
    )

    queries = [query for query, _params in client.calls]
    assert saved.schedule == "0 3 * * *"
    assert any("UPSERT backup_settings CONTENT $record WHERE uuid = $uuid" in query for query in queries)
    assert not any("DELETE FROM backup_settings" in query for query in queries)


@pytest.mark.asyncio
async def test_surreal_backup_record_save_uses_upsert_statement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    org_id = uuid4()
    record = {
        "uuid": str(uuid4()),
        "organization_id": str(org_id),
        "backup_id": "backup_upsert",
        "status": "pending",
        "include_database_dump": False,
        "include_graph": True,
    }
    client = _SequencedContentClient([[], [record]])

    @asynccontextmanager
    async def client_scope():
        yield client

    monkeypatch.setattr(surreal_backups, "surreal_content_client", client_scope)

    saved = await surreal_backups.create_backup_record(
        org_id=org_id,
        backup_id="backup_upsert",
        include_database_dump=False,
        include_graph=True,
        created_by_user_id=None,
    )

    queries = [query for query, _params in client.calls]
    assert saved.backup_id == "backup_upsert"
    assert any("UPSERT backups CONTENT $record WHERE uuid = $uuid" in query for query in queries)
    assert not any("DELETE FROM backups" in query for query in queries)


@pytest.mark.asyncio
async def test_surreal_delete_document_batches_chunk_delete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    org_id = uuid4()
    source_id = uuid4()
    document_id = uuid4()
    client = _SequencedContentClient(
        [
            {
                "uuid": str(document_id),
                "source_id": str(source_id),
                "url": "https://example.test/page",
                "title": "Page",
                "raw_content": "body",
                "content": "body",
                "content_hash": "hash",
            },
            {
                "uuid": str(source_id),
                "organization_id": str(org_id),
                "name": "Docs",
                "url": "https://example.test",
                "source_type": SourceType.WEBSITE.value,
                "document_count": 1,
                "chunk_count": 2,
            },
            [{"uuid": str(uuid4())}, {"uuid": str(uuid4())}],
            [],
            [],
        ]
    )

    @asynccontextmanager
    async def client_scope():
        yield client

    monkeypatch.setattr(surreal_content, "surreal_content_client", client_scope)

    deleted = await delete_crawled_document_record(
        None,
        document_id=document_id,
        organization_id=org_id,
    )

    assert deleted is not None
    queries = [query for query, _params in client.calls]
    assert "DELETE FROM document_chunks WHERE document_id = $document_id;" in queries
    assert "DELETE FROM document_chunks WHERE uuid = $uuid;" not in queries


@pytest.mark.asyncio
async def test_surreal_content_schema_bootstrap_creates_tables(
    surreal_content_client: SurrealContentClient,
) -> None:
    info = await surreal_content_client.execute_query("INFO FOR DB;")
    tables = info.get("tables", {}) if isinstance(info, dict) else {}

    for table_name in (
        "crawl_sources",
        "crawled_documents",
        "document_chunks",
        "raw_captures",
        "system_settings",
        "backup_settings",
        "backups",
    ):
        assert table_name in tables


@pytest.mark.asyncio
async def test_content_archive_restore_preserves_embeddings_and_metadata(
    surreal_content_client: SurrealContentClient,
) -> None:
    source_id = uuid4()
    document_id = uuid4()
    chunk_id = uuid4()
    capture_id = uuid4()
    backup_settings_id = uuid4()
    backup_id = uuid4()
    payload = {
        "version": "1.0",
        "created_at": "2026-04-21T03:00:00+00:00",
        "tables": {
            "crawl_sources": [
                {
                    "id": str(source_id),
                    "organization_id": "org-123",
                    "name": "Docs",
                    "url": "https://docs.example.com",
                    "source_type": "website",
                    "include_patterns": ["/docs/**"],
                    "exclude_patterns": [],
                    "tags": ["docs"],
                    "categories": ["reference"],
                    "document_count": 1,
                    "chunk_count": 1,
                    "total_tokens": 42,
                    "created_at": "2026-04-20T00:00:00+00:00",
                    "updated_at": "2026-04-20T00:00:00+00:00",
                }
            ],
            "crawled_documents": [
                {
                    "id": str(document_id),
                    "source_id": str(source_id),
                    "url": "https://docs.example.com/page",
                    "title": "Docs Page",
                    "content": "Chunk me",
                    "raw_content": "<html>Chunk me</html>",
                    "section_path": ["Docs"],
                    "headings": ["Docs Page"],
                    "links": ["https://docs.example.com/next"],
                    "code_languages": ["python"],
                    "created_at": "2026-04-20T00:00:00+00:00",
                    "updated_at": "2026-04-20T00:00:00+00:00",
                }
            ],
            "document_chunks": [
                {
                    "id": str(chunk_id),
                    "document_id": str(document_id),
                    "chunk_index": 0,
                    "chunk_type": "text",
                    "content": "Chunk me",
                    "heading_path": ["Docs"],
                    "embedding": [0.1] * 1536,
                    "entity_ids": ["entity-1"],
                    "created_at": "2026-04-20T00:00:00+00:00",
                    "updated_at": "2026-04-20T00:00:00+00:00",
                }
            ],
            "raw_captures": [
                {
                    "id": str(capture_id),
                    "organization_id": "org-123",
                    "title": "Capture",
                    "raw_content": "captured",
                    "entity_type": "note",
                    "tags": ["capture"],
                    "metadata": {"source": "manual"},
                    "created_at": "2026-04-20T00:00:00+00:00",
                }
            ],
            "system_settings": [
                {
                    "key": "openai_api_key",
                    "value": "encrypted",
                    "is_secret": True,
                    "description": "OpenAI key",
                    "created_at": "2026-04-20T00:00:00+00:00",
                    "updated_at": "2026-04-20T00:00:00+00:00",
                }
            ],
            "backup_settings": [
                {
                    "id": str(backup_settings_id),
                    "organization_id": "org-123",
                    "enabled": True,
                    "schedule": "0 2 * * *",
                    "retention_days": 30,
                    "include_database_dump": True,
                    "include_graph": True,
                    "created_at": "2026-04-20T00:00:00+00:00",
                    "updated_at": "2026-04-20T00:00:00+00:00",
                }
            ],
            "backups": [
                {
                    "id": str(backup_id),
                    "organization_id": "org-123",
                    "backup_id": "backup_123",
                    "status": "completed",
                    "size_bytes": 128,
                    "include_database_dump": True,
                    "include_graph": True,
                    "created_at": "2026-04-20T00:00:00+00:00",
                    "updated_at": "2026-04-20T00:00:00+00:00",
                }
            ],
        },
        "row_counts": {
            "crawl_sources": 1,
            "crawled_documents": 1,
            "document_chunks": 1,
            "raw_captures": 1,
            "system_settings": 1,
            "backup_settings": 1,
            "backups": 1,
        },
        "total_rows": 7,
    }

    with (
        patch.object(surreal_content_client, "close", AsyncMock()),
        patch(
            "sibyl.persistence.content_archive.build_surreal_content_client",
            return_value=surreal_content_client,
        ),
    ):
        result = await restore_content_archive_payload(payload, clean=True)

    assert result.success is True
    assert result.tables_restored == 7
    assert result.rows_restored == 7

    chunk_rows = _normalize_records(
        await surreal_content_client.execute_query(
            "SELECT * FROM document_chunks WHERE uuid = $uuid LIMIT 1;",
            uuid=str(chunk_id),
        )
    )
    capture_rows = _normalize_records(
        await surreal_content_client.execute_query(
            "SELECT * FROM raw_captures WHERE uuid = $uuid LIMIT 1;",
            uuid=str(capture_id),
        )
    )
    setting_rows = _normalize_records(
        await surreal_content_client.execute_query(
            "SELECT * FROM system_settings WHERE key = $key LIMIT 1;",
            key="openai_api_key",
        )
    )
    backup_setting_rows = _normalize_records(
        await surreal_content_client.execute_query(
            "SELECT * FROM backup_settings WHERE organization_id = $organization_id LIMIT 1;",
            organization_id="org-123",
        )
    )
    backup_rows = _normalize_records(
        await surreal_content_client.execute_query(
            "SELECT * FROM backups WHERE backup_id = $backup_id LIMIT 1;",
            backup_id="backup_123",
        )
    )

    assert chunk_rows[0]["document_id"] == str(document_id)
    assert chunk_rows[0]["embedding"] == [0.1] * 1536
    assert capture_rows[0]["metadata"] == {"source": "manual"}
    assert setting_rows[0]["is_secret"] is True
    assert backup_setting_rows[0]["include_database_dump"] is True
    assert backup_rows[0]["include_database_dump"] is True


@pytest.mark.asyncio
async def test_content_archive_restore_parses_pgvector_text_embeddings(
    surreal_content_client: SurrealContentClient,
) -> None:
    source_id = uuid4()
    document_id = uuid4()
    chunk_id = uuid4()
    embedding = [0.1] * 1536
    payload = {
        "version": "1.0",
        "created_at": "2026-04-21T03:00:00+00:00",
        "tables": {
            "crawl_sources": [
                {
                    "id": str(source_id),
                    "organization_id": "org-123",
                    "name": "Docs",
                    "url": "https://docs.example.com",
                    "source_type": "website",
                    "created_at": "2026-04-20T00:00:00+00:00",
                    "updated_at": "2026-04-20T00:00:00+00:00",
                }
            ],
            "crawled_documents": [
                {
                    "id": str(document_id),
                    "source_id": str(source_id),
                    "url": "https://docs.example.com/page",
                    "title": "Docs Page",
                    "content": "Chunk me",
                    "created_at": "2026-04-20T00:00:00+00:00",
                    "updated_at": "2026-04-20T00:00:00+00:00",
                }
            ],
            "document_chunks": [
                {
                    "id": str(chunk_id),
                    "document_id": str(document_id),
                    "chunk_index": 0,
                    "chunk_type": "text",
                    "content": "Chunk me",
                    "heading_path": ["Docs"],
                    "embedding": json.dumps(embedding),
                    "entity_ids": [],
                    "created_at": "2026-04-20T00:00:00+00:00",
                    "updated_at": "2026-04-20T00:00:00+00:00",
                }
            ],
            "raw_captures": [],
            "system_settings": [],
            "backup_settings": [],
            "backups": [],
        },
        "row_counts": {
            "crawl_sources": 1,
            "crawled_documents": 1,
            "document_chunks": 1,
            "raw_captures": 0,
            "system_settings": 0,
            "backup_settings": 0,
            "backups": 0,
        },
        "total_rows": 3,
    }

    with (
        patch.object(surreal_content_client, "close", AsyncMock()),
        patch(
            "sibyl.persistence.content_archive.build_surreal_content_client",
            return_value=surreal_content_client,
        ),
    ):
        result = await restore_content_archive_payload(payload, clean=True)

    assert result.success is True
    chunk_rows = _normalize_records(
        await surreal_content_client.execute_query(
            "SELECT * FROM document_chunks WHERE uuid = $uuid LIMIT 1;",
            uuid=str(chunk_id),
        )
    )
    assert chunk_rows[0]["embedding"] == embedding


@pytest.mark.asyncio
async def test_surreal_content_write_helpers_round_trip(
    surreal_content_client: SurrealContentClient,
) -> None:
    org_id = uuid4()

    with (
        patch.object(surreal_content_client, "close", AsyncMock()),
        patch(
            "sibyl.persistence.surreal.content.build_surreal_content_client",
            return_value=surreal_content_client,
        ),
    ):
        source = await create_crawl_source_record(
            None,
            name="Docs",
            url="https://docs.example.com/",
            organization_id=org_id,
            source_type=SourceType.WEBSITE,
            description="Reference docs",
            crawl_depth=3,
            include_patterns=["/docs/**"],
            exclude_patterns=["/blog/**"],
        )
        source.document_count = 1
        source.chunk_count = 1
        source.current_job_id = "job-123"
        source = await save_crawl_source_record(None, source=source)

        document = await save_crawled_document_record(
            None,
            document=CrawledDocument(
                source_id=source.id,
                url="https://docs.example.com/page",
                title="Page",
                raw_content="<h1>Page</h1>",
                content="# Page\nbody",
                content_hash="hash",
                word_count=2,
                token_count=4,
                has_code=False,
                headings=["Page"],
            ),
        )

        await surreal_content_client.execute_query(
            "CREATE document_chunks CONTENT $record;",
            record={
                "uuid": str(uuid4()),
                "document_id": str(document.id),
                "chunk_index": 0,
                "chunk_type": ChunkType.TEXT.value,
                "content": "body",
                "token_count": 2,
                "start_char": 0,
                "end_char": 4,
                "heading_path": ["Page"],
                "is_complete": True,
                "has_entities": False,
                "entity_ids": [],
            },
        )

        capture = await save_raw_capture_record(
            None,
            capture=RawCapture(
                organization_id=org_id,
                entity_id="episode_123",
                title="Quick note",
                raw_content="captured",
                entity_type="episode",
                tags=["alpha"],
                metadata_={"review_state": "pending"},
            ),
        )

        status = await get_link_graph_status_payload(None, organization_id=org_id)
        deleted_document = await delete_crawled_document_record(
            None,
            document_id=document.id,
            organization_id=org_id,
        )
        deleted_source = await delete_crawl_source_record(
            None,
            source_id=source.id,
            organization_id=org_id,
        )

    capture_rows = _normalize_records(
        await surreal_content_client.execute_query(
            "SELECT * FROM raw_captures WHERE uuid = $uuid LIMIT 1;",
            uuid=str(capture.id),
        )
    )
    source_rows = _normalize_records(
        await surreal_content_client.execute_query(
            "SELECT * FROM crawl_sources WHERE uuid = $uuid LIMIT 1;",
            uuid=str(source.id),
        )
    )
    document_rows = _normalize_records(
        await surreal_content_client.execute_query(
            "SELECT * FROM crawled_documents WHERE uuid = $uuid LIMIT 1;",
            uuid=str(document.id),
        )
    )

    assert source.current_job_id == "job-123"
    assert capture_rows[0]["metadata"] == {"review_state": "pending"}
    assert status.total_chunks == 1
    assert status.chunks_with_entities == 0
    assert [(item.source_id, item.pending) for item in status.sources] == [(str(source.id), 1)]
    assert deleted_document is not None
    assert deleted_document[1] == 1
    assert deleted_source is not None
    assert source_rows == []
    assert document_rows == []


@pytest.mark.asyncio
async def test_surreal_system_setting_helpers_round_trip(
    surreal_content_client: SurrealContentClient,
) -> None:
    @asynccontextmanager
    async def fake_content_client():
        yield surreal_content_client

    with patch(
        "sibyl.persistence.surreal.system_settings.surreal_content_client",
        fake_content_client,
    ):
        saved = await save_system_setting(
            None,
            setting=SystemSetting(
                key="openai_api_key",
                value="encrypted",
                is_secret=True,
                description="OpenAI key",
            ),
        )
        fetched = await get_system_setting(None, key="openai_api_key")
        listed = await list_system_settings(None)
        deleted = await delete_system_setting(None, key="openai_api_key")
        missing = await get_system_setting(None, key="openai_api_key")

    assert saved.key == "openai_api_key"
    assert fetched is not None
    assert fetched.value == "encrypted"
    assert fetched.is_secret is True
    assert [setting.key for setting in listed] == ["openai_api_key"]
    assert deleted is True
    assert missing is None


@pytest.mark.asyncio
async def test_content_archive_export_reads_from_surreal_backend(
    monkeypatch: pytest.MonkeyPatch,
    surreal_content_client: SurrealContentClient,
) -> None:
    org_id = str(uuid4())
    await surreal_content_client.execute_query(
        "CREATE system_settings CONTENT $record;",
        record={
            "key": "exported_setting",
            "value": "present",
            "is_secret": False,
            "description": "export me",
        },
    )
    await surreal_content_client.execute_query(
        "CREATE backup_settings CONTENT $record;",
        record={
            "uuid": str(uuid4()),
            "organization_id": org_id,
            "enabled": True,
            "schedule": "0 2 * * *",
            "retention_days": 14,
            "include_database_dump": False,
            "include_graph": True,
        },
    )
    await surreal_content_client.execute_query(
        "CREATE backups CONTENT $record;",
        record={
            "uuid": str(uuid4()),
            "organization_id": org_id,
            "backup_id": "backup_export",
            "status": "completed",
            "size_bytes": 128,
            "include_database_dump": False,
            "include_graph": True,
        },
    )
    close = AsyncMock()

    monkeypatch.setattr(content_archive.config_module.settings, "store", "surreal")
    monkeypatch.setattr(
        content_archive,
        "build_surreal_content_client",
        lambda: surreal_content_client,
    )
    monkeypatch.setattr(surreal_content_client, "close", close)

    payload = await content_archive.export_content_archive_payload()

    assert payload["row_counts"]["system_settings"] == 1
    assert payload["row_counts"]["backup_settings"] == 1
    assert payload["row_counts"]["backups"] == 1
    assert payload["total_rows"] == 3
    assert payload["tables"]["system_settings"][0]["key"] == "exported_setting"
    assert payload["tables"]["backup_settings"][0]["include_database_dump"] is False
    assert payload["tables"]["backups"][0]["include_database_dump"] is False
    close.assert_awaited_once()


@pytest.mark.asyncio
async def test_surreal_backup_helpers_round_trip(
    surreal_content_client: SurrealContentClient,
) -> None:
    org_id = uuid4()

    @asynccontextmanager
    async def fake_content_client():
        yield surreal_content_client

    with patch(
        "sibyl.persistence.surreal.backups.surreal_content_client",
        fake_content_client,
    ):
        settings = await get_backup_settings(org_id)
        updated_settings = await update_backup_settings(
            org_id,
            enabled=True,
            schedule="0 3 * * *",
            retention_days=14,
            include_database_dump=False,
            include_graph=True,
        )
        enabled = await list_enabled_backup_settings()
        created = await create_backup_record(
            org_id=org_id,
            backup_id="backup_fixed",
            include_database_dump=False,
            include_graph=True,
            created_by_user_id=None,
            triggered_by="manual",
        )
        attached = await attach_backup_job(created.id, "job-123")
        completed = await update_backup_record(
            "backup_fixed",
            status="completed",
            filename="sibyl_backup_fixed.tar.gz",
            file_path="backups/sibyl_backup_fixed.tar.gz",
            size_bytes=128,
            entity_count=3,
            relationship_count=5,
            duration_seconds=1.5,
        )
        listed = await list_backups(org_id, limit=10, offset=0)
        fetched = await get_backup(org_id, "backup_fixed")
        retention = await get_backup_retention(org_id, None)
        setting_rows = _normalize_records(
            await surreal_content_client.execute_query(
                "SELECT * FROM backup_settings WHERE organization_id = $organization_id LIMIT 1;",
                organization_id=str(org_id),
            )
        )
        backup_rows = _normalize_records(
            await surreal_content_client.execute_query(
                "SELECT * FROM backups WHERE backup_id = $backup_id LIMIT 1;",
                backup_id="backup_fixed",
            )
        )
        deleted = await delete_backup_record(org_id, "backup_fixed")

    assert isinstance(settings, BackupSettings)
    assert updated_settings.schedule == "0 3 * * *"
    assert updated_settings.retention_days == 14
    assert updated_settings.include_database_dump is False
    assert [item.organization_id for item in enabled] == [org_id]
    assert isinstance(created, Backup)
    assert created.include_database_dump is False
    assert attached.job_id == "job-123"
    assert completed is not None
    assert completed.status == "completed"
    assert completed.filename == "sibyl_backup_fixed.tar.gz"
    assert listed.total == 1
    assert listed.backups[0].backup_id == "backup_fixed"
    assert fetched.backup_id == "backup_fixed"
    assert retention == 14
    assert setting_rows[0]["include_database_dump"] is False
    assert backup_rows[0]["include_database_dump"] is False
    assert deleted.backup_id == "backup_fixed"
