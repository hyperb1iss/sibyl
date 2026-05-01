from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from starlette.requests import Request

from sibyl.auth import dependencies, rls
from sibyl.db.models import OrganizationRole


def _make_request(*, user_id: str, org_id: str) -> Request:
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/test",
            "headers": [],
            "state": {},
        }
    )
    request.state.jwt_claims = {"sub": user_id, "org": org_id}
    return request


@pytest.mark.asyncio
async def test_build_auth_context_uses_surreal_resolver_without_postgres(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id = uuid4()
    org_id = uuid4()
    request = _make_request(user_id=str(user_id), org_id=str(org_id))
    expected_ctx = SimpleNamespace(
        user=SimpleNamespace(id=user_id), organization=SimpleNamespace(id=org_id)
    )

    resolve_auth_context = AsyncMock(return_value=expected_ctx)

    monkeypatch.setattr(dependencies.settings, "auth_store", "surreal")
    monkeypatch.setattr(
        dependencies,
        "resolve_auth_context",
        resolve_auth_context,
    )

    result = await dependencies.build_auth_context(request)

    assert result is expected_ctx
    resolve_auth_context.assert_awaited_once_with(
        claims={"sub": str(user_id), "org": str(org_id)},
        session=None,
    )


@pytest.mark.asyncio
async def test_auth_context_is_cached_across_request_dependencies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id = uuid4()
    org_id = uuid4()
    request = _make_request(user_id=str(user_id), org_id=str(org_id))
    expected_ctx = SimpleNamespace(
        user=SimpleNamespace(id=user_id),
        organization=SimpleNamespace(id=org_id),
        org_role=OrganizationRole.MEMBER,
    )
    resolve_auth_context = AsyncMock(return_value=expected_ctx)

    monkeypatch.setattr(dependencies.settings, "auth_store", "surreal")
    monkeypatch.setattr(dependencies, "resolve_auth_context", resolve_auth_context)

    role = await dependencies.get_current_org_role(request)
    organization = await dependencies.get_current_organization(request)
    ctx = await dependencies.get_auth_context(request)

    assert role is OrganizationRole.MEMBER
    assert organization is expected_ctx.organization
    assert ctx is expected_ctx
    resolve_auth_context.assert_awaited_once_with(
        claims={"sub": str(user_id), "org": str(org_id)},
        session=None,
    )


@pytest.mark.asyncio
async def test_build_auth_context_does_not_reuse_cache_for_explicit_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id = uuid4()
    org_id = uuid4()
    request = _make_request(user_id=str(user_id), org_id=str(org_id))
    cached_ctx = SimpleNamespace(user=SimpleNamespace(id=user_id))
    session_ctx = SimpleNamespace(user=SimpleNamespace(id=user_id), session_scoped=True)
    session = object()
    resolve_auth_context = AsyncMock(return_value=session_ctx)
    request.state.auth_context = cached_ctx

    monkeypatch.setattr(dependencies.settings, "auth_store", "postgres")
    monkeypatch.setattr(dependencies, "resolve_auth_context", resolve_auth_context)

    result = await dependencies.build_auth_context(request, session)

    assert result is session_ctx
    resolve_auth_context.assert_awaited_once_with(
        claims={"sub": str(user_id), "org": str(org_id)},
        session=session,
    )


@pytest.mark.asyncio
async def test_get_current_user_uses_surreal_lookup_without_postgres(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id = uuid4()
    org_id = uuid4()
    request = _make_request(user_id=str(user_id), org_id=str(org_id))
    expected_user = SimpleNamespace(id=user_id, email="nova@example.com")

    get_user_by_id = AsyncMock(return_value=expected_user)

    monkeypatch.setattr(dependencies.settings, "auth_store", "surreal")
    monkeypatch.setattr(dependencies, "get_user_by_id", get_user_by_id)

    result = await dependencies.get_current_user(request)

    assert result is expected_user
    get_user_by_id.assert_awaited_once_with(user_id)


@pytest.mark.asyncio
async def test_get_current_user_reuses_cached_auth_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id = uuid4()
    org_id = uuid4()
    request = _make_request(user_id=str(user_id), org_id=str(org_id))
    expected_user = SimpleNamespace(id=user_id, email="nova@example.com")
    request.state.auth_context = SimpleNamespace(user=expected_user)
    get_user_by_id = AsyncMock()

    monkeypatch.setattr(dependencies.settings, "auth_store", "surreal")
    monkeypatch.setattr(dependencies, "get_user_by_id", get_user_by_id)

    result = await dependencies.get_current_user(request)

    assert result is expected_user
    get_user_by_id.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_current_user_ignores_cached_auth_context_for_other_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id = uuid4()
    org_id = uuid4()
    request = _make_request(user_id=str(user_id), org_id=str(org_id))
    request.state.auth_context = SimpleNamespace(user=SimpleNamespace(id=uuid4()))
    expected_user = SimpleNamespace(id=user_id, email="nova@example.com")
    get_user_by_id = AsyncMock(return_value=expected_user)

    monkeypatch.setattr(dependencies.settings, "auth_store", "surreal")
    monkeypatch.setattr(dependencies, "get_user_by_id", get_user_by_id)

    result = await dependencies.get_current_user(request)

    assert result is expected_user
    get_user_by_id.assert_awaited_once_with(user_id)


@pytest.mark.asyncio
async def test_get_auth_session_uses_surreal_context_without_postgres(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id = uuid4()
    org_id = uuid4()
    request = _make_request(user_id=str(user_id), org_id=str(org_id))
    expected_ctx = SimpleNamespace(
        user=SimpleNamespace(id=user_id),
        organization=SimpleNamespace(id=org_id),
    )

    @asynccontextmanager
    async def fail_get_session():
        raise AssertionError("postgres session should stay off in surreal auth mode")
        yield

    build_auth_context = AsyncMock(return_value=expected_ctx)

    monkeypatch.setattr(rls.settings, "store", "surreal")
    monkeypatch.setattr(dependencies.settings, "auth_store", "surreal")
    monkeypatch.setattr(rls, "get_session", fail_get_session)
    monkeypatch.setattr(dependencies, "build_auth_context", build_auth_context)

    generator = rls.get_auth_session(request)
    auth_session = await anext(generator)

    assert auth_session.ctx is expected_ctx
    assert auth_session.session is None
    build_auth_context.assert_awaited_once_with(request, None)

    with pytest.raises(StopAsyncIteration):
        await anext(generator)


@pytest.mark.asyncio
async def test_get_auth_session_keeps_relational_session_in_mixed_surreal_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from contextlib import asynccontextmanager

    user_id = uuid4()
    org_id = uuid4()
    request = _make_request(user_id=str(user_id), org_id=str(org_id))
    expected_ctx = SimpleNamespace(
        user=SimpleNamespace(id=user_id),
        organization=SimpleNamespace(id=org_id),
    )
    mock_session = AsyncMock()
    set_rls_context = AsyncMock()
    build_auth_context = AsyncMock(return_value=expected_ctx)

    @asynccontextmanager
    async def relational_session():
        yield mock_session

    monkeypatch.setattr(rls.settings, "store", "surreal")
    monkeypatch.setattr(dependencies.settings, "auth_store", "postgres")
    monkeypatch.setattr(rls, "get_session", relational_session)
    monkeypatch.setattr(rls, "set_rls_context", set_rls_context)
    monkeypatch.setattr(dependencies, "build_auth_context", build_auth_context)

    generator = rls.get_auth_session(request)
    auth_session = await anext(generator)

    assert auth_session.ctx is expected_ctx
    assert auth_session.session is mock_session
    build_auth_context.assert_awaited_once_with(request, mock_session)
    set_rls_context.assert_awaited_once_with(
        mock_session,
        user_id=user_id,
        org_id=org_id,
    )

    with pytest.raises(StopAsyncIteration):
        await anext(generator)
