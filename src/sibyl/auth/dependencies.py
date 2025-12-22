"""FastAPI auth dependencies."""

from __future__ import annotations

from uuid import UUID

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from sibyl.auth.api_keys import ApiKeyManager
from sibyl.auth.context import AuthContext
from sibyl.auth.http import select_access_token
from sibyl.auth.jwt import JwtError, verify_access_token
from sibyl.db.connection import get_session_dependency
from sibyl.db.models import Organization, OrganizationMember, OrganizationRole, User


async def resolve_claims(request: Request, session: AsyncSession | None = None) -> dict | None:
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

    if session is not None and token.startswith("sk_"):
        auth = await ApiKeyManager(session).authenticate(token)
        if auth:
            return {"sub": str(auth.user_id), "org": str(auth.organization_id), "typ": "api_key"}

    return None


async def get_current_user(
    request: Request,
    session: AsyncSession = Depends(get_session_dependency),
) -> User:
    claims = await resolve_claims(request, session)
    if not claims:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")

    try:
        user_id = UUID(str(claims.get("sub", "")))
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token") from e

    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")
    return user


async def get_current_organization(
    request: Request,
    session: AsyncSession = Depends(get_session_dependency),
) -> Organization:
    claims = await resolve_claims(request, session)
    if not claims:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")

    org_raw = claims.get("org")
    if not org_raw:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="No organization context")

    try:
        org_id = UUID(str(org_raw))
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token") from e

    org = await session.get(Organization, org_id)
    if org is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organization not found")
    return org


async def get_current_org_role(
    user: User = Depends(get_current_user),
    org: Organization = Depends(get_current_organization),
    session: AsyncSession = Depends(get_session_dependency),
) -> OrganizationRole:
    result = await session.execute(
        select(OrganizationMember).where(
            OrganizationMember.organization_id == org.id,
            OrganizationMember.user_id == user.id,
        )
    )
    membership = result.scalar_one_or_none()
    if membership is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not a member")
    return membership.role


def require_org_role(*allowed: OrganizationRole):
    async def _dep(role: OrganizationRole = Depends(get_current_org_role)) -> None:
        if role not in allowed:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")

    return _dep


async def get_auth_context(
    request: Request,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session_dependency),
) -> AuthContext:
    claims = await resolve_claims(request, session)
    org = None
    role = None

    if claims and claims.get("org"):
        try:
            org_id = UUID(str(claims["org"]))
        except ValueError:
            org_id = None

        if org_id:
            org = await session.get(Organization, org_id)
            if org is not None:
                result = await session.execute(
                    select(OrganizationMember).where(
                        OrganizationMember.organization_id == org.id,
                        OrganizationMember.user_id == user.id,
                    )
                )
                membership = result.scalar_one_or_none()
                role = membership.role if membership else None

    scopes = frozenset(str(s) for s in (claims.get("scopes", []) if claims else []))
    return AuthContext(user=user, organization=org, org_role=role, scopes=scopes)


def require_org_admin():
    async def _dep(ctx: AuthContext = Depends(get_auth_context)) -> None:
        if ctx.organization is None or ctx.org_role not in {
            OrganizationRole.OWNER,
            OrganizationRole.ADMIN,
        }:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")

    return _dep
