from __future__ import annotations

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from sibyl.persistence import auth_runtime
from sibyl.persistence.auth_common import InvalidAuthClaimsError, UserNotFoundError
from sibyl.persistence.legacy import auth_runtime as legacy_auth_runtime
from sibyl.persistence.legacy.auth import LegacyAuthContextResolver
from sibyl.persistence.surreal import auth_runtime as surreal_auth_runtime
from sibyl.persistence.surreal.auth import SurrealAuthContextResolver
from sibyl.persistence.surreal.auth_runtime import (
    SurrealSessionRepository,
    resolve_surreal_auth_context,
)


def test_auth_runtime_uses_shared_error_types(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(auth_runtime.settings, "auth_store", "postgres")

    assert auth_runtime.InvalidAuthClaimsError is InvalidAuthClaimsError
    assert auth_runtime.UserNotFoundError is UserNotFoundError
    assert auth_runtime.LegacyAuthContextResolver is LegacyAuthContextResolver


def test_auth_runtime_maps_resolver_name_for_surreal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(auth_runtime.settings, "auth_store", "surreal")

    assert auth_runtime.LegacyAuthContextResolver is SurrealAuthContextResolver
    assert auth_runtime.LegacySessionRepository is SurrealSessionRepository
    assert auth_runtime.resolve_surreal_auth_context is resolve_surreal_auth_context


def test_auth_runtime_maps_auth_exports_for_surreal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(auth_runtime.settings, "auth_store", "surreal")

    assert auth_runtime.authenticate_legacy_api_key.__module__ == (
        "sibyl.persistence.surreal.auth_runtime"
    )


@pytest.mark.asyncio
async def test_auth_runtime_dispatches_profile_patch_to_surreal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id = uuid4()
    org_id = uuid4()
    expected = object()
    dispatched = AsyncMock(return_value=expected)

    monkeypatch.setattr(auth_runtime.settings, "auth_store", "surreal")
    monkeypatch.setattr(surreal_auth_runtime, "patch_legacy_auth_user", dispatched)

    result = await auth_runtime.patch_legacy_auth_user(
        user_id=user_id,
        updates={"name": "Nova"},
        organization_id=org_id,
        request=None,
    )

    assert result is expected
    dispatched.assert_awaited_once_with(
        user_id=user_id,
        updates={"name": "Nova"},
        organization_id=org_id,
        request=None,
    )


@pytest.mark.asyncio
async def test_auth_runtime_dispatches_oauth_listing_to_postgres_helper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id = uuid4()
    expected = object()
    dispatched = AsyncMock(return_value=expected)

    monkeypatch.setattr(auth_runtime.settings, "auth_store", "postgres")
    monkeypatch.setattr(legacy_auth_runtime, "list_legacy_oauth_connections", dispatched)

    result = await auth_runtime.list_legacy_oauth_connections(user_id=user_id)

    assert result is expected
    dispatched.assert_awaited_once_with(user_id=user_id)
