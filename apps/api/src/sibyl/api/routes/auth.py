"""Authentication endpoints."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from urllib.parse import quote, urlencode, urlparse
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from sibyl import config as config_module
from sibyl.api.rate_limit import limiter
from sibyl.auth.api_keys import ApiKeyManager
from sibyl.auth.audit import AuditLogger
from sibyl.auth.context import AuthContext
from sibyl.auth.dependencies import (
    get_auth_context,
    get_current_user,
    require_org_admin,
    resolve_claims,
)
from sibyl.auth.device_authorization import (
    DeviceAuthorizationManager,
    DeviceTokenError,
    normalize_user_code,
)
from sibyl.auth.jwt import JwtError, create_access_token, create_refresh_token, verify_refresh_token
from sibyl.auth.memberships import OrganizationMembershipManager
from sibyl.auth.oauth_state import OAuthStateError, issue_state, verify_state
from sibyl.auth.organizations import OrganizationManager
from sibyl.auth.sessions import SessionManager
from sibyl.auth.users import GitHubUserIdentity, UserManager
from sibyl.db.connection import get_session_dependency
from sibyl.db.models import ApiKey, Organization, OrganizationMember, OrganizationRole, User

router = APIRouter(prefix="/auth", tags=["auth"])

GITHUB_AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
GITHUB_TOKEN_URL = "https://github.com/login/oauth/access_token"  # noqa: S105
GITHUB_API_URL = "https://api.github.com"

ACCESS_TOKEN_COOKIE = "sibyl_access_token"  # noqa: S105
REFRESH_TOKEN_COOKIE = "sibyl_refresh_token"  # noqa: S105
OAUTH_STATE_COOKIE = "sibyl_oauth_state"


class ApiKeyCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    live: bool = Field(default=True, description="Use sk_live_ prefix (true) or sk_test_ (false)")
    scopes: list[str] = Field(default_factory=lambda: ["mcp"], description="Granted scopes")
    expires_days: int | None = Field(
        default=None, ge=1, le=365, description="Optional expiry in days"
    )


class MeUpdateRequest(BaseModel):
    email: str | None = Field(default=None, max_length=255)
    name: str | None = Field(default=None, max_length=255)
    avatar_url: str | None = Field(default=None, max_length=2048)
    current_password: str | None = Field(default=None, min_length=1)
    new_password: str | None = Field(default=None, min_length=8)


def _cookie_secure() -> bool:
    if config_module.settings.cookie_secure is not None:
        return bool(config_module.settings.cookie_secure)
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


def _frontend_redirect(request: Request) -> str:
    return request.query_params.get("redirect", config_module.settings.frontend_url)


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


def _frontend_login_url(*, error: str | None = None) -> str:
    base = config_module.settings.frontend_url
    parsed = urlparse(base)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    url = origin + "/login"
    if error:
        url += f"?error={quote(error)}"
    return url


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


class LocalLoginRequest(BaseModel):
    email: str = Field(..., min_length=3, max_length=255)
    password: str = Field(..., min_length=1, max_length=1024)
    redirect: str | None = None


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


@router.get("/github/callback")
async def github_callback(
    request: Request,
    session: AsyncSession = Depends(get_session_dependency),
) -> Response:
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

    user = await UserManager(session).upsert_from_github(identity)
    org = await OrganizationManager(session).create_personal_for_user(user)
    await OrganizationMembershipManager(session).add_member(
        organization_id=org.id,
        user_id=user.id,
        role=OrganizationRole.OWNER,
    )

    # Generate tokens
    access_token = create_access_token(user_id=user.id, organization_id=org.id)
    refresh_token, refresh_expires = create_refresh_token(user_id=user.id, organization_id=org.id)

    # Create session record
    access_expires = datetime.now(UTC) + timedelta(
        minutes=config_module.settings.access_token_expire_minutes
    )
    await SessionManager(session).create_session(
        user_id=user.id,
        organization_id=org.id,
        token=access_token,
        expires_at=access_expires,
        refresh_token=refresh_token,
        refresh_token_expires_at=refresh_expires,
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )

    await AuditLogger(session).log(
        action="auth.github.login",
        user_id=user.id,
        organization_id=org.id,
        request=request,
        details={"github_id": user.github_id, "email": user.email},
    )

    response = RedirectResponse(url=_frontend_redirect(request), status_code=status.HTTP_302_FOUND)
    _set_auth_cookies(
        response,
        access_token=access_token,
        refresh_token=refresh_token,
        refresh_expires=refresh_expires,
    )
    response.delete_cookie(
        OAUTH_STATE_COOKIE, domain=config_module.settings.cookie_domain, path="/"
    )
    return response


@router.post("/local/signup", response_model=None)
async def local_signup(
    request: Request,
    session: AsyncSession = Depends(get_session_dependency),
):
    _ = _require_jwt_secret()
    data = await _read_auth_payload(request)
    body = LocalSignupRequest.model_validate(data)

    try:
        user = await UserManager(session).create_local_user(
            email=body.email,
            password=body.password,
            name=body.name,
        )
    except ValueError as e:
        if body.redirect is not None or request.query_params.get("redirect") is not None:
            return RedirectResponse(
                url=_frontend_login_url(error=str(e)),
                status_code=status.HTTP_302_FOUND,
            )
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e)) from e

    org = await OrganizationManager(session).create_personal_for_user(user)
    await OrganizationMembershipManager(session).add_member(
        organization_id=org.id,
        user_id=user.id,
        role=OrganizationRole.OWNER,
    )

    # Generate tokens
    access_token = create_access_token(user_id=user.id, organization_id=org.id)
    refresh_token, refresh_expires = create_refresh_token(user_id=user.id, organization_id=org.id)

    # Create session record
    access_expires = datetime.now(UTC) + timedelta(
        minutes=config_module.settings.access_token_expire_minutes
    )
    await SessionManager(session).create_session(
        user_id=user.id,
        organization_id=org.id,
        token=access_token,
        expires_at=access_expires,
        refresh_token=refresh_token,
        refresh_token_expires_at=refresh_expires,
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )

    await AuditLogger(session).log(
        action="auth.local.signup",
        user_id=user.id,
        organization_id=org.id,
        request=request,
        details={"email": user.email},
    )

    redirect = _safe_frontend_redirect(body.redirect or request.query_params.get("redirect"))
    response: Response
    if body.redirect is not None or request.query_params.get("redirect") is not None:
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
        "user": {"id": str(user.id), "email": user.email, "name": user.name},
        "organization": {"id": str(org.id), "slug": org.slug, "name": org.name},
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_in": config_module.settings.access_token_expire_minutes * 60,
    }


@router.post("/local/login", response_model=None)
@limiter.limit("5/minute")  # Strict limit to prevent brute force
async def local_login(
    request: Request,
    session: AsyncSession = Depends(get_session_dependency),
):
    _ = _require_jwt_secret()
    data = await _read_auth_payload(request)
    body = LocalLoginRequest.model_validate(data)

    user = await UserManager(session).authenticate_local(email=body.email, password=body.password)
    if user is None:
        if body.redirect is not None or request.query_params.get("redirect") is not None:
            return RedirectResponse(
                url=_frontend_login_url(error="invalid_credentials"),
                status_code=status.HTTP_302_FOUND,
            )
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")

    org = await OrganizationManager(session).create_personal_for_user(user)
    await OrganizationMembershipManager(session).add_member(
        organization_id=org.id,
        user_id=user.id,
        role=OrganizationRole.OWNER,
    )

    # Generate tokens
    access_token = create_access_token(user_id=user.id, organization_id=org.id)
    refresh_token, refresh_expires = create_refresh_token(user_id=user.id, organization_id=org.id)

    # Create session record
    access_expires = datetime.now(UTC) + timedelta(
        minutes=config_module.settings.access_token_expire_minutes
    )
    await SessionManager(session).create_session(
        user_id=user.id,
        organization_id=org.id,
        token=access_token,
        expires_at=access_expires,
        refresh_token=refresh_token,
        refresh_token_expires_at=refresh_expires,
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )

    await AuditLogger(session).log(
        action="auth.local.login",
        user_id=user.id,
        organization_id=org.id,
        request=request,
        details={"email": user.email},
    )

    redirect = _safe_frontend_redirect(body.redirect or request.query_params.get("redirect"))
    response: Response
    if body.redirect is not None or request.query_params.get("redirect") is not None:
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
        "user": {"id": str(user.id), "email": user.email, "name": user.name},
        "organization": {"id": str(org.id), "slug": org.slug, "name": org.name},
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_in": config_module.settings.access_token_expire_minutes * 60,
    }


@router.post("/device", response_model=None)
@limiter.limit("10/minute")  # Limit device code generation
async def device_start(
    request: Request,
    session: AsyncSession = Depends(get_session_dependency),
) -> dict[str, object]:
    """Start a device authorization request (for CLI login)."""
    _ = _require_jwt_secret()
    data = await _read_auth_payload(request)
    body = DeviceStartRequest.model_validate(data)

    mgr = DeviceAuthorizationManager(session)
    req, device_code = await mgr.start(
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
async def device_token(
    request: Request,
    session: AsyncSession = Depends(get_session_dependency),
) -> Response:
    """Poll the device token endpoint until approved."""
    _ = _require_jwt_secret()
    data = await _read_auth_payload(request)
    body = DeviceTokenRequest.model_validate(data)
    if body.grant_type and body.grant_type != "urn:ietf:params:oauth:grant-type:device_code":
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"error": "unsupported_grant_type"},
        )

    mgr = DeviceAuthorizationManager(session)
    try:
        tok = await mgr.exchange_device_code(device_code=body.device_code)
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
    authed_user: User | None = None,
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
    input {
      width: 100%;
      padding: 12px 14px;
      border-radius: 10px;
      border: 1px solid #2a2640;
      background: #0d0a14;
      color: #f0f0f8;
      font-size: 15px;
      transition: border-color 0.2s, box-shadow 0.2s;
    }
    input:focus {
      outline: none;
      border-color: #e135ff;
      box-shadow: 0 0 0 3px rgba(225, 53, 255, 0.15);
    }
    input::placeholder { color: #505068; }
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
        body_html = f"""
        <form method="post" action="/api/auth/device/verify">
          <input type="hidden" name="action" value="login" />
          <input type="hidden" name="user_code" value="{safe_code}" />
          <label>Email</label>
          <input name="email" type="email" autocomplete="username" required autofocus />
          <label>Password</label>
          <input name="password" type="password" autocomplete="current-password" required />
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
    if is_authed and not err:
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
async def device_verify_get(
    request: Request,
    session: AsyncSession = Depends(get_session_dependency),
) -> Response:
    """User-facing approval page for device login."""
    _ = _require_jwt_secret()
    raw_code = request.query_params.get("user_code")
    user_code = normalize_user_code(raw_code)
    error_code = (request.query_params.get("error") or "").strip() or None

    claims = await resolve_claims(request, session)
    user: User | None = None
    if claims:
        try:
            user_id = UUID(str(claims.get("sub", "")))
        except ValueError:
            user_id = None
        if user_id:
            user = await session.get(User, user_id)

    pending: dict[str, object] | None = None
    if user_code:
        req = await DeviceAuthorizationManager(session).get_by_user_code(user_code)
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
async def device_verify_post(
    request: Request,
    session: AsyncSession = Depends(get_session_dependency),
) -> Response:
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
        email = str(form.get("email") or "").strip()
        password = str(form.get("password") or "").strip()
        user = await UserManager(session).authenticate_local(email=email, password=password)
        if user is None:
            return RedirectResponse(url=verify_url + "&error=invalid_credentials", status_code=302)

        org = await OrganizationManager(session).create_personal_for_user(user)
        await OrganizationMembershipManager(session).add_member(
            organization_id=org.id,
            user_id=user.id,
            role=OrganizationRole.OWNER,
        )
        token = create_access_token(user_id=user.id, organization_id=org.id)
        await AuditLogger(session).log(
            action="auth.device.local_login",
            user_id=user.id,
            organization_id=org.id,
            request=request,
            details={"email": user.email},
        )

        response = RedirectResponse(url=verify_url, status_code=302)
        response.set_cookie(
            ACCESS_TOKEN_COOKIE,
            token,
            httponly=True,
            secure=_cookie_secure(),
            samesite="lax",
            max_age=int(
                timedelta(
                    minutes=config_module.settings.access_token_expire_minutes
                ).total_seconds()
            ),
            domain=config_module.settings.cookie_domain,
            path="/",
        )
        return response

    claims = await resolve_claims(request, session)
    if not claims:
        return RedirectResponse(url=verify_url + "&error=not_authenticated", status_code=302)

    try:
        user_id = UUID(str(claims.get("sub", "")))
    except ValueError:
        return RedirectResponse(url=verify_url + "&error=invalid_token", status_code=302)

    user = await session.get(User, user_id)
    if user is None:
        return RedirectResponse(url=verify_url + "&error=user_not_found", status_code=302)

    mgr = DeviceAuthorizationManager(session)
    req = await mgr.get_by_user_code(user_code)
    now = datetime.now(UTC).replace(tzinfo=None)
    # Security: Use same error for invalid/expired/consumed to prevent code enumeration
    if req is None or req.expires_at <= now or req.status != "pending":
        return RedirectResponse(url=verify_url + "&error=invalid_or_expired", status_code=302)

    if action == "deny":
        await mgr.deny(req)
        await AuditLogger(session).log(
            action="auth.device.deny",
            user_id=user.id,
            organization_id=None,
            request=request,
            details={"device_request_id": str(req.id), "client_name": req.client_name},
        )
        return _render_device_result_page(
            title="Access Denied",
            message="You can close this tab and return to your terminal.",
            success=False,
        )

    if action != "approve":
        return RedirectResponse(url=verify_url + "&error=invalid_action", status_code=302)

    org = await OrganizationManager(session).create_personal_for_user(user)
    await OrganizationMembershipManager(session).add_member(
        organization_id=org.id,
        user_id=user.id,
        role=OrganizationRole.OWNER,
    )
    await mgr.approve(req, user_id=user.id, organization_id=org.id)
    await AuditLogger(session).log(
        action="auth.device.approve",
        user_id=user.id,
        organization_id=org.id,
        request=request,
        details={"device_request_id": str(req.id), "client_name": req.client_name},
    )
    return _render_device_result_page(
        title="Device Approved",
        message="You're all set! Close this tab and return to your terminal.",
        success=True,
    )


@router.post("/refresh", response_model=None)
@limiter.limit("30/minute")
async def refresh_tokens(
    request: Request,
    session: AsyncSession = Depends(get_session_dependency),
):
    """Exchange a refresh token for new access + refresh tokens (token rotation).

    Accepts refresh token from:
    1. Request body (for API clients)
    2. Cookie (for browser clients)
    """
    _ = _require_jwt_secret()

    # Try body first, then cookie
    refresh_token: str | None = None
    data = await _read_auth_payload(request)
    if data.get("refresh_token"):
        refresh_token = data["refresh_token"]
    else:
        refresh_token = request.cookies.get(REFRESH_TOKEN_COOKIE)

    if not refresh_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="No refresh token provided",
        )

    # Verify the refresh token JWT
    try:
        claims = verify_refresh_token(refresh_token)
    except JwtError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid refresh token: {e}",
        ) from e

    # Find the session by refresh token
    session_mgr = SessionManager(session)
    user_session = await session_mgr.get_session_by_refresh_token(refresh_token)
    if user_session is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session not found or revoked",
        )

    # Extract user/org from claims
    try:
        user_id = UUID(str(claims["sub"]))
    except (KeyError, ValueError) as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token claims",
        ) from e

    org_raw = claims.get("org")
    org_id = UUID(str(org_raw)) if org_raw else None

    # Generate new tokens (rotation)
    new_access_token = create_access_token(user_id=user_id, organization_id=org_id)
    new_refresh_token, new_refresh_expires = create_refresh_token(
        user_id=user_id,
        organization_id=org_id,
        session_id=user_session.id,
    )

    # Calculate access token expiry
    access_expires = datetime.now(UTC) + timedelta(
        minutes=config_module.settings.access_token_expire_minutes
    )

    # Update session with new tokens
    await session_mgr.rotate_tokens(
        user_session,
        new_access_token=new_access_token,
        new_access_expires_at=access_expires,
        new_refresh_token=new_refresh_token,
        new_refresh_expires_at=new_refresh_expires,
    )

    await AuditLogger(session).log(
        action="auth.token.refresh",
        user_id=user_id,
        organization_id=org_id,
        request=request,
        details={"session_id": str(user_session.id)},
    )

    # Set new auth cookies
    response = JSONResponse(
        content={
            "access_token": new_access_token,
            "refresh_token": new_refresh_token,
            "token_type": "Bearer",
            "expires_in": config_module.settings.access_token_expire_minutes * 60,
        }
    )
    _set_auth_cookies(
        response,
        access_token=new_access_token,
        refresh_token=new_refresh_token,
        refresh_expires=new_refresh_expires,
    )
    return response


@router.post("/logout")
async def logout(
    request: Request,
    session: AsyncSession = Depends(get_session_dependency),
) -> Response:
    claims = await resolve_claims(request, session)
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
        await AuditLogger(session).log(
            action="auth.logout",
            user_id=user_id,
            organization_id=org_id,
            request=request,
            details={},
        )

    # Best-effort server-side revocation for JWT sessions.
    if token and not token.startswith("sk_"):
        session_mgr = SessionManager(session)
        existing = await session_mgr.get_session_by_token(token)
        if existing is not None:
            existing.revoked_at = datetime.now(UTC).replace(tzinfo=None)
            session.add(existing)
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
    session: AsyncSession = Depends(get_session_dependency),
):
    if ctx.organization is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="No organization context")

    keys = await ApiKeyManager(session).list_for_user(
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
    session: AsyncSession = Depends(get_session_dependency),
):
    if ctx.organization is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="No organization context")

    expires_at = (
        datetime.now(UTC) + timedelta(days=int(body.expires_days))
        if body.expires_days is not None
        else None
    )
    record, raw = await ApiKeyManager(session).create(
        organization_id=ctx.organization.id,
        user_id=ctx.user.id,
        name=body.name,
        live=body.live,
        scopes=body.scopes,
        expires_at=expires_at,
    )
    await AuditLogger(session).log(
        action="auth.api_key.create",
        user_id=ctx.user.id,
        organization_id=ctx.organization.id,
        request=request,
        details={"api_key_id": str(record.id), "name": record.name, "prefix": record.key_prefix},
    )
    return {
        "id": str(record.id),
        "name": record.name,
        "prefix": record.key_prefix,
        "scopes": list(record.scopes or []),
        "expires_at": record.expires_at,
        "api_key": raw,
    }


@router.post("/api-keys/{api_key_id}/revoke")
async def revoke_api_key(
    request: Request,
    api_key_id: UUID,
    ctx: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session_dependency),
):
    if ctx.organization is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="No organization context")

    key = await session.get(ApiKey, api_key_id)
    if key is None or key.organization_id != ctx.organization.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="API key not found")

    if key.user_id != ctx.user.id and ctx.org_role not in {
        OrganizationRole.OWNER,
        OrganizationRole.ADMIN,
    }:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")

    await ApiKeyManager(session).revoke(api_key_id)
    await AuditLogger(session).log(
        action="auth.api_key.revoke",
        user_id=ctx.user.id,
        organization_id=ctx.organization.id,
        request=request,
        details={"api_key_id": str(api_key_id)},
    )
    return {"success": True, "id": str(api_key_id)}


@router.get("/me")
async def me(
    request: Request,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session_dependency),
):
    claims = await resolve_claims(request, session)
    org = None
    role = None

    org_id = claims.get("org") if claims else None
    if org_id:
        try:
            org_uuid = UUID(str(org_id))
        except ValueError:
            org_uuid = None

        if org_uuid:
            org = await session.get(Organization, org_uuid)
        if org:
            result = await session.execute(
                select(OrganizationMember).where(
                    OrganizationMember.organization_id == org.id,
                    OrganizationMember.user_id == user.id,
                )
            )
            membership = result.scalar_one_or_none()
            role = membership.role.value if membership else None

    return {
        "user": {
            "id": str(user.id),
            "github_id": user.github_id,
            "email": user.email,
            "name": user.name,
            "avatar_url": user.avatar_url,
        },
        "organization": ({"id": str(org.id), "slug": org.slug, "name": org.name} if org else None),
        "org_role": role,
    }


@router.patch("/me")
async def update_me(
    request: Request,
    body: MeUpdateRequest,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session_dependency),
):
    from sibyl.auth.users import PasswordChange

    changes: list[str] = []
    if body.email is not None:
        changes.append("email")
    if body.name is not None:
        changes.append("name")
    if body.avatar_url is not None:
        changes.append("avatar_url")
    if body.new_password is not None:
        changes.append("password")
    if not changes:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No fields to update")

    manager = UserManager(session)
    try:
        await manager.update_profile(
            user,
            email=body.email,
            name=body.name,
            avatar_url=body.avatar_url,
        )
        if body.new_password is not None:
            await manager.change_password(
                user,
                PasswordChange(
                    current_password=body.current_password,
                    new_password=body.new_password,
                ),
            )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e

    claims = await resolve_claims(request, session)
    org_id: UUID | None = None
    if claims and claims.get("org"):
        try:
            org_id = UUID(str(claims["org"]))
        except ValueError:
            org_id = None

    if any(c != "password" for c in changes):
        await AuditLogger(session).log(
            action="user.update_profile",
            user_id=user.id,
            organization_id=org_id,
            request=request,
            details={"fields": [c for c in changes if c != "password"]},
        )
    if "password" in changes:
        await AuditLogger(session).log(
            action="user.change_password",
            user_id=user.id,
            organization_id=org_id,
            request=request,
            details={},
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
