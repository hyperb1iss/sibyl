"""Organization helpers and CRUD."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Self

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from sibyl.db.models import Organization, User

if TYPE_CHECKING:
    from uuid import UUID

_SLUG_SAFE = re.compile(r"[^a-z0-9]+")


def slugify(name: str) -> str:
    slug = name.strip().lower()
    slug = _SLUG_SAFE.sub("-", slug).strip("-")
    return slug or "org"


class OrganizationManager:
    """CRUD helpers for `Organization`."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    @classmethod
    def from_session(cls, session: AsyncSession) -> Self:
        return cls(session)

    async def get_by_id(self, org_id: UUID) -> Organization | None:
        return await self._session.get(Organization, org_id)

    async def get_by_slug(self, slug: str) -> Organization | None:
        result = await self._session.execute(select(Organization).where(Organization.slug == slug))
        return result.scalar_one_or_none()

    async def list(self, limit: int = 100) -> list[Organization]:
        result = await self._session.execute(select(Organization).limit(limit))
        return list(result.scalars().all())

    async def create(
        self,
        *,
        name: str,
        slug: str | None = None,
        is_personal: bool = False,
        settings: dict[str, Any] | None = None,
    ) -> Organization:
        org = Organization(
            name=name,
            slug=slugify(slug or name),
            is_personal=is_personal,
            settings=settings or {},
        )
        self._session.add(org)
        await self._session.flush()
        return org

    async def update(
        self,
        org: Organization,
        *,
        name: str | None = None,
        slug: str | None = None,
        settings: dict[str, Any] | None = None,
    ) -> Organization:
        if name is not None:
            org.name = name
        if slug is not None:
            org.slug = slugify(slug)
        if settings is not None:
            org.settings = settings
        self._session.add(org)
        return org

    async def delete(self, org: Organization) -> None:
        await self._session.delete(org)

    async def create_personal_for_user(self, user: User) -> Organization:
        """Create a personal organization for a user.

        Uses a deterministic slug so repeated calls are naturally idempotent.
        """
        suffix = str(user.github_id) if user.github_id is not None else str(user.id)
        slug = f"u-{suffix}"
        existing = await self.get_by_slug(slug)
        if existing is not None:
            return existing
        return await self.create(
            name=user.name or f"User {suffix}",
            slug=slug,
            is_personal=True,
            settings={},
        )
