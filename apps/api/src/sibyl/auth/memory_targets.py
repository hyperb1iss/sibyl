"""Shared authorization for relationship targets on public write surfaces."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from sibyl_core.auth.memory_policy import (
    memory_metadata_read_allowed,
    private_scope_granted_for,
)
from sibyl_core.errors import EntityNotFoundError
from sibyl_core.models.relations import declared_relation_targets


class RelatedTargetNotFoundError(ValueError):
    """A relationship target is absent or hidden from the current reader."""

    def __init__(self, target_id: str) -> None:
        self.target_id = target_id
        super().__init__(f"Related entity not found: {target_id}")


@dataclass(frozen=True, slots=True)
class RelationshipReaderScope:
    """One authorization snapshot reused across every target in a write."""

    user_id: str | None
    accessible_projects: frozenset[str]
    memory_grants: frozenset[str] | None

    @classmethod
    def from_values(
        cls,
        *,
        user_id: str | None,
        accessible_projects: Iterable[str] | None,
        memory_grants: Iterable[str] | None,
    ) -> RelationshipReaderScope:
        return cls(
            user_id=user_id,
            accessible_projects=frozenset(str(value) for value in accessible_projects or ()),
            memory_grants=(
                frozenset(str(value) for value in memory_grants)
                if memory_grants is not None
                else None
            ),
        )


def _entity_type_value(entity: Any) -> str:
    entity_type = getattr(entity, "entity_type", None)
    value = getattr(entity_type, "value", entity_type)
    return str(value or "").lower()


def _entity_project_id(entity: Any) -> str | None:
    if _entity_type_value(entity) == "project":
        entity_id = getattr(entity, "id", None)
        return str(entity_id) if entity_id else None
    project_id = getattr(entity, "project_id", None)
    if project_id:
        return str(project_id)
    metadata = getattr(entity, "metadata", None) or {}
    if isinstance(metadata, dict):
        project_id = metadata.get("project_id")
        return str(project_id) if project_id else None
    return None


def relationship_target_visible(entity: Any, *, scope: RelationshipReaderScope) -> bool:
    """Return whether a reader may name ``entity`` as a relationship target."""
    project_id = _entity_project_id(entity)
    if project_id is not None and project_id not in scope.accessible_projects:
        return False
    return memory_metadata_read_allowed(
        getattr(entity, "metadata", None),
        principal_id=scope.user_id,
        accessible_projects=scope.accessible_projects,
        allowed_memory_scope_keys=scope.memory_grants,
        private_scope_granted=private_scope_granted_for(
            scope.memory_grants,
            principal_id=scope.user_id,
        ),
        row_project_id=project_id,
    )


async def validate_relationship_targets(
    *,
    entity_manager: Any,
    related_to: list[str] | None,
    scope: RelationshipReaderScope,
) -> None:
    """Refuse absent and unreadable targets with one indistinguishable error."""
    checked_ids: set[str] = set()
    for target_id in declared_relation_targets(related_to):
        if target_id in checked_ids:
            continue
        checked_ids.add(target_id)
        error = RelatedTargetNotFoundError(target_id)
        try:
            target = await entity_manager.get(target_id)
        except (EntityNotFoundError, KeyError) as exc:
            raise error from exc
        if target is None or not relationship_target_visible(target, scope=scope):
            raise error


__all__ = [
    "RelatedTargetNotFoundError",
    "RelationshipReaderScope",
    "relationship_target_visible",
    "validate_relationship_targets",
]
