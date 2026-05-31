from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
import pytest_asyncio

from sibyl.crawler.service import SourceAlreadyExistsError
from sibyl.persistence import content_archive
from sibyl.persistence.backups_common import BackupRecord, BackupSettingsRecord
from sibyl.persistence.content_archive import restore_content_archive_payload
from sibyl.persistence.content_common import (
    ApiIdempotencyRecord,
    CrawledDocumentRecord,
    RawCaptureRecord,
)
from sibyl.persistence.settings_types import SystemSettingRecord
from sibyl.persistence.surreal import (
    backups as surreal_backups,
    content as surreal_content,
    system_settings as surreal_system_settings,
)
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
    purge_due_deleted_raw_captures,
    save_api_idempotency_record,
    save_crawl_source_record,
    save_crawled_document_record,
    save_raw_capture_record,
    soft_delete_private_raw_captures_for_user,
    update_raw_capture_review_state,
)
from sibyl.persistence.surreal.system_settings import (
    delete_system_setting,
    get_system_setting,
    list_system_settings,
    save_system_setting,
)
from sibyl_core.backends.surreal import SurrealContentClient, bootstrap_content_schema
from sibyl_core.models import ChunkType, SourceType

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
    await surreal_content.close_shared_surreal_content_client()
    client = SurrealContentClient(url="memory://")
    await bootstrap_content_schema(client, reset=True)
    try:
        yield client
    finally:
        await surreal_content.close_shared_surreal_content_client()
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
    assert (
        "UPSERT crawl_sources CONTENT $record "
        "WHERE uuid = $uuid AND organization_id = $organization_id"
    ) in query
    assert "DELETE FROM crawl_sources" not in query
    assert params == {
        "uuid": str(source_id),
        "organization_id": record["organization_id"],
        "record": record,
    }


@pytest.mark.asyncio
async def test_surreal_content_replace_record_requires_org_scope() -> None:
    client = _RecordingContentClient([])

    with pytest.raises(RuntimeError, match="requires organization_id"):
        await surreal_content._replace_record(
            client,
            "raw_captures",
            uuid=uuid4(),
            record={"uuid": str(uuid4()), "title": "missing scope"},
        )

    assert client.calls == []


@pytest.mark.asyncio
async def test_api_idempotency_record_round_trips_through_surreal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    org_id = uuid4()
    record = ApiIdempotencyRecord(
        organization_id=org_id,
        principal_id="user-123",
        idempotency_key="idem-123",
        method="POST",
        path="/entities",
        request_hash="hash-123",
        response_status_code=201,
        response_body={"id": "episode_123"},
    )
    client = _SequencedContentClient([])

    @asynccontextmanager
    async def client_scope():
        yield client

    monkeypatch.setattr(surreal_content, "surreal_content_client", client_scope)

    saved = await save_api_idempotency_record(None, record=record)

    assert saved == record
    query, params = client.calls[0]
    assert (
        "UPSERT api_idempotency_records CONTENT $record "
        "WHERE uuid = $uuid AND organization_id = $organization_id"
    ) in query
    assert params["uuid"] == str(record.id)
    assert params["organization_id"] == str(record.organization_id)
    assert params["record"]["response_body"] == {"id": "episode_123"}


def test_raw_capture_record_preserves_first_class_lifecycle_fields() -> None:
    org_id = uuid4()
    user_id = uuid4()
    captured_at = datetime(2026, 5, 30, 12, 0, tzinfo=UTC).replace(tzinfo=None)
    deleted_at = datetime(2026, 5, 31, 12, 0, tzinfo=UTC).replace(tzinfo=None)
    purge_after = deleted_at + timedelta(days=30)
    capture = RawCaptureRecord(
        organization_id=org_id,
        title="Project import",
        raw_content="captured body",
        entity_type="note",
        source_id="source:project:1",
        principal_id=str(user_id),
        memory_scope="project",
        scope_key="project_123",
        agent_id="agent_123",
        project_id="project_123",
        review_state="deferred",
        entity_id="note_123",
        tags=["raw"],
        metadata={"source": "manual"},
        provenance={"source_import_id": "import_123"},
        capture_surface="source_import",
        created_by_user_id=user_id,
        captured_at=captured_at,
        deleted_at=deleted_at,
        purge_after=purge_after,
    )

    record = surreal_content._raw_capture_record(capture)
    round_tripped = surreal_content._raw_capture_from_record(record)

    assert record["source_id"] == "source:project:1"
    assert record["principal_id"] == str(user_id)
    assert record["memory_scope"] == "project"
    assert record["scope_key"] == "project_123"
    assert record["agent_id"] == "agent_123"
    assert record["project_id"] == "project_123"
    assert record["review_state"] == "deferred"
    assert record["provenance"] == {"source_import_id": "import_123"}
    assert record["captured_at"] == captured_at
    assert record["deleted_at"] == deleted_at
    assert record["purge_after"] == purge_after
    assert round_tripped.source_id == "source:project:1"
    assert round_tripped.principal_id == str(user_id)
    assert round_tripped.memory_scope == "project"
    assert round_tripped.scope_key == "project_123"
    assert round_tripped.review_state == "deferred"
    assert round_tripped.provenance == {"source_import_id": "import_123"}
    assert round_tripped.captured_at == captured_at
    assert round_tripped.deleted_at == deleted_at
    assert round_tripped.purge_after == purge_after


@pytest.mark.asyncio
async def test_link_graph_status_uses_server_side_aggregate_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    org_id = uuid4()
    source_a_id = uuid4()
    source_b_id = uuid4()
    source_c_id = uuid4()
    unknown_source_id = uuid4()
    client = _SequencedContentClient(
        [
            [
                {
                    "uuid": str(source_a_id),
                    "organization_id": str(org_id),
                    "name": "Docs A",
                    "url": "https://a.example.com",
                    "source_type": SourceType.WEBSITE.value,
                },
                {
                    "uuid": str(source_b_id),
                    "organization_id": str(org_id),
                    "name": "Docs B",
                    "url": "https://b.example.com",
                    "source_type": SourceType.WEBSITE.value,
                },
                {
                    "uuid": str(source_c_id),
                    "organization_id": str(org_id),
                    "name": "Docs C",
                    "url": "https://c.example.com",
                    "source_type": SourceType.WEBSITE.value,
                },
            ],
            [{"total": 6}],
            [{"total": 2}],
            [
                {"source_id": str(source_a_id), "pending": 3},
                {"source_id": str(source_b_id), "pending": 1},
                {"source_id": str(unknown_source_id), "pending": 7},
            ],
        ]
    )

    @asynccontextmanager
    async def client_scope():
        yield client

    monkeypatch.setattr(surreal_content, "surreal_content_client", client_scope)

    status = await get_link_graph_status_payload(None, organization_id=org_id)

    assert status.total_chunks == 6
    assert status.chunks_with_entities == 2
    assert {item.source_id: item.pending for item in status.sources} == {
        str(source_a_id): 3,
        str(source_b_id): 1,
    }
    queries = [query for query, _ in client.calls]
    assert len(queries) == 4
    assert all("SELECT * FROM document_chunks" not in query for query in queries)
    assert any("SELECT count() AS total FROM document_chunks" in query for query in queries)
    assert "GROUP BY source_id" in queries[-1]


@pytest.mark.asyncio
async def test_soft_delete_private_raw_captures_marks_purge_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id = uuid4()
    capture_id = uuid4()
    purge_after = datetime.now(UTC).replace(tzinfo=None) + timedelta(days=30)
    client = _SequencedContentClient(
        [
            [
                {
                    "uuid": str(capture_id),
                    "principal_id": str(user_id),
                    "memory_scope": "private",
                    "metadata": {"existing": "yes"},
                }
            ],
            [{"uuid": str(capture_id)}],
        ]
    )

    @asynccontextmanager
    async def client_scope():
        yield client

    monkeypatch.setattr(surreal_content, "surreal_content_client", client_scope)

    count = await soft_delete_private_raw_captures_for_user(
        user_id=user_id,
        purge_after=purge_after,
    )

    assert count == 1
    select_query, select_params = client.calls[0]
    update_query, update_params = client.calls[1]
    assert "principal_id = $user_id" in select_query
    assert "memory_scope = 'private'" in select_query
    assert select_params["user_id"] == str(user_id)
    assert "review_state = 'deleted'" in update_query
    assert update_params["metadata"]["existing"] == "yes"
    assert update_params["metadata"]["review_state"] == "deleted"
    assert update_params["metadata"]["deleted_by_user_id"] == str(user_id)
    assert update_params["purge_after"] == purge_after


@pytest.mark.asyncio
async def test_purge_due_deleted_raw_captures_deletes_due_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(UTC).replace(tzinfo=None)
    client = _SequencedContentClient([[{"uuid": str(uuid4())}, {"uuid": str(uuid4())}]])

    @asynccontextmanager
    async def client_scope():
        yield client

    monkeypatch.setattr(surreal_content, "surreal_content_client", client_scope)

    purged = await purge_due_deleted_raw_captures(now=now)

    assert len(purged) == 2
    assert purged[0]["uuid"]
    query, params = client.calls[0]
    assert "DELETE FROM raw_captures" in query
    assert "purge_after <= $now" in query
    assert params["now"] == now


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
    assert any(
        "UPSERT backup_settings CONTENT $record WHERE uuid = $uuid" in query for query in queries
    )
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
async def test_surreal_system_setting_save_uses_upsert_statement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = {
        "key": "openai_api_key",
        "value": "encrypted",
        "is_secret": True,
        "description": "OpenAI",
    }
    client = _SequencedContentClient([[], [record]])

    @asynccontextmanager
    async def client_scope():
        yield client

    monkeypatch.setattr(surreal_system_settings, "surreal_content_client", client_scope)

    saved = await surreal_system_settings.save_system_setting(
        None,
        setting=SystemSettingRecord(
            key="openai_api_key",
            value="encrypted",
            is_secret=True,
            description="OpenAI",
        ),
    )

    queries = [query for query, _params in client.calls]
    assert saved.key == "openai_api_key"
    assert any(
        "UPSERT system_settings CONTENT $record WHERE key = $key" in query for query in queries
    )
    assert not any("DELETE FROM system_settings" in query for query in queries)


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
    txns = [query for query in queries if "BEGIN TRANSACTION;" in query]
    assert len(txns) == 1
    txn = txns[0]
    assert "COMMIT TRANSACTION;" in txn
    assert (
        "DELETE FROM document_chunks "
        "WHERE document_id = $document_id AND organization_id = $organization_id;"
    ) in txn
    assert (
        "DELETE FROM crawled_documents "
        "WHERE uuid = $document_uuid AND organization_id = $organization_id;"
    ) in txn
    assert "DELETE FROM document_chunks WHERE uuid = $uuid;" not in txn


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
        "source_imports",
        "system_settings",
        "backup_settings",
        "backups",
    ):
        assert table_name in tables


@pytest.mark.asyncio
async def test_surreal_content_allows_same_source_url_across_orgs(
    surreal_content_client: SurrealContentClient,
) -> None:
    org_a = uuid4()
    org_b = uuid4()

    @asynccontextmanager
    async def fake_content_client():
        yield surreal_content_client

    with patch("sibyl.persistence.surreal.content.surreal_content_client", fake_content_client):
        first = await create_crawl_source_record(
            None,
            name="Docs A",
            url="https://docs.example.com/",
            organization_id=org_a,
            source_type=SourceType.WEBSITE,
            description=None,
            crawl_depth=2,
            include_patterns=None,
            exclude_patterns=None,
        )
        second = await create_crawl_source_record(
            None,
            name="Docs B",
            url="https://docs.example.com/",
            organization_id=org_b,
            source_type=SourceType.WEBSITE,
            description=None,
            crawl_depth=2,
            include_patterns=None,
            exclude_patterns=None,
        )

    assert first.url == "https://docs.example.com"
    assert second.url == "https://docs.example.com"
    assert first.organization_id == org_a
    assert second.organization_id == org_b


@pytest.mark.asyncio
async def test_surreal_content_rejects_duplicate_source_url_in_same_org(
    surreal_content_client: SurrealContentClient,
) -> None:
    org_id = uuid4()

    @asynccontextmanager
    async def fake_content_client():
        yield surreal_content_client

    with patch("sibyl.persistence.surreal.content.surreal_content_client", fake_content_client):
        await create_crawl_source_record(
            None,
            name="Docs",
            url="https://docs.example.com/",
            organization_id=org_id,
            source_type=SourceType.WEBSITE,
            description=None,
            crawl_depth=2,
            include_patterns=None,
            exclude_patterns=None,
        )

        with pytest.raises(SourceAlreadyExistsError):
            await create_crawl_source_record(
                None,
                name="Docs Again",
                url="https://docs.example.com",
                organization_id=org_id,
                source_type=SourceType.WEBSITE,
                description=None,
                crawl_depth=2,
                include_patterns=None,
                exclude_patterns=None,
            )


@pytest.mark.asyncio
async def test_surreal_content_allows_same_document_url_across_sources(
    surreal_content_client: SurrealContentClient,
) -> None:
    org_id = uuid4()

    @asynccontextmanager
    async def fake_content_client():
        yield surreal_content_client

    with patch("sibyl.persistence.surreal.content.surreal_content_client", fake_content_client):
        first_source = await create_crawl_source_record(
            None,
            name="Docs A",
            url="https://docs-a.example.com",
            organization_id=org_id,
            source_type=SourceType.WEBSITE,
            description=None,
            crawl_depth=2,
            include_patterns=None,
            exclude_patterns=None,
        )
        second_source = await create_crawl_source_record(
            None,
            name="Docs B",
            url="https://docs-b.example.com",
            organization_id=org_id,
            source_type=SourceType.WEBSITE,
            description=None,
            crawl_depth=2,
            include_patterns=None,
            exclude_patterns=None,
        )
        first_document = await save_crawled_document_record(
            None,
            document=CrawledDocumentRecord(
                source_id=first_source.id,
                url="https://shared.example.com/page",
                title="Shared",
            ),
        )
        second_document = await save_crawled_document_record(
            None,
            document=CrawledDocumentRecord(
                source_id=second_source.id,
                url="https://shared.example.com/page",
                title="Shared",
            ),
        )

    assert first_document.url == second_document.url
    assert first_document.source_id == first_source.id
    assert second_document.source_id == second_source.id


@pytest.mark.asyncio
async def test_content_schema_migration_rejects_duplicate_source_urls() -> None:
    client = SurrealContentClient(url="memory://")
    org_id = str(uuid4())
    now = datetime.now(UTC).replace(tzinfo=None)
    try:
        await client.execute_query(
            "CREATE schema_version:content CONTENT $record;",
            record={
                "name": "content",
                "version": 1,
                "migrations": [{"version": 1, "name": "content_schema_bootstrap"}],
                "created_at": now,
                "updated_at": now,
            },
        )
        for name in ("Docs A", "Docs B"):
            await client.execute_query(
                "CREATE crawl_sources CONTENT $record;",
                record={
                    "uuid": str(uuid4()),
                    "organization_id": org_id,
                    "name": name,
                    "url": "https://docs.example.com",
                    "source_type": SourceType.WEBSITE.value,
                },
            )

        with pytest.raises(RuntimeError, match="duplicate organization_id/url"):
            await bootstrap_content_schema(client)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_content_schema_migration_rejects_duplicate_document_urls() -> None:
    client = SurrealContentClient(url="memory://")
    source_id = str(uuid4())
    now = datetime.now(UTC).replace(tzinfo=None)
    try:
        await client.execute_query(
            "CREATE schema_version:content CONTENT $record;",
            record={
                "name": "content",
                "version": 2,
                "migrations": [
                    {"version": 1, "name": "content_schema_bootstrap"},
                    {"version": 2, "name": "content_source_url_org_scope"},
                ],
                "created_at": now,
                "updated_at": now,
            },
        )
        for title in ("Page A", "Page B"):
            await client.execute_query(
                "CREATE crawled_documents CONTENT $record;",
                record={
                    "uuid": str(uuid4()),
                    "source_id": source_id,
                    "url": "https://docs.example.com/page",
                    "title": title,
                    "raw_content": "",
                    "content": "",
                    "content_hash": "",
                },
            )

        with pytest.raises(RuntimeError, match="duplicate source_id/url"):
            await bootstrap_content_schema(client)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_content_schema_migration_rejects_orphan_documents() -> None:
    client = SurrealContentClient(url="memory://")
    now = datetime.now(UTC).replace(tzinfo=None)
    try:
        await client.execute_query(
            "CREATE schema_version:content CONTENT $record;",
            record={
                "name": "content",
                "version": 3,
                "migrations": [
                    {"version": 1, "name": "content_schema_bootstrap"},
                    {"version": 2, "name": "content_source_url_org_scope"},
                    {"version": 3, "name": "content_document_url_source_scope"},
                ],
                "created_at": now,
                "updated_at": now,
            },
        )
        await client.execute_query(
            "CREATE crawled_documents CONTENT $record;",
            record={
                "uuid": str(uuid4()),
                "source_id": str(uuid4()),
                "url": "https://docs.example.com/orphan",
                "title": "Orphan",
                "raw_content": "",
                "content": "",
                "content_hash": "",
            },
        )

        with pytest.raises(RuntimeError, match="parent crawl_sources rows are missing"):
            await bootstrap_content_schema(client)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_content_schema_migration_rejects_orphan_chunks() -> None:
    client = SurrealContentClient(url="memory://")
    now = datetime.now(UTC).replace(tzinfo=None)
    try:
        await client.execute_query(
            "CREATE schema_version:content CONTENT $record;",
            record={
                "name": "content",
                "version": 3,
                "migrations": [
                    {"version": 1, "name": "content_schema_bootstrap"},
                    {"version": 2, "name": "content_source_url_org_scope"},
                    {"version": 3, "name": "content_document_url_source_scope"},
                ],
                "created_at": now,
                "updated_at": now,
            },
        )
        await client.execute_query(
            "CREATE document_chunks CONTENT $record;",
            record={
                "uuid": str(uuid4()),
                "document_id": str(uuid4()),
                "chunk_index": 0,
                "chunk_type": ChunkType.TEXT.value,
                "content": "orphan chunk",
            },
        )

        with pytest.raises(RuntimeError, match="parent crawled_documents rows are missing"):
            await bootstrap_content_schema(client)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_content_schema_migration_rejects_orphan_chunks_after_parent_check_batches() -> None:
    client = SurrealContentClient(url="memory://")
    now = datetime.now(UTC).replace(tzinfo=None)
    source_id = "source-good"
    try:
        await client.execute_query(
            "CREATE schema_version:content CONTENT $record;",
            record={
                "name": "content",
                "version": 3,
                "migrations": [
                    {"version": 1, "name": "content_schema_bootstrap"},
                    {"version": 2, "name": "content_source_url_org_scope"},
                    {"version": 3, "name": "content_document_url_source_scope"},
                ],
                "created_at": now,
                "updated_at": now,
            },
        )
        await client.execute_query(
            "CREATE crawl_sources CONTENT $record;",
            record={
                "uuid": source_id,
                "organization_id": str(uuid4()),
                "name": "Docs",
                "url": "https://docs.example.com",
                "source_type": SourceType.WEBSITE.value,
            },
        )
        document_ids = [f"doc-{index:03d}" for index in range(129)]
        for document_id in document_ids:
            await client.execute_query(
                "CREATE crawled_documents CONTENT $record;",
                record={
                    "uuid": document_id,
                    "source_id": source_id,
                    "url": f"https://docs.example.com/{document_id}",
                    "title": document_id,
                    "raw_content": "",
                    "content": "",
                    "content_hash": "",
                },
            )
            await client.execute_query(
                "CREATE document_chunks CONTENT $record;",
                record={
                    "uuid": f"chunk-{document_id}",
                    "document_id": document_id,
                    "chunk_index": 0,
                    "chunk_type": ChunkType.TEXT.value,
                    "content": "body",
                },
            )
        await client.execute_query(
            "CREATE document_chunks CONTENT $record;",
            record={
                "uuid": "chunk-orphan",
                "document_id": "doc-z-orphan",
                "chunk_index": 0,
                "chunk_type": ChunkType.TEXT.value,
                "content": "orphan chunk",
            },
        )

        with pytest.raises(RuntimeError, match="parent crawled_documents rows are missing"):
            await bootstrap_content_schema(client)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_content_schema_migration_defines_child_scope_fields_without_backfill() -> None:
    client = SurrealContentClient(url="memory://")
    org_id = str(uuid4())
    source_id = str(uuid4())
    document_id = str(uuid4())
    chunk_id = str(uuid4())
    now = datetime.now(UTC).replace(tzinfo=None)
    try:
        await client.execute_query(
            "CREATE schema_version:content CONTENT $record;",
            record={
                "name": "content",
                "version": 3,
                "migrations": [
                    {"version": 1, "name": "content_schema_bootstrap"},
                    {"version": 2, "name": "content_source_url_org_scope"},
                    {"version": 3, "name": "content_document_url_source_scope"},
                ],
                "created_at": now,
                "updated_at": now,
            },
        )
        await client.execute_query(
            "CREATE crawl_sources CONTENT $record;",
            record={
                "uuid": source_id,
                "organization_id": org_id,
                "name": "Docs",
                "url": "https://docs.example.com",
                "source_type": SourceType.WEBSITE.value,
            },
        )
        await client.execute_query(
            "CREATE crawled_documents CONTENT $record;",
            record={
                "uuid": document_id,
                "source_id": source_id,
                "url": "https://docs.example.com/page",
                "title": "Page",
                "raw_content": "",
                "content": "",
                "content_hash": "",
            },
        )
        await client.execute_query(
            "CREATE document_chunks CONTENT $record;",
            record={
                "uuid": chunk_id,
                "document_id": document_id,
                "chunk_index": 0,
                "chunk_type": ChunkType.TEXT.value,
                "content": "body",
            },
        )

        await bootstrap_content_schema(client)

        document_rows = _normalize_records(
            await client.execute_query(
                "SELECT * FROM crawled_documents WHERE uuid = $uuid LIMIT 1;",
                uuid=document_id,
            )
        )
        chunk_rows = _normalize_records(
            await client.execute_query(
                "SELECT * FROM document_chunks WHERE uuid = $uuid LIMIT 1;",
                uuid=chunk_id,
            )
        )
        assert document_rows[0].get("organization_id") in {None, ""}
        assert chunk_rows[0].get("organization_id") in {None, ""}
        assert chunk_rows[0].get("source_id") in {None, ""}
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_content_schema_migration_rejects_invalid_enum_values() -> None:
    client = SurrealContentClient(url="memory://")
    now = datetime.now(UTC).replace(tzinfo=None)
    try:
        await client.execute_query(
            "CREATE schema_version:content CONTENT $record;",
            record={
                "name": "content",
                "version": 4,
                "migrations": [
                    {"version": 1, "name": "content_schema_bootstrap"},
                    {"version": 2, "name": "content_source_url_org_scope"},
                    {"version": 3, "name": "content_document_url_source_scope"},
                    {"version": 4, "name": "content_child_scope_fields"},
                ],
                "created_at": now,
                "updated_at": now,
            },
        )
        await client.execute_query(
            "CREATE crawl_sources CONTENT $record;",
            record={
                "uuid": str(uuid4()),
                "organization_id": str(uuid4()),
                "name": "Bad Docs",
                "url": "https://bad-docs.example.com",
                "source_type": "rss",
                "crawl_status": "pending",
            },
        )

        with pytest.raises(RuntimeError, match=r"crawl_sources\.source_type enum assertion"):
            await bootstrap_content_schema(client)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_content_schema_assertions_reject_invalid_writes(
    surreal_content_client: SurrealContentClient,
) -> None:
    with pytest.raises(Exception, match=r"source_type|rss"):
        await surreal_content_client.execute_query(
            "CREATE crawl_sources CONTENT $record;",
            record={
                "uuid": str(uuid4()),
                "organization_id": str(uuid4()),
                "name": "Bad Docs",
                "url": "https://bad-docs.example.com",
                "source_type": "rss",
                "crawl_status": "pending",
            },
        )


@pytest.mark.asyncio
async def test_content_archive_restore_preserves_embeddings_and_metadata(
    surreal_content_client: SurrealContentClient,
) -> None:
    source_id = uuid4()
    document_id = uuid4()
    chunk_id = uuid4()
    capture_id = uuid4()
    source_import_id = uuid4()
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
                    "source_id": "source:docs:1",
                    "principal_id": "user-123",
                    "memory_scope": "private",
                    "scope_key": "user-123",
                    "title": "Capture",
                    "raw_content": "captured",
                    "entity_type": "note",
                    "tags": ["capture"],
                    "metadata": {"source": "manual"},
                    "provenance": {"source_import_id": str(source_import_id)},
                    "capture_surface": "source_import",
                    "created_at": "2026-04-20T00:00:00+00:00",
                }
            ],
            "source_imports": [
                {
                    "id": str(source_import_id),
                    "organization_id": "org-123",
                    "principal_id": "user-123",
                    "adapter_name": "mailbox",
                    "adapter_version": "1.0",
                    "source_uri": "mbox://docs",
                    "source_identity": "docs-mailbox",
                    "source_version": "2026-04-20",
                    "privacy_class": "personal",
                    "target_memory_scope": "private",
                    "target_scope_key": "user-123",
                    "status": "completed",
                    "checkpoint": {"cursor": None, "done": True},
                    "options": {"label": "docs"},
                    "policy_context": {"memory_scope": "private", "scope_key": "user-123"},
                    "counters": {"imported_count": 1},
                    "raw_memory_ids": [str(capture_id)],
                    "source_ids": ["source:docs:1"],
                    "dedupe_keys": ["mailbox:docs:1"],
                    "duplicate_dedupe_keys": [],
                    "skipped_records": [],
                    "errors": [],
                    "raw_memory_by_source_id": {"source:docs:1": str(capture_id)},
                    "batch_size": 100,
                    "promotion_preview_approved": False,
                    "created_at": "2026-04-20T00:00:00+00:00",
                    "updated_at": "2026-04-20T00:00:00+00:00",
                    "completed_at": "2026-04-20T00:00:00+00:00",
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
            "source_imports": 1,
            "system_settings": 1,
            "backup_settings": 1,
            "backups": 1,
        },
        "total_rows": 8,
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
    assert result.tables_restored == 8
    assert result.rows_restored == 8

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
    source_import_rows = _normalize_records(
        await surreal_content_client.execute_query(
            "SELECT * FROM source_imports WHERE uuid = $uuid LIMIT 1;",
            uuid=str(source_import_id),
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
    assert capture_rows[0]["source_id"] == "source:docs:1"
    assert capture_rows[0]["memory_scope"] == "private"
    assert capture_rows[0]["scope_key"] == "user-123"
    assert capture_rows[0]["metadata"] == {"source": "manual"}
    assert capture_rows[0]["provenance"] == {"source_import_id": str(source_import_id)}
    assert source_import_rows[0]["target_memory_scope"] == "private"
    assert source_import_rows[0]["target_scope_key"] == "user-123"
    assert source_import_rows[0]["source_ids"] == ["source:docs:1"]
    assert source_import_rows[0]["raw_memory_by_source_id"] == {"source:docs:1": str(capture_id)}
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
            document=CrawledDocumentRecord(
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
                "organization_id": str(org_id),
                "source_id": str(source.id),
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
            capture=RawCaptureRecord(
                organization_id=org_id,
                entity_id="episode_123",
                title="Quick note",
                raw_content="captured",
                entity_type="episode",
                tags=["alpha"],
                metadata={"review_state": "pending"},
            ),
        )
        updated_capture = await update_raw_capture_review_state(
            None,
            organization_id=org_id,
            capture_id=capture.id,
            review_state="promoted",
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
    assert updated_capture is not None
    assert updated_capture.metadata["promoted_at"]
    assert capture_rows[0]["captured_at"] is not None
    assert capture_rows[0]["review_state"] == "promoted"
    assert capture_rows[0]["metadata"]["review_state"] == "promoted"
    assert capture_rows[0]["metadata"]["promoted_at"]
    assert status.total_chunks == 1
    assert status.chunks_with_entities == 0
    assert [(item.source_id, item.pending) for item in status.sources] == [(str(source.id), 1)]
    assert deleted_document is not None
    assert deleted_document[1] == 1
    assert deleted_source is not None
    assert source_rows == []
    assert document_rows == []


@pytest.mark.asyncio
async def test_raw_capture_save_rejects_cross_org_uuid_overwrite(
    surreal_content_client: SurrealContentClient,
) -> None:
    capture_id = uuid4()
    first_org_id = uuid4()
    second_org_id = uuid4()

    with (
        patch.object(surreal_content_client, "close", AsyncMock()),
        patch(
            "sibyl.persistence.surreal.content.build_surreal_content_client",
            return_value=surreal_content_client,
        ),
    ):
        await save_raw_capture_record(
            None,
            capture=RawCaptureRecord(
                id=capture_id,
                organization_id=first_org_id,
                title="Original",
                raw_content="keep me",
                entity_type="episode",
            ),
        )

        with pytest.raises(RuntimeError):
            await save_raw_capture_record(
                None,
                capture=RawCaptureRecord(
                    id=capture_id,
                    organization_id=second_org_id,
                    title="Overwrite",
                    raw_content="wrong org",
                    entity_type="episode",
                ),
            )

    rows = _normalize_records(
        await surreal_content_client.execute_query(
            "SELECT * FROM raw_captures WHERE uuid = $uuid LIMIT 1;",
            uuid=str(capture_id),
        )
    )
    assert rows[0]["organization_id"] == str(first_org_id)
    assert rows[0]["raw_content"] == "keep me"


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
            setting=SystemSettingRecord(
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
    await surreal_content_client.execute_query(
        "CREATE source_imports CONTENT $record;",
        record={
            "uuid": str(uuid4()),
            "organization_id": org_id,
            "principal_id": "user-export",
            "adapter_name": "mailbox",
            "status": "completed",
            "target_memory_scope": "private",
            "target_scope_key": "user-export",
            "source_ids": ["source:export:1"],
            "raw_memory_by_source_id": {"source:export:1": "raw-export"},
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
    assert payload["row_counts"]["source_imports"] == 1
    assert payload["row_counts"]["backup_settings"] == 1
    assert payload["row_counts"]["backups"] == 1
    assert payload["total_rows"] == 4
    assert payload["tables"]["system_settings"][0]["key"] == "exported_setting"
    assert payload["tables"]["source_imports"][0]["target_memory_scope"] == "private"
    assert payload["tables"]["source_imports"][0]["source_ids"] == ["source:export:1"]
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

    assert isinstance(settings, BackupSettingsRecord)
    assert updated_settings.schedule == "0 3 * * *"
    assert updated_settings.retention_days == 14
    assert updated_settings.include_database_dump is False
    assert [item.organization_id for item in enabled] == [org_id]
    assert isinstance(created, BackupRecord)
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
