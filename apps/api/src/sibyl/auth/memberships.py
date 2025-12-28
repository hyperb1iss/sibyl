"""Organization membership helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING, Self

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import func
from sqlmodel import select

from sibyl.db.models import OrganizationMember, OrganizationRole

if TYPE_CHECKING:
    from uuid import UUID


class OrganizationMembershipManager:
    """CRUD + invariants for `OrganizationMember`."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    @classmethod
    def from_session(cls, session: AsyncSession) -> Self:
        return cls(session)

    async def get(self, membership_id: UUID) -> OrganizationMember | None:
        return await self._session.get(OrganizationMember, membership_id)

    async def get_for_user(self, organization_id: UUID, user_id: UUID) -> OrganizationMember | None:
        result = await self._session.execute(
            select(OrganizationMember).where(
                OrganizationMember.organization_id == organization_id,
                OrganizationMember.user_id == user_id,
            )
        )
        return result.scalar_one_or_none()

    async def list_for_org(self, organization_id: UUID) -> list[OrganizationMember]:
        result = await self._session.execute(
            select(OrganizationMember).where(OrganizationMember.organization_id == organization_id)
        )
        return list(result.scalars().all())

    async def add_member(
        self,
        *,
        organization_id: UUID,
        user_id: UUID,
        role: OrganizationRole = OrganizationRole.MEMBER,
    ) -> OrganizationMember:
        existing = await self.get_for_user(organization_id, user_id)
        if existing is not None:
            existing.role = role
            self._session.add(existing)
            return existing

        membership = OrganizationMember(
            organization_id=organization_id,
            user_id=user_id,
            role=role,
        )
        self._session.add(membership)
        return membership

    async def remove_member(self, *, organization_id: UUID, user_id: UUID) -> None:
        membership = await self.get_for_user(organization_id, user_id)
        if membership is None:
            return

        if membership.role == OrganizationRole.OWNER:
            owners = await self._count_owners(organization_id)
            if owners <= 1:
                raise ValueError("Cannot remove the last organization owner")

        await self._session.delete(membership)

    async def set_role(
        self,
        *,
        organization_id: UUID,
        user_id: UUID,
        role: OrganizationRole,
    ) -> OrganizationMember:
        membership = await self.get_for_user(organization_id, user_id)
        if membership is None:
            raise ValueError("User is not a member of this organization")

        if membership.role == OrganizationRole.OWNER and role != OrganizationRole.OWNER:
            owners = await self._count_owners(organization_id)
            if owners <= 1:
                raise ValueError("Cannot demote the last organization owner")

        membership.role = role
        self._session.add(membership)
        return membership

    async def _count_owners(self, organization_id: UUID) -> int:
        result = await self._session.execute(
            select(func.count())
            .select_from(OrganizationMember)
            .where(
                OrganizationMember.organization_id == organization_id,
                OrganizationMember.role == OrganizationRole.OWNER,
            )
        )
        return int(result.scalar() or 0)
