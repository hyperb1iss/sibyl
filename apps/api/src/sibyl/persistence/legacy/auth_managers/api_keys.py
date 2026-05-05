"""API key generation + verification."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Self
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col, select

from sibyl.auth.api_key_common import (
    ApiKeyAuth,
    ApiKeyError,
    api_key_prefix,
    generate_api_key,
    hash_api_key,
    verify_api_key,
)
from sibyl.db.models import ApiKey, ApiKeyProjectScope

__all__ = [
    "ApiKeyAuth",
    "ApiKeyError",
    "ApiKeyManager",
    "api_key_prefix",
    "generate_api_key",
    "hash_api_key",
    "verify_api_key",
]


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
            expires_at=expires_at.replace(tzinfo=None)
            if expires_at and expires_at.tzinfo
            else expires_at,
        )
        self._session.add(record)
        await self._session.flush()
        return record, raw

    async def authenticate(self, raw_key: str) -> ApiKeyAuth | None:
        """Authenticate an API key and load project restrictions."""
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

                # Load project scope restrictions
                project_ids = await self._load_project_restrictions(key.id)

                return ApiKeyAuth(
                    api_key_id=key.id,
                    user_id=key.user_id,
                    organization_id=key.organization_id,
                    scopes=list(key.scopes or []),
                    project_ids=project_ids,
                )
        return None

    async def _load_project_restrictions(self, api_key_id: UUID) -> list[UUID] | None:
        """Load project restrictions for an API key.

        Returns:
            None: No restrictions (all accessible projects allowed)
            list[UUID]: Only these projects are allowed (empty = no access)
        """
        result = await self._session.execute(
            select(ApiKeyProjectScope.project_id).where(ApiKeyProjectScope.api_key_id == api_key_id)
        )
        project_ids = list(result.scalars().all())

        # No rows means no restrictions (all projects allowed)
        # Rows means restricted to only those projects
        return project_ids if project_ids else None

    async def list_for_org(self, organization_id: UUID, *, limit: int = 100) -> list[ApiKey]:
        result = await self._session.execute(
            select(ApiKey)
            .where(ApiKey.organization_id == organization_id)
            .order_by(col(ApiKey.created_at).desc())
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
            .order_by(col(ApiKey.created_at).desc())
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
