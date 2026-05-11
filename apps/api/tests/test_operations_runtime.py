from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from starlette.requests import Request

from sibyl.persistence import operations_runtime
from sibyl.persistence.legacy import setup as legacy_setup
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
    legacy_status = AsyncMock(side_effect=AssertionError("legacy setup status should not run"))

    monkeypatch.setattr(surreal_setup, "get_setup_status", surreal_status)
    monkeypatch.setattr(legacy_setup, "get_setup_status", legacy_status)

    assert await operations_runtime.get_setup_status() == expected
    surreal_status.assert_awaited_once_with()
    legacy_status.assert_not_called()


@pytest.mark.asyncio
async def test_operations_runtime_dispatches_settings_admin_to_surreal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request()
    legacy_admin = AsyncMock(side_effect=AssertionError("legacy settings admin should not run"))
    surreal_admin = AsyncMock()

    monkeypatch.setattr(legacy_setup, "require_settings_admin", legacy_admin)
    monkeypatch.setattr(surreal_setup, "require_settings_admin", surreal_admin)

    await operations_runtime.require_settings_admin(request)

    surreal_admin.assert_awaited_once_with(request)
    legacy_admin.assert_not_called()


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
        "require_setup_mode_or_admin",
        "require_setup_mode_or_auth",
        "list_backups",
        "list_oauth_connections",
        "remove_oauth_connection",
        "request_password_reset",
        "update_backup_settings",
    ]
