"""Organization invitation endpoints."""

from __future__ import annotations

from datetime import timedelta
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from sibyl import config as config_module
from sibyl.auth.dependencies import get_current_user
from sibyl.auth.invitations import InvitationError, InvitationManager
from sibyl.auth.jwt import create_access_token
from sibyl.auth.memberships import OrganizationMembershipManager
from sibyl.auth.organizations import OrganizationManager
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
        max_age=int(timedelta(hours=config_module.settings.jwt_expiry_hours).total_seconds()),
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
    slug: str,
    invitation_id: UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session_dependency),
):
    _ = await _require_org_admin(slug=slug, user=user, session=session)
    await InvitationManager(session).delete(invitation_id)
    return {"success": True}


@invitations_router.post("/{token}/accept")
async def accept_invitation(
    token: str,
    response: Response,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session_dependency),
):
    try:
        invite = await InvitationManager(session).accept(token=token, user=user)
    except InvitationError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e

    access = create_access_token(user_id=user.id, organization_id=invite.organization_id)
    _set_access_cookie(response, access)
    return {"access_token": access, "organization_id": str(invite.organization_id)}
