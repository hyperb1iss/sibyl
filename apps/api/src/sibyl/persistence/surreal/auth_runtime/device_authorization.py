"""Surreal-backed request-time auth adapters."""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

from sibyl import config as config_module
from sibyl.auth.jwt import (
    create_access_token,
    create_refresh_token,
)
from sibyl.auth.primitives import (
    DeviceTokenError,
    generate_device_code,
    generate_user_code,
    hash_device_code,
)
from sibyl.persistence.surreal.auth_runtime._common import (
    DeviceBrowserLogin,
    QueryClient,
    SurrealRecord,
    SurrealSessionRepository,
    _auth_client_scope,
    _auth_org_namespace,
    _auth_user_model,
    _device_request_namespace,
    _ensure_personal_org_membership_record,
    _log_audit_event,
    _record_payload,
    _require_namespace,
    _SurrealRepository,
)
from sibyl.persistence.surreal.auth_runtime.login import login_local_user
from sibyl_core.auth import (
    AuthUser,
)
from sibyl_core.backends.surreal.records import (
    normalize_record as _normalize_record,
    normalize_records as _normalize_records,
    utcnow as _utcnow,
)


async def start_device_authorization(
    *,
    client_name: str | None,
    scope: str,
    expires_in,
    poll_interval_seconds: int,
):
    async with _auth_client_scope() as client:
        repo = _SurrealRepository(client)
        now = _utcnow()
        expires_at = now + expires_in
        for _ in range(20):
            device_code = generate_device_code()
            user_code = generate_user_code()
            device_code_hash = hash_device_code(device_code)
            existing = await repo.select_one(
                "SELECT * FROM device_authorization_requests "
                "WHERE device_code_hash = $device_code_hash OR user_code = $user_code LIMIT 1;",
                device_code_hash=device_code_hash,
                user_code=user_code,
            )
            if existing is not None:
                continue
            record = {
                "uuid": str(uuid4()),
                "device_code_hash": device_code_hash,
                "user_code": user_code,
                "client_name": (client_name or "").strip() or None,
                "scope": (scope or "").strip() or "mcp",
                "status": "pending",
                "poll_interval_seconds": max(1, int(poll_interval_seconds)),
                "last_polled_at": None,
                "expires_at": expires_at,
                "approved_at": None,
                "denied_at": None,
                "consumed_at": None,
                "user_id": None,
                "organization_id": None,
                "created_at": now,
                "updated_at": now,
            }
            created = _normalize_records(
                await client.execute_query(
                    "CREATE device_authorization_requests CONTENT $record;",
                    record=record,
                )
            )
            if not created:
                msg = "Failed to create device authorization request"
                raise RuntimeError(msg)
            return _device_request_namespace(created[0]), device_code
    msg = "Failed to allocate unique device/user codes"
    raise RuntimeError(msg)


async def exchange_device_code(*, device_code: str) -> dict[str, object]:
    async with _auth_client_scope() as client:
        repo = _SurrealRepository(client)
        sessions = SurrealSessionRepository.from_client(client)
        device_code_hash = hash_device_code(device_code)
        record = await repo.select_one(
            "SELECT * FROM device_authorization_requests "
            "WHERE device_code_hash = $device_code_hash LIMIT 1;",
            device_code_hash=device_code_hash,
        )
        request_row = _device_request_namespace(record)
        if request_row is None:
            raise DeviceTokenError("invalid_grant", "Invalid device_code")
        now = _utcnow()
        if request_row.expires_at <= now:
            raise DeviceTokenError("expired_token", "Device code expired")
        if request_row.status == "denied":
            raise DeviceTokenError("access_denied", "User denied the request")
        if request_row.status == "consumed":
            raise DeviceTokenError("invalid_grant", "Device code already used")
        if request_row.status != "approved":
            interval = int(request_row.poll_interval_seconds or 5)
            if request_row.last_polled_at is not None:
                delta = (now - request_row.last_polled_at).total_seconds()
                if delta < interval:
                    raise DeviceTokenError("slow_down", "Polling too frequently")
            await client.execute_query(
                """
                    UPDATE device_authorization_requests
                    SET last_polled_at = $last_polled_at,
                        updated_at = $updated_at
                    WHERE uuid = $uuid;
                """,
                uuid=str(request_row.id),
                last_polled_at=now,
                updated_at=now,
            )
            raise DeviceTokenError("authorization_pending", "Authorization pending")

        if request_row.user_id is None:
            raise DeviceTokenError("server_error", "Approved request missing user_id")
        session_id = uuid4()
        access_token = create_access_token(
            user_id=request_row.user_id,
            organization_id=request_row.organization_id,
            session_id=session_id,
            extra_claims={"scope": (request_row.scope or "mcp").strip() or "mcp"},
        )
        refresh_token, refresh_expires = create_refresh_token(
            user_id=request_row.user_id,
            organization_id=request_row.organization_id,
            session_id=session_id,
        )
        access_expires = now + timedelta(minutes=config_module.settings.access_token_expire_minutes)
        await sessions.create_session(
            user_id=request_row.user_id,
            organization_id=request_row.organization_id,
            token=access_token,
            expires_at=access_expires,
            session_id=session_id,
            refresh_token=refresh_token,
            refresh_token_expires_at=refresh_expires,
            device_name=request_row.client_name,
            device_type="device",
        )
        await client.execute_query(
            """
                UPDATE device_authorization_requests
                SET status = $status,
                    consumed_at = $consumed_at,
                    updated_at = $updated_at
                WHERE uuid = $uuid;
            """,
            uuid=str(request_row.id),
            status="consumed",
            consumed_at=now,
            updated_at=now,
        )
        return {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "token_type": "Bearer",
            "expires_in": int(
                timedelta(
                    minutes=config_module.settings.access_token_expire_minutes
                ).total_seconds()
            ),
            "scope": (request_row.scope or "mcp").strip() or "mcp",
        }


async def get_device_request_by_user_code(user_code: str):
    async with _auth_client_scope() as client:
        repo = _SurrealRepository(client)
        record = await repo.select_one(
            "SELECT * FROM device_authorization_requests WHERE user_code = $user_code LIMIT 1;",
            user_code=user_code,
        )
        return _device_request_namespace(record)


async def _load_device_authorization_user_and_request(
    client: QueryClient, *, user_id: UUID, user_code: str
) -> tuple[AuthUser | None, SurrealRecord | None, SimpleNamespace | None]:
    payload = await client.execute_query(
        """
            RETURN {
                user: (SELECT * FROM users WHERE uuid = $user_id LIMIT 1)[0],
                device_request: (
                    SELECT * FROM device_authorization_requests
                    WHERE user_code = $user_code
                    LIMIT 1
                )[0],
            };
        """,
        user_id=str(user_id),
        user_code=user_code,
    )
    payload = _record_payload(payload)
    user = _auth_user_model(_normalize_record(payload.get("user")))
    record = _normalize_record(payload.get("device_request"))
    return user, record, _device_request_namespace(record)


async def login_device_browser_user(
    *,
    email: str,
    password: str,
    request,
    break_glass_reason: str | None = None,
):
    issued = await login_local_user(
        email=email,
        password=password,
        request=request,
        break_glass_reason=break_glass_reason,
    )
    if issued is None:
        return None
    return DeviceBrowserLogin(
        user=issued.user,
        organization=issued.organization,
        access_token=issued.access_token,
        refresh_token=issued.refresh_token,
        refresh_expires=issued.refresh_expires,
    )


async def deny_device_authorization(*, user_id: UUID, user_code: str, request):
    async with _auth_client_scope() as client:
        repo = _SurrealRepository(client)
        user, record, request_row = await _load_device_authorization_user_and_request(
            client, user_id=user_id, user_code=user_code
        )
        if user is None:
            return None
        now = _utcnow()
        if (
            record is None
            or request_row is None
            or request_row.expires_at <= now
            or request_row.status != "pending"
        ):
            return None
        updated = {
            **record,
            "status": "denied",
            "denied_at": now,
            "updated_at": now,
        }
        written = await repo.replace_record(
            "device_authorization_requests",
            uuid=request_row.id,
            record=updated,
        )
        await _log_audit_event(
            client,
            action="auth.device.deny",
            user_id=user.id,
            organization_id=None,
            request=request,
            details={
                "device_request_id": str(request_row.id),
                "client_name": request_row.client_name,
            },
        )
        return _device_request_namespace(written)


async def approve_device_authorization(*, user_id: UUID, user_code: str, request):
    async with _auth_client_scope() as client:
        repo = _SurrealRepository(client)
        user, record, request_row = await _load_device_authorization_user_and_request(
            client, user_id=user_id, user_code=user_code
        )
        if user is None:
            return None
        now = _utcnow()
        if (
            record is None
            or request_row is None
            or request_row.expires_at <= now
            or request_row.status != "pending"
        ):
            return None
        organization_record = await _ensure_personal_org_membership_record(client, user)
        organization = _require_namespace(_auth_org_namespace(organization_record), label="org")
        updated = {
            **record,
            "status": "approved",
            "approved_at": now,
            "user_id": str(user.id),
            "organization_id": str(organization.id),
            "updated_at": now,
        }
        written = await repo.replace_record(
            "device_authorization_requests",
            uuid=request_row.id,
            record=updated,
        )
        await _log_audit_event(
            client,
            action="auth.device.approve",
            user_id=user.id,
            organization_id=organization.id,
            request=request,
            details={
                "device_request_id": str(request_row.id),
                "client_name": request_row.client_name,
            },
        )
        return (
            _auth_org_namespace(
                {
                    "uuid": str(organization.id),
                    "name": organization.name,
                    "slug": organization.slug,
                    "is_personal": organization.is_personal,
                    "settings": dict(organization.settings),
                }
            ),
            _device_request_namespace(written),
        )
