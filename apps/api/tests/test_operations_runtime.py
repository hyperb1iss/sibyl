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

    monkeypatch.setattr(operations_runtime.settings, "auth_store", "surreal")
    monkeypatch.setattr(surreal_setup, "get_setup_status", surreal_status)
    monkeypatch.setattr(legacy_setup, "get_setup_status", legacy_status)

    assert await operations_runtime.get_setup_status() == expected
    surreal_status.assert_awaited_once_with()
    legacy_status.assert_not_called()


@pytest.mark.asyncio
async def test_operations_runtime_dispatches_settings_admin_to_postgres(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request()
    legacy_admin = AsyncMock()
    surreal_admin = AsyncMock(side_effect=AssertionError("surreal settings admin should not run"))

    monkeypatch.setattr(operations_runtime.settings, "auth_store", "postgres")
    monkeypatch.setattr(legacy_setup, "require_settings_admin", legacy_admin)
    monkeypatch.setattr(surreal_setup, "require_settings_admin", surreal_admin)

    await operations_runtime.require_settings_admin(request)

    legacy_admin.assert_awaited_once_with(request)
    surreal_admin.assert_not_called()


def test_operations_runtime_keeps_legacy_aliases_pointed_at_active_surface() -> None:
    assert operations_runtime.get_legacy_setup_status is operations_runtime.get_setup_status
    assert operations_runtime.require_legacy_settings_admin is operations_runtime.require_settings_admin
    assert operations_runtime.attach_legacy_backup_job is operations_runtime.attach_backup_job
    assert operations_runtime.request_legacy_password_reset is operations_runtime.request_password_reset
