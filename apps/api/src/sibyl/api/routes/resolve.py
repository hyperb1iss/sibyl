"""Resolve short graph ID prefixes."""

from __future__ import annotations

from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from sibyl.api.routes.memory import _api_key_memory_scope_allowed, _inspect_content_policy
from sibyl.auth.context import AuthContext
from sibyl.auth.dependencies import (
    get_auth_context,
    get_current_organization,
    require_org_role,
)
from sibyl.persistence.auth_runtime import list_accessible_project_graph_ids
from sibyl.persistence.graph_runtime import execute_surreal_graph_query, get_entity_graph_runtime
from sibyl_core.auth import AuthOrganization, OrganizationRole
from sibyl_core.auth.memory_policy import (
    memory_metadata_read_allowed,
    memory_row_project_id,
    private_scope_granted_for,
)
from sibyl_core.models.entities import EntityType
from sibyl_core.services.surreal_content import RawMemory, resolve_raw_memory_prefix

log = structlog.get_logger()
_RAW_MEMORY_TYPE = "raw_memory"
_READ_ROLES = (
    OrganizationRole.OWNER,
    OrganizationRole.ADMIN,
    OrganizationRole.MEMBER,
    OrganizationRole.VIEWER,
)

router = APIRouter(
    prefix="/resolve",
    tags=["resolve"],
    dependencies=[Depends(require_org_role(*_READ_ROLES))],
)


class ResolveCandidate(BaseModel):
    id: str
    entity_type: str
    name: str
    project_id: str | None = None


class ResolveResponse(BaseModel):
    prefix: str
    matches: list[ResolveCandidate]
    count: int


def _prefix_candidates(prefix: str, entity_type: str | None) -> list[str]:
    normalized = prefix.strip()
    candidates = [normalized]
    if entity_type and not normalized.startswith(f"{entity_type}_"):
        candidates.append(f"{entity_type}_{normalized}")
    return list(dict.fromkeys(candidate for candidate in candidates if candidate))


def _row_metadata(row: dict[str, object]) -> dict[str, object]:
    """Read a row's metadata bag.

    A graph row stores it in the flexible `attributes` column; `metadata` is
    the name the Entity model gives it. The projection is accepted under
    either name so a row's scope is never read as absent — an absent scope
    reads as unscoped, which fails open.
    """
    for key in ("attributes", "metadata"):
        value = row.get(key)
        if isinstance(value, dict):
            return {str(field): field_value for field, field_value in value.items()}
    return {}


def _row_project_id(row: dict[str, object]) -> str | None:
    entity_type = str(row.get("entity_type") or "")
    entity_id = str(row.get("uuid") or row.get("id") or "")
    if entity_type == EntityType.PROJECT.value:
        return entity_id or None

    project_id = row.get("project_id")
    if project_id:
        return str(project_id)

    metadata_project = _row_metadata(row).get("project_id")
    return str(metadata_project) if metadata_project else None


def _candidate_from_row(row: dict[str, object]) -> ResolveCandidate:
    return ResolveCandidate(
        id=str(row.get("uuid") or row.get("id") or ""),
        entity_type=str(row.get("entity_type") or ""),
        name=str(row.get("name") or ""),
        project_id=_row_project_id(row),
    )


def _candidate_from_raw_memory(memory: RawMemory) -> ResolveCandidate:
    return ResolveCandidate(
        id=memory.id,
        entity_type=_RAW_MEMORY_TYPE,
        name=memory.title or memory.source_id or memory.id,
        project_id=memory.project_id,
    )


def _entity_project_id(entity: Any, entity_id: str) -> str | None:
    entity_type = str(getattr(getattr(entity, "entity_type", ""), "value", ""))
    if entity_type == EntityType.PROJECT.value:
        return entity_id

    project_id = getattr(entity, "project_id", None)
    if project_id:
        return str(project_id)

    metadata = getattr(entity, "metadata", {}) or {}
    if isinstance(metadata, dict):
        metadata_project = metadata.get("project_id")
        return str(metadata_project) if metadata_project else None
    return None


def _scope_visible(
    metadata: object,
    *,
    principal_id: str | None,
    accessible_projects: set[str],
    allowed_memory_scope_keys: set[str] | None,
    entity_type: str | None = None,
    entity_id: str | None = None,
) -> bool:
    # A resolve candidate carries the entity's name, so the scope rule has to
    # run before one is built. Matching on project alone let every row without
    # a project through, private ones included.
    return memory_metadata_read_allowed(
        metadata if isinstance(metadata, dict) else None,
        principal_id=principal_id,
        accessible_projects=accessible_projects,
        allowed_memory_scope_keys=allowed_memory_scope_keys,
        private_scope_granted=private_scope_granted_for(
            allowed_memory_scope_keys, principal_id=principal_id
        ),
        row_project_id=memory_row_project_id(
            metadata if isinstance(metadata, dict) else None,
            entity_type=entity_type,
            entity_id=entity_id,
        ),
    )


async def _resolve_reader(ctx: AuthContext) -> tuple[str | None, set[str], set[str] | None]:
    accessible_projects = {
        str(project_id) for project_id in await list_accessible_project_graph_ids(ctx) or set()
    }
    principal_id = str(getattr(getattr(ctx, "user", None), "id", None) or "") or None
    grants = getattr(ctx, "api_key_memory_scope_keys", None)
    return principal_id, accessible_projects, set(grants) if grants is not None else None


def _filter_visible_candidates(
    candidates: list[ResolveCandidate],
    *,
    accessible_projects: set[str],
) -> list[ResolveCandidate]:
    visible: list[ResolveCandidate] = []
    for candidate in candidates:
        project_id = candidate.project_id
        if project_id is None or project_id in accessible_projects:
            visible.append(candidate)
    return visible


async def _filter_visible_raw_memories(
    memories: list[RawMemory],
    *,
    ctx: AuthContext,
) -> list[RawMemory]:
    visible: list[RawMemory] = []
    for memory in memories:
        if not _api_key_memory_scope_allowed(
            ctx,
            memory_scope=memory.memory_scope.value,
            scope_key=memory.scope_key,
        ):
            continue
        decision = await _inspect_content_policy(ctx=ctx, memory=memory)
        if decision.allowed:
            visible.append(memory)
    return visible


def _matches_candidate_prefix(entity_id: str, prefixes: list[str]) -> bool:
    return any(entity_id.startswith(prefix) for prefix in prefixes)


async def _resolve_via_surreal(
    group_id: str,
    *,
    prefixes: list[str],
    entity_type: str | None,
    limit: int,
    principal_id: str | None,
    accessible_projects: set[str],
    allowed_memory_scope_keys: set[str] | None,
) -> list[ResolveCandidate] | None:
    clauses = []
    params: dict[str, object] = {"limit": limit}
    for index, prefix in enumerate(prefixes):
        params[f"prefix_{index}"] = prefix
        params[f"prefix_upper_{index}"] = f"{prefix}\uffff"
        clauses.append(f"(uuid >= $prefix_{index} AND uuid < $prefix_upper_{index})")

    params["entity_type"] = entity_type
    entity_type_clause = "AND entity_type = $entity_type" if entity_type else ""
    prefix_clause = " OR ".join(clauses)
    query = (
        "SELECT uuid, name, entity_type, attributes "  # noqa: S608
        "FROM entity "
        "WHERE group_id = $group_id "
        f"AND ({prefix_clause}) "
        f"{entity_type_clause} "
        "ORDER BY uuid ASC "
        "LIMIT $limit;"
    )
    rows = await execute_surreal_graph_query(group_id, query, **params)
    if rows is None:
        return None
    return [
        _candidate_from_row(row)
        for row in rows
        if _scope_visible(
            _row_metadata(row),
            principal_id=principal_id,
            accessible_projects=accessible_projects,
            allowed_memory_scope_keys=allowed_memory_scope_keys,
            entity_type=str(row.get("entity_type") or "") or None,
            entity_id=str(row.get("uuid") or row.get("id") or "") or None,
        )
    ]


async def _resolve_via_entity_manager(
    group_id: str,
    *,
    prefixes: list[str],
    entity_type: str | None,
    limit: int,
    principal_id: str | None,
    accessible_projects: set[str],
    allowed_memory_scope_keys: set[str] | None,
) -> list[ResolveCandidate]:
    runtime = await get_entity_graph_runtime(group_id)
    manager = runtime.entity_manager
    entities: list[Any]
    if entity_type:
        entities = await manager.list_by_type(
            EntityType(entity_type),
            limit=limit * 20,
            include_archived=True,
        )
    else:
        entities = await manager.list_all(limit=limit * 20, include_archived=True)

    matches: list[ResolveCandidate] = []
    for entity in entities:
        entity_id = str(getattr(entity, "id", ""))
        if not _matches_candidate_prefix(entity_id, prefixes):
            continue
        if not _scope_visible(
            getattr(entity, "metadata", None),
            principal_id=principal_id,
            accessible_projects=accessible_projects,
            allowed_memory_scope_keys=allowed_memory_scope_keys,
            entity_type=str(getattr(getattr(entity, "entity_type", ""), "value", "")) or None,
            entity_id=entity_id,
        ):
            continue
        matches.append(
            ResolveCandidate(
                id=entity_id,
                entity_type=str(getattr(getattr(entity, "entity_type", ""), "value", "")),
                name=str(getattr(entity, "name", "")),
                project_id=_entity_project_id(entity, entity_id),
            )
        )
        if len(matches) >= limit:
            break
    return matches


async def _resolve_raw_memory_candidates(
    group_id: str,
    *,
    prefix: str,
    ctx: AuthContext,
    limit: int,
) -> list[ResolveCandidate]:
    memories = await resolve_raw_memory_prefix(
        organization_id=group_id,
        prefix=prefix,
        limit=limit,
    )
    visible_memories = await _filter_visible_raw_memories(memories, ctx=ctx)
    return [_candidate_from_raw_memory(memory) for memory in visible_memories]


@router.get("/{prefix}", response_model=ResolveResponse)
async def resolve_id_prefix(
    prefix: str,
    org: AuthOrganization = Depends(get_current_organization),
    ctx: AuthContext = Depends(get_auth_context),
    entity_type: str | None = Query(default=None, description="Narrow to an entity type"),
    limit: int = Query(default=20, ge=1, le=50, description="Maximum candidates"),
) -> ResolveResponse:
    normalized = prefix.strip()
    if not normalized:
        raise HTTPException(status_code=400, detail="Prefix cannot be empty")

    type_value = entity_type.strip() if entity_type else None
    if type_value == _RAW_MEMORY_TYPE:
        matches = await _resolve_raw_memory_candidates(
            str(org.id),
            prefix=normalized,
            ctx=ctx,
            limit=limit,
        )
        return ResolveResponse(prefix=normalized, matches=matches, count=len(matches))

    if type_value is not None:
        try:
            type_value = EntityType(type_value).value
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid entity type: {type_value}",
            ) from exc

    prefixes = _prefix_candidates(normalized, type_value)
    group_id = str(org.id)

    principal_id, accessible_projects, memory_grants = await _resolve_reader(ctx)

    try:
        candidates = await _resolve_via_surreal(
            group_id,
            prefixes=prefixes,
            entity_type=type_value,
            limit=limit,
            principal_id=principal_id,
            accessible_projects=accessible_projects,
            allowed_memory_scope_keys=memory_grants,
        )
        if candidates is None:
            candidates = await _resolve_via_entity_manager(
                group_id,
                prefixes=prefixes,
                entity_type=type_value,
                limit=limit,
                principal_id=principal_id,
                accessible_projects=accessible_projects,
                allowed_memory_scope_keys=memory_grants,
            )
        visible = _filter_visible_candidates(
            candidates,
            accessible_projects=accessible_projects,
        )
        return ResolveResponse(prefix=normalized, matches=visible, count=len(visible))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid entity type: {type_value}") from exc
    except HTTPException:
        raise
    except Exception as exc:
        log.exception("resolve_id_prefix_failed", prefix=normalized, entity_type=type_value)
        raise HTTPException(status_code=500, detail="Failed to resolve ID prefix") from exc
