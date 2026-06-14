"""Surreal-backed request-time auth adapters."""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from uuid import UUID

from fastapi import HTTPException
from starlette.requests import Request

from sibyl import config as config_module
from sibyl.auth.passwords import (
    PasswordError,
    hash_password,
    verify_password,
    verify_password_timing_floor,
)
from sibyl.auth.session_cache import access_session_cache
from sibyl.persistence.auth_common import UserNotFoundError
from sibyl.persistence.content_runtime import soft_delete_private_raw_captures_for_user
from sibyl.persistence.surreal.auth import (
    SurrealUserRepository,
)
from sibyl.persistence.surreal.auth_runtime._common import (
    QueryClient,
    SurrealRecord,
    UserDeletionRequestResult,
    _auth_client_scope,
    _auth_org_namespace,
    _auth_user_namespace,
    _coerce_int,
    _coerce_optional_uuid,
    _ensure_personal_org_membership_record,
    _list_user_org_records,
    _log_audit_event,
    _record_payload,
    _SurrealRepository,
)
from sibyl.persistence.surreal.auth_runtime.audit import log_memory_audit_event
from sibyl_core.backends.surreal.records import (
    normalize_record as _normalize_record,
    utcnow as _utcnow,
)


def _apply_password_change(
    record: SurrealRecord,
    *,
    current_password: str | None,
    new_password: str,
) -> SurrealRecord:
    updated = dict(record)
    has_local_password = bool(
        record.get("password_salt")
        and record.get("password_hash")
        and record.get("password_iterations")
    )
    if not has_local_password:
        # OAuth-only accounts have no credential to verify, so a change-password
        # request here would set a new local password unauthenticated, turning a
        # transient OAuth session into persistent takeover. Adding a first local
        # password must go through a dedicated, re-authenticated flow.
        raise HTTPException(
            status_code=400,
            detail="This account has no password to change",
        )
    if not current_password:
        raise HTTPException(status_code=400, detail="Current password is required")
    try:
        password_matches = verify_password(
            current_password,
            salt_hex=str(record["password_salt"]),
            hash_hex=str(record["password_hash"]),
            iterations=_coerce_int(
                record.get("password_iterations"),
                field_name="user.password_iterations",
            ),
        )
    except (TypeError, ValueError):
        password_matches = False
    if not password_matches:
        raise HTTPException(status_code=400, detail="Invalid current password")

    try:
        password_state = hash_password(new_password)
    except PasswordError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    updated["password_salt"] = password_state.salt_hex
    updated["password_hash"] = password_state.hash_hex
    updated["password_iterations"] = password_state.iterations
    return updated


async def _load_user_update_records(
    client: QueryClient, *, user_id: UUID, email: str | None
) -> tuple[SurrealRecord | None, SurrealRecord | None]:
    if email is None:
        payload = await client.execute_query(
            """
                RETURN {
                    user: (SELECT * FROM users WHERE uuid = $user_id LIMIT 1)[0],
                    email_owner: NONE,
                };
            """,
            user_id=str(user_id),
        )
    else:
        payload = await client.execute_query(
            """
                RETURN {
                    user: (SELECT * FROM users WHERE uuid = $user_id LIMIT 1)[0],
                    email_owner: (
                        SELECT * FROM users
                        WHERE email = $email
                        LIMIT 1
                    )[0],
                };
            """,
            user_id=str(user_id),
            email=email,
        )
    payload = _record_payload(payload)
    return _normalize_record(payload.get("user")), _normalize_record(payload.get("email_owner"))


async def authenticate_local_user(*, email: str, password: str):
    if not password:
        verify_password_timing_floor(
            password,
            iterations=config_module.settings.password_iterations,
        )
        return None
    async with _auth_client_scope() as client:
        repo = _SurrealRepository(client)
        record = await repo.select_one(
            "SELECT * FROM users WHERE email = $email AND deleted_at = NONE LIMIT 1;",
            email=email.strip().lower(),
        )
        if record is None:
            verify_password_timing_floor(
                password,
                iterations=config_module.settings.password_iterations,
            )
            return None
        if not record.get("password_salt") or not record.get("password_hash"):
            verify_password_timing_floor(
                password,
                iterations=config_module.settings.password_iterations,
            )
            return None
        ok = verify_password(
            password,
            salt_hex=str(record["password_salt"]),
            hash_hex=str(record["password_hash"]),
            iterations=_coerce_int(
                record.get("password_iterations") or config_module.settings.password_iterations,
                field_name="user.password_iterations",
            ),
        )
        if not ok:
            return None
        return _auth_user_namespace(record)


async def get_user_by_id(user_id: UUID):
    async with _auth_client_scope() as client:
        repo = _SurrealRepository(client)
        record = await repo.select_one(
            "SELECT * FROM users WHERE uuid = $uuid AND deleted_at = NONE LIMIT 1;",
            uuid=str(user_id),
        )
        return _auth_user_namespace(record)


async def list_user_organizations(*, user_id: UUID) -> list[SimpleNamespace]:
    async with _auth_client_scope() as client:
        records = await _list_user_org_records(client, user_id=user_id)
        return [org for record in records if (org := _auth_org_namespace(record)) is not None]


async def ensure_personal_organization(*, user_id: UUID):
    async with _auth_client_scope() as client:
        users = SurrealUserRepository.from_client(client)
        user = await users.get_by_id(user_id)
        if user is None:
            return None
        return _auth_org_namespace(await _ensure_personal_org_membership_record(client, user))


async def request_user_deletion(
    *,
    user_id: UUID,
    organization_id: UUID | None,
    request: Request | None,
) -> UserDeletionRequestResult:
    now = _utcnow()
    purge_after = now + timedelta(days=30)
    async with _auth_client_scope() as client:
        repo = _SurrealRepository(client)
        user_record = await repo.select_one(
            "SELECT * FROM users WHERE uuid = $user_id AND deleted_at = NONE LIMIT 1;",
            user_id=str(user_id),
        )
        if user_record is None:
            msg = f"User not found: {user_id}"
            raise UserNotFoundError(msg)

    private_memories_scheduled = await soft_delete_private_raw_captures_for_user(
        user_id=user_id,
        purge_after=purge_after,
    )

    async with _auth_client_scope() as client:
        repo = _SurrealRepository(client)
        user_record = await repo.select_one(
            "SELECT * FROM users WHERE uuid = $user_id AND deleted_at = NONE LIMIT 1;",
            user_id=str(user_id),
        )
        if user_record is None:
            msg = f"User not found: {user_id}"
            raise UserNotFoundError(msg)

        updated_user = {
            **user_record,
            "deleted_at": now,
            "purge_after": purge_after,
            "updated_at": now,
        }
        await repo.replace_record("users", uuid=user_id, record=updated_user)
        api_key_rows = await repo.select_many(
            """
                UPDATE api_keys
                SET revoked_at = $now,
                    updated_at = $now
                WHERE user_id = $user_id
                    AND revoked_at = NONE;
            """,
            user_id=str(user_id),
            now=now,
        )
        session_rows = await repo.select_many(
            """
                UPDATE user_sessions
                SET revoked_at = $now,
                    updated_at = $now
                WHERE user_id = $user_id
                    AND revoked_at = NONE;
            """,
            user_id=str(user_id),
            now=now,
        )
        access_session_cache.invalidate_user(user_id)
        await _log_audit_event(
            client,
            action="auth.user.delete_requested",
            user_id=user_id,
            organization_id=organization_id,
            request=request,
            details={
                "purge_after": purge_after.isoformat(),
                "private_memories_scheduled": private_memories_scheduled,
                "api_keys_revoked": len(api_key_rows),
                "sessions_revoked": len(session_rows),
            },
        )

    await log_memory_audit_event(
        action="memory.delete.personal_scheduled",
        user_id=user_id,
        organization_id=organization_id,
        request=request,
        memory_scope="private",
        scope_key=str(user_id),
        policy_allowed=True,
        policy_reason="user_deletion_requested",
        details={
            "purge_after": purge_after.isoformat(),
            "private_memories_scheduled": private_memories_scheduled,
        },
    )
    return UserDeletionRequestResult(
        user_id=user_id,
        purge_after=purge_after,
        private_memories_scheduled=private_memories_scheduled,
        api_keys_revoked=len(api_key_rows),
        sessions_revoked=len(session_rows),
    )


async def update_auth_user(
    *,
    user_id: UUID,
    email: str | None,
    name: str | None,
    avatar_url: str | None,
    current_password: str | None,
    new_password: str | None,
    organization_id: UUID | None,
    request,
):
    async with _auth_client_scope() as client:
        repo = _SurrealRepository(client)
        normalized_email = email.strip().lower() if email is not None else None
        user, email_owner = await _load_user_update_records(
            client,
            user_id=user_id,
            email=normalized_email,
        )
        if user is None:
            raise HTTPException(status_code=404, detail="User not found")
        changes: list[str] = []
        updated = dict(user)
        if email is not None:
            if (
                email_owner is not None
                and _coerce_optional_uuid(email_owner.get("uuid")) != user_id
            ):
                raise HTTPException(status_code=400, detail="Email is already in use")
            updated["email"] = normalized_email
            changes.append("email")
        if name is not None:
            normalized_name = name.strip()
            if not normalized_name:
                raise HTTPException(status_code=400, detail="Name is required")
            updated["name"] = normalized_name
            changes.append("name")
        if avatar_url is not None:
            updated["avatar_url"] = avatar_url.strip() or None
            changes.append("avatar_url")
        if new_password is not None:
            updated = _apply_password_change(
                updated,
                current_password=current_password,
                new_password=new_password,
            )
            changes.append("password")
        if not changes:
            raise HTTPException(status_code=400, detail="No fields to update")
        updated["updated_at"] = _utcnow()
        written = await repo.replace_record("users", uuid=user_id, record=updated)
        if any(change != "password" for change in changes):
            await _log_audit_event(
                client,
                action="user.update_profile",
                user_id=user_id,
                organization_id=organization_id,
                request=request,
                details={"fields": [change for change in changes if change != "password"]},
            )
        if "password" in changes:
            await _log_audit_event(
                client,
                action="user.change_password",
                user_id=user_id,
                organization_id=organization_id,
                request=request,
                details={},
            )
        return _auth_user_namespace(written)


async def patch_auth_user(
    *,
    user_id: UUID,
    updates: SurrealRecord,
    organization_id: UUID | None,
    request,
):
    async with _auth_client_scope() as client:
        repo = _SurrealRepository(client)
        if not updates:
            raise HTTPException(status_code=400, detail="No fields to update")

        normalized_email: str | None = None
        if "email" in updates:
            email = updates["email"]
            normalized_email = str(email).strip().lower() if email is not None else ""
            if not normalized_email:
                raise HTTPException(status_code=400, detail="Email is required")

        user, email_owner = await _load_user_update_records(
            client,
            user_id=user_id,
            email=normalized_email,
        )
        if user is None:
            raise HTTPException(status_code=404, detail="User not found")

        updated = dict(user)
        changes: list[str] = []

        if "email" in updates:
            if (
                email_owner is not None
                and _coerce_optional_uuid(email_owner.get("uuid")) != user_id
            ):
                raise HTTPException(status_code=400, detail="Email is already in use")
            updated["email"] = normalized_email
            changes.append("email")
        if "name" in updates:
            name = str(updates["name"] or "").strip()
            if not name:
                raise HTTPException(status_code=400, detail="Name is required")
            updated["name"] = name
            changes.append("name")
        if "avatar_url" in updates:
            avatar_url = updates["avatar_url"]
            updated["avatar_url"] = (
                str(avatar_url).strip() or None if avatar_url is not None else None
            )
            changes.append("avatar_url")
        if "bio" in updates:
            bio = updates["bio"]
            updated["bio"] = str(bio).strip() or None if bio is not None else None
            changes.append("bio")
        if "timezone" in updates:
            timezone = updates["timezone"]
            updated["timezone"] = str(timezone).strip() or "UTC" if timezone is not None else "UTC"
            changes.append("timezone")
        if "preferences" in updates:
            preferences = updates["preferences"]
            if not isinstance(preferences, dict):
                raise HTTPException(status_code=400, detail="Preferences must be an object")
            updated["preferences"] = dict(preferences)
            changes.append("preferences")
        if not changes:
            raise HTTPException(status_code=400, detail="No fields to update")

        updated["updated_at"] = _utcnow()
        written = await repo.replace_record("users", uuid=user_id, record=updated)
        await _log_audit_event(
            client,
            action="user.update_profile",
            user_id=user_id,
            organization_id=organization_id,
            request=request,
            details={"fields": changes},
        )
        return _auth_user_namespace(written)
