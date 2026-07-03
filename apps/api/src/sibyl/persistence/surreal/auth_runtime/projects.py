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


async def _generate_unique_team_slug(
    repo: _SurrealRepository,
    *,
    organization_id: UUID,
    name: str,
    exclude_uuid: UUID | None = None,
) -> str:
    import re

    base_slug = re.sub(r"[^a-z0-9\\s-]", "", name.lower())
    base_slug = re.sub(r"[\s_]+", "-", base_slug)
    base_slug = re.sub(r"-+", "-", base_slug).strip("-")[:64] or "team"
    slug = base_slug
    suffix = 1

    while suffix <= 100:
        existing = await repo.select_one(
            "SELECT * FROM teams WHERE organization_id = $organization_id AND slug = $slug "
            "LIMIT 1;",
            organization_id=str(organization_id),
            slug=slug,
        )
        existing_uuid = _coerce_optional_uuid(existing.get("uuid")) if existing else None
        if existing is None or existing_uuid == exclude_uuid:
            return slug
        suffix += 1
        slug = f"{base_slug}-{suffix}"

    return f"{base_slug[:55]}-{secrets.token_hex(4)}"


async def _normalize_team_slug(
    repo: _SurrealRepository,
    *,
    organization_id: UUID,
    name: str,
    requested_slug: str | None = None,
    exclude_uuid: UUID | None = None,
) -> str:
    import re

    if not requested_slug:
        return await _generate_unique_team_slug(
            repo,
            organization_id=organization_id,
            name=name,
            exclude_uuid=exclude_uuid,
        )
    slug = re.sub(r"[^a-z0-9-]", "-", requested_slug.strip().lower())
    slug = re.sub(r"-+", "-", slug).strip("-")[:64]
    if not slug:
        raise HTTPException(status_code=400, detail="invalid_team_slug")
    existing = await repo.select_one(
        "SELECT * FROM teams WHERE organization_id = $organization_id AND slug = $slug LIMIT 1;",
        organization_id=str(organization_id),
        slug=slug,
    )
    existing_uuid = _coerce_optional_uuid(existing.get("uuid")) if existing else None
    if existing is not None and existing_uuid != exclude_uuid:
        raise HTTPException(status_code=409, detail="team_slug_exists")
    return slug


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


def _team_record_namespace(
    record: SurrealRecord,
    *,
    memory_space: SurrealRecord | None = None,
    members: list[SimpleNamespace] | None = None,
    projects: list[SimpleNamespace] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=_coerce_uuid(record.get("uuid"), field_name="teams.uuid"),
        organization_id=_coerce_uuid(
            record.get("organization_id"), field_name="teams.organization_id"
        ),
        name=str(record.get("name") or ""),
        slug=str(record.get("slug") or ""),
        description=_optional_str(record.get("description")),
        is_default=bool(record.get("is_default")),
        graph_entity_id=_optional_str(record.get("graph_entity_id")),
        memory_space_id=(
            _coerce_uuid(memory_space.get("uuid"), field_name="memory_spaces.uuid")
            if memory_space is not None and memory_space.get("uuid")
            else None
        ),
        memory_scope_key=(
            str(memory_space.get("scope_key"))
            if memory_space is not None and str(memory_space.get("scope_key") or "").strip()
            else str(record.get("uuid") or "")
        ),
        created_at=_coerce_datetime(record.get("created_at")),
        updated_at=_coerce_datetime(record.get("updated_at")),
        members=members or [],
        projects=projects or [],
    )


def _team_member_namespace(record: SurrealRecord) -> SimpleNamespace:
    return SimpleNamespace(
        id=_coerce_uuid(record.get("uuid"), field_name="team_members.uuid"),
        team_id=_coerce_uuid(record.get("team_id"), field_name="team_members.team_id"),
        user_id=_coerce_uuid(record.get("user_id"), field_name="team_members.user_id"),
        role=str(record.get("role") or "member"),
        joined_at=_coerce_datetime(record.get("joined_at")),
        created_at=_coerce_datetime(record.get("created_at")),
        updated_at=_coerce_datetime(record.get("updated_at")),
    )


def _team_project_namespace(record: SurrealRecord) -> SimpleNamespace:
    return SimpleNamespace(
        id=_coerce_uuid(record.get("uuid"), field_name="team_projects.uuid"),
        organization_id=_coerce_uuid(
            record.get("organization_id"), field_name="team_projects.organization_id"
        ),
        team_id=_coerce_uuid(record.get("team_id"), field_name="team_projects.team_id"),
        project_id=_coerce_uuid(record.get("project_id"), field_name="team_projects.project_id"),
        graph_project_id=_optional_str(record.get("graph_project_id")),
        role=ProjectRole(str(record.get("role") or ProjectRole.CONTRIBUTOR.value)),
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


async def _get_team_record(
    repo: _SurrealRepository,
    *,
    organization_id: UUID,
    team_ref: str,
) -> SurrealRecord:
    record = await repo.select_one(
        "SELECT * FROM teams "
        "WHERE organization_id = $organization_id AND (uuid = $team_ref OR slug = $team_ref) "
        "LIMIT 1;",
        organization_id=str(organization_id),
        team_ref=team_ref,
    )
    if record is None:
        raise HTTPException(status_code=404, detail="team_not_found")
    return record


async def _get_team_memory_space(
    repo: _SurrealRepository,
    *,
    organization_id: UUID,
    team_id: UUID,
) -> SurrealRecord | None:
    return await repo.select_one(
        "SELECT * FROM memory_spaces "
        "WHERE organization_id = $organization_id "
        "AND memory_scope = 'team' "
        "AND scope_key = $scope_key "
        "LIMIT 1;",
        organization_id=str(organization_id),
        scope_key=str(team_id),
    )


def _team_role_value(role: str) -> str:
    normalized = role.strip().lower()
    if normalized not in {"owner", "admin", "member", "viewer"}:
        raise HTTPException(status_code=400, detail="invalid_team_role")
    return normalized


def _project_link_role_value(role: ProjectRole | str) -> str:
    raw = _role_value(role) or str(role)
    try:
        return ProjectRole(raw).value
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid_project_role") from exc


async def list_team_records(*, organization_id: UUID):
    async with _auth_client_scope() as client:
        raw_payload = await client.execute_query(
            """
                RETURN {
                    teams: (
                        SELECT * FROM teams
                        WHERE organization_id = $organization_id
                        ORDER BY created_at ASC
                    ),
                    spaces: (
                        SELECT uuid, scope_key FROM memory_spaces
                        WHERE organization_id = $organization_id
                        AND memory_scope = 'team'
                        ORDER BY created_at ASC
                    ),
                };
            """,
            organization_id=str(organization_id),
        )
        payload = _record_payload(raw_payload)
        spaces_by_scope_key = {
            str(record.get("scope_key")): record
            for record in _normalize_records(payload.get("spaces"))
            if str(record.get("scope_key") or "").strip()
        }
        return [
            _team_record_namespace(
                record,
                memory_space=spaces_by_scope_key.get(str(record.get("uuid"))),
            )
            for record in _normalize_records(payload.get("teams"))
        ]


async def get_team_record(*, organization_id: UUID, team_ref: str):
    async with _auth_client_scope() as client:
        repo = _SurrealRepository(client)
        team = await _get_team_record(repo, organization_id=organization_id, team_ref=team_ref)
        team_id = _coerce_uuid(team.get("uuid"), field_name="teams.uuid")
        memory_space = await _get_team_memory_space(
            repo,
            organization_id=organization_id,
            team_id=team_id,
        )
        members = [
            _team_member_namespace(record)
            for record in await repo.select_many(
                "SELECT * FROM team_members WHERE team_id = $team_id ORDER BY created_at ASC;",
                team_id=str(team_id),
            )
        ]
        project_rows = await repo.select_many(
            "SELECT * FROM team_projects "
            "WHERE organization_id = $organization_id AND team_id = $team_id "
            "ORDER BY created_at ASC;",
            organization_id=str(organization_id),
            team_id=str(team_id),
        )
        project_ids = [
            str(record.get("project_id"))
            for record in project_rows
            if str(record.get("project_id") or "").strip()
        ]
        project_records = (
            await repo.select_many(
                "SELECT uuid, graph_project_id FROM projects "
                "WHERE organization_id = $organization_id AND uuid IN $project_ids;",
                organization_id=str(organization_id),
                project_ids=project_ids,
            )
            if project_ids
            else []
        )
        graph_ids_by_project = {
            str(record.get("uuid")): str(record.get("graph_project_id") or "")
            for record in project_records
        }
        for project_row in project_rows:
            project_row["graph_project_id"] = graph_ids_by_project.get(
                str(project_row.get("project_id")),
            )
        projects = [_team_project_namespace(record) for record in project_rows]
        return _team_record_namespace(
            team,
            memory_space=memory_space,
            members=members,
            projects=projects,
        )


async def create_team_record(
    *,
    organization_id: UUID,
    created_by_user_id: UUID,
    name: str,
    slug: str | None = None,
    description: str | None = None,
):
    async with _auth_client_scope() as client:
        repo = _SurrealRepository(client)
        now = _utcnow()
        team_id = uuid4()
        team_slug = await _normalize_team_slug(
            repo,
            organization_id=organization_id,
            name=name,
            requested_slug=slug,
        )
        team_record: SurrealRecord = {
            "uuid": str(team_id),
            "organization_id": str(organization_id),
            "name": name[:200],
            "slug": team_slug,
            "description": description[:2000] if description else None,
            "settings": {},
            "is_default": False,
            "graph_entity_id": None,
            "last_synced_at": None,
            "created_at": now,
            "updated_at": now,
        }
        created_team = await repo.replace_record("teams", uuid=team_id, record=team_record)
        state, disabled_reason = _memory_space_state("team")
        space_id = uuid4()
        space_record: SurrealRecord = {
            "uuid": str(space_id),
            "organization_id": str(organization_id),
            "memory_scope": "team",
            "scope_key": str(team_id),
            "name": f"{name[:180]} memory",
            "description": description[:2000] if description else None,
            "state": state,
            "disabled_reason": disabled_reason,
            "metadata": {"team_id": str(team_id), "team_slug": team_slug},
            "created_by_user_id": str(created_by_user_id),
            "created_at": now,
            "updated_at": now,
        }
        memory_space = await repo.replace_record(
            "memory_spaces",
            uuid=space_id,
            record=space_record,
        )
        member_record: SurrealRecord = {
            "uuid": str(uuid4()),
            "team_id": str(team_id),
            "user_id": str(created_by_user_id),
            "role": "owner",
            "joined_at": now,
            "created_at": now,
            "updated_at": now,
        }
        created_member = await repo.replace_record(
            "team_members",
            uuid=_coerce_uuid(member_record["uuid"], field_name="team_members.uuid"),
            record=member_record,
        )
        space_member_record: SurrealRecord = {
            "uuid": str(uuid4()),
            "organization_id": str(organization_id),
            "space_id": str(space_id),
            "principal_type": "team",
            "principal_id": str(team_id),
            "role": "owner",
            "permissions": ["read", "write", "reflect"],
            "expires_at": None,
            "created_by_user_id": str(created_by_user_id),
            "created_at": now,
            "updated_at": now,
        }
        await repo.replace_record(
            "memory_space_members",
            uuid=_coerce_uuid(
                space_member_record["uuid"],
                field_name="memory_space_members.uuid",
            ),
            record=space_member_record,
        )
        return _team_record_namespace(
            created_team,
            memory_space=memory_space,
            members=[_team_member_namespace(created_member)],
        )


async def update_team_record(
    *,
    organization_id: UUID,
    team_ref: str,
    name: str | None = None,
    slug: str | None = None,
    description: str | None = None,
):
    async with _auth_client_scope() as client:
        repo = _SurrealRepository(client)
        existing = await _get_team_record(repo, organization_id=organization_id, team_ref=team_ref)
        team_id = _coerce_uuid(existing.get("uuid"), field_name="teams.uuid")
        updated = dict(existing)
        if name is not None:
            updated["name"] = name[:200]
        if slug is not None or name is not None:
            updated["slug"] = await _normalize_team_slug(
                repo,
                organization_id=organization_id,
                name=str(updated.get("name") or name or "team"),
                requested_slug=slug,
                exclude_uuid=team_id,
            )
        if description is not None:
            updated["description"] = description[:2000] if description else None
        updated["updated_at"] = _utcnow()
        saved = await repo.replace_record("teams", uuid=team_id, record=updated)
        memory_space = await _get_team_memory_space(
            repo,
            organization_id=organization_id,
            team_id=team_id,
        )
        if memory_space is not None:
            memory_space_update = dict(memory_space)
            if name is not None:
                memory_space_update["name"] = f"{name[:180]} memory"
            if description is not None:
                memory_space_update["description"] = description[:2000] if description else None
            metadata = _record_payload(memory_space_update.get("metadata"))
            metadata.update({"team_id": str(team_id), "team_slug": str(saved.get("slug") or "")})
            memory_space_update["metadata"] = metadata
            memory_space_update["updated_at"] = updated["updated_at"]
            memory_space = await repo.replace_record(
                "memory_spaces",
                uuid=_coerce_uuid(memory_space_update.get("uuid"), field_name="memory_spaces.uuid"),
                record=memory_space_update,
            )
        return _team_record_namespace(saved, memory_space=memory_space)


async def delete_team_record(*, organization_id: UUID, team_ref: str) -> bool:
    async with _auth_client_scope() as client:
        repo = _SurrealRepository(client)
        team = await _get_team_record(repo, organization_id=organization_id, team_ref=team_ref)
        team_id = _coerce_uuid(team.get("uuid"), field_name="teams.uuid")
        await _execute_raw_statement_records(
            client,
            """
                BEGIN TRANSACTION;
                DELETE FROM memory_space_members
                WHERE organization_id = $organization_id
                AND space_id IN (
                    SELECT VALUE uuid FROM memory_spaces
                    WHERE organization_id = $organization_id
                    AND memory_scope = 'team'
                    AND scope_key = $team_id
                );
                DELETE FROM memory_spaces
                WHERE organization_id = $organization_id
                AND memory_scope = 'team'
                AND scope_key = $team_id;
                DELETE FROM team_projects WHERE organization_id = $organization_id
                AND team_id = $team_id;
                DELETE FROM team_members WHERE team_id = $team_id;
                DELETE FROM teams WHERE organization_id = $organization_id AND uuid = $team_id;
                COMMIT TRANSACTION;
            """,
            organization_id=str(organization_id),
            team_id=str(team_id),
        )
        return True


async def add_team_member_record(
    *,
    organization_id: UUID,
    team_ref: str,
    user_id: UUID,
    role: str = "member",
):
    async with _auth_client_scope() as client:
        repo = _SurrealRepository(client)
        team = await _get_team_record(repo, organization_id=organization_id, team_ref=team_ref)
        team_id = _coerce_uuid(team.get("uuid"), field_name="teams.uuid")
        existing = await repo.select_one(
            "SELECT * FROM team_members WHERE team_id = $team_id AND user_id = $user_id LIMIT 1;",
            team_id=str(team_id),
            user_id=str(user_id),
        )
        now = _utcnow()
        record: SurrealRecord = dict(existing or {})
        record.update(
            {
                "uuid": str(record.get("uuid") or uuid4()),
                "team_id": str(team_id),
                "user_id": str(user_id),
                "role": _team_role_value(role),
                "updated_at": now,
            }
        )
        record.setdefault("joined_at", now)
        record.setdefault("created_at", now)
        saved = await repo.replace_record(
            "team_members",
            uuid=_coerce_uuid(record["uuid"], field_name="team_members.uuid"),
            record=record,
        )
        return _team_member_namespace(saved)


async def remove_team_member_record(
    *,
    organization_id: UUID,
    team_ref: str,
    user_id: UUID,
) -> bool:
    async with _auth_client_scope() as client:
        repo = _SurrealRepository(client)
        team = await _get_team_record(repo, organization_id=organization_id, team_ref=team_ref)
        team_id = _coerce_uuid(team.get("uuid"), field_name="teams.uuid")
        await _execute_raw_statement_records(
            client,
            "DELETE FROM team_members WHERE team_id = $team_id AND user_id = $user_id;",
            team_id=str(team_id),
            user_id=str(user_id),
        )
        return True


async def _resolve_project_for_team_link(*, organization_id: UUID, project_ref: str):
    try:
        return await get_project_record_by_id(
            organization_id=organization_id,
            project_id=UUID(project_ref),
        )
    except ValueError:
        return await get_project_record_by_graph_id(
            organization_id=organization_id,
            graph_project_id=project_ref,
        )
    except HTTPException:
        return await get_project_record_by_graph_id(
            organization_id=organization_id,
            graph_project_id=project_ref,
        )


async def link_team_project_record(
    *,
    organization_id: UUID,
    team_ref: str,
    project_ref: str,
    role: ProjectRole | str = ProjectRole.CONTRIBUTOR,
):
    project = await _resolve_project_for_team_link(
        organization_id=organization_id,
        project_ref=project_ref,
    )
    async with _auth_client_scope() as client:
        repo = _SurrealRepository(client)
        team = await _get_team_record(repo, organization_id=organization_id, team_ref=team_ref)
        team_id = _coerce_uuid(team.get("uuid"), field_name="teams.uuid")
        existing = await repo.select_one(
            "SELECT * FROM team_projects "
            "WHERE organization_id = $organization_id AND team_id = $team_id "
            "AND project_id = $project_id LIMIT 1;",
            organization_id=str(organization_id),
            team_id=str(team_id),
            project_id=str(project.id),
        )
        now = _utcnow()
        record: SurrealRecord = dict(existing or {})
        record.update(
            {
                "uuid": str(record.get("uuid") or uuid4()),
                "organization_id": str(organization_id),
                "team_id": str(team_id),
                "project_id": str(project.id),
                "graph_project_id": str(project.graph_project_id),
                "role": _project_link_role_value(role),
                "updated_at": now,
            }
        )
        record.setdefault("created_at", now)
        saved = await repo.replace_record(
            "team_projects",
            uuid=_coerce_uuid(record["uuid"], field_name="team_projects.uuid"),
            record=record,
        )
        return _team_project_namespace(saved)


async def unlink_team_project_record(
    *,
    organization_id: UUID,
    team_ref: str,
    project_ref: str,
) -> bool:
    project = await _resolve_project_for_team_link(
        organization_id=organization_id,
        project_ref=project_ref,
    )
    async with _auth_client_scope() as client:
        repo = _SurrealRepository(client)
        team = await _get_team_record(repo, organization_id=organization_id, team_ref=team_ref)
        team_id = _coerce_uuid(team.get("uuid"), field_name="teams.uuid")
        await _execute_raw_statement_records(
            client,
            "DELETE FROM team_projects "
            "WHERE organization_id = $organization_id AND team_id = $team_id "
            "AND project_id = $project_id;",
            organization_id=str(organization_id),
            team_id=str(team_id),
            project_id=str(project.id),
        )
        return True


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


async def list_accessible_team_scope_keys(ctx) -> set[str]:
    """Return team memory scope keys the current principal may read."""
    if ctx.organization is None:
        return set()
    async with _auth_client_scope() as client:
        org_id = str(ctx.organization.id)
        org_role = _role_value(ctx.org_role)
        user_id = str(ctx.user.id)
        if org_role in _ORG_ADMIN_ROLE_VALUES:
            records = _normalize_records(
                await client.execute_query(
                    """
                        SELECT uuid, slug FROM teams
                        WHERE organization_id = $organization_id
                        ORDER BY created_at ASC;
                    """,
                    organization_id=org_id,
                )
            )
        else:
            records = _normalize_records(
                await client.execute_query(
                    """
                        SELECT uuid, slug FROM teams
                        WHERE organization_id = $organization_id
                        AND uuid IN (
                            SELECT VALUE team_id FROM team_members WHERE user_id = $user_id
                        )
                        ORDER BY created_at ASC;
                    """,
                    organization_id=org_id,
                    user_id=user_id,
                )
            )
        keys: set[str] = set()
        for record in records:
            if str(record.get("uuid") or "").strip():
                keys.add(str(record["uuid"]))
            if str(record.get("slug") or "").strip():
                keys.add(str(record["slug"]))
        return keys


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
