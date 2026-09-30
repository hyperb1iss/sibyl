"""Current credential authority and retained ceilings for archive requests."""

from __future__ import annotations

from dataclasses import replace
from uuid import UUID

from fastapi import HTTPException, Request

from sibyl.auth.context import AuthContext
from sibyl.auth.dependencies import (
    _api_key_allows_rest,
    _api_key_claims,
    _auth_storage_unavailable,
    _insufficient_api_scope,
)
from sibyl.auth.http import select_access_token
from sibyl.auth.jwt import JwtError, verify_access_token
from sibyl.persistence.auth_runtime import (
    InvalidAuthClaimsError,
    SessionRepository,
    UserNotFoundError,
    authenticate_api_key,
    resolve_api_key_authority,
    resolve_auth_context,
)
from sibyl.persistence.surreal.auth import surreal_auth_client_scope
from sibyl_core.auth import OrganizationRole
from sibyl_core.migrate.personal_archive_plan import ArchiveCredentialCeiling

_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_WRITE_ROLES = frozenset({OrganizationRole.OWNER, OrganizationRole.ADMIN, OrganizationRole.MEMBER})


def archive_credential_ceiling(context: AuthContext) -> ArchiveCredentialCeiling:
    """Capture restrictions from trusted request authentication, never archive input."""
    return ArchiveCredentialCeiling(
        credential_kind="api_key" if context.api_key_id is not None else "session",
        api_key_id=context.api_key_id,
        rest_scopes=tuple(sorted(context.scopes)),
        project_restricted=context.api_key_project_ids is not None,
        project_ids=tuple(sorted(context.api_key_project_ids or ())),
        memory_restricted=(
            context.api_key_memory_space_ids is not None
            or context.api_key_memory_scope_keys is not None
        ),
        memory_space_ids=tuple(sorted(context.api_key_memory_space_ids or ())),
        memory_scope_keys=tuple(sorted(context.api_key_memory_scope_keys or ())),
    )


def _credential_identity(ceiling: ArchiveCredentialCeiling) -> tuple[str, str | None]:
    return ceiling.credential_kind, ceiling.api_key_id


def _intersection(values: tuple[tuple[str, ...] | None, ...]) -> tuple[str, ...] | None:
    restricted = [set(value) for value in values if value is not None]
    return tuple(sorted(set.intersection(*restricted))) if restricted else None


def _scope_capabilities(scopes: tuple[str, ...]) -> tuple[str, ...]:
    # REST write already includes read in the existing API policy.
    values = set(scopes)
    if "api:write" in values:
        values.add("api:read")
    return tuple(sorted(values))


def _effective_ceiling(
    ceilings: tuple[ArchiveCredentialCeiling, ...],
) -> ArchiveCredentialCeiling:
    projects = _intersection(
        tuple(c.project_ids if c.project_restricted else None for c in ceilings)
    )
    spaces = _intersection(
        tuple(c.memory_space_ids if c.memory_restricted else None for c in ceilings)
    )
    scope_keys = _intersection(
        tuple(c.memory_scope_keys if c.memory_restricted else None for c in ceilings)
    )
    scopes = _intersection(tuple(_scope_capabilities(c.rest_scopes) for c in ceilings))
    return ArchiveCredentialCeiling(
        credential_kind=ceilings[0].credential_kind,
        api_key_id=ceilings[0].api_key_id,
        rest_scopes=scopes or (),
        project_restricted=projects is not None,
        project_ids=projects or (),
        memory_restricted=spaces is not None or scope_keys is not None,
        memory_space_ids=spaces or (),
        memory_scope_keys=scope_keys or (),
    )


def _require_membership(context: AuthContext, method: str) -> None:
    if context.organization is None or context.org_role is None:
        raise HTTPException(status_code=403, detail="Archive access requires current membership")
    if method.upper() not in _SAFE_METHODS and context.org_role not in _WRITE_ROLES:
        raise HTTPException(status_code=403, detail="Archive check requires write access")


async def _current_context(request: Request, initial: AuthContext) -> AuthContext:
    token = select_access_token(
        authorization=request.headers.get("authorization"),
        cookie_token=request.cookies.get("sibyl_access_token"),
    )
    if token is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    if token.startswith("sk_"):
        presented = await authenticate_api_key(token)
        if presented is None:
            raise HTTPException(status_code=401, detail="Not authenticated")
        if (
            str(presented.user_id) != initial.user_id
            or str(presented.organization_id) != initial.organization_id
            or str(presented.api_key_id) != initial.api_key_id
        ):
            raise HTTPException(status_code=409, detail="Archive credential identity changed")
        current = await resolve_api_key_authority(
            api_key_id=presented.api_key_id,
            organization_id=presented.organization_id,
            user_id=presented.user_id,
        )
        if current is None:
            raise HTTPException(status_code=401, detail="Not authenticated")
        claims = _api_key_claims(current, scopes=list(current.scopes))
    else:
        claims = verify_access_token(token)
        async with surreal_auth_client_scope() as client:
            sessions = SessionRepository.from_client(client)
            session = (
                await sessions.get_session_by_id(UUID(str(claims["sid"])))
                if claims.get("sid") is not None
                else await sessions.get_session_by_token(token)
            )
        if (
            session is None
            or str(session.user_id) != str(claims.get("sub"))
            or (
                session.organization_id is not None
                and str(session.organization_id) != str(claims.get("org"))
            )
        ):
            raise HTTPException(status_code=401, detail="Not authenticated")
    return await resolve_auth_context(claims=claims)


async def refresh_archive_authority(
    request: Request,
    initial_context: AuthContext,
    *,
    original_ceiling: ArchiveCredentialCeiling | None = None,
) -> tuple[AuthContext, ArchiveCredentialCeiling]:
    """Refresh presented credentials after parsing and retain every trusted ceiling.

    Callers authorize audiences using the returned context before reading target
    diagnostics. This auth-store cut does not make later content/graph writes
    atomic with revocation.
    """
    _require_membership(initial_context, request.method)
    initial_ceiling = archive_credential_ceiling(initial_context)
    if original_ceiling is not None and _credential_identity(original_ceiling) != (
        _credential_identity(initial_ceiling)
    ):
        raise HTTPException(status_code=409, detail="Archive credential identity changed")
    try:
        current_context = await _current_context(request, initial_context)
    except TimeoutError as exc:
        raise _auth_storage_unavailable(exc) from exc
    except (JwtError, InvalidAuthClaimsError, UserNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=401, detail="Not authenticated") from exc
    if (
        current_context.user_id != initial_context.user_id
        or current_context.organization_id != initial_context.organization_id
    ):
        raise HTTPException(status_code=409, detail="Archive principal identity changed")
    _require_membership(current_context, request.method)
    current_ceiling = archive_credential_ceiling(current_context)
    if _credential_identity(current_ceiling) != _credential_identity(initial_ceiling):
        raise HTTPException(status_code=409, detail="Archive credential identity changed")
    ceilings = (initial_ceiling, current_ceiling)
    if original_ceiling is not None:
        ceilings = (original_ceiling, *ceilings)
    effective = _effective_ceiling(ceilings)
    if effective.credential_kind == "api_key" and not _api_key_allows_rest(
        scopes=list(effective.rest_scopes), method=request.method
    ):
        raise _insufficient_api_scope(scopes=list(effective.rest_scopes), method=request.method)
    return (
        replace(
            current_context,
            scopes=frozenset(effective.rest_scopes),
            api_key_project_ids=frozenset(effective.project_ids)
            if effective.project_restricted
            else None,
            api_key_memory_space_ids=frozenset(effective.memory_space_ids)
            if effective.memory_restricted
            else None,
            api_key_memory_scope_keys=frozenset(effective.memory_scope_keys)
            if effective.memory_restricted
            else None,
        ),
        effective,
    )
