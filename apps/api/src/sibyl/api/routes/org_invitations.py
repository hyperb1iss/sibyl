"""Organization invitation endpoints."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field

from sibyl import config as config_module
from sibyl.auth.dependencies import get_current_user
from sibyl.db.models import OrganizationRole, User
from sibyl.persistence.legacy.org_invitations import (
    accept_legacy_org_invitation,
    create_legacy_org_invitation,
    delete_legacy_org_invitation,
    list_legacy_org_invitations,
)

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


@router.get("")
async def list_invitations(
    slug: str,
    user: User = Depends(get_current_user),
):
    invites = await list_legacy_org_invitations(slug=slug, actor_id=user.id)
    return {
        "invitations": [
            {
                "id": str(invite.id),
                "email": invite.email,
                "role": invite.role.value,
                "created_at": invite.created_at,
                "expires_at": invite.expires_at,
            }
            for invite in invites
        ]
    }


@router.post("")
async def create_invitation(
    request: Request,
    slug: str,
    body: InvitationCreateRequest,
    user: User = Depends(get_current_user),
):
    invite = await create_legacy_org_invitation(
        slug=slug,
        actor_id=user.id,
        email=body.email,
        role=body.role,
        expires_days=body.expires_days,
        request=request,
    )
    return {
        "invitation": {
            "id": str(invite.id),
            "email": invite.email,
            "role": invite.role.value,
            "expires_at": invite.expires_at,
            "accept_url": invite.accept_url,
        }
    }


@router.delete("/{invitation_id}")
async def delete_invitation(
    request: Request,
    slug: str,
    invitation_id: UUID,
    user: User = Depends(get_current_user),
):
    await delete_legacy_org_invitation(
        slug=slug,
        actor_id=user.id,
        invitation_id=invitation_id,
        request=request,
    )
    return {"success": True}


@invitations_router.post("/{token}/accept")
async def accept_invitation(
    request: Request,
    token: str,
    response: Response,
    user: User = Depends(get_current_user),
):
    accepted = await accept_legacy_org_invitation(token=token, user=user, request=request)

    _set_auth_cookies(
        response,
        access_token=accepted.access_token,
        refresh_token=accepted.refresh_token,
        refresh_expires=accepted.refresh_expires,
    )
    return {
        "access_token": accepted.access_token,
        "refresh_token": accepted.refresh_token,
        "token_type": "Bearer",
        "expires_in": config_module.settings.access_token_expire_minutes * 60,
        "organization_id": str(accepted.organization_id),
    }
