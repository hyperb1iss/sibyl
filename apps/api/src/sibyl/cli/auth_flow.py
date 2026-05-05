"""Replay the auth surface used as the SurrealDB cutover acceptance gate."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse

import httpx


class AuthFlowError(RuntimeError):
    """Raised when an auth flow step does not satisfy the acceptance contract."""


@dataclass(frozen=True, slots=True)
class AuthFlowResult:
    primary_email: str
    secondary_email: str
    organization_slug: str
    steps: tuple[str, ...]


JsonObject = dict[str, object]


@dataclass(frozen=True, slots=True)
class _PrimarySession:
    access_token: str
    refresh_token: str
    organization_slug: str


@dataclass(frozen=True, slots=True)
class _SecondarySession:
    access_token: str
    organization_slug: str


async def replay_auth_flow(
    *,
    base_url: str,
    email: str,
    password: str,
    name: str = "Sibyl Auth Flow",
    request_timeout: float = 15.0,
    transport: httpx.AsyncBaseTransport | None = None,
) -> AuthFlowResult:
    steps: list[str] = []
    secondary_email = _secondary_email(email)

    async with httpx.AsyncClient(
        base_url=base_url.rstrip("/"),
        follow_redirects=False,
        timeout=request_timeout,
        transport=transport,
    ) as client:
        primary = await _signup_login_refresh_primary(
            client=client,
            email=email,
            password=password,
            name=name,
            steps=steps,
        )
        await _exercise_api_key(client=client, primary=primary, steps=steps)
        secondary = await _signup_secondary_user(
            client=client,
            email=secondary_email,
            password=password,
            name=f"{name} Member",
            steps=steps,
        )
        await _invite_accept_and_switch_org(
            client=client,
            primary=primary,
            secondary=secondary,
            secondary_email=secondary_email,
            steps=steps,
        )
        await _exercise_device_auth(client=client, primary=primary, steps=steps)
        primary = await _exercise_password_paths(
            client=client,
            primary=primary,
            email=email,
            password=password,
            steps=steps,
        )
        await _list_sessions_and_logout(client=client, primary=primary, steps=steps)

    return AuthFlowResult(
        primary_email=email,
        secondary_email=secondary_email,
        organization_slug=primary.organization_slug,
        steps=tuple(steps),
    )


async def _signup_login_refresh_primary(
    *,
    client: httpx.AsyncClient,
    email: str,
    password: str,
    name: str,
    steps: list[str],
) -> _PrimarySession:
    signup = await _post_json(
        client,
        "/api/auth/local/signup",
        {"email": email, "password": password, "name": name},
        expected_status=201,
        step="signup primary user",
    )
    steps.append("signup_primary_user")
    _required_string(signup, "access_token", "signup primary user")
    refresh_token = _required_string(signup, "refresh_token", "signup primary user")
    primary_org = _required_object(signup, "organization", "signup primary user")
    primary_slug = _required_string(primary_org, "slug", "signup primary user")

    login = await _post_json(
        client,
        "/api/auth/local/login",
        {"email": email, "password": password},
        expected_status=200,
        step="login primary user",
    )
    steps.append("login_primary_user")
    _required_string(login, "access_token", "login primary user")
    refresh_token = _required_string(login, "refresh_token", "login primary user")

    refresh = await _post_json(
        client,
        "/api/auth/refresh",
        {"refresh_token": refresh_token},
        expected_status=200,
        step="refresh tokens",
    )
    steps.append("refresh_tokens")
    return _PrimarySession(
        access_token=_required_string(refresh, "access_token", "refresh tokens"),
        refresh_token=_required_string(refresh, "refresh_token", "refresh tokens"),
        organization_slug=primary_slug,
    )


async def _exercise_api_key(
    *,
    client: httpx.AsyncClient,
    primary: _PrimarySession,
    steps: list[str],
) -> None:
    primary_headers = _bearer_headers(primary.access_token)
    api_key = await _post_json(
        client,
        "/api/auth/api-keys",
        {
            "name": "SurrealDB cutover auth flow",
            "live": False,
            "scopes": ["mcp", "api:read", "api:write"],
            "expires_days": 1,
        },
        headers=primary_headers,
        expected_status=200,
        step="create api key",
    )
    steps.append("create_api_key")
    api_key_id = _required_string(api_key, "id", "create api key")
    raw_api_key = _required_string(api_key, "api_key", "create api key")

    await _get_json(
        client,
        "/api/auth/me",
        headers=_bearer_headers(raw_api_key),
        expected_status=200,
        step="authenticate api key",
    )
    steps.append("authenticate_api_key")

    await _post_json(
        client,
        f"/api/auth/api-keys/{api_key_id}/revoke",
        {},
        headers=primary_headers,
        expected_status=200,
        step="revoke api key",
    )
    revoked = await client.get("/api/auth/me", headers=_bearer_headers(raw_api_key))
    _expect_status_in(revoked, {401, 403}, "verify revoked api key")
    steps.append("revoke_api_key")


async def _signup_secondary_user(
    *,
    client: httpx.AsyncClient,
    email: str,
    password: str,
    name: str,
    steps: list[str],
) -> _SecondarySession:
    signup = await _post_json(
        client,
        "/api/auth/local/signup",
        {"email": email, "password": password, "name": name},
        expected_status=201,
        step="signup invited user",
    )
    steps.append("signup_invited_user")
    secondary_org = _required_object(signup, "organization", "signup invited user")
    return _SecondarySession(
        access_token=_required_string(signup, "access_token", "signup invited user"),
        organization_slug=_required_string(secondary_org, "slug", "signup invited user"),
    )


async def _invite_accept_and_switch_org(
    *,
    client: httpx.AsyncClient,
    primary: _PrimarySession,
    secondary: _SecondarySession,
    secondary_email: str,
    steps: list[str],
) -> None:
    invitation = await _post_json(
        client,
        f"/api/orgs/{primary.organization_slug}/invitations",
        {"email": secondary_email, "role": "member", "expires_days": 1},
        headers=_bearer_headers(primary.access_token),
        expected_status=200,
        step="invite user to org",
    )
    invite_payload = _required_object(invitation, "invitation", "invite user to org")
    accept_url = _required_string(invite_payload, "accept_url", "invite user to org")
    invite_token = _invitation_token(accept_url)
    accepted = await _post_json(
        client,
        f"/api/invitations/{invite_token}/accept",
        {},
        headers=_bearer_headers(secondary.access_token),
        expected_status=200,
        step="accept org invitation",
    )
    steps.append("invite_and_accept_user")
    secondary_access = _required_string(accepted, "access_token", "accept org invitation")

    switched = await _post_json(
        client,
        f"/api/orgs/{secondary.organization_slug}/switch",
        {},
        headers=_bearer_headers(secondary_access),
        expected_status=200,
        step="switch invited user org",
    )
    secondary_access = _required_string(switched, "access_token", "switch invited user org")
    await _post_json(
        client,
        f"/api/orgs/{primary.organization_slug}/switch",
        {},
        headers=_bearer_headers(secondary_access),
        expected_status=200,
        step="switch invited user back",
    )
    steps.append("switch_active_org")


async def _exercise_device_auth(
    *,
    client: httpx.AsyncClient,
    primary: _PrimarySession,
    steps: list[str],
) -> None:
    device_start = await _post_json(
        client,
        "/api/auth/device",
        {
            "client_name": "SurrealDB cutover auth flow",
            "scope": "mcp",
            "interval": 1,
            "expires_in": 600,
        },
        expected_status=200,
        step="start device auth",
    )
    device_code = _required_string(device_start, "device_code", "start device auth")
    user_code = _required_string(device_start, "user_code", "start device auth")
    pending = await client.post("/api/auth/device/token", json={"device_code": device_code})
    _expect_status(pending, 400, "poll pending device auth")
    pending_payload = _json_object(pending, "poll pending device auth")
    if pending_payload.get("error") != "authorization_pending":
        raise AuthFlowError("device auth did not report authorization_pending")

    approved = await client.post(
        "/api/auth/device/verify",
        data={"action": "approve", "user_code": user_code},
        headers=_bearer_headers(primary.access_token),
    )
    _expect_status(approved, 200, "approve device auth")
    device_token = await _post_json(
        client,
        "/api/auth/device/token",
        {"device_code": device_code},
        expected_status=200,
        step="exchange device auth",
    )
    _required_string(device_token, "access_token", "exchange device auth")
    steps.append("device_auth_flow")


async def _exercise_password_paths(
    *,
    client: httpx.AsyncClient,
    primary: _PrimarySession,
    email: str,
    password: str,
    steps: list[str],
) -> _PrimarySession:
    new_password = f"{password}-rotated"
    await _post_no_content(
        client,
        "/api/users/me/password",
        {"current_password": password, "new_password": new_password},
        headers=_bearer_headers(primary.access_token),
        step="change password",
    )
    login = await _post_json(
        client,
        "/api/auth/local/login",
        {"email": email, "password": new_password},
        expected_status=200,
        step="login after password change",
    )
    steps.append("change_password")

    await _post_json(
        client,
        "/api/users/password/reset",
        {"email": email},
        expected_status=202,
        step="request password reset",
    )
    steps.append("request_password_reset")
    return _PrimarySession(
        access_token=_required_string(login, "access_token", "login after password change"),
        refresh_token=_required_string(login, "refresh_token", "login after password change"),
        organization_slug=primary.organization_slug,
    )


async def _list_sessions_and_logout(
    *,
    client: httpx.AsyncClient,
    primary: _PrimarySession,
    steps: list[str],
) -> None:
    primary_headers = _bearer_headers(primary.access_token)
    sessions = await client.get("/api/users/me/sessions", headers=primary_headers)
    _expect_status(sessions, 200, "list sessions")
    steps.append("list_user_sessions")

    logout = await client.post("/api/auth/logout", headers=primary_headers)
    _expect_status(logout, 204, "logout")
    rejected = await client.get("/api/auth/me", headers=primary_headers)
    _expect_status_in(rejected, {401, 403}, "verify logged out token")
    steps.append("logout_rejects_access_token")


async def _post_json(
    client: httpx.AsyncClient,
    path: str,
    body: JsonObject,
    *,
    expected_status: int,
    step: str,
    headers: dict[str, str] | None = None,
) -> JsonObject:
    response = await client.post(path, json=body, headers=headers)
    _expect_status(response, expected_status, step)
    return _json_object(response, step)


async def _post_no_content(
    client: httpx.AsyncClient,
    path: str,
    body: JsonObject,
    *,
    headers: dict[str, str],
    step: str,
) -> None:
    response = await client.post(path, json=body, headers=headers)
    _expect_status(response, 204, step)


async def _get_json(
    client: httpx.AsyncClient,
    path: str,
    *,
    expected_status: int,
    step: str,
    headers: dict[str, str] | None = None,
) -> JsonObject:
    response = await client.get(path, headers=headers)
    _expect_status(response, expected_status, step)
    return _json_object(response, step)


def _bearer_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _expect_status(response: httpx.Response, expected: int, step: str) -> None:
    if response.status_code != expected:
        raise AuthFlowError(
            f"{step} failed with HTTP {response.status_code}: {_response_excerpt(response)}"
        )


def _expect_status_in(response: httpx.Response, expected: set[int], step: str) -> None:
    if response.status_code not in expected:
        choices = ", ".join(str(status) for status in sorted(expected))
        raise AuthFlowError(
            f"{step} expected HTTP {choices}, got {response.status_code}: "
            f"{_response_excerpt(response)}"
        )


def _json_object(response: httpx.Response, step: str) -> JsonObject:
    try:
        payload: object = response.json()
    except ValueError as exc:
        raise AuthFlowError(f"{step} returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise AuthFlowError(f"{step} returned JSON {type(payload).__name__}, expected object")
    return {str(key): value for key, value in payload.items()}


def _required_string(payload: JsonObject, key: str, step: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise AuthFlowError(f"{step} did not return {key}")
    return value


def _required_object(payload: JsonObject, key: str, step: str) -> JsonObject:
    value = payload.get(key)
    if not isinstance(value, dict):
        raise AuthFlowError(f"{step} did not return {key}")
    return {str(item_key): item_value for item_key, item_value in value.items()}


def _secondary_email(email: str) -> str:
    local, separator, domain = email.partition("@")
    if not separator or not local or not domain:
        return "auth-flow-member@sibyl.dev"
    return f"{local}+member@{domain}"


def _invitation_token(accept_url: str) -> str:
    path = urlparse(accept_url).path
    marker = "/invitations/"
    if marker not in path:
        raise AuthFlowError("invitation response did not include an accept token URL")
    token = path.split(marker, 1)[1].split("/", 1)[0]
    if not token:
        raise AuthFlowError("invitation accept token was empty")
    return token


def _response_excerpt(response: httpx.Response) -> str:
    text = response.text.strip().replace("\n", " ")
    if len(text) > 400:
        return text[:397] + "..."
    return text or "<empty response>"
