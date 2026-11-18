from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlencode, urlsplit
from uuid import UUID, uuid4

import pytest
from mcp.shared.auth import OAuthClientInformationFull
from starlette.requests import Request

from sibyl import config as config_module
from sibyl.api.routes import auth as auth_routes
from sibyl.auth.mcp_oauth import SibylMcpOAuthProvider
from sibyl.auth.oidc import EnabledOIDCProvider
from sibyl.persistence.surreal.auth_runtime.oauth_authorization import (
    SurrealOAuthAuthorizationStore,
)
from sibyl_core.auth import PrincipalKind
from sibyl_core.backends.surreal import SurrealAuthClient, bootstrap_auth_schema
from sibyl_core.backends.surreal.records import utcnow


@pytest.fixture
async def store() -> AsyncIterator[SurrealOAuthAuthorizationStore]:
    client = SurrealAuthClient(url="memory://", namespace=f"oauth_ui_{uuid4().hex}")
    await bootstrap_auth_schema(client)

    @asynccontextmanager
    async def scope() -> AsyncIterator[SurrealAuthClient]:
        yield client

    try:
        yield SurrealOAuthAuthorizationStore(scope)
    finally:
        await client.close()


async def _pending(
    store: SurrealOAuthAuthorizationStore, request_id: str, *, authed_user: UUID | None = None
) -> None:
    await store.create_request(
        request_key=request_id,
        client_id="client1",
        state="state123",
        scopes=["mcp"],
        code_challenge="challenge",
        redirect_uri="http://client.local/cb",
        redirect_uri_provided_explicitly=True,
        resource="http://localhost:3334/mcp",
        expires_at=utcnow() + timedelta(minutes=10),
    )
    if authed_user is not None:
        assert await store.mark_authenticated(
            request_id,
            user_id=authed_user,
            authenticated_expires_at=utcnow() + timedelta(minutes=5),
        )


def _make_get_request(*, path: str, query: dict[str, str]) -> Request:
    scope = {
        "type": "http",
        "method": "GET",
        "path": path,
        "query_string": urlencode(query).encode("utf-8"),
        "headers": [(b"host", b"testserver")],
        "scheme": "http",
        "server": ("testserver", 80),
        "client": ("127.0.0.1", 12345),
    }
    return Request(scope)


def _make_form_post_request(*, path: str, data: dict[str, str]) -> Request:
    body = urlencode(data).encode("utf-8")

    async def receive():  # type: ignore[no-untyped-def]
        return {"type": "http.request", "body": body, "more_body": False}

    scope = {
        "type": "http",
        "method": "POST",
        "path": path,
        "query_string": b"",
        "headers": [
            (b"host", b"testserver"),
            (b"content-type", b"application/x-www-form-urlencoded"),
        ],
        "scheme": "http",
        "server": ("testserver", 80),
        "client": ("127.0.0.1", 12345),
    }
    return Request(scope, receive)


@pytest.mark.asyncio
async def test_mcp_oauth_login_redirects_to_org_selection_for_multi_org_user(
    monkeypatch, store: SurrealOAuthAuthorizationStore
) -> None:
    provider = SibylMcpOAuthProvider(authorization_store=store)

    user = SimpleNamespace(id=uuid4(), name="Test User")
    org1 = SimpleNamespace(id=uuid4(), name="Org One", slug="org-one", is_personal=False)
    org2 = SimpleNamespace(id=uuid4(), name="Org Two", slug="org-two", is_personal=True)

    await _pending(store, "req123")

    monkeypatch.setattr(provider, "_session_user", AsyncMock(return_value=user))
    monkeypatch.setattr(
        provider,
        "_list_user_orgs",
        AsyncMock(return_value=[org1, org2]),
    )

    req = _make_form_post_request(
        path="/_oauth/login",
        data={"req": "req123", "action": "approve"},
    )
    resp = await provider.ui_login_post(req)

    assert resp.status_code == 302
    assert resp.headers["location"] == "/_oauth/org?req=req123"
    pending = await store.load_request("req123")
    assert pending is not None
    assert pending.authenticated_user(now=utcnow()) == user.id


@pytest.mark.asyncio
async def test_mcp_oauth_org_selection_issues_code(
    monkeypatch, store: SurrealOAuthAuthorizationStore
) -> None:
    provider = SibylMcpOAuthProvider(authorization_store=store)

    user = SimpleNamespace(id=uuid4(), name="Test User")
    org1 = SimpleNamespace(id=uuid4(), name="Org One", slug="org-one", is_personal=False)
    org2 = SimpleNamespace(id=uuid4(), name="Org Two", slug="org-two", is_personal=True)

    await _pending(store, "req123", authed_user=user.id)

    monkeypatch.setattr(
        provider,
        "_list_user_orgs",
        AsyncMock(return_value=[org1, org2]),
    )
    monkeypatch.setattr("sibyl.auth.mcp_oauth.secrets.token_urlsafe", lambda n=32: "code_abc")  # type: ignore[assignment]

    req = _make_form_post_request(
        path="/_oauth/org",
        data={"req": "req123", "org_id": str(org2.id)},
    )
    resp = await provider.ui_org_post(req)

    assert resp.status_code == 302
    location = resp.headers["location"]
    parsed = urlsplit(location)
    assert parsed.scheme == "http"
    assert parsed.netloc == "client.local"
    qs = parse_qs(parsed.query)
    assert qs["code"] == ["code_abc"]
    assert qs["state"] == ["state123"]
    assert await store.load_request("req123") is None
    code = await store.load_code("code_abc")
    assert code is not None
    assert code.organization_id == org2.id
    assert code.user_id == user.id


@pytest.mark.asyncio
async def test_mcp_oauth_org_page_escapes_org_name(
    monkeypatch, store: SurrealOAuthAuthorizationStore
) -> None:
    provider = SibylMcpOAuthProvider(authorization_store=store)

    user = SimpleNamespace(id=uuid4(), name="Test User")
    hostile_org = SimpleNamespace(
        id=uuid4(),
        name='</strong><script>alert("xss")</script><strong>',
        slug="evil",
        is_personal=False,
    )

    await _pending(store, "req123", authed_user=user.id)

    monkeypatch.setattr(
        provider,
        "_list_user_orgs",
        AsyncMock(return_value=[hostile_org]),
    )

    req = _make_get_request(path="/_oauth/org", query={"req": "req123"})
    resp = await provider.ui_org_get(req)

    assert resp.status_code == 200
    body = resp.body.decode("utf-8")
    assert '<script>alert("xss")</script>' not in body
    assert "&lt;script&gt;alert(&quot;xss&quot;)&lt;/script&gt;" in body


@pytest.mark.asyncio
async def test_mcp_oauth_login_page_escapes_client_name(
    monkeypatch, store: SurrealOAuthAuthorizationStore
) -> None:
    provider = SibylMcpOAuthProvider(authorization_store=store)
    monkeypatch.setattr(provider, "_save_oauth_client_registration", AsyncMock())
    await _pending(store, "req123")
    await provider.register_client(
        OAuthClientInformationFull(
            client_id="client1",
            client_secret="secret1",
            redirect_uris=["http://127.0.0.1:9911/callback"],
            token_endpoint_auth_method="client_secret_post",
            scope="mcp",
            client_name='</strong><script>alert("xss")</script><strong>',
        )
    )

    monkeypatch.setattr(provider, "_session_user", AsyncMock(return_value=None))
    monkeypatch.setattr(provider, "_local_login_offered", AsyncMock(return_value=True))
    req = _make_get_request(path="/_oauth/login", query={"req": "req123"})
    resp = await provider.ui_login_get(req)

    assert resp.status_code == 200
    body = resp.body.decode("utf-8")
    assert '<script>alert("xss")</script>' not in body
    assert (
        "&lt;/strong&gt;&lt;script&gt;alert(&quot;xss&quot;)&lt;/script&gt;&lt;strong&gt;" in body
    )


@pytest.fixture
def sso_only(monkeypatch) -> None:
    """An SSO-only instance past setup: neither local nor break-glass sign-in."""
    monkeypatch.setattr(config_module.settings, "local_auth_enabled", False)
    monkeypatch.setattr(config_module.settings, "break_glass_enabled", False)
    monkeypatch.setattr(auth_routes, "is_setup_mode", AsyncMock(return_value=False))
    monkeypatch.setattr(
        "sibyl.auth.mcp_oauth.enabled_oidc_providers",
        lambda: [
            EnabledOIDCProvider(
                name="entra", label="Microsoft", login_url="/api/auth/oidc/entra/login"
            )
        ],
    )


@pytest.mark.asyncio
@pytest.mark.usefixtures("sso_only")
async def test_mcp_oauth_login_refuses_passwords_when_local_auth_is_disabled(
    monkeypatch, store: SurrealOAuthAuthorizationStore
) -> None:
    provider = SibylMcpOAuthProvider(authorization_store=store)
    login = AsyncMock()
    monkeypatch.setattr(auth_routes, "login_local_user", login)
    await _pending(store, "req123")

    resp = await provider.ui_login_post(
        _make_form_post_request(
            path="/_oauth/login",
            data={
                "req": "req123",
                "action": "login",
                "email": "retained@example.com",
                "password": "still-valid-password",
            },
        )
    )

    assert resp.status_code == 302
    assert resp.headers["location"] == "/_oauth/login?req=req123&error=local_auth_disabled"
    assert "set-cookie" not in resp.headers
    login.assert_not_awaited()
    pending = await store.load_request("req123")
    assert pending is not None
    assert pending.authenticated_user(now=utcnow()) is None


@pytest.mark.asyncio
@pytest.mark.usefixtures("sso_only")
async def test_mcp_oauth_login_page_offers_sso_back_to_the_request_without_passwords(
    monkeypatch, store: SurrealOAuthAuthorizationStore
) -> None:
    provider = SibylMcpOAuthProvider(authorization_store=store)
    monkeypatch.setattr(provider, "_session_user", AsyncMock(return_value=None))
    await _pending(store, "req123")

    resp = await provider.ui_login_get(
        _make_get_request(path="/_oauth/login", query={"req": "req123"})
    )

    assert resp.status_code == 200
    body = resp.body.decode("utf-8")
    assert 'href="/api/auth/oidc/entra/login?redirect=%2F_oauth%2Flogin%3Freq%3Dreq123"' in body
    assert 'name="password"' not in body


@pytest.mark.asyncio
async def test_mcp_oauth_password_login_starts_a_session_without_authorizing(
    monkeypatch, store: SurrealOAuthAuthorizationStore
) -> None:
    provider = SibylMcpOAuthProvider(authorization_store=store)
    monkeypatch.setattr(config_module.settings, "local_auth_enabled", True)
    monkeypatch.setattr(config_module.settings, "break_glass_enabled", False)
    monkeypatch.setattr(auth_routes, "is_setup_mode", AsyncMock(return_value=False))
    issued = SimpleNamespace(
        access_token="access-jwt",
        refresh_token="refresh-jwt",
        refresh_expires=datetime.now(UTC) + timedelta(days=30),
    )
    login = AsyncMock(return_value=issued)
    monkeypatch.setattr(auth_routes, "login_local_user", login)
    await _pending(store, "req123")

    resp = await provider.ui_login_post(
        _make_form_post_request(
            path="/_oauth/login",
            data={"req": "req123", "action": "login", "email": "a@b.c", "password": "pw"},
        )
    )

    assert resp.status_code == 302
    assert resp.headers["location"] == "/_oauth/login?req=req123"
    assert "sibyl_access_token=access-jwt" in resp.headers["set-cookie"]
    login.assert_awaited_once()
    # The client gets nothing until the signed-in user approves it.
    pending = await store.load_request("req123")
    assert pending is not None
    assert pending.authenticated_user(now=utcnow()) is None


@pytest.mark.asyncio
async def test_mcp_oauth_signed_in_user_sees_consent_not_a_code(
    monkeypatch, store: SurrealOAuthAuthorizationStore
) -> None:
    provider = SibylMcpOAuthProvider(authorization_store=store)
    user = SimpleNamespace(id=uuid4(), email="sso@example.com", name="SSO User")
    monkeypatch.setattr(provider, "_session_user", AsyncMock(return_value=user))
    await _pending(store, "req123")

    resp = await provider.ui_login_get(
        _make_get_request(path="/_oauth/login", query={"req": "req123"})
    )

    assert resp.status_code == 200
    body = resp.body.decode("utf-8")
    assert "sso@example.com" in body
    assert "<code>client.local</code>" in body
    assert 'name="action" value="approve"' in body
    assert await store.load_request("req123") is not None


@pytest.mark.asyncio
async def test_mcp_oauth_deny_returns_access_denied_to_the_client(
    monkeypatch, store: SurrealOAuthAuthorizationStore
) -> None:
    provider = SibylMcpOAuthProvider(authorization_store=store)
    user = SimpleNamespace(id=uuid4(), email="sso@example.com")
    monkeypatch.setattr(provider, "_session_user", AsyncMock(return_value=user))
    issue = AsyncMock()
    monkeypatch.setattr(provider, "_issue_code", issue)
    await store.create_request(
        request_key="req123",
        client_id="client1",
        state="state123",
        scopes=["mcp"],
        code_challenge="challenge",
        redirect_uri="http://client.local/cb?channel=desktop",
        redirect_uri_provided_explicitly=True,
        resource="http://localhost:3334/mcp",
        expires_at=utcnow() + timedelta(minutes=10),
    )
    # An earlier approve left the flow at the org picker, e.g. in another tab.
    assert await store.mark_authenticated(
        "req123", user_id=user.id, authenticated_expires_at=utcnow() + timedelta(minutes=5)
    )

    resp = await provider.ui_login_post(
        _make_form_post_request(path="/_oauth/login", data={"req": "req123", "action": "deny"})
    )

    assert resp.status_code == 302
    location = urlsplit(resp.headers["location"])
    assert location.netloc == "client.local"
    assert parse_qs(location.query) == {
        "channel": ["desktop"],
        "error": ["access_denied"],
        "state": ["state123"],
    }
    issue.assert_not_awaited()
    # The denied flow is closed, so the stale org picker cannot issue a code.
    stale_org_post = await provider.ui_org_post(
        _make_form_post_request(path="/_oauth/org", data={"req": "req123", "org_id": "x"})
    )
    assert stale_org_post.status_code == 400
    assert await store.load_request("req123") is None


@pytest.mark.asyncio
async def test_mcp_oauth_approve_without_a_session_goes_back_to_sign_in(
    monkeypatch, store: SurrealOAuthAuthorizationStore
) -> None:
    provider = SibylMcpOAuthProvider(authorization_store=store)
    monkeypatch.setattr(provider, "_session_user", AsyncMock(return_value=None))
    issue = AsyncMock()
    monkeypatch.setattr(provider, "_issue_code", issue)
    await _pending(store, "req123")

    resp = await provider.ui_login_post(
        _make_form_post_request(path="/_oauth/login", data={"req": "req123", "action": "approve"})
    )

    assert resp.status_code == 302
    assert resp.headers["location"] == "/_oauth/login?req=req123"
    issue.assert_not_awaited()


@pytest.mark.asyncio
async def test_mcp_oauth_bot_session_cannot_authorize(
    monkeypatch, store: SurrealOAuthAuthorizationStore
) -> None:
    provider = SibylMcpOAuthProvider(authorization_store=store)
    bot = SimpleNamespace(id=uuid4(), email="bot@example.com", kind=PrincipalKind.SERVICE)
    monkeypatch.setattr(provider, "_session_user", AsyncMock(return_value=bot))
    issue = AsyncMock()
    monkeypatch.setattr(provider, "_issue_code", issue)
    await _pending(store, "req123")

    resp = await provider.ui_login_post(
        _make_form_post_request(path="/_oauth/login", data={"req": "req123", "action": "approve"})
    )

    assert resp.status_code == 403
    issue.assert_not_awaited()
