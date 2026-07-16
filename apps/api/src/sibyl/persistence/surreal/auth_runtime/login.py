"""Surreal-backed request-time auth adapters."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from uuid import UUID, uuid4

from fastapi import HTTPException
from starlette import status
from starlette.requests import Request

from sibyl import config as config_module
from sibyl.auth.locks import first_user_admin_lock, oauth_identity_lock, signup_email_lock
from sibyl.persistence.surreal.auth import (
    SurrealUserRepository,
)
from sibyl.persistence.surreal.auth_runtime._common import (
    IssuedAuthSession,
    IssuedOidcSession,
    QueryClient,
    SurrealRecord,
    _auth_client_scope,
    _auth_org_namespace,
    _auth_user_namespace,
    _ensure_personal_org_membership_record,
    _execute_raw_statement_records,
    _issue_auth_session,
    _issue_oidc_session,
    _require_namespace,
    _role_value,
    _SurrealRepository,
    _unique_strings,
)
from sibyl_core.auth import (
    OrganizationRole,
)
from sibyl_core.backends.surreal.records import (
    coerce_uuid as _coerce_uuid,
    normalize_records as _normalize_records,
    query_error as _query_error,
    utcnow as _utcnow,
)


def _normalize_oidc_role(role: object) -> OrganizationRole:
    value = _role_value(role) or str(role)
    try:
        return OrganizationRole(value)
    except ValueError as exc:
        msg = f"Unsupported OIDC organization role: {value}"
        raise ValueError(msg) from exc


async def _safe_oidc_email(
    client: QueryClient,
    *,
    email: str | None,
    user_id: UUID | None,
) -> str | None:
    normalized = (email or "").strip().lower()
    if not normalized:
        return None
    repo = _SurrealRepository(client)
    owner = await repo.select_one(
        "SELECT uuid FROM users WHERE email = $email AND deleted_at = NONE LIMIT 1;",
        email=normalized,
    )
    if owner is None:
        return normalized
    owner_id = _coerce_uuid(owner.get("uuid"), field_name="user.uuid")
    return normalized if user_id is not None and owner_id == user_id else None


async def _upsert_identity_provider(
    client: QueryClient,
    *,
    provider_name: str,
    issuer: str,
    client_id: str | None,
    scopes: list[str],
    role_claim: str | None,
) -> None:
    repo = _SurrealRepository(client)
    existing = await repo.select_one(
        "SELECT * FROM identity_provider WHERE name = $name LIMIT 1;",
        name=provider_name,
    )
    now = _utcnow()
    provider_id = (
        _coerce_uuid(existing.get("uuid"), field_name="identity_provider.uuid")
        if existing is not None
        else uuid4()
    )
    record: SurrealRecord = {
        "uuid": str(provider_id),
        "name": provider_name,
        "issuer": issuer,
        "client_id": client_id,
        "scopes": _unique_strings(scopes),
        "role_claim": role_claim or config_module.settings.oidc.role_claim,
        "enabled": True,
        "created_at": existing.get("created_at") if existing is not None else now,
        "updated_at": now,
    }
    await repo.replace_record("identity_provider", uuid=provider_id, record=record)


async def _resolve_oidc_organization_record(
    client: QueryClient,
    *,
    organization_slug: str,
) -> SurrealRecord:
    slug = organization_slug.strip()
    if not slug:
        msg = "OIDC organization slug is required"
        raise ValueError(msg)
    organization = await _SurrealRepository(client).select_one(
        """
            SELECT * FROM organizations
            WHERE slug = $slug AND is_personal = false
            LIMIT 1;
        """,
        slug=slug,
    )
    if organization is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "oidc_organization_unavailable",
                "message": "OIDC organization is not configured",
            },
        )
    return organization


async def _ensure_oidc_organization_membership_record(
    client: QueryClient,
    *,
    user_id: UUID,
    organization: SurrealRecord,
    role: OrganizationRole,
) -> SurrealRecord:
    repo = _SurrealRepository(client)
    organization_id = _coerce_uuid(organization.get("uuid"), field_name="organization.uuid")
    membership = await repo.select_one(
        """
            SELECT * FROM organization_members
            WHERE organization_id = $organization_id AND user_id = $user_id
            LIMIT 1;
        """,
        organization_id=str(organization_id),
        user_id=str(user_id),
    )
    now = _utcnow()
    if membership is None:
        write_result = await client.execute_query(
            "CREATE organization_members CONTENT $record;",
            record={
                "uuid": str(uuid4()),
                "organization_id": str(organization_id),
                "user_id": str(user_id),
                "role": role.value,
                "created_at": now,
                "updated_at": now,
            },
        )
    elif _role_value(membership.get("role")) != role.value:
        # The verified IdP claim is authoritative here, unlike operator-driven role
        # changes: demoting the last owner is allowed, and ownership returns with the
        # next login that carries an owner claim.
        write_result = await client.execute_query(
            """
                UPDATE organization_members
                SET role = $role, updated_at = $updated_at
                WHERE uuid = $uuid;
            """,
            uuid=str(_coerce_uuid(membership.get("uuid"), field_name="membership.uuid")),
            role=role.value,
            updated_at=now,
        )
    else:
        write_result = None
    if write_result is not None:
        error = _query_error(write_result)
        if error is not None:
            raise RuntimeError(error)
        if not _normalize_records(write_result):
            msg = "Failed to write OIDC organization membership"
            raise RuntimeError(msg)
    return organization


async def login_oidc_identity(
    *,
    provider_name: str,
    issuer: str,
    organization_slug: str,
    client_id: str | None = None,
    scopes: list[str] | None = None,
    role_claim: str | None = None,
    subject: str,
    subject_key: str,
    email: str | None,
    name: str,
    avatar_url: str | None,
    role: OrganizationRole | str,
    claims: Mapping[str, object],
    request: Request,
    action: str = "auth.oidc.login",
) -> IssuedOidcSession:
    provider = provider_name.strip().lower()
    if not provider or not subject_key.strip():
        msg = "OIDC provider and subject key are required"
        raise ValueError(msg)
    org_role = _normalize_oidc_role(role)
    now = _utcnow()
    async with (
        oauth_identity_lock(provider, subject_key),
        _auth_client_scope() as client,
    ):
        repo = _SurrealRepository(client)
        oidc_organization = await _resolve_oidc_organization_record(
            client,
            organization_slug=organization_slug,
        )
        await _upsert_identity_provider(
            client,
            provider_name=provider,
            issuer=issuer,
            client_id=client_id,
            scopes=scopes or [],
            role_claim=role_claim,
        )
        identity = await repo.select_one(
            """
                SELECT * FROM user_identity
                WHERE provider_name = $provider_name AND subject_key = $subject_key
                LIMIT 1;
            """,
            provider_name=provider,
            subject_key=subject_key,
        )
        user_record: SurrealRecord | None = None
        user_id: UUID | None = None
        if identity is not None:
            user_id = _coerce_uuid(identity.get("user_id"), field_name="user_identity.user_id")
            user_record = await repo.select_one(
                "SELECT * FROM users WHERE uuid = $uuid LIMIT 1;",
                uuid=str(user_id),
            )
            if user_record is not None and user_record.get("deleted_at") is not None:
                msg = "User is scheduled for deletion"
                raise ValueError(msg)

        safe_email = await _safe_oidc_email(client, email=email, user_id=user_id)
        display_name = name.strip() or safe_email or subject
        is_admin = bool(user_record and user_record.get("is_admin"))
        if user_record is None:
            user_id = uuid4()
            create_result = await client.execute_query(
                "CREATE users CONTENT $record;",
                record={
                    "uuid": str(user_id),
                    "email": safe_email,
                    "name": display_name,
                    "avatar_url": avatar_url,
                    "github_id": None,
                    "is_admin": is_admin,
                    "bio": None,
                    "timezone": "UTC",
                    "preferences": {},
                    "password_salt": None,
                    "password_hash": None,
                    "password_iterations": None,
                    "email_verified_at": now if safe_email else None,
                    "last_login_at": now,
                    "created_at": now,
                    "updated_at": now,
                },
            )
            error = _query_error(create_result)
            if error is not None:
                raise RuntimeError(error)
            records = _normalize_records(create_result)
            if not records:
                msg = "Failed to create OIDC user"
                raise RuntimeError(msg)
            user_record = records[0]
        else:
            user_id = _coerce_uuid(user_record.get("uuid"), field_name="user.uuid")
            update_email = safe_email if safe_email is not None else user_record.get("email")
            update_result = await client.execute_query(
                """
                    UPDATE users
                    SET email = $email,
                        name = $name,
                        avatar_url = $avatar_url,
                        is_admin = $is_admin,
                        last_login_at = $last_login_at,
                        updated_at = $updated_at
                    WHERE uuid = $uuid;
                """,
                uuid=str(user_id),
                email=update_email,
                name=display_name,
                avatar_url=avatar_url,
                is_admin=is_admin,
                last_login_at=now,
                updated_at=now,
            )
            error = _query_error(update_result)
            if error is not None:
                raise RuntimeError(error)
            records = _normalize_records(update_result)
            if not records:
                msg = "Failed to update OIDC user"
                raise RuntimeError(msg)
            user_record = records[0]

        identity_id = (
            _coerce_uuid(identity.get("uuid"), field_name="user_identity.uuid")
            if identity is not None
            else uuid4()
        )
        identity_record: SurrealRecord = {
            "uuid": str(identity_id),
            "provider_name": provider,
            "issuer": issuer,
            "subject": subject,
            "subject_key": subject_key,
            "user_id": str(user_id),
            "email": safe_email,
            "claims": {str(key): value for key, value in claims.items()},
            "created_at": identity.get("created_at") if identity is not None else now,
            "updated_at": now,
            "last_login_at": now,
        }
        await repo.replace_record("user_identity", uuid=identity_id, record=identity_record)

        organization = _require_namespace(
            _auth_org_namespace(
                await _ensure_oidc_organization_membership_record(
                    client,
                    user_id=user_id,
                    organization=oidc_organization,
                    role=org_role,
                )
            ),
            label="organization",
        )
        return await _issue_oidc_session(
            client,
            user=_require_namespace(_auth_user_namespace(user_record), label="user"),
            organization=organization,
            request=request,
            action=action,
            details={
                "provider_name": provider,
                "issuer": issuer,
                "subject_key": subject_key,
                "role": org_role.value,
                "email": safe_email,
            },
        )


async def login_github_identity(*, identity, request) -> IssuedAuthSession:
    async with (
        oauth_identity_lock("github", identity.github_id),
        first_user_admin_lock(),
        _auth_client_scope() as client,
    ):
        users = SurrealUserRepository.from_client(client)
        is_first_user = not await users.has_any_users()
        user = await users.upsert_from_github(identity, is_admin=is_first_user)
        organization = _require_namespace(
            _auth_org_namespace(await _ensure_personal_org_membership_record(client, user)),
            label="organization",
        )
        return await _issue_auth_session(
            client,
            user=_require_namespace(
                _auth_user_namespace(
                    {
                        "uuid": str(user.id),
                        "email": user.email,
                        "name": user.name,
                        "avatar_url": user.avatar_url,
                        "github_id": user.github_id,
                        "is_admin": user.is_admin,
                        "bio": user.bio,
                        "timezone": user.timezone,
                        "preferences": dict(user.preferences),
                    }
                ),
                label="user",
            ),
            organization=organization,
            request=request,
            action="auth.github.login",
            details={"github_id": user.github_id, "email": user.email},
        )


async def signup_local_user(*, email: str, password: str, name: str, request):
    async with signup_email_lock(email), first_user_admin_lock(), _auth_client_scope() as client:
        users = SurrealUserRepository.from_client(client)
        is_first_user = not await users.has_any_users()
        user = await users.create_local_user(
            email=email,
            password=password,
            name=name,
            is_admin=is_first_user,
        )
        organization = _require_namespace(
            _auth_org_namespace(await _ensure_personal_org_membership_record(client, user)),
            label="organization",
        )
        return await _issue_auth_session(
            client,
            user=_require_namespace(
                _auth_user_namespace(
                    {
                        "uuid": str(user.id),
                        "email": user.email,
                        "name": user.name,
                        "avatar_url": user.avatar_url,
                        "github_id": user.github_id,
                        "is_admin": user.is_admin,
                        "bio": user.bio,
                        "timezone": user.timezone,
                        "preferences": dict(user.preferences),
                    }
                ),
                label="user",
            ),
            organization=organization,
            request=request,
            action="auth.local.signup",
            details={"email": user.email},
        )


async def delete_failed_local_signup_user(*, user_id: UUID, organization_id: UUID | None) -> None:
    statements = ["DELETE FROM user_sessions WHERE user_id = $user_id;"]
    if organization_id is not None:
        statements.append(
            "DELETE FROM organization_members "
            "WHERE user_id = $user_id AND organization_id = $organization_id;"
        )
        statements.append(
            "DELETE FROM organizations WHERE uuid = $organization_id AND is_personal = true;"
        )
    statements.append("DELETE FROM users WHERE uuid = $user_id;")
    body = "\n".join(statements)
    async with _auth_client_scope() as client:
        await _execute_raw_statement_records(
            client,
            f"BEGIN TRANSACTION;\n{body}\nCOMMIT TRANSACTION;",
            user_id=str(user_id),
            organization_id=str(organization_id) if organization_id is not None else None,
        )


def _break_glass_audit_details(
    *,
    user: object,
    reason: str | None,
) -> SurrealRecord:
    now = datetime.now(UTC)
    expires_at = config_module.settings.break_glass_expires_at
    email = str(getattr(user, "email", ""))
    actor_name = getattr(user, "name", None)
    normalized_reason = (reason or "").strip()
    if not normalized_reason:
        msg = "Break-glass reason is required"
        raise ValueError(msg)
    details: SurrealRecord = {
        "break_glass": True,
        "email": email,
        "actor_name": actor_name,
        "reason": normalized_reason,
        "started_at": now.isoformat(),
    }
    if expires_at is not None:
        details["expires_at"] = expires_at.isoformat()
    return details


async def login_local_user(
    *,
    email: str,
    password: str,
    request,
    break_glass_reason: str | None = None,
):
    async with _auth_client_scope() as client:
        users = SurrealUserRepository.from_client(client)
        user = await users.authenticate_local(email=email, password=password)
        if user is None:
            return None
        organization = _require_namespace(
            _auth_org_namespace(await _ensure_personal_org_membership_record(client, user)),
            label="organization",
        )
        break_glass = config_module.settings.break_glass_enabled
        details: SurrealRecord
        details = (
            _break_glass_audit_details(user=user, reason=break_glass_reason)
            if break_glass
            else {"break_glass": False, "email": user.email}
        )
        return await _issue_auth_session(
            client,
            user=_require_namespace(
                _auth_user_namespace(
                    {
                        "uuid": str(user.id),
                        "email": user.email,
                        "name": user.name,
                        "avatar_url": user.avatar_url,
                        "github_id": user.github_id,
                        "is_admin": user.is_admin,
                        "bio": user.bio,
                        "timezone": user.timezone,
                        "preferences": dict(user.preferences),
                    }
                ),
                label="user",
            ),
            organization=organization,
            request=request,
            action="auth.break_glass.login" if break_glass else "auth.local.login",
            details=details,
        )
