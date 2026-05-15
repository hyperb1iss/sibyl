"""Surreal-backed organization runtime adapters."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Protocol
from uuid import UUID, uuid4

import structlog
from fastapi import HTTPException, status
from starlette.requests import Request

from sibyl import config as config_module
from sibyl.auth.http import select_access_token
from sibyl.auth.jwt import create_access_token, create_refresh_token
from sibyl.auth.primitives import generate_invite_token, slugify
from sibyl.persistence.auth_runtime import log_audit_event
from sibyl.persistence.organization_common import (
    InvitationAcceptance,
    InvitationRecord,
    OrgAuthResult,
    OrgMemberChange,
    OrgRoleResult,
    OrgSummary,
    ProjectMemberChange,
    ProjectMembersResult,
    can_manage_project_members,
)
from sibyl.persistence.surreal.auth import surreal_auth_client_scope
from sibyl.persistence.surreal.auth_runtime import SurrealSessionRepository
from sibyl_core.auth import AuthUser, OrganizationRole, ProjectRole

log = structlog.get_logger()
type SurrealRecord = dict[str, object]
_OWNER_ROLE_DETAIL = "Only organization owners can manage owner roles"


async def ensure_graph_indexes(group_id: str) -> None:
    from sibyl.persistence.graph_runtime import ensure_graph_indexes as service

    await service(group_id)


async def delete_graph_data(group_id: str) -> None:
    from sibyl.persistence.graph_runtime import delete_graph_data as service

    await service(group_id)


class QueryClient(Protocol):
    async def execute_query(self, query: str, **params: object) -> object: ...


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _normalize_record(record: object) -> SurrealRecord | None:
    if record is None or not isinstance(record, dict):
        return None
    out = {str(key): value for key, value in record.items()}
    out.pop("id", None)
    return out


def _normalize_records(result: object) -> list[SurrealRecord]:
    if result is None:
        return []
    if isinstance(result, dict):
        record = _normalize_record(result)
        return [record] if record is not None else []
    if not isinstance(result, list):
        return []

    records: list[SurrealRecord] = []
    for item in result:
        if isinstance(item, list):
            for nested in item:
                record = _normalize_record(nested)
                if record is not None:
                    records.append(record)
            continue
        record = _normalize_record(item)
        if record is not None:
            records.append(record)
    return records


def _enforce_owner_role_boundary(
    *,
    actor_role: OrganizationRole,
    current_role: OrganizationRole | None = None,
    requested_role: OrganizationRole | None = None,
) -> None:
    if actor_role is OrganizationRole.OWNER:
        return
    if current_role is OrganizationRole.OWNER or requested_role is OrganizationRole.OWNER:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=_OWNER_ROLE_DETAIL)


def _record_payload(value: object) -> SurrealRecord:
    if not isinstance(value, dict):
        return {}
    return {str(key): item for key, item in value.items()}


def _query_error(result: object) -> str | None:
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        payload = {str(key): value for key, value in result.items()}
        status_value = payload.get("status")
        if isinstance(status_value, str) and status_value.upper() == "ERR":
            detail = payload.get("detail") or payload.get("result") or payload
            return str(detail)
        return None
    if not isinstance(result, list):
        return None
    for item in result:
        error = _query_error(item)
        if error is not None:
            return error
    return None


def _is_uniqueness_error(error: str) -> bool:
    lowered = error.lower()
    return "unique" in lowered or "already contains" in lowered


def _coerce_uuid(value: object | None, *, field_name: str) -> UUID:
    if isinstance(value, UUID):
        return value
    if isinstance(value, str):
        return UUID(value)
    msg = f"{field_name} is required"
    raise TypeError(msg)


def _coerce_datetime(value: object | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            return value.astimezone(UTC).replace(tzinfo=None)
        return value
    if isinstance(value, str):
        normalized = value.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is not None:
            return parsed.astimezone(UTC).replace(tzinfo=None)
        return parsed
    return None


async def _list_user_records_by_id(
    client: QueryClient,
    user_ids: list[UUID],
) -> dict[UUID, SurrealRecord]:
    user_id_strings: list[str] = []
    seen_ids: set[str] = set()
    for user_id in user_ids:
        user_id_string = str(user_id)
        if user_id_string in seen_ids:
            continue
        seen_ids.add(user_id_string)
        user_id_strings.append(user_id_string)
    if not user_id_strings:
        return {}

    records = _normalize_records(
        await client.execute_query(
            "SELECT * FROM users WHERE uuid IN $user_ids;",
            user_ids=user_id_strings,
        )
    )
    return {_coerce_uuid(record.get("uuid"), field_name="user.uuid"): record for record in records}


def _member_user_payload(
    record: Mapping[str, object], *, include_github_id: bool = False
) -> SurrealRecord:
    payload = {
        "id": str(_coerce_uuid(record.get("uuid"), field_name="user.uuid")),
        "email": record.get("email"),
        "name": record.get("name"),
        "avatar_url": record.get("avatar_url"),
    }
    if include_github_id:
        payload["github_id"] = record.get("github_id")
    return payload


def _invitation_from_record(
    record: Mapping[str, object], *, include_accept_url: bool = False
) -> InvitationRecord:
    invitation = InvitationRecord(
        id=_coerce_uuid(record.get("uuid"), field_name="organization_invitations.uuid"),
        email=str(record.get("invited_email") or ""),
        role=OrganizationRole(str(record.get("invited_role") or OrganizationRole.MEMBER.value)),
        created_at=_coerce_datetime(record.get("created_at")),
        expires_at=_coerce_datetime(record.get("expires_at")),
    )
    if include_accept_url:
        invitation.accept_url = (
            f"{config_module.settings.server_url}/api/invitations/{record['token']}/accept"
        )
    return invitation


@asynccontextmanager
async def _auth_client_scope() -> AsyncIterator[QueryClient]:
    async with surreal_auth_client_scope() as client:
        yield client


async def _rotate_or_create_org_session(
    *,
    client: QueryClient,
    request: Request,
    user_id: UUID,
    organization_id: UUID,
) -> tuple[str, str, datetime]:
    current = select_access_token(
        authorization=request.headers.get("authorization"),
        cookie_token=request.cookies.get("sibyl_access_token"),
    )

    access_expires = _utcnow() + timedelta(
        minutes=config_module.settings.access_token_expire_minutes
    )
    sessions = SurrealSessionRepository.from_client(client)
    existing = await sessions.get_session_by_token(current) if current else None
    session_id = existing.id if existing is not None else uuid4()
    access_token = create_access_token(
        user_id=user_id,
        organization_id=organization_id,
        session_id=session_id,
    )
    refresh_token, refresh_expires = create_refresh_token(
        user_id=user_id,
        organization_id=organization_id,
        session_id=session_id,
    )
    if existing is not None:
        await sessions.rotate_tokens(
            existing,
            new_access_token=access_token,
            new_access_expires_at=access_expires,
            new_refresh_token=refresh_token,
            new_refresh_expires_at=refresh_expires,
        )
        return access_token, refresh_token, refresh_expires

    await sessions.create_session(
        user_id=user_id,
        organization_id=organization_id,
        token=access_token,
        expires_at=access_expires,
        session_id=session_id,
        refresh_token=refresh_token,
        refresh_token_expires_at=refresh_expires,
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
    return access_token, refresh_token, refresh_expires


async def _require_org_admin(
    *,
    slug: str,
    user_id: UUID,
) -> tuple[SimpleNamespace, SimpleNamespace]:
    async with _auth_client_scope() as client:
        organization, membership = await _load_org_role_records(
            client,
            slug=slug,
            user_id=user_id,
        )
        if organization is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
        if membership is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
        role = OrganizationRole(str(membership.get("role") or OrganizationRole.MEMBER.value))
        if role not in {OrganizationRole.OWNER, OrganizationRole.ADMIN}:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")
        organization_id = _coerce_uuid(organization.get("uuid"), field_name="organization.uuid")
        membership_id = _coerce_uuid(membership.get("uuid"), field_name="membership.uuid")
        return (
            SimpleNamespace(
                id=organization_id,
                slug=str(organization.get("slug") or ""),
                name=str(organization.get("name") or ""),
                is_personal=bool(organization.get("is_personal", False)),
            ),
            SimpleNamespace(
                id=membership_id,
                organization_id=organization_id,
                user_id=user_id,
                role=role,
            ),
        )


async def _replace_org_invitation_record(
    client: QueryClient, *, uuid: UUID, record: SurrealRecord
) -> SurrealRecord:
    created = _normalize_records(
        await client.execute_query(
            "UPSERT organization_invitations CONTENT $record WHERE uuid = $uuid;",
            uuid=str(uuid),
            record=record,
        )
    )
    if not created:
        msg = f"Failed to write organization invitation {uuid}"
        raise RuntimeError(msg)
    return created[0]


async def _load_org_member_add_records(
    client: QueryClient,
    *,
    slug: str,
    actor_id: UUID,
    target_user_id: UUID,
) -> tuple[
    SurrealRecord | None,
    SurrealRecord | None,
    SurrealRecord | None,
    SurrealRecord | None,
    list[SurrealRecord],
]:
    payload = await client.execute_query(
        """
            RETURN {
                organization: (SELECT * FROM organizations WHERE slug = $slug LIMIT 1)[0],
                actor_membership: (
                    SELECT * FROM organization_members
                    WHERE organization_id IN (
                        SELECT VALUE uuid FROM organizations WHERE slug = $slug LIMIT 1
                    )
                        AND user_id = $actor_id
                    LIMIT 1
                )[0],
                target_user: (SELECT * FROM users WHERE uuid = $target_user_id LIMIT 1)[0],
                target_membership: (
                    SELECT * FROM organization_members
                    WHERE organization_id IN (
                        SELECT VALUE uuid FROM organizations WHERE slug = $slug LIMIT 1
                    )
                        AND user_id = $target_user_id
                    LIMIT 1
                )[0],
                owner_memberships: (
                    SELECT uuid FROM organization_members
                    WHERE organization_id IN (
                        SELECT VALUE uuid FROM organizations WHERE slug = $slug LIMIT 1
                    )
                        AND role = $owner_role
                ),
            };
        """,
        slug=slug,
        actor_id=str(actor_id),
        target_user_id=str(target_user_id),
        owner_role=OrganizationRole.OWNER.value,
    )
    payload = _record_payload(payload)
    return (
        _normalize_record(payload.get("organization")),
        _normalize_record(payload.get("actor_membership")),
        _normalize_record(payload.get("target_user")),
        _normalize_record(payload.get("target_membership")),
        _normalize_records(payload.get("owner_memberships")),
    )


async def _load_org_member_mutation_records(
    client: QueryClient,
    *,
    slug: str,
    actor_id: UUID,
    target_user_id: UUID,
) -> tuple[
    SurrealRecord | None,
    SurrealRecord | None,
    SurrealRecord | None,
    list[SurrealRecord],
]:
    payload = await client.execute_query(
        """
            RETURN {
                organization: (SELECT * FROM organizations WHERE slug = $slug LIMIT 1)[0],
                actor_membership: (
                    SELECT * FROM organization_members
                    WHERE organization_id IN (
                        SELECT VALUE uuid FROM organizations WHERE slug = $slug LIMIT 1
                    )
                        AND user_id = $actor_id
                    LIMIT 1
                )[0],
                target_membership: (
                    SELECT * FROM organization_members
                    WHERE organization_id IN (
                        SELECT VALUE uuid FROM organizations WHERE slug = $slug LIMIT 1
                    )
                        AND user_id = $target_user_id
                    LIMIT 1
                )[0],
                owner_memberships: (
                    SELECT uuid FROM organization_members
                    WHERE organization_id IN (
                        SELECT VALUE uuid FROM organizations WHERE slug = $slug LIMIT 1
                    )
                        AND role = $owner_role
                ),
            };
        """,
        slug=slug,
        actor_id=str(actor_id),
        target_user_id=str(target_user_id),
        owner_role=OrganizationRole.OWNER.value,
    )
    payload = _record_payload(payload)
    return (
        _normalize_record(payload.get("organization")),
        _normalize_record(payload.get("actor_membership")),
        _normalize_record(payload.get("target_membership")),
        _normalize_records(payload.get("owner_memberships")),
    )


async def _load_org_role_records(
    client: QueryClient,
    *,
    slug: str,
    user_id: UUID,
) -> tuple[SurrealRecord | None, SurrealRecord | None]:
    payload = await client.execute_query(
        """
            RETURN {
                organization: (SELECT * FROM organizations WHERE slug = $slug LIMIT 1)[0],
                membership: (
                    SELECT * FROM organization_members
                    WHERE organization_id IN (
                        SELECT VALUE uuid FROM organizations WHERE slug = $slug LIMIT 1
                    )
                        AND user_id = $user_id
                    LIMIT 1
                )[0],
            };
        """,
        slug=slug,
        user_id=str(user_id),
    )
    payload = _record_payload(payload)
    return _normalize_record(payload.get("organization")), _normalize_record(
        payload.get("membership")
    )


async def _load_org_member_list_records(
    client: QueryClient,
    *,
    slug: str,
    actor_id: UUID,
) -> tuple[
    SurrealRecord | None,
    SurrealRecord | None,
    list[SurrealRecord],
    list[SurrealRecord],
]:
    payload = await client.execute_query(
        """
            RETURN {
                organization: (SELECT * FROM organizations WHERE slug = $slug LIMIT 1)[0],
                actor_membership: (
                    SELECT * FROM organization_members
                    WHERE organization_id IN (
                        SELECT VALUE uuid FROM organizations WHERE slug = $slug LIMIT 1
                    )
                        AND user_id = $actor_id
                    LIMIT 1
                )[0],
                memberships: (
                    SELECT * FROM organization_members
                    WHERE organization_id IN (
                        SELECT VALUE uuid FROM organizations WHERE slug = $slug LIMIT 1
                    )
                    ORDER BY created_at ASC
                ),
                users: (
                    SELECT * FROM users
                    WHERE uuid IN (
                        SELECT VALUE user_id FROM organization_members
                        WHERE organization_id IN (
                            SELECT VALUE uuid FROM organizations WHERE slug = $slug LIMIT 1
                        )
                    )
                ),
            };
        """,
        slug=slug,
        actor_id=str(actor_id),
    )
    payload = _record_payload(payload)
    return (
        _normalize_record(payload.get("organization")),
        _normalize_record(payload.get("actor_membership")),
        _normalize_records(payload.get("memberships")),
        _normalize_records(payload.get("users")),
    )


async def _load_invitation_accept_records(
    client: QueryClient,
    *,
    token: str,
    user_id: UUID,
) -> tuple[SurrealRecord | None, SurrealRecord | None, SurrealRecord | None]:
    payload = await client.execute_query(
        """
            RETURN {
                invitation: (
                    SELECT * FROM organization_invitations
                    WHERE token = $token
                    LIMIT 1
                )[0],
                organization: (
                    SELECT * FROM organizations
                    WHERE uuid IN (
                        SELECT VALUE organization_id FROM organization_invitations
                        WHERE token = $token
                        LIMIT 1
                    )
                    LIMIT 1
                )[0],
                membership: (
                    SELECT * FROM organization_members
                    WHERE organization_id IN (
                        SELECT VALUE organization_id FROM organization_invitations
                        WHERE token = $token
                        LIMIT 1
                    )
                        AND user_id = $user_id
                    LIMIT 1
                )[0],
            };
        """,
        token=token,
        user_id=str(user_id),
    )
    payload = _record_payload(payload)
    return (
        _normalize_record(payload.get("invitation")),
        _normalize_record(payload.get("organization")),
        _normalize_record(payload.get("membership")),
    )


async def list_orgs(*, user_id: UUID) -> list[OrgSummary]:
    async with _auth_client_scope() as client:
        payload = await client.execute_query(
            """
                RETURN {
                    memberships: (
                        SELECT * FROM organization_members
                        WHERE user_id = $user_id
                        ORDER BY created_at ASC
                    ),
                    organizations: (
                        SELECT * FROM organizations
                        WHERE uuid IN (
                            SELECT VALUE organization_id FROM organization_members
                            WHERE user_id = $user_id
                        )
                    ),
                };
            """,
            user_id=str(user_id),
        )
        payload = _record_payload(payload)
        records = _normalize_records(payload.get("memberships"))
        organizations = _normalize_records(payload.get("organizations"))
        organizations_by_id = {str(record.get("uuid")): record for record in organizations}

        summaries: list[OrgSummary] = []
        for record in records:
            org_id = str(record.get("organization_id") or "")
            organization = organizations_by_id.get(org_id)
            if organization is None:
                continue
            summaries.append(
                OrgSummary(
                    id=_coerce_uuid(organization.get("uuid"), field_name="organization.uuid"),
                    slug=str(organization.get("slug") or ""),
                    name=str(organization.get("name") or ""),
                    is_personal=bool(organization.get("is_personal", False)),
                    role=OrganizationRole(str(record.get("role") or OrganizationRole.MEMBER.value)),
                )
            )
        summaries.sort(key=lambda item: item.slug)
        return summaries


async def list_org_ids() -> list[str]:
    async with _auth_client_scope() as client:
        organizations = _normalize_records(
            await client.execute_query(
                "SELECT uuid, created_at FROM organizations ORDER BY created_at ASC LIMIT $limit;",
                limit=100_000,
            )
        )
        return [str(record["uuid"]) for record in organizations if record.get("uuid") is not None]


async def create_org(
    *,
    request: Request,
    user_id: UUID,
    name: str,
    slug: str | None = None,
) -> OrgAuthResult:
    async with _auth_client_scope() as client:
        resolved_slug = slugify(slug or name)
        existing = _normalize_records(
            await client.execute_query(
                "SELECT uuid FROM organizations WHERE slug = $slug LIMIT 1;",
                slug=resolved_slug,
            )
        )
        if existing:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Slug already taken")

        now = _utcnow()
        create_result = await client.execute_query(
            "CREATE organizations CONTENT $record;",
            record={
                "uuid": str(uuid4()),
                "name": name,
                "slug": resolved_slug,
                "is_personal": False,
                "settings": {},
                "created_at": now,
                "updated_at": now,
            },
        )
        error = _query_error(create_result)
        if error is not None:
            if _is_uniqueness_error(error):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Slug already taken",
                )
            raise RuntimeError(error)
        created = _normalize_records(create_result)
        if not created:
            msg = "Failed to create organization"
            raise RuntimeError(msg)
        organization = created[0]
        organization_id = _coerce_uuid(organization.get("uuid"), field_name="organization.uuid")

        membership_result = await client.execute_query(
            "CREATE organization_members CONTENT $record;",
            record={
                "uuid": str(uuid4()),
                "organization_id": str(organization_id),
                "user_id": str(user_id),
                "role": OrganizationRole.OWNER.value,
                "created_at": now,
                "updated_at": now,
            },
        )
        error = _query_error(membership_result)
        if error is not None:
            raise RuntimeError(error)
        if not _normalize_records(membership_result):
            msg = "Failed to create organization owner membership"
            raise RuntimeError(msg)

        try:
            await ensure_graph_indexes(str(organization_id))
        except Exception as exc:
            log.debug(
                "Graph index setup deferred",
                org_id=str(organization_id),
                error=str(exc),
            )

        access_token, refresh_token, refresh_expires = await _rotate_or_create_org_session(
            client=client,
            request=request,
            user_id=user_id,
            organization_id=organization_id,
        )
        await log_audit_event(
            action="org.create",
            user_id=user_id,
            organization_id=organization_id,
            request=request,
            details={
                "slug": str(organization.get("slug") or ""),
                "name": str(organization.get("name") or ""),
            },
        )
        return OrgAuthResult(
            id=organization_id,
            slug=str(organization.get("slug") or ""),
            name=str(organization.get("name") or ""),
            access_token=access_token,
            refresh_token=refresh_token,
            refresh_expires=refresh_expires,
        )


async def get_org(*, slug: str, user_id: UUID) -> OrgRoleResult:
    async with _auth_client_scope() as client:
        organization, membership = await _load_org_role_records(
            client,
            slug=slug,
            user_id=user_id,
        )
        if organization is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
        if membership is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
        return OrgRoleResult(
            id=_coerce_uuid(organization.get("uuid"), field_name="organization.uuid"),
            slug=str(organization.get("slug") or ""),
            name=str(organization.get("name") or ""),
            role=OrganizationRole(str(membership.get("role") or OrganizationRole.MEMBER.value)),
        )


async def switch_org(
    *,
    request: Request,
    slug: str,
    user_id: UUID,
) -> OrgAuthResult:
    async with _auth_client_scope() as client:
        organization, membership = await _load_org_role_records(
            client,
            slug=slug,
            user_id=user_id,
        )
        if organization is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
        if membership is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")

        organization_id = _coerce_uuid(organization.get("uuid"), field_name="organization.uuid")
        access_token, refresh_token, refresh_expires = await _rotate_or_create_org_session(
            client=client,
            request=request,
            user_id=user_id,
            organization_id=organization_id,
        )
        await log_audit_event(
            action="org.switch",
            user_id=user_id,
            organization_id=organization_id,
            request=request,
            details={
                "slug": str(organization.get("slug") or ""),
                "name": str(organization.get("name") or ""),
            },
        )
        return OrgAuthResult(
            id=organization_id,
            slug=str(organization.get("slug") or ""),
            name=str(organization.get("name") or ""),
            access_token=access_token,
            refresh_token=refresh_token,
            refresh_expires=refresh_expires,
        )


async def update_org(
    *,
    request: Request,
    slug: str,
    user_id: UUID,
    name: str | None = None,
    new_slug: str | None = None,
) -> OrgSummary:
    async with _auth_client_scope() as client:
        organization, membership = await _load_org_role_records(
            client,
            slug=slug,
            user_id=user_id,
        )
        if organization is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
        if membership is None:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")
        role = OrganizationRole(str(membership.get("role") or OrganizationRole.MEMBER.value))
        if role not in {OrganizationRole.OWNER, OrganizationRole.ADMIN}:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")

        organization_id = _coerce_uuid(organization.get("uuid"), field_name="organization.uuid")
        organization_slug = str(organization.get("slug") or "")
        resolved_slug = slugify(new_slug) if new_slug else None
        if resolved_slug and resolved_slug != organization_slug:
            existing = _normalize_records(
                await client.execute_query(
                    "SELECT * FROM organizations WHERE slug = $slug LIMIT 1;",
                    slug=resolved_slug,
                )
            )
            if existing:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Slug already taken",
                )

        update_params: dict[str, object] = {
            "uuid": str(organization_id),
            "updated_at": _utcnow(),
        }
        if name is not None and resolved_slug is not None:
            update_params["name"] = name
            update_params["slug"] = resolved_slug
            update_query = (
                "UPDATE organizations "
                "SET name = $name, slug = $slug, updated_at = $updated_at "
                "WHERE uuid = $uuid;"
            )
        elif name is not None:
            update_params["name"] = name
            update_query = (
                "UPDATE organizations SET name = $name, updated_at = $updated_at "
                "WHERE uuid = $uuid;"
            )
        elif resolved_slug is not None:
            update_params["slug"] = resolved_slug
            update_query = (
                "UPDATE organizations SET slug = $slug, updated_at = $updated_at "
                "WHERE uuid = $uuid;"
            )
        else:
            update_query = "UPDATE organizations SET updated_at = $updated_at WHERE uuid = $uuid;"

        update_result = await client.execute_query(update_query, **update_params)
        error = _query_error(update_result)
        if error is not None:
            if _is_uniqueness_error(error):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Slug already taken",
                )
            raise RuntimeError(error)
        updated_records = _normalize_records(update_result)
        updated = updated_records[0] if updated_records else None
        if updated is None:
            msg = f"Organization disappeared during update: {organization_id}"
            raise RuntimeError(msg)
        await log_audit_event(
            action="org.update",
            user_id=user_id,
            organization_id=organization_id,
            request=request,
            details={
                "slug": slug,
                "new_slug": str(updated.get("slug") or ""),
                "name": str(updated.get("name") or ""),
            },
        )
        return OrgSummary(
            id=_coerce_uuid(updated.get("uuid"), field_name="organization.uuid"),
            slug=str(updated.get("slug") or ""),
            name=str(updated.get("name") or ""),
            is_personal=bool(updated.get("is_personal", False)),
            role=role,
        )


async def _delete_org_auth_child_records(client, *, organization_id: UUID) -> None:
    team_rows = _normalize_records(
        await client.execute_query(
            "SELECT * FROM teams WHERE organization_id = $organization_id;",
            organization_id=str(organization_id),
        )
    )
    api_key_rows = _normalize_records(
        await client.execute_query(
            "SELECT * FROM api_keys WHERE organization_id = $organization_id;",
            organization_id=str(organization_id),
        )
    )

    team_ids = [str(team["uuid"]) for team in team_rows if team.get("uuid") is not None]
    if team_ids:
        await client.execute_query(
            "DELETE FROM team_members WHERE team_id IN $team_ids;",
            team_ids=team_ids,
        )

    api_key_ids = [
        str(api_key["uuid"]) for api_key in api_key_rows if api_key.get("uuid") is not None
    ]
    if api_key_ids:
        await client.execute_query(
            "DELETE FROM api_key_project_scopes WHERE api_key_id IN $api_key_ids;",
            api_key_ids=api_key_ids,
        )


async def delete_org(*, request: Request, slug: str, user_id: UUID) -> None:
    async with _auth_client_scope() as client:
        organization, membership = await _load_org_role_records(
            client,
            slug=slug,
            user_id=user_id,
        )
        if organization is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
        organization_id = _coerce_uuid(organization.get("uuid"), field_name="organization.uuid")
        organization_slug = str(organization.get("slug") or "")
        organization_name = str(organization.get("name") or "")
        if bool(organization.get("is_personal", False)):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Cannot delete personal organization",
            )

        if membership is None:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")
        role = OrganizationRole(str(membership.get("role") or OrganizationRole.MEMBER.value))
        if role is not OrganizationRole.OWNER:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")

        await log_audit_event(
            action="org.delete",
            user_id=user_id,
            organization_id=organization_id,
            request=request,
            details={"slug": organization_slug, "name": organization_name},
        )

        await _delete_org_auth_child_records(client, organization_id=organization_id)

        for query in (
            "DELETE FROM team_projects WHERE organization_id = $organization_id;",
            "DELETE FROM project_members WHERE organization_id = $organization_id;",
            "DELETE FROM projects WHERE organization_id = $organization_id;",
            "DELETE FROM organization_invitations WHERE organization_id = $organization_id;",
            "DELETE FROM organization_members WHERE organization_id = $organization_id;",
            "DELETE FROM teams WHERE organization_id = $organization_id;",
            "DELETE FROM api_keys WHERE organization_id = $organization_id;",
            "DELETE FROM user_sessions WHERE organization_id = $organization_id;",
            "DELETE FROM device_authorization_requests WHERE organization_id = $organization_id;",
        ):
            await client.execute_query(query, organization_id=str(organization_id))

        from sibyl.persistence.surreal.content import (
            delete_crawl_source_record,
            surreal_content_client,
        )

        async with surreal_content_client() as content_client:
            source_rows = _normalize_records(
                await content_client.execute_query(
                    "SELECT * FROM crawl_sources WHERE organization_id = $organization_id;",
                    organization_id=str(organization_id),
                )
            )
            for source_row in source_rows:
                await delete_crawl_source_record(
                    None,
                    source_id=_coerce_uuid(source_row.get("uuid"), field_name="crawl_sources.uuid"),
                    organization_id=organization_id,
                )

            for query in (
                "DELETE FROM raw_captures WHERE organization_id = $organization_id;",
                "DELETE FROM backups WHERE organization_id = $organization_id;",
                "DELETE FROM backup_settings WHERE organization_id = $organization_id;",
            ):
                await content_client.execute_query(query, organization_id=str(organization_id))

        await delete_graph_data(str(organization_id))

        await client.execute_query(
            "DELETE FROM organizations WHERE uuid = $organization_id;",
            organization_id=str(organization_id),
        )


async def list_org_members(*, slug: str, actor_id: UUID) -> list[dict[str, object]]:
    async with _auth_client_scope() as client:
        organization, actor_membership, records, users = await _load_org_member_list_records(
            client,
            slug=slug,
            actor_id=actor_id,
        )
        if organization is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
        if actor_membership is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")

        users_by_id = {
            _coerce_uuid(record.get("uuid"), field_name="user.uuid"): record for record in users
        }
        rows: list[dict[str, object]] = []
        for membership in records:
            user_id = _coerce_uuid(membership.get("user_id"), field_name="membership.user_id")
            user = users_by_id.get(user_id)
            if user is None:
                continue
            rows.append(
                {
                    "user": _member_user_payload(user, include_github_id=True),
                    "role": str(membership.get("role") or OrganizationRole.MEMBER.value),
                    "created_at": _coerce_datetime(membership.get("created_at")),
                }
            )
        return rows


async def add_org_member(
    *,
    slug: str,
    actor_id: UUID,
    target_user_id: UUID,
    role: OrganizationRole,
    request: Request,
) -> OrgMemberChange:
    async with _auth_client_scope() as client:
        (
            organization,
            actor_membership,
            target_user,
            target_membership,
            owner_memberships,
        ) = await _load_org_member_add_records(
            client,
            slug=slug,
            actor_id=actor_id,
            target_user_id=target_user_id,
        )
        if organization is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
        if actor_membership is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
        actor_role = OrganizationRole(
            str(actor_membership.get("role") or OrganizationRole.MEMBER.value)
        )
        if actor_role not in {OrganizationRole.OWNER, OrganizationRole.ADMIN}:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")
        _enforce_owner_role_boundary(actor_role=actor_role, requested_role=role)
        if target_user is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

        organization_id = _coerce_uuid(organization.get("uuid"), field_name="organization.uuid")
        now = _utcnow()
        if target_membership is not None:
            target_role = OrganizationRole(
                str(target_membership.get("role") or OrganizationRole.MEMBER.value)
            )
            _enforce_owner_role_boundary(
                actor_role=actor_role,
                current_role=target_role,
                requested_role=role,
            )
            if target_role is OrganizationRole.OWNER and role is not OrganizationRole.OWNER:
                if len(owner_memberships) <= 1:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail="Cannot demote the last organization owner",
                    )
            write_result = await client.execute_query(
                """
                    UPDATE organization_members
                    SET role = $role,
                        updated_at = $updated_at
                    WHERE uuid = $uuid;
                """,
                uuid=str(_coerce_uuid(target_membership.get("uuid"), field_name="membership.uuid")),
                role=role.value,
                updated_at=now,
            )
        else:
            write_result = await client.execute_query(
                "CREATE organization_members CONTENT $record;",
                record={
                    "uuid": str(uuid4()),
                    "organization_id": str(organization_id),
                    "user_id": str(target_user_id),
                    "role": role.value,
                    "created_at": now,
                    "updated_at": now,
                },
            )
        error = _query_error(write_result)
        if error is not None:
            if _is_uniqueness_error(error):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="User is already a member",
                )
            raise RuntimeError(error)
        written = _normalize_records(write_result)
        if not written:
            msg = "Failed to write organization membership"
            raise RuntimeError(msg)
        membership = written[0]
        await log_audit_event(
            action="org.member.add",
            user_id=actor_id,
            organization_id=organization_id,
            request=request,
            details={"target_user_id": str(target_user_id), "role": role.value},
        )
        return OrgMemberChange(
            org_id=organization_id,
            user_id=_coerce_uuid(membership.get("user_id"), field_name="membership.user_id"),
            role=OrganizationRole(str(membership.get("role") or role.value)),
        )


async def update_org_member_role(
    *,
    slug: str,
    actor_id: UUID,
    target_user_id: UUID,
    role: OrganizationRole,
    request: Request,
) -> OrgMemberChange:
    async with _auth_client_scope() as client:
        (
            organization,
            actor_membership,
            target_membership,
            owner_memberships,
        ) = await _load_org_member_mutation_records(
            client,
            slug=slug,
            actor_id=actor_id,
            target_user_id=target_user_id,
        )
        if organization is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
        if actor_membership is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
        actor_role = OrganizationRole(
            str(actor_membership.get("role") or OrganizationRole.MEMBER.value)
        )
        if actor_role not in {OrganizationRole.OWNER, OrganizationRole.ADMIN}:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")
        if target_membership is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="User is not a member of this organization",
            )
        target_role = OrganizationRole(
            str(target_membership.get("role") or OrganizationRole.MEMBER.value)
        )
        _enforce_owner_role_boundary(
            actor_role=actor_role,
            current_role=target_role,
            requested_role=role,
        )
        if target_role is OrganizationRole.OWNER and role is not OrganizationRole.OWNER:
            if len(owner_memberships) <= 1:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Cannot demote the last organization owner",
                )

        update_result = await client.execute_query(
            """
                UPDATE organization_members
                SET role = $role,
                    updated_at = $updated_at
                WHERE uuid = $uuid;
            """,
            uuid=str(_coerce_uuid(target_membership.get("uuid"), field_name="membership.uuid")),
            role=role.value,
            updated_at=_utcnow(),
        )
        error = _query_error(update_result)
        if error is not None:
            raise RuntimeError(error)
        written = _normalize_records(update_result)
        if not written:
            msg = "Failed to update organization membership"
            raise RuntimeError(msg)
        membership = written[0]
        organization_id = _coerce_uuid(organization.get("uuid"), field_name="organization.uuid")

        await log_audit_event(
            action="org.member.update_role",
            user_id=actor_id,
            organization_id=organization_id,
            request=request,
            details={"target_user_id": str(target_user_id), "role": role.value},
        )
        return OrgMemberChange(
            org_id=organization_id,
            user_id=_coerce_uuid(membership.get("user_id"), field_name="membership.user_id"),
            role=OrganizationRole(str(membership.get("role") or role.value)),
        )


async def remove_org_member(
    *,
    slug: str,
    actor_id: UUID,
    target_user_id: UUID,
    request: Request,
) -> OrgMemberChange:
    async with _auth_client_scope() as client:
        (
            organization,
            actor_membership,
            target_membership,
            owner_memberships,
        ) = await _load_org_member_mutation_records(
            client,
            slug=slug,
            actor_id=actor_id,
            target_user_id=target_user_id,
        )
        if organization is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
        if actor_membership is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
        actor_role = OrganizationRole(
            str(actor_membership.get("role") or OrganizationRole.MEMBER.value)
        )
        if actor_id != target_user_id and actor_role not in {
            OrganizationRole.OWNER,
            OrganizationRole.ADMIN,
        }:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")

        organization_id = _coerce_uuid(organization.get("uuid"), field_name="organization.uuid")
        if target_membership is not None:
            target_role = OrganizationRole(
                str(target_membership.get("role") or OrganizationRole.MEMBER.value)
            )
            _enforce_owner_role_boundary(actor_role=actor_role, current_role=target_role)
            if target_role is OrganizationRole.OWNER and len(owner_memberships) <= 1:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Cannot remove the last organization owner",
                )
            delete_result = await client.execute_query(
                "DELETE FROM organization_members WHERE uuid = $uuid;",
                uuid=str(_coerce_uuid(target_membership.get("uuid"), field_name="membership.uuid")),
            )
            error = _query_error(delete_result)
            if error is not None:
                raise RuntimeError(error)

        await log_audit_event(
            action="org.member.remove",
            user_id=actor_id,
            organization_id=organization_id,
            request=request,
            details={"target_user_id": str(target_user_id)},
        )
        return OrgMemberChange(org_id=organization_id, user_id=target_user_id)


async def list_org_invitations(
    *,
    slug: str,
    actor_id: UUID,
) -> list[InvitationRecord]:
    organization, _membership = await _require_org_admin(slug=slug, user_id=actor_id)
    async with _auth_client_scope() as client:
        records = _normalize_records(
            await client.execute_query(
                "SELECT * FROM organization_invitations "
                "WHERE organization_id = $organization_id ORDER BY created_at DESC;",
                organization_id=str(organization.id),
            )
        )
        return [
            _invitation_from_record(record)
            for record in records
            if record.get("accepted_at") is None
        ]


async def create_org_invitation(
    *,
    slug: str,
    actor_id: UUID,
    email: str,
    role: OrganizationRole,
    expires_days: int,
    request: Request,
) -> InvitationRecord:
    organization, _membership = await _require_org_admin(slug=slug, user_id=actor_id)
    async with _auth_client_scope() as client:
        now = _utcnow()
        record = {
            "uuid": str(uuid4()),
            "organization_id": str(organization.id),
            "invited_email": email.strip().lower(),
            "invited_role": role.value,
            "token": generate_invite_token(),
            "created_by_user_id": str(actor_id),
            "expires_at": now + timedelta(days=expires_days),
            "accepted_at": None,
            "accepted_by_user_id": None,
            "created_at": now,
            "updated_at": now,
        }
        created = _normalize_records(
            await client.execute_query(
                "CREATE organization_invitations CONTENT $record;", record=record
            )
        )
        if not created:
            msg = "Failed to create organization invitation"
            raise RuntimeError(msg)
        invite = created[0]
        await log_audit_event(
            action="org.invitation.create",
            user_id=actor_id,
            organization_id=organization.id,
            request=request,
            details={
                "invitation_id": str(invite["uuid"]),
                "email": invite["invited_email"],
                "role": invite["invited_role"],
            },
        )
        return _invitation_from_record(invite, include_accept_url=True)


async def delete_org_invitation(
    *,
    slug: str,
    actor_id: UUID,
    invitation_id: UUID,
    request: Request,
) -> None:
    organization, _membership = await _require_org_admin(slug=slug, user_id=actor_id)
    async with _auth_client_scope() as client:
        await client.execute_query(
            "DELETE FROM organization_invitations WHERE uuid = $uuid;",
            uuid=str(invitation_id),
        )
        await log_audit_event(
            action="org.invitation.delete",
            user_id=actor_id,
            organization_id=organization.id,
            request=request,
            details={"invitation_id": str(invitation_id), "slug": slug},
        )


async def accept_org_invitation(
    *,
    token: str,
    user: AuthUser,
    request: Request,
) -> InvitationAcceptance:
    async with _auth_client_scope() as client:
        invite, organization, membership = await _load_invitation_accept_records(
            client,
            token=token,
            user_id=user.id,
        )
        if invite is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="Invitation not found"
            )

        if invite.get("accepted_at") is not None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invitation already accepted",
            )
        expires_at = _coerce_datetime(invite.get("expires_at"))
        if expires_at is not None and expires_at < _utcnow():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="Invitation expired"
            )
        invited_email = str(invite.get("invited_email") or "").strip().lower()
        if invited_email and (user.email or "").strip().lower() != invited_email:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invitation email does not match current user",
            )

        organization_id = _coerce_uuid(
            invite.get("organization_id"),
            field_name="organization_invitations.organization_id",
        )
        if organization is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")

        role = OrganizationRole(str(invite.get("invited_role") or OrganizationRole.MEMBER.value))
        now = _utcnow()
        if membership is None:
            member_write_result = await client.execute_query(
                "CREATE organization_members CONTENT $record;",
                record={
                    "uuid": str(uuid4()),
                    "organization_id": str(organization_id),
                    "user_id": str(user.id),
                    "role": role.value,
                    "created_at": now,
                    "updated_at": now,
                },
            )
        else:
            member_write_result = await client.execute_query(
                """
                    UPDATE organization_members
                    SET role = $role,
                        updated_at = $updated_at
                    WHERE uuid = $uuid;
                """,
                uuid=str(_coerce_uuid(membership.get("uuid"), field_name="membership.uuid")),
                role=role.value,
                updated_at=now,
            )
        member_write_error = _query_error(member_write_result)
        if member_write_error is not None:
            raise RuntimeError(member_write_error)
        if not _normalize_records(member_write_result):
            msg = "Failed to write organization membership"
            raise RuntimeError(msg)

        access_token, refresh_token, refresh_expires = await _rotate_or_create_org_session(
            client=client,
            request=request,
            user_id=user.id,
            organization_id=organization_id,
        )

        updated = {
            **invite,
            "accepted_at": _utcnow(),
            "accepted_by_user_id": str(user.id),
            "updated_at": _utcnow(),
        }
        written = await _replace_org_invitation_record(
            client,
            uuid=_coerce_uuid(updated.get("uuid"), field_name="organization_invitations.uuid"),
            record=updated,
        )
        await log_audit_event(
            action="org.invitation.accept",
            user_id=user.id,
            organization_id=organization_id,
            request=request,
            details={"invitation_id": str(written["uuid"])},
        )
        return InvitationAcceptance(
            access_token=access_token,
            refresh_token=refresh_token,
            refresh_expires=refresh_expires,
            organization_id=organization_id,
            invitation_id=_coerce_uuid(
                written.get("uuid"),
                field_name="organization_invitations.uuid",
            ),
        )


async def _resolve_project_record(
    client: QueryClient, *, project_id: str, org_id: UUID
) -> SurrealRecord:
    if project_id.startswith("project_"):
        records = _normalize_records(
            await client.execute_query(
                "SELECT * FROM projects "
                "WHERE organization_id = $organization_id AND graph_project_id = $graph_project_id "
                "LIMIT 1;",
                organization_id=str(org_id),
                graph_project_id=project_id,
            )
        )
    else:
        try:
            uuid_id = UUID(project_id)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Project not found"
            ) from exc
        records = _normalize_records(
            await client.execute_query(
                "SELECT * FROM projects "
                "WHERE organization_id = $organization_id AND uuid = $uuid LIMIT 1;",
                organization_id=str(org_id),
                uuid=str(uuid_id),
            )
        )
    if not records:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    return records[0]


async def _get_project_and_user_role(
    *,
    client: QueryClient,
    project_id: str,
    user_id: UUID,
    org_id: UUID,
) -> tuple[SimpleNamespace, ProjectRole | None]:
    params: dict[str, str] = {
        "organization_id": str(org_id),
        "user_id": str(user_id),
    }
    if project_id.startswith("project_"):
        params["graph_project_id"] = project_id
        query = """
            RETURN {
                project: (
                    SELECT * FROM projects
                    WHERE organization_id = $organization_id
                        AND graph_project_id = $graph_project_id
                    LIMIT 1
                )[0],
                membership: (
                    SELECT * FROM project_members
                    WHERE user_id = $user_id
                        AND project_id IN (
                            SELECT VALUE uuid FROM projects
                            WHERE organization_id = $organization_id
                                AND graph_project_id = $graph_project_id
                            LIMIT 1
                        )
                    LIMIT 1
                )[0],
                org_membership: (
                    SELECT * FROM organization_members
                    WHERE organization_id = $organization_id AND user_id = $user_id
                    LIMIT 1
                )[0],
            };
        """
    else:
        try:
            uuid_id = UUID(project_id)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Project not found"
            ) from exc
        params["uuid"] = str(uuid_id)
        query = """
            RETURN {
                project: (
                    SELECT * FROM projects
                    WHERE organization_id = $organization_id AND uuid = $uuid
                    LIMIT 1
                )[0],
                membership: (
                    SELECT * FROM project_members
                    WHERE user_id = $user_id
                        AND project_id IN (
                            SELECT VALUE uuid FROM projects
                            WHERE organization_id = $organization_id AND uuid = $uuid
                            LIMIT 1
                        )
                    LIMIT 1
                )[0],
                org_membership: (
                    SELECT * FROM organization_members
                    WHERE organization_id = $organization_id AND user_id = $user_id
                    LIMIT 1
                )[0],
            };
        """
    payload = await client.execute_query(query, **params)
    payload = _record_payload(payload)
    project_record = _normalize_record(payload.get("project"))
    if project_record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    if _normalize_record(payload.get("org_membership")) is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not a member")
    project = SimpleNamespace(**project_record)
    project.id = _coerce_uuid(project_record.get("uuid"), field_name="project.uuid")
    project.owner_user_id = _coerce_uuid(
        project_record.get("owner_user_id"),
        field_name="project.owner_user_id",
    )
    if project.owner_user_id == user_id:
        return project, ProjectRole.OWNER

    membership = _normalize_record(payload.get("membership"))
    if membership is not None:
        return project, ProjectRole(membership["role"])
    return project, None


async def _list_project_member_records(
    client: QueryClient,
    *,
    project_db_id: UUID,
    user_id: UUID,
) -> list[SurrealRecord]:
    return _normalize_records(
        await client.execute_query(
            "SELECT * FROM project_members WHERE project_id = $project_id AND user_id = $user_id;",
            project_id=str(project_db_id),
            user_id=str(user_id),
        )
    )


async def _load_project_member_target(
    client: QueryClient,
    *,
    project_db_id: UUID,
    user_id: UUID,
    org_id: UUID,
) -> tuple[SurrealRecord | None, SurrealRecord | None, SurrealRecord | None]:
    payload = await client.execute_query(
        """
            RETURN {
                user: (SELECT * FROM users WHERE uuid = $user_id LIMIT 1)[0],
                org_membership: (
                    SELECT * FROM organization_members
                    WHERE organization_id = $organization_id AND user_id = $user_id
                    LIMIT 1
                )[0],
                membership: (
                    SELECT * FROM project_members
                    WHERE project_id = $project_id AND user_id = $user_id
                    LIMIT 1
                )[0],
            };
        """,
        project_id=str(project_db_id),
        user_id=str(user_id),
        organization_id=str(org_id),
    )
    payload = _record_payload(payload)
    return (
        _normalize_record(payload.get("user")),
        _normalize_record(payload.get("org_membership")),
        _normalize_record(payload.get("membership")),
    )


async def _target_is_org_member(
    client: QueryClient,
    *,
    org_id: UUID,
    user_id: UUID,
) -> bool:
    records = _normalize_records(
        await client.execute_query(
            "SELECT * FROM organization_members "
            "WHERE organization_id = $organization_id AND user_id = $user_id LIMIT 1;",
            organization_id=str(org_id),
            user_id=str(user_id),
        )
    )
    return bool(records)


async def _delete_project_member_records(
    client: QueryClient,
    *,
    membership_records: Sequence[Mapping[str, object]],
) -> None:
    membership_ids = [
        str(membership_record["uuid"])
        for membership_record in membership_records
        if membership_record.get("uuid")
    ]
    if not membership_ids:
        return
    await client.execute_query(
        "DELETE FROM project_members WHERE uuid IN $membership_ids;",
        membership_ids=membership_ids,
    )


async def list_project_members(
    *,
    project_id: str,
    actor,
    org_id: UUID,
) -> ProjectMembersResult:
    async with _auth_client_scope() as client:
        project, user_role = await _get_project_and_user_role(
            client=client,
            project_id=project_id,
            user_id=actor.id,
            org_id=org_id,
        )

        payload = await client.execute_query(
            """
                RETURN {
                    members: (
                        SELECT * FROM project_members
                        WHERE project_id = $project_id
                        ORDER BY created_at ASC
                    ),
                    users: (
                        SELECT * FROM users
                        WHERE uuid = $owner_user_id
                            OR uuid IN (
                                SELECT VALUE user_id FROM project_members
                                WHERE project_id = $project_id
                            )
                    ),
                    org_members: (
                        SELECT * FROM organization_members
                        WHERE organization_id = $organization_id
                            AND (
                                user_id = $owner_user_id
                                OR user_id IN (
                                    SELECT VALUE user_id FROM project_members
                                    WHERE project_id = $project_id
                                )
                            )
                    ),
                };
            """,
            project_id=str(project.id),
            owner_user_id=str(project.owner_user_id),
            organization_id=str(org_id),
        )
        payload = _record_payload(payload)
        member_records = _normalize_records(payload.get("members"))
        org_member_ids = {
            _coerce_uuid(record.get("user_id"), field_name="organization_members.user_id")
            for record in _normalize_records(payload.get("org_members"))
        }
        users_by_id = {
            _coerce_uuid(record.get("uuid"), field_name="user.uuid"): record
            for record in _normalize_records(payload.get("users"))
            if _coerce_uuid(record.get("uuid"), field_name="user.uuid") in org_member_ids
        }

        rows: list[dict[str, object]] = []
        owner = users_by_id.get(project.owner_user_id)
        if owner is not None:
            rows.append(
                {
                    "user": _member_user_payload(owner),
                    "role": ProjectRole.OWNER.value,
                    "is_owner": True,
                    "created_at": getattr(project, "created_at", None),
                }
            )

        seen_member_ids: set[UUID] = set()
        for membership_record in member_records:
            member_user_id = _coerce_uuid(membership_record.get("user_id"), field_name="user_id")
            if member_user_id == project.owner_user_id:
                continue
            if member_user_id in seen_member_ids:
                continue
            seen_member_ids.add(member_user_id)
            user = users_by_id.get(member_user_id)
            if user is None:
                continue
            rows.append(
                {
                    "user": _member_user_payload(user),
                    "role": str(membership_record.get("role") or ProjectRole.CONTRIBUTOR.value),
                    "is_owner": False,
                    "created_at": membership_record.get("created_at"),
                }
            )

        return ProjectMembersResult(
            members=rows,
            can_manage=can_manage_project_members(user_role, project, actor),
        )


async def add_project_member(
    *,
    request: Request,
    project_id: str,
    actor,
    org_id: UUID,
    target_user_id: UUID,
    role: ProjectRole,
) -> ProjectMemberChange:
    async with _auth_client_scope() as client:
        project, user_role = await _get_project_and_user_role(
            client=client,
            project_id=project_id,
            user_id=actor.id,
            org_id=org_id,
        )
        if not can_manage_project_members(user_role, project, actor):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")
        target_user, target_org_membership, existing = await _load_project_member_target(
            client,
            project_db_id=project.id,
            user_id=target_user_id,
            org_id=org_id,
        )
        if target_user is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
        if target_org_membership is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="User is not an organization member",
            )
        if existing is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="User is already a member",
            )

        now = _utcnow()
        record = {
            "uuid": str(uuid4()),
            "organization_id": str(org_id),
            "project_id": str(project.id),
            "user_id": str(target_user_id),
            "role": role.value,
            "created_at": now,
            "updated_at": now,
        }
        create_result = await client.execute_query(
            "CREATE project_members CONTENT $record;", record=record
        )
        error = _query_error(create_result)
        if error is not None:
            if _is_uniqueness_error(error):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="User is already a member",
                )
            raise RuntimeError(error)

        await log_audit_event(
            action="project.member.add",
            user_id=actor.id,
            organization_id=org_id,
            request=request,
            details={
                "project_id": str(project_id),
                "target_user_id": str(target_user_id),
                "role": role.value,
            },
        )

        return ProjectMemberChange(
            org_id=org_id,
            project_db_id=project.id,
            user_id=target_user_id,
            role=role,
        )


async def update_project_member_role(
    *,
    request: Request,
    project_id: str,
    actor,
    org_id: UUID,
    target_user_id: UUID,
    role: ProjectRole,
) -> ProjectMemberChange:
    async with _auth_client_scope() as client:
        project, user_role = await _get_project_and_user_role(
            client=client,
            project_id=project_id,
            user_id=actor.id,
            org_id=org_id,
        )
        if not can_manage_project_members(user_role, project, actor):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")
        if target_user_id == project.owner_user_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot change project owner's role",
            )
        if not await _target_is_org_member(client, org_id=org_id, user_id=target_user_id):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="User is not an organization member",
            )

        membership_records = await _list_project_member_records(
            client,
            project_db_id=project.id,
            user_id=target_user_id,
        )
        if not membership_records:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Member not found")
        update_result = await client.execute_query(
            "UPDATE project_members SET role = $role, updated_at = $updated_at "
            "WHERE project_id = $project_id AND user_id = $user_id;",
            project_id=str(project.id),
            user_id=str(target_user_id),
            role=role.value,
            updated_at=_utcnow(),
        )
        error = _query_error(update_result)
        if error is not None:
            raise RuntimeError(error)

        await log_audit_event(
            action="project.member.update_role",
            user_id=actor.id,
            organization_id=org_id,
            request=request,
            details={
                "project_id": str(project_id),
                "target_user_id": str(target_user_id),
                "role": role.value,
            },
        )

        return ProjectMemberChange(
            org_id=org_id,
            project_db_id=project.id,
            user_id=target_user_id,
            role=role,
        )


async def remove_project_member(
    *,
    request: Request,
    project_id: str,
    actor,
    org_id: UUID,
    target_user_id: UUID,
) -> ProjectMemberChange:
    async with _auth_client_scope() as client:
        project, user_role = await _get_project_and_user_role(
            client=client,
            project_id=project_id,
            user_id=actor.id,
            org_id=org_id,
        )
        if target_user_id == project.owner_user_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot remove project owner",
            )
        if actor.id != target_user_id and not can_manage_project_members(
            user_role,
            project,
            actor,
        ):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")

        membership_records = await _list_project_member_records(
            client,
            project_db_id=project.id,
            user_id=target_user_id,
        )
        if not membership_records:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Member not found")
        await _delete_project_member_records(client, membership_records=membership_records)

        await log_audit_event(
            action="project.member.remove",
            user_id=actor.id,
            organization_id=org_id,
            request=request,
            details={"project_id": str(project_id), "target_user_id": str(target_user_id)},
        )

        return ProjectMemberChange(
            org_id=org_id,
            project_db_id=project.id,
            user_id=target_user_id,
        )
