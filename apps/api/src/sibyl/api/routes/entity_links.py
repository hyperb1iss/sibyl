"""Authenticated additive entity links with a native revision guard."""

from dataclasses import asdict
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException

from sibyl.api.routes import entity_contracts as contracts, entity_policy as policy
from sibyl.api.schemas.entities import EntityLinksRequest, EntityLinksResponse
from sibyl.auth.authorization import verify_entity_project_access
from sibyl.auth.context import AuthContext
from sibyl.auth.dependencies import get_auth_context, get_current_organization, require_org_role
from sibyl.persistence.content_runtime import get_content_read_session_dependency
from sibyl_core.auth import AuthOrganization, ProjectRole
from sibyl_core.models.entities import EntityType, Relationship, RelationshipType
from sibyl_core.models.relations import parse_relation_declarations
from sibyl_core.services.graph_link_writes import (
    EntityLinkConflictError,
    add_entity_links_if_revision,
    load_entity_link_snapshot,
)

router = APIRouter(
    prefix="/entities",
    tags=["entities"],
    dependencies=[Depends(require_org_role(*contracts.WRITE_ROLES))],
)


@router.post("/{entity_id}/links", response_model=EntityLinksResponse)
async def add_entity_links(
    entity_id: str,
    links: EntityLinksRequest,
    org: AuthOrganization = Depends(get_current_organization),
    ctx: AuthContext = Depends(get_auth_context),
    content_session: object = Depends(get_content_read_session_dependency),
) -> EntityLinksResponse:
    """Fill missing links without replacing stored content or existing topology."""
    group_id = str(org.id)
    runtime = await policy.get_entity_graph_runtime(group_id)
    source = await load_entity_link_snapshot(runtime.client, entity_id, group_id=group_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Entity not found")
    project_id = policy.entity_read_project_id(source.entity)
    await verify_entity_project_access(
        content_session,
        ctx,
        project_id,
        required_role=ProjectRole.CONTRIBUTOR,
        require_existing_project=True,
    )
    await policy.require_entity_scope_visible(ctx, source.entity, project_id=project_id)
    if (
        links.epic_id or links.parent_task_id or links.depends_on
    ) and source.entity.entity_type != EntityType.TASK:
        raise HTTPException(status_code=422, detail="Task topology requires a task source")

    target_ids = {
        declaration.target_id for declaration in parse_relation_declarations(links.related_to)
    }
    target_ids.update(links.depends_on)
    target_ids.update(value for value in (links.epic_id, links.parent_task_id) if value)
    targets = {}
    for target_id in sorted(target_ids):
        target = (
            source
            if target_id == entity_id
            else await load_entity_link_snapshot(runtime.client, target_id, group_id=group_id)
        )
        if target is None:
            raise HTTPException(status_code=404, detail="Related entity not found")
        await policy.require_entity_read_access(ctx, target.entity)
        targets[target_id] = target
    for target_id, required_type in (
        (links.epic_id, EntityType.EPIC),
        (links.parent_task_id, EntityType.TASK),
    ):
        if target_id and (
            target_id == entity_id or targets[target_id].entity.entity_type != required_type
        ):
            raise HTTPException(status_code=422, detail="Invalid task topology target")
    if any(
        target_id == entity_id or targets[target_id].entity.entity_type != EntityType.TASK
        for target_id in links.depends_on
    ):
        raise HTTPException(status_code=422, detail="A dependency must target another task")

    scope = await policy.reader_scope(ctx)
    relationships = await policy.declared_bulk_relationships(
        entity_id,
        links.related_to,
        entity_manager=runtime.entity_manager,
        principal_id=scope.user_id,
        accessible_projects=scope.accessible_projects,
        allowed_memory_scope_keys=scope.memory_grants,
        now=datetime.now(UTC),
    )
    topology_edges = [(RelationshipType.DEPENDS_ON, target_id) for target_id in links.depends_on]
    if links.epic_id:
        topology_edges.append((RelationshipType.BELONGS_TO, links.epic_id))
    if links.parent_task_id:
        topology_edges.append((RelationshipType.RELATED_TO, links.parent_task_id))
    for relationship_type, target_id in topology_edges:
        relationships.append(
            Relationship(
                id=f"rel_{entity_id}_{relationship_type.value.lower()}_{target_id}",
                source_id=entity_id,
                target_id=target_id,
                relationship_type=relationship_type,
            )
        )
    # Repeated declarations have the same immutable endpoints and predicate.
    unique = {relationship.id: relationship for relationship in relationships}
    try:
        result = await add_entity_links_if_revision(
            runtime.client,
            group_id=group_id,
            source=source,
            targets=list(targets.values()),
            expected_revision=links.expected_revision,
            relationships=list(unique.values()),
            epic_id=links.epic_id,
            parent_task_id=links.parent_task_id,
            depends_on=links.depends_on,
            modified_by=str(ctx.user.id) if ctx.user is not None else None,
        )
    except EntityLinkConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail={"error": "entity_links_conflict", "reason": exc.reason},
        ) from exc
    return EntityLinksResponse(**asdict(result))
