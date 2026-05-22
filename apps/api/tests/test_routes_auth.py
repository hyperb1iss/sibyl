from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from inspect import signature
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException, Response

from sibyl.api.errors import http_exception_payload
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
    if "response" in signature(target).parameters and "response" not in kwargs:
        kwargs["response"] = Response()
    return await target(**kwargs)


def _set_cookie_headers(response: Response) -> list[str]:
    return [value.decode() for key, value in response.raw_headers if key.lower() == b"set-cookie"]


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
        session_id=uuid4(),
        access_token="access-token",
        refresh_token="refresh-token",
        refresh_expires=datetime.now(UTC) + timedelta(days=30),
    )


@pytest.fixture(autouse=True)
def _enable_local_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(auth_routes.config_module.settings, "local_auth_enabled", True)
    monkeypatch.setattr(auth_routes.config_module.settings, "break_glass_enabled", False)
    monkeypatch.setattr(auth_routes.config_module.settings, "break_glass_allowed_ips", [])
    monkeypatch.setattr(auth_routes.config_module.settings, "break_glass_expires_at", None)


@pytest.mark.asyncio
async def test_auth_providers_exposes_break_glass_local_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(auth_routes.config_module.settings, "local_auth_enabled", False)
    monkeypatch.setattr(auth_routes.config_module.settings, "break_glass_enabled", True)
    monkeypatch.setattr(auth_routes, "is_setup_mode", AsyncMock(return_value=False))

    response = await auth_routes.auth_providers()

    assert response.local_auth_enabled is True
    assert response.break_glass_enabled is True


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

    monkeypatch.setattr(
        auth_routes, "_require_jwt_secret", lambda: "test-jwt-secret-key-for-api-tests"
    )
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

    monkeypatch.setattr(
        auth_routes, "_require_jwt_secret", lambda: "test-jwt-secret-key-for-api-tests"
    )
    monkeypatch.setattr(auth_routes, "is_setup_mode", AsyncMock(return_value=True))
    monkeypatch.setattr(auth_routes, "signup_local_user", signup)

    route_response = Response()
    response = await _call_route(auth_routes.local_signup, request=request, response=route_response)

    assert response["user"]["email"] == issued.user.email
    assert response["organization"]["slug"] == issued.organization.slug
    assert route_response.status_code == 201
    cookies = _set_cookie_headers(route_response)
    assert any(auth_routes.ACCESS_TOKEN_COOKIE in cookie for cookie in cookies)
    assert any(auth_routes.REFRESH_TOKEN_COOKIE in cookie for cookie in cookies)
    signup.assert_awaited_once_with(
        email="nova@example.com",
        password="super-secret",
        name="Nova",
        request=request,
    )


@pytest.mark.asyncio
async def test_local_signup_rejects_public_signup_when_setup_complete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = FakeRequest(
        json_data={
            "email": "nova@example.com",
            "password": "super-secret",
            "name": "Nova",
        }
    )
    signup = AsyncMock()

    monkeypatch.setattr(
        auth_routes, "_require_jwt_secret", lambda: "test-jwt-secret-key-for-api-tests"
    )
    monkeypatch.setattr(auth_routes, "is_setup_mode", AsyncMock(return_value=False))
    monkeypatch.setattr(auth_routes.config_module.settings, "public_signups_enabled", False)
    monkeypatch.setattr(auth_routes, "signup_local_user", signup)

    with pytest.raises(HTTPException) as exc_info:
        await _call_route(auth_routes.local_signup, request=request)

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail["code"] == "signup_disabled"
    signup.assert_not_awaited()


@pytest.mark.asyncio
async def test_local_signup_accepts_invitation_when_public_signup_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = FakeRequest(
        json_data={
            "email": "nova@example.com",
            "password": "super-secret",
            "name": "Nova",
            "invite_token": "invite-token",
        }
    )
    issued = _issued_session()
    accepted = SimpleNamespace(
        access_token="invited-access-token",
        refresh_token="invited-refresh-token",
        refresh_expires=datetime.now(UTC) + timedelta(days=30),
        organization_id=uuid4(),
        invitation_id=uuid4(),
        organization_slug="electric-coven",
        organization_name="Electric Coven",
    )
    validate = AsyncMock()
    signup = AsyncMock(return_value=issued)
    accept = AsyncMock(return_value=accepted)

    monkeypatch.setattr(
        auth_routes, "_require_jwt_secret", lambda: "test-jwt-secret-key-for-api-tests"
    )
    monkeypatch.setattr(auth_routes, "is_setup_mode", AsyncMock(return_value=False))
    monkeypatch.setattr(auth_routes.config_module.settings, "public_signups_enabled", False)
    monkeypatch.setattr(
        auth_routes.organization_runtime, "validate_org_invitation_for_signup", validate
    )
    monkeypatch.setattr(auth_routes.organization_runtime, "accept_org_invitation", accept)
    monkeypatch.setattr(auth_routes, "signup_local_user", signup)

    response = await _call_route(auth_routes.local_signup, request=request)

    validate.assert_awaited_once_with(token="invite-token", email="nova@example.com")
    accept.assert_awaited_once_with(
        token="invite-token",
        user=issued.user,
        request=request,
        existing_session_id=issued.session_id,
    )
    assert response["access_token"] == "invited-access-token"
    assert response["organization"]["slug"] == "electric-coven"


@pytest.mark.asyncio
async def test_local_signup_validates_invitation_before_setup_signup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = FakeRequest(
        json_data={
            "email": "nova@example.com",
            "password": "super-secret",
            "name": "Nova",
            "invite_token": "bad-token",
        }
    )
    validate = AsyncMock(side_effect=HTTPException(status_code=400, detail="Invalid invitation"))
    signup = AsyncMock()

    monkeypatch.setattr(
        auth_routes, "_require_jwt_secret", lambda: "test-jwt-secret-key-for-api-tests"
    )
    monkeypatch.setattr(auth_routes, "is_setup_mode", AsyncMock(return_value=True))
    monkeypatch.setattr(auth_routes.config_module.settings, "public_signups_enabled", False)
    monkeypatch.setattr(
        auth_routes.organization_runtime, "validate_org_invitation_for_signup", validate
    )
    monkeypatch.setattr(auth_routes, "signup_local_user", signup)

    with pytest.raises(HTTPException):
        await _call_route(auth_routes.local_signup, request=request)

    validate.assert_awaited_once_with(token="bad-token", email="nova@example.com")
    signup.assert_not_awaited()


@pytest.mark.asyncio
async def test_local_signup_with_failed_invitation_deletes_created_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = FakeRequest(
        json_data={
            "email": "nova@example.com",
            "password": "super-secret",
            "name": "Nova",
            "invite_token": "invite-token",
        }
    )
    issued = _issued_session()
    validate = AsyncMock()
    signup = AsyncMock(return_value=issued)
    accept = AsyncMock(side_effect=HTTPException(status_code=400, detail="Invalid invitation"))
    cleanup = AsyncMock()

    monkeypatch.setattr(
        auth_routes, "_require_jwt_secret", lambda: "test-jwt-secret-key-for-api-tests"
    )
    monkeypatch.setattr(auth_routes, "is_setup_mode", AsyncMock(return_value=False))
    monkeypatch.setattr(auth_routes.config_module.settings, "public_signups_enabled", False)
    monkeypatch.setattr(
        auth_routes.organization_runtime, "validate_org_invitation_for_signup", validate
    )
    monkeypatch.setattr(auth_routes.organization_runtime, "accept_org_invitation", accept)
    monkeypatch.setattr(auth_routes, "signup_local_user", signup)
    monkeypatch.setattr(auth_routes, "delete_failed_local_signup_user", cleanup)

    with pytest.raises(HTTPException):
        await _call_route(auth_routes.local_signup, request=request)

    cleanup.assert_awaited_once_with(
        user_id=issued.user.id,
        organization_id=issued.organization.id,
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

    monkeypatch.setattr(
        auth_routes, "_require_jwt_secret", lambda: "test-jwt-secret-key-for-api-tests"
    )
    monkeypatch.setattr(auth_routes, "login_local_user", login)

    route_response = Response()
    response = await _call_route(auth_routes.local_login, request=request, response=route_response)

    assert response["access_token"] == issued.access_token
    cookies = _set_cookie_headers(route_response)
    assert any(auth_routes.ACCESS_TOKEN_COOKIE in cookie for cookie in cookies)
    assert any(auth_routes.REFRESH_TOKEN_COOKIE in cookie for cookie in cookies)
    login.assert_awaited_once_with(
        email="nova@example.com",
        password="super-secret",
        request=request,
        break_glass_reason=None,
    )


@pytest.mark.asyncio
async def test_local_login_accepts_invitation_for_existing_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = FakeRequest(
        json_data={
            "email": "nova@example.com",
            "password": "super-secret",
            "invite_token": "invite-token",
        }
    )
    issued = _issued_session()
    accepted = SimpleNamespace(
        access_token="team-access-token",
        refresh_token="team-refresh-token",
        refresh_expires=datetime.now(UTC) + timedelta(days=30),
        organization_id=uuid4(),
        invitation_id=uuid4(),
        organization_slug="shared-team",
        organization_name="Shared Team",
    )
    login = AsyncMock(return_value=issued)
    validate = AsyncMock()
    accept = AsyncMock(return_value=accepted)

    monkeypatch.setattr(
        auth_routes, "_require_jwt_secret", lambda: "test-jwt-secret-key-for-api-tests"
    )
    monkeypatch.setattr(auth_routes, "login_local_user", login)
    monkeypatch.setattr(
        auth_routes.organization_runtime, "validate_org_invitation_for_signup", validate
    )
    monkeypatch.setattr(auth_routes.organization_runtime, "accept_org_invitation", accept)

    response = await _call_route(auth_routes.local_login, request=request)

    validate.assert_awaited_once_with(token="invite-token", email="nova@example.com")
    accept.assert_awaited_once_with(
        token="invite-token",
        user=issued.user,
        request=request,
        existing_session_id=issued.session_id,
    )
    assert response["access_token"] == "team-access-token"
    assert response["organization"]["slug"] == "shared-team"


@pytest.mark.asyncio
async def test_local_login_with_failed_invitation_revokes_issued_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = FakeRequest(
        json_data={
            "email": "nova@example.com",
            "password": "super-secret",
            "invite_token": "invite-token",
        }
    )
    issued = _issued_session()
    login = AsyncMock(return_value=issued)
    validate = AsyncMock()
    accept = AsyncMock(side_effect=HTTPException(status_code=400, detail="Invalid invitation"))
    revoke = AsyncMock()

    monkeypatch.setattr(
        auth_routes, "_require_jwt_secret", lambda: "test-jwt-secret-key-for-api-tests"
    )
    monkeypatch.setattr(auth_routes, "login_local_user", login)
    monkeypatch.setattr(auth_routes, "revoke_access_session", revoke)
    monkeypatch.setattr(
        auth_routes.organization_runtime, "validate_org_invitation_for_signup", validate
    )
    monkeypatch.setattr(auth_routes.organization_runtime, "accept_org_invitation", accept)

    with pytest.raises(HTTPException):
        await _call_route(auth_routes.local_login, request=request)

    revoke.assert_awaited_once_with(issued.access_token)


@pytest.mark.asyncio
async def test_local_login_rejects_invalid_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    request = FakeRequest(
        json_data={
            "email": "nova@example.com",
            "password": "wrong",
        }
    )
    login = AsyncMock(return_value=None)

    monkeypatch.setattr(
        auth_routes, "_require_jwt_secret", lambda: "test-jwt-secret-key-for-api-tests"
    )
    monkeypatch.setattr(auth_routes, "login_local_user", login)

    with pytest.raises(HTTPException, match="Invalid credentials") as exc_info:
        await _call_route(auth_routes.local_login, request=request)

    assert exc_info.value.status_code == 401
    login.assert_awaited_once()


@pytest.mark.asyncio
async def test_local_login_respects_enterprise_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    request = FakeRequest(
        json_data={
            "email": "nova@example.com",
            "password": "super-secret",
        }
    )
    login = AsyncMock()

    monkeypatch.setattr(
        auth_routes, "_require_jwt_secret", lambda: "test-jwt-secret-key-for-api-tests"
    )
    monkeypatch.setattr(auth_routes.config_module.settings, "local_auth_enabled", False)
    monkeypatch.setattr(auth_routes, "is_setup_mode", AsyncMock(return_value=False))
    monkeypatch.setattr(auth_routes, "login_local_user", login)

    with pytest.raises(HTTPException) as exc_info:
        await _call_route(auth_routes.local_login, request=request)

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail["code"] == "local_auth_disabled"
    assert http_exception_payload(exc_info.value, "req_local_auth") == {
        "error": "local_auth_disabled",
        "message": "Local sign-in is disabled for this instance.",
        "request_id": "req_local_auth",
    }
    login.assert_not_awaited()


@pytest.mark.asyncio
async def test_local_login_rejects_break_glass_without_expiry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = FakeRequest(
        json_data={
            "email": "nova@example.com",
            "password": "super-secret",
        }
    )
    login = AsyncMock()

    monkeypatch.setattr(
        auth_routes, "_require_jwt_secret", lambda: "test-jwt-secret-key-for-api-tests"
    )
    monkeypatch.setattr(auth_routes.config_module.settings, "local_auth_enabled", False)
    monkeypatch.setattr(auth_routes.config_module.settings, "break_glass_enabled", True)
    monkeypatch.setattr(
        auth_routes.config_module.settings,
        "break_glass_allowed_ips",
        ["127.0.0.1/32"],
    )
    monkeypatch.setattr(auth_routes, "is_setup_mode", AsyncMock(return_value=False))
    monkeypatch.setattr(auth_routes, "login_local_user", login)

    with pytest.raises(HTTPException) as exc_info:
        await _call_route(auth_routes.local_login, request=request)

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail["code"] == "break_glass_expiry_required"
    login.assert_not_awaited()


@pytest.mark.asyncio
async def test_local_login_rejects_break_glass_window_over_four_hours(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = FakeRequest(
        json_data={
            "email": "nova@example.com",
            "password": "super-secret",
        }
    )
    login = AsyncMock()

    monkeypatch.setattr(
        auth_routes, "_require_jwt_secret", lambda: "test-jwt-secret-key-for-api-tests"
    )
    monkeypatch.setattr(auth_routes.config_module.settings, "local_auth_enabled", False)
    monkeypatch.setattr(auth_routes.config_module.settings, "break_glass_enabled", True)
    monkeypatch.setattr(
        auth_routes.config_module.settings,
        "break_glass_allowed_ips",
        ["127.0.0.1/32"],
    )
    monkeypatch.setattr(
        auth_routes.config_module.settings,
        "break_glass_expires_at",
        datetime.now(UTC) + timedelta(hours=4, minutes=1),
    )
    monkeypatch.setattr(auth_routes, "is_setup_mode", AsyncMock(return_value=False))
    monkeypatch.setattr(auth_routes, "login_local_user", login)

    with pytest.raises(HTTPException) as exc_info:
        await _call_route(auth_routes.local_login, request=request)

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail["code"] == "break_glass_expiry_too_long"
    login.assert_not_awaited()


@pytest.mark.asyncio
async def test_local_login_rejects_expired_break_glass(monkeypatch: pytest.MonkeyPatch) -> None:
    request = FakeRequest(
        json_data={
            "email": "nova@example.com",
            "password": "super-secret",
        }
    )
    login = AsyncMock()

    monkeypatch.setattr(
        auth_routes, "_require_jwt_secret", lambda: "test-jwt-secret-key-for-api-tests"
    )
    monkeypatch.setattr(auth_routes.config_module.settings, "local_auth_enabled", False)
    monkeypatch.setattr(auth_routes.config_module.settings, "break_glass_enabled", True)
    monkeypatch.setattr(
        auth_routes.config_module.settings,
        "break_glass_expires_at",
        datetime.now(UTC) - timedelta(minutes=1),
    )
    monkeypatch.setattr(auth_routes, "is_setup_mode", AsyncMock(return_value=False))
    monkeypatch.setattr(auth_routes, "login_local_user", login)

    with pytest.raises(HTTPException) as exc_info:
        await _call_route(auth_routes.local_login, request=request)

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail["code"] == "break_glass_expired"
    login.assert_not_awaited()


@pytest.mark.asyncio
async def test_local_login_rejects_break_glass_without_source_ips(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = FakeRequest(
        json_data={
            "email": "nova@example.com",
            "password": "super-secret",
        }
    )
    login = AsyncMock()

    monkeypatch.setattr(
        auth_routes, "_require_jwt_secret", lambda: "test-jwt-secret-key-for-api-tests"
    )
    monkeypatch.setattr(auth_routes.config_module.settings, "local_auth_enabled", False)
    monkeypatch.setattr(auth_routes.config_module.settings, "break_glass_enabled", True)
    monkeypatch.setattr(
        auth_routes.config_module.settings,
        "break_glass_expires_at",
        datetime.now(UTC) + timedelta(hours=3),
    )
    monkeypatch.setattr(auth_routes, "is_setup_mode", AsyncMock(return_value=False))
    monkeypatch.setattr(auth_routes, "login_local_user", login)

    with pytest.raises(HTTPException) as exc_info:
        await _call_route(auth_routes.local_login, request=request)

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail["code"] == "break_glass_ip_required"
    login.assert_not_awaited()


@pytest.mark.asyncio
async def test_local_login_rejects_break_glass_source_ip(monkeypatch: pytest.MonkeyPatch) -> None:
    request = FakeRequest(
        json_data={
            "email": "nova@example.com",
            "password": "super-secret",
        }
    )
    request.client.host = "198.51.100.10"
    login = AsyncMock()

    monkeypatch.setattr(
        auth_routes, "_require_jwt_secret", lambda: "test-jwt-secret-key-for-api-tests"
    )
    monkeypatch.setattr(auth_routes.config_module.settings, "local_auth_enabled", False)
    monkeypatch.setattr(auth_routes.config_module.settings, "break_glass_enabled", True)
    monkeypatch.setattr(
        auth_routes.config_module.settings,
        "break_glass_allowed_ips",
        ["203.0.113.0/24"],
    )
    monkeypatch.setattr(
        auth_routes.config_module.settings,
        "break_glass_expires_at",
        datetime.now(UTC) + timedelta(hours=3),
    )
    monkeypatch.setattr(auth_routes, "is_setup_mode", AsyncMock(return_value=False))
    monkeypatch.setattr(auth_routes, "login_local_user", login)

    with pytest.raises(HTTPException) as exc_info:
        await _call_route(auth_routes.local_login, request=request)

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail["code"] == "break_glass_ip_denied"
    login.assert_not_awaited()


@pytest.mark.asyncio
async def test_local_login_rejects_break_glass_without_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = FakeRequest(
        json_data={
            "email": "nova@example.com",
            "password": "super-secret",
        }
    )
    request.client.host = "203.0.113.10"
    login = AsyncMock()

    monkeypatch.setattr(
        auth_routes, "_require_jwt_secret", lambda: "test-jwt-secret-key-for-api-tests"
    )
    monkeypatch.setattr(auth_routes.config_module.settings, "local_auth_enabled", False)
    monkeypatch.setattr(auth_routes.config_module.settings, "break_glass_enabled", True)
    monkeypatch.setattr(
        auth_routes.config_module.settings,
        "break_glass_allowed_ips",
        ["203.0.113.0/24"],
    )
    monkeypatch.setattr(
        auth_routes.config_module.settings,
        "break_glass_expires_at",
        datetime.now(UTC) + timedelta(hours=3),
    )
    monkeypatch.setattr(auth_routes, "is_setup_mode", AsyncMock(return_value=False))
    monkeypatch.setattr(auth_routes, "login_local_user", login)

    with pytest.raises(HTTPException) as exc_info:
        await _call_route(auth_routes.local_login, request=request)

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail["code"] == "break_glass_reason_required"
    login.assert_not_awaited()


@pytest.mark.asyncio
async def test_local_login_rejects_blank_break_glass_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = FakeRequest(
        json_data={
            "email": "nova@example.com",
            "password": "super-secret",
            "break_glass_reason": " ",
        }
    )
    request.client.host = "203.0.113.10"
    login = AsyncMock()

    monkeypatch.setattr(
        auth_routes, "_require_jwt_secret", lambda: "test-jwt-secret-key-for-api-tests"
    )
    monkeypatch.setattr(auth_routes.config_module.settings, "local_auth_enabled", False)
    monkeypatch.setattr(auth_routes.config_module.settings, "break_glass_enabled", True)
    monkeypatch.setattr(
        auth_routes.config_module.settings,
        "break_glass_allowed_ips",
        ["203.0.113.0/24"],
    )
    monkeypatch.setattr(
        auth_routes.config_module.settings,
        "break_glass_expires_at",
        datetime.now(UTC) + timedelta(hours=3),
    )
    monkeypatch.setattr(auth_routes, "is_setup_mode", AsyncMock(return_value=False))
    monkeypatch.setattr(auth_routes, "login_local_user", login)

    with pytest.raises(HTTPException) as exc_info:
        await _call_route(auth_routes.local_login, request=request)

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail["code"] == "break_glass_reason_required"
    login.assert_not_awaited()


@pytest.mark.asyncio
async def test_local_login_rejects_long_break_glass_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = FakeRequest(
        json_data={
            "email": "nova@example.com",
            "password": "super-secret",
            "break_glass_reason": "x" * 513,
        }
    )
    request.client.host = "203.0.113.10"
    login = AsyncMock()

    monkeypatch.setattr(
        auth_routes, "_require_jwt_secret", lambda: "test-jwt-secret-key-for-api-tests"
    )
    monkeypatch.setattr(auth_routes.config_module.settings, "local_auth_enabled", False)
    monkeypatch.setattr(auth_routes.config_module.settings, "break_glass_enabled", True)
    monkeypatch.setattr(
        auth_routes.config_module.settings,
        "break_glass_allowed_ips",
        ["203.0.113.0/24"],
    )
    monkeypatch.setattr(
        auth_routes.config_module.settings,
        "break_glass_expires_at",
        datetime.now(UTC) + timedelta(hours=3),
    )
    monkeypatch.setattr(auth_routes, "is_setup_mode", AsyncMock(return_value=False))
    monkeypatch.setattr(auth_routes, "login_local_user", login)

    with pytest.raises(HTTPException) as exc_info:
        await _call_route(auth_routes.local_login, request=request)

    assert exc_info.value.status_code == 422
    assert exc_info.value.detail["code"] == "break_glass_reason_too_long"
    login.assert_not_awaited()


@pytest.mark.asyncio
async def test_local_login_allows_break_glass_source_ip(monkeypatch: pytest.MonkeyPatch) -> None:
    request = FakeRequest(
        json_data={
            "email": "nova@example.com",
            "password": "super-secret",
            "break_glass_reason": "INC-123 IdP outage",
        }
    )
    request.client.host = "203.0.113.10"
    issued = _issued_session()
    login = AsyncMock(return_value=issued)

    monkeypatch.setattr(
        auth_routes, "_require_jwt_secret", lambda: "test-jwt-secret-key-for-api-tests"
    )
    monkeypatch.setattr(auth_routes.config_module.settings, "local_auth_enabled", False)
    monkeypatch.setattr(auth_routes.config_module.settings, "break_glass_enabled", True)
    monkeypatch.setattr(
        auth_routes.config_module.settings,
        "break_glass_allowed_ips",
        ["203.0.113.0/24"],
    )
    monkeypatch.setattr(
        auth_routes.config_module.settings,
        "break_glass_expires_at",
        datetime.now(UTC) + timedelta(hours=3),
    )
    monkeypatch.setattr(auth_routes, "is_setup_mode", AsyncMock(return_value=False))
    monkeypatch.setattr(auth_routes, "login_local_user", login)

    response = await _call_route(auth_routes.local_login, request=request)

    assert response["access_token"] == issued.access_token
    login.assert_awaited_once_with(
        email="nova@example.com",
        password="super-secret",
        request=request,
        break_glass_reason="INC-123 IdP outage",
    )


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

    monkeypatch.setattr(
        auth_routes, "_require_jwt_secret", lambda: "test-jwt-secret-key-for-api-tests"
    )
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

    monkeypatch.setattr(
        auth_routes, "_require_jwt_secret", lambda: "test-jwt-secret-key-for-api-tests"
    )
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

    monkeypatch.setattr(
        auth_routes, "_require_jwt_secret", lambda: "test-jwt-secret-key-for-api-tests"
    )
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
    login = AsyncMock(
        return_value=SimpleNamespace(
            access_token="access-token",
            refresh_token="refresh-token",
            refresh_expires=datetime.now(UTC) + timedelta(days=30),
        )
    )

    monkeypatch.setattr(
        auth_routes, "_require_jwt_secret", lambda: "test-jwt-secret-key-for-api-tests"
    )
    monkeypatch.setattr(auth_routes, "login_device_browser_user", login)

    response = await _call_route(auth_routes.device_verify_post, request=request)

    assert response.status_code == 302
    set_cookie_headers = [
        value.decode() for name, value in response.raw_headers if name.lower() == b"set-cookie"
    ]
    assert any("sibyl_access_token=access-token" in header for header in set_cookie_headers)
    assert any("sibyl_refresh_token=refresh-token" in header for header in set_cookie_headers)
    login.assert_awaited_once_with(
        email="nova@example.com",
        password="super-secret",
        request=request,
        break_glass_reason=None,
    )


@pytest.mark.asyncio
async def test_device_verify_post_login_accepts_break_glass_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = FakeRequest(
        form_data={
            "action": "login",
            "user_code": "ABCD-EFGH",
            "email": "nova@example.com",
            "password": "super-secret",
            "break_glass_reason": "INC-123 CLI approval",
        }
    )
    request.client.host = "203.0.113.10"
    login = AsyncMock(
        return_value=SimpleNamespace(
            access_token="access-token",
            refresh_token="refresh-token",
            refresh_expires=datetime.now(UTC) + timedelta(days=30),
        )
    )

    monkeypatch.setattr(
        auth_routes, "_require_jwt_secret", lambda: "test-jwt-secret-key-for-api-tests"
    )
    monkeypatch.setattr(auth_routes.config_module.settings, "local_auth_enabled", False)
    monkeypatch.setattr(auth_routes.config_module.settings, "break_glass_enabled", True)
    monkeypatch.setattr(
        auth_routes.config_module.settings,
        "break_glass_allowed_ips",
        ["203.0.113.0/24"],
    )
    monkeypatch.setattr(
        auth_routes.config_module.settings,
        "break_glass_expires_at",
        datetime.now(UTC) + timedelta(hours=3),
    )
    monkeypatch.setattr(auth_routes, "is_setup_mode", AsyncMock(return_value=False))
    monkeypatch.setattr(auth_routes, "login_device_browser_user", login)

    response = await _call_route(auth_routes.device_verify_post, request=request)

    assert response.status_code == 302
    login.assert_awaited_once_with(
        email="nova@example.com",
        password="super-secret",
        request=request,
        break_glass_reason="INC-123 CLI approval",
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

    monkeypatch.setattr(
        auth_routes, "_require_jwt_secret", lambda: "test-jwt-secret-key-for-api-tests"
    )
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

    monkeypatch.setattr(
        auth_routes, "_require_jwt_secret", lambda: "test-jwt-secret-key-for-api-tests"
    )
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

    monkeypatch.setattr(
        auth_routes, "_require_jwt_secret", lambda: "test-jwt-secret-key-for-api-tests"
    )
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

    monkeypatch.setattr(
        auth_routes, "_require_jwt_secret", lambda: "test-jwt-secret-key-for-api-tests"
    )
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

    monkeypatch.setattr(
        auth_routes, "_require_jwt_secret", lambda: "test-jwt-secret-key-for-api-tests"
    )
    monkeypatch.setattr(
        auth_routes,
        "verify_refresh_token",
        lambda _: {"sub": str(user_id), "org": "not-a-uuid"},
    )
    monkeypatch.setattr(auth_routes, "rotate_refresh_exchange", AsyncMock())

    response = await _call_route(auth_routes.refresh_tokens, request=request)
    set_cookie_headers = [
        value.decode() for name, value in response.raw_headers if name.lower() == b"set-cookie"
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
    memory_space_id = uuid4()
    record = SimpleNamespace(
        id=uuid4(),
        name="CLI",
        key_prefix="sk_live_abcd",
        scopes=["mcp"],
        project_ids=["project-alpha"],
        memory_space_ids=[str(memory_space_id)],
        expires_at=None,
    )
    create_key = AsyncMock(return_value=(record, "raw-secret"))
    monkeypatch.setattr(auth_routes, "create_api_key_for_user", create_key)

    response = await auth_routes.create_api_key(
        request=request,
        body=auth_routes.ApiKeyCreateRequest(
            name="CLI",
            project_ids=["project-alpha"],
            memory_space_ids=[memory_space_id],
        ),
        ctx=ctx,
        _admin=None,
    )

    assert response["api_key"] == "raw-secret"
    assert response["project_ids"] == ["project-alpha"]
    assert response["memory_space_ids"] == [str(memory_space_id)]
    create_key.assert_awaited_once_with(
        organization_id=ctx.organization.id,
        user_id=ctx.user.id,
        name="CLI",
        live=True,
        scopes=["mcp"],
        project_ids=["project-alpha"],
        memory_space_ids=[memory_space_id],
        expires_at=None,
        request=request,
    )


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
