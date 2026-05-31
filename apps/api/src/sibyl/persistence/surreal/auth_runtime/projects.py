"""Surreal-backed request-time auth adapters."""

from __future__ import annotations

import secrets
from collections.abc import Mapping
from datetime import datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

from fastapi import HTTPException

from sibyl.persistence.surreal.auth_runtime._common import (
    _ENABLED_MEMORY_SPACE_SCOPES,
    _MEMORY_SPACE_SCOPES,
    _ORG_ADMIN_ROLE_VALUES,
    _PROJECT_ROLE_LEVELS,
    SurrealRecord,
    _auth_client_scope,
    _coerce_optional_uuid,
    _execute_raw_statement_records,
    _optional_str,
    _project_not_found_detail,
    _record_payload,
    _resolve_auth_context_from_claims,
    _role_value,
    _SurrealRepository,
)
from sibyl_core.auth import (
    ProjectRole,
    ProjectVisibility,
)
from sibyl_core.backends.surreal.records import (
    coerce_datetime as _coerce_datetime,
    coerce_uuid as _coerce_uuid,
    normalize_record as _normalize_record,
    normalize_records as _normalize_records,
    utcnow as _utcnow,
)


async def _generate_unique_project_slug(
    repo: _SurrealRepository,
    *,
    organization_id: UUID,
    name: str,
    exclude_uuid: UUID | None = None,
) -> str:
    import re

    base_slug = re.sub(r"[^a-z0-9\\s-]", "", name.lower())
    base_slug = re.sub(r"[\s_]+", "-", base_slug)
    base_slug = re.sub(r"-+", "-", base_slug).strip("-")[:64] or "project"
    slug = base_slug
    suffix = 1

    while suffix <= 100:
        existing = await repo.select_one(
            "SELECT * FROM projects WHERE organization_id = $organization_id AND slug = $slug LIMIT 1;",
            organization_id=str(organization_id),
            slug=slug,
        )
        existing_uuid = _coerce_optional_uuid(existing.get("uuid")) if existing else None
        if existing is None or existing_uuid == exclude_uuid:
            return slug
        suffix += 1
        slug = f"{base_slug}-{suffix}"

    return f"{base_slug[:55]}-{secrets.token_hex(4)}"


def _project_record_namespace(record: SurrealRecord) -> SimpleNamespace:
    owner_user_id = _coerce_optional_uuid(record.get("owner_user_id"))
    return SimpleNamespace(
        id=_coerce_uuid(record.get("uuid"), field_name="projects.uuid"),
        organization_id=_coerce_uuid(
            record.get("organization_id"), field_name="projects.organization_id"
        ),
        graph_project_id=str(record.get("graph_project_id") or ""),
        name=record.get("name"),
        description=record.get("description"),
        visibility=ProjectVisibility(str(record.get("visibility") or ProjectVisibility.ORG.value)),
        default_role=ProjectRole(str(record.get("default_role") or ProjectRole.VIEWER.value)),
        owner_user_id=owner_user_id,
    )


def _memory_space_state(memory_scope: str, state: str | None = None) -> tuple[str, str | None]:
    if memory_scope not in _MEMORY_SPACE_SCOPES:
        raise HTTPException(status_code=400, detail="invalid_memory_scope")
    if state is not None and state not in {"active", "disabled"}:
        raise HTTPException(status_code=400, detail="invalid_memory_space_state")
    if memory_scope not in _ENABLED_MEMORY_SPACE_SCOPES:
        return "disabled", "scope_not_enabled"
    if state == "disabled":
        return "disabled", "manually_disabled"
    return "active", None


def _memory_space_scope_key(
    *,
    memory_scope: str,
    scope_key: str | None,
    created_by_user_id: UUID,
) -> str | None:
    if memory_scope == "private":
        actor_scope_key = str(created_by_user_id)
        if scope_key and scope_key != actor_scope_key:
            raise HTTPException(status_code=400, detail="private_scope_key_mismatch")
        return actor_scope_key
    if memory_scope in {"delegated", "project", "team", "shared"} and not scope_key:
        raise HTTPException(status_code=400, detail="missing_scope_key")
    return scope_key


def _memory_space_namespace(record: SurrealRecord) -> SimpleNamespace:
    return SimpleNamespace(
        id=_coerce_uuid(record.get("uuid"), field_name="memory_spaces.uuid"),
        organization_id=_coerce_uuid(
            record.get("organization_id"), field_name="memory_spaces.organization_id"
        ),
        memory_scope=str(record.get("memory_scope") or "private"),
        scope_key=_optional_str(record.get("scope_key")),
        name=str(record.get("name") or ""),
        description=_optional_str(record.get("description")),
        state=str(record.get("state") or "active"),
        disabled_reason=_optional_str(record.get("disabled_reason")),
        metadata=_record_payload(record.get("metadata")),
        created_by_user_id=_coerce_uuid(
            record.get("created_by_user_id"),
            field_name="memory_spaces.created_by_user_id",
        ),
        created_at=_coerce_datetime(record.get("created_at")),
        updated_at=_coerce_datetime(record.get("updated_at")),
    )


def _memory_space_member_namespace(record: SurrealRecord) -> SimpleNamespace:
    permissions_value = record.get("permissions", [])
    permissions = (
        [str(item) for item in permissions_value if str(item)]
        if isinstance(permissions_value, list)
        else []
    )
    return SimpleNamespace(
        id=_coerce_uuid(record.get("uuid"), field_name="memory_space_members.uuid"),
        organization_id=_coerce_uuid(
            record.get("organization_id"),
            field_name="memory_space_members.organization_id",
        ),
        space_id=_coerce_uuid(record.get("space_id"), field_name="memory_space_members.space_id"),
        principal_type=str(record.get("principal_type") or "user"),
        principal_id=str(record.get("principal_id") or ""),
        role=str(record.get("role") or "reader"),
        permissions=permissions,
        expires_at=_coerce_datetime(record.get("expires_at")),
        created_by_user_id=_coerce_uuid(
            record.get("created_by_user_id"),
            field_name="memory_space_members.created_by_user_id",
        ),
        created_at=_coerce_datetime(record.get("created_at")),
        updated_at=_coerce_datetime(record.get("updated_at")),
    )


async def _assert_project_space_target(
    *,
    organization_id: UUID,
    memory_scope: str,
    scope_key: str | None,
) -> None:
    if memory_scope != "project" or not scope_key:
        return
    await get_project_record_by_graph_id(
        organization_id=organization_id,
        graph_project_id=scope_key,
    )


async def create_project_record(
    *,
    organization_id: UUID,
    owner_user_id: UUID,
    graph_project_id: str,
    name: str,
    description: str | None = None,
) -> SurrealRecord:
    async with _auth_client_scope() as client:
        repo = _SurrealRepository(client)
        existing = await repo.select_one(
            "SELECT * FROM projects "
            "WHERE organization_id = $organization_id AND graph_project_id = $graph_project_id "
            "LIMIT 1;",
            organization_id=str(organization_id),
            graph_project_id=graph_project_id,
        )
        if existing is not None:
            return existing

        now = _utcnow()
        record: SurrealRecord = {
            "uuid": str(uuid4()),
            "organization_id": str(organization_id),
            "owner_user_id": str(owner_user_id),
            "name": name,
            "slug": await _generate_unique_project_slug(
                repo,
                organization_id=organization_id,
                name=name,
            ),
            "description": description[:2000] if description else None,
            "graph_project_id": graph_project_id,
            "visibility": ProjectVisibility.ORG.value,
            "default_role": ProjectRole.VIEWER.value,
            "settings": {},
            "created_at": now,
            "updated_at": now,
        }
        return await repo.replace_record(
            "projects",
            uuid=_coerce_uuid(record["uuid"], field_name="projects.uuid"),
            record=record,
        )


async def update_project_record(
    *,
    organization_id: UUID,
    graph_project_id: str,
    name: str | None = None,
    description: str | None = None,
) -> bool:
    async with _auth_client_scope() as client:
        repo = _SurrealRepository(client)
        existing = await repo.select_one(
            "SELECT * FROM projects "
            "WHERE organization_id = $organization_id AND graph_project_id = $graph_project_id "
            "LIMIT 1;",
            organization_id=str(organization_id),
            graph_project_id=graph_project_id,
        )
        if existing is None:
            return False

        updated = dict(existing)
        project_uuid = _coerce_uuid(existing.get("uuid"), field_name="projects.uuid")
        if name is not None and name != existing.get("name"):
            updated["name"] = name
            updated["slug"] = await _generate_unique_project_slug(
                repo,
                organization_id=organization_id,
                name=name,
                exclude_uuid=project_uuid,
            )
        if description is not None:
            updated["description"] = description[:2000] if description else None
        updated["updated_at"] = _utcnow()
        await repo.replace_record("projects", uuid=project_uuid, record=updated)
        return True


async def delete_project_record(
    *,
    organization_id: UUID,
    graph_project_id: str,
) -> bool:
    async with _auth_client_scope() as client:
        repo = _SurrealRepository(client)
        existing = await repo.select_one(
            "SELECT * FROM projects "
            "WHERE organization_id = $organization_id AND graph_project_id = $graph_project_id "
            "LIMIT 1;",
            organization_id=str(organization_id),
            graph_project_id=graph_project_id,
        )
        if existing is None:
            return False

        project_uuid = str(existing["uuid"])
        from sibyl.persistence.graph_runtime import delete_project_graph_data

        await delete_project_graph_data(str(organization_id), graph_project_id)
        await _execute_raw_statement_records(
            client,
            """
                BEGIN TRANSACTION;
                DELETE FROM api_key_project_scopes WHERE project_id = $project_id;
                DELETE FROM team_projects WHERE project_id = $project_id;
                DELETE FROM project_members WHERE project_id = $project_id;
                DELETE FROM projects WHERE uuid = $uuid AND organization_id = $organization_id;
                COMMIT TRANSACTION;
            """,
            project_id=project_uuid,
            uuid=project_uuid,
            organization_id=str(organization_id),
        )
        return True


async def get_project_record_by_graph_id(
    *,
    organization_id: UUID,
    graph_project_id: str,
):
    async with _auth_client_scope() as client:
        repo = _SurrealRepository(client)
        record = await repo.select_one(
            "SELECT * FROM projects "
            "WHERE organization_id = $organization_id AND graph_project_id = $graph_project_id "
            "LIMIT 1;",
            organization_id=str(organization_id),
            graph_project_id=graph_project_id,
        )
        if record is None:
            raise HTTPException(
                status_code=404,
                detail=_project_not_found_detail(graph_project_id),
            )
        return _project_record_namespace(record)


async def get_project_record_by_id(
    *,
    organization_id: UUID,
    project_id: UUID,
):
    async with _auth_client_scope() as client:
        repo = _SurrealRepository(client)
        record = await repo.select_one(
            "SELECT * FROM projects "
            "WHERE organization_id = $organization_id AND uuid = $project_id "
            "LIMIT 1;",
            organization_id=str(organization_id),
            project_id=str(project_id),
        )
        if record is None:
            raise HTTPException(
                status_code=404,
                detail=_project_not_found_detail(project_id),
            )
        return _project_record_namespace(record)


async def list_memory_spaces(*, organization_id: UUID):
    async with _auth_client_scope() as client:
        repo = _SurrealRepository(client)
        records = await repo.select_many(
            "SELECT * FROM memory_spaces "
            "WHERE organization_id = $organization_id "
            "ORDER BY created_at ASC;",
            organization_id=str(organization_id),
        )
        return [_memory_space_namespace(record) for record in records]


async def create_memory_space(
    *,
    organization_id: UUID,
    created_by_user_id: UUID,
    memory_scope: str,
    scope_key: str | None = None,
    name: str,
    description: str | None = None,
    metadata: Mapping[str, object] | None = None,
):
    normalized_scope = str(memory_scope)
    normalized_scope_key = _memory_space_scope_key(
        memory_scope=normalized_scope,
        scope_key=scope_key,
        created_by_user_id=created_by_user_id,
    )
    state, disabled_reason = _memory_space_state(normalized_scope)
    await _assert_project_space_target(
        organization_id=organization_id,
        memory_scope=normalized_scope,
        scope_key=normalized_scope_key,
    )
    async with _auth_client_scope() as client:
        repo = _SurrealRepository(client)
        existing = await repo.select_one(
            "SELECT * FROM memory_spaces "
            "WHERE organization_id = $organization_id "
            "AND memory_scope = $memory_scope "
            "AND scope_key = $scope_key "
            "LIMIT 1;",
            organization_id=str(organization_id),
            memory_scope=normalized_scope,
            scope_key=normalized_scope_key,
        )
        if existing is not None:
            return _memory_space_namespace(existing)

        now = _utcnow()
        record: SurrealRecord = {
            "uuid": str(uuid4()),
            "organization_id": str(organization_id),
            "memory_scope": normalized_scope,
            "scope_key": normalized_scope_key,
            "name": name[:200],
            "description": description[:2000] if description else None,
            "state": state,
            "disabled_reason": disabled_reason,
            "metadata": dict(metadata or {}),
            "created_by_user_id": str(created_by_user_id),
            "created_at": now,
            "updated_at": now,
        }
        created = await repo.replace_record(
            "memory_spaces",
            uuid=_coerce_uuid(record["uuid"], field_name="memory_spaces.uuid"),
            record=record,
        )
        return _memory_space_namespace(created)


async def get_memory_space(*, organization_id: UUID, space_id: UUID):
    async with _auth_client_scope() as client:
        repo = _SurrealRepository(client)
        record = await repo.select_one(
            "SELECT * FROM memory_spaces "
            "WHERE organization_id = $organization_id AND uuid = $space_id "
            "LIMIT 1;",
            organization_id=str(organization_id),
            space_id=str(space_id),
        )
        if record is None:
            raise HTTPException(status_code=404, detail="memory_space_not_found")
        return _memory_space_namespace(record)


async def list_memory_space_members(*, organization_id: UUID, space_id: UUID):
    async with _auth_client_scope() as client:
        repo = _SurrealRepository(client)
        records = await repo.select_many(
            "SELECT * FROM memory_space_members "
            "WHERE organization_id = $organization_id AND space_id = $space_id "
            "ORDER BY created_at ASC;",
            organization_id=str(organization_id),
            space_id=str(space_id),
        )
        return [_memory_space_member_namespace(record) for record in records]


async def update_memory_space(
    *,
    organization_id: UUID,
    space_id: UUID,
    name: str | None = None,
    description: str | None = None,
    state: str | None = None,
    metadata: Mapping[str, object] | None = None,
):
    async with _auth_client_scope() as client:
        repo = _SurrealRepository(client)
        existing = await repo.select_one(
            "SELECT * FROM memory_spaces "
            "WHERE organization_id = $organization_id AND uuid = $space_id "
            "LIMIT 1;",
            organization_id=str(organization_id),
            space_id=str(space_id),
        )
        if existing is None:
            raise HTTPException(status_code=404, detail="memory_space_not_found")

        updated: SurrealRecord = dict(existing)
        if name is not None:
            updated["name"] = name[:200]
        if description is not None:
            updated["description"] = description[:2000] if description else None
        if metadata is not None:
            updated["metadata"] = dict(metadata)
        memory_scope = str(updated.get("memory_scope") or "private")
        if state is None:
            if memory_scope not in _ENABLED_MEMORY_SPACE_SCOPES:
                next_state, disabled_reason = "disabled", "scope_not_enabled"
            else:
                next_state = str(updated.get("state") or "active")
                disabled_reason = (
                    _optional_str(updated.get("disabled_reason"))
                    if next_state == "disabled"
                    else None
                )
        else:
            next_state, disabled_reason = _memory_space_state(memory_scope, state)
        updated["state"] = next_state
        updated["disabled_reason"] = disabled_reason
        updated["updated_at"] = _utcnow()
        saved = await repo.replace_record(
            "memory_spaces",
            uuid=_coerce_uuid(existing.get("uuid"), field_name="memory_spaces.uuid"),
            record=updated,
        )
        return _memory_space_namespace(saved)


async def add_memory_space_member(
    *,
    organization_id: UUID,
    space_id: UUID,
    created_by_user_id: UUID,
    principal_type: str,
    principal_id: str,
    role: str = "reader",
    permissions: list[str] | None = None,
    expires_at: datetime | None = None,
):
    space = await get_memory_space(organization_id=organization_id, space_id=space_id)
    if space.state == "disabled":
        raise HTTPException(status_code=409, detail=space.disabled_reason or "scope_not_enabled")
    async with _auth_client_scope() as client:
        repo = _SurrealRepository(client)
        existing = await repo.select_one(
            "SELECT * FROM memory_space_members "
            "WHERE organization_id = $organization_id "
            "AND space_id = $space_id "
            "AND principal_type = $principal_type "
            "AND principal_id = $principal_id "
            "LIMIT 1;",
            organization_id=str(organization_id),
            space_id=str(space_id),
            principal_type=principal_type,
            principal_id=principal_id,
        )
        now = _utcnow()
        record: SurrealRecord = dict(existing or {})
        record.update(
            {
                "uuid": str(record.get("uuid") or uuid4()),
                "organization_id": str(organization_id),
                "space_id": str(space_id),
                "principal_type": principal_type,
                "principal_id": principal_id,
                "role": role,
                "permissions": list(permissions or []),
                "expires_at": expires_at,
                "created_by_user_id": str(record.get("created_by_user_id") or created_by_user_id),
                "updated_at": now,
            }
        )
        record.setdefault("created_at", now)
        saved = await repo.replace_record(
            "memory_space_members",
            uuid=_coerce_uuid(record["uuid"], field_name="memory_space_members.uuid"),
            record=record,
        )
        return _memory_space_member_namespace(saved)


async def has_owner_membership(*, org_id: str, user_id: str | None) -> bool:
    if user_id is None:
        return False
    async with _auth_client_scope() as client:
        records = _normalize_records(
            await client.execute_query(
                """
                    SELECT role FROM organization_members
                    WHERE organization_id = $organization_id AND user_id = $user_id
                    LIMIT 1;
                """,
                organization_id=str(UUID(org_id)),
                user_id=str(UUID(user_id)),
            )
        )
        return bool(records) and _role_value(records[0].get("role")) == "owner"


async def resolve_org_role(*, org_id: str, user_id: str | None) -> str | None:
    """Resolve a user's current org role from live membership.

    Mirrors the membership-validated source REST uses so authorization never
    trusts a role baked into a stale token; a downgraded or revoked member
    resolves to ``None`` even if their claim still carries an elevated role.
    """
    if user_id is None:
        return None
    async with _auth_client_scope() as client:
        records = _normalize_records(
            await client.execute_query(
                """
                    SELECT role FROM organization_members
                    WHERE organization_id = $organization_id AND user_id = $user_id
                    LIMIT 1;
                """,
                organization_id=str(UUID(org_id)),
                user_id=str(UUID(user_id)),
            )
        )
        return _role_value(records[0].get("role")) if records else None


async def list_accessible_project_graph_ids(ctx) -> set[str]:
    if ctx.organization is None:
        return set()
    async with _auth_client_scope() as client:
        org_id = str(ctx.organization.id)
        org_role = _role_value(ctx.org_role)
        user_id = str(ctx.user.id)
        payload: SurrealRecord = {}
        if org_role in _ORG_ADMIN_ROLE_VALUES:
            project_records = _normalize_records(
                await client.execute_query(
                    """
                        SELECT graph_project_id, created_at FROM projects
                        WHERE organization_id = $organization_id
                        ORDER BY created_at ASC;
                    """,
                    organization_id=org_id,
                )
            )
        else:
            raw_payload = await client.execute_query(
                """
                    RETURN {
                        projects: (
                            SELECT * FROM projects
                            WHERE organization_id = $organization_id
                            ORDER BY created_at ASC
                        ),
                        direct_memberships: (
                            SELECT * FROM project_members
                            WHERE organization_id = $organization_id AND user_id = $user_id
                            ORDER BY created_at ASC
                        ),
                        team_members: (
                            SELECT * FROM team_members
                            WHERE user_id = $user_id
                            ORDER BY created_at ASC
                        ),
                        team_projects: (
                            SELECT * FROM team_projects
                            WHERE team_id IN (
                                SELECT VALUE team_id FROM team_members WHERE user_id = $user_id
                            )
                            ORDER BY created_at ASC
                        ),
                    };
                """,
                organization_id=org_id,
                user_id=user_id,
            )
            payload = _record_payload(raw_payload)
            project_records = _normalize_records(payload.get("projects"))
        if not project_records:
            return set()
        if org_role in _ORG_ADMIN_ROLE_VALUES:
            accessible = {
                str(record["graph_project_id"])
                for record in project_records
                if str(record.get("graph_project_id") or "").strip()
            }
            api_key_allowed = getattr(ctx, "api_key_project_ids", None)
            if api_key_allowed is not None:
                return accessible & {str(project_id) for project_id in api_key_allowed}
            return accessible
        accessible: set[str] = set()
        org_visible = {
            str(record["uuid"]): str(record["graph_project_id"])
            for record in project_records
            if record.get("visibility") == ProjectVisibility.ORG.value
            and str(record.get("graph_project_id") or "").strip()
        }
        accessible.update(org_visible.values())
        direct_memberships = _normalize_records(payload.get("direct_memberships"))
        direct_project_ids = {
            str(record["project_id"])
            for record in direct_memberships
            if str(record.get("project_id") or "").strip()
        }
        accessible.update(
            str(record["graph_project_id"])
            for record in project_records
            if str(record.get("uuid")) in direct_project_ids
            and str(record.get("graph_project_id") or "").strip()
        )
        team_projects = _normalize_records(payload.get("team_projects"))
        granted_project_ids = {
            str(record["project_id"])
            for record in team_projects
            if str(record.get("project_id") or "").strip()
        }
        accessible.update(
            str(record["graph_project_id"])
            for record in project_records
            if str(record.get("uuid")) in granted_project_ids
            and str(record.get("graph_project_id") or "").strip()
        )
        api_key_allowed = getattr(ctx, "api_key_project_ids", None)
        if api_key_allowed is not None:
            return accessible & {str(project_id) for project_id in api_key_allowed}
        return accessible


async def list_accessible_delegated_scope_keys(ctx) -> set[str]:
    """Return delegated memory scope keys the current principal may read."""
    if ctx.organization is None:
        return set()
    async with _auth_client_scope() as client:
        org_id = str(ctx.organization.id)
        user_id = str(ctx.user.id)
        raw_payload = await client.execute_query(
            """
                RETURN {
                    spaces: (
                        SELECT uuid, scope_key, created_at FROM memory_spaces
                        WHERE organization_id = $organization_id
                        AND memory_scope = 'delegated'
                        AND state = 'active'
                        ORDER BY created_at ASC
                    ),
                    memberships: (
                        SELECT space_id, created_at FROM memory_space_members
                        WHERE organization_id = $organization_id
                        AND principal_type = 'user'
                        AND principal_id = $user_id
                        AND (expires_at = NONE OR expires_at > time::now())
                        ORDER BY created_at ASC
                    ),
                };
            """,
            organization_id=org_id,
            user_id=user_id,
        )
        payload = _record_payload(raw_payload)
        spaces = _normalize_records(payload.get("spaces"))
        memberships = _normalize_records(payload.get("memberships"))
        member_space_ids = {
            str(record.get("space_id"))
            for record in memberships
            if str(record.get("space_id") or "").strip()
        }
        return {
            str(record.get("scope_key"))
            for record in spaces
            if str(record.get("uuid")) in member_space_ids
            and str(record.get("scope_key") or "").strip()
        }


async def resolve_accessible_project_graph_ids(
    *,
    user_id: str,
    org_id: str,
    scopes=None,
    api_key_project_ids=None,
) -> set[str] | None:
    try:
        auth_ctx = await _resolve_auth_context_from_claims(
            {"sub": user_id, "org": org_id, "scopes": list(scopes or [])}
        )
    except Exception:
        return set()
    if auth_ctx.organization is None:
        return set()
    user_accessible = await list_accessible_project_graph_ids(auth_ctx)
    if api_key_project_ids is not None:
        api_key_allowed = {str(project_id) for project_id in api_key_project_ids}
        if user_accessible is None:
            return api_key_allowed
        return user_accessible & api_key_allowed
    return user_accessible


async def verify_entity_project_access(
    *,
    ctx,
    entity_project_id: str | None,
    required_role: ProjectRole,
    require_existing_project: bool = False,
):
    if ctx.organization is None:
        from sibyl.auth.authorization import ProjectAuthorizationError

        raise ProjectAuthorizationError(
            project_id=entity_project_id or "unknown",
            required_role=required_role,
            actual_role=None,
        )
    if entity_project_id is None:
        if _role_value(ctx.org_role) in _ORG_ADMIN_ROLE_VALUES:
            return ProjectRole.OWNER
        if ctx.org_role is not None and required_role == ProjectRole.VIEWER:
            return ProjectRole.VIEWER
        from sibyl.auth.authorization import ProjectAuthorizationError

        raise ProjectAuthorizationError(
            project_id="unassigned",
            required_role=required_role,
            actual_role=ProjectRole.VIEWER if ctx.org_role else None,
        )
    if _role_value(ctx.org_role) in _ORG_ADMIN_ROLE_VALUES and not require_existing_project:
        return ProjectRole.OWNER
    async with _auth_client_scope() as client:
        payload = await client.execute_query(
            """
                RETURN {
                    project: (
                        SELECT * FROM projects
                        WHERE organization_id = $organization_id
                            AND graph_project_id = $graph_project_id
                        LIMIT 1
                    )[0],
                    direct_membership: (
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
                    team_projects: (
                        SELECT * FROM team_projects
                        WHERE project_id IN (
                                SELECT VALUE uuid FROM projects
                                WHERE organization_id = $organization_id
                                    AND graph_project_id = $graph_project_id
                                LIMIT 1
                            )
                            AND team_id IN (
                                SELECT VALUE team_id FROM team_members WHERE user_id = $user_id
                            )
                        LIMIT 10
                    ),
                };
            """,
            organization_id=str(ctx.organization.id),
            graph_project_id=entity_project_id,
            user_id=str(ctx.user.id),
        )
        payload = _record_payload(payload)
        record = _normalize_record(payload.get("project"))
        if record is None:
            if require_existing_project:
                raise HTTPException(
                    status_code=404,
                    detail=_project_not_found_detail(entity_project_id),
                )
            if _role_value(ctx.org_role) in _ORG_ADMIN_ROLE_VALUES:
                return ProjectRole.OWNER
            if ctx.org_role is not None and required_role == ProjectRole.VIEWER:
                return ProjectRole.VIEWER
            from sibyl.auth.authorization import ProjectAuthorizationError

            raise ProjectAuthorizationError(
                project_id=entity_project_id,
                required_role=required_role,
                actual_role=ProjectRole.VIEWER if ctx.org_role else None,
            )
        effective_role = _effective_project_role_from_records(
            ctx=ctx,
            project=record,
            direct_record=_normalize_record(payload.get("direct_membership")),
            team_project_records=_normalize_records(payload.get("team_projects")),
        )
        if effective_role is None:
            from sibyl.auth.authorization import ProjectAuthorizationError

            raise ProjectAuthorizationError(
                project_id=entity_project_id,
                required_role=required_role,
                actual_role=None,
            )
        if _PROJECT_ROLE_LEVELS[effective_role] < _PROJECT_ROLE_LEVELS[required_role]:
            from sibyl.auth.authorization import ProjectAuthorizationError

            raise ProjectAuthorizationError(
                project_id=entity_project_id,
                required_role=required_role,
                actual_role=effective_role,
            )
        return effective_role


def _effective_project_role_from_records(
    *,
    ctx,
    project: SurrealRecord,
    direct_record: SurrealRecord | None,
    team_project_records: list[SurrealRecord],
) -> ProjectRole | None:
    if _role_value(ctx.org_role) in _ORG_ADMIN_ROLE_VALUES:
        return ProjectRole.OWNER
    if _coerce_optional_uuid(project.get("owner_user_id")) == ctx.user.id:
        return ProjectRole.OWNER
    roles: list[ProjectRole] = []
    direct_role = _coerce_project_role(direct_record.get("role")) if direct_record else None
    if direct_role is not None:
        roles.append(direct_role)
    for team_project in team_project_records:
        team_role = _coerce_project_role(team_project.get("role"))
        if team_role is not None:
            roles.append(team_role)
    if project.get("visibility") == ProjectVisibility.ORG.value:
        visibility_role = _coerce_project_role(project.get("default_role"))
        if visibility_role is not None:
            roles.append(visibility_role)
    if not roles:
        return None
    return max(roles, key=lambda role: _PROJECT_ROLE_LEVELS[role])


def _coerce_project_role(value: object | None) -> ProjectRole | None:
    if value is None:
        return None
    raw = _role_value(value)
    if raw is None:
        return None
    return ProjectRole(raw)
