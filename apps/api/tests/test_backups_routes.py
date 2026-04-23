from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from sibyl.api.routes import backups as backup_routes
from sibyl.persistence.legacy.backups import LegacyBackupList


def _org() -> SimpleNamespace:
    return SimpleNamespace(id=uuid4())


def _user() -> SimpleNamespace:
    return SimpleNamespace(id=uuid4())


def test_backup_settings_update_accepts_legacy_database_dump_alias() -> None:
    request = backup_routes.BackupSettingsUpdate(include_postgres=True)

    assert request.include_database_dump is True


def test_create_backup_request_accepts_legacy_database_dump_alias() -> None:
    request = backup_routes.CreateBackupRequest(include_postgres=True)

    assert request.include_database_dump is True


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
    monkeypatch.setattr(backup_routes.settings, "auth_store", "postgres")

    response = await backup_routes.get_backup_settings(org=_org())

    assert response.enabled is True
    assert response.retention_days == 30
    assert response.database_dump_supported is True
    assert response.include_database_dump is True
    assert response.archive_contents == ["postgres.sql", "metadata.json"]


@pytest.mark.asyncio
async def test_get_backup_settings_accepts_legacy_settings_alias(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = SimpleNamespace(
        enabled=True,
        schedule="0 2 * * *",
        retention_days=30,
        include_postgres=True,
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
    monkeypatch.setattr(backup_routes.settings, "auth_store", "postgres")

    response = await backup_routes.get_backup_settings(org=_org())

    assert response.include_database_dump is True


@pytest.mark.asyncio
async def test_create_backup_uses_runtime_record_helpers(monkeypatch: pytest.MonkeyPatch) -> None:
    org = _org()
    user = _user()
    backup = SimpleNamespace(id=uuid4(), status="pending")
    queued = SimpleNamespace(id=backup.id, status="pending")

    monkeypatch.setattr(backup_routes.settings, "store", "legacy")
    monkeypatch.setattr(backup_routes.settings, "auth_store", "postgres")
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
    assert response.archive_contents == ["postgres.sql", "metadata.json"]


@pytest.mark.asyncio
async def test_create_backup_disables_postgres_in_fully_surreal_mode(
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
        request=backup_routes.CreateBackupRequest(include_postgres=True, include_graph=False),
        org=org,
        user=user,
    )

    backup_routes.create_backup_record.assert_awaited_once_with(
        org_id=org.id,
        backup_id="backup_fixed",
        include_database_dump=False,
        include_graph=False,
        created_by_user_id=user.id,
    )
    from sibyl.jobs import queue as jobs_queue

    jobs_queue.enqueue_backup.assert_awaited_once_with(
        str(org.id),
        include_database_dump=False,
        include_graph=False,
        backup_id="backup_fixed",
    )
    assert response.archive_contents == ["auth.json", "content.json", "metadata.json"]


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
        AsyncMock(return_value=LegacyBackupList(backups=[backup], total=1)),
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
    monkeypatch.setattr(
        "sibyl.jobs.queue.enqueue_backup_cleanup",
        AsyncMock(return_value="cleanup-job"),
    )

    response = await backup_routes.run_cleanup(
        request=backup_routes.CleanupRequest(retention_days=None),
        org=_org(),
    )

    assert response.job_id == "cleanup-job"


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
    monkeypatch.setattr("sibyl.jobs.backup.delete_backup", lambda _: None)

    response = await backup_routes.delete_backup("backup_a", org=_org())

    assert response == {"deleted": True, "backup_id": "backup_a"}
