"""Password reset flow management."""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import ColumnElement
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from sibyl.auth.passwords import hash_password
from sibyl.db.models import LoginHistory, PasswordResetToken, User
from sibyl.email import EmailClient, PasswordResetEmail


def _is_none(column: ColumnElement) -> ColumnElement:  # type: ignore[type-arg]
    """Create an IS NULL comparison for a column."""
    return column.is_(None)  # type: ignore[union-attr]


@dataclass
class ResetTokenResult:
    """Result of creating a password reset token."""

    token: str
    expires_at: datetime


class PasswordResetError(Exception):
    """Password reset operation error."""


class PasswordResetManager:
    """Manages password reset flow."""

    TOKEN_VALID_MINUTES = 60
    RATE_LIMIT_MINUTES = 2

    def __init__(self, session: AsyncSession, email_client: EmailClient) -> None:
        self._session = session
        self._email_client = email_client

    @staticmethod
    def _hash_token(token: str) -> str:
        """Hash a reset token for storage."""
        return hashlib.sha256(token.encode()).hexdigest()

    @staticmethod
    def _generate_token() -> str:
        """Generate a secure random token."""
        return secrets.token_urlsafe(32)

    async def request_reset(
        self,
        email: str,
        *,
        ip_address: str | None = None,
        user_agent: str | None = None,
        reset_url_template: str = "{frontend_url}/reset-password?token={token}",
    ) -> bool:
        """Request a password reset for an email address."""
        from sibyl.config import settings

        normalized_email = email.strip().lower()
        if not normalized_email:
            return True

        result = await self._session.execute(select(User).where(User.email == normalized_email))
        user = result.scalar_one_or_none()

        if user is None:
            await self._log_event(
                user_id=None,
                event_type="password_reset_request",
                success=False,
                failure_reason="user_not_found",
                email_attempted=normalized_email,
                ip_address=ip_address,
                user_agent=user_agent,
            )
            return True

        now = datetime.now(UTC).replace(tzinfo=None)
        rate_limit_cutoff = now - timedelta(minutes=self.RATE_LIMIT_MINUTES)

        result = await self._session.execute(
            select(PasswordResetToken)
            .where(PasswordResetToken.user_id == user.id)
            .where(PasswordResetToken.created_at > rate_limit_cutoff)
            .where(_is_none(PasswordResetToken.revoked_at))
        )
        recent_token = result.scalar_one_or_none()

        if recent_token is not None:
            await self._log_event(
                user_id=user.id,
                event_type="password_reset_request",
                success=False,
                failure_reason="rate_limited",
                email_attempted=normalized_email,
                ip_address=ip_address,
                user_agent=user_agent,
            )
            return True

        result = await self._session.execute(
            select(PasswordResetToken)
            .where(PasswordResetToken.user_id == user.id)
            .where(_is_none(PasswordResetToken.used_at))
            .where(_is_none(PasswordResetToken.revoked_at))
        )
        for old_token in result.scalars():
            old_token.revoked_at = now

        raw_token = self._generate_token()
        token_hash = self._hash_token(raw_token)
        expires_at = now + timedelta(minutes=self.TOKEN_VALID_MINUTES)

        reset_token = PasswordResetToken(
            id=uuid4(),
            user_id=user.id,
            token_hash=token_hash,
            expires_at=expires_at,
            ip_address=ip_address,
            user_agent=user_agent,
        )
        self._session.add(reset_token)
        await self._session.flush()

        reset_url = reset_url_template.format(
            frontend_url=settings.frontend_url.rstrip("/"),
            token=raw_token,
        )

        template = PasswordResetEmail(
            reset_url=reset_url,
            user_name=user.name or None,
            expires_in_minutes=self.TOKEN_VALID_MINUTES,
        )
        await self._email_client.send_template(template, to=user.email or normalized_email)

        await self._log_event(
            user_id=user.id,
            event_type="password_reset_request",
            success=True,
            email_attempted=normalized_email,
            ip_address=ip_address,
            user_agent=user_agent,
        )

        await self._session.commit()
        return True

    async def confirm_reset(
        self,
        token: str,
        new_password: str,
        *,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> bool:
        """Confirm a password reset with a token."""
        token_hash = self._hash_token(token)
        now = datetime.now(UTC).replace(tzinfo=None)

        result = await self._session.execute(
            select(PasswordResetToken).where(PasswordResetToken.token_hash == token_hash)
        )
        reset_token = result.scalar_one_or_none()

        if reset_token is None:
            await self._log_event(
                user_id=None,
                event_type="password_reset_confirm",
                success=False,
                failure_reason="token_not_found",
                ip_address=ip_address,
                user_agent=user_agent,
            )
            raise PasswordResetError("Invalid or expired reset link")

        if reset_token.used_at is not None:
            raise PasswordResetError("This reset link has already been used")

        if reset_token.revoked_at is not None:
            raise PasswordResetError("This reset link has been revoked")

        if reset_token.expires_at < now:
            raise PasswordResetError("This reset link has expired")

        user = await self._session.get(User, reset_token.user_id)
        if user is None:
            raise PasswordResetError("User not found")

        pw = hash_password(new_password)
        user.password_salt = pw.salt_hex
        user.password_hash = pw.hash_hex
        user.password_iterations = pw.iterations

        reset_token.used_at = now

        await self._log_event(
            user_id=user.id,
            event_type="password_reset_confirm",
            success=True,
            ip_address=ip_address,
            user_agent=user_agent,
        )

        await self._session.commit()
        return True

    async def _log_event(
        self,
        *,
        user_id: UUID | None,
        event_type: str,
        success: bool,
        failure_reason: str | None = None,
        email_attempted: str | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> None:
        """Log a password reset event."""
        log = LoginHistory(
            id=uuid4(),
            user_id=user_id,
            event_type=event_type,
            auth_method="password_reset",
            success=success,
            failure_reason=failure_reason,
            email_attempted=email_attempted,
            ip_address=ip_address,
            user_agent=user_agent,
        )
        self._session.add(log)
