"""Surreal-backed request-time auth adapters."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID, uuid4

from fastapi import HTTPException

from sibyl.auth.api_key_common import (
    ApiKeyAuth,
    api_key_prefix,
    generate_api_key,
    hash_api_key,
    verify_api_key,
)
from sibyl.persistence.surreal.auth_runtime._common import (
    _ORG_ADMIN_ROLE_VALUES,
    QueryClient,
    _api_key_memory_space_scope,
    _api_key_namespace,
    _auth_client_scope,
    _coerce_optional_uuid,
    _log_audit_event,
    _role_value,
    _scopes_list,
    _SurrealRepository,
    _unique_strings,
    _unique_uuids,
)
from sibyl_core.backends.surreal.records import (
    coerce_datetime as _coerce_datetime,
    coerce_uuid as _coerce_uuid,
    normalize_records as _normalize_records,
    utcnow as _utcnow,
)


async def authenticate_api_key(raw_key: str):
    async with _auth_client_scope() as client:
        repo = _SurrealRepository(client)
        candidates = await repo.select_many(
            "SELECT * FROM api_keys WHERE key_prefix = $key_prefix ORDER BY created_at DESC;",
            key_prefix=api_key_prefix(raw_key),
        )
        now = _utcnow()
        for candidate in candidates:
            if candidate.get("revoked_at") is not None:
                continue
            expires_at = _coerce_datetime(candidate.get("expires_at"))
            if expires_at is not None and expires_at <= now:
                continue
            if not verify_api_key(
                raw_key,
                salt_hex=str(candidate.get("key_salt") or ""),
                hash_hex=str(candidate.get("key_hash") or ""),
            ):
                continue
            api_key_id = _coerce_uuid(candidate.get("uuid"), field_name="api_key.uuid")
            await client.execute_query(
                """
                UPDATE api_keys
                SET last_used_at = $last_used_at, updated_at = $updated_at
                WHERE uuid = $api_key_id AND revoked_at = NONE;
                """,
                api_key_id=str(api_key_id),
                last_used_at=now,
                updated_at=now,
            )
            project_scope_records = await repo.select_many(
                "SELECT * FROM api_key_project_scopes "
                "WHERE api_key_id = $api_key_id ORDER BY created_at ASC;",
                api_key_id=str(api_key_id),
            )
            project_record_ids = [
                str(record["project_id"])
                for record in project_scope_records
                if str(record.get("project_id") or "").strip()
            ]
            project_records = (
                await repo.select_many(
                    "SELECT uuid, graph_project_id FROM projects "
                    "WHERE uuid IN $project_ids ORDER BY created_at ASC;",
                    project_ids=project_record_ids,
                )
                if project_record_ids
                else []
            )
            project_ids = [
                str(record["graph_project_id"])
                for record in project_records
                if str(record.get("graph_project_id") or "").strip()
            ]
            memory_scope_records = await repo.select_many(
                "SELECT * FROM api_key_memory_space_scopes "
                "WHERE api_key_id = $api_key_id ORDER BY created_at ASC;",
                api_key_id=str(api_key_id),
            )
            memory_space_ids = [
                str(record["memory_space_id"])
                for record in memory_scope_records
                if str(record.get("memory_space_id") or "").strip()
            ]
            memory_space_records = (
                await repo.select_many(
                    "SELECT uuid, memory_scope, scope_key FROM memory_spaces "
                    "WHERE uuid IN $memory_space_ids AND organization_id = $organization_id "
                    "ORDER BY created_at ASC;",
                    memory_space_ids=memory_space_ids,
                    organization_id=str(candidate.get("organization_id")),
                )
                if memory_space_ids
                else []
            )
            memory_spaces = [_api_key_memory_space_scope(record) for record in memory_space_records]
            return ApiKeyAuth(
                api_key_id=api_key_id,
                user_id=_coerce_uuid(candidate.get("user_id"), field_name="api_key.user_id"),
                organization_id=_coerce_uuid(
                    candidate.get("organization_id"), field_name="api_key.organization_id"
                ),
                scopes=_scopes_list(candidate.get("scopes")),
                project_ids=project_ids or None,
                memory_space_ids=[space.memory_space_id for space in memory_spaces] or None,
                memory_spaces=memory_spaces or None,
            )
    return None


async def _resolve_api_key_project_record_ids(
    repo: _SurrealRepository,
    *,
    organization_id: UUID,
    project_ids: list[str] | tuple[str, ...] | None,
) -> list[str]:
    normalized = _unique_strings(project_ids)
    if not normalized:
        return []
    records = await repo.select_many(
        "SELECT uuid, graph_project_id FROM projects "
        "WHERE organization_id = $organization_id AND graph_project_id IN $project_ids "
        "ORDER BY created_at ASC;",
        organization_id=str(organization_id),
        project_ids=normalized,
    )
    by_graph_id = {str(record.get("graph_project_id")): record for record in records}
    missing = [project_id for project_id in normalized if project_id not in by_graph_id]
    if missing:
        raise HTTPException(status_code=400, detail="invalid_api_key_project_scope")
    return [str(by_graph_id[project_id]["uuid"]) for project_id in normalized]


async def _resolve_api_key_memory_space_ids(
    repo: _SurrealRepository,
    *,
    organization_id: UUID,
    memory_space_ids: list[UUID] | tuple[UUID, ...] | None,
) -> list[UUID]:
    normalized = _unique_uuids(memory_space_ids)
    if not normalized:
        return []
    records = await repo.select_many(
        "SELECT uuid FROM memory_spaces "
        "WHERE organization_id = $organization_id AND uuid IN $memory_space_ids "
        "ORDER BY created_at ASC;",
        organization_id=str(organization_id),
        memory_space_ids=[str(memory_space_id) for memory_space_id in normalized],
    )
    found = {
        _coerce_uuid(record.get("uuid"), field_name="memory_spaces.uuid") for record in records
    }
    missing = [memory_space_id for memory_space_id in normalized if memory_space_id not in found]
    if missing:
        raise HTTPException(status_code=400, detail="invalid_api_key_memory_space_scope")
    return normalized


async def _write_api_key_scope_records(
    client: QueryClient,
    *,
    api_key_id: UUID,
    project_record_ids: list[str],
    memory_space_ids: list[UUID],
) -> None:
    now = _utcnow()
    for project_id in project_record_ids:
        await client.execute_query(
            "CREATE api_key_project_scopes CONTENT $record;",
            record={
                "uuid": str(uuid4()),
                "api_key_id": str(api_key_id),
                "project_id": project_id,
                "allowed_operations": [],
                "created_at": now,
                "updated_at": now,
            },
        )
    for memory_space_id in memory_space_ids:
        await client.execute_query(
            "CREATE api_key_memory_space_scopes CONTENT $record;",
            record={
                "uuid": str(uuid4()),
                "api_key_id": str(api_key_id),
                "memory_space_id": str(memory_space_id),
                "allowed_operations": [],
                "created_at": now,
                "updated_at": now,
            },
        )


async def _decorate_api_key_scopes(
    repo: _SurrealRepository,
    keys: list[SimpleNamespace],
) -> list[SimpleNamespace]:
    api_key_ids = [str(key.id) for key in keys if getattr(key, "id", None) is not None]
    if not api_key_ids:
        return keys
    project_scope_records = await repo.select_many(
        "SELECT * FROM api_key_project_scopes "
        "WHERE api_key_id IN $api_key_ids ORDER BY created_at ASC;",
        api_key_ids=api_key_ids,
    )
    project_record_ids = [
        str(record["project_id"])
        for record in project_scope_records
        if str(record.get("project_id") or "").strip()
    ]
    project_records = (
        await repo.select_many(
            "SELECT uuid, graph_project_id FROM projects WHERE uuid IN $project_ids;",
            project_ids=project_record_ids,
        )
        if project_record_ids
        else []
    )
    graph_ids_by_uuid = {
        str(record["uuid"]): str(record["graph_project_id"])
        for record in project_records
        if str(record.get("uuid") or "").strip()
        and str(record.get("graph_project_id") or "").strip()
    }
    memory_scope_records = await repo.select_many(
        "SELECT * FROM api_key_memory_space_scopes "
        "WHERE api_key_id IN $api_key_ids ORDER BY created_at ASC;",
        api_key_ids=api_key_ids,
    )
    projects_by_key: dict[str, list[str]] = {}
    for record in project_scope_records:
        graph_id = graph_ids_by_uuid.get(str(record.get("project_id") or ""))
        if graph_id:
            projects_by_key.setdefault(str(record["api_key_id"]), []).append(graph_id)
    memory_spaces_by_key: dict[str, list[str]] = {}
    for record in memory_scope_records:
        memory_space_id = str(record.get("memory_space_id") or "").strip()
        if memory_space_id:
            memory_spaces_by_key.setdefault(str(record["api_key_id"]), []).append(memory_space_id)
    for key in keys:
        key_id = str(key.id)
        key.project_ids = projects_by_key.get(key_id, [])
        key.memory_space_ids = memory_spaces_by_key.get(key_id, [])
    return keys


async def list_api_keys_for_user(*, organization_id: UUID, user_id: UUID):
    async with _auth_client_scope() as client:
        repo = _SurrealRepository(client)
        records = await repo.select_many(
            "SELECT * FROM api_keys "
            "WHERE organization_id = $organization_id AND user_id = $user_id "
            "ORDER BY created_at DESC;",
            organization_id=str(organization_id),
            user_id=str(user_id),
        )
        keys = [key for record in records if (key := _api_key_namespace(record)) is not None]
        return await _decorate_api_key_scopes(repo, keys)


async def create_api_key_for_user(
    *,
    organization_id: UUID,
    user_id: UUID,
    name: str,
    live: bool,
    scopes: list[str],
    project_ids: list[str] | None = None,
    memory_space_ids: list[UUID] | None = None,
    expires_at,
    request,
):
    async with _auth_client_scope() as client:
        repo = _SurrealRepository(client)
        project_record_ids = await _resolve_api_key_project_record_ids(
            repo,
            organization_id=organization_id,
            project_ids=project_ids,
        )
        resolved_memory_space_ids = await _resolve_api_key_memory_space_ids(
            repo,
            organization_id=organization_id,
            memory_space_ids=memory_space_ids,
        )
        raw = generate_api_key(live=live)
        salt_hex, hash_hex = hash_api_key(raw)
        now = _utcnow()
        record = {
            "uuid": str(uuid4()),
            "organization_id": str(organization_id),
            "user_id": str(user_id),
            "name": name,
            "key_prefix": api_key_prefix(raw),
            "key_salt": salt_hex,
            "key_hash": hash_hex,
            "scopes": [scope.strip() for scope in scopes if str(scope).strip()],
            "expires_at": _coerce_datetime(expires_at),
            "revoked_at": None,
            "last_used_at": None,
            "created_at": now,
            "updated_at": now,
        }
        created = _normalize_records(
            await client.execute_query("CREATE api_keys CONTENT $record;", record=record)
        )
        if not created:
            msg = "Failed to create API key"
            raise RuntimeError(msg)
        key = _api_key_namespace(created[0])
        if key is None:
            msg = "Failed to materialize API key record"
            raise RuntimeError(msg)
        await _write_api_key_scope_records(
            client,
            api_key_id=key.id,
            project_record_ids=project_record_ids,
            memory_space_ids=resolved_memory_space_ids,
        )
        key.project_ids = list(project_ids or [])
        key.memory_space_ids = [
            str(memory_space_id) for memory_space_id in resolved_memory_space_ids
        ]
        await _log_audit_event(
            client,
            action="auth.api_key.create",
            user_id=user_id,
            organization_id=organization_id,
            request=request,
            details={
                "api_key_id": str(key.id),
                "memory_space_scope_count": len(resolved_memory_space_ids),
                "name": key.name,
                "prefix": key.key_prefix,
                "project_scope_count": len(project_record_ids),
            },
        )
        return key, raw


async def revoke_api_key_for_user(
    *,
    api_key_id: UUID,
    organization_id: UUID,
    actor_user_id: UUID,
    actor_org_role,
    request,
) -> None:
    async with _auth_client_scope() as client:
        repo = _SurrealRepository(client)
        record = await repo.select_one(
            "SELECT * FROM api_keys WHERE uuid = $uuid LIMIT 1;",
            uuid=str(api_key_id),
        )
        if (
            record is None
            or _coerce_optional_uuid(record.get("organization_id")) != organization_id
        ):
            raise HTTPException(status_code=404, detail="API key not found")
        if (
            _coerce_optional_uuid(record.get("user_id")) != actor_user_id
            and _role_value(actor_org_role) not in _ORG_ADMIN_ROLE_VALUES
        ):
            raise HTTPException(status_code=403, detail="Forbidden")
        updated = {**record, "revoked_at": _utcnow(), "updated_at": _utcnow()}
        await repo.replace_record("api_keys", uuid=api_key_id, record=updated)
        await _log_audit_event(
            client,
            action="auth.api_key.revoke",
            user_id=actor_user_id,
            organization_id=organization_id,
            request=request,
            details={"api_key_id": str(api_key_id)},
        )
