"""Shared scope and lifecycle predicate for public graph rows."""

from sibyl_core.auth.memory_policy import (
    memory_metadata_read_allowed,
    memory_row_project_id,
    private_scope_granted_for,
)
from sibyl_core.memory_pipeline.lifecycle import graph_metadata_recallable
from sibyl_core.models.entities import Entity, Relationship


def graph_row_read_allowed(
    entity: Entity | Relationship,
    *,
    principal_id: str | None,
    accessible_projects: set[str] | None,
    allowed_memory_scope_keys: set[str] | None,
) -> bool:
    # A visualization node carries the entity's label and metadata bag, so a
    # private memory reaches the picture as a readable title unless the same
    # scope rule that governs search is applied here too. Work items are
    # deliberately unstamped, so their project is the channel that gates them.
    return graph_metadata_recallable(
        getattr(entity, "metadata", None)
    ) and memory_metadata_read_allowed(
        getattr(entity, "metadata", None),
        principal_id=principal_id,
        accessible_projects=accessible_projects,
        allowed_memory_scope_keys=allowed_memory_scope_keys,
        private_scope_granted=private_scope_granted_for(
            allowed_memory_scope_keys, principal_id=principal_id
        ),
        row_project_id=memory_row_project_id(
            getattr(entity, "metadata", None),
            entity_type=getattr(getattr(entity, "entity_type", None), "value", None),
            entity_id=getattr(entity, "id", None),
        ),
    )
