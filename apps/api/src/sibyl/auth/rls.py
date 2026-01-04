"""Row-Level Security (RLS) session variable management.

Sets PostgreSQL session variables (app.user_id, app.org_id) so RLS policies
can filter rows based on the authenticated user's context.

Usage:
    from sibyl.auth.rls import get_rls_session

    @router.get("/protected")
    async def protected(session: AsyncSession = Depends(get_rls_session)):
        # RLS policies now filter based on current user/org
        ...
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from uuid import UUID

import structlog
from fastapi import HTTPException, Request, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from sibyl.auth.api_keys import ApiKeyManager
from sibyl.auth.http import select_access_token
from sibyl.auth.jwt import JwtError, verify_access_token
from sibyl.config import settings
from sibyl.db.connection import get_session

log = structlog.get_logger()


async def _resolve_claims_minimal(request: Request, session: AsyncSession) -> dict | None:
    """Resolve JWT claims without loading full User/Org objects.

    This is a minimal version optimized for RLS setup - we only need
    the user_id and org_id, not the full database records.
    """
    claims = getattr(request.state, "jwt_claims", None)
    if claims:
        return claims

    token = select_access_token(
        authorization=request.headers.get("authorization"),
        cookie_token=request.cookies.get("sibyl_access_token"),
    )
    if not token:
        return None

    try:
        return verify_access_token(token)
    except JwtError:
        pass

    # API key fallback
    if token.startswith("sk_"):
        auth = await ApiKeyManager(session).authenticate(token)
        if auth:
            return {
                "sub": str(auth.user_id),
                "org": str(auth.organization_id),
                "typ": "api_key",
                "scopes": list(auth.scopes or []),
            }

    return None


async def set_rls_context(
    session: AsyncSession,
    *,
    user_id: UUID | str | None = None,
    org_id: UUID | str | None = None,
) -> None:
    """Set RLS session variables on a database connection.

    PostgreSQL RLS policies can access these via:
        current_setting('app.user_id', true)
        current_setting('app.org_id', true)

    The second parameter (true) makes it return NULL if not set,
    rather than raising an error.

    Args:
        session: Database session to configure
        user_id: Current user's UUID
        org_id: Current organization's UUID
    """
    # Use local variables (transaction-scoped, reset on commit/rollback)
    if user_id:
        await session.execute(
            text("SET LOCAL app.user_id = :user_id"),
            {"user_id": str(user_id)},
        )
    else:
        # Reset to empty string if no user (RLS should deny)
        await session.execute(text("RESET app.user_id"))

    if org_id:
        await session.execute(
            text("SET LOCAL app.org_id = :org_id"),
            {"org_id": str(org_id)},
        )
    else:
        await session.execute(text("RESET app.org_id"))


async def get_rls_session(request: Request) -> AsyncGenerator[AsyncSession]:
    """FastAPI dependency that provides a session with RLS context set.

    This dependency:
    1. Resolves auth claims from the request (JWT or API key)
    2. Opens a database session
    3. Sets app.user_id and app.org_id session variables
    4. Yields the configured session

    Usage:
        @router.get("/items")
        async def list_items(session: AsyncSession = Depends(get_rls_session)):
            # RLS policies automatically filter to user's accessible rows
            result = await session.execute(select(Item))
            return result.scalars().all()
    """
    async with get_session() as session:
        if settings.disable_auth:
            # No RLS in dev mode when auth is disabled
            yield session
            return

        claims = await _resolve_claims_minimal(request, session)

        if claims:
            user_id = claims.get("sub")
            org_id = claims.get("org")

            try:
                await set_rls_context(
                    session,
                    user_id=UUID(str(user_id)) if user_id else None,
                    org_id=UUID(str(org_id)) if org_id else None,
                )
            except Exception as e:
                log.warning("Failed to set RLS context", error=str(e))
                # Continue without RLS - policies should deny by default

        yield session


async def require_rls_session(request: Request) -> AsyncGenerator[AsyncSession]:
    """Like get_rls_session, but requires authentication.

    Raises 401 if no valid auth context is found.
    """
    async with get_session() as session:
        if settings.disable_auth:
            yield session
            return

        claims = await _resolve_claims_minimal(request, session)
        if not claims:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Not authenticated",
            )

        user_id = claims.get("sub")
        org_id = claims.get("org")

        if not user_id:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid token: missing user",
            )

        try:
            await set_rls_context(
                session,
                user_id=UUID(str(user_id)),
                org_id=UUID(str(org_id)) if org_id else None,
            )
        except Exception as e:
            log.exception("Failed to set RLS context", error=str(e))
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to initialize security context",
            ) from e

        yield session
