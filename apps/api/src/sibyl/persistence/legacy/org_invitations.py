"""Legacy organization invitation adapters backed by the current auth runtime."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi import HTTPException, status
from starlette.requests import Request

from sibyl import config as config_module
from sibyl.auth.audit import AuditLogger
from sibyl.auth.http import select_access_token
from sibyl.auth.invitations import InvitationError, InvitationManager
from sibyl.auth.jwt import create_access_token, create_refresh_token
from sibyl.auth.memberships import OrganizationMembershipManager
from sibyl.auth.organizations import OrganizationManager
from sibyl.auth.sessions import SessionManager
from sibyl.db.connection import get_session
from sibyl.db.models import OrganizationRole, User
from sibyl.persistence.organization_common import (
    InvitationAcceptance,
    InvitationRecord,
)

LegacyInvitationRecord = InvitationRecord
LegacyInvitationAcceptance = InvitationAcceptance


async def _require_org_admin(*, slug: str, user_id: UUID) -> UUID:
    async with get_session() as session:
        org = await OrganizationManager.from_session(session).get_by_slug(slug)
        if org is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
        member = await OrganizationMembershipManager.from_session(session).get_for_user(
            org.id,
            user_id,
        )
        if member is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
        if member.role not in {OrganizationRole.OWNER, OrganizationRole.ADMIN}:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")
        return org.id


async def list_legacy_org_invitations(
    *,
    slug: str,
    actor_id: UUID,
) -> list[LegacyInvitationRecord]:
    org_id = await _require_org_admin(slug=slug, user_id=actor_id)

    async with get_session() as session:
        invites = await InvitationManager(session).list_for_org(org_id, include_accepted=False)
        return [
            LegacyInvitationRecord(
                id=invite.id,
                email=invite.invited_email,
                role=invite.invited_role,
                created_at=invite.created_at,
                expires_at=invite.expires_at,
            )
            for invite in invites
        ]


async def create_legacy_org_invitation(
    *,
    slug: str,
    actor_id: UUID,
    email: str,
    role: OrganizationRole,
    expires_days: int,
    request: Request,
) -> LegacyInvitationRecord:
    org_id = await _require_org_admin(slug=slug, user_id=actor_id)

    async with get_session() as session:
        invite = await InvitationManager(session).create(
            organization_id=org_id,
            invited_email=email,
            invited_role=role,
            created_by_user_id=actor_id,
            expires_in=timedelta(days=expires_days),
        )
        await AuditLogger(session).log(
            action="org.invitation.create",
            user_id=actor_id,
            organization_id=org_id,
            request=request,
            details={
                "invitation_id": str(invite.id),
                "email": invite.invited_email,
                "role": invite.invited_role.value,
            },
        )
        return LegacyInvitationRecord(
            id=invite.id,
            email=invite.invited_email,
            role=invite.invited_role,
            expires_at=invite.expires_at,
            accept_url=(
                f"{config_module.settings.server_url}/api/invitations/{invite.token}/accept"
            ),
        )


async def delete_legacy_org_invitation(
    *,
    slug: str,
    actor_id: UUID,
    invitation_id: UUID,
    request: Request,
) -> None:
    org_id = await _require_org_admin(slug=slug, user_id=actor_id)

    async with get_session() as session:
        await InvitationManager(session).delete(invitation_id)
        await AuditLogger(session).log(
            action="org.invitation.delete",
            user_id=actor_id,
            organization_id=org_id,
            request=request,
            details={"invitation_id": str(invitation_id), "slug": slug},
        )


async def accept_legacy_org_invitation(
    *,
    token: str,
    user: User,
    request: Request,
) -> LegacyInvitationAcceptance:
    async with get_session() as session:
        try:
            invite = await InvitationManager(session).accept(token=token, user=user)
        except InvitationError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

        access_token = create_access_token(user_id=user.id, organization_id=invite.organization_id)
        refresh_token, refresh_expires = create_refresh_token(
            user_id=user.id,
            organization_id=invite.organization_id,
        )

        current = select_access_token(
            authorization=request.headers.get("authorization"),
            cookie_token=request.cookies.get("sibyl_access_token"),
        )
        access_expires = datetime.now(UTC) + timedelta(
            minutes=config_module.settings.access_token_expire_minutes
        )
        session_manager = SessionManager(session)
        if current:
            existing = await session_manager.get_session_by_token(current)
            if existing is not None:
                await session_manager.rotate_tokens(
                    existing,
                    new_access_token=access_token,
                    new_access_expires_at=access_expires,
                    new_refresh_token=refresh_token,
                    new_refresh_expires_at=refresh_expires,
                )
            else:
                await session_manager.create_session(
                    user_id=user.id,
                    organization_id=invite.organization_id,
                    token=access_token,
                    expires_at=access_expires,
                    refresh_token=refresh_token,
                    refresh_token_expires_at=refresh_expires,
                    ip_address=request.client.host if request.client else None,
                    user_agent=request.headers.get("user-agent"),
                )

        await AuditLogger(session).log(
            action="org.invitation.accept",
            user_id=user.id,
            organization_id=invite.organization_id,
            request=request,
            details={"invitation_id": str(invite.id)},
        )
        return LegacyInvitationAcceptance(
            access_token=access_token,
            refresh_token=refresh_token,
            refresh_expires=refresh_expires,
            organization_id=invite.organization_id,
            invitation_id=invite.id,
        )


list_org_invitations = list_legacy_org_invitations
create_org_invitation = create_legacy_org_invitation
delete_org_invitation = delete_legacy_org_invitation
accept_org_invitation = accept_legacy_org_invitation
