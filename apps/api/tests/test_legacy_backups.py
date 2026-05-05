from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from sibyl.persistence.legacy import backups as legacy_backups


@pytest.mark.asyncio
async def test_get_legacy_backup_settings_creates_default_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = MagicMock()
    result.scalar_one_or_none.return_value = None

    session = AsyncMock()
    session.execute.return_value = result
    session_manager = AsyncMock()
    session_manager.__aenter__.return_value = session
    session_manager.__aexit__.return_value = False

    monkeypatch.setattr(legacy_backups, "get_session", lambda: session_manager)

    settings = await legacy_backups.get_legacy_backup_settings(uuid4())

    assert settings.organization_id is not None
    session.add.assert_called_once()
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_legacy_backup_retention_prefers_request_value() -> None:
    retention = await legacy_backups.get_legacy_backup_retention(uuid4(), 14)

    assert retention == 14


@pytest.mark.asyncio
async def test_get_legacy_backup_raises_for_missing_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = MagicMock()
    result.scalar_one_or_none.return_value = None

    session = AsyncMock()
    session.execute.return_value = result
    session_manager = AsyncMock()
    session_manager.__aenter__.return_value = session
    session_manager.__aexit__.return_value = False

    monkeypatch.setattr(legacy_backups, "get_session", lambda: session_manager)

    with pytest.raises(HTTPException, match="Backup not found") as exc_info:
        await legacy_backups.get_legacy_backup(uuid4(), "backup_test")

    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_delete_legacy_backup_record_deletes_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backup = SimpleNamespace(id=uuid4(), backup_id="backup_test")
    result = MagicMock()
    result.scalar_one_or_none.return_value = backup

    session = AsyncMock()
    session.execute.return_value = result
    session_manager = AsyncMock()
    session_manager.__aenter__.return_value = session
    session_manager.__aexit__.return_value = False

    monkeypatch.setattr(legacy_backups, "get_session", lambda: session_manager)

    removed = await legacy_backups.delete_legacy_backup_record(uuid4(), "backup_test")

    assert removed.id == backup.id
    assert removed.backup_id == "backup_test"
    session.delete.assert_awaited_once_with(backup)
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_update_legacy_backup_settings_forwards_database_dump_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    updated = SimpleNamespace(include_database_dump=False)
    update_backup_settings = AsyncMock(return_value=updated)
    monkeypatch.setattr(legacy_backups, "update_backup_settings", update_backup_settings)

    result = await legacy_backups.update_legacy_backup_settings(
        uuid4(),
        include_database_dump=False,
    )

    assert result is updated
    update_backup_settings.assert_awaited_once()
    assert update_backup_settings.await_args.kwargs["include_database_dump"] is False


@pytest.mark.asyncio
async def test_create_legacy_backup_record_defaults_database_dump_to_true(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created = SimpleNamespace(backup_id="backup_test")
    create_backup_record = AsyncMock(return_value=created)
    monkeypatch.setattr(legacy_backups, "create_backup_record", create_backup_record)

    result = await legacy_backups.create_legacy_backup_record(
        org_id=uuid4(),
        backup_id="backup_test",
        include_graph=True,
        created_by_user_id=None,
    )

    assert result is created
    create_backup_record.assert_awaited_once()
    assert create_backup_record.await_args.kwargs["include_database_dump"] is True
