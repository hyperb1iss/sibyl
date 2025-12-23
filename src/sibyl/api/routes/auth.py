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
from sibyl.auth.api_keys import ApiKeyManager
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
from sibyl.auth.jwt import create_access_token
from sibyl.auth.memberships import OrganizationMembershipManager
from sibyl.auth.oauth_state import OAuthStateError, issue_state, verify_state
from sibyl.auth.organizations import OrganizationManager
from sibyl.auth.users import GitHubUserIdentity, UserManager
from sibyl.db.connection import get_session_dependency
from sibyl.db.models import ApiKey, Organization, OrganizationMember, OrganizationRole, User

router = APIRouter(prefix="/auth", tags=["auth"])

GITHUB_AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
GITHUB_TOKEN_URL = "https://github.com/login/oauth/access_token"  # noqa: S105
GITHUB_API_URL = "https://api.github.com"

ACCESS_TOKEN_COOKIE = "sibyl_access_token"  # noqa: S105
OAUTH_STATE_COOKIE = "sibyl_oauth_state"


class ApiKeyCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    live: bool = Field(default=True, description="Use sk_live_ prefix (true) or sk_test_ (false)")


def _cookie_secure() -> bool:
    if config_module.settings.cookie_secure is not None:
        return bool(config_module.settings.cookie_secure)
    return config_module.settings.server_url.startswith("https://")


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
    access_token = await _github_exchange_code(code=code, redirect_uri=redirect_uri)
    identity = await _github_fetch_identity(access_token)

    user = await UserManager(session).upsert_from_github(identity)
    org = await OrganizationManager(session).create_personal_for_user(user)
    await OrganizationMembershipManager(session).add_member(
        organization_id=org.id,
        user_id=user.id,
        role=OrganizationRole.OWNER,
    )

    token = create_access_token(user_id=user.id, organization_id=org.id)

    response = RedirectResponse(url=_frontend_redirect(request), status_code=status.HTTP_302_FOUND)
    response.set_cookie(
        ACCESS_TOKEN_COOKIE,
        token,
        httponly=True,
        secure=_cookie_secure(),
        samesite="lax",
        max_age=int(timedelta(hours=config_module.settings.jwt_expiry_hours).total_seconds()),
        domain=config_module.settings.cookie_domain,
        path="/",
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

    token = create_access_token(user_id=user.id, organization_id=org.id)

    redirect = _safe_frontend_redirect(body.redirect or request.query_params.get("redirect"))
    response: Response
    if body.redirect is not None or request.query_params.get("redirect") is not None:
        response = RedirectResponse(url=redirect, status_code=status.HTTP_302_FOUND)
    else:
        response = Response(status_code=status.HTTP_201_CREATED)

    response.set_cookie(
        ACCESS_TOKEN_COOKIE,
        token,
        httponly=True,
        secure=_cookie_secure(),
        samesite="lax",
        max_age=int(timedelta(hours=config_module.settings.jwt_expiry_hours).total_seconds()),
        domain=config_module.settings.cookie_domain,
        path="/",
    )
    if isinstance(response, RedirectResponse):
        return response
    return {
        "user": {"id": str(user.id), "email": user.email, "name": user.name},
        "organization": {"id": str(org.id), "slug": org.slug, "name": org.name},
        "access_token": token,
    }


@router.post("/local/login", response_model=None)
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

    token = create_access_token(user_id=user.id, organization_id=org.id)

    redirect = _safe_frontend_redirect(body.redirect or request.query_params.get("redirect"))
    response: Response
    if body.redirect is not None or request.query_params.get("redirect") is not None:
        response = RedirectResponse(url=redirect, status_code=status.HTTP_302_FOUND)
    else:
        response = Response(status_code=status.HTTP_200_OK)

    response.set_cookie(
        ACCESS_TOKEN_COOKIE,
        token,
        httponly=True,
        secure=_cookie_secure(),
        samesite="lax",
        max_age=int(timedelta(hours=config_module.settings.jwt_expiry_hours).total_seconds()),
        domain=config_module.settings.cookie_domain,
        path="/",
    )
    if isinstance(response, RedirectResponse):
        return response
    return {
        "user": {"id": str(user.id), "email": user.email, "name": user.name},
        "organization": {"id": str(org.id), "slug": org.slug, "name": org.name},
        "access_token": token,
    }


@router.post("/device", response_model=None)
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


def _render_device_verify_page(
    *,
    user_code: str | None,
    error_code: str | None = None,
    authed_user: User | None = None,
    pending: dict[str, object] | None = None,
) -> HTMLResponse:
    safe_code = user_code or ""
    err = error_code or ""
    is_authed = authed_user is not None
    title = "Approve Device Login"

    client_name = ""
    scope = ""
    expires_at = ""
    if pending:
        client_name = str(pending.get("client_name") or "")
        scope = str(pending.get("scope") or "")
        expires_at = str(pending.get("expires_at") or "")

    authed_banner = (
        f"<div class='sub'>Signed in as <strong>{authed_user.email or authed_user.name}</strong></div>"
        if is_authed
        else "<div class='sub'>Sign in to approve this device.</div>"
    )

    error_html = f"<div class='err'>Error: <code>{err}</code></div>" if err else ""

    pending_html = ""
    if pending:
        pending_html = (
            "<div class='card'>"
            f"<div><strong>Client</strong>: {client_name or 'sibyl-cli'}</div>"
            f"<div><strong>Scope</strong>: <code>{scope or 'mcp'}</code></div>"
            f"<div><strong>Expires</strong>: {expires_at}</div>"
            "</div>"
        )

    login_form = f"""
    <form method="post" action="/api/auth/device/verify">
      <input type="hidden" name="action" value="login" />
      <input type="hidden" name="user_code" value="{safe_code}" />
      <label>Email</label>
      <input name="email" type="email" autocomplete="username" required />
      <label>Password</label>
      <input name="password" type="password" autocomplete="current-password" required />
      <button type="submit">Sign in</button>
    </form>
    """

    approve_form = f"""
    <form method="post" action="/api/auth/device/verify">
      <input type="hidden" name="action" value="approve" />
      <input type="hidden" name="user_code" value="{safe_code}" />
      <button type="submit">Approve</button>
    </form>
    <form method="post" action="/api/auth/device/verify" style="margin-top: 10px">
      <input type="hidden" name="action" value="deny" />
      <input type="hidden" name="user_code" value="{safe_code}" />
      <button type="submit" class="secondary">Deny</button>
    </form>
    """

    code_form = f"""
    <form method="get" action="/api/auth/device/verify">
      <label>Device code</label>
      <input name="user_code" value="{safe_code}" placeholder="ABCD-EFGH" />
      <button type="submit">Continue</button>
    </form>
    """

    body_html = code_form if not safe_code else (login_form if not is_authed else approve_form)

    html = f"""
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{title}</title>
  <style>
    :root {{ color-scheme: dark; }}
    body {{ font-family: system-ui, -apple-system, Segoe UI, sans-serif; background: #0b0b10; color: #e8e8f0; margin: 0; }}
    .wrap {{ max-width: 560px; margin: 8vh auto; padding: 24px; background: #12121a; border: 1px solid #2a2a3a; border-radius: 14px; }}
    h1 {{ margin: 0 0 8px; font-size: 22px; }}
    .sub {{ color: #a7a7c7; margin-bottom: 18px; }}
    label {{ display: block; margin: 12px 0 6px; color: #cfcfe9; }}
    input {{ width: 100%; padding: 10px 12px; border-radius: 10px; border: 1px solid #2a2a3a; background: #0f0f16; color: #fff; }}
    button {{ margin-top: 16px; width: 100%; padding: 10px 12px; border-radius: 10px; border: 1px solid #3a2a6a; background: #5b2bff; color: #fff; font-weight: 600; cursor: pointer; }}
    button.secondary {{ background: #1a1a24; border-color: #2a2a3a; }}
    .card {{ margin: 16px 0; padding: 12px; border-radius: 12px; border: 1px solid #2a2a3a; background: #0f0f16; color: #cfcfe9; }}
    .err {{ margin: 12px 0; padding: 10px 12px; border-radius: 12px; border: 1px solid #5a2a2a; background: #1a0f12; color: #ffb4b4; }}
    code {{ color: #80ffea; }}
  </style>
</head>
<body>
  <div class="wrap">
    <h1>{title}</h1>
    {authed_banner}
    {error_html}
    {pending_html}
    {body_html}
  </div>
</body>
</html>
"""
    return HTMLResponse(html, status_code=200)


@router.get("/device/verify", response_model=None)
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
        if req is None:
            return _render_device_verify_page(
                user_code=user_code,
                error_code="invalid_user_code",
                authed_user=user,
            )
        now = datetime.now(UTC).replace(tzinfo=None)
        if req.expires_at <= now:
            return _render_device_verify_page(
                user_code=user_code,
                error_code="expired_token",
                authed_user=user,
            )
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
async def device_verify_post(
    request: Request,
    session: AsyncSession = Depends(get_session_dependency),
) -> Response:
    _ = _require_jwt_secret()
    form = await request.form()
    action = str(form.get("action") or "").strip()
    user_code = normalize_user_code(str(form.get("user_code") or "").strip())
    if not user_code:
        return RedirectResponse(url="/api/auth/device/verify?error=missing_user_code", status_code=302)

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

        response = RedirectResponse(url=verify_url, status_code=302)
        response.set_cookie(
            ACCESS_TOKEN_COOKIE,
            token,
            httponly=True,
            secure=_cookie_secure(),
            samesite="lax",
            max_age=int(timedelta(hours=config_module.settings.jwt_expiry_hours).total_seconds()),
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
    if req is None:
        return RedirectResponse(url=verify_url + "&error=invalid_user_code", status_code=302)

    now = datetime.now(UTC).replace(tzinfo=None)
    if req.expires_at <= now:
        return RedirectResponse(url=verify_url + "&error=expired_token", status_code=302)

    if action == "deny":
        await mgr.deny(req)
        return HTMLResponse(
            "<h1>Denied</h1><p>You can close this tab and return to your terminal.</p>",
            status_code=200,
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
    return HTMLResponse(
        "<h1>Approved</h1><p>Device login approved. You can close this tab and return to your terminal.</p>",
        status_code=200,
    )


@router.post("/logout")
async def logout() -> Response:
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    response.delete_cookie(
        ACCESS_TOKEN_COOKIE, domain=config_module.settings.cookie_domain, path="/"
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
                "revoked_at": k.revoked_at,
                "last_used_at": k.last_used_at,
                "created_at": k.created_at,
            }
            for k in keys
        ]
    }


@router.post("/api-keys")
async def create_api_key(
    body: ApiKeyCreateRequest,
    ctx: AuthContext = Depends(get_auth_context),
    _admin: None = Depends(require_org_admin()),
    session: AsyncSession = Depends(get_session_dependency),
):
    if ctx.organization is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="No organization context")

    record, raw = await ApiKeyManager(session).create(
        organization_id=ctx.organization.id,
        user_id=ctx.user.id,
        name=body.name,
        live=body.live,
    )
    return {
        "id": str(record.id),
        "name": record.name,
        "prefix": record.key_prefix,
        "api_key": raw,
    }


@router.post("/api-keys/{api_key_id}/revoke")
async def revoke_api_key(
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
