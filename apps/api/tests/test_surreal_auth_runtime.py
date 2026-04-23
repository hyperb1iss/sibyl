from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from sibyl.auth.device_authorization import DeviceTokenError
from sibyl.persistence.surreal import auth_runtime as surreal_auth_runtime


class _StaticAuthClientScope:
    def __init__(self, client: object) -> None:
        self._client = client

    async def __aenter__(self) -> object:
        return self._client

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False


def test_coerce_datetime_normalizes_aware_datetime_instances() -> None:
    aware = datetime(2026, 4, 23, 16, 0, tzinfo=UTC)

    actual = surreal_auth_runtime._coerce_datetime(aware)

    assert actual == aware.replace(tzinfo=None)
    assert actual is not None
    assert actual.tzinfo is None


def test_surreal_session_repository_accepts_aware_expiry_datetimes() -> None:
    repo = surreal_auth_runtime.SurrealSessionRepository(object())
    record = {
        "expires_at": datetime.now(UTC) + timedelta(minutes=5),
        "refresh_token_expires_at": datetime.now(UTC) + timedelta(days=1),
        "revoked_at": None,
    }

    assert repo._is_session_active(record) is True
    assert repo._has_refresh_session(record) is True


@pytest.mark.asyncio
async def test_exchange_legacy_device_code_accepts_aware_datetime_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device_request_id = uuid4()
    user_id = uuid4()
    organization_id = uuid4()
    fake_client = object()
    create_session = AsyncMock()
    replace_record = AsyncMock()
    select_one = AsyncMock(
        return_value={
            "uuid": str(device_request_id),
            "device_code_hash": "hash",
            "user_code": "ABCD-EFGH",
            "client_name": "sibyl-cli",
            "scope": "mcp",
            "status": "approved",
            "poll_interval_seconds": 5,
            "last_polled_at": datetime.now(UTC) - timedelta(seconds=10),
            "expires_at": datetime.now(UTC) + timedelta(minutes=10),
            "approved_at": datetime.now(UTC) - timedelta(seconds=5),
            "denied_at": None,
            "consumed_at": None,
            "user_id": str(user_id),
            "organization_id": str(organization_id),
            "created_at": datetime.now(UTC) - timedelta(minutes=1),
            "updated_at": datetime.now(UTC) - timedelta(seconds=10),
        }
    )

    monkeypatch.setattr(
        surreal_auth_runtime,
        "_auth_client_scope",
        lambda: _StaticAuthClientScope(fake_client),
    )
    monkeypatch.setattr(surreal_auth_runtime, "create_access_token", lambda **_: "access-token")
    monkeypatch.setattr(
        surreal_auth_runtime,
        "create_refresh_token",
        lambda **_: ("refresh-token", datetime.now(UTC) + timedelta(days=30)),
    )
    monkeypatch.setattr(surreal_auth_runtime._SurrealRepository, "select_one", select_one)
    monkeypatch.setattr(surreal_auth_runtime._SurrealRepository, "replace_record", replace_record)
    monkeypatch.setattr(
        surreal_auth_runtime.SurrealSessionRepository,
        "from_client",
        lambda client: SimpleNamespace(create_session=create_session),
    )

    token = await surreal_auth_runtime.exchange_legacy_device_code(device_code="device-code")

    assert token["access_token"] == "access-token"
    assert token["refresh_token"] == "refresh-token"
    create_session.assert_awaited_once()
    replace_record.assert_awaited_once()


@pytest.mark.asyncio
async def test_exchange_legacy_device_code_handles_missing_last_polled_at(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device_request_id = uuid4()
    fake_client = object()
    replace_record = AsyncMock()
    select_one = AsyncMock(
        return_value={
            "uuid": str(device_request_id),
            "device_code_hash": "hash",
            "user_code": "ABCD-EFGH",
            "client_name": "sibyl-cli",
            "scope": "mcp",
            "status": "pending",
            "poll_interval_seconds": 5,
            "expires_at": datetime.now(UTC) + timedelta(minutes=10),
            "created_at": datetime.now(UTC) - timedelta(minutes=1),
            "updated_at": datetime.now(UTC) - timedelta(seconds=10),
        }
    )

    monkeypatch.setattr(
        surreal_auth_runtime,
        "_auth_client_scope",
        lambda: _StaticAuthClientScope(fake_client),
    )
    monkeypatch.setattr(surreal_auth_runtime._SurrealRepository, "select_one", select_one)
    monkeypatch.setattr(surreal_auth_runtime._SurrealRepository, "replace_record", replace_record)

    with pytest.raises(DeviceTokenError, match="Authorization pending") as exc_info:
        await surreal_auth_runtime.exchange_legacy_device_code(device_code="device-code")

    assert exc_info.value.error == "authorization_pending"
    replace_record.assert_awaited_once()
