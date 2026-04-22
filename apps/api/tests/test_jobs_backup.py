from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from sibyl.jobs import backup as backup_jobs


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
        include_postgres=True,
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
async def test_run_scheduled_backups_removes_orphan_record_when_queue_fails() -> None:
    org_id = uuid4()
    settings = SimpleNamespace(
        organization_id=org_id,
        include_postgres=True,
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
