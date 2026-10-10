from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from sibyl.api.routes import backups as backup_routes
from sibyl.coordination.broker import JobInfo, JobStatus
from sibyl.persistence.backups_common import BackupListResult
from sibyl_core.backends.archive_store import ArchiveStoreUnavailable


def _org() -> SimpleNamespace:
    return SimpleNamespace(id=uuid4())


def _user() -> SimpleNamespace:
    return SimpleNamespace(id=uuid4())


def test_backup_settings_update_accepts_deprecated_include_fields() -> None:
    request = backup_routes.BackupSettingsUpdate(
        include_database_dump=True,
        include_graph=False,
    )

    assert request.include_database_dump is True
    assert request.include_graph is False


def test_create_backup_request_accepts_deprecated_include_fields() -> None:
    request = backup_routes.CreateBackupRequest(
        include_database_dump=True,
        include_graph=False,
    )

    assert request.include_database_dump is True
    assert request.include_graph is False


@pytest.mark.asyncio
async def test_get_backup_settings_uses_runtime_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = SimpleNamespace(
        enabled=True,
        schedule="0 2 * * *",
        retention_days=30,
        include_database_dump=True,
        include_graph=False,
        last_backup_at=None,
        last_backup_id=None,
    )
    monkeypatch.setattr(
        backup_routes,
        "load_backup_settings",
        AsyncMock(return_value=settings),
    )
    monkeypatch.setattr(backup_routes.settings, "store", "legacy")
    monkeypatch.setattr(backup_routes.settings, "auth_store", "surreal")

    response = await backup_routes.get_backup_settings(org=_org())

    assert response.enabled is True
    assert response.retention_days == 30
    assert response.database_dump_supported is False
    assert response.include_database_dump is False
    assert response.include_graph is True
    assert response.archive_contents == ["auth.json", "graph.json", "metadata.json"]


@pytest.mark.asyncio
async def test_get_backup_settings_reads_database_dump_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = SimpleNamespace(
        enabled=True,
        schedule="0 2 * * *",
        retention_days=30,
        include_database_dump=False,
        include_graph=False,
        last_backup_at=None,
        last_backup_id=None,
    )
    monkeypatch.setattr(
        backup_routes,
        "load_backup_settings",
        AsyncMock(return_value=settings),
    )
    monkeypatch.setattr(backup_routes.settings, "store", "legacy")
    monkeypatch.setattr(backup_routes.settings, "auth_store", "surreal")

    response = await backup_routes.get_backup_settings(org=_org())

    assert response.include_database_dump is False
    assert response.include_graph is True
    assert response.archive_contents == ["auth.json", "graph.json", "metadata.json"]


@pytest.mark.asyncio
async def test_create_backup_uses_runtime_record_helpers(monkeypatch: pytest.MonkeyPatch) -> None:
    org = _org()
    user = _user()
    backup = SimpleNamespace(id=uuid4(), status="pending")
    queued = SimpleNamespace(id=backup.id, status="pending")

    monkeypatch.setattr(backup_routes.settings, "store", "legacy")
    monkeypatch.setattr(backup_routes.settings, "auth_store", "surreal")
    monkeypatch.setattr(backup_routes, "generate_backup_id", lambda _: "backup_fixed")
    monkeypatch.setattr(
        backup_routes,
        "create_backup_record",
        AsyncMock(return_value=backup),
    )
    monkeypatch.setattr(
        backup_routes,
        "attach_backup_job_record",
        AsyncMock(return_value=queued),
    )
    monkeypatch.setattr(
        "sibyl.jobs.queue.enqueue_backup",
        AsyncMock(return_value="job-123"),
    )

    response = await backup_routes.create_backup(
        request=backup_routes.CreateBackupRequest(
            include_database_dump=True,
            include_graph=False,
        ),
        org=org,
        user=user,
    )

    assert response.backup_id == "backup_fixed"
    assert response.job_id == "job-123"
    assert response.archive_contents == ["auth.json", "graph.json", "metadata.json"]
    backup_routes.create_backup_record.assert_awaited_once_with(
        org_id=org.id,
        backup_id="backup_fixed",
        include_database_dump=False,
        include_graph=True,
        created_by_user_id=user.id,
    )
    from sibyl.jobs import queue as jobs_queue

    jobs_queue.enqueue_backup.assert_awaited_once_with(
        str(org.id),
        include_database_dump=False,
        include_graph=True,
        backup_id="backup_fixed",
    )


@pytest.mark.asyncio
async def test_create_backup_disables_database_dump_in_fully_surreal_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    org = _org()
    user = _user()
    backup = SimpleNamespace(id=uuid4(), status="pending")
    queued = SimpleNamespace(id=backup.id, status="pending")

    monkeypatch.setattr(backup_routes.settings, "store", "surreal")
    monkeypatch.setattr(backup_routes.settings, "auth_store", "surreal")
    monkeypatch.setattr(backup_routes, "generate_backup_id", lambda _: "backup_fixed")
    monkeypatch.setattr(
        backup_routes,
        "create_backup_record",
        AsyncMock(return_value=backup),
    )
    monkeypatch.setattr(
        backup_routes,
        "attach_backup_job_record",
        AsyncMock(return_value=queued),
    )
    monkeypatch.setattr(
        "sibyl.jobs.queue.enqueue_backup",
        AsyncMock(return_value="job-123"),
    )

    response = await backup_routes.create_backup(
        request=backup_routes.CreateBackupRequest(
            include_database_dump=True,
            include_graph=False,
        ),
        org=org,
        user=user,
    )

    backup_routes.create_backup_record.assert_awaited_once_with(
        org_id=org.id,
        backup_id="backup_fixed",
        include_database_dump=False,
        include_graph=True,
        created_by_user_id=user.id,
    )
    from sibyl.jobs import queue as jobs_queue

    jobs_queue.enqueue_backup.assert_awaited_once_with(
        str(org.id),
        include_database_dump=False,
        include_graph=True,
        backup_id="backup_fixed",
    )
    assert response.archive_contents == ["auth.json", "content.json", "graph.json", "metadata.json"]


@pytest.mark.asyncio
async def test_list_backups_uses_runtime_helper(monkeypatch: pytest.MonkeyPatch) -> None:
    backup = SimpleNamespace(
        id=uuid4(),
        backup_id="backup_a",
        status="completed",
        filename="a.tar.gz",
        size_bytes=128,
        entity_count=3,
        relationship_count=5,
        duration_seconds=1.2,
        triggered_by="manual",
        created_at=datetime.now(UTC).replace(tzinfo=None),
        started_at=None,
        completed_at=None,
        error=None,
    )
    monkeypatch.setattr(
        backup_routes,
        "list_backup_records",
        AsyncMock(return_value=BackupListResult(backups=[backup], total=1)),
    )

    response = await backup_routes.list_backups(org=_org(), limit=10, offset=0)

    assert response.total == 1
    assert response.backups[0].backup_id == "backup_a"


@pytest.mark.asyncio
async def test_run_cleanup_uses_retention_helper(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        backup_routes,
        "resolve_backup_retention",
        AsyncMock(return_value=14),
    )
    enqueue = AsyncMock(return_value="cleanup-job")
    monkeypatch.setattr("sibyl.jobs.queue.enqueue_backup_cleanup", enqueue)
    org = _org()

    response = await backup_routes.run_cleanup(
        request=backup_routes.CleanupRequest(retention_days=None),
        org=org,
    )

    assert response.job_id == "cleanup-job"
    # One organization's admin cleans up only that organization's archives.
    enqueue.assert_awaited_once_with(retention_days=14, organization_id=str(org.id))


@pytest.mark.asyncio
async def test_delete_backup_uses_runtime_delete_helper(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        backup_routes,
        "get_backup_record",
        AsyncMock(return_value=SimpleNamespace(backup_id="backup_a")),
    )
    monkeypatch.setattr(
        backup_routes,
        "delete_backup_record",
        AsyncMock(return_value=SimpleNamespace(backup_id="backup_a")),
    )
    deleted: list[str] = []
    store = SimpleNamespace(delete=deleted.append)
    monkeypatch.setattr(backup_routes, "backup_archive_store", lambda: store)

    response = await backup_routes.delete_backup("backup_a", org=_org())

    assert response == {"deleted": True, "backup_id": "backup_a"}
    assert deleted == ["sibyl_backup_a.tar.gz"]


@pytest.mark.asyncio
async def test_delete_backup_keeps_the_record_when_the_store_refuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        backup_routes,
        "get_backup_record",
        AsyncMock(return_value=SimpleNamespace(backup_id="backup_a")),
    )
    delete_record = AsyncMock()
    monkeypatch.setattr(backup_routes, "delete_backup_record", delete_record)

    def refuse(_name: str) -> None:
        raise ArchiveStoreUnavailable("Backup archive bucket b refused DeleteObject: AccessDenied")

    monkeypatch.setattr(
        backup_routes, "backup_archive_store", lambda: SimpleNamespace(delete=refuse)
    )

    with pytest.raises(HTTPException) as refused:
        await backup_routes.delete_backup("backup_a", org=_org())

    assert refused.value.status_code == 503
    delete_record.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("record", "opened", "status"),
    [
        (SimpleNamespace(backup_id="backup_a", status="in_progress"), None, 400),
        (SimpleNamespace(backup_id="backup_a", status="completed"), None, 404),
        (
            SimpleNamespace(backup_id="backup_a", status="completed"),
            ArchiveStoreUnavailable("Backup archive bucket b refused GetObject: SlowDown (503)"),
            503,
        ),
    ],
)
async def test_download_answers_missing_and_unavailable_archives(
    monkeypatch: pytest.MonkeyPatch, record, opened, status
) -> None:
    monkeypatch.setattr(backup_routes, "get_backup_record", AsyncMock(return_value=record))

    def open_archive(_name: str):
        if isinstance(opened, Exception):
            raise opened
        return opened

    monkeypatch.setattr(
        backup_routes, "backup_archive_store", lambda: SimpleNamespace(open=open_archive)
    )

    with pytest.raises(HTTPException) as answered:
        await backup_routes.download_backup("backup_a", org=_org())

    assert answered.value.status_code == status


def _backup_job(organization_id: str) -> JobInfo:
    return JobInfo(
        job_id="backup:backup_secret",
        function="run_backup",
        status=JobStatus.COMPLETE,
        args=(organization_id,),
        kwargs={"backup_id": "backup_secret"},
        result={
            "success": True,
            "backup_id": "backup_secret",
            "organization_id": organization_id,
            "archive_path": "/var/lib/sibyl/backups/backup_secret.tar.gz",
        },
    )


@pytest.mark.asyncio
async def test_backup_job_status_hides_another_orgs_job(monkeypatch: pytest.MonkeyPatch) -> None:
    reader = _org()
    owner_org_id = str(uuid4())
    monkeypatch.setattr(
        "sibyl.jobs.queue.get_job_status",
        AsyncMock(return_value=_backup_job(owner_org_id)),
    )

    with pytest.raises(HTTPException) as exc_info:
        await backup_routes.get_backup_job_status("backup:backup_secret", org=reader)

    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "Job not found: backup:backup_secret"


@pytest.mark.asyncio
async def test_backup_job_status_returns_own_orgs_job(monkeypatch: pytest.MonkeyPatch) -> None:
    org = _org()
    monkeypatch.setattr(
        "sibyl.jobs.queue.get_job_status",
        AsyncMock(return_value=_backup_job(str(org.id))),
    )

    response = await backup_routes.get_backup_job_status("backup:backup_secret", org=org)

    assert response["status"] == "complete"
    assert response["result"]["organization_id"] == str(org.id)


@pytest.mark.asyncio
async def test_backup_job_status_answers_unknown_and_foreign_jobs_alike(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 200 for one and a 404 for the other would confirm a foreign job exists."""
    reader = _org()

    monkeypatch.setattr(
        "sibyl.jobs.queue.get_job_status",
        AsyncMock(return_value=_backup_job(str(uuid4()))),
    )
    with pytest.raises(HTTPException) as foreign:
        await backup_routes.get_backup_job_status("backup:backup_secret", org=reader)

    unknown_job = JobInfo(
        job_id="backup:backup_secret",
        function="unknown",
        status=JobStatus.NOT_FOUND,
    )
    monkeypatch.setattr("sibyl.jobs.queue.get_job_status", AsyncMock(return_value=unknown_job))
    with pytest.raises(HTTPException) as unknown:
        await backup_routes.get_backup_job_status("backup:backup_secret", org=reader)

    assert foreign.value.status_code == unknown.value.status_code == 404
    assert foreign.value.detail == unknown.value.detail
