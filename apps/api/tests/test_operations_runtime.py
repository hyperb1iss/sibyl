from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from starlette.requests import Request

from sibyl.persistence import operations_runtime
from sibyl.persistence.setup_common import SetupStatus
from sibyl.persistence.surreal import setup as surreal_setup


def _request() -> Request:
    return Request({"type": "http", "method": "GET", "path": "/settings", "headers": []})


@pytest.mark.asyncio
async def test_operations_runtime_dispatches_setup_status_to_surreal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = SetupStatus(has_users=True, has_orgs=True)
    surreal_status = AsyncMock(return_value=expected)

    monkeypatch.setattr(surreal_setup, "get_setup_status", surreal_status)

    assert await operations_runtime.get_setup_status() == expected
    surreal_status.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_operations_runtime_dispatches_settings_admin_to_surreal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request()
    surreal_admin = AsyncMock()

    monkeypatch.setattr(surreal_setup, "require_settings_admin", surreal_admin)

    await operations_runtime.require_settings_admin(request)

    surreal_admin.assert_awaited_once_with(request)


@pytest.mark.asyncio
async def test_operations_runtime_dispatches_settings_owner_to_surreal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request()
    surreal_owner = AsyncMock()

    monkeypatch.setattr(surreal_setup, "require_settings_owner", surreal_owner)

    await operations_runtime.require_settings_owner(request)

    surreal_owner.assert_awaited_once_with(request)


def test_operations_runtime_exports_neutral_runtime_surface() -> None:
    assert operations_runtime.__all__ == [
        "attach_backup_job",
        "confirm_password_reset",
        "create_backup_record",
        "delete_backup_record",
        "get_backup",
        "get_backup_retention",
        "get_backup_settings",
        "get_setup_status",
        "is_setup_mode",
        "require_settings_admin",
        "require_settings_owner",
        "require_setup_mode_or_admin",
        "require_setup_mode_or_auth",
        "list_backups",
        "list_oauth_connections",
        "remove_oauth_connection",
        "request_password_reset",
        "update_backup_settings",
    ]
