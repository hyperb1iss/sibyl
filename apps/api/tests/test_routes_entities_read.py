from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest
from fastapi import HTTPException

from sibyl.api.routes.entities import (
    _entity_visible_to_reader,
    _should_fallback_to_document_entity,
    _summarize_related_entities,
    get_entity,
)
from sibyl.auth.errors import ProjectAccessDeniedError
from sibyl.persistence.content_common import DocumentEntityRecord
from sibyl.persistence.graph_runtime import GraphEntityStore
from sibyl_core.auth import ProjectRole
from sibyl_core.auth.memory_policy import memory_metadata_read_allowed
from sibyl_core.models.entities import (
    Entity,
    EntityType,
    Procedure,
    Relationship,
    RelationshipType,
)
from sibyl_core.storage import EntityBundle


class _AsyncContext:
    def __init__(self, value: object) -> None:
        self.value = value

    async def __aenter__(self) -> object:
        return self.value

    async def __aexit__(self, *args: object) -> None:
        return None


def _ctx() -> SimpleNamespace:
    return SimpleNamespace(user=SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000222")))


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


def test_graph_entity_store_hydrates_native_mapping_without_node_to_entity() -> None:
    store = GraphEntityStore(SimpleNamespace(), driver=object(), group_id="org-1")

    entity = store.entity_from_node(
        {
            "uuid": "procedure-1",
            "name": "Procedure",
            "entity_type": "procedure",
            "group_id": "org-1",
            "attributes": {"metadata": {"category": None}},
        }
    )

    assert isinstance(entity, Procedure)
    assert entity.id == "procedure-1"
    assert entity.category == ""


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

    with (
        patch(
            "sibyl.api.routes.entities.get_entity_graph_runtime",
            AsyncMock(),
        ) as get_entity_graph_runtime,
        patch(
            "sibyl.api.routes.entities.list_accessible_project_graph_ids",
            AsyncMock(return_value={"project-1"}),
        ),
    ):
        response = await get_entity("task-1", org=org, ctx=_ctx(), service=service)

    assert response.id == "task-1"
    assert response.metadata["priority"] == "high"
    assert response.related is not None
    assert response.related[0].id == "project-1"
    assert response.related[0].relationship == "BELONGS_TO"
    service.get_entity_bundle.assert_awaited_once_with("task-1")
    get_entity_graph_runtime.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_entity_denies_private_memory_projection_for_non_owner() -> None:
    org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
    entity = Entity(
        id="person-1",
        entity_type=EntityType.PERSON,
        name="Private Person",
        description="secret",
        metadata={"memory_scope": "private", "scope_key": "different-user"},
    )
    service = AsyncMock()
    service.get_entity_bundle.return_value = EntityBundle(entity=entity)

    with (
        patch(
            "sibyl.api.routes.entities.get_entity_graph_runtime",
            AsyncMock(),
        ),
        patch(
            "sibyl.api.routes.entities.list_accessible_project_graph_ids",
            AsyncMock(return_value={"project-1"}),
        ),
        pytest.raises(HTTPException) as exc,
    ):
        await get_entity("person-1", org=org, ctx=_ctx(), service=service)

    assert exc.value.status_code == 404


@pytest.mark.parametrize(
    "metadata",
    [
        pytest.param({"memory_scope": "team", "scope_key": "team-x"}, id="team"),
        pytest.param({"memory_scope": "delegated", "scope_key": "del-x"}, id="delegated"),
        pytest.param({"memory_scope": "agent", "scope_key": "agent-x"}, id="agent"),
        pytest.param({"memory_scope": "sooper_sekrit", "scope_key": "x"}, id="unrecognized"),
    ],
)
def test_entity_visibility_matches_the_search_rule_on_every_scope(
    metadata: dict[str, str],
) -> None:
    """A local copy served every scope it did not name to the whole org.

    Reading a row and finding it through search ask the same question, so a
    shape either implementation allows and the other refuses is reachable on
    one surface and hidden on the other.
    """
    reader = "reader-1"
    entity = SimpleNamespace(metadata=metadata)

    route_allows = _entity_visible_to_reader(
        entity,
        reader_user_id=reader,
        accessible_projects=set(),
        allowed_memory_scope_keys=None,
    )
    search_allows = memory_metadata_read_allowed(
        metadata,
        principal_id=reader,
        accessible_projects=set(),
        private_scope_granted=True,
    )

    assert route_allows is False
    assert route_allows == search_allows


def test_related_summaries_hide_a_private_neighbour_but_keep_your_own() -> None:
    """A readable row must not become a pivot onto its private neighbours.

    Neighbour summaries carry the entity name, and they were screened by
    project membership alone — which a private memory, having no project,
    passes. Both constraints are orthogonal and both have to hold.
    """
    reader = "reader-1"
    others_private = SimpleNamespace(
        id="decision_private",
        name="Acquisition target is Initech",
        entity_type="episode",
        metadata={"memory_scope": "private", "principal_id": "victim"},
    )
    own_private = SimpleNamespace(
        id="decision_mine",
        name="My own note",
        entity_type="episode",
        metadata={"memory_scope": "private", "principal_id": reader},
    )
    unscoped = SimpleNamespace(
        id="episode_shared",
        name="Shared episode",
        entity_type="episode",
        metadata={},
    )
    neighbours = [others_private, own_private, unscoped]
    relationships = [
        SimpleNamespace(
            source_id="task-1",
            target_id=neighbour.id,
            relationship_type="RELATED_TO",
        )
        for neighbour in neighbours
    ]

    summaries = _summarize_related_entities(
        "task-1",
        related_entities=neighbours,
        relationships=relationships,
        accessible_projects=set(),
        reader_user_id=reader,
    )

    assert [summary.id for summary in summaries or []] == ["decision_mine", "episode_shared"]
    assert "Acquisition target" not in str(summaries)


def test_entity_visibility_treats_scope_key_as_a_private_owner_channel() -> None:
    """`owner = principal_id or scope_key`, so both channels resolve an owner."""
    reader = "reader-1"
    owned = SimpleNamespace(metadata={"memory_scope": "private", "scope_key": reader})
    other = SimpleNamespace(metadata={"memory_scope": "private", "scope_key": "someone-else"})

    assert (
        _entity_visible_to_reader(
            owned, reader_user_id=reader, accessible_projects=set(), allowed_memory_scope_keys=None
        )
        is True
    )
    assert (
        _entity_visible_to_reader(
            other, reader_user_id=reader, accessible_projects=set(), allowed_memory_scope_keys=None
        )
        is False
    )


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

    with (
        patch(
            "sibyl.api.routes.entities.get_entity_graph_runtime",
            AsyncMock(return_value=runtime),
        ),
        patch(
            "sibyl.api.routes.entities.verify_entity_project_access",
            AsyncMock(),
        ),
        patch(
            "sibyl.api.routes.entities.list_accessible_project_graph_ids",
            AsyncMock(return_value={"project-1"}),
        ),
    ):
        response = await get_entity("project-1", org=org, ctx=_ctx(), service=service)

    assert response.id == "project-1"
    assert response.metadata["total_tasks"] == 3
    assert response.metadata["actionable_tasks"][0]["id"] == "task-1"
    assert response.related is not None
    assert response.related[0].id == "task-1"
    assert response.related[0].relationship == "doing"


@pytest.mark.asyncio
async def test_get_entity_summary_without_related_skips_bundle_loading() -> None:
    org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
    project = Entity(
        id="project-1",
        entity_type=EntityType.PROJECT,
        name="Sibyl Native",
        metadata={"status": "active"},
    )
    service = AsyncMock()
    service.get_entity.return_value = project
    manager = MagicMock()
    manager.get_project_summary = AsyncMock(
        return_value={
            "total_tasks": 1,
            "status_counts": {"doing": 1},
            "progress_pct": 0,
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

    with (
        patch(
            "sibyl.api.routes.entities.get_entity_graph_runtime",
            AsyncMock(return_value=runtime),
        ),
        patch(
            "sibyl.api.routes.entities.verify_entity_project_access",
            AsyncMock(),
        ),
        patch(
            "sibyl.api.routes.entities.list_accessible_project_graph_ids",
            AsyncMock(return_value={"project-1"}),
        ),
    ):
        response = await get_entity(
            "project-1",
            org=org,
            ctx=_ctx(),
            service=service,
            related_limit=0,
        )

    assert response.metadata["total_tasks"] == 1
    assert response.metadata["actionable_tasks"][0]["id"] == "task-1"
    assert response.related is None
    service.get_entity.assert_awaited_once_with("project-1")
    service.get_entity_bundle.assert_not_awaited()


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

    with (
        patch(
            "sibyl.api.routes.entities.get_entity_graph_runtime",
            AsyncMock(),
        ) as get_entity_graph_runtime,
        patch(
            "sibyl.api.routes.entities.list_accessible_project_graph_ids",
            AsyncMock(return_value=set()),
        ),
    ):
        response = await get_entity(
            "task-1",
            org=org,
            ctx=_ctx(),
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
        await get_entity("task_deadbeef", org=org, ctx=_ctx(), service=service)

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
        response = await get_entity(str(chunk_id), org=org, ctx=_ctx(), service=service)

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

    with (
        patch(
            "sibyl.api.routes.entities.get_entity_graph_runtime",
            AsyncMock(return_value=runtime),
        ),
        patch(
            "sibyl.api.routes.entities.verify_entity_project_access",
            AsyncMock(),
        ),
        patch(
            "sibyl.api.routes.entities.list_accessible_project_graph_ids",
            AsyncMock(return_value={"project-1"}),
        ),
    ):
        response = await get_entity("project-1", org=org, ctx=_ctx(), service=service)

    assert response.related is not None
    assert [rel.id for rel in response.related] == ["pattern-1"]
    assert response.metadata["actionable_tasks"][0]["id"] == "task-1"


@pytest.mark.asyncio
async def test_get_entity_rejects_inaccessible_project_entity() -> None:
    org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
    project = Entity(
        id="project-hidden",
        entity_type=EntityType.PROJECT,
        name="Hidden Project",
    )
    service = AsyncMock()
    service.get_entity_bundle.return_value = EntityBundle(entity=project)

    with (
        patch(
            "sibyl.api.routes.entities.verify_entity_project_access",
            AsyncMock(
                side_effect=ProjectAccessDeniedError(
                    project_id="project-hidden",
                    required_role=ProjectRole.VIEWER.value,
                )
            ),
        ),
        pytest.raises(ProjectAccessDeniedError) as exc,
    ):
        await get_entity("project-hidden", org=org, ctx=_ctx(), service=service)

    assert exc.value.status_code == 403
    assert exc.value.detail["details"]["project_id"] == "project-hidden"


@pytest.mark.asyncio
async def test_get_entity_rejects_inaccessible_project_scoped_entity() -> None:
    org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
    task = Entity(
        id="task-hidden",
        entity_type=EntityType.TASK,
        name="Hidden task",
        metadata={"project_id": "project-hidden"},
    )
    service = AsyncMock()
    service.get_entity_bundle.return_value = EntityBundle(entity=task)

    with (
        patch(
            "sibyl.api.routes.entities.verify_entity_project_access",
            AsyncMock(
                side_effect=ProjectAccessDeniedError(
                    project_id="project-hidden",
                    required_role=ProjectRole.VIEWER.value,
                )
            ),
        ),
        pytest.raises(ProjectAccessDeniedError) as exc,
    ):
        await get_entity("task-hidden", org=org, ctx=_ctx(), service=service)

    assert exc.value.status_code == 403
    assert exc.value.detail["details"]["project_id"] == "project-hidden"


@pytest.mark.asyncio
async def test_get_entity_filters_inaccessible_related_entities() -> None:
    org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
    task = Entity(
        id="task-1",
        entity_type=EntityType.TASK,
        name="Scoped task",
        metadata={"project_id": "project-visible"},
    )
    hidden_task = Entity(
        id="task-hidden",
        entity_type=EntityType.TASK,
        name="Hidden task",
        metadata={"project_id": "project-hidden"},
    )
    unassigned_pattern = Entity(
        id="pattern-1",
        entity_type=EntityType.PATTERN,
        name="Visible pattern",
    )
    service = AsyncMock()
    service.get_entity_bundle.return_value = EntityBundle(
        entity=task,
        relationships=[
            Relationship(
                id="rel-hidden",
                relationship_type=RelationshipType.RELATED_TO,
                source_id="task-1",
                target_id="task-hidden",
            ),
            Relationship(
                id="rel-visible",
                relationship_type=RelationshipType.RELATED_TO,
                source_id="task-1",
                target_id="pattern-1",
            ),
        ],
        related_entities=[hidden_task, unassigned_pattern],
    )

    with (
        patch(
            "sibyl.api.routes.entities.verify_entity_project_access",
            AsyncMock(),
        ),
        patch(
            "sibyl.api.routes.entities.list_accessible_project_graph_ids",
            AsyncMock(return_value={"project-visible"}),
        ),
    ):
        response = await get_entity("task-1", org=org, ctx=_ctx(), service=service)

    assert response.related is not None
    assert [related.id for related in response.related] == ["pattern-1"]


@pytest.mark.asyncio
async def test_get_entity_graph_mode_filters_fetched_related_entities() -> None:
    org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
    task = Entity(
        id="task-visible",
        entity_type=EntityType.TASK,
        name="Visible task",
        metadata={"project_id": "project-visible"},
    )
    hidden_task = Entity(
        id="task-hidden",
        entity_type=EntityType.TASK,
        name="Hidden task",
        metadata={"project_id": "project-hidden"},
    )
    visible_project = Entity(
        id="project-visible",
        entity_type=EntityType.PROJECT,
        name="Visible project",
    )
    hidden_project = Entity(
        id="project-hidden",
        entity_type=EntityType.PROJECT,
        name="Hidden project",
    )
    unassigned_pattern = Entity(
        id="pattern-unassigned",
        entity_type=EntityType.PATTERN,
        name="Unassigned pattern",
    )
    service = AsyncMock()
    service.get_entity.return_value = task
    relationship_manager = MagicMock()
    relationship_manager.get_related_entities = AsyncMock(
        return_value=[
            (
                hidden_task,
                Relationship(
                    id="rel-hidden-task",
                    relationship_type=RelationshipType.RELATED_TO,
                    source_id="task-visible",
                    target_id="task-hidden",
                ),
            ),
            (
                hidden_project,
                Relationship(
                    id="rel-hidden-project",
                    relationship_type=RelationshipType.RELATED_TO,
                    source_id="task-visible",
                    target_id="project-hidden",
                ),
            ),
            (
                visible_project,
                Relationship(
                    id="rel-visible-project",
                    relationship_type=RelationshipType.BELONGS_TO,
                    source_id="task-visible",
                    target_id="project-visible",
                ),
            ),
            (
                unassigned_pattern,
                Relationship(
                    id="rel-unassigned",
                    relationship_type=RelationshipType.RELATED_TO,
                    source_id="task-visible",
                    target_id="pattern-unassigned",
                ),
            ),
        ]
    )
    runtime = SimpleNamespace(relationship_manager=relationship_manager)
    ctx = _ctx()

    with (
        patch(
            "sibyl.api.routes.entities.get_entity_graph_runtime",
            AsyncMock(return_value=runtime),
        ),
        patch(
            "sibyl.api.routes.entities.verify_entity_project_access",
            AsyncMock(),
        ) as verify_project,
        patch(
            "sibyl.api.routes.entities.list_accessible_project_graph_ids",
            AsyncMock(return_value={"project-visible"}),
        ),
    ):
        response = await get_entity(
            "task-visible",
            org=org,
            ctx=ctx,
            service=service,
            include_summary=False,
            related_limit=5,
        )

    verify_project.assert_awaited_once_with(
        None,
        ctx,
        "project-visible",
        required_role=ProjectRole.VIEWER,
    )
    assert response.related is not None
    assert [related.id for related in response.related] == [
        "project-visible",
        "pattern-unassigned",
    ]
