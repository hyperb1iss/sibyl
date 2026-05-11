"""Retired relational Row-Level Security dependency shims."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Awaitable, Mapping
from typing import TYPE_CHECKING, Protocol

from fastapi import HTTPException, Request, status

if TYPE_CHECKING:
    from sibyl.auth.context import AuthContext


class RlsSession(Protocol):
    def execute(
        self,
        statement: object,
        params: Mapping[str, object] | None = None,
    ) -> Awaitable[object]: ...


def _unavailable() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="Relational RLS sessions were removed after the v0.6.0 compatibility release",
    )


async def get_rls_session(request: Request) -> AsyncGenerator[RlsSession]:
    """Fail closed for retired relational RLS dependencies."""
    del request
    raise _unavailable()
    yield


async def require_rls_session(request: Request) -> AsyncGenerator[RlsSession]:
    """Fail closed for retired relational RLS dependencies."""
    del request
    raise _unavailable()
    yield


async def apply_rls_from_auth_context(
    session: RlsSession,
    ctx: AuthContext,
) -> None:
    """No-op retained for archive-era callers that already have auth context."""
    del session, ctx


class AuthSession:
    """Container for authenticated context and no relational session."""

    __slots__ = ("ctx", "session")

    def __init__(self, ctx: AuthContext, session: RlsSession | None) -> None:
        self.ctx = ctx
        self.session = session


async def get_auth_session(request: Request) -> AsyncGenerator[AuthSession]:
    """Provide auth context without opening a relational session."""
    from sibyl.auth.dependencies import build_auth_context

    ctx = await build_auth_context(request, None)
    yield AuthSession(ctx, None)
