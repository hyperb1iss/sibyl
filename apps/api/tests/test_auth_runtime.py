from __future__ import annotations

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from sibyl.db.models import ProjectRole
from sibyl.persistence import auth_runtime
from sibyl.persistence.auth_common import InvalidAuthClaimsError, UserNotFoundError
from sibyl.persistence.legacy import auth_runtime as legacy_auth_runtime
from sibyl.persistence.surreal import auth_runtime as surreal_auth_runtime
from sibyl.persistence.surreal.auth import SurrealAuthContextResolver
from sibyl.persistence.surreal.auth_runtime import SurrealSessionRepository


def test_auth_runtime_uses_shared_error_types() -> None:
    assert auth_runtime.InvalidAuthClaimsError is InvalidAuthClaimsError
    assert auth_runtime.UserNotFoundError is UserNotFoundError
    assert auth_runtime.AuthContextResolver is SurrealAuthContextResolver
    assert auth_runtime.SessionRepository is SurrealSessionRepository
    assert "AuthContextResolver" in dir(auth_runtime)


def test_auth_runtime_maps_resolver_name_for_surreal() -> None:
    assert auth_runtime.AuthContextResolver is SurrealAuthContextResolver
    assert auth_runtime.SessionRepository is SurrealSessionRepository


def test_auth_runtime_maps_auth_exports_for_surreal() -> None:
    assert auth_runtime._resolve_backend_export("authenticate_api_key").__module__ == (
        "sibyl.persistence.surreal.auth_runtime"
    )


def test_auth_runtime_resolves_surreal_after_postgres_auth_removal() -> None:
    assert auth_runtime._resolve_backend_export("authenticate_api_key").__module__ == (
        "sibyl.persistence.surreal.auth_runtime"
    )
    assert auth_runtime.AuthContextResolver is SurrealAuthContextResolver
    assert auth_runtime.SessionRepository is SurrealSessionRepository


def test_auth_runtime_exports_neutral_runtime_surface() -> None:
    assert "resolve_auth_context" in auth_runtime.__all__
    assert "patch_auth_user" in auth_runtime.__all__
    assert "list_oauth_connections" in auth_runtime.__all__
    assert "validate_access_session" in auth_runtime.__all__

    assert hasattr(legacy_auth_runtime, "resolve_auth_context")
    assert hasattr(legacy_auth_runtime, "patch_auth_user")
    assert "AuthContextResolver" in surreal_auth_runtime.__all__
    assert "authenticate_api_key" in surreal_auth_runtime.__all__
    assert "SessionRepository" in surreal_auth_runtime.__all__
    assert "list_accessible_project_graph_ids" in surreal_auth_runtime.__all__
    assert "verify_entity_project_access" in surreal_auth_runtime.__all__
    assert "validate_access_session" in surreal_auth_runtime.__all__
    assert surreal_auth_runtime.SessionRepository is SurrealSessionRepository


def test_auth_runtime_backends_cover_public_exports() -> None:
    skipped = {"InvalidAuthClaimsError", "UserNotFoundError"}

    for name in auth_runtime.__all__:
        if name in skipped:
            continue
        assert hasattr(surreal_auth_runtime, name), name
        assert auth_runtime._resolve_backend_export(name) is getattr(surreal_auth_runtime, name)


def test_auth_runtime_surreal_backend_covers_public_exports() -> None:
    skipped = {"InvalidAuthClaimsError", "UserNotFoundError"}

    for name in auth_runtime.__all__:
        if name in skipped:
            continue
        assert hasattr(surreal_auth_runtime, name), name


@pytest.mark.asyncio
async def test_auth_runtime_dispatches_profile_patch_to_surreal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id = uuid4()
    org_id = uuid4()
    expected = object()
    dispatched = AsyncMock(return_value=expected)

    monkeypatch.setattr(surreal_auth_runtime, "patch_auth_user", dispatched)

    result = await auth_runtime.patch_auth_user(
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

    monkeypatch.setattr(surreal_auth_runtime, "authenticate_api_key", dispatched)

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

    monkeypatch.setattr(surreal_auth_runtime, "login_local_user", dispatched)

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
async def test_auth_runtime_dispatches_auth_context_resolution_to_surreal_helper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claims = {"sub": str(uuid4()), "org": str(uuid4())}
    session = object()
    expected = object()
    dispatched = AsyncMock(return_value=expected)

    monkeypatch.setattr(surreal_auth_runtime, "resolve_auth_context", dispatched)

    result = await auth_runtime.resolve_auth_context(claims=claims, session=session)

    assert result is expected
    dispatched.assert_awaited_once_with(claims=claims, session=session)


@pytest.mark.asyncio
async def test_auth_runtime_dispatches_oauth_listing_to_surreal_helper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id = uuid4()
    expected = object()
    dispatched = AsyncMock(return_value=expected)

    monkeypatch.setattr(surreal_auth_runtime, "list_oauth_connections", dispatched)

    result = await auth_runtime.list_oauth_connections(user_id=user_id)

    assert result is expected
    dispatched.assert_awaited_once_with(user_id=user_id)


@pytest.mark.asyncio
async def test_auth_runtime_dispatches_project_lookup_by_graph_id_to_surreal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    organization_id = uuid4()
    expected = object()
    dispatched = AsyncMock(return_value=expected)

    monkeypatch.setattr(surreal_auth_runtime, "get_project_record_by_graph_id", dispatched)

    result = await auth_runtime.get_project_record_by_graph_id(
        organization_id=organization_id,
        graph_project_id="project_abc123",
    )

    assert result is expected
    dispatched.assert_awaited_once_with(
        organization_id=organization_id,
        graph_project_id="project_abc123",
    )


@pytest.mark.asyncio
async def test_auth_runtime_dispatches_project_lookup_by_id_to_surreal_helper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    organization_id = uuid4()
    project_id = uuid4()
    expected = object()
    dispatched = AsyncMock(return_value=expected)

    monkeypatch.setattr(surreal_auth_runtime, "get_project_record_by_id", dispatched)

    result = await auth_runtime.get_project_record_by_id(
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

    monkeypatch.setattr(
        surreal_auth_runtime,
        "list_accessible_project_graph_ids",
        dispatched,
    )

    result = await auth_runtime.list_accessible_project_graph_ids(ctx)

    assert result == expected
    dispatched.assert_awaited_once_with(ctx=ctx)


@pytest.mark.asyncio
async def test_auth_runtime_dispatches_entity_project_access_to_surreal_helper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = object()
    expected = ProjectRole.CONTRIBUTOR
    dispatched = AsyncMock(return_value=expected)

    monkeypatch.setattr(surreal_auth_runtime, "verify_entity_project_access", dispatched)

    result = await auth_runtime.verify_entity_project_access(
        ctx=ctx,
        entity_project_id="project_alpha",
        required_role=ProjectRole.CONTRIBUTOR,
    )

    assert result == expected
    dispatched.assert_awaited_once_with(
        ctx=ctx,
        entity_project_id="project_alpha",
        required_role=ProjectRole.CONTRIBUTOR,
        require_existing_project=False,
    )
