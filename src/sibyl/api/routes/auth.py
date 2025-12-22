"""Authentication endpoints."""

from __future__ import annotations

from datetime import timedelta
from urllib.parse import urlencode
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from sibyl import config as config_module
from sibyl.auth.api_keys import ApiKeyManager
from sibyl.auth.dependencies import get_auth_context, get_current_user, resolve_claims, require_org_admin
from sibyl.auth.context import AuthContext
from sibyl.auth.jwt import create_access_token
from sibyl.auth.memberships import OrganizationMembershipManager
from sibyl.auth.oauth_state import OAuthStateError, issue_state, verify_state
from sibyl.auth.organizations import OrganizationManager
from sibyl.auth.users import GitHubUserIdentity, UserManager
from sibyl.db.connection import get_session_dependency
from sibyl.db.models import ApiKey, Organization, OrganizationMember, OrganizationRole, User

router = APIRouter(prefix="/auth", tags=["auth"])

GITHUB_AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
GITHUB_TOKEN_URL = "https://github.com/login/oauth/access_token"
GITHUB_API_URL = "https://api.github.com"

ACCESS_TOKEN_COOKIE = "sibyl_access_token"
OAUTH_STATE_COOKIE = "sibyl_oauth_state"


class ApiKeyCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    live: bool = Field(default=True, description="Use sk_live_ prefix (true) or sk_test_ (false)")


def _cookie_secure() -> bool:
    return config_module.settings.server_url.startswith("https://")


def _frontend_redirect(request: Request) -> str:
    return request.query_params.get("redirect", "http://localhost:3337/")


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
            headers={"Authorization": f"Bearer {access_token}", "Accept": "application/vnd.github+json"},
        )
        user_resp.raise_for_status()
        user_json = user_resp.json()

        email_resp = await client.get(
            f"{GITHUB_API_URL}/user/emails",
            headers={"Authorization": f"Bearer {access_token}", "Accept": "application/vnd.github+json"},
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
async def github_login(request: Request) -> Response:
    jwt_secret = config_module.settings.jwt_secret.get_secret_value()
    if not jwt_secret:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="JWT secret not configured",
        )

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
        path="/",
    )
    return response


@router.get("/github/callback")
async def github_callback(
    request: Request,
    session: AsyncSession = Depends(get_session_dependency),
) -> Response:
    jwt_secret = config_module.settings.jwt_secret.get_secret_value()
    if not jwt_secret:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="JWT secret not configured",
        )
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
        path="/",
    )
    response.delete_cookie(OAUTH_STATE_COOKIE, path="/")
    return response


@router.post("/logout")
async def logout() -> Response:
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    response.delete_cookie(ACCESS_TOKEN_COOKIE, path="/")
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
        "organization": (
            {"id": str(org.id), "slug": org.slug, "name": org.name} if org else None
        ),
        "org_role": role,
    }
