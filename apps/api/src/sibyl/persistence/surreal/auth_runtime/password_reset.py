"""Surreal-backed request-time auth adapters."""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

from fastapi import HTTPException

from sibyl.auth.passwords import hash_password
from sibyl.email import PasswordResetEmail, get_email_client
from sibyl.persistence.surreal.auth_runtime._common import (
    SurrealRecord,
    _auth_client_scope,
    _generate_reset_token,
    _hash_reset_token,
    _log_login_history,
    _password_reset_namespace,
    _record_payload,
    _SurrealRepository,
)
from sibyl_core.backends.surreal.records import (
    coerce_datetime as _coerce_datetime,
    coerce_uuid as _coerce_uuid,
    normalize_record as _normalize_record,
    normalize_records as _normalize_records,
    utcnow as _utcnow,
)


async def request_password_reset(email: str) -> None:
    normalized_email = email.strip().lower()
    if not normalized_email:
        return

    async with _auth_client_scope() as client:
        payload = await client.execute_query(
            """
                RETURN {
                    user: (
                        SELECT * FROM users
                        WHERE email = $email AND deleted_at = NONE
                        LIMIT 1
                    )[0],
                    tokens: (
                        SELECT * FROM password_reset_tokens
                        WHERE user_id IN (
                            SELECT VALUE uuid FROM users
                            WHERE email = $email AND deleted_at = NONE
                            LIMIT 1
                        )
                        ORDER BY created_at DESC
                    ),
                };
            """,
            email=normalized_email,
        )
        payload = _record_payload(payload)
        user = _normalize_record(payload.get("user"))
        if user is None:
            await _log_login_history(
                client,
                user_id=None,
                event_type="password_reset_request",
                success=False,
                failure_reason="user_not_found",
                email_attempted=normalized_email,
            )
            return

        now = _utcnow()
        rate_limit_cutoff = now - timedelta(minutes=2)
        existing_tokens = _normalize_records(payload.get("tokens"))
        for token_record in existing_tokens:
            created_at = _coerce_datetime(token_record.get("created_at"))
            if (
                created_at is not None
                and created_at > rate_limit_cutoff
                and token_record.get("revoked_at") is None
            ):
                await _log_login_history(
                    client,
                    user_id=_coerce_uuid(user.get("uuid"), field_name="user.uuid"),
                    event_type="password_reset_request",
                    success=False,
                    failure_reason="rate_limited",
                    email_attempted=normalized_email,
                )
                return
        await client.execute_query(
            "UPDATE password_reset_tokens SET revoked_at = $revoked_at "
            "WHERE user_id = $user_id AND used_at = NONE AND revoked_at = NONE;",
            user_id=str(user["uuid"]),
            revoked_at=now,
        )

        raw_token = _generate_reset_token()
        expires_at = now + timedelta(minutes=60)
        token_record = {
            "uuid": str(uuid4()),
            "user_id": str(user["uuid"]),
            "token_hash": _hash_reset_token(raw_token),
            "expires_at": expires_at,
            "used_at": None,
            "revoked_at": None,
            "ip_address": None,
            "user_agent": None,
            "created_at": now,
        }
        await client.execute_query(
            "CREATE password_reset_tokens CONTENT $record;", record=token_record
        )

        from sibyl.config import settings as app_settings

        reset_url = f"{app_settings.frontend_url.rstrip('/')}/reset-password?token={raw_token}"
        template = PasswordResetEmail(
            reset_url=reset_url,
            user_name=str(user.get("name") or "") or None,
            expires_in_minutes=60,
        )
        await get_email_client().send_template(
            template, to=str(user.get("email") or normalized_email)
        )
        await _log_login_history(
            client,
            user_id=_coerce_uuid(user.get("uuid"), field_name="user.uuid"),
            event_type="password_reset_request",
            success=True,
            email_attempted=normalized_email,
        )


async def confirm_password_reset(token: str, new_password: str) -> None:
    async with _auth_client_scope() as client:
        repo = _SurrealRepository(client)
        payload = await client.execute_query(
            """
                RETURN {
                    token: (
                        SELECT * FROM password_reset_tokens
                        WHERE token_hash = $token_hash
                        LIMIT 1
                    )[0],
                    user: (
                        SELECT * FROM users
                        WHERE uuid IN (
                            SELECT VALUE user_id FROM password_reset_tokens
                            WHERE token_hash = $token_hash
                            LIMIT 1
                        )
                        LIMIT 1
                    )[0],
                };
            """,
            token_hash=_hash_reset_token(token),
        )
        payload = _record_payload(payload)
        token_record = _normalize_record(payload.get("token"))
        if token_record is None:
            await _log_login_history(
                client,
                user_id=None,
                event_type="password_reset_confirm",
                success=False,
                failure_reason="token_not_found",
            )
            raise HTTPException(status_code=400, detail="Invalid or expired reset link")
        reset_token = _password_reset_namespace(token_record)
        if reset_token is None:
            await _log_login_history(
                client,
                user_id=None,
                event_type="password_reset_confirm",
                success=False,
                failure_reason="token_not_found",
            )
            raise HTTPException(status_code=400, detail="Invalid or expired reset link")
        now = _utcnow()
        if reset_token.used_at is not None:
            raise HTTPException(status_code=400, detail="This reset link has already been used")
        if reset_token.revoked_at is not None:
            raise HTTPException(status_code=400, detail="This reset link has been revoked")
        if reset_token.expires_at is None or reset_token.expires_at < now:
            raise HTTPException(status_code=400, detail="This reset link has expired")

        user = _normalize_record(payload.get("user"))
        if user is None:
            raise HTTPException(status_code=400, detail="User not found")

        password_state = hash_password(new_password)
        updated_user = {
            **user,
            "password_salt": password_state.salt_hex,
            "password_hash": password_state.hash_hex,
            "password_iterations": password_state.iterations,
            "updated_at": now,
        }
        await repo.replace_record("users", uuid=reset_token.user_id, record=updated_user)

        updated_token: SurrealRecord = {**token_record, "used_at": now}
        await repo.replace_record(
            "password_reset_tokens",
            uuid=reset_token.id,
            record=updated_token,
        )
        await _log_login_history(
            client,
            user_id=reset_token.user_id,
            event_type="password_reset_confirm",
            success=True,
        )
