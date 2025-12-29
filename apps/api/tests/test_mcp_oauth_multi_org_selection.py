from __future__ import annotations

import time
from contextlib import asynccontextmanager
from urllib.parse import parse_qs, urlencode, urlsplit
from uuid import uuid4

import pytest
from mcp.server.auth.provider import AuthorizationParams
from pydantic.networks import AnyUrl
from starlette.requests import Request

from sibyl.auth.mcp_oauth import SibylMcpOAuthProvider, _AuthedUser, _PendingAuth
from sibyl.auth.users import UserManager
from sibyl.db.models import Organization, User


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
async def test_mcp_oauth_login_redirects_to_org_selection_for_multi_org_user(monkeypatch) -> None:
    provider = SibylMcpOAuthProvider()

    user = User(id=uuid4(), name="Test User")
    org1 = Organization(id=uuid4(), name="Org One", slug="org-one", is_personal=False)
    org2 = Organization(id=uuid4(), name="Org Two", slug="org-two", is_personal=True)

    params = AuthorizationParams(
        state="state123",
        scopes=["mcp"],
        code_challenge="challenge",
        redirect_uri=AnyUrl("http://client.local/cb"),
        redirect_uri_provided_explicitly=True,
        resource="http://localhost:3334/mcp",
    )
    provider._pending["req123"] = _PendingAuth(
        client_id="client1", expires_at=time.time() + 600, params=params
    )

    @asynccontextmanager
    async def fake_session():  # type: ignore[no-untyped-def]
        yield object()

    async def fake_authenticate_local(self, *, email: str, password: str):  # type: ignore[no-untyped-def]
        return user

    async def fake_list_user_orgs(self, session, *, user_id):  # type: ignore[no-untyped-def]
        return [org1, org2]

    monkeypatch.setattr("sibyl.auth.mcp_oauth.get_session", fake_session)
    monkeypatch.setattr(UserManager, "authenticate_local", fake_authenticate_local)
    monkeypatch.setattr(provider, "_list_user_orgs", fake_list_user_orgs.__get__(provider))

    req = _make_form_post_request(
        path="/_oauth/login",
        data={"req": "req123", "email": "test@example.com", "password": "pw"},
    )
    resp = await provider.ui_login_post(req)

    assert resp.status_code == 302
    assert resp.headers["location"] == "/_oauth/org?req=req123"
    authed = provider._get_authed_user("req123")
    assert authed is not None
    assert authed.user_id == user.id


@pytest.mark.asyncio
async def test_mcp_oauth_org_selection_issues_code(monkeypatch) -> None:
    provider = SibylMcpOAuthProvider()

    user = User(id=uuid4(), name="Test User")
    org1 = Organization(id=uuid4(), name="Org One", slug="org-one", is_personal=False)
    org2 = Organization(id=uuid4(), name="Org Two", slug="org-two", is_personal=True)

    params = AuthorizationParams(
        state="state123",
        scopes=["mcp"],
        code_challenge="challenge",
        redirect_uri=AnyUrl("http://client.local/cb"),
        redirect_uri_provided_explicitly=True,
        resource="http://localhost:3334/mcp",
    )
    provider._pending["req123"] = _PendingAuth(
        client_id="client1", expires_at=time.time() + 600, params=params
    )
    provider._authed["req123"] = _AuthedUser(user_id=user.id, expires_at=time.time() + 300)

    @asynccontextmanager
    async def fake_session():  # type: ignore[no-untyped-def]
        yield object()

    async def fake_list_user_orgs(self, session, *, user_id):  # type: ignore[no-untyped-def]
        return [org1, org2]

    monkeypatch.setattr("sibyl.auth.mcp_oauth.get_session", fake_session)
    monkeypatch.setattr(provider, "_list_user_orgs", fake_list_user_orgs.__get__(provider))
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
    assert "req123" not in provider._pending
    assert "req123" not in provider._authed
