"""User identity helpers (GitHub-backed for now)."""

from __future__ import annotations

from typing import Self
from uuid import UUID

from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from sibyl.db.models import User


class GitHubUserIdentity(BaseModel):
    """Normalized subset of the GitHub user payload."""

    github_id: int = Field(..., alias="id")
    login: str
    email: str | None = None
    name: str | None = None
    avatar_url: str | None = None


class UserManager:
    """CRUD helpers for `User`."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, user_id: UUID) -> User | None:
        return await self._session.get(User, user_id)

    async def get_by_github_id(self, github_id: int) -> User | None:
        result = await self._session.execute(select(User).where(User.github_id == github_id))
        return result.scalar_one_or_none()

    async def upsert_from_github(self, identity: GitHubUserIdentity) -> User:
        """Create or update a user from a GitHub identity payload.

        Does not commit; caller controls transaction scope.
        """
        existing = await self.get_by_github_id(identity.github_id)
        if existing is None:
            user = User(
                github_id=identity.github_id,
                email=identity.email.lower() if identity.email else None,
                name=identity.name or identity.login,
                avatar_url=identity.avatar_url,
            )
            self._session.add(user)
            return user

        existing.email = identity.email.lower() if identity.email else existing.email
        existing.name = identity.name or existing.name
        existing.avatar_url = identity.avatar_url or existing.avatar_url
        return existing

    async def create_from_github(self, identity: GitHubUserIdentity) -> User:
        """Create a new user from GitHub identity.

        Raises IntegrityError if the user already exists (github_id or email).
        """
        user = User(
            github_id=identity.github_id,
            email=identity.email.lower() if identity.email else None,
            name=identity.name or identity.login,
            avatar_url=identity.avatar_url,
        )
        self._session.add(user)
        try:
            await self._session.flush()
        except IntegrityError:
            raise
        return user

    @classmethod
    def from_session(cls, session: AsyncSession) -> Self:
        return cls(session)
