from __future__ import annotations

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from sibyl.db.models import ProjectRole
from sibyl.persistence import auth_runtime
from sibyl.persistence.auth_common import InvalidAuthClaimsError, UserNotFoundError
from sibyl.persistence.legacy import auth_runtime as legacy_auth_runtime
from sibyl.persistence.legacy.auth import LegacyAuthContextResolver, LegacySessionRepository
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
    assert auth_runtime.AuthContextResolver is LegacyAuthContextResolver
    assert auth_runtime.LegacyAuthContextResolver is LegacyAuthContextResolver
    assert auth_runtime.SessionRepository is LegacySessionRepository
    assert "AuthContextResolver" in dir(auth_runtime)


def test_auth_runtime_maps_resolver_name_for_surreal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(auth_runtime.settings, "auth_store", "surreal")

    assert auth_runtime.AuthContextResolver is SurrealAuthContextResolver
    assert auth_runtime.LegacyAuthContextResolver is SurrealAuthContextResolver
    assert auth_runtime.SessionRepository is SurrealSessionRepository
    assert auth_runtime.LegacySessionRepository is SurrealSessionRepository
    assert auth_runtime.resolve_surreal_auth_context is resolve_surreal_auth_context


def test_auth_runtime_maps_auth_exports_for_surreal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(auth_runtime.settings, "auth_store", "surreal")

    assert auth_runtime.authenticate_legacy_api_key.__module__ == (
        "sibyl.persistence.surreal.auth_runtime"
    )


def test_auth_runtime_keeps_legacy_helper_aliases_pointed_at_neutral_exports() -> None:
    assert auth_runtime.resolve_legacy_auth_context is auth_runtime.resolve_auth_context
    assert auth_runtime.patch_legacy_auth_user is auth_runtime.patch_auth_user
    assert auth_runtime.get_legacy_project_record_by_graph_id is auth_runtime.get_project_record_by_graph_id
    assert auth_runtime.list_legacy_oauth_connections is auth_runtime.list_oauth_connections


def test_auth_runtime_surreal_backend_covers_public_exports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(auth_runtime.settings, "auth_store", "surreal")

    overrides = auth_runtime._BACKEND_NAME_OVERRIDES.get("surreal", {})
    skipped = {"InvalidAuthClaimsError", "UserNotFoundError"}

    for name in auth_runtime.__all__:
        if name in skipped:
            continue
        export_name = overrides.get(name, name)
        assert hasattr(surreal_auth_runtime, export_name), name


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
async def test_auth_runtime_neutral_api_key_alias_dispatches_to_surreal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = object()
    dispatched = AsyncMock(return_value=expected)

    monkeypatch.setattr(auth_runtime.settings, "auth_store", "surreal")
    monkeypatch.setattr(surreal_auth_runtime, "authenticate_legacy_api_key", dispatched)

    result = await auth_runtime.authenticate_api_key("sk_live_test")

    assert result is expected
    dispatched.assert_awaited_once_with("sk_live_test")


@pytest.mark.asyncio
async def test_auth_runtime_neutral_login_alias_dispatches_to_surreal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = object()
    request = object()
    dispatched = AsyncMock(return_value=expected)

    monkeypatch.setattr(auth_runtime.settings, "auth_store", "surreal")
    monkeypatch.setattr(surreal_auth_runtime, "login_legacy_local_user", dispatched)

    result = await auth_runtime.login_local_user(
        email="nova@example.com",
        password="secret",
        request=request,
    )

    assert result is expected
    dispatched.assert_awaited_once_with(
        email="nova@example.com",
        password="secret",
        request=request,
    )


@pytest.mark.asyncio
async def test_auth_runtime_dispatches_auth_context_resolution_to_postgres_helper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claims = {"sub": str(uuid4()), "org": str(uuid4())}
    session = object()
    expected = object()
    dispatched = AsyncMock(return_value=expected)

    monkeypatch.setattr(auth_runtime.settings, "auth_store", "postgres")
    monkeypatch.setattr(legacy_auth_runtime, "resolve_legacy_auth_context", dispatched)

    result = await auth_runtime.resolve_legacy_auth_context(claims=claims, session=session)

    assert result is expected
    dispatched.assert_awaited_once_with(claims=claims, session=session)


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


@pytest.mark.asyncio
async def test_auth_runtime_dispatches_project_lookup_by_graph_id_to_surreal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    organization_id = uuid4()
    expected = object()
    dispatched = AsyncMock(return_value=expected)

    monkeypatch.setattr(auth_runtime.settings, "auth_store", "surreal")
    monkeypatch.setattr(surreal_auth_runtime, "get_legacy_project_record_by_graph_id", dispatched)

    result = await auth_runtime.get_legacy_project_record_by_graph_id(
        organization_id=organization_id,
        graph_project_id="project_abc123",
    )

    assert result is expected
    dispatched.assert_awaited_once_with(
        organization_id=organization_id,
        graph_project_id="project_abc123",
    )


@pytest.mark.asyncio
async def test_auth_runtime_dispatches_project_lookup_by_id_to_postgres_helper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    organization_id = uuid4()
    project_id = uuid4()
    expected = object()
    dispatched = AsyncMock(return_value=expected)

    monkeypatch.setattr(auth_runtime.settings, "auth_store", "postgres")
    monkeypatch.setattr(legacy_auth_runtime, "get_legacy_project_record_by_id", dispatched)

    result = await auth_runtime.get_legacy_project_record_by_id(
        organization_id=organization_id,
        project_id=project_id,
    )

    assert result is expected
    dispatched.assert_awaited_once_with(
        organization_id=organization_id,
        project_id=project_id,
    )


@pytest.mark.asyncio
async def test_auth_runtime_dispatches_project_access_list_to_surreal_helper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = object()
    expected = {"project_alpha"}
    dispatched = AsyncMock(return_value=expected)

    monkeypatch.setattr(auth_runtime.settings, "auth_store", "surreal")
    monkeypatch.setattr(
        surreal_auth_runtime,
        "list_legacy_accessible_project_graph_ids",
        dispatched,
    )

    result = await auth_runtime.list_legacy_accessible_project_graph_ids(ctx)

    assert result == expected
    dispatched.assert_awaited_once_with(ctx=ctx)


@pytest.mark.asyncio
async def test_auth_runtime_dispatches_entity_project_access_to_postgres_helper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = object()
    expected = ProjectRole.CONTRIBUTOR
    dispatched = AsyncMock(return_value=expected)

    monkeypatch.setattr(auth_runtime.settings, "auth_store", "postgres")
    monkeypatch.setattr(legacy_auth_runtime, "verify_legacy_entity_project_access", dispatched)

    result = await auth_runtime.verify_legacy_entity_project_access(
        ctx=ctx,
        entity_project_id="project_alpha",
        required_role=ProjectRole.CONTRIBUTOR,
    )

    assert result == expected
    dispatched.assert_awaited_once_with(
        ctx=ctx,
        entity_project_id="project_alpha",
        required_role=ProjectRole.CONTRIBUTOR,
    )
