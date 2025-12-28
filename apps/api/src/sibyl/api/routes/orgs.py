"""Organization REST APIs."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from sibyl import config as config_module
from sibyl.auth.audit import AuditLogger
from sibyl.auth.context import AuthContext
from sibyl.auth.dependencies import get_auth_context, get_current_user
from sibyl.auth.http import select_access_token
from sibyl.auth.jwt import create_access_token, create_refresh_token
from sibyl.auth.memberships import OrganizationMembershipManager
from sibyl.auth.organizations import OrganizationManager, slugify
from sibyl.auth.sessions import SessionManager
from sibyl.db.connection import get_session_dependency
from sibyl.db.models import Organization, OrganizationMember, OrganizationRole, User

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
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session_dependency),
):
    result = await session.execute(
        select(Organization, OrganizationMember.role)
        .join(OrganizationMember, OrganizationMember.organization_id == Organization.id)
        .where(OrganizationMember.user_id == user.id)
        .order_by(Organization.slug.asc())
    )

    orgs = []
    for org, role in result.all():
        orgs.append(
            {
                "id": str(org.id),
                "slug": org.slug,
                "name": org.name,
                "is_personal": org.is_personal,
                "role": role.value if role else None,
            }
        )
    return {"orgs": orgs}


@router.post("")
async def create_org(
    request: Request,
    body: OrganizationCreateRequest,
    response: Response,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session_dependency),
):
    org_manager = OrganizationManager(session)
    slug = slugify(body.slug or body.name)

    existing = await org_manager.get_by_slug(slug)
    if existing is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Slug already taken")

    org = await org_manager.create(name=body.name, slug=slug, is_personal=False)
    await OrganizationMembershipManager(session).add_member(
        organization_id=org.id,
        user_id=user.id,
        role=OrganizationRole.OWNER,
    )

    # Initialize graph indexes for the new org (vector index for semantic search)
    try:
        from sibyl.graph.client import get_graph_client

        client = await get_graph_client()
        await client.ensure_indexes(str(org.id))
    except Exception as e:
        # Don't fail org creation if graph setup fails - indexes will be created lazily
        import structlog

        structlog.get_logger().debug("Graph index setup deferred", org_id=str(org.id), error=str(e))

    access_token = create_access_token(user_id=user.id, organization_id=org.id)
    refresh_token, refresh_expires = create_refresh_token(user_id=user.id, organization_id=org.id)

    # Rotate the current session to keep refresh token org-consistent.
    current = select_access_token(
        authorization=request.headers.get("authorization"),
        cookie_token=request.cookies.get(ACCESS_TOKEN_COOKIE),
    )
    access_expires = datetime.now(UTC) + timedelta(
        minutes=config_module.settings.access_token_expire_minutes
    )
    session_mgr = SessionManager(session)
    if current:
        existing = await session_mgr.get_session_by_token(current)
        if existing is not None:
            await session_mgr.rotate_tokens(
                existing,
                new_access_token=access_token,
                new_access_expires_at=access_expires,
                new_refresh_token=refresh_token,
                new_refresh_expires_at=refresh_expires,
            )
        else:
            await session_mgr.create_session(
                user_id=user.id,
                organization_id=org.id,
                token=access_token,
                expires_at=access_expires,
                refresh_token=refresh_token,
                refresh_token_expires_at=refresh_expires,
                ip_address=request.client.host if request.client else None,
                user_agent=request.headers.get("user-agent"),
            )
    response.status_code = status.HTTP_201_CREATED
    _set_auth_cookies(
        response,
        access_token=access_token,
        refresh_token=refresh_token,
        refresh_expires=refresh_expires,
    )
    await AuditLogger(session).log(
        action="org.create",
        user_id=user.id,
        organization_id=org.id,
        request=request,
        details={"slug": org.slug, "name": org.name},
    )
    return {
        "organization": {"id": str(org.id), "slug": org.slug, "name": org.name},
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": "Bearer",
        "expires_in": config_module.settings.access_token_expire_minutes * 60,
    }


@router.get("/{slug}")
async def get_org(
    slug: str,
    ctx: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session_dependency),
):
    org = await OrganizationManager(session).get_by_slug(slug)
    if org is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")

    member = await OrganizationMembershipManager(session).get_for_user(org.id, ctx.user.id)
    if member is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")

    return {
        "organization": {"id": str(org.id), "slug": org.slug, "name": org.name},
        "role": member.role.value,
    }


@router.patch("/{slug}")
async def update_org(
    request: Request,
    slug: str,
    body: OrganizationUpdateRequest,
    ctx: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session_dependency),
):
    org = await OrganizationManager(session).get_by_slug(slug)
    if org is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")

    member = await OrganizationMembershipManager(session).get_for_user(org.id, ctx.user.id)
    if member is None or member.role not in {OrganizationRole.OWNER, OrganizationRole.ADMIN}:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")

    new_slug = slugify(body.slug) if body.slug else None
    if new_slug and new_slug != org.slug:
        existing = await OrganizationManager(session).get_by_slug(new_slug)
        if existing is not None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Slug already taken")

    updated = await OrganizationManager(session).update(org, name=body.name, slug=new_slug)
    await AuditLogger(session).log(
        action="org.update",
        user_id=ctx.user.id,
        organization_id=updated.id,
        request=request,
        details={"slug": slug, "new_slug": updated.slug, "name": updated.name},
    )
    return {"organization": {"id": str(updated.id), "slug": updated.slug, "name": updated.name}}


@router.delete("/{slug}")
async def delete_org(
    request: Request,
    slug: str,
    ctx: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session_dependency),
):
    org = await OrganizationManager(session).get_by_slug(slug)
    if org is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    if org.is_personal:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cannot delete personal organization",
        )

    member = await OrganizationMembershipManager(session).get_for_user(org.id, ctx.user.id)
    if member is None or member.role != OrganizationRole.OWNER:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")

    await AuditLogger(session).log(
        action="org.delete",
        user_id=ctx.user.id,
        organization_id=org.id,
        request=request,
        details={"slug": org.slug, "name": org.name},
    )
    await OrganizationManager(session).delete(org)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{slug}/switch")
async def switch_org(
    request: Request,
    slug: str,
    response: Response,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session_dependency),
):
    org = await OrganizationManager(session).get_by_slug(slug)
    if org is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")

    member = await OrganizationMembershipManager(session).get_for_user(org.id, user.id)
    if member is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")

    access_token = create_access_token(user_id=user.id, organization_id=org.id)
    refresh_token, refresh_expires = create_refresh_token(user_id=user.id, organization_id=org.id)

    current = select_access_token(
        authorization=request.headers.get("authorization"),
        cookie_token=request.cookies.get(ACCESS_TOKEN_COOKIE),
    )
    access_expires = datetime.now(UTC) + timedelta(
        minutes=config_module.settings.access_token_expire_minutes
    )
    session_mgr = SessionManager(session)
    if current:
        existing = await session_mgr.get_session_by_token(current)
        if existing is not None:
            await session_mgr.rotate_tokens(
                existing,
                new_access_token=access_token,
                new_access_expires_at=access_expires,
                new_refresh_token=refresh_token,
                new_refresh_expires_at=refresh_expires,
            )
        else:
            await session_mgr.create_session(
                user_id=user.id,
                organization_id=org.id,
                token=access_token,
                expires_at=access_expires,
                refresh_token=refresh_token,
                refresh_token_expires_at=refresh_expires,
                ip_address=request.client.host if request.client else None,
                user_agent=request.headers.get("user-agent"),
            )

    _set_auth_cookies(
        response,
        access_token=access_token,
        refresh_token=refresh_token,
        refresh_expires=refresh_expires,
    )
    await AuditLogger(session).log(
        action="org.switch",
        user_id=user.id,
        organization_id=org.id,
        request=request,
        details={"slug": org.slug, "name": org.name},
    )
    return {
        "organization": {"id": str(org.id), "slug": org.slug, "name": org.name},
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": "Bearer",
        "expires_in": config_module.settings.access_token_expire_minutes * 60,
    }
