from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from sibyl.api.routes import auth as auth_routes
from sibyl_core.auth import AuthContext, AuthOrganization, AuthUser, OrganizationRole


class FakeRequest:
    def __init__(
        self,
        *,
        json_data: dict[str, object] | None = None,
        form_data: dict[str, object] | None = None,
        query_params: dict[str, str] | None = None,
        cookies: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        state: SimpleNamespace | None = None,
    ) -> None:
        self._json_data = json_data
        self._form_data = form_data or {}
        self.query_params = query_params or {}
        self.cookies = cookies or {}
        self.headers = {k.lower(): v for k, v in (headers or {}).items()}
        if json_data is not None and "content-type" not in self.headers:
            self.headers["content-type"] = "application/json"
        self.client = SimpleNamespace(host="127.0.0.1")
        self.state = state or SimpleNamespace()

    async def json(self) -> dict[str, object] | None:
        return self._json_data

    async def form(self) -> dict[str, object]:
        return self._form_data


async def _call_route(endpoint, /, **kwargs):
    target = getattr(endpoint, "__wrapped__", endpoint)
    return await target(**kwargs)


def _ctx(*, include_org: bool = True) -> AuthContext:
    user = AuthUser(
        id=uuid4(),
        email="nova@example.com",
        name="Nova",
        github_id=42,
        is_admin=True,
        avatar_url="https://example.com/avatar.png",
    )
    organization = AuthOrganization(id=uuid4(), name="Sibyl", slug="sibyl") if include_org else None
    return AuthContext(
        user=user,
        organization=organization,
        org_role=OrganizationRole.ADMIN if include_org else None,
        scopes=frozenset({"api:write"}),
    )


def _issued_session() -> SimpleNamespace:
    return SimpleNamespace(
        user=SimpleNamespace(
            id=uuid4(),
            email="nova@example.com",
            name="Nova",
            github_id=42,
        ),
        organization=SimpleNamespace(
            id=uuid4(),
            slug="sibyl",
            name="Sibyl",
        ),
        access_token="access-token",
        refresh_token="refresh-token",
        refresh_expires=datetime.now(UTC) + timedelta(days=30),
    )


@pytest.mark.asyncio
async def test_github_callback_uses_runtime_helper(monkeypatch: pytest.MonkeyPatch) -> None:
    request = FakeRequest(
        query_params={
            "state": "issued-state",
            "code": "oauth-code",
            "redirect": "/studio",
        },
        cookies={auth_routes.OAUTH_STATE_COOKIE: "state-cookie"},
    )
    identity = SimpleNamespace(github_id=42, email="nova@example.com")
    issued = _issued_session()
    login = AsyncMock(return_value=issued)

    monkeypatch.setattr(auth_routes, "_require_jwt_secret", lambda: "secret")
    monkeypatch.setattr(auth_routes, "verify_state", lambda **_: None)
    monkeypatch.setattr(auth_routes, "_github_exchange_code", AsyncMock(return_value="gh-token"))
    monkeypatch.setattr(auth_routes, "_github_fetch_identity", AsyncMock(return_value=identity))
    monkeypatch.setattr(auth_routes, "login_github_identity", login)

    response = await auth_routes.github_callback(request=request)

    assert response.status_code == 302
    assert response.headers["location"].endswith("/studio")
    login.assert_awaited_once_with(identity=identity, request=request)


@pytest.mark.asyncio
async def test_local_signup_uses_runtime_helper(monkeypatch: pytest.MonkeyPatch) -> None:
    request = FakeRequest(
        json_data={
            "email": "nova@example.com",
            "password": "super-secret",
            "name": "Nova",
        }
    )
    issued = _issued_session()
    signup = AsyncMock(return_value=issued)

    monkeypatch.setattr(auth_routes, "_require_jwt_secret", lambda: "secret")
    monkeypatch.setattr(auth_routes, "signup_local_user", signup)

    response = await auth_routes.local_signup(request=request)

    assert response["user"]["email"] == issued.user.email
    assert response["organization"]["slug"] == issued.organization.slug
    signup.assert_awaited_once_with(
        email="nova@example.com",
        password="super-secret",
        name="Nova",
        request=request,
    )


@pytest.mark.asyncio
async def test_local_login_uses_runtime_helper(monkeypatch: pytest.MonkeyPatch) -> None:
    request = FakeRequest(
        json_data={
            "email": "nova@example.com",
            "password": "super-secret",
        }
    )
    issued = _issued_session()
    login = AsyncMock(return_value=issued)

    monkeypatch.setattr(auth_routes, "_require_jwt_secret", lambda: "secret")
    monkeypatch.setattr(auth_routes, "login_local_user", login)

    response = await _call_route(auth_routes.local_login, request=request)

    assert response["access_token"] == issued.access_token
    login.assert_awaited_once_with(
        email="nova@example.com",
        password="super-secret",
        request=request,
    )


@pytest.mark.asyncio
async def test_local_login_rejects_invalid_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    request = FakeRequest(
        json_data={
            "email": "nova@example.com",
            "password": "wrong",
        }
    )
    login = AsyncMock(return_value=None)

    monkeypatch.setattr(auth_routes, "_require_jwt_secret", lambda: "secret")
    monkeypatch.setattr(auth_routes, "login_local_user", login)

    with pytest.raises(HTTPException, match="Invalid credentials") as exc_info:
        await _call_route(auth_routes.local_login, request=request)

    assert exc_info.value.status_code == 401
    login.assert_awaited_once()


@pytest.mark.asyncio
async def test_device_start_uses_runtime_helper(monkeypatch: pytest.MonkeyPatch) -> None:
    request = FakeRequest(
        json_data={
            "client_name": "sibyl-cli",
            "scope": "mcp",
            "interval": 7,
            "expires_in": 900,
        }
    )
    start = AsyncMock(return_value=(SimpleNamespace(user_code="ABCD-EFGH"), "device-code"))

    monkeypatch.setattr(auth_routes, "_require_jwt_secret", lambda: "secret")
    monkeypatch.setattr(auth_routes, "start_device_authorization", start)

    response = await _call_route(auth_routes.device_start, request=request)

    assert response["device_code"] == "device-code"
    assert response["verification_uri_complete"].endswith("user_code=ABCD-EFGH")
    start.assert_awaited_once_with(
        client_name="sibyl-cli",
        scope="mcp",
        expires_in=timedelta(seconds=900),
        poll_interval_seconds=7,
    )


@pytest.mark.asyncio
async def test_device_token_uses_runtime_helper(monkeypatch: pytest.MonkeyPatch) -> None:
    request = FakeRequest(json_data={"device_code": "device-code"})
    exchange = AsyncMock(return_value={"access_token": "access-token"})

    monkeypatch.setattr(auth_routes, "_require_jwt_secret", lambda: "secret")
    monkeypatch.setattr(auth_routes, "exchange_device_code", exchange)

    response = await _call_route(auth_routes.device_token, request=request)

    assert response.status_code == 200
    assert json.loads(response.body)["access_token"] == "access-token"
    exchange.assert_awaited_once_with(device_code="device-code")


@pytest.mark.asyncio
async def test_device_verify_get_uses_runtime_helpers(monkeypatch: pytest.MonkeyPatch) -> None:
    request = FakeRequest(query_params={"user_code": "ABCD-EFGH"})
    resolve_user = AsyncMock(return_value=SimpleNamespace(email="nova@example.com", name="Nova"))
    get_request = AsyncMock(
        return_value=SimpleNamespace(
            client_name="sibyl-cli",
            scope="mcp",
            expires_at=datetime.now(UTC).replace(tzinfo=None) + timedelta(minutes=10),
            status="pending",
        )
    )

    monkeypatch.setattr(auth_routes, "_require_jwt_secret", lambda: "secret")
    monkeypatch.setattr(auth_routes, "resolve_request_user", resolve_user)
    monkeypatch.setattr(auth_routes, "get_device_request_by_user_code", get_request)

    response = await _call_route(auth_routes.device_verify_get, request=request)
    body = response.body.decode()

    assert "sibyl-cli" in body
    resolve_user.assert_awaited_once_with(request)
    get_request.assert_awaited_once_with("ABCD-EFGH")


@pytest.mark.asyncio
async def test_device_verify_post_login_uses_runtime_helper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = FakeRequest(
        form_data={
            "action": "login",
            "user_code": "ABCD-EFGH",
            "email": "nova@example.com",
            "password": "super-secret",
        }
    )
    login = AsyncMock(return_value=SimpleNamespace(access_token="access-token"))

    monkeypatch.setattr(auth_routes, "_require_jwt_secret", lambda: "secret")
    monkeypatch.setattr(auth_routes, "login_device_browser_user", login)

    response = await _call_route(auth_routes.device_verify_post, request=request)

    assert response.status_code == 302
    assert "sibyl_access_token=access-token" in response.headers.get("set-cookie", "")
    login.assert_awaited_once_with(
        email="nova@example.com",
        password="super-secret",
        request=request,
    )


@pytest.mark.asyncio
async def test_device_verify_post_approve_uses_runtime_helpers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id = uuid4()
    request = FakeRequest(
        form_data={
            "action": "approve",
            "user_code": "ABCD-EFGH",
        }
    )
    claims = AsyncMock(return_value={"sub": str(user_id)})
    get_user = AsyncMock(return_value=SimpleNamespace(id=user_id))
    approve = AsyncMock(return_value=(SimpleNamespace(id=uuid4()), SimpleNamespace(id=uuid4())))

    monkeypatch.setattr(auth_routes, "_require_jwt_secret", lambda: "secret")
    monkeypatch.setattr(auth_routes, "resolve_request_claims", claims)
    monkeypatch.setattr(auth_routes, "get_user_by_id", get_user)
    monkeypatch.setattr(auth_routes, "approve_device_authorization", approve)

    response = await _call_route(auth_routes.device_verify_post, request=request)

    assert response.status_code == 200
    assert "Device Approved" in response.body.decode()
    claims.assert_awaited_once_with(request)
    get_user.assert_awaited_once_with(user_id)
    approve.assert_awaited_once_with(
        user_id=user_id,
        user_code="ABCD-EFGH",
        request=request,
    )


@pytest.mark.asyncio
async def test_refresh_tokens_uses_runtime_rotation(monkeypatch: pytest.MonkeyPatch) -> None:
    user_id = uuid4()
    org_id = uuid4()
    request = FakeRequest(json_data={"refresh_token": "refresh-token"})
    rotation = SimpleNamespace(
        access_token="new-access-token",
        refresh_token="new-refresh-token",
        refresh_expires=datetime.now(UTC) + timedelta(days=30),
    )
    rotate = AsyncMock(return_value=rotation)

    monkeypatch.setattr(auth_routes, "_require_jwt_secret", lambda: "secret")
    monkeypatch.setattr(
        auth_routes,
        "verify_refresh_token",
        lambda _: {"sub": str(user_id), "org": str(org_id)},
    )
    monkeypatch.setattr(auth_routes, "rotate_refresh_exchange", rotate)

    response = await _call_route(auth_routes.refresh_tokens, request=request)

    assert response.status_code == 200
    assert json.loads(response.body)["access_token"] == "new-access-token"
    rotate.assert_awaited_once_with(
        refresh_token="refresh-token",
        user_id=user_id,
        organization_id=org_id,
        request=request,
    )


@pytest.mark.asyncio
async def test_refresh_tokens_returns_503_when_auth_storage_times_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id = uuid4()
    request = FakeRequest(json_data={"refresh_token": "refresh-token"})
    rotate = AsyncMock(side_effect=TimeoutError("timed out during opening handshake"))

    monkeypatch.setattr(auth_routes, "_require_jwt_secret", lambda: "secret")
    monkeypatch.setattr(
        auth_routes,
        "verify_refresh_token",
        lambda _: {"sub": str(user_id)},
    )
    monkeypatch.setattr(auth_routes, "rotate_refresh_exchange", rotate)

    response = await _call_route(auth_routes.refresh_tokens, request=request)

    assert response.status_code == 503
    assert json.loads(response.body)["detail"] == "Authentication storage temporarily unavailable"


@pytest.mark.asyncio
async def test_refresh_tokens_rejects_invalid_org_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id = uuid4()
    request = FakeRequest(json_data={"refresh_token": "refresh-token"})
    rotate = AsyncMock()

    monkeypatch.setattr(auth_routes, "_require_jwt_secret", lambda: "secret")
    monkeypatch.setattr(
        auth_routes,
        "verify_refresh_token",
        lambda _: {"sub": str(user_id), "org": "not-a-uuid"},
    )
    monkeypatch.setattr(auth_routes, "rotate_refresh_exchange", rotate)

    response = await _call_route(auth_routes.refresh_tokens, request=request)

    assert response.status_code == 401
    assert json.loads(response.body)["detail"] == "Invalid token claims"
    rotate.assert_not_awaited()


@pytest.mark.asyncio
async def test_refresh_tokens_clears_cookies_for_invalid_cookie_claims(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id = uuid4()
    request = FakeRequest(cookies={auth_routes.REFRESH_TOKEN_COOKIE: "refresh-token"})

    monkeypatch.setattr(auth_routes, "_require_jwt_secret", lambda: "secret")
    monkeypatch.setattr(
        auth_routes,
        "verify_refresh_token",
        lambda _: {"sub": str(user_id), "org": "not-a-uuid"},
    )
    monkeypatch.setattr(auth_routes, "rotate_refresh_exchange", AsyncMock())

    response = await _call_route(auth_routes.refresh_tokens, request=request)
    set_cookie_headers = [
        value.decode()
        for name, value in response.raw_headers
        if name.lower() == b"set-cookie"
    ]

    assert response.status_code == 401
    assert any(auth_routes.ACCESS_TOKEN_COOKIE in header for header in set_cookie_headers)
    assert any(auth_routes.REFRESH_TOKEN_COOKIE in header for header in set_cookie_headers)


@pytest.mark.asyncio
async def test_logout_uses_runtime_helpers(monkeypatch: pytest.MonkeyPatch) -> None:
    user_id = uuid4()
    org_id = uuid4()
    request = FakeRequest(cookies={auth_routes.ACCESS_TOKEN_COOKIE: "access-token"})
    resolve_claims = AsyncMock(return_value={"sub": str(user_id), "org": str(org_id)})
    log_audit = AsyncMock()
    revoke = AsyncMock()

    monkeypatch.setattr(auth_routes, "resolve_request_claims", resolve_claims)
    monkeypatch.setattr(auth_routes, "log_audit_event", log_audit)
    monkeypatch.setattr(auth_routes, "revoke_access_session", revoke)

    response = await auth_routes.logout(request=request)

    assert response.status_code == 204
    resolve_claims.assert_awaited_once_with(request)
    log_audit.assert_awaited_once_with(
        action="auth.logout",
        user_id=user_id,
        organization_id=org_id,
        request=request,
        details={},
    )
    revoke.assert_awaited_once_with("access-token")


@pytest.mark.asyncio
async def test_list_api_keys_uses_runtime_helper(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _ctx()
    key = SimpleNamespace(
        id=uuid4(),
        name="CLI",
        key_prefix="sk_live_abcd",
        scopes=["mcp"],
        expires_at=None,
        revoked_at=None,
        last_used_at=None,
        created_at=None,
    )
    list_keys = AsyncMock(return_value=[key])
    monkeypatch.setattr(auth_routes, "list_api_keys_for_user", list_keys)

    response = await auth_routes.list_api_keys(ctx=ctx)

    assert response["keys"][0]["name"] == "CLI"
    list_keys.assert_awaited_once_with(
        organization_id=ctx.organization.id,
        user_id=ctx.user.id,
    )


@pytest.mark.asyncio
async def test_create_api_key_uses_runtime_helper(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _ctx()
    request = SimpleNamespace()
    record = SimpleNamespace(
        id=uuid4(),
        name="CLI",
        key_prefix="sk_live_abcd",
        scopes=["mcp"],
        expires_at=None,
    )
    create_key = AsyncMock(return_value=(record, "raw-secret"))
    monkeypatch.setattr(auth_routes, "create_api_key_for_user", create_key)

    response = await auth_routes.create_api_key(
        request=request,
        body=auth_routes.ApiKeyCreateRequest(name="CLI"),
        ctx=ctx,
        _admin=None,
    )

    assert response["api_key"] == "raw-secret"
    create_key.assert_awaited_once()


@pytest.mark.asyncio
async def test_revoke_api_key_rejects_missing_org(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _ctx(include_org=False)

    with pytest.raises(HTTPException, match="No organization context") as exc_info:
        await auth_routes.revoke_api_key(
            request=SimpleNamespace(),
            api_key_id=uuid4(),
            ctx=ctx,
        )

    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_me_uses_auth_context_payload() -> None:
    ctx = _ctx()

    response = await auth_routes.me(ctx=ctx)

    assert response["user"]["email"] == "nova@example.com"
    assert response["organization"]["slug"] == "sibyl"
    assert response["org_role"] == "admin"


@pytest.mark.asyncio
async def test_update_me_uses_runtime_helper(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _ctx()
    request = SimpleNamespace()
    updated_user = SimpleNamespace(
        id=ctx.user.id,
        github_id=ctx.user.github_id,
        email="updated@example.com",
        name="Updated Nova",
        avatar_url="https://example.com/new.png",
    )
    update_user = AsyncMock(return_value=updated_user)
    monkeypatch.setattr(auth_routes, "update_auth_user", update_user)

    response = await auth_routes.update_me(
        request=request,
        body=auth_routes.MeUpdateRequest(name="Updated Nova"),
        ctx=ctx,
    )

    assert response["user"]["email"] == "updated@example.com"
    update_user.assert_awaited_once()
