from __future__ import annotations

import json
import tarfile
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from sibyl.jobs import backup as backup_jobs


@dataclass
class _GraphBackupPayload:
    entities: list[dict[str, str]]
    relationships: list[dict[str, str]]


@pytest.mark.asyncio
async def test_update_backup_db_uses_runtime_helper() -> None:
    backup = SimpleNamespace(status="completed")

    with patch(
        "sibyl.jobs.backup.update_backup_record",
        AsyncMock(return_value=backup),
    ) as update_record:
        await backup_jobs._update_backup_db(
            "backup_123",
            status="completed",
            filename="sibyl_backup_123.tar.gz",
            size_bytes=128,
        )

    update_record.assert_awaited_once_with(
        "backup_123",
        status="completed",
        filename="sibyl_backup_123.tar.gz",
        file_path=None,
        size_bytes=128,
        entity_count=None,
        relationship_count=None,
        started_at=None,
        completed_at=None,
        duration_seconds=None,
        error=None,
    )


@pytest.mark.asyncio
async def test_run_scheduled_backups_uses_runtime_helpers() -> None:
    org_id = uuid4()
    settings = SimpleNamespace(
        organization_id=org_id,
        include_database_dump=True,
        include_graph=False,
    )
    backup = SimpleNamespace(id=uuid4())

    with (
        patch(
            "sibyl.jobs.backup.list_enabled_backup_settings",
            AsyncMock(return_value=[settings]),
        ) as list_settings,
        patch(
            "sibyl.jobs.backup.create_backup_record",
            AsyncMock(return_value=backup),
        ) as create_record,
        patch(
            "sibyl.jobs.backup.attach_backup_job",
            AsyncMock(),
        ) as attach_job,
        patch(
            "sibyl.jobs.queue.enqueue_backup",
            AsyncMock(return_value="job-123"),
        ) as enqueue_backup,
    ):
        result = await backup_jobs.run_scheduled_backups({})

    assert result == {
        "success": True,
        "orgs_queued": 1,
        "orgs_skipped": 0,
        "errors": [],
    }
    list_settings.assert_awaited_once_with()
    create_record.assert_awaited_once()
    enqueue_backup.assert_awaited_once()
    attach_job.assert_awaited_once_with(backup.id, "job-123")


@pytest.mark.asyncio
async def test_run_scheduled_backups_disables_postgres_in_fully_surreal_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    org_id = uuid4()
    settings = SimpleNamespace(
        organization_id=org_id,
        include_database_dump=True,
        include_graph=False,
    )
    backup = SimpleNamespace(id=uuid4())

    monkeypatch.setattr(backup_jobs.settings, "store", "surreal")
    monkeypatch.setattr(backup_jobs.settings, "auth_store", "surreal")

    with (
        patch(
            "sibyl.jobs.backup.list_enabled_backup_settings",
            AsyncMock(return_value=[settings]),
        ),
        patch(
            "sibyl.jobs.backup.create_backup_record",
            AsyncMock(return_value=backup),
        ) as create_record,
        patch(
            "sibyl.jobs.backup.attach_backup_job",
            AsyncMock(),
        ),
        patch(
            "sibyl.jobs.queue.enqueue_backup",
            AsyncMock(return_value="job-123"),
        ) as enqueue_backup,
    ):
        await backup_jobs.run_scheduled_backups({})

    create_record.assert_awaited_once_with(
        org_id=org_id,
        backup_id=create_record.await_args.kwargs["backup_id"],
        include_database_dump=False,
        include_graph=False,
        created_by_user_id=None,
        triggered_by="scheduled",
    )
    enqueue_backup.assert_awaited_once_with(
        str(org_id),
        include_database_dump=False,
        include_graph=False,
        backup_id=enqueue_backup.await_args.kwargs["backup_id"],
    )


@pytest.mark.asyncio
async def test_run_scheduled_backups_removes_orphan_record_when_queue_fails() -> None:
    org_id = uuid4()
    settings = SimpleNamespace(
        organization_id=org_id,
        include_database_dump=True,
        include_graph=True,
    )
    backup = SimpleNamespace(id=uuid4())

    with (
        patch(
            "sibyl.jobs.backup.list_enabled_backup_settings",
            AsyncMock(return_value=[settings]),
        ),
        patch(
            "sibyl.jobs.backup.create_backup_record",
            AsyncMock(return_value=backup),
        ),
        patch(
            "sibyl.jobs.queue.enqueue_backup",
            AsyncMock(side_effect=RuntimeError("queue down")),
        ),
        patch(
            "sibyl.jobs.backup.delete_backup_record",
            AsyncMock(),
        ) as delete_record,
    ):
        result = await backup_jobs.run_scheduled_backups({})

    assert result["success"] is True
    assert result["orgs_queued"] == 0
    assert result["orgs_skipped"] == 1
    assert result["errors"] == [{"organization_id": str(org_id), "error": "queue down"}]
    delete_record.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_backup_in_fully_surreal_mode_includes_runtime_snapshots(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    org_id = str(uuid4())
    graph_result = SimpleNamespace(
        success=True,
        backup_data=_GraphBackupPayload(
            entities=[{"uuid": "entity-1"}],
            relationships=[{"uuid": "rel-1"}],
        ),
        entity_count=1,
        relationship_count=1,
        message="ok",
    )

    monkeypatch.setattr(backup_jobs.settings, "store", "surreal")
    monkeypatch.setattr(backup_jobs.settings, "auth_store", "surreal")
    monkeypatch.setattr(backup_jobs.settings, "backup_dir", tmp_path)

    with (
        patch(
            "sibyl.jobs.backup.export_auth_archive_payload",
            AsyncMock(return_value={"version": "1.0", "tables": {"users": []}}),
        ) as export_auth,
        patch(
            "sibyl.jobs.backup.export_content_archive_payload",
            AsyncMock(return_value={"version": "1.0", "tables": {"crawl_sources": []}}),
        ) as export_content,
        patch(
            "sibyl.jobs.backup._update_backup_db",
            AsyncMock(),
        ) as update_backup_db,
        patch(
            "sibyl.jobs.backup._safe_broadcast",
            AsyncMock(),
        ),
        patch(
            "sibyl_core.tools.admin.create_backup",
            AsyncMock(return_value=graph_result),
        ) as create_graph_backup,
    ):
        result = await backup_jobs.run_backup(
            {},
            org_id,
            include_database_dump=True,
            include_graph=True,
            backup_id="backup_fixed",
        )

    assert result["success"] is True
    assert result["backup_id"] == "backup_fixed"
    assert result["database_dump_size_bytes"] == 0
    assert "pg_size_bytes" not in result
    export_auth.assert_awaited_once_with()
    export_content.assert_awaited_once_with()
    create_graph_backup.assert_awaited_once_with(organization_id=org_id)
    assert update_backup_db.await_count == 2

    archive_path = tmp_path / "sibyl_backup_fixed.tar.gz"
    assert archive_path.exists()

    with tarfile.open(archive_path, "r:gz") as archive:
        names = set(archive.getnames())
        metadata = json.load(archive.extractfile("metadata.json"))

    assert {"metadata.json", "auth.json", "content.json", "graph.json"} <= names
    assert "postgres.sql" not in names
    assert metadata["database_dump_tables"] == 0
    assert "pg_entities" not in metadata
