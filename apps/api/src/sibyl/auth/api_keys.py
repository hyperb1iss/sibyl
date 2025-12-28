"""API key generation + verification."""

from __future__ import annotations

import hmac
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import pbkdf2_hmac
from typing import TYPE_CHECKING, Self

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from sibyl.db.models import ApiKey

if TYPE_CHECKING:
    from uuid import UUID


class ApiKeyError(ValueError):
    """API key error."""


def generate_api_key(*, live: bool = True) -> str:
    prefix = "sk_live_" if live else "sk_test_"
    return prefix + secrets.token_urlsafe(32)


def api_key_prefix(key: str, length: int = 16) -> str:
    return key[: max(1, length)]


def hash_api_key(
    key: str,
    *,
    salt: bytes | None = None,
    iterations: int = 210_000,
) -> tuple[str, str]:
    if not key:
        raise ApiKeyError("Key is empty")
    salt_bytes = salt or secrets.token_bytes(16)
    dk = pbkdf2_hmac("sha256", key.encode("utf-8"), salt_bytes, iterations, dklen=32)
    return salt_bytes.hex(), dk.hex()


def verify_api_key(key: str, *, salt_hex: str, hash_hex: str, iterations: int = 210_000) -> bool:
    try:
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
    except ValueError:
        return False
    dk = pbkdf2_hmac("sha256", key.encode("utf-8"), salt, iterations, dklen=len(expected))
    return hmac.compare_digest(dk, expected)


@dataclass(frozen=True)
class ApiKeyAuth:
    api_key_id: UUID
    user_id: UUID
    organization_id: UUID
    scopes: list[str]


class ApiKeyManager:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    @classmethod
    def from_session(cls, session: AsyncSession) -> Self:
        return cls(session)

    async def create(
        self,
        *,
        organization_id: UUID,
        user_id: UUID,
        name: str,
        live: bool = True,
        scopes: list[str] | None = None,
        expires_at: datetime | None = None,
    ) -> tuple[ApiKey, str]:
        raw = generate_api_key(live=live)
        salt_hex, hash_hex = hash_api_key(raw)

        normalized_scopes = [s.strip() for s in (scopes or ["mcp"]) if str(s).strip()]

        record = ApiKey(
            organization_id=organization_id,
            user_id=user_id,
            name=name,
            key_prefix=api_key_prefix(raw),
            key_salt=salt_hex,
            key_hash=hash_hex,
            scopes=normalized_scopes,
            expires_at=expires_at.replace(tzinfo=None) if expires_at and expires_at.tzinfo else expires_at,
        )
        self._session.add(record)
        await self._session.flush()
        return record, raw

    async def authenticate(self, raw_key: str) -> ApiKeyAuth | None:
        prefix = api_key_prefix(raw_key)
        result = await self._session.execute(select(ApiKey).where(ApiKey.key_prefix == prefix))
        candidates = list(result.scalars().all())
        now = datetime.now(UTC).replace(tzinfo=None)
        for key in candidates:
            if key.revoked_at is not None:
                continue
            if key.expires_at is not None and key.expires_at <= now:
                continue
            if verify_api_key(raw_key, salt_hex=key.key_salt, hash_hex=key.key_hash):
                key.last_used_at = datetime.now(UTC).replace(tzinfo=None)
                self._session.add(key)
                return ApiKeyAuth(
                    api_key_id=key.id,
                    user_id=key.user_id,
                    organization_id=key.organization_id,
                    scopes=list(key.scopes or []),
                )
        return None

    async def list_for_org(self, organization_id: UUID, *, limit: int = 100) -> list[ApiKey]:
        result = await self._session.execute(
            select(ApiKey)
            .where(ApiKey.organization_id == organization_id)
            .order_by(ApiKey.created_at.desc())
            .limit(limit)
        )
        return list(result.scalars().all())

    async def list_for_user(
        self,
        *,
        organization_id: UUID,
        user_id: UUID,
        limit: int = 100,
    ) -> list[ApiKey]:
        result = await self._session.execute(
            select(ApiKey)
            .where(
                ApiKey.organization_id == organization_id,
                ApiKey.user_id == user_id,
            )
            .order_by(ApiKey.created_at.desc())
            .limit(limit)
        )
        return list(result.scalars().all())

    async def revoke(self, api_key_id: UUID) -> ApiKey | None:
        key = await self._session.get(ApiKey, api_key_id)
        if key is None:
            return None
        key.revoked_at = datetime.now(UTC).replace(tzinfo=None)
        self._session.add(key)
        return key
