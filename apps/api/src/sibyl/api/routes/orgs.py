"""Organization REST APIs."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Request, Response, status
from pydantic import BaseModel, Field

from sibyl import config as config_module
from sibyl.auth.context import AuthContext
from sibyl.auth.dependencies import get_auth_context, get_current_user
from sibyl.persistence import organization_runtime

router = APIRouter(prefix="/orgs", tags=["orgs"])

ACCESS_TOKEN_COOKIE = "sibyl_access_token"  # noqa: S105
REFRESH_TOKEN_COOKIE = "sibyl_refresh_token"  # noqa: S105


class OrganizationCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    slug: str | None = Field(default=None, max_length=64)


class OrganizationUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    slug: str | None = Field(default=None, max_length=64)


def _cookie_secure() -> bool:
    if config_module.settings.cookie_secure is not None:
        return bool(config_module.settings.cookie_secure)
    return config_module.settings.server_url.startswith("https://")


def _set_access_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        ACCESS_TOKEN_COOKIE,
        token,
        httponly=True,
        secure=_cookie_secure(),
        samesite="lax",
        max_age=int(
            timedelta(minutes=config_module.settings.access_token_expire_minutes).total_seconds()
        ),
        domain=config_module.settings.cookie_domain,
        path="/",
    )


def _set_auth_cookies(
    response: Response,
    *,
    access_token: str,
    refresh_token: str,
    refresh_expires: datetime,
) -> None:
    _set_access_cookie(response, access_token)
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


@router.get("")
async def list_orgs(
    user: Any = Depends(get_current_user),
):
    orgs = await organization_runtime.list_orgs(user_id=user.id)
    return {
        "orgs": [
            {
                "id": str(org.id),
                "slug": org.slug,
                "name": org.name,
                "is_personal": org.is_personal,
                "role": org.role.value if org.role else None,
            }
            for org in orgs
        ]
    }


@router.post("")
async def create_org(
    request: Request,
    body: OrganizationCreateRequest,
    response: Response,
    user: Any = Depends(get_current_user),
):
    created = await organization_runtime.create_org(
        request=request,
        user_id=user.id,
        name=body.name,
        slug=body.slug,
    )
    response.status_code = status.HTTP_201_CREATED
    _set_auth_cookies(
        response,
        access_token=created.access_token,
        refresh_token=created.refresh_token,
        refresh_expires=created.refresh_expires,
    )
    return {
        "organization": {"id": str(created.id), "slug": created.slug, "name": created.name},
        "access_token": created.access_token,
        "refresh_token": created.refresh_token,
        "token_type": "Bearer",
        "expires_in": config_module.settings.access_token_expire_minutes * 60,
    }


@router.get("/{slug}")
async def get_org(
    slug: str,
    ctx: AuthContext = Depends(get_auth_context),
):
    org = await organization_runtime.get_org(slug=slug, user_id=ctx.user.id)
    return {
        "organization": {"id": str(org.id), "slug": org.slug, "name": org.name},
        "role": org.role.value,
    }


@router.patch("/{slug}")
async def update_org(
    request: Request,
    slug: str,
    body: OrganizationUpdateRequest,
    ctx: AuthContext = Depends(get_auth_context),
):
    updated = await organization_runtime.update_org(
        request=request,
        slug=slug,
        user_id=ctx.user.id,
        name=body.name,
        new_slug=body.slug,
    )
    return {"organization": {"id": str(updated.id), "slug": updated.slug, "name": updated.name}}


@router.delete("/{slug}")
async def delete_org(
    request: Request,
    slug: str,
    ctx: AuthContext = Depends(get_auth_context),
):
    await organization_runtime.delete_org(request=request, slug=slug, user_id=ctx.user.id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{slug}/switch")
async def switch_org(
    request: Request,
    slug: str,
    response: Response,
    user: Any = Depends(get_current_user),
):
    switched = await organization_runtime.switch_org(request=request, slug=slug, user_id=user.id)
    _set_auth_cookies(
        response,
        access_token=switched.access_token,
        refresh_token=switched.refresh_token,
        refresh_expires=switched.refresh_expires,
    )
    return {
        "organization": {"id": str(switched.id), "slug": switched.slug, "name": switched.name},
        "access_token": switched.access_token,
        "refresh_token": switched.refresh_token,
        "token_type": "Bearer",
        "expires_in": config_module.settings.access_token_expire_minutes * 60,
    }
