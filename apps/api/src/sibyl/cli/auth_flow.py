"""Replay the auth surface used as the SurrealDB cutover acceptance gate."""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import re
from dataclasses import dataclass
from pathlib import Path
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
    token_claims: tuple[AuthFlowTokenClaims, ...]


@dataclass(frozen=True, slots=True)
class AuthFlowTokenClaims:
    step: str
    field_name: str
    typ: str
    has_sub: bool
    has_org: bool
    has_sid: bool
    has_jti: bool


JsonObject = dict[str, object]
_RESET_TOKEN_RE = re.compile(r"/reset-password\?token=([A-Za-z0-9_-]+)")


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
    email_outbox_path: Path | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> AuthFlowResult:
    steps: list[str] = []
    token_claims: list[AuthFlowTokenClaims] = []
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
            token_claims=token_claims,
        )
        await _exercise_api_key(client=client, primary=primary, steps=steps)
        secondary = await _signup_secondary_user(
            client=client,
            email=secondary_email,
            password=password,
            name=f"{name} Member",
            steps=steps,
            token_claims=token_claims,
        )
        await _invite_accept_and_switch_org(
            client=client,
            primary=primary,
            secondary=secondary,
            secondary_email=secondary_email,
            steps=steps,
            token_claims=token_claims,
        )
        await _exercise_device_auth(
            client=client,
            primary=primary,
            steps=steps,
            token_claims=token_claims,
        )
        primary = await _exercise_password_paths(
            client=client,
            primary=primary,
            email=email,
            password=password,
            email_outbox_path=email_outbox_path,
            steps=steps,
            token_claims=token_claims,
        )
        await _list_sessions_and_logout(client=client, primary=primary, steps=steps)

    return AuthFlowResult(
        primary_email=email,
        secondary_email=secondary_email,
        organization_slug=primary.organization_slug,
        steps=tuple(steps),
        token_claims=tuple(token_claims),
    )


def compare_auth_flow_results(
    *,
    left_label: str,
    left: AuthFlowResult,
    right_label: str,
    right: AuthFlowResult,
) -> None:
    mismatches: list[str] = []
    if left.steps != right.steps:
        mismatches.append(
            f"step sequence differed: {left_label}={left.steps!r} {right_label}={right.steps!r}"
        )

    left_claims = tuple(_claim_fingerprint(claims) for claims in left.token_claims)
    right_claims = tuple(_claim_fingerprint(claims) for claims in right.token_claims)
    if left_claims != right_claims:
        mismatches.append(
            f"JWT claim shape differed: {left_label}={left_claims!r} {right_label}={right_claims!r}"
        )

    if mismatches:
        raise AuthFlowError("; ".join(mismatches))


async def _signup_login_refresh_primary(
    *,
    client: httpx.AsyncClient,
    email: str,
    password: str,
    name: str,
    steps: list[str],
    token_claims: list[AuthFlowTokenClaims],
) -> _PrimarySession:
    signup = await _post_json(
        client,
        "/api/auth/local/signup",
        {"email": email, "password": password, "name": name},
        expected_status=201,
        step="signup primary user",
    )
    steps.append("signup_primary_user")
    _, refresh_token = _record_response_tokens(token_claims, signup, step="signup primary user")
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
    _, refresh_token = _record_response_tokens(token_claims, login, step="login primary user")

    refresh = await _post_json(
        client,
        "/api/auth/refresh",
        {"refresh_token": refresh_token},
        expected_status=200,
        step="refresh tokens",
    )
    steps.append("refresh_tokens")
    refresh_access, refresh_token = _record_response_tokens(
        token_claims, refresh, step="refresh tokens"
    )
    return _PrimarySession(
        access_token=refresh_access,
        refresh_token=refresh_token,
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
    token_claims: list[AuthFlowTokenClaims],
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
    access_token, _ = _record_response_tokens(token_claims, signup, step="signup invited user")
    return _SecondarySession(
        access_token=access_token,
        organization_slug=_required_string(secondary_org, "slug", "signup invited user"),
    )


async def _invite_accept_and_switch_org(
    *,
    client: httpx.AsyncClient,
    primary: _PrimarySession,
    secondary: _SecondarySession,
    secondary_email: str,
    steps: list[str],
    token_claims: list[AuthFlowTokenClaims],
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
    secondary_access, _ = _record_response_tokens(
        token_claims, accepted, step="accept org invitation"
    )

    switched = await _post_json(
        client,
        f"/api/orgs/{secondary.organization_slug}/switch",
        {},
        headers=_bearer_headers(secondary_access),
        expected_status=200,
        step="switch invited user org",
    )
    secondary_access, _ = _record_response_tokens(
        token_claims, switched, step="switch invited user org"
    )
    switched_back = await _post_json(
        client,
        f"/api/orgs/{primary.organization_slug}/switch",
        {},
        headers=_bearer_headers(secondary_access),
        expected_status=200,
        step="switch invited user back",
    )
    _record_response_tokens(token_claims, switched_back, step="switch invited user back")
    steps.append("switch_active_org")


async def _exercise_device_auth(
    *,
    client: httpx.AsyncClient,
    primary: _PrimarySession,
    steps: list[str],
    token_claims: list[AuthFlowTokenClaims],
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
    _record_response_tokens(token_claims, device_token, step="exchange device auth")
    steps.append("device_auth_flow")


async def _exercise_password_paths(
    *,
    client: httpx.AsyncClient,
    primary: _PrimarySession,
    email: str,
    password: str,
    email_outbox_path: Path | None,
    steps: list[str],
    token_claims: list[AuthFlowTokenClaims],
) -> _PrimarySession:
    new_password = f"{password}-rotated"
    reset_password = f"{password}-reset"
    await _post_no_content(
        client,
        "/api/users/me/password",
        {"current_password": password, "new_password": new_password},
        headers=_bearer_headers(primary.access_token),
        step="change password",
    )
    changed_login = await _post_json(
        client,
        "/api/auth/local/login",
        {"email": email, "password": new_password},
        expected_status=200,
        step="login after password change",
    )
    _record_response_tokens(token_claims, changed_login, step="login after password change")
    steps.append("change_password")

    outbox_offset = _outbox_offset(email_outbox_path)
    await _post_json(
        client,
        "/api/users/password/reset",
        {"email": email},
        expected_status=202,
        step="request password reset",
    )
    reset_token = await _read_reset_token(
        email=email,
        email_outbox_path=email_outbox_path,
        offset=outbox_offset,
    )
    await _post_no_content(
        client,
        "/api/users/password/reset/confirm",
        {"token": reset_token, "new_password": reset_password},
        step="confirm password reset",
    )
    reset_login = await _post_json(
        client,
        "/api/auth/local/login",
        {"email": email, "password": reset_password},
        expected_status=200,
        step="login after password reset",
    )
    reset_access, reset_refresh = _record_response_tokens(
        token_claims, reset_login, step="login after password reset"
    )
    steps.append("password_reset_request_and_consume")
    return _PrimarySession(
        access_token=reset_access,
        refresh_token=reset_refresh,
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
    step: str,
    headers: dict[str, str] | None = None,
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


def _record_token_claims(
    token_claims: list[AuthFlowTokenClaims],
    *,
    step: str,
    field_name: str,
    token: str,
    expected_type: str,
) -> None:
    claims = _decode_jwt_payload(token, step=step, field_name=field_name)
    typ = _required_claim(claims, "typ", step=step, field_name=field_name)
    if typ != expected_type:
        raise AuthFlowError(f"{step} {field_name} had typ={typ!r}, expected {expected_type!r}")
    has_sub = _has_claim(claims, "sub")
    has_org = _has_claim(claims, "org")
    has_sid = _has_claim(claims, "sid")
    has_jti = _has_claim(claims, "jti")
    missing = [
        claim
        for claim, present in (
            ("sub", has_sub),
            ("org", has_org),
            ("sid", has_sid),
            ("jti", has_jti or expected_type != "refresh"),
        )
        if not present
    ]
    if missing:
        joined = ", ".join(missing)
        raise AuthFlowError(f"{step} {field_name} missing required JWT claims: {joined}")
    token_claims.append(
        AuthFlowTokenClaims(
            step=step,
            field_name=field_name,
            typ=typ,
            has_sub=has_sub,
            has_org=has_org,
            has_sid=has_sid,
            has_jti=has_jti,
        )
    )


def _record_response_tokens(
    token_claims: list[AuthFlowTokenClaims],
    payload: JsonObject,
    *,
    step: str,
) -> tuple[str, str]:
    access_token = _required_string(payload, "access_token", step)
    refresh_token = _required_string(payload, "refresh_token", step)
    _record_token_claims(
        token_claims,
        step=step,
        field_name="access_token",
        token=access_token,
        expected_type="access",
    )
    _record_token_claims(
        token_claims,
        step=step,
        field_name="refresh_token",
        token=refresh_token,
        expected_type="refresh",
    )
    return access_token, refresh_token


def _claim_fingerprint(claims: AuthFlowTokenClaims) -> tuple[str, str, str, bool, bool, bool, bool]:
    return (
        claims.step,
        claims.field_name,
        claims.typ,
        claims.has_sub,
        claims.has_org,
        claims.has_sid,
        claims.has_jti,
    )


def _decode_jwt_payload(token: str, *, step: str, field_name: str) -> JsonObject:
    parts = token.split(".")
    if len(parts) < 2:
        raise AuthFlowError(f"{step} {field_name} was not a JWT")
    payload_segment = parts[1]
    padded = payload_segment + ("=" * (-len(payload_segment) % 4))
    try:
        decoded = base64.urlsafe_b64decode(padded.encode("ascii"))
        payload: object = json.loads(decoded.decode("utf-8"))
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AuthFlowError(f"{step} {field_name} had an invalid JWT payload") from exc
    if not isinstance(payload, dict):
        raise AuthFlowError(f"{step} {field_name} JWT payload was not an object")
    return {str(key): value for key, value in payload.items()}


def _required_claim(claims: JsonObject, key: str, *, step: str, field_name: str) -> str:
    value = claims.get(key)
    if not isinstance(value, str) or not value:
        raise AuthFlowError(f"{step} {field_name} missing required JWT claim: {key}")
    return value


def _has_claim(claims: JsonObject, key: str) -> bool:
    value = claims.get(key)
    return isinstance(value, str) and bool(value)


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


def _outbox_offset(email_outbox_path: Path | None) -> int:
    if email_outbox_path is None:
        raise AuthFlowError("password reset consume requires --email-outbox-path")
    path = email_outbox_path.expanduser()
    if not path.exists():
        return 0
    return path.stat().st_size


async def _read_reset_token(
    *,
    email: str,
    email_outbox_path: Path | None,
    offset: int,
) -> str:
    if email_outbox_path is None:
        raise AuthFlowError("password reset consume requires --email-outbox-path")
    path = email_outbox_path.expanduser()
    for _ in range(50):
        token = _find_reset_token(email=email, path=path, offset=offset)
        if token is not None:
            return token
        await asyncio.sleep(0.1)
    raise AuthFlowError(f"password reset token was not written to {path}")


def _find_reset_token(*, email: str, path: Path, offset: int) -> str | None:
    if not path.exists():
        return None
    with path.open(encoding="utf-8") as file:
        file.seek(offset)
        for line in file:
            token = _reset_token_from_outbox_line(email=email, line=line)
            if token is not None:
                return token
    return None


def _reset_token_from_outbox_line(*, email: str, line: str) -> str | None:
    try:
        payload: object = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    recipients = payload.get("to")
    if not isinstance(recipients, list) or email not in recipients:
        return None
    for key in ("html", "text"):
        value = payload.get(key)
        if isinstance(value, str):
            match = _RESET_TOKEN_RE.search(value)
            if match:
                return match.group(1)
    return None


def _response_excerpt(response: httpx.Response) -> str:
    text = response.text.strip().replace("\n", " ")
    if len(text) > 400:
        return text[:397] + "..."
    return text or "<empty response>"
