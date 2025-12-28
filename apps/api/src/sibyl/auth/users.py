"""User identity helpers (GitHub OAuth and local password auth)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Self

from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from sibyl.auth.passwords import PasswordError, hash_password, verify_password
from sibyl.db.models import User

if TYPE_CHECKING:
    from uuid import UUID


@dataclass(frozen=True, slots=True)
class PasswordChange:
    current_password: str | None
    new_password: str


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

    async def get_by_email(self, email: str) -> User | None:
        normalized = email.strip().lower()
        if not normalized:
            return None
        result = await self._session.execute(select(User).where(User.email == normalized))
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

    async def create_local_user(self, *, email: str, password: str, name: str) -> User:
        normalized = email.strip().lower()
        if not normalized:
            raise ValueError("Email is required")
        if not name.strip():
            raise ValueError("Name is required")

        existing = await self.get_by_email(normalized)
        if existing is not None:
            raise ValueError("Email is already in use")

        pw = hash_password(password)
        user = User(
            github_id=None,
            email=normalized,
            name=name.strip(),
            avatar_url=None,
            password_salt=pw.salt_hex,
            password_hash=pw.hash_hex,
            password_iterations=pw.iterations,
        )
        self._session.add(user)
        await self._session.flush()
        return user

    async def authenticate_local(self, *, email: str, password: str) -> User | None:
        normalized = email.strip().lower()
        if not normalized:
            return None
        user = await self.get_by_email(normalized)
        if user is None:
            return None
        if not user.password_salt or not user.password_hash or not user.password_iterations:
            return None
        try:
            ok = verify_password(
                password,
                salt_hex=user.password_salt,
                hash_hex=user.password_hash,
                iterations=int(user.password_iterations),
            )
        except PasswordError:
            return None
        return user if ok else None

    async def update_profile(
        self,
        user: User,
        *,
        email: str | None = None,
        name: str | None = None,
        avatar_url: str | None = None,
    ) -> User:
        if email is not None:
            normalized = email.strip().lower()
            if not normalized:
                raise ValueError("Email is required")
            existing = await self.get_by_email(normalized)
            if existing is not None and existing.id != user.id:
                raise ValueError("Email is already in use")
            user.email = normalized

        if name is not None:
            normalized_name = name.strip()
            if not normalized_name:
                raise ValueError("Name is required")
            user.name = normalized_name

        if avatar_url is not None:
            user.avatar_url = avatar_url.strip() or None

        await self._session.flush()
        return user

    async def change_password(self, user: User, change: PasswordChange) -> User:
        if user.password_salt and user.password_hash and user.password_iterations:
            if not change.current_password:
                raise ValueError("Current password is required")
            try:
                ok = verify_password(
                    change.current_password,
                    salt_hex=user.password_salt,
                    hash_hex=user.password_hash,
                    iterations=int(user.password_iterations),
                )
            except PasswordError as e:
                raise ValueError("Invalid current password") from e
            if not ok:
                raise ValueError("Invalid current password")

        pw = hash_password(change.new_password)
        user.password_salt = pw.salt_hex
        user.password_hash = pw.hash_hex
        user.password_iterations = pw.iterations
        await self._session.flush()
        return user

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
        await self._session.flush()
        return user

    @classmethod
    def from_session(cls, session: AsyncSession) -> Self:
        return cls(session)
