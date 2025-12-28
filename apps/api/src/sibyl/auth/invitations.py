"""Organization invitation helpers."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Self

from sqlalchemy import ColumnElement
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from sibyl.auth.memberships import OrganizationMembershipManager
from sibyl.db.models import OrganizationInvitation, OrganizationRole, User

if TYPE_CHECKING:
    from uuid import UUID


def _is_none(column: ColumnElement) -> ColumnElement:  # type: ignore[type-arg]
    """Create an IS NULL comparison for a column."""
    return column.is_(None)  # type: ignore[union-attr]


def _desc(column: ColumnElement) -> ColumnElement:  # type: ignore[type-arg]
    """Create a DESC order for a column."""
    return column.desc()  # type: ignore[union-attr]


class InvitationError(ValueError):
    """Invitation error."""


def generate_invite_token() -> str:
    return secrets.token_urlsafe(48)


def utcnow_naive() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class InvitationManager:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    @classmethod
    def from_session(cls, session: AsyncSession) -> Self:
        return cls(session)

    async def list_for_org(
        self, organization_id: UUID, *, include_accepted: bool = False
    ) -> list[OrganizationInvitation]:
        stmt = select(OrganizationInvitation).where(
            OrganizationInvitation.organization_id == organization_id
        )
        if not include_accepted:
            stmt = stmt.where(_is_none(OrganizationInvitation.accepted_at))
        stmt = stmt.order_by(_desc(OrganizationInvitation.created_at))
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def create(
        self,
        *,
        organization_id: UUID,
        invited_email: str,
        invited_role: OrganizationRole,
        created_by_user_id: UUID,
        expires_in: timedelta | None = timedelta(days=7),
    ) -> OrganizationInvitation:
        token = generate_invite_token()
        expires_at = None
        if expires_in is not None:
            expires_at = utcnow_naive() + expires_in

        invitation = OrganizationInvitation(
            organization_id=organization_id,
            invited_email=invited_email.lower().strip(),
            invited_role=invited_role,
            token=token,
            created_by_user_id=created_by_user_id,
            expires_at=expires_at,
        )
        self._session.add(invitation)
        await self._session.flush()
        return invitation

    async def delete(self, invitation_id: UUID) -> None:
        invite = await self._session.get(OrganizationInvitation, invitation_id)
        if invite is None:
            return
        await self._session.delete(invite)

    async def get_by_token(self, token: str) -> OrganizationInvitation | None:
        result = await self._session.execute(
            select(OrganizationInvitation).where(OrganizationInvitation.token == token)
        )
        return result.scalar_one_or_none()

    async def accept(self, *, token: str, user: User) -> OrganizationInvitation:
        invite = await self.get_by_token(token)
        if invite is None:
            raise InvitationError("Invitation not found")
        if invite.accepted_at is not None:
            raise InvitationError("Invitation already accepted")
        if invite.expires_at is not None and invite.expires_at < utcnow_naive():
            raise InvitationError("Invitation expired")

        if invite.invited_email and (user.email or "").lower() != invite.invited_email.lower():
            raise InvitationError("Invitation email does not match current user")

        await OrganizationMembershipManager(self._session).add_member(
            organization_id=invite.organization_id,
            user_id=user.id,
            role=invite.invited_role,
        )

        invite.accepted_at = utcnow_naive()
        invite.accepted_by_user_id = user.id
        self._session.add(invite)
        return invite
