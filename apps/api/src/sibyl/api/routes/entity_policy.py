"""Canonical policy ownership for entity routes."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import structlog
from fastapi import HTTPException

from sibyl.auth.api_key_common import api_key_memory_scope_key
from sibyl.auth.authorization import verify_entity_project_access
from sibyl.auth.context import AuthContext
from sibyl.auth.memory_targets import (
    RelatedTargetNotFoundError,
    RelationshipReaderScope,
    validate_relationship_targets,
)
from sibyl.persistence.auth_runtime import (
    list_accessible_delegated_scope_keys,
    list_accessible_project_graph_ids,
)
from sibyl.persistence.content_common import RawCaptureRecord
from sibyl_core.auth import MemoryPolicyContext, ProjectRole
from sibyl_core.auth.memory_policy import (
    authorize_memory_read,
    memory_metadata_read_allowed,
    private_scope_granted_for,
)
from sibyl_core.models.entities import EntityType, Relationship, RelationshipType
from sibyl_core.models.relations import (
    SUPPRESSING_RELATIONSHIP_TYPES,
    parse_relation_declarations,
)
from sibyl_core.services.memory import declared_suppression_allowed

log = structlog.get_logger()


async def get_entity_graph_runtime(group_id: str):
    from sibyl.persistence.graph_runtime import get_entity_graph_runtime as service

    return await service(group_id)


def entity_is_archived(entity: Any) -> bool:
    metadata = getattr(entity, "metadata", None) or {}
    return bool(metadata.get("archived")) or str(metadata.get("status", "")).lower() == "archived"


def _capture_policy_value(value: object | None) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


def _capture_memory_scope(capture: RawCaptureRecord) -> str:
    metadata = capture.metadata or {}
    return _capture_policy_value(capture.memory_scope or metadata.get("memory_scope")) or "private"


def _capture_scope_key(capture: RawCaptureRecord, memory_scope: str) -> str | None:
    metadata = capture.metadata or {}
    if memory_scope == "private":
        return _capture_policy_value(
            capture.principal_id
            or metadata.get("principal_id")
            or (str(capture.created_by_user_id) if capture.created_by_user_id else "")
        )
    if memory_scope == "project":
        return _capture_policy_value(
            capture.scope_key
            or capture.project_id
            or metadata.get("scope_key")
            or metadata.get("project_id")
        )
    return _capture_policy_value(capture.scope_key or metadata.get("scope_key"))


def _raw_capture_api_key_scope_allowed(
    *,
    ctx: AuthContext,
    memory_scope: str,
    scope_key: str | None,
) -> bool:
    allowed_scope_keys = ctx.api_key_memory_scope_keys
    if allowed_scope_keys is None:
        return True
    if not isinstance(allowed_scope_keys, list | tuple | set | frozenset):
        return True
    return api_key_memory_scope_key(memory_scope, scope_key) in {
        str(scope_key) for scope_key in allowed_scope_keys
    }


def _api_key_delegated_scope_keys(ctx: AuthContext) -> set[str]:
    allowed_scope_keys = ctx.api_key_memory_scope_keys
    if not isinstance(allowed_scope_keys, list | tuple | set | frozenset):
        return set()

    prefix = api_key_memory_scope_key("delegated", "")
    return {
        scope_key[len(prefix) :]
        for scope_key in (str(value) for value in allowed_scope_keys)
        if scope_key.startswith(prefix) and scope_key != prefix
    }


def raw_capture_visible_to_reader(
    capture: RawCaptureRecord,
    *,
    ctx: AuthContext,
    accessible_projects: set[str],
    accessible_delegations: set[str],
) -> bool:
    reader_user_id = getattr(ctx, "user_id", None)
    memory_scope = _capture_memory_scope(capture)
    scope_key = _capture_scope_key(capture, memory_scope)

    if memory_scope == "private" and scope_key != reader_user_id:
        return False

    if not _raw_capture_api_key_scope_allowed(
        ctx=ctx,
        memory_scope=memory_scope,
        scope_key=scope_key,
    ):
        return False

    policy_context = MemoryPolicyContext(
        actor_user_id=reader_user_id,
        organization_id=getattr(ctx, "organization_id", None),
        organization_role=getattr(ctx, "org_role", None),
        memory_space=memory_scope,
        scope_key=scope_key,
        project_id=scope_key if memory_scope == "project" else capture.project_id,
        agent_id=capture.agent_id,
        accessible_projects=accessible_projects,
        accessible_delegations=accessible_delegations,
        source_surface="entities_raw_capture",
    )
    return authorize_memory_read(policy_context=policy_context).allowed


def _entity_project_id(entity: Any) -> str | None:
    return getattr(entity, "project_id", None) or (
        entity.metadata.get("project_id") if getattr(entity, "metadata", None) else None
    )


def _entity_type_value(entity: Any) -> str:
    entity_type = getattr(entity, "entity_type", None)
    value = getattr(entity_type, "value", entity_type)
    return str(value or "")


def lightweight_entity_list_kwargs(entity_manager: Any) -> dict[str, bool]:
    if getattr(entity_manager, "supports_lightweight_entity_list", False) is True:
        return {"include_content": False}
    return {}


def entity_read_project_id(entity: Any) -> str | None:
    if _entity_type_value(entity) == EntityType.PROJECT.value:
        entity_id = getattr(entity, "id", None)
        return str(entity_id) if entity_id else None
    return _entity_project_id(entity)


def entity_visible_to_projects(entity: Any, accessible_projects: set[str]) -> bool:
    project_id = entity_read_project_id(entity)
    return project_id is None or project_id in accessible_projects


def entity_visible_to_reader(
    entity: Any,
    *,
    reader_user_id: str | None,
    accessible_projects: set[str],
    allowed_memory_scope_keys: set[str] | None,
) -> bool:
    # Reading a row here and finding it through search are the same question,
    # so they answer to one implementation. A local copy handled only project
    # and private and served every other scope to the whole organization.
    return memory_metadata_read_allowed(
        getattr(entity, "metadata", None),
        principal_id=reader_user_id,
        accessible_projects=accessible_projects,
        allowed_memory_scope_keys=allowed_memory_scope_keys,
        private_scope_granted=private_scope_granted_for(
            allowed_memory_scope_keys, principal_id=reader_user_id
        ),
        row_project_id=entity_read_project_id(entity),
    )


def related_entity_visible(
    entity: Any,
    *,
    reader_user_id: str | None,
    accessible_projects: set[str],
    allowed_memory_scope_keys: set[str] | None,
) -> bool:
    """Both constraints a neighbour has to clear before its name is returned.

    Project membership and memory scope are orthogonal: a work item is
    addressed by its project and carries no scope, while a private memory
    carries a scope and no project. Checking either one alone lets the other
    kind through, and a neighbour summary carries the entity's name.
    """
    return entity_visible_to_projects(entity, accessible_projects) and entity_visible_to_reader(
        entity,
        reader_user_id=reader_user_id,
        accessible_projects=accessible_projects,
        allowed_memory_scope_keys=allowed_memory_scope_keys,
    )


def reader_memory_grants(ctx: AuthContext) -> set[str] | None:
    grants = ctx.api_key_memory_scope_keys
    return set(grants) if grants is not None else None


def reader_user_id(ctx: AuthContext) -> str | None:
    return str(getattr(getattr(ctx, "user", None), "id", None) or "") or None


async def accessible_project_ids_for_read(ctx: AuthContext) -> set[str]:
    accessible_projects = await list_accessible_project_graph_ids(ctx)
    return {str(project_id) for project_id in accessible_projects or set()}


async def accessible_delegation_scope_keys_for_read(ctx: AuthContext) -> set[str]:
    accessible_delegations = await list_accessible_delegated_scope_keys(ctx)
    return {
        str(scope_key) for scope_key in accessible_delegations or set()
    } | _api_key_delegated_scope_keys(ctx)


async def resolve_entity_list_project_filter(
    *,
    ctx: AuthContext,
    project_ids: list[str] | None,
) -> tuple[list[str], list[str], bool]:
    unassigned_marker = "__unassigned__"
    requested_project_ids = list(project_ids or [])

    if requested_project_ids:
        real_project_ids = [
            project_id for project_id in requested_project_ids if project_id != unassigned_marker
        ]
        for project_id in real_project_ids:
            await verify_entity_project_access(
                None,
                ctx,
                project_id,
                required_role=ProjectRole.VIEWER,
            )
        return (
            requested_project_ids,
            list(dict.fromkeys(real_project_ids)),
            unassigned_marker in requested_project_ids,
        )

    accessible_projects = await accessible_project_ids_for_read(ctx)
    effective_project_ids = [*sorted(accessible_projects), unassigned_marker]
    return effective_project_ids, sorted(accessible_projects), True


async def require_entity_scope_visible(
    ctx: AuthContext,
    entity: Any,
    *,
    project_id: str | None,
) -> set[str]:
    accessible_projects = await accessible_project_ids_for_read(ctx)
    if project_id is not None:
        accessible_projects.add(project_id)
    reader_user_id = str(getattr(getattr(ctx, "user", None), "id", None) or "") or None
    if not entity_visible_to_reader(
        entity,
        reader_user_id=reader_user_id,
        accessible_projects=accessible_projects,
        allowed_memory_scope_keys=reader_memory_grants(ctx),
    ):
        raise HTTPException(status_code=404, detail="Entity not found")
    return accessible_projects


async def require_entity_read_access(ctx: AuthContext, entity: Any) -> set[str]:
    project_id = entity_read_project_id(entity)
    if project_id is not None:
        await verify_entity_project_access(
            None,
            ctx,
            project_id,
            required_role=ProjectRole.VIEWER,
        )
    return await require_entity_scope_visible(ctx, entity, project_id=project_id)


async def declared_bulk_relationships(
    source_id: str,
    related_to: list[str] | None,
    *,
    entity_manager: Any,
    principal_id: str | None,
    accessible_projects: set[str],
    allowed_memory_scope_keys: set[str] | None,
    now: datetime,
) -> list[Relationship]:
    """Build a bulk entry's edges, honoring and authorizing declared predicates.

    Bulk mints its own edges rather than routing through `add()`, so it repeats
    the same authorization: an edge retrieval demotes on cannot be aimed at a
    memory this caller cannot read. A refusal downgrades that edge to
    RELATED_TO rather than failing the batch.
    """
    relationships: list[Relationship] = []
    for declaration in parse_relation_declarations(related_to):
        relationship_type = declaration.relationship_type
        declared = declaration.declared
        if declared and relationship_type in SUPPRESSING_RELATIONSHIP_TYPES:
            allowed = await declared_suppression_allowed(
                entity_manager=entity_manager,
                target_id=declaration.target_id,
                principal_id=principal_id,
                accessible_projects=accessible_projects,
                allowed_memory_scope_keys=allowed_memory_scope_keys,
            )
            if not allowed:
                relationship_type = RelationshipType.RELATED_TO
                declared = False
        metadata: dict[str, Any] = {"created_at": now.isoformat()}
        if declared:
            metadata["agent_declared"] = True
        relationships.append(
            Relationship(
                id=f"rel_{source_id}_{relationship_type.value.lower()}_{declaration.target_id}",
                source_id=source_id,
                target_id=declaration.target_id,
                relationship_type=relationship_type,
                metadata=metadata,
            )
        )
    return relationships


@dataclass(frozen=True, slots=True)
class ReaderScope:
    """One snapshot of what a caller may read, taken before anything is written.

    Resolving membership per entry let a mid-request change make sibling order
    matter, and re-resolving after persistence meant an auth-store failure
    could raise with rows already in the graph.
    """

    user_id: str | None
    accessible_projects: set[str]
    memory_grants: set[str] | None


async def reader_scope(ctx: AuthContext) -> ReaderScope:
    return ReaderScope(
        user_id=reader_user_id(ctx),
        accessible_projects=await accessible_project_ids_for_read(ctx),
        memory_grants=reader_memory_grants(ctx),
    )


async def validate_related_to_targets_for_write(
    *,
    entity_manager: Any,
    related_to: list[str] | None,
    scope: ReaderScope,
) -> None:
    """Resolve every link target and refuse the ones this caller cannot read.

    A target the caller cannot see answers exactly like a target that does not
    exist: same status, same message. Distinguishing them turns the link
    parameter into an existence oracle over other principals' private
    memories, since ids are guessable and a 404 would confirm absence.

    Reading a row is the whole requirement. Naming an entity as an edge target
    does not mutate it, so demanding write authority over it refused a member
    who can retrieve a project's decisions from ever saying one was superseded,
    which is exactly the person best placed to say it.
    """
    try:
        await validate_relationship_targets(
            entity_manager=entity_manager,
            related_to=related_to,
            scope=RelationshipReaderScope.from_values(
                user_id=scope.user_id,
                accessible_projects=scope.accessible_projects,
                memory_grants=scope.memory_grants,
            ),
        )
    except RelatedTargetNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def _entity_matches_project_filter(
    entity: Any,
    *,
    project_ids: list[str] | None,
    real_project_ids: list[str],
    has_unassigned: bool,
) -> bool:
    if not project_ids:
        return True

    entity_project = entity_read_project_id(entity)
    if entity_project:
        if real_project_ids and entity_project not in real_project_ids:
            return False
        return not (has_unassigned and not real_project_ids)
    return bool(has_unassigned)


def entity_matches_list_filters(
    entity: Any,
    *,
    project_ids: list[str] | None,
    real_project_ids: list[str],
    has_unassigned: bool,
    reader_user_id: str | None,
    accessible_projects: set[str],
    allowed_memory_scope_keys: set[str] | None,
    language: str | None,
    category: str | None,
    search: str | None,
) -> bool:
    if entity_is_archived(entity):
        return False

    if not _entity_matches_project_filter(
        entity,
        project_ids=project_ids,
        real_project_ids=real_project_ids,
        has_unassigned=has_unassigned,
    ):
        return False

    if not entity_visible_to_reader(
        entity,
        reader_user_id=reader_user_id,
        allowed_memory_scope_keys=allowed_memory_scope_keys,
        accessible_projects=accessible_projects,
    ):
        return False

    if language:
        entity_langs = getattr(entity, "languages", []) or []
        if language.lower() not in [lang.lower() for lang in entity_langs]:
            return False

    if category:
        metadata = getattr(entity, "metadata", {}) or {}
        entity_cat = getattr(entity, "category", None) or metadata.get("category") or ""
        if category.lower() not in entity_cat.lower():
            return False

    if search:
        search_lower = search.lower()
        name = (getattr(entity, "name", "") or "").lower()
        description = (getattr(entity, "description", "") or "").lower()
        if search_lower not in name and search_lower not in description:
            return False

    return True
