"""Organization invitation endpoints."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from sibyl import config as config_module
from sibyl.auth.audit import AuditLogger
from sibyl.auth.dependencies import get_current_user
from sibyl.auth.http import select_access_token
from sibyl.auth.invitations import InvitationError, InvitationManager
from sibyl.auth.jwt import create_access_token, create_refresh_token
from sibyl.auth.memberships import OrganizationMembershipManager
from sibyl.auth.organizations import OrganizationManager
from sibyl.auth.sessions import SessionManager
from sibyl.db.connection import get_session_dependency
from sibyl.db.models import OrganizationRole, User

router = APIRouter(prefix="/orgs/{slug}/invitations", tags=["org-invitations"])
invitations_router = APIRouter(prefix="/invitations", tags=["invitations"])


class InvitationCreateRequest(BaseModel):
    email: str = Field(..., min_length=3, max_length=255)
    role: OrganizationRole = Field(default=OrganizationRole.MEMBER)
    expires_days: int = Field(default=7, ge=1, le=30)


def _cookie_secure() -> bool:
    if config_module.settings.cookie_secure is not None:
        return bool(config_module.settings.cookie_secure)
    return config_module.settings.server_url.startswith("https://")


def _set_access_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        "sibyl_access_token",
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
        "sibyl_refresh_token",
        refresh_token,
        httponly=True,
        secure=_cookie_secure(),
        samesite="lax",
        max_age=max(refresh_max_age, 0),
        domain=config_module.settings.cookie_domain,
        path="/",
    )


async def _require_org_admin(
    *,
    slug: str,
    user: User,
    session: AsyncSession,
) -> UUID:
    org = await OrganizationManager(session).get_by_slug(slug)
    if org is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    member = await OrganizationMembershipManager(session).get_for_user(org.id, user.id)
    if member is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    if member.role not in {OrganizationRole.OWNER, OrganizationRole.ADMIN}:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")
    return org.id


@router.get("")
async def list_invitations(
    slug: str,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session_dependency),
):
    org_id = await _require_org_admin(slug=slug, user=user, session=session)
    invites = await InvitationManager(session).list_for_org(org_id, include_accepted=False)
    return {
        "invitations": [
            {
                "id": str(i.id),
                "email": i.invited_email,
                "role": i.invited_role.value,
                "created_at": i.created_at,
                "expires_at": i.expires_at,
            }
            for i in invites
        ]
    }


@router.post("")
async def create_invitation(
    request: Request,
    slug: str,
    body: InvitationCreateRequest,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session_dependency),
):
    org_id = await _require_org_admin(slug=slug, user=user, session=session)
    invite = await InvitationManager(session).create(
        organization_id=org_id,
        invited_email=body.email,
        invited_role=body.role,
        created_by_user_id=user.id,
        expires_in=timedelta(days=body.expires_days),
    )
    accept_url = f"{config_module.settings.server_url}/api/invitations/{invite.token}/accept"
    await AuditLogger(session).log(
        action="org.invitation.create",
        user_id=user.id,
        organization_id=org_id,
        request=request,
        details={
            "invitation_id": str(invite.id),
            "email": invite.invited_email,
            "role": invite.invited_role.value,
        },
    )
    return {
        "invitation": {
            "id": str(invite.id),
            "email": invite.invited_email,
            "role": invite.invited_role.value,
            "expires_at": invite.expires_at,
            "accept_url": accept_url,
        }
    }


@router.delete("/{invitation_id}")
async def delete_invitation(
    request: Request,
    slug: str,
    invitation_id: UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session_dependency),
):
    org_id = await _require_org_admin(slug=slug, user=user, session=session)
    await InvitationManager(session).delete(invitation_id)
    await AuditLogger(session).log(
        action="org.invitation.delete",
        user_id=user.id,
        organization_id=org_id,
        request=request,
        details={"invitation_id": str(invitation_id), "slug": slug},
    )
    return {"success": True}


@invitations_router.post("/{token}/accept")
async def accept_invitation(
    request: Request,
    token: str,
    response: Response,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session_dependency),
):
    try:
        invite = await InvitationManager(session).accept(token=token, user=user)
    except InvitationError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e

    access_token = create_access_token(user_id=user.id, organization_id=invite.organization_id)
    refresh_token, refresh_expires = create_refresh_token(
        user_id=user.id, organization_id=invite.organization_id
    )

    # Rotate the current session to keep refresh token org-consistent.
    current = select_access_token(
        authorization=request.headers.get("authorization"),
        cookie_token=request.cookies.get("sibyl_access_token"),
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
                organization_id=invite.organization_id,
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
        action="org.invitation.accept",
        user_id=user.id,
        organization_id=invite.organization_id,
        request=request,
        details={"invitation_id": str(invite.id)},
    )
    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": "Bearer",
        "expires_in": config_module.settings.access_token_expire_minutes * 60,
        "organization_id": str(invite.organization_id),
    }
