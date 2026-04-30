"""Surreal-backed organization runtime adapters."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import structlog
from fastapi import HTTPException, status
from starlette.requests import Request

from sibyl import config as config_module
from sibyl.auth.http import select_access_token
from sibyl.auth.jwt import create_access_token, create_refresh_token
from sibyl.auth.primitives import generate_invite_token, slugify
from sibyl.db.models import OrganizationRole, ProjectRole, User
from sibyl.persistence.auth_runtime import log_audit_event
from sibyl.persistence.graph_runtime import ensure_graph_indexes
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
from sibyl.persistence.surreal.auth import (
    SurrealOrganizationMembershipRepository,
    SurrealOrganizationRepository,
    SurrealUserRepository,
    surreal_auth_client_scope,
)
from sibyl.persistence.surreal.auth_runtime import SurrealSessionRepository

log = structlog.get_logger()


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _normalize_record(record: Any) -> dict[str, Any] | None:
    if record is None or not isinstance(record, dict):
        return None
    out = dict(record)
    out.pop("id", None)
    return out


def _normalize_records(result: Any) -> list[dict[str, Any]]:
    if result is None:
        return []
    if isinstance(result, dict):
        record = _normalize_record(result)
        return [record] if record is not None else []
    if not isinstance(result, list):
        return []

    records: list[dict[str, Any]] = []
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
    client: Any,
    user_ids: list[UUID],
) -> dict[UUID, dict[str, Any]]:
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
    return {
        _coerce_uuid(record.get("uuid"), field_name="user.uuid"): record for record in records
    }


def _member_user_payload(record: dict[str, Any], *, include_github_id: bool = False) -> dict[str, Any]:
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
    record: dict[str, Any], *, include_accept_url: bool = False
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
async def _auth_client_scope():
    async with surreal_auth_client_scope() as client:
        yield client


async def _rotate_or_create_org_session(
    *,
    client: Any,
    request: Request,
    user_id: UUID,
    organization_id: UUID,
    access_token: str,
    refresh_token: str,
    refresh_expires: datetime,
) -> None:
    current = select_access_token(
        authorization=request.headers.get("authorization"),
        cookie_token=request.cookies.get("sibyl_access_token"),
    )
    if not current:
        return

    access_expires = _utcnow() + timedelta(
        minutes=config_module.settings.access_token_expire_minutes
    )
    sessions = SurrealSessionRepository.from_client(client)
    existing = await sessions.get_session_by_token(current)
    if existing is not None:
        await sessions.rotate_tokens(
            existing,
            new_access_token=access_token,
            new_access_expires_at=access_expires,
            new_refresh_token=refresh_token,
            new_refresh_expires_at=refresh_expires,
        )
        return

    await sessions.create_session(
        user_id=user_id,
        organization_id=organization_id,
        token=access_token,
        expires_at=access_expires,
        refresh_token=refresh_token,
        refresh_token_expires_at=refresh_expires,
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )


async def _require_org_admin(
    *,
    slug: str,
    user_id: UUID,
) -> tuple[Any, Any]:
    async with _auth_client_scope() as client:
        orgs = SurrealOrganizationRepository.from_client(client)
        memberships = SurrealOrganizationMembershipRepository.from_client(client)
        organization = await orgs.get_by_slug(slug)
        if organization is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
        membership = await memberships.get_for_user(organization.id, user_id)
        if membership is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
        if membership.role not in {OrganizationRole.OWNER, OrganizationRole.ADMIN}:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")
        return organization, membership


async def _replace_org_invitation_record(
    client: Any, *, uuid: UUID, record: dict[str, Any]
) -> dict[str, Any]:
    await client.execute_query(
        "DELETE FROM organization_invitations WHERE uuid = $uuid;",
        uuid=str(uuid),
    )
    created = _normalize_records(
        await client.execute_query(
            "CREATE organization_invitations CONTENT $record;", record=record
        )
    )
    if not created:
        msg = f"Failed to write organization invitation {uuid}"
        raise RuntimeError(msg)
    return created[0]


async def list_orgs(*, user_id: UUID) -> list[OrgSummary]:
    async with _auth_client_scope() as client:
        records = _normalize_records(
            await client.execute_query(
                "SELECT * FROM organization_members WHERE user_id = $user_id ORDER BY created_at ASC;",
                user_id=str(user_id),
            )
        )
        organization_ids = [
            str(record["organization_id"]) for record in records if record.get("organization_id")
        ]
        if not organization_ids:
            return []
        organizations = _normalize_records(
            await client.execute_query(
                "SELECT * FROM organizations WHERE uuid IN $organization_ids;",
                organization_ids=organization_ids,
            )
        )
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
        orgs = SurrealOrganizationRepository.from_client(client)
        organizations = await orgs.list_all(limit=100_000)
        return [str(org.id) for org in organizations]


async def create_org(
    *,
    request: Request,
    user_id: UUID,
    name: str,
    slug: str | None = None,
) -> OrgAuthResult:
    async with _auth_client_scope() as client:
        orgs = SurrealOrganizationRepository.from_client(client)
        memberships = SurrealOrganizationMembershipRepository.from_client(client)
        resolved_slug = slugify(slug or name)
        existing = await orgs.get_by_slug(resolved_slug)
        if existing is not None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Slug already taken")

        organization = await orgs.create(name=name, slug=resolved_slug, is_personal=False)
        await memberships.add_member(
            organization_id=organization.id,
            user_id=user_id,
            role=OrganizationRole.OWNER,
        )
        try:
            await ensure_graph_indexes(str(organization.id))
        except Exception as exc:
            log.debug(
                "Graph index setup deferred",
                org_id=str(organization.id),
                error=str(exc),
            )

        access_token = create_access_token(user_id=user_id, organization_id=organization.id)
        refresh_token, refresh_expires = create_refresh_token(
            user_id=user_id,
            organization_id=organization.id,
        )
        await _rotate_or_create_org_session(
            client=client,
            request=request,
            user_id=user_id,
            organization_id=organization.id,
            access_token=access_token,
            refresh_token=refresh_token,
            refresh_expires=refresh_expires,
        )
        await log_audit_event(
            action="org.create",
            user_id=user_id,
            organization_id=organization.id,
            request=request,
            details={"slug": organization.slug, "name": organization.name},
        )
        return OrgAuthResult(
            id=organization.id,
            slug=organization.slug,
            name=organization.name,
            access_token=access_token,
            refresh_token=refresh_token,
            refresh_expires=refresh_expires,
        )


async def get_org(*, slug: str, user_id: UUID) -> OrgRoleResult:
    async with _auth_client_scope() as client:
        orgs = SurrealOrganizationRepository.from_client(client)
        memberships = SurrealOrganizationMembershipRepository.from_client(client)
        organization = await orgs.get_by_slug(slug)
        if organization is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
        membership = await memberships.get_for_user(organization.id, user_id)
        if membership is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
        return OrgRoleResult(
            id=organization.id,
            slug=organization.slug,
            name=organization.name,
            role=membership.role,
        )


async def switch_org(
    *,
    request: Request,
    slug: str,
    user_id: UUID,
) -> OrgAuthResult:
    async with _auth_client_scope() as client:
        orgs = SurrealOrganizationRepository.from_client(client)
        memberships = SurrealOrganizationMembershipRepository.from_client(client)
        organization = await orgs.get_by_slug(slug)
        if organization is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
        membership = await memberships.get_for_user(organization.id, user_id)
        if membership is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")

        access_token = create_access_token(user_id=user_id, organization_id=organization.id)
        refresh_token, refresh_expires = create_refresh_token(
            user_id=user_id,
            organization_id=organization.id,
        )
        await _rotate_or_create_org_session(
            client=client,
            request=request,
            user_id=user_id,
            organization_id=organization.id,
            access_token=access_token,
            refresh_token=refresh_token,
            refresh_expires=refresh_expires,
        )
        await log_audit_event(
            action="org.switch",
            user_id=user_id,
            organization_id=organization.id,
            request=request,
            details={"slug": organization.slug, "name": organization.name},
        )
        return OrgAuthResult(
            id=organization.id,
            slug=organization.slug,
            name=organization.name,
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
        orgs = SurrealOrganizationRepository.from_client(client)
        memberships = SurrealOrganizationMembershipRepository.from_client(client)
        organization = await orgs.get_by_slug(slug)
        if organization is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
        membership = await memberships.get_for_user(organization.id, user_id)
        if membership is None or membership.role not in {
            OrganizationRole.OWNER,
            OrganizationRole.ADMIN,
        }:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")

        resolved_slug = slugify(new_slug) if new_slug else None
        if resolved_slug and resolved_slug != organization.slug:
            existing = await orgs.get_by_slug(resolved_slug)
            if existing is not None:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Slug already taken",
                )

        update_params: dict[str, object] = {
            "uuid": str(organization.id),
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
            msg = f"Organization disappeared during update: {organization.id}"
            raise RuntimeError(msg)
        await log_audit_event(
            action="org.update",
            user_id=user_id,
            organization_id=organization.id,
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
            role=membership.role,
        )


async def delete_org(*, request: Request, slug: str, user_id: UUID) -> None:
    async with _auth_client_scope() as client:
        orgs = SurrealOrganizationRepository.from_client(client)
        memberships = SurrealOrganizationMembershipRepository.from_client(client)
        organization = await orgs.get_by_slug(slug)
        if organization is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
        if organization.is_personal:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Cannot delete personal organization",
            )

        membership = await memberships.get_for_user(organization.id, user_id)
        if membership is None or membership.role is not OrganizationRole.OWNER:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")

        await log_audit_event(
            action="org.delete",
            user_id=user_id,
            organization_id=organization.id,
            request=request,
            details={"slug": organization.slug, "name": organization.name},
        )

        team_rows = _normalize_records(
            await client.execute_query(
                "SELECT * FROM teams WHERE organization_id = $organization_id;",
                organization_id=str(organization.id),
            )
        )
        api_key_rows = _normalize_records(
            await client.execute_query(
                "SELECT * FROM api_keys WHERE organization_id = $organization_id;",
                organization_id=str(organization.id),
            )
        )

        for team in team_rows:
            await client.execute_query(
                "DELETE FROM team_members WHERE team_id = $team_id;",
                team_id=str(team["uuid"]),
            )
        for api_key in api_key_rows:
            await client.execute_query(
                "DELETE FROM api_key_project_scopes WHERE api_key_id = $api_key_id;",
                api_key_id=str(api_key["uuid"]),
            )

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
            await client.execute_query(query, organization_id=str(organization.id))

        from sibyl.persistence.surreal.content import (
            delete_crawl_source_record,
            surreal_content_client,
        )

        async with surreal_content_client() as content_client:
            source_rows = _normalize_records(
                await content_client.execute_query(
                    "SELECT * FROM crawl_sources WHERE organization_id = $organization_id;",
                    organization_id=str(organization.id),
                )
            )
            for source_row in source_rows:
                await delete_crawl_source_record(
                    None,
                    source_id=_coerce_uuid(source_row.get("uuid"), field_name="crawl_sources.uuid"),
                    organization_id=organization.id,
                )

            for query in (
                "DELETE FROM raw_captures WHERE organization_id = $organization_id;",
                "DELETE FROM backups WHERE organization_id = $organization_id;",
                "DELETE FROM backup_settings WHERE organization_id = $organization_id;",
            ):
                await content_client.execute_query(query, organization_id=str(organization.id))

        from sibyl_core.graph.client import get_graph_client

        graph_client = await get_graph_client()
        if config_module.settings.store == "surreal":
            from sibyl_core.backends.surreal.schema import GRAPH_EDGES, GRAPH_TABLES

            driver = graph_client.get_org_driver(str(organization.id))
            graph_ops = getattr(driver, "graph_ops", None)
            if graph_ops is not None:
                try:
                    await graph_ops.clear_data(driver, group_ids=[str(organization.id)])
                except Exception:
                    for table in (*GRAPH_EDGES, *GRAPH_TABLES):
                        query = f"DELETE FROM {table} WHERE group_id = $group_id;"  # noqa: S608
                        await driver.execute_query(
                            query,
                            group_id=str(organization.id),
                        )
            else:
                for table in (*GRAPH_EDGES, *GRAPH_TABLES):
                    query = f"DELETE FROM {table} WHERE group_id = $group_id;"  # noqa: S608
                    await driver.execute_query(
                        query,
                        group_id=str(organization.id),
                    )
        else:
            await graph_client.execute_write_org(
                "MATCH (n) DETACH DELETE n RETURN count(n) AS deleted",
                str(organization.id),
            )

        await orgs.delete(organization)


async def list_org_members(*, slug: str, actor_id: UUID) -> list[dict[str, object]]:
    async with _auth_client_scope() as client:
        orgs = SurrealOrganizationRepository.from_client(client)
        memberships = SurrealOrganizationMembershipRepository.from_client(client)

        organization = await orgs.get_by_slug(slug)
        if organization is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
        actor_membership = await memberships.get_for_user(organization.id, actor_id)
        if actor_membership is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")

        records = await memberships.list_for_org(organization.id)
        users_by_id = await _list_user_records_by_id(
            client,
            [membership.user_id for membership in records],
        )
        rows: list[dict[str, object]] = []
        for membership in records:
            user = users_by_id.get(membership.user_id)
            if user is None:
                continue
            rows.append(
                {
                    "user": _member_user_payload(user, include_github_id=True),
                    "role": membership.role.value,
                    "created_at": membership.created_at,
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
        orgs = SurrealOrganizationRepository.from_client(client)
        memberships = SurrealOrganizationMembershipRepository.from_client(client)
        users = SurrealUserRepository.from_client(client)

        organization = await orgs.get_by_slug(slug)
        if organization is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
        actor_membership = await memberships.get_for_user(organization.id, actor_id)
        if actor_membership is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
        if actor_membership.role not in {OrganizationRole.OWNER, OrganizationRole.ADMIN}:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")
        if await users.get_by_id(target_user_id) is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

        membership = await memberships.add_member(
            organization_id=organization.id,
            user_id=target_user_id,
            role=role,
        )
        await log_audit_event(
            action="org.member.add",
            user_id=actor_id,
            organization_id=organization.id,
            request=request,
            details={"target_user_id": str(membership.user_id), "role": membership.role.value},
        )
        return OrgMemberChange(
            org_id=organization.id,
            user_id=membership.user_id,
            role=membership.role,
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
        orgs = SurrealOrganizationRepository.from_client(client)
        memberships = SurrealOrganizationMembershipRepository.from_client(client)

        organization = await orgs.get_by_slug(slug)
        if organization is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
        actor_membership = await memberships.get_for_user(organization.id, actor_id)
        if actor_membership is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
        if actor_membership.role not in {OrganizationRole.OWNER, OrganizationRole.ADMIN}:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")

        try:
            membership = await memberships.set_role(
                organization_id=organization.id,
                user_id=target_user_id,
                role=role,
            )
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

        await log_audit_event(
            action="org.member.update_role",
            user_id=actor_id,
            organization_id=organization.id,
            request=request,
            details={"target_user_id": str(membership.user_id), "role": membership.role.value},
        )
        return OrgMemberChange(
            org_id=organization.id,
            user_id=membership.user_id,
            role=membership.role,
        )


async def remove_org_member(
    *,
    slug: str,
    actor_id: UUID,
    target_user_id: UUID,
    request: Request,
) -> OrgMemberChange:
    async with _auth_client_scope() as client:
        orgs = SurrealOrganizationRepository.from_client(client)
        memberships = SurrealOrganizationMembershipRepository.from_client(client)

        organization = await orgs.get_by_slug(slug)
        if organization is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
        actor_membership = await memberships.get_for_user(organization.id, actor_id)
        if actor_membership is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
        if actor_id != target_user_id and actor_membership.role not in {
            OrganizationRole.OWNER,
            OrganizationRole.ADMIN,
        }:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")

        try:
            await memberships.remove_member(
                organization_id=organization.id,
                user_id=target_user_id,
            )
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

        await log_audit_event(
            action="org.member.remove",
            user_id=actor_id,
            organization_id=organization.id,
            request=request,
            details={"target_user_id": str(target_user_id)},
        )
        return OrgMemberChange(org_id=organization.id, user_id=target_user_id)


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
    user: User,
    request: Request,
) -> InvitationAcceptance:
    async with _auth_client_scope() as client:
        memberships = SurrealOrganizationMembershipRepository.from_client(client)
        orgs = SurrealOrganizationRepository.from_client(client)
        records = _normalize_records(
            await client.execute_query(
                "SELECT * FROM organization_invitations WHERE token = $token LIMIT 1;",
                token=token,
            )
        )
        if not records:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="Invitation not found"
            )

        invite = records[0]
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
        organization = await orgs.get_by_id(organization_id)
        if organization is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")

        await memberships.add_member(
            organization_id=organization.id,
            user_id=user.id,
            role=OrganizationRole(str(invite.get("invited_role") or OrganizationRole.MEMBER.value)),
        )

        access_token = create_access_token(user_id=user.id, organization_id=organization.id)
        refresh_token, refresh_expires = create_refresh_token(
            user_id=user.id,
            organization_id=organization.id,
        )
        await _rotate_or_create_org_session(
            client=client,
            request=request,
            user_id=user.id,
            organization_id=organization.id,
            access_token=access_token,
            refresh_token=refresh_token,
            refresh_expires=refresh_expires,
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
            organization_id=organization.id,
            request=request,
            details={"invitation_id": str(written["uuid"])},
        )
        return InvitationAcceptance(
            access_token=access_token,
            refresh_token=refresh_token,
            refresh_expires=refresh_expires,
            organization_id=organization.id,
            invitation_id=_coerce_uuid(
                written.get("uuid"),
                field_name="organization_invitations.uuid",
            ),
        )


async def _resolve_project_record(client: Any, *, project_id: str, org_id: UUID) -> dict[str, Any]:
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
    client: Any,
    project_id: str,
    user_id: UUID,
    org_id: UUID,
) -> tuple[Any, ProjectRole | None]:
    project_record = await _resolve_project_record(client, project_id=project_id, org_id=org_id)
    project = SimpleNamespace(**project_record)
    project.id = _coerce_uuid(project_record.get("uuid"), field_name="project.uuid")
    project.owner_user_id = _coerce_uuid(
        project_record.get("owner_user_id"),
        field_name="project.owner_user_id",
    )
    if project.owner_user_id == user_id:
        return project, ProjectRole.OWNER

    memberships = _normalize_records(
        await client.execute_query(
            "SELECT * FROM project_members WHERE project_id = $project_id AND user_id = $user_id LIMIT 1;",
            project_id=str(project.id),
            user_id=str(user_id),
        )
    )
    if memberships:
        return project, ProjectRole(memberships[0]["role"])
    return project, None


async def _list_project_member_records(
    client: Any,
    *,
    project_db_id: UUID,
    user_id: UUID,
) -> list[dict[str, Any]]:
    return _normalize_records(
        await client.execute_query(
            "SELECT * FROM project_members WHERE project_id = $project_id AND user_id = $user_id;",
            project_id=str(project_db_id),
            user_id=str(user_id),
        )
    )


async def _delete_project_member_records(
    client: Any,
    *,
    membership_records: list[dict[str, Any]],
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

        member_records = _normalize_records(
            await client.execute_query(
                "SELECT * FROM project_members WHERE project_id = $project_id ORDER BY created_at ASC;",
                project_id=str(project.id),
            )
        )
        member_user_ids: list[UUID] = []
        for membership_record in member_records:
            member_user_id = _coerce_uuid(membership_record.get("user_id"), field_name="user_id")
            if member_user_id == project.owner_user_id or member_user_id in member_user_ids:
                continue
            member_user_ids.append(member_user_id)
        users_by_id = await _list_user_records_by_id(
            client,
            [project.owner_user_id, *member_user_ids],
        )

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
        users = SurrealUserRepository.from_client(client)
        project, user_role = await _get_project_and_user_role(
            client=client,
            project_id=project_id,
            user_id=actor.id,
            org_id=org_id,
        )
        if not can_manage_project_members(user_role, project, actor):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")
        if await users.get_by_id(target_user_id) is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

        existing = await _list_project_member_records(
            client,
            project_db_id=project.id,
            user_id=target_user_id,
        )
        if existing:
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

        membership_records = await _list_project_member_records(
            client,
            project_db_id=project.id,
            user_id=target_user_id,
        )
        if not membership_records:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Member not found")
        membership = membership_records[0]
        updated = {**membership, "role": role.value, "updated_at": _utcnow()}
        await _delete_project_member_records(client, membership_records=membership_records)
        create_result = await client.execute_query(
            "CREATE project_members CONTENT $record;", record=updated
        )
        error = _query_error(create_result)
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
