"""Surreal-backed request-time auth adapters."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

from sibyl import config as config_module
from sibyl.auth.http import select_access_token
from sibyl.auth.jwt import (
    JwtError,
    create_access_token,
    create_refresh_token,
    verify_access_token,
)
from sibyl.auth.session_cache import access_session_cache
from sibyl.persistence.surreal.auth_runtime._common import (
    RefreshRotation,
    SurrealRecord,
    SurrealSessionRepository,
    _api_key_allows_rest,
    _api_key_claim_payload,
    _auth_client_scope,
    _auth_session_namespace,
    _insufficient_api_scope,
    _is_rest_request,
    _log_audit_event,
    _resolve_auth_context_from_claims,
    _session_id_from_access_token,
    _session_namespace,
    _SurrealRepository,
)
from sibyl.persistence.surreal.auth_runtime.api_keys import authenticate_api_key
from sibyl.persistence.surreal.auth_runtime.users import get_user_by_id
from sibyl_core.auth import (
    AuthContext,
)
from sibyl_core.backends.surreal.records import (
    coerce_uuid as _coerce_uuid,
    utcnow as _utcnow,
)


async def resolve_auth_context(
    *,
    claims: Mapping[str, object],
    session: object | None = None,
) -> AuthContext:
    del session
    return await _resolve_auth_context_from_claims(claims)


async def create_session_record(
    *,
    user_id: UUID,
    token: str,
    expires_at,
    session_id: UUID | None = None,
    organization_id: UUID | None = None,
    refresh_token: str | None = None,
    refresh_token_expires_at=None,
    device_name: str | None = None,
    device_type: str | None = None,
    browser: str | None = None,
    os: str | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
    location: str | None = None,
):
    async with _auth_client_scope() as client:
        sessions = SurrealSessionRepository.from_client(client)
        session = await sessions.create_session(
            user_id=user_id,
            token=token,
            expires_at=expires_at,
            session_id=session_id,
            organization_id=organization_id,
            refresh_token=refresh_token,
            refresh_token_expires_at=refresh_token_expires_at,
            device_name=device_name,
            device_type=device_type,
            browser=browser,
            os=os,
            ip_address=ip_address,
            user_agent=user_agent,
            location=location,
        )
        return _auth_session_namespace(session)


async def load_refresh_session_record(refresh_token: str):
    async with _auth_client_scope() as client:
        sessions = SurrealSessionRepository.from_client(client)
        session = await sessions.get_session_by_refresh_token(refresh_token)
        return _auth_session_namespace(session)


async def rotate_refresh_session_record(
    refresh_token: str,
    *,
    new_access_token: str,
    new_access_expires_at,
    new_refresh_token: str,
    new_refresh_expires_at,
):
    async with _auth_client_scope() as client:
        sessions = SurrealSessionRepository.from_client(client)
        existing = await sessions.get_session_by_refresh_token(refresh_token)
        if existing is None:
            return None
        rotated = await sessions.rotate_tokens(
            existing,
            new_access_token=new_access_token,
            new_access_expires_at=new_access_expires_at,
            new_refresh_token=new_refresh_token,
            new_refresh_expires_at=new_refresh_expires_at,
        )
        return _auth_session_namespace(rotated)


async def revoke_refresh_session_record(refresh_token: str) -> None:
    async with _auth_client_scope() as client:
        sessions = SurrealSessionRepository.from_client(client)
        existing = await sessions.get_session_by_refresh_token(refresh_token)
        if existing is None:
            return
        await sessions.revoke_loaded_session(existing)


async def load_oauth_client_registration(client_id: str) -> SurrealRecord | None:
    normalized = client_id.strip()
    if not normalized:
        return None
    async with _auth_client_scope() as client:
        repo = _SurrealRepository(client)
        record = await repo.select_one(
            "SELECT * FROM oauth_client_registrations WHERE client_id = $client_id LIMIT 1;",
            client_id=normalized,
        )
        if record is None:
            return None
        client_info = record.get("client_info")
        if not isinstance(client_info, dict):
            return None
        return {str(key): value for key, value in client_info.items()}


async def save_oauth_client_registration(
    *,
    client_id: str,
    client_info: Mapping[str, object],
) -> None:
    normalized = client_id.strip()
    if not normalized:
        return
    async with _auth_client_scope() as client:
        repo = _SurrealRepository(client)
        existing = await repo.select_one(
            "SELECT * FROM oauth_client_registrations WHERE client_id = $client_id LIMIT 1;",
            client_id=normalized,
        )
        now = _utcnow()
        registration_id = (
            _coerce_uuid(existing.get("uuid"), field_name="oauth_client_registrations.uuid")
            if existing is not None
            else uuid4()
        )
        record: SurrealRecord = {
            "uuid": str(registration_id),
            "client_id": normalized,
            "client_info": dict(client_info),
            "created_at": existing.get("created_at") if existing is not None else now,
            "updated_at": now,
        }
        await repo.replace_record(
            "oauth_client_registrations",
            uuid=registration_id,
            record=record,
        )


async def resolve_request_claims(request) -> SurrealRecord | None:
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
    if token.startswith("sk_"):
        auth = await authenticate_api_key(token)
        if auth is None:
            return None
        scopes = list(auth.scopes or [])
        if _is_rest_request(request) and not _api_key_allows_rest(
            scopes=scopes, method=request.method
        ):
            raise _insufficient_api_scope(scopes=scopes, method=request.method)
        return _api_key_claim_payload(auth)
    return None


async def resolve_request_user(request):
    claims = await resolve_request_claims(request)
    if not claims:
        return None
    try:
        user_id = UUID(str(claims.get("sub", "")))
    except ValueError:
        return None
    return await get_user_by_id(user_id)


async def validate_access_session(token: str) -> bool:
    session_id = _session_id_from_access_token(token)
    if session_id is not None:
        cached = access_session_cache.get(session_id)
        if cached is not None:
            return cached

    async with _auth_client_scope() as client:
        sessions = SurrealSessionRepository.from_client(client)
        if session_id is not None:
            session = await sessions.get_session_by_id(session_id)
            if session is None:
                access_session_cache.mark_revoked(session_id)
                return False
            access_session_cache.store_session(session)
            return True
        return await sessions.get_session_by_token(token) is not None


async def rotate_refresh_exchange(
    *,
    refresh_token: str,
    user_id: UUID,
    organization_id: UUID | None,
    request,
):
    async with _auth_client_scope() as client:
        sessions = SurrealSessionRepository.from_client(client)
        existing = await sessions.get_session_by_refresh_token(refresh_token)
        if existing is None:
            return None

        rotation: RefreshRotation | None = None
        for attempt in range(2):
            access_token = create_access_token(
                user_id=user_id,
                organization_id=organization_id,
                session_id=existing.id,
            )
            new_refresh_token, refresh_expires = create_refresh_token(
                user_id=user_id,
                organization_id=organization_id,
                session_id=existing.id,
            )
            access_expires = _utcnow() + timedelta(
                minutes=config_module.settings.access_token_expire_minutes
            )
            try:
                await sessions.rotate_tokens(
                    existing,
                    new_access_token=access_token,
                    new_access_expires_at=access_expires,
                    new_refresh_token=new_refresh_token,
                    new_refresh_expires_at=refresh_expires,
                )
            except LookupError:
                if attempt == 1:
                    return None
                existing = await sessions.get_session_by_refresh_token(refresh_token)
                if existing is None:
                    return None
                continue
            rotation = RefreshRotation(
                session_id=existing.id,
                access_token=access_token,
                refresh_token=new_refresh_token,
                refresh_expires=refresh_expires,
                user_id=user_id,
                organization_id=organization_id,
            )
            break

        if rotation is None:
            return None
        await _log_audit_event(
            client,
            action="auth.token.refresh",
            user_id=user_id,
            organization_id=organization_id,
            request=request,
            details={"session_id": str(existing.id)},
        )
        return rotation


async def revoke_access_session(token: str) -> None:
    async with _auth_client_scope() as client:
        sessions = SurrealSessionRepository.from_client(client)
        session_id = _session_id_from_access_token(token)
        existing = (
            await sessions.get_session_by_id(session_id)
            if session_id is not None
            else await sessions.get_session_by_token(token)
        )
        if existing is None:
            if session_id is not None:
                access_session_cache.mark_revoked(session_id)
            return
        await sessions.revoke_loaded_session(existing)
        access_session_cache.mark_revoked(
            existing.id,
            user_id=existing.user_id,
            organization_id=existing.organization_id,
            expires_at=existing.refresh_token_expires_at or existing.expires_at,
        )


async def list_user_sessions(
    *,
    user_id: UUID,
    include_expired: bool = False,
) -> list[SimpleNamespace]:
    async with _auth_client_scope() as client:
        repo = _SurrealRepository(client)
        params: SurrealRecord = {"user_id": str(user_id)}
        query = "SELECT * FROM user_sessions WHERE user_id = $user_id AND revoked_at = NONE"
        if not include_expired:
            params["now"] = _utcnow()
            query += " AND expires_at > $now"
        query += " ORDER BY last_active_at DESC;"
        rows = await repo.select_many(query, **params)
        sessions: list[SimpleNamespace] = []
        for row in rows:
            session = _session_namespace(row)
            if session is not None:
                sessions.append(session)
        return sessions


async def revoke_all_user_sessions(
    *,
    user_id: UUID,
    exclude_token_hash: str | None = None,
) -> int:
    async with _auth_client_scope() as client:
        sessions = SurrealSessionRepository.from_client(client)
        return await sessions.revoke_all_sessions(user_id, exclude_token_hash=exclude_token_hash)


async def revoke_user_session(
    *,
    user_id: UUID,
    session_id: UUID,
) -> bool:
    async with _auth_client_scope() as client:
        sessions = SurrealSessionRepository.from_client(client)
        return await sessions.revoke_session(session_id, user_id)
