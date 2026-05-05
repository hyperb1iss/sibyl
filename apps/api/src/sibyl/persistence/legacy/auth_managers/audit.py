"""Audit logging for sensitive operations.

Stores a minimal, append-only trail in Postgres for security + debugging.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request

from sibyl.db.models import AuditLog

if TYPE_CHECKING:
    from uuid import UUID


class AuditLogger:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def log(
        self,
        *,
        action: str,
        user_id: UUID | None,
        organization_id: UUID | None,
        request: Request | None = None,
        details: dict[str, Any] | None = None,
    ) -> AuditLog:
        ip_address = request.client.host if request and request.client else None
        user_agent = request.headers.get("user-agent") if request else None

        event = AuditLog(
            organization_id=organization_id,
            user_id=user_id,
            action=action,
            ip_address=ip_address,
            user_agent=user_agent,
            details=details or {},
        )
        self._session.add(event)
        await self._session.flush()
        return event
