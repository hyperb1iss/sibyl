"""Authentication endpoints."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from ipaddress import ip_address, ip_network
from typing import Protocol
from urllib.parse import quote, urlencode, urlparse
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from sibyl import config as config_module
from sibyl.api.rate_limit import limiter
from sibyl.auth.context import AuthContext
from sibyl.auth.dependencies import (
    get_auth_context,
    require_org_admin,
)
from sibyl.auth.http import select_access_token
from sibyl.auth.jit import provision_oidc_user
from sibyl.auth.jwt import JwtError, verify_refresh_token
from sibyl.auth.oauth_state import OAuthStateError, issue_state, verify_state
from sibyl.auth.oidc import (
    enabled_oidc_providers,
    get_oidc_provider,
    oidc_authorize_redirect,
    oidc_callback_claims,
    oidc_redirect_uri,
)
from sibyl.auth.primitives import DeviceTokenError, normalize_user_code
from sibyl.auth.silent_refresh import is_soft_refresh_error, silent_refresh_bounce
from sibyl.persistence import organization_runtime
from sibyl.persistence.auth_runtime import (
    approve_device_authorization,
    create_api_key_for_user,
    delete_failed_local_signup_user,
    deny_device_authorization,
    exchange_device_code,
    get_device_request_by_user_code,
    get_user_by_id,
    list_api_keys_for_user,
    log_audit_event,
    login_device_browser_user,
    login_github_identity,
    login_local_user,
    resolve_request_claims,
    resolve_request_user,
    revoke_access_session,
    revoke_api_key_for_user,
    rotate_refresh_exchange,
    signup_local_user,
    start_device_authorization,
    update_auth_user,
)
from sibyl.persistence.operations_runtime import is_setup_mode
from sibyl_core.auth import AuthUser, GitHubUserIdentity

router = APIRouter(prefix="/auth", tags=["auth"])

GITHUB_AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
GITHUB_TOKEN_URL = "https://github.com/login/oauth/access_token"  # noqa: S105
GITHUB_API_URL = "https://api.github.com"

ACCESS_TOKEN_COOKIE = "sibyl_access_token"  # noqa: S105
REFRESH_TOKEN_COOKIE = "sibyl_refresh_token"  # noqa: S105
OAUTH_STATE_COOKIE = "sibyl_oauth_state"
INVALID_INVITATION_DETAIL = "Invitation is not valid for this account"
SIGNUP_DISABLED_DETAIL = {
    "code": "signup_disabled",
    "message": "Public signups are disabled. Ask an admin for an invitation.",
}
LOCAL_AUTH_DISABLED_DETAIL = {
    "code": "local_auth_disabled",
    "message": "Local sign-in is disabled for this instance.",
}
BREAK_GLASS_EXPIRED_DETAIL = {
    "code": "break_glass_expired",
    "message": "Break-glass access has expired for this instance.",
}
BREAK_GLASS_EXPIRY_REQUIRED_DETAIL = {
    "code": "break_glass_expiry_required",
    "message": "Break-glass access requires an expiry timestamp.",
}
BREAK_GLASS_EXPIRY_TOO_LONG_DETAIL = {
    "code": "break_glass_expiry_too_long",
    "message": "Break-glass access expiry must be within four hours.",
}
BREAK_GLASS_IP_REQUIRED_DETAIL = {
    "code": "break_glass_ip_required",
    "message": "Break-glass access requires at least one allowed source CIDR.",
}
BREAK_GLASS_IP_DENIED_DETAIL = {
    "code": "break_glass_ip_denied",
    "message": "Break-glass access is not allowed from this source address.",
}
BREAK_GLASS_REASON_REQUIRED_DETAIL = {
    "code": "break_glass_reason_required",
    "message": "Break-glass access requires an incident reason.",
}
BREAK_GLASS_REASON_TOO_LONG_DETAIL = {
    "code": "break_glass_reason_too_long",
    "message": "Break-glass access reason must be 512 characters or fewer.",
}
BREAK_GLASS_MAX_WINDOW = timedelta(hours=4)
BREAK_GLASS_REASON_MAX_LENGTH = 512


class ApiKeyCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    live: bool = Field(default=True, description="Use sk_live_ prefix (true) or sk_test_ (false)")
    scopes: list[str] = Field(default_factory=lambda: ["mcp"], description="Granted scopes")
    project_ids: list[str] = Field(
        default_factory=list,
        description="Optional graph project IDs this key may access",
    )
    memory_space_ids: list[UUID] = Field(
        default_factory=list,
        description="Optional memory-space IDs this key may access",
    )
    expires_days: int | None = Field(
        default=None, ge=1, le=365, description="Optional expiry in days"
    )


class MeUpdateRequest(BaseModel):
    email: str | None = Field(default=None, max_length=255)
    name: str | None = Field(default=None, max_length=255)
    avatar_url: str | None = Field(default=None, max_length=2048)
    current_password: str | None = Field(default=None, min_length=1)
    new_password: str | None = Field(default=None, min_length=8)


class OIDCProviderResponse(BaseModel):
    name: str
    label: str
    login_url: str


class AuthProvidersResponse(BaseModel):
    local_auth_enabled: bool
    break_glass_enabled: bool = False
    providers: list[OIDCProviderResponse]


def _cookie_secure() -> bool:
    if config_module.settings.cookie_secure is not None:
        return bool(config_module.settings.cookie_secure)
    if config_module.settings.environment == "production":
        return True
    return config_module.settings.server_url.startswith("https://")


def _set_auth_cookies(
    response: Response,
    *,
    access_token: str,
    refresh_token: str,
    refresh_expires: datetime,
) -> None:
    """Set both access and refresh token cookies on a response."""
    # Access token cookie (short-lived, 1 hour)
    response.set_cookie(
        ACCESS_TOKEN_COOKIE,
        access_token,
        httponly=True,
        secure=_cookie_secure(),
        samesite="lax",
        max_age=int(
            timedelta(minutes=config_module.settings.access_token_expire_minutes).total_seconds()
        ),
        domain=config_module.settings.cookie_domain,
        path="/",
    )
    # Refresh token cookie (long-lived, 30 days)
    refresh_max_age = int((refresh_expires - datetime.now(UTC)).total_seconds())
    response.set_cookie(
        REFRESH_TOKEN_COOKIE,
        refresh_token,
        httponly=True,
        secure=_cookie_secure(),
        samesite="lax",
        max_age=max(refresh_max_age, 0),
        domain=config_module.settings.cookie_domain,
        path="/",
    )


def _set_access_cookie(
    response: Response,
    *,
    access_token: str,
    max_age_seconds: int | None = None,
) -> None:
    response.set_cookie(
        ACCESS_TOKEN_COOKIE,
        access_token,
        httponly=True,
        secure=_cookie_secure(),
        samesite="lax",
        max_age=max_age_seconds
        or int(
            timedelta(minutes=config_module.settings.access_token_expire_minutes).total_seconds()
        ),
        domain=config_module.settings.cookie_domain,
        path="/",
    )


def _clear_refresh_cookie(response: Response) -> None:
    response.delete_cookie(
        REFRESH_TOKEN_COOKIE, domain=config_module.settings.cookie_domain, path="/"
    )


def _clear_auth_cookies(response: Response) -> None:
    response.delete_cookie(
        ACCESS_TOKEN_COOKIE, domain=config_module.settings.cookie_domain, path="/"
    )
    response.delete_cookie(
        REFRESH_TOKEN_COOKIE, domain=config_module.settings.cookie_domain, path="/"
    )


def _safe_frontend_redirect(redirect_value: str | None) -> str:
    target = (redirect_value or "").strip()
    if not target:
        return config_module.settings.frontend_url

    if target.startswith("/"):
        base = config_module.settings.frontend_url
        parsed = urlparse(base)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        return origin + target

    base_parsed = urlparse(config_module.settings.frontend_url)
    target_parsed = urlparse(target)
    if (
        target_parsed.scheme
        and target_parsed.netloc
        and target_parsed.scheme == base_parsed.scheme
        and target_parsed.netloc == base_parsed.netloc
    ):
        return target

    return config_module.settings.frontend_url


def _frontend_login_url(*, error: str | None = None, invite_token: str | None = None) -> str:
    base = config_module.settings.frontend_url
    parsed = urlparse(base)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    params: dict[str, str] = {}
    if error:
        params["error"] = error
    if invite_token:
        params["invite"] = invite_token
    query = f"?{urlencode(params, quote_via=quote)}" if params else ""
    return origin + "/login" + query


def _request_session(request: Request) -> dict[str, object] | None:
    try:
        return request.session
    except (AssertionError, AttributeError):
        return None


def _oidc_session_key(provider_name: str, suffix: str) -> str:
    return f"sibyl_oidc:{provider_name}:{suffix}"


def _store_oidc_redirect(request: Request, *, provider_name: str) -> None:
    redirect = request.query_params.get("redirect") or request.query_params.get("next")
    if not redirect:
        return
    session = _request_session(request)
    if session is not None:
        session[_oidc_session_key(provider_name, "redirect")] = redirect


def _pop_oidc_redirect(request: Request, *, provider_name: str) -> str | None:
    session = _request_session(request)
    if session is None:
        return request.query_params.get("redirect") or request.query_params.get("next")
    value = session.pop(_oidc_session_key(provider_name, "redirect"), None)
    return str(value) if value is not None else None


async def _read_auth_payload(request: Request) -> dict[str, str]:
    content_type = (request.headers.get("content-type") or "").lower()
    try:
        if "application/json" in content_type:
            payload = await request.json()
            if isinstance(payload, dict):
                return {str(k): str(v) for k, v in payload.items() if v is not None}
            return {}
        form = await request.form()
        return {str(k): str(v) for k, v in dict(form).items() if v is not None}
    except Exception:
        return {}


def _require_jwt_secret() -> str:
    secret = config_module.settings.jwt_secret.get_secret_value()
    if not secret:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="JWT secret not configured",
        )
    return secret


class LocalSignupRequest(BaseModel):
    email: str = Field(..., min_length=3, max_length=255)
    password: str = Field(..., min_length=8, max_length=1024)
    name: str = Field(..., min_length=1, max_length=255)
    redirect: str | None = None
    invite_token: str | None = Field(default=None, min_length=1, max_length=512)


class LocalLoginRequest(BaseModel):
    email: str = Field(..., min_length=3, max_length=255)
    password: str = Field(..., min_length=1, max_length=1024)
    redirect: str | None = None
    invite_token: str | None = Field(default=None, min_length=1, max_length=512)
    break_glass_reason: str | None = None


class IssuedOrganization(Protocol):
    id: object
    slug: object
    name: object


class IssuedUser(Protocol):
    id: UUID
    email: object
    name: object


class IssuedSession(Protocol):
    user: IssuedUser
    organization: IssuedOrganization
    session_id: UUID
    access_token: str
    refresh_token: str
    refresh_expires: datetime


def _auth_payload_invite_token(body_token: str | None) -> str | None:
    token = body_token
    token = (token or "").strip()
    return token or None


def _auth_payload_has_redirect(body_redirect: str | None, request: Request) -> bool:
    return body_redirect is not None or request.query_params.get("redirect") is not None


def _auth_error_code(detail: object) -> str:
    if isinstance(detail, dict):
        code = detail.get("code")
        if code:
            return str(code)
    if str(detail) == INVALID_INVITATION_DETAIL:
        return "invalid_invitation"
    return "authentication_failed"


async def _validate_invitation_for_email(*, token: str | None, email: str) -> None:
    if token is None:
        return
    await organization_runtime.validate_org_invitation_for_signup(
        token=token,
        email=email,
    )


async def _require_signup_allowed(
    *,
    body: LocalSignupRequest,
) -> str | None:
    invite_token = _auth_payload_invite_token(body.invite_token)
    if invite_token is not None:
        await _validate_invitation_for_email(token=invite_token, email=body.email)
        return invite_token
    if config_module.settings.public_signups_enabled or await is_setup_mode():
        return None
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=SIGNUP_DISABLED_DETAIL)


async def _require_local_auth_allowed(request: Request) -> None:
    if await is_setup_mode():
        return
    if config_module.settings.break_glass_enabled:
        _require_break_glass_allowed(request)
        return
    if config_module.settings.local_auth_enabled:
        return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=LOCAL_AUTH_DISABLED_DETAIL,
    )


def _require_break_glass_allowed(request: Request) -> None:
    now = datetime.now(UTC)
    expires_at = config_module.settings.break_glass_expires_at
    if expires_at is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=BREAK_GLASS_EXPIRY_REQUIRED_DETAIL,
        )
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    else:
        expires_at = expires_at.astimezone(UTC)
    if now >= expires_at:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=BREAK_GLASS_EXPIRED_DETAIL,
        )
    if expires_at > now + BREAK_GLASS_MAX_WINDOW:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=BREAK_GLASS_EXPIRY_TOO_LONG_DETAIL,
        )

    allowed_ips = config_module.settings.break_glass_allowed_ips
    if not allowed_ips:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=BREAK_GLASS_IP_REQUIRED_DETAIL,
        )
    if not _client_ip_allowed(request, allowed_ips):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=BREAK_GLASS_IP_DENIED_DETAIL,
        )


def _client_ip_allowed(request: Request, cidrs: list[str]) -> bool:
    if request.client is None:
        return False
    try:
        client_ip = ip_address(request.client.host)
    except ValueError:
        return False
    for cidr in cidrs:
        try:
            network = ip_network(cidr, strict=False)
        except ValueError:
            return False
        if client_ip in network:
            return True
    return False


def _require_break_glass_reason(reason: str | None) -> str | None:
    if not config_module.settings.break_glass_enabled:
        return None
    normalized = (reason or "").strip()
    if not normalized:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=BREAK_GLASS_REASON_REQUIRED_DETAIL,
        )
    if len(normalized) > BREAK_GLASS_REASON_MAX_LENGTH:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=BREAK_GLASS_REASON_TOO_LONG_DETAIL,
        )
    return normalized


def _organization_payload(organization: IssuedOrganization) -> dict[str, str]:
    return {
        "id": str(organization.id),
        "slug": str(organization.slug),
        "name": str(organization.name),
    }


async def _apply_invitation_to_issued_session(
    *,
    issued: IssuedSession,
    invite_token: str | None,
    request: Request,
    cleanup_new_user_on_failure: bool = False,
) -> tuple[str, str, datetime, dict[str, str]]:
    if invite_token is None:
        return (
            issued.access_token,
            issued.refresh_token,
            issued.refresh_expires,
            _organization_payload(issued.organization),
        )

    try:
        accepted = await organization_runtime.accept_org_invitation(
            token=invite_token,
            user=issued.user,
            request=request,
            existing_session_id=issued.session_id,
        )
    except HTTPException:
        if cleanup_new_user_on_failure:
            await delete_failed_local_signup_user(
                user_id=issued.user.id,
                organization_id=UUID(str(issued.organization.id)),
            )
        else:
            await revoke_access_session(issued.access_token)
        raise
    return (
        accepted.access_token,
        accepted.refresh_token,
        accepted.refresh_expires,
        {
            "id": str(accepted.organization_id),
            "slug": accepted.organization_slug or "",
            "name": accepted.organization_name or "",
        },
    )


class DeviceStartRequest(BaseModel):
    client_name: str | None = Field(default=None, max_length=255)
    scope: str = Field(default="mcp", max_length=255)
    interval: int = Field(default=5, ge=1, le=60, description="Polling interval seconds")
    expires_in: int = Field(default=600, ge=60, le=3600, description="Expiry seconds")


class DeviceTokenRequest(BaseModel):
    device_code: str = Field(..., min_length=10, max_length=512)
    grant_type: str | None = Field(default=None, description="Optional, OAuth-style")


class RefreshTokenRequest(BaseModel):
    refresh_token: str = Field(..., min_length=10, max_length=4096)


async def _github_exchange_code(*, code: str, redirect_uri: str) -> str:
    client_id = config_module.settings.github_client_id.get_secret_value()
    client_secret = config_module.settings.github_client_secret.get_secret_value()
    if not client_id or not client_secret:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="GitHub OAuth is not configured",
        )

    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            GITHUB_TOKEN_URL,
            headers={"Accept": "application/json"},
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "code": code,
                "redirect_uri": redirect_uri,
            },
        )
        resp.raise_for_status()
        data = resp.json()

    token = data.get("access_token")
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="GitHub OAuth failed",
        )
    return str(token)


async def _github_fetch_identity(access_token: str) -> GitHubUserIdentity:
    async with httpx.AsyncClient(timeout=10) as client:
        user_resp = await client.get(
            f"{GITHUB_API_URL}/user",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/vnd.github+json",
            },
        )
        user_resp.raise_for_status()
        user_json = user_resp.json()

        email_resp = await client.get(
            f"{GITHUB_API_URL}/user/emails",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/vnd.github+json",
            },
        )
        email_resp.raise_for_status()
        emails = email_resp.json()

    primary_email = None
    if isinstance(emails, list):
        for e in emails:
            if e.get("primary") and e.get("verified"):
                primary_email = e.get("email")
                break

    payload = dict(user_json)
    if primary_email:
        payload["email"] = primary_email
    return GitHubUserIdentity.model_validate(payload)


@router.get("/providers", response_model=AuthProvidersResponse)
async def auth_providers() -> AuthProvidersResponse:
    setup_mode = await is_setup_mode()
    return AuthProvidersResponse(
        local_auth_enabled=(
            config_module.settings.local_auth_enabled
            or config_module.settings.break_glass_enabled
            or setup_mode
        ),
        break_glass_enabled=config_module.settings.break_glass_enabled,
        providers=[
            OIDCProviderResponse(
                name=provider.name,
                label=provider.label,
                login_url=provider.login_url,
            )
            for provider in enabled_oidc_providers()
        ],
    )


@router.get("/github")
async def github_login() -> Response:
    jwt_secret = _require_jwt_secret()

    client_id = config_module.settings.github_client_id.get_secret_value()
    client_secret = config_module.settings.github_client_secret.get_secret_value()
    if not client_id or not client_secret:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="GitHub OAuth is not configured",
        )

    state_cookie, issued = issue_state(secret=jwt_secret)
    redirect_uri = f"{config_module.settings.server_url}/api/auth/github/callback"

    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "state": issued.state,
        "scope": "read:user user:email",
    }
    url = f"{GITHUB_AUTHORIZE_URL}?{urlencode(params)}"

    response = RedirectResponse(url=url, status_code=status.HTTP_302_FOUND)
    response.set_cookie(
        OAUTH_STATE_COOKIE,
        state_cookie,
        httponly=True,
        secure=_cookie_secure(),
        samesite="lax",
        max_age=10 * 60,
        domain=config_module.settings.cookie_domain,
        path="/",
    )
    return response


@router.get("/oidc/{provider_name}/login")
async def oidc_login(request: Request, provider_name: str) -> Response:
    provider = get_oidc_provider(provider_name)
    _store_oidc_redirect(request, provider_name=provider.name)
    return await oidc_authorize_redirect(
        request,
        provider=provider,
        redirect_uri=oidc_redirect_uri(provider, route="callback"),
    )


async def _complete_oidc_request(
    request: Request,
    *,
    provider_name: str,
    action: str,
) -> Response:
    provider = get_oidc_provider(provider_name)
    error = request.query_params.get("error")
    if error:
        if is_soft_refresh_error(error):
            return silent_refresh_bounce(request, error=error)
        return RedirectResponse(url=_frontend_login_url(error=error), status_code=302)

    identity = await oidc_callback_claims(request, provider=provider)
    issued = await provision_oidc_user(identity=identity, request=request, action=action)
    redirect = _safe_frontend_redirect(_pop_oidc_redirect(request, provider_name=provider.name))
    response = RedirectResponse(url=redirect, status_code=302)
    max_age_seconds = int(
        timedelta(minutes=config_module.settings.oidc.session_minutes).total_seconds()
    )
    _set_access_cookie(
        response,
        access_token=issued.access_token,
        max_age_seconds=max_age_seconds,
    )
    if request.cookies.get(REFRESH_TOKEN_COOKIE):
        _clear_refresh_cookie(response)
    return response


@router.get("/oidc/{provider_name}/callback")
async def oidc_callback(request: Request, provider_name: str) -> Response:
    return await _complete_oidc_request(
        request,
        provider_name=provider_name,
        action="auth.oidc.login",
    )


@router.get("/oidc/{provider_name}/refresh")
async def oidc_silent_refresh(request: Request, provider_name: str) -> Response:
    if not config_module.settings.oidc.silent_refresh_enabled:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "oidc_silent_refresh_disabled"},
        )
    provider = get_oidc_provider(provider_name)
    if request.query_params.get("code") or request.query_params.get("error"):
        return await _complete_oidc_request(
            request,
            provider_name=provider.name,
            action="auth.oidc.refresh",
        )
    _store_oidc_redirect(request, provider_name=provider.name)
    return await oidc_authorize_redirect(
        request,
        provider=provider,
        redirect_uri=oidc_redirect_uri(provider, route="refresh"),
        prompt="none",
    )


@router.get("/github/callback")
async def github_callback(request: Request) -> Response:
    jwt_secret = _require_jwt_secret()
    try:
        verify_state(
            secret=jwt_secret,
            cookie_value=request.cookies.get(OAUTH_STATE_COOKIE),
            returned_state=request.query_params.get("state"),
        )
    except OAuthStateError as e:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(e)) from e

    code = request.query_params.get("code")
    if not code:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing code")

    redirect_uri = f"{config_module.settings.server_url}/api/auth/github/callback"
    github_token = await _github_exchange_code(code=code, redirect_uri=redirect_uri)
    identity = await _github_fetch_identity(github_token)
    issued = await login_github_identity(identity=identity, request=request)

    redirect = _safe_frontend_redirect(request.query_params.get("redirect"))
    response = RedirectResponse(url=redirect, status_code=status.HTTP_302_FOUND)
    _set_auth_cookies(
        response,
        access_token=issued.access_token,
        refresh_token=issued.refresh_token,
        refresh_expires=issued.refresh_expires,
    )
    response.delete_cookie(
        OAUTH_STATE_COOKIE, domain=config_module.settings.cookie_domain, path="/"
    )
    return response


@router.post("/local/signup", response_model=None)
async def local_signup(request: Request):
    _ = _require_jwt_secret()
    await _require_local_auth_allowed(request)
    data = await _read_auth_payload(request)
    body = LocalSignupRequest.model_validate(data)
    invite_token = _auth_payload_invite_token(body.invite_token)
    has_redirect = _auth_payload_has_redirect(body.redirect, request)

    try:
        invite_token = await _require_signup_allowed(body=body)
        issued = await signup_local_user(
            email=body.email,
            password=body.password,
            name=body.name,
            request=request,
        )
        (
            access_token,
            refresh_token,
            refresh_expires,
            organization,
        ) = await _apply_invitation_to_issued_session(
            issued=issued,
            invite_token=invite_token,
            request=request,
            cleanup_new_user_on_failure=True,
        )
    except HTTPException as e:
        if has_redirect:
            return RedirectResponse(
                url=_frontend_login_url(
                    error=_auth_error_code(e.detail),
                    invite_token=invite_token,
                ),
                status_code=status.HTTP_302_FOUND,
            )
        raise
    except ValueError as e:
        if has_redirect:
            return RedirectResponse(
                url=_frontend_login_url(error="account_conflict", invite_token=invite_token),
                status_code=status.HTTP_302_FOUND,
            )
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e)) from e

    redirect = _safe_frontend_redirect(body.redirect or request.query_params.get("redirect"))
    response: Response
    if has_redirect:
        response = RedirectResponse(url=redirect, status_code=status.HTTP_302_FOUND)
    else:
        response = Response(status_code=status.HTTP_201_CREATED)

    _set_auth_cookies(
        response,
        access_token=access_token,
        refresh_token=refresh_token,
        refresh_expires=refresh_expires,
    )
    if isinstance(response, RedirectResponse):
        return response
    return {
        "user": {
            "id": str(issued.user.id),
            "email": issued.user.email,
            "name": issued.user.name,
        },
        "organization": organization,
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_in": config_module.settings.access_token_expire_minutes * 60,
    }


@router.post("/local/login", response_model=None)
@limiter.limit("5/minute")  # Strict limit to prevent brute force
async def local_login(request: Request):
    _ = _require_jwt_secret()
    await _require_local_auth_allowed(request)
    data = await _read_auth_payload(request)
    body = LocalLoginRequest.model_validate(data)
    break_glass_reason = _require_break_glass_reason(body.break_glass_reason)
    invite_token = _auth_payload_invite_token(body.invite_token)
    has_redirect = _auth_payload_has_redirect(body.redirect, request)

    try:
        await _validate_invitation_for_email(token=invite_token, email=body.email)
    except HTTPException as e:
        if has_redirect:
            return RedirectResponse(
                url=_frontend_login_url(
                    error=_auth_error_code(e.detail),
                    invite_token=invite_token,
                ),
                status_code=status.HTTP_302_FOUND,
            )
        raise

    issued = await login_local_user(
        email=body.email,
        password=body.password,
        request=request,
        break_glass_reason=break_glass_reason,
    )
    if issued is None:
        if has_redirect:
            return RedirectResponse(
                url=_frontend_login_url(error="invalid_credentials", invite_token=invite_token),
                status_code=status.HTTP_302_FOUND,
            )
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")

    try:
        (
            access_token,
            refresh_token,
            refresh_expires,
            organization,
        ) = await _apply_invitation_to_issued_session(
            issued=issued,
            invite_token=invite_token,
            request=request,
        )
    except HTTPException as e:
        if has_redirect:
            return RedirectResponse(
                url=_frontend_login_url(
                    error=_auth_error_code(e.detail),
                    invite_token=invite_token,
                ),
                status_code=status.HTTP_302_FOUND,
            )
        raise

    redirect = _safe_frontend_redirect(body.redirect or request.query_params.get("redirect"))
    response: Response
    if has_redirect:
        response = RedirectResponse(url=redirect, status_code=status.HTTP_302_FOUND)
    else:
        response = Response(status_code=status.HTTP_200_OK)

    _set_auth_cookies(
        response,
        access_token=access_token,
        refresh_token=refresh_token,
        refresh_expires=refresh_expires,
    )
    if isinstance(response, RedirectResponse):
        return response
    return {
        "user": {
            "id": str(issued.user.id),
            "email": issued.user.email,
            "name": issued.user.name,
        },
        "organization": organization,
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_in": config_module.settings.access_token_expire_minutes * 60,
    }


@router.post("/device", response_model=None)
@limiter.limit("10/minute")  # Limit device code generation
async def device_start(request: Request) -> dict[str, object]:
    """Start a device authorization request (for CLI login)."""
    _ = _require_jwt_secret()
    data = await _read_auth_payload(request)
    body = DeviceStartRequest.model_validate(data)

    req, device_code = await start_device_authorization(
        client_name=body.client_name,
        scope=body.scope,
        expires_in=timedelta(seconds=body.expires_in),
        poll_interval_seconds=body.interval,
    )

    verify_url = f"{config_module.settings.server_url.rstrip('/')}/api/auth/device/verify"
    return {
        "device_code": device_code,
        "user_code": req.user_code,
        "verification_uri": verify_url,
        "verification_uri_complete": f"{verify_url}?user_code={req.user_code}",
        "expires_in": int(body.expires_in),
        "interval": int(body.interval),
    }


@router.post("/device/token", response_model=None)
@limiter.limit("60/minute")  # Allow frequent polling but prevent abuse
async def device_token(request: Request) -> Response:
    """Poll the device token endpoint until approved."""
    _ = _require_jwt_secret()
    data = await _read_auth_payload(request)
    body = DeviceTokenRequest.model_validate(data)
    if body.grant_type and body.grant_type != "urn:ietf:params:oauth:grant-type:device_code":
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"error": "unsupported_grant_type"},
        )

    try:
        tok = await exchange_device_code(device_code=body.device_code)
    except DeviceTokenError as e:
        content: dict[str, object] = {"error": e.error}
        if e.error_description:
            content["error_description"] = e.error_description
        return JSONResponse(status_code=status.HTTP_400_BAD_REQUEST, content=content)

    return JSONResponse(status_code=status.HTTP_200_OK, content=tok)


def _render_device_result_page(*, title: str, message: str, success: bool = True) -> HTMLResponse:
    """Render a styled result page for device auth (approved/denied)."""
    icon = "✓" if success else "✗"
    accent = "#50fa7b" if success else "#ff6363"  # SilkCircuit green/red
    glow = "rgba(80, 250, 123, 0.2)" if success else "rgba(255, 99, 99, 0.2)"

    html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{title} — Sibyl</title>
  <style>
    :root {{ color-scheme: dark; }}
    * {{ box-sizing: border-box; }}
    body {{
      font-family: system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif;
      background: linear-gradient(180deg, #0a0812 0%, #0d0a14 100%);
      color: #f0f0f8;
      margin: 0;
      min-height: 100vh;
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 24px;
    }}
    .wrap {{
      text-align: center;
      width: 100%;
      max-width: 400px;
      padding: 48px 32px;
      background: #12101a;
      border: 1px solid #2a2640;
      border-radius: 16px;
      box-shadow: 0 4px 24px rgba(0, 0, 0, 0.4);
    }}
    .icon-wrap {{
      width: 72px;
      height: 72px;
      margin: 0 auto 20px;
      border-radius: 50%;
      background: {glow};
      display: flex;
      align-items: center;
      justify-content: center;
    }}
    .icon {{
      font-size: 36px;
      color: {accent};
    }}
    h1 {{
      margin: 0 0 12px;
      font-size: 22px;
      font-weight: 600;
      color: {accent};
    }}
    p {{
      color: #8888a8;
      margin: 0;
      line-height: 1.6;
      font-size: 15px;
    }}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="icon-wrap">
      <div class="icon">{icon}</div>
    </div>
    <h1>{title}</h1>
    <p>{message}</p>
  </div>
</body>
</html>"""
    return HTMLResponse(html, status_code=200)


def _render_device_verify_page(
    *,
    user_code: str | None,
    error_code: str | None = None,
    authed_user: AuthUser | None = None,
    pending: dict[str, object] | None = None,
) -> HTMLResponse:
    """Render the device verification page with SilkCircuit styling."""
    safe_code = user_code or ""
    err = error_code or ""
    is_authed = authed_user is not None

    # Error messages with user-friendly descriptions
    error_messages = {
        "invalid_or_expired": "This device code has expired or is invalid. Please return to your terminal and start a new login.",
        "invalid_credentials": "Incorrect email or password. Please try again.",
        "not_authenticated": "You need to sign in first.",
        "invalid_token": "Your session has expired. Please sign in again.",
        "user_not_found": "User account not found.",
        "missing_user_code": "No device code provided.",
        "invalid_action": "Invalid action.",
    }
    error_message = error_messages.get(err, f"An error occurred: {err}") if err else ""

    # SilkCircuit CSS (matches frontend design tokens)
    css = """
    :root { color-scheme: dark; }
    * { box-sizing: border-box; }
    body {
      font-family: system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif;
      background: linear-gradient(180deg, #0a0812 0%, #0d0a14 100%);
      color: #f0f0f8;
      margin: 0;
      min-height: 100vh;
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 24px;
    }
    .wrap {
      width: 100%;
      max-width: 420px;
      padding: 32px;
      background: #12101a;
      border: 1px solid #2a2640;
      border-radius: 16px;
      box-shadow: 0 4px 24px rgba(0, 0, 0, 0.4), 0 0 0 1px rgba(225, 53, 255, 0.05);
    }
    .logo {
      text-align: center;
      margin-bottom: 24px;
    }
    .logo-icon {
      width: 48px;
      height: 48px;
      background: linear-gradient(135deg, #e135ff 0%, #80ffea 100%);
      border-radius: 12px;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      font-size: 24px;
      margin-bottom: 8px;
    }
    h1 {
      margin: 0 0 8px;
      font-size: 20px;
      font-weight: 600;
      color: #f0f0f8;
      text-align: center;
    }
    .sub {
      color: #8888a8;
      font-size: 14px;
      text-align: center;
      margin-bottom: 24px;
    }
    .sub strong { color: #80ffea; font-weight: 500; }
    .card {
      margin: 20px 0;
      padding: 16px;
      border-radius: 12px;
      border: 1px solid #2a2640;
      background: #0d0a14;
      font-size: 13px;
      line-height: 1.6;
    }
    .card div { color: #8888a8; }
    .card strong { color: #c0c0d8; font-weight: 500; }
    .card code { color: #80ffea; background: rgba(128, 255, 234, 0.1); padding: 2px 6px; border-radius: 4px; font-size: 12px; }
    .err {
      margin: 0 0 20px;
      padding: 16px;
      border-radius: 12px;
      border: 1px solid rgba(255, 99, 99, 0.3);
      background: rgba(255, 99, 99, 0.08);
      color: #ff9999;
      font-size: 14px;
      line-height: 1.5;
      text-align: center;
    }
    .err-icon { font-size: 32px; margin-bottom: 8px; }
    label {
      display: block;
      margin: 16px 0 6px;
      color: #a0a0c0;
      font-size: 13px;
      font-weight: 500;
    }
    input, textarea {
      width: 100%;
      padding: 12px 14px;
      border-radius: 10px;
      border: 1px solid #2a2640;
      background: #0d0a14;
      color: #f0f0f8;
      font-size: 15px;
      transition: border-color 0.2s, box-shadow 0.2s;
    }
    textarea {
      min-height: 84px;
      resize: vertical;
    }
    input:focus, textarea:focus {
      outline: none;
      border-color: #e135ff;
      box-shadow: 0 0 0 3px rgba(225, 53, 255, 0.15);
    }
    input::placeholder, textarea::placeholder { color: #505068; }
    button {
      margin-top: 20px;
      width: 100%;
      padding: 12px 16px;
      border-radius: 10px;
      border: none;
      background: linear-gradient(135deg, #e135ff 0%, #a855f7 100%);
      color: #fff;
      font-size: 15px;
      font-weight: 600;
      cursor: pointer;
      transition: opacity 0.2s, transform 0.1s;
    }
    button:hover { opacity: 0.9; }
    button:active { transform: scale(0.98); }
    button.secondary {
      background: #1a1624;
      border: 1px solid #2a2640;
      color: #c0c0d8;
    }
    button.secondary:hover { background: #221e30; }
    .link {
      display: block;
      text-align: center;
      margin-top: 16px;
      color: #80ffea;
      font-size: 14px;
      text-decoration: none;
    }
    .link:hover { text-decoration: underline; }
    """

    # Page content varies by state
    if err:
        # Error state: show message with option to try again
        body_html = f"""
        <div class="err">
          <div class="err-icon">⚠</div>
          {error_message}
        </div>
        <a href="/api/auth/device/verify" class="link">← Enter a different code</a>
        """
        title = "Device Login Failed"
    elif not safe_code:
        # No code: show code entry form
        body_html = """
        <form method="get" action="/api/auth/device/verify">
          <label>Device Code</label>
          <input name="user_code" placeholder="ABCD-EFGH" autofocus />
          <button type="submit">Continue</button>
        </form>
        """
        title = "Device Login"
    elif not is_authed:
        # Has code but not logged in: show login form
        break_glass_reason_field = (
            """
          <label>Incident reason</label>
          <textarea name="break_glass_reason" maxlength="512" required
            placeholder="Incident or change record for this emergency access"></textarea>
            """
            if config_module.settings.break_glass_enabled
            else ""
        )
        body_html = f"""
        <form method="post" action="/api/auth/device/verify">
          <input type="hidden" name="action" value="login" />
          <input type="hidden" name="user_code" value="{safe_code}" />
          <label>Email</label>
          <input name="email" type="email" autocomplete="username" required autofocus />
          <label>Password</label>
          <input name="password" type="password" autocomplete="current-password" required />
          {break_glass_reason_field}
          <button type="submit">Sign in & Continue</button>
        </form>
        """
        title = "Sign In to Approve"
    else:
        # Logged in with valid code: show approve/deny
        client_name = str(pending.get("client_name") or "sibyl-cli") if pending else "sibyl-cli"
        scope = str(pending.get("scope") or "mcp") if pending else "mcp"
        body_html = f"""
        <div class="card">
          <div><strong>Application:</strong> {client_name}</div>
          <div><strong>Permissions:</strong> <code>{scope}</code></div>
        </div>
        <form method="post" action="/api/auth/device/verify">
          <input type="hidden" name="action" value="approve" />
          <input type="hidden" name="user_code" value="{safe_code}" />
          <button type="submit">Approve Device</button>
        </form>
        <form method="post" action="/api/auth/device/verify">
          <input type="hidden" name="action" value="deny" />
          <input type="hidden" name="user_code" value="{safe_code}" />
          <button type="submit" class="secondary">Deny</button>
        </form>
        """
        title = "Approve Device Login"

    # Auth status banner
    if authed_user is not None and not err:
        authed_banner = f"<div class='sub'>Signed in as <strong>{authed_user.email or authed_user.name}</strong></div>"
    elif not safe_code:
        authed_banner = "<div class='sub'>Enter the code shown in your terminal</div>"
    elif not err:
        authed_banner = "<div class='sub'>Sign in to approve this device</div>"
    else:
        authed_banner = ""

    html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{title} — Sibyl</title>
  <style>{css}</style>
</head>
<body>
  <div class="wrap">
    <div class="logo">
      <div class="logo-icon">◈</div>
    </div>
    <h1>{title}</h1>
    {authed_banner}
    {body_html}
  </div>
</body>
</html>"""
    return HTMLResponse(html, status_code=200)


@router.get("/device/verify", response_model=None)
@limiter.limit("30/minute")  # Limit code verification attempts
async def device_verify_get(request: Request) -> Response:
    """User-facing approval page for device login."""
    _ = _require_jwt_secret()
    raw_code = request.query_params.get("user_code")
    user_code = normalize_user_code(raw_code)
    error_code = (request.query_params.get("error") or "").strip() or None

    user = await resolve_request_user(request)

    pending: dict[str, object] | None = None
    if user_code:
        req = await get_device_request_by_user_code(user_code)
        now = datetime.now(UTC).replace(tzinfo=None)
        # Security: Use same error for invalid and expired to prevent code enumeration
        if req is None or req.expires_at <= now or req.status != "pending":
            return _render_device_verify_page(
                user_code=user_code,
                error_code="invalid_or_expired",
                authed_user=user,
            )
        # Only show pending details if user is authenticated (prevents info leak)
        if user:
            pending = {
                "client_name": req.client_name,
                "scope": req.scope,
                "expires_at": req.expires_at.isoformat(),
            }

    return _render_device_verify_page(
        user_code=user_code,
        error_code=error_code,
        authed_user=user,
        pending=pending,
    )


@router.post("/device/verify", response_model=None)
@limiter.limit("10/minute")  # Stricter limit on form submissions
async def device_verify_post(request: Request) -> Response:
    _ = _require_jwt_secret()
    form = await request.form()
    action = str(form.get("action") or "").strip()
    user_code = normalize_user_code(str(form.get("user_code") or "").strip())
    if not user_code:
        return RedirectResponse(
            url="/api/auth/device/verify?error=missing_user_code", status_code=302
        )

    verify_url = f"/api/auth/device/verify?user_code={user_code}"

    if action == "login":
        await _require_local_auth_allowed(request)
        break_glass_reason = _require_break_glass_reason(str(form.get("break_glass_reason") or ""))
        email = str(form.get("email") or "").strip()
        password = str(form.get("password") or "").strip()
        login = await login_device_browser_user(
            email=email,
            password=password,
            request=request,
            break_glass_reason=break_glass_reason,
        )
        if login is None:
            return RedirectResponse(url=verify_url + "&error=invalid_credentials", status_code=302)

        response = RedirectResponse(url=verify_url, status_code=302)
        _set_auth_cookies(
            response,
            access_token=login.access_token,
            refresh_token=login.refresh_token,
            refresh_expires=login.refresh_expires,
        )
        return response

    claims = await resolve_request_claims(request)
    if not claims:
        return RedirectResponse(url=verify_url + "&error=not_authenticated", status_code=302)

    try:
        user_id = UUID(str(claims.get("sub", "")))
    except ValueError:
        return RedirectResponse(url=verify_url + "&error=invalid_token", status_code=302)

    user = await get_user_by_id(user_id)
    if user is None:
        return RedirectResponse(url=verify_url + "&error=user_not_found", status_code=302)

    if action == "deny":
        denied = await deny_device_authorization(
            user_id=user.id,
            user_code=user_code,
            request=request,
        )
        if denied is None:
            return RedirectResponse(url=verify_url + "&error=invalid_or_expired", status_code=302)
        return _render_device_result_page(
            title="Access Denied",
            message="You can close this tab and return to your terminal.",
            success=False,
        )

    if action != "approve":
        return RedirectResponse(url=verify_url + "&error=invalid_action", status_code=302)

    approved = await approve_device_authorization(
        user_id=user.id,
        user_code=user_code,
        request=request,
    )
    if approved is None:
        return RedirectResponse(url=verify_url + "&error=invalid_or_expired", status_code=302)
    return _render_device_result_page(
        title="Device Approved",
        message="You're all set! Close this tab and return to your terminal.",
        success=True,
    )


@router.post("/refresh", response_model=None)
@limiter.limit("30/minute")
async def refresh_tokens(request: Request):
    """Exchange a refresh token for new access + refresh tokens (token rotation).

    Accepts refresh token from:
    1. Request body (for API clients)
    2. Cookie (for browser clients)
    """
    _ = _require_jwt_secret()

    # Try body first, then cookie
    refresh_token: str | None = None
    data = await _read_auth_payload(request)
    refresh_from_body = bool(data.get("refresh_token"))
    if refresh_from_body:
        refresh_token = data["refresh_token"]
    else:
        refresh_token = request.cookies.get(REFRESH_TOKEN_COOKIE)

    def _unauthorized(detail: str) -> Response:
        response = JSONResponse(
            content={"detail": detail}, status_code=status.HTTP_401_UNAUTHORIZED
        )
        # Browser clients rely on cookie refresh; if it's invalid, clear cookies so the
        # frontend can reach `/login` without getting stuck in a redirect/refresh loop.
        if not refresh_from_body:
            _clear_auth_cookies(response)
        return response

    if not refresh_token:
        return _unauthorized("No refresh token provided")

    # Verify the refresh token JWT
    try:
        claims = verify_refresh_token(refresh_token)
    except JwtError as e:
        return _unauthorized(f"Invalid refresh token: {e}")

    # Extract user/org from claims
    try:
        user_id = UUID(str(claims["sub"]))
        org_raw = claims.get("org")
        org_id = UUID(str(org_raw)) if org_raw else None
    except (KeyError, ValueError):
        return _unauthorized("Invalid token claims")

    try:
        rotation = await rotate_refresh_exchange(
            refresh_token=refresh_token,
            user_id=user_id,
            organization_id=org_id,
            request=request,
        )
    except TimeoutError:
        return JSONResponse(
            content={"detail": "Authentication storage temporarily unavailable"},
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    if rotation is None:
        return _unauthorized("Session not found or revoked")

    # Set new auth cookies
    response = JSONResponse(
        content={
            "access_token": rotation.access_token,
            "refresh_token": rotation.refresh_token,
            "token_type": "Bearer",
            "expires_in": config_module.settings.access_token_expire_minutes * 60,
        }
    )
    _set_auth_cookies(
        response,
        access_token=rotation.access_token,
        refresh_token=rotation.refresh_token,
        refresh_expires=rotation.refresh_expires,
    )
    return response


@router.post("/logout")
async def logout(request: Request) -> Response:
    claims = await resolve_request_claims(request)
    token = select_access_token(
        authorization=request.headers.get("authorization"),
        cookie_token=request.cookies.get(ACCESS_TOKEN_COOKIE),
    )
    user_id: UUID | None = None
    org_id: UUID | None = None
    if claims:
        try:
            user_id = UUID(str(claims.get("sub", "")))
        except ValueError:
            user_id = None
        try:
            org_raw = claims.get("org")
            org_id = UUID(str(org_raw)) if org_raw else None
        except ValueError:
            org_id = None

    if user_id:
        await log_audit_event(
            action="auth.logout",
            user_id=user_id,
            organization_id=org_id,
            request=request,
            details={},
        )

    # Best-effort server-side revocation for JWT sessions.
    if token and not token.startswith("sk_"):
        await revoke_access_session(token)
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    response.delete_cookie(
        ACCESS_TOKEN_COOKIE, domain=config_module.settings.cookie_domain, path="/"
    )
    response.delete_cookie(
        REFRESH_TOKEN_COOKIE, domain=config_module.settings.cookie_domain, path="/"
    )
    return response


@router.get("/api-keys")
async def list_api_keys(
    ctx: AuthContext = Depends(get_auth_context),
):
    if ctx.organization is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="No organization context")

    keys = await list_api_keys_for_user(
        organization_id=ctx.organization.id,
        user_id=ctx.user.id,
    )
    return {
        "keys": [
            {
                "id": str(k.id),
                "name": k.name,
                "prefix": k.key_prefix,
                "scopes": list(k.scopes or []),
                "project_ids": list(getattr(k, "project_ids", []) or []),
                "memory_space_ids": list(getattr(k, "memory_space_ids", []) or []),
                "expires_at": k.expires_at,
                "revoked_at": k.revoked_at,
                "last_used_at": k.last_used_at,
                "created_at": k.created_at,
            }
            for k in keys
        ]
    }


@router.post("/api-keys")
async def create_api_key(
    request: Request,
    body: ApiKeyCreateRequest,
    ctx: AuthContext = Depends(get_auth_context),
    _admin: None = Depends(require_org_admin()),
):
    if ctx.organization is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="No organization context")

    expires_at = (
        datetime.now(UTC) + timedelta(days=int(body.expires_days))
        if body.expires_days is not None
        else None
    )
    record, raw = await create_api_key_for_user(
        organization_id=ctx.organization.id,
        user_id=ctx.user.id,
        name=body.name,
        live=body.live,
        scopes=body.scopes,
        project_ids=body.project_ids,
        memory_space_ids=body.memory_space_ids,
        expires_at=expires_at,
        request=request,
    )
    return {
        "id": str(record.id),
        "name": record.name,
        "prefix": record.key_prefix,
        "scopes": list(record.scopes or []),
        "project_ids": list(getattr(record, "project_ids", []) or []),
        "memory_space_ids": list(getattr(record, "memory_space_ids", []) or []),
        "expires_at": record.expires_at,
        "api_key": raw,
    }


@router.post("/api-keys/{api_key_id}/revoke")
async def revoke_api_key(
    request: Request,
    api_key_id: UUID,
    ctx: AuthContext = Depends(get_auth_context),
):
    if ctx.organization is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="No organization context")

    await revoke_api_key_for_user(
        api_key_id=api_key_id,
        organization_id=ctx.organization.id,
        actor_user_id=ctx.user.id,
        actor_org_role=ctx.org_role,
        request=request,
    )
    return {"success": True, "id": str(api_key_id)}


@router.get("/me")
async def me(
    ctx: AuthContext = Depends(get_auth_context),
):
    return {
        "user": {
            "id": str(ctx.user.id),
            "github_id": ctx.user.github_id,
            "email": ctx.user.email,
            "name": ctx.user.name,
            "avatar_url": ctx.user.avatar_url,
            "is_admin": ctx.user.is_admin,
        },
        "organization": (
            {
                "id": str(ctx.organization.id),
                "slug": ctx.organization.slug,
                "name": ctx.organization.name,
            }
            if ctx.organization
            else None
        ),
        "org_role": ctx.org_role.value if ctx.org_role else None,
    }


@router.patch("/me")
async def update_me(
    request: Request,
    body: MeUpdateRequest,
    ctx: AuthContext = Depends(get_auth_context),
):
    user = await update_auth_user(
        user_id=ctx.user.id,
        email=body.email,
        name=body.name,
        avatar_url=body.avatar_url,
        current_password=body.current_password,
        new_password=body.new_password,
        organization_id=ctx.organization.id if ctx.organization else None,
        request=request,
    )

    return {
        "user": {
            "id": str(user.id),
            "github_id": user.github_id,
            "email": user.email,
            "name": user.name,
            "avatar_url": user.avatar_url,
        }
    }
