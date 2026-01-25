"""User session management."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import ColumnElement
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from sibyl.db.models import UserSession


def _is_none(column: ColumnElement) -> ColumnElement:
    """Create an IS NULL comparison for a column."""
    return column.is_(None)


def _is_true(column: ColumnElement) -> ColumnElement:
    """Create an IS TRUE comparison for a column."""
    return column.is_(True)


def _desc(column: ColumnElement) -> ColumnElement:
    """Create a DESC order for a column."""
    return column.desc()


class SessionManager:
    """Manages user sessions for tracking and revocation."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    @staticmethod
    def hash_token(token: str) -> str:
        """Create SHA256 hash of a token for storage."""
        return hashlib.sha256(token.encode()).hexdigest()

    async def create_session(
        self,
        *,
        user_id: UUID,
        token: str,
        expires_at: datetime,
        organization_id: UUID | None = None,
        refresh_token: str | None = None,
        refresh_token_expires_at: datetime | None = None,
        device_name: str | None = None,
        device_type: str | None = None,
        browser: str | None = None,
        os: str | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
        location: str | None = None,
    ) -> UserSession:
        """Create a new user session with optional refresh token."""
        token_hash = self.hash_token(token)
        refresh_hash = self.hash_token(refresh_token) if refresh_token else None
        refresh_exp = (
            refresh_token_expires_at.replace(tzinfo=None)
            if refresh_token_expires_at and refresh_token_expires_at.tzinfo
            else refresh_token_expires_at
        )

        session_record = UserSession(
            id=uuid4(),
            user_id=user_id,
            organization_id=organization_id,
            token_hash=token_hash,
            refresh_token_hash=refresh_hash,
            refresh_token_expires_at=refresh_exp,
            device_name=device_name,
            device_type=device_type,
            browser=browser,
            os=os,
            ip_address=ip_address,
            user_agent=user_agent,
            location=location,
            is_current=False,
            last_active_at=datetime.now(UTC).replace(tzinfo=None),
            expires_at=expires_at if expires_at.tzinfo is None else expires_at.replace(tzinfo=None),
        )
        self._session.add(session_record)
        await self._session.flush()
        return session_record

    async def get_session_by_token(self, token: str) -> UserSession | None:
        """Get a session by raw token."""
        token_hash = self.hash_token(token)
        result = await self._session.execute(
            select(UserSession)
            .where(UserSession.token_hash == token_hash)
            .where(_is_none(UserSession.revoked_at))
        )
        return result.scalar_one_or_none()

    async def get_session_by_refresh_token(self, refresh_token: str) -> UserSession | None:
        """Get a session by refresh token (for token rotation)."""
        refresh_hash = self.hash_token(refresh_token)
        now = datetime.now(UTC).replace(tzinfo=None)
        result = await self._session.execute(
            select(UserSession)
            .where(UserSession.refresh_token_hash == refresh_hash)
            .where(_is_none(UserSession.revoked_at))
            .where(UserSession.refresh_token_expires_at.is_not(None))
            .where(UserSession.refresh_token_expires_at > now)
        )
        return result.scalar_one_or_none()

    async def rotate_tokens(
        self,
        session: UserSession,
        *,
        new_access_token: str,
        new_access_expires_at: datetime,
        new_refresh_token: str,
        new_refresh_expires_at: datetime,
    ) -> UserSession:
        """Rotate both access and refresh tokens for a session."""
        session.token_hash = self.hash_token(new_access_token)
        session.expires_at = (
            new_access_expires_at.replace(tzinfo=None)
            if new_access_expires_at.tzinfo
            else new_access_expires_at
        )
        session.refresh_token_hash = self.hash_token(new_refresh_token)
        session.refresh_token_expires_at = (
            new_refresh_expires_at.replace(tzinfo=None)
            if new_refresh_expires_at.tzinfo
            else new_refresh_expires_at
        )
        session.last_active_at = datetime.now(UTC).replace(tzinfo=None)
        await self._session.flush()
        return session

    async def list_user_sessions(
        self,
        user_id: UUID,
        *,
        include_expired: bool = False,
    ) -> list[UserSession]:
        """List all sessions for a user."""
        query = (
            select(UserSession)
            .where(UserSession.user_id == user_id)
            .where(_is_none(UserSession.revoked_at))
            .order_by(_desc(UserSession.last_active_at))
        )

        if not include_expired:
            now = datetime.now(UTC).replace(tzinfo=None)
            query = query.where(UserSession.expires_at > now)

        result = await self._session.execute(query)
        return list(result.scalars().all())

    async def update_activity(self, token: str) -> bool:
        """Update last_active_at for a session."""
        session = await self.get_session_by_token(token)
        if session is None:
            return False

        session.last_active_at = datetime.now(UTC).replace(tzinfo=None)
        return True

    async def mark_current(self, token: str) -> bool:
        """Mark a session as the current one for its user."""
        session = await self.get_session_by_token(token)
        if session is None:
            return False

        result = await self._session.execute(
            select(UserSession)
            .where(UserSession.user_id == session.user_id)
            .where(_is_true(UserSession.is_current))
        )
        for s in result.scalars():
            s.is_current = False

        session.is_current = True
        return True

    async def revoke_session(self, session_id: UUID, user_id: UUID) -> bool:
        """Revoke a specific session."""
        result = await self._session.execute(
            select(UserSession)
            .where(UserSession.id == session_id)
            .where(UserSession.user_id == user_id)
            .where(_is_none(UserSession.revoked_at))
        )
        session = result.scalar_one_or_none()

        if session is None:
            return False

        session.revoked_at = datetime.now(UTC).replace(tzinfo=None)
        await self._session.commit()
        return True

    async def revoke_all_sessions(
        self,
        user_id: UUID,
        *,
        exclude_token_hash: str | None = None,
    ) -> int:
        """Revoke all sessions for a user."""
        query = (
            select(UserSession)
            .where(UserSession.user_id == user_id)
            .where(_is_none(UserSession.revoked_at))
        )

        if exclude_token_hash:
            query = query.where(UserSession.token_hash != exclude_token_hash)

        result = await self._session.execute(query)
        sessions = result.scalars().all()

        now = datetime.now(UTC).replace(tzinfo=None)
        count = 0
        for session in sessions:
            session.revoked_at = now
            count += 1

        await self._session.commit()
        return count

    async def cleanup_expired(self, *, older_than_days: int = 30) -> int:
        """Delete expired sessions older than specified days."""
        from datetime import timedelta

        cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=older_than_days)

        result = await self._session.execute(
            select(UserSession).where(UserSession.expires_at < cutoff)
        )
        sessions = result.scalars().all()

        count = 0
        for session in sessions:
            await self._session.delete(session)
            count += 1

        await self._session.commit()
        return count
