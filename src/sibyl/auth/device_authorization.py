"""Device authorization grant helpers (RFC 8628-style).

Implements a small, server-managed device code flow for the CLI:
- Client starts: POST /api/auth/device -> device_code + user_code + verify URL
- User approves: GET/POST /api/auth/device/verify
- Client polls: POST /api/auth/device/token -> access_token or oauth-style errors

Storage is PostgreSQL-backed via DeviceAuthorizationRequest.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Self
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from sibyl import config as config_module
from sibyl.auth.jwt import create_access_token
from sibyl.db.models import DeviceAuthorizationRequest

_USER_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # avoid 0/O, 1/I


def _utcnow_naive() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _hash_device_code(device_code: str) -> str:
    return hashlib.sha256(device_code.encode("utf-8")).hexdigest()


def generate_user_code() -> str:
    """Generate a short, human-friendly code like ABCD-EFGH."""
    raw = "".join(secrets.choice(_USER_CODE_ALPHABET) for _ in range(8))
    return raw[:4] + "-" + raw[4:]


def normalize_user_code(value: str | None) -> str | None:
    """Normalize user code input to canonical form (ABCD-EFGH) or return None."""
    if not value:
        return None
    cleaned = value.strip().upper().replace(" ", "").replace("-", "")
    if len(cleaned) != 8:
        return None
    if any(ch not in _USER_CODE_ALPHABET for ch in cleaned):
        return None
    return cleaned[:4] + "-" + cleaned[4:]


@dataclass(frozen=True)
class DeviceTokenError(Exception):
    error: str
    error_description: str | None = None


class DeviceAuthorizationManager:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    @classmethod
    def from_session(cls, session: AsyncSession) -> Self:
        return cls(session)

    async def start(
        self,
        *,
        client_name: str | None = None,
        scope: str = "mcp",
        expires_in: timedelta = timedelta(minutes=10),
        poll_interval_seconds: int = 5,
    ) -> tuple[DeviceAuthorizationRequest, str]:
        """Create and persist a device authorization request.

        Returns the persisted request plus the raw device_code (not stored).
        """
        now = _utcnow_naive()
        expires_at = now + expires_in

        for _ in range(20):
            device_code = secrets.token_urlsafe(32)
            user_code = generate_user_code()
            device_hash = _hash_device_code(device_code)

            existing = await self._session.execute(
                select(DeviceAuthorizationRequest).where(
                    (DeviceAuthorizationRequest.device_code_hash == device_hash)
                    | (DeviceAuthorizationRequest.user_code == user_code)
                )
            )
            if existing.scalar_one_or_none() is None:
                req = DeviceAuthorizationRequest(
                    device_code_hash=device_hash,
                    user_code=user_code,
                    client_name=(client_name or "").strip() or None,
                    scope=(scope or "").strip() or "mcp",
                    status="pending",
                    poll_interval_seconds=max(1, int(poll_interval_seconds)),
                    expires_at=expires_at,
                )
                self._session.add(req)
                await self._session.flush()
                return req, device_code

        raise RuntimeError("Failed to allocate unique device/user codes")

    async def get_by_user_code(self, user_code: str) -> DeviceAuthorizationRequest | None:
        result = await self._session.execute(
            select(DeviceAuthorizationRequest).where(DeviceAuthorizationRequest.user_code == user_code)
        )
        return result.scalar_one_or_none()

    async def get_by_device_code(self, device_code: str) -> DeviceAuthorizationRequest | None:
        device_hash = _hash_device_code(device_code)
        result = await self._session.execute(
            select(DeviceAuthorizationRequest).where(
                DeviceAuthorizationRequest.device_code_hash == device_hash
            )
        )
        return result.scalar_one_or_none()

    async def approve(
        self,
        req: DeviceAuthorizationRequest,
        *,
        user_id: UUID,
        organization_id: UUID | None,
    ) -> DeviceAuthorizationRequest:
        if req.status != "pending":
            return req
        req.status = "approved"
        req.approved_at = _utcnow_naive()
        req.user_id = user_id
        req.organization_id = organization_id
        self._session.add(req)
        return req

    async def deny(self, req: DeviceAuthorizationRequest) -> DeviceAuthorizationRequest:
        if req.status != "pending":
            return req
        req.status = "denied"
        req.denied_at = _utcnow_naive()
        self._session.add(req)
        return req

    async def exchange_device_code(
        self,
        *,
        device_code: str,
        min_interval_seconds: int | None = None,
    ) -> dict[str, object]:
        """Poll for completion; returns token response or raises DeviceTokenError."""
        req = await self.get_by_device_code(device_code)
        if req is None:
            raise DeviceTokenError("invalid_grant", "Invalid device_code")

        now = _utcnow_naive()
        if req.expires_at <= now:
            raise DeviceTokenError("expired_token", "Device code expired")

        if req.status == "denied":
            raise DeviceTokenError("access_denied", "User denied the request")

        if req.status == "consumed":
            raise DeviceTokenError("invalid_grant", "Device code already used")

        if req.status != "approved":
            # enforce polling interval
            interval = int(min_interval_seconds or req.poll_interval_seconds or 5)
            if req.last_polled_at is not None:
                delta = (now - req.last_polled_at).total_seconds()
                if delta < interval:
                    raise DeviceTokenError("slow_down", "Polling too frequently")

            req.last_polled_at = now
            self._session.add(req)
            raise DeviceTokenError("authorization_pending", "Authorization pending")

        if not req.user_id:
            raise DeviceTokenError("server_error", "Approved request missing user_id")

        access_token = create_access_token(
            user_id=req.user_id,
            organization_id=req.organization_id,
            extra_claims={"scope": (req.scope or "mcp").strip() or "mcp"},
        )
        req.status = "consumed"
        req.consumed_at = now
        self._session.add(req)

        expires_in = int(timedelta(hours=config_module.settings.jwt_expiry_hours).total_seconds())
        return {
            "access_token": access_token,
            "token_type": "Bearer",
            "expires_in": expires_in,
            "scope": (req.scope or "mcp").strip() or "mcp",
        }
