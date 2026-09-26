"""Shared pytest fixtures and factories for sibyl-core tests."""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from sibyl_core.models.entities import Entity, EntityType


@pytest.fixture(autouse=True)
def disable_raw_memory_auto_embedding(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "sibyl_core.services.content_models.configured_raw_memory_embedding_provider",
        lambda: None,
    )


@pytest.fixture(autouse=True)
def reset_vector_lane_readiness() -> None:
    # Lane verdicts are cached per process; one test's plane state must not
    # decide another test's lanes.
    from sibyl_core.services.embedding_lane_readiness import reset_lane_readiness_cache

    reset_lane_readiness_cache()


def make_entity(
    entity_id: str | None = None,
    name: str = "Test entity",
    entity_type: EntityType = EntityType.TOPIC,
    description: str = "",
    content: str = "",
    metadata: dict[str, Any] | None = None,
    organization_id: str = "test-org-id",
    **kwargs: Any,
) -> Entity:
    """Factory for creating test Entity instances.

    Args:
        entity_id: Unique ID (auto-generated if not provided).
        name: Entity name.
        entity_type: Type of entity.
        description: Entity description.
        content: Entity content.
        metadata: Additional metadata.
        organization_id: Organization scope.
        **kwargs: Additional Entity fields.

    Returns:
        Configured Entity instance.
    """
    return Entity(
        id=entity_id or f"entity_{uuid.uuid4().hex[:8]}",
        name=name,
        entity_type=entity_type,
        description=description,
        content=content,
        metadata=metadata or {},
        organization_id=organization_id,
        **kwargs,
    )
