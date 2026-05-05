from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest
from fastapi import HTTPException

from sibyl.api.routes.entities import _should_fallback_to_document_entity, get_entity
from sibyl.persistence.content_common import DocumentEntityRecord
from sibyl_core.models.entities import Entity, EntityType, Relationship, RelationshipType
from sibyl_core.storage import EntityBundle


class _AsyncContext:
    def __init__(self, value: object) -> None:
        self.value = value

    async def __aenter__(self) -> object:
        return self.value

    async def __aexit__(self, *args: object) -> None:
        return None


@pytest.mark.parametrize(
    ("entity_id", "expected"),
    [
        ("00000000-0000-0000-0000-000000000111", True),
        ("deadbeef", True),
        ("task_deadbeef", False),
        ("project_deadbeef", False),
        ("epic_deadbeef", False),
        ("not-a-chunk-id", False),
    ],
)
def test_should_fallback_to_document_entity_requires_chunk_id_shape(
    entity_id: str,
    expected: bool,
) -> None:
    assert _should_fallback_to_document_entity(entity_id) is expected


@pytest.mark.asyncio
async def test_get_entity_uses_knowledge_service_for_graph_entities() -> None:
    org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
    task = Entity(
        id="task-1",
        entity_type=EntityType.TASK,
        name="Ship the seam",
        metadata={"priority": "high"},
    )
    project = Entity(
        id="project-1",
        entity_type=EntityType.PROJECT,
        name="Sibyl Native",
    )
    relationship = Relationship(
        id="rel-1",
        relationship_type=RelationshipType.BELONGS_TO,
        source_id="task-1",
        target_id="project-1",
    )
    service = AsyncMock()
    service.get_entity_bundle.return_value = EntityBundle(
        entity=task,
        relationships=[relationship],
        related_entities=[project],
    )

    with patch(
        "sibyl.api.routes.entities.get_entity_graph_runtime",
        AsyncMock(),
    ) as get_entity_graph_runtime:
        response = await get_entity("task-1", org=org, service=service)

    assert response.id == "task-1"
    assert response.metadata["priority"] == "high"
    assert response.related is not None
    assert response.related[0].id == "project-1"
    assert response.related[0].relationship == "BELONGS_TO"
    service.get_entity_bundle.assert_awaited_once_with("task-1")
    get_entity_graph_runtime.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_entity_keeps_project_summary_enrichment() -> None:
    org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
    project = Entity(
        id="project-1",
        entity_type=EntityType.PROJECT,
        name="Sibyl Native",
        metadata={"status": "active"},
    )
    service = AsyncMock()
    service.get_entity_bundle.return_value = EntityBundle(entity=project)
    manager = MagicMock()
    manager.get_project_summary = AsyncMock(
        return_value={
            "total_tasks": 3,
            "status_counts": {"todo": 2, "doing": 1},
            "progress_pct": 33.3,
            "critical_tasks": [],
            "epics": [],
            "actionable_tasks": [
                {
                    "id": "task-1",
                    "name": "Ship graph seam",
                    "status": "doing",
                }
            ],
        }
    )
    runtime = SimpleNamespace(entity_manager=manager, relationship_manager=MagicMock())

    with patch(
        "sibyl.api.routes.entities.get_entity_graph_runtime",
        AsyncMock(return_value=runtime),
    ):
        response = await get_entity("project-1", org=org, service=service)

    assert response.id == "project-1"
    assert response.metadata["total_tasks"] == 3
    assert response.metadata["actionable_tasks"][0]["id"] == "task-1"
    assert response.related is not None
    assert response.related[0].id == "task-1"
    assert response.related[0].relationship == "doing"


@pytest.mark.asyncio
async def test_get_entity_graph_mode_skips_bundle_loading() -> None:
    org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
    task = Entity(
        id="task-1",
        entity_type=EntityType.TASK,
        name="Ship the seam",
        metadata={"priority": "high"},
    )
    service = AsyncMock()
    service.get_entity.return_value = task

    with patch(
        "sibyl.api.routes.entities.get_entity_graph_runtime",
        AsyncMock(),
    ) as get_entity_graph_runtime:
        response = await get_entity(
            "task-1",
            org=org,
            service=service,
            include_summary=False,
            related_limit=0,
        )

    assert response.id == "task-1"
    assert response.metadata["priority"] == "high"
    assert response.related is None
    service.get_entity.assert_awaited_once_with("task-1")
    service.get_entity_bundle.assert_not_awaited()
    get_entity_graph_runtime.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_entity_skips_document_fallback_for_typed_graph_ids() -> None:
    org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
    service = AsyncMock()
    service.get_entity_bundle.return_value = None

    with (
        patch("sibyl.api.routes.entities.get_content_read_session") as read_session,
        patch(
            "sibyl.api.routes.entities.content_runtime.resolve_document_entity",
            AsyncMock(),
        ) as resolve_document_entity,
        pytest.raises(HTTPException) as exc_info,
    ):
        await get_entity("task_deadbeef", org=org, service=service)

    assert exc_info.value.status_code == 404
    read_session.assert_not_called()
    resolve_document_entity.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_entity_keeps_document_fallback_for_uuid_shaped_ids() -> None:
    org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
    chunk_id = UUID("11111111-1111-1111-1111-111111111111")
    document_id = UUID("22222222-2222-2222-2222-222222222222")
    source_id = UUID("33333333-3333-3333-3333-333333333333")
    service = AsyncMock()
    service.get_entity_bundle.return_value = None
    record = DocumentEntityRecord(
        chunk_id=chunk_id,
        document_id=document_id,
        source_id=source_id,
        source_name="Docs",
        source_url="https://example.test",
        document_title="Install Guide",
        document_url="https://example.test/install",
        chunk_index=2,
        chunk_type=None,
        heading_path=("Guide", "Install"),
        language="python",
        content="Use the Surreal runtime.",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        updated_at=datetime(2026, 1, 2, tzinfo=UTC),
    )
    session = object()

    with (
        patch(
            "sibyl.api.routes.entities.get_content_read_session",
            return_value=_AsyncContext(session),
        ),
        patch(
            "sibyl.api.routes.entities.content_runtime.resolve_document_entity",
            AsyncMock(return_value=record),
        ) as resolve_document_entity,
    ):
        response = await get_entity(str(chunk_id), org=org, service=service)

    assert response.entity_type == EntityType.DOCUMENT
    assert response.id == str(chunk_id)
    assert response.name == "Install Guide"
    resolve_document_entity.assert_awaited_once_with(
        session,
        organization_id=org.id,
        entity_id=str(chunk_id),
    )


@pytest.mark.asyncio
async def test_get_entity_preserves_preloaded_project_related_context() -> None:
    org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
    project = Entity(
        id="project-1",
        entity_type=EntityType.PROJECT,
        name="Sibyl Native",
        metadata={"status": "active"},
    )
    pattern = Entity(
        id="pattern-1",
        entity_type=EntityType.PATTERN,
        name="Prefer graph-light sidebars",
    )
    relationship = Relationship(
        id="rel-1",
        relationship_type=RelationshipType.RELATED_TO,
        source_id="project-1",
        target_id="pattern-1",
    )
    service = AsyncMock()
    service.get_entity_bundle.return_value = EntityBundle(
        entity=project,
        relationships=[relationship],
        related_entities=[pattern],
    )
    manager = MagicMock()
    manager.get_project_summary = AsyncMock(
        return_value={
            "total_tasks": 3,
            "status_counts": {"todo": 2, "doing": 1},
            "progress_pct": 33.3,
            "critical_tasks": [],
            "epics": [],
            "actionable_tasks": [
                {
                    "id": "task-1",
                    "name": "Ship graph seam",
                    "status": "doing",
                }
            ],
        }
    )
    runtime = SimpleNamespace(entity_manager=manager, relationship_manager=MagicMock())

    with patch(
        "sibyl.api.routes.entities.get_entity_graph_runtime",
        AsyncMock(return_value=runtime),
    ):
        response = await get_entity("project-1", org=org, service=service)

    assert response.related is not None
    assert [rel.id for rel in response.related] == ["pattern-1"]
    assert response.metadata["actionable_tasks"][0]["id"] == "task-1"
