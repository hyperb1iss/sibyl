from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from sibyl.api.routes.entities import (
    _entity_from_bulk_create,
    create_entities_bulk,
    create_entity,
    delete_entity,
    requeue_entity_background_jobs,
    update_entity,
)
from sibyl.api.schemas import (
    EntityBackgroundJobsRequeueRequest,
    EntityBulkCreateRequest,
    EntityCreate,
    EntityUpdate,
)
from sibyl.auth.errors import ProjectAccessDeniedError
from sibyl_core.auth import ProjectRole
from sibyl_core.models.entities import Entity, EntityType, Relationship, RelationshipType


def _request() -> MagicMock:
    request = MagicMock()
    request.headers = {}
    request.cookies = {}
    request.client = SimpleNamespace(host="127.0.0.1")
    return request


def _org() -> SimpleNamespace:
    return SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))


def _ctx() -> SimpleNamespace:
    return SimpleNamespace(user=SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000222")))


def test_bulk_entity_ids_are_idempotent_within_project_and_isolated_between_projects() -> None:
    now = datetime.now(UTC)

    def build(project_id: str) -> Entity:
        return _entity_from_bulk_create(
            EntityCreate(
                name="Shared session",
                content="same source content",
                entity_type=EntityType.SESSION,
                skip_conflicts=True,
                metadata={"project_id": project_id},
            ),
            group_id=str(_org().id),
            now=now,
        )

    first = build("project_first")

    assert first.id == build("project_first").id
    assert first.id != build("project_second").id


def _project_entity(*, name: str, description: str) -> SimpleNamespace:
    return SimpleNamespace(
        id="project_new",
        entity_type=EntityType.PROJECT,
        name=name,
        description=description,
        content=description,
        category=None,
        languages=[],
        tags=[],
        metadata={},
        source_file=None,
        created_at=None,
        updated_at=None,
    )


@asynccontextmanager
async def _locked_entity(*_args, **_kwargs):
    yield "lock-token"


@pytest.mark.asyncio
async def test_create_project_routes_through_runtime_project_record() -> None:
    org = _org()
    ctx = _ctx()
    entity = EntityCreate(
        name="Surreal Native",
        description="cut postgres loose",
        entity_type=EntityType.PROJECT,
    )
    add_result = SimpleNamespace(success=True, id="project_new", message="ok")
    runtime = SimpleNamespace(
        entity_manager=SimpleNamespace(
            get=AsyncMock(
                return_value=_project_entity(name=entity.name, description=entity.description)
            )
        )
    )

    with (
        patch("sibyl_core.tools.core.add", AsyncMock(return_value=add_result)),
        patch(
            "sibyl.api.routes.entities.get_entity_graph_runtime", AsyncMock(return_value=runtime)
        ),
        patch("sibyl.api.routes.entities.broadcast_event", AsyncMock()),
        patch("sibyl.api.routes.entities.create_project_record", AsyncMock()) as create_project,
        patch("sibyl.api.routes.entities.log_audit_event", AsyncMock()) as audit_log,
    ):
        response = await create_entity(
            request=_request(),
            entity=entity,
            org=org,
            ctx=ctx,
            content_session=None,
            sync=False,
        )

    assert response.id == "project_new"
    runtime.entity_manager.get.assert_not_awaited()
    create_project.assert_awaited_once_with(
        organization_id=org.id,
        owner_user_id=ctx.user.id,
        graph_project_id="project_new",
        name="Surreal Native",
        description="cut postgres loose",
    )
    audit_log.assert_awaited_once()


@pytest.mark.asyncio
async def test_create_entity_can_defer_embeddings_to_background_backfill() -> None:
    org = _org()
    ctx = _ctx()
    entity = EntityCreate(
        name="Lexical first",
        content="Persist immediately and backfill vectors after the write.",
        entity_type=EntityType.SESSION,
        defer_embeddings=True,
    )
    add_result = SimpleNamespace(
        success=True,
        id="session_new",
        message="queued",
        background_jobs={
            "embedding_backfill": {
                "status": "deferred",
                "queued_by": "create_entity:session_new",
                "queued_entities": 1,
                "queued_relationships": 0,
            }
        },
    )
    runtime = SimpleNamespace(entity_manager=SimpleNamespace(get=AsyncMock()))

    with (
        patch("sibyl_core.tools.core.add", AsyncMock(return_value=add_result)) as add,
        patch(
            "sibyl.api.routes.entities.get_entity_graph_runtime",
            AsyncMock(return_value=runtime),
        ),
        patch("sibyl.api.routes.entities.broadcast_event", AsyncMock()),
    ):
        response = await create_entity(
            request=_request(),
            entity=entity,
            org=org,
            ctx=ctx,
            content_session=None,
            sync=False,
        )

    assert response.id == "session_new"
    assert response.background_jobs["embedding_backfill"]["status"] == "deferred"
    add.assert_awaited_once()
    assert add.await_args.kwargs["generate_embeddings"] is False


@pytest.mark.asyncio
async def test_create_entity_broadcast_carries_no_memory_content() -> None:
    """The broadcast reaches every connection in the org, authorized as none.

    A connection authenticates as an organization and carries no principal,
    so a private memory's content on this channel reaches every client.
    """
    org = _org()
    ctx = _ctx()
    entity = EntityCreate(
        name="Acquisition target is Initech",
        content="board voted 5-2, do not disclose",
        entity_type=EntityType.EPISODE,
        metadata={"memory_scope": "private"},
    )
    add_result = SimpleNamespace(
        success=True,
        id="decision_private",
        message="ok",
        background_jobs=None,
    )
    runtime = SimpleNamespace(entity_manager=SimpleNamespace(get=AsyncMock()))
    broadcast = AsyncMock()

    with (
        patch("sibyl_core.tools.core.add", AsyncMock(return_value=add_result)),
        patch(
            "sibyl.api.routes.entities.get_entity_graph_runtime",
            AsyncMock(return_value=runtime),
        ),
        patch("sibyl.api.routes.entities.broadcast_event", broadcast),
    ):
        response = await create_entity(
            request=_request(),
            entity=entity,
            org=org,
            ctx=ctx,
            content_session=None,
            sync=False,
        )

    # The response still carries the content to its authorized caller.
    assert response.content == "board voted 5-2, do not disclose"
    assert broadcast.await_args_list
    for call in broadcast.await_args_list:
        assert call.args[1] == {"id": "decision_private", "entity_type": "episode"}
    assert "do not disclose" not in str(broadcast.await_args_list)
    assert "Acquisition target" not in str(broadcast.await_args_list)


@pytest.mark.asyncio
async def test_create_entities_bulk_uses_runtime_bulk_create() -> None:
    org = _org()
    ctx = _ctx()
    batch = EntityBulkCreateRequest(
        entities=[
            EntityCreate(
                name="Session one",
                content="semantic memory content",
                entity_type=EntityType.SESSION,
                skip_conflicts=True,
                metadata={"source": "import"},
            ),
            EntityCreate(
                name="Session two",
                content="more semantic memory content",
                entity_type=EntityType.SESSION,
                skip_conflicts=True,
                metadata={"source": "import"},
            ),
        ]
    )
    runtime = SimpleNamespace(
        entity_manager=SimpleNamespace(
            create_direct_bulk=AsyncMock(return_value=["session_one", "session_two"])
        ),
        relationship_manager=SimpleNamespace(create_bulk=AsyncMock(return_value=(0, 0))),
    )

    with patch(
        "sibyl.api.routes.entities.get_entity_graph_runtime",
        AsyncMock(return_value=runtime),
    ):
        response = await create_entities_bulk(
            batch=batch,
            org=org,
            ctx=ctx,
            content_session=None,
        )

    assert response.created == 2
    assert [entity.id for entity in response.entities] == ["session_one", "session_two"]
    runtime.entity_manager.create_direct_bulk.assert_awaited_once()
    call = runtime.entity_manager.create_direct_bulk.await_args
    assert len(call.args[0]) == 2
    assert call.kwargs == {"generate_embeddings": True}


@pytest.mark.asyncio
async def test_create_entities_bulk_verifies_each_project_once() -> None:
    org = _org()
    ctx = _ctx()
    batch = EntityBulkCreateRequest(
        entities=[
            EntityCreate(
                name=f"Session {index}",
                content="project-scoped memory",
                entity_type=EntityType.SESSION,
                skip_conflicts=True,
                metadata={"project_id": "project_shared"},
            )
            for index in range(2)
        ]
    )
    runtime = SimpleNamespace(
        entity_manager=SimpleNamespace(
            create_direct_bulk=AsyncMock(return_value=["session_one", "session_two"])
        ),
        relationship_manager=SimpleNamespace(create_bulk=AsyncMock(return_value=(0, 0))),
    )

    with (
        patch(
            "sibyl.api.routes.entities.get_entity_graph_runtime",
            AsyncMock(return_value=runtime),
        ),
        patch(
            "sibyl.api.routes.entities.verify_entity_project_access",
            AsyncMock(),
        ) as verify_access,
    ):
        await create_entities_bulk(
            batch=batch,
            org=org,
            ctx=ctx,
            content_session="session",
        )

    verify_access.assert_awaited_once_with(
        "session",
        ctx,
        "project_shared",
        required_role=ProjectRole.CONTRIBUTOR,
        require_existing_project=True,
    )


@pytest.mark.asyncio
async def test_create_entities_bulk_can_defer_embeddings_to_backfill_job() -> None:
    org = _org()
    ctx = _ctx()
    batch = EntityBulkCreateRequest(
        defer_embeddings=True,
        entities=[
            EntityCreate(
                name="Session one",
                content="lexical memory content",
                entity_type=EntityType.SESSION,
                skip_conflicts=True,
                related_to=["pattern_existing"],
            )
        ],
    )
    entity_manager = SimpleNamespace(
        create_direct_bulk=AsyncMock(return_value=["session_one"]),
        get=AsyncMock(return_value=SimpleNamespace(metadata={})),
    )
    relationship_manager = SimpleNamespace(
        create_direct_bulk=AsyncMock(return_value=["rel_session_one_related_to_pattern_existing"]),
        create_bulk=AsyncMock(return_value=(0, 0)),
    )
    runtime = SimpleNamespace(
        entity_manager=entity_manager,
        relationship_manager=relationship_manager,
    )
    extraction_enqueue = SimpleNamespace(
        status="skipped",
        job_ids=(),
        queued_sources=0,
        skipped_sources=1,
        queue_depth=0,
        reason="disabled",
    )

    with (
        patch(
            "sibyl.api.routes.entities.get_entity_graph_runtime",
            AsyncMock(return_value=runtime),
        ),
        patch(
            "sibyl.jobs.queue.enqueue_entity_embedding_backfill",
            AsyncMock(return_value="embed-entities-1"),
        ) as enqueue_embeddings,
        patch(
            "sibyl.jobs.memory_extraction.enqueue_memory_extraction_batches",
            AsyncMock(return_value=extraction_enqueue),
        ),
    ):
        response = await create_entities_bulk(
            batch=batch,
            org=org,
            ctx=ctx,
            content_session=None,
        )

    assert entity_manager.create_direct_bulk.await_args.kwargs == {"generate_embeddings": False}
    assert relationship_manager.create_direct_bulk.await_args.kwargs["generate_embeddings"] is False
    enqueue_embeddings.assert_awaited_once()
    entities_payload, group_id = enqueue_embeddings.await_args.args
    assert group_id == str(org.id)
    assert entities_payload[0]["id"] == "session_one"
    assert enqueue_embeddings.await_args.kwargs["relationships"][0]["id"] == (
        "rel_session_one_related_to_pattern_existing"
    )
    jobs = response.background_jobs["embedding_backfill"]
    assert jobs["status"] == "queued"
    assert jobs["job_ids"] == ["embed-entities-1"]
    assert jobs["queued_entities"] == 1
    assert jobs["queued_relationships"] == 1


@pytest.mark.asyncio
async def test_requeue_entity_background_jobs_uses_persisted_entities() -> None:
    org = _org()
    ctx = _ctx()
    entity = Entity(
        id="session_one",
        entity_type=EntityType.SESSION,
        name="Session one",
        content="I bought a Samsung TV for the den.",
        organization_id=str(org.id),
        metadata={"project_id": "project_shared"},
    )
    relationship = Relationship(
        id="rel-session-pattern",
        source_id=entity.id,
        target_id="pattern_shared",
        relationship_type=RelationshipType.RELATED_TO,
    )
    runtime = SimpleNamespace(
        entity_manager=SimpleNamespace(get=AsyncMock(return_value=entity)),
        relationship_manager=SimpleNamespace(get_for_entity=AsyncMock(return_value=[relationship])),
    )

    with (
        patch(
            "sibyl.api.routes.entities.get_entity_graph_runtime",
            AsyncMock(return_value=runtime),
        ),
        patch(
            "sibyl.api.routes.entities.verify_entity_project_access",
            AsyncMock(),
        ) as verify_access,
        patch(
            "sibyl.api.routes.entities._require_entity_read_access",
            AsyncMock(return_value={"project_shared"}),
        ) as require_read_access,
        patch(
            "sibyl.api.routes.entities.extract_projected_memory_entities",
            MagicMock(return_value=[{"entity_type": "fact"}]),
        ),
        patch(
            "sibyl.jobs.queue.enqueue_entity_embedding_backfill",
            AsyncMock(return_value="embed-restored"),
        ) as enqueue_embeddings,
        patch(
            "sibyl.jobs.queue.enqueue_memory_projection",
            AsyncMock(return_value="projection-restored"),
        ) as enqueue_projection,
    ):
        response = await requeue_entity_background_jobs(
            request=EntityBackgroundJobsRequeueRequest(
                entity_ids=[entity.id],
                jobs=["embedding_backfill", "memory_projection"],
            ),
            org=org,
            ctx=ctx,
            content_session="session",
        )

    verify_access.assert_awaited_once_with(
        "session",
        ctx,
        "project_shared",
        required_role=ProjectRole.CONTRIBUTOR,
        require_existing_project=True,
    )
    require_read_access.assert_awaited_once_with(ctx, entity)
    embedding_payload, embedding_group = enqueue_embeddings.await_args.args
    assert embedding_group == str(org.id)
    assert embedding_payload[0]["id"] == entity.id
    assert enqueue_embeddings.await_args.kwargs["relationships"][0]["id"] == relationship.id
    projection_payload, projection_group = enqueue_projection.await_args.args
    assert projection_group == str(org.id)
    assert projection_payload[0]["id"] == entity.id
    assert enqueue_projection.await_args.kwargs == {"created_source_ids": [entity.id]}
    assert response.entity_ids == [entity.id]
    assert response.background_jobs["embedding_backfill"]["job_ids"] == ["embed-restored"]
    assert response.background_jobs["memory_projection"]["job_ids"] == ["projection-restored"]


@pytest.mark.parametrize(
    "payload",
    [
        {"jobs": ["embedding_backfill"]},
        {
            "entity_ids": ["event-one"],
            "manifest_id": "artifact-one",
            "jobs": ["embedding_backfill"],
        },
        {"manifest_id": "artifact-one", "jobs": ["memory_projection"]},
    ],
)
def test_requeue_background_jobs_requires_one_compatible_target(
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        EntityBackgroundJobsRequeueRequest.model_validate(payload)


@pytest.mark.asyncio
async def test_requeue_operational_embedding_job_restores_manifest_completion() -> None:
    org = _org()
    ctx = _ctx()
    event = Entity(
        id="event_capture-1",
        entity_type=EntityType.EVENT,
        name="Incident state",
        content="Incident INC001 is closed.",
        organization_id=str(org.id),
        metadata={"project_id": "project_shared"},
    )
    manifest = Entity(
        id="artifact_capture-1",
        entity_type=EntityType.ARTIFACT,
        name="Operational experience manifest",
        content="capture-1",
        organization_id=str(org.id),
        metadata={
            "project_id": "project_shared",
            "projection_kind": "manifest",
            "operational_projection_state": "embedding_pending",
            "expected_entity_ids": [event.id, "artifact_capture-1"],
            "expected_relationship_ids": ["rel-event-manifest"],
            "_direct_insert": True,
            "description": "hydrated storage field",
            "entity_type": "artifact",
            "source_file": "",
            "updated_at": "2026-07-14T00:00:00+00:00",
        },
    )
    persisted = {event.id: event, manifest.id: manifest}
    relationship_manager = SimpleNamespace(get_for_entity=AsyncMock())
    runtime = SimpleNamespace(
        entity_manager=SimpleNamespace(
            get=AsyncMock(side_effect=lambda entity_id: persisted[entity_id]),
            get_many=AsyncMock(
                side_effect=lambda entity_ids: [
                    persisted[entity_id] for entity_id in entity_ids if entity_id in persisted
                ]
            ),
        ),
        relationship_manager=relationship_manager,
    )

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
            "sibyl.api.routes.entities._require_entity_read_access",
            AsyncMock(return_value={"project_shared"}),
        ),
        patch(
            "sibyl.jobs.queue.enqueue_entity_embedding_backfill",
            AsyncMock(return_value="embed-restored"),
        ) as enqueue_embeddings,
    ):
        response = await requeue_entity_background_jobs(
            request=EntityBackgroundJobsRequeueRequest(
                entity_ids=[event.id, manifest.id],
                jobs=["embedding_backfill"],
            ),
            org=org,
            ctx=ctx,
            content_session="session",
        )

    embedding_payload, embedding_group = enqueue_embeddings.await_args.args
    assert embedding_group == str(org.id)
    assert [entity["id"] for entity in embedding_payload] == [event.id]
    assert enqueue_embeddings.await_args.kwargs["relationships"] == []
    completion_manifest = enqueue_embeddings.await_args.kwargs["completion_manifest"]
    assert completion_manifest["id"] == manifest.id
    assert completion_manifest["metadata"]["operational_projection_state"] == "complete"
    assert "_direct_insert" not in completion_manifest["metadata"]
    assert "description" not in completion_manifest["metadata"]
    assert "entity_type" not in completion_manifest["metadata"]
    assert "source_file" not in completion_manifest["metadata"]
    assert "updated_at" not in completion_manifest["metadata"]
    relationship_manager.get_for_entity.assert_not_awaited()
    assert response.background_jobs["embedding_backfill"]["queued_entities"] == 1
    assert response.manifest_id is None
    assert runtime.entity_manager.get.await_count == 2
    runtime.entity_manager.get_many.assert_not_awaited()


@pytest.mark.asyncio
async def test_requeue_completed_operational_manifest_is_idempotent() -> None:
    org = _org()
    ctx = _ctx()
    manifest = Entity(
        id="artifact_capture-complete",
        entity_type=EntityType.ARTIFACT,
        name="Operational experience manifest",
        content="capture-complete",
        organization_id=str(org.id),
        metadata={
            "project_id": "project_shared",
            "projection_kind": "manifest",
            "operational_projection_state": "complete",
            "expected_entity_ids": ["event_pruned", "artifact_capture-complete"],
        },
    )
    runtime = SimpleNamespace(
        entity_manager=SimpleNamespace(
            get=AsyncMock(return_value=manifest),
            get_many=AsyncMock(),
        )
    )

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
            "sibyl.api.routes.entities._require_entity_read_access",
            AsyncMock(return_value={"project_shared"}),
        ),
        patch(
            "sibyl.jobs.queue.enqueue_entity_embedding_backfill",
            AsyncMock(),
        ) as enqueue_embeddings,
    ):
        response = await requeue_entity_background_jobs(
            request=EntityBackgroundJobsRequeueRequest(
                manifest_id=manifest.id,
                jobs=["embedding_backfill"],
            ),
            org=org,
            ctx=ctx,
            content_session="session",
        )

    assert response.entity_ids == [manifest.id]
    assert response.background_jobs["embedding_backfill"] == {
        "status": "skipped",
        "job_ids": [],
        "reason": "manifest_complete",
    }
    runtime.entity_manager.get_many.assert_not_awaited()
    enqueue_embeddings.assert_not_awaited()


@pytest.mark.asyncio
async def test_requeue_operational_manifest_requires_itself_in_inventory() -> None:
    manifest = Entity(
        id="artifact_capture-malformed",
        entity_type=EntityType.ARTIFACT,
        name="Operational experience manifest",
        content="capture-malformed",
        organization_id=str(_org().id),
        metadata={
            "projection_kind": "manifest",
            "operational_projection_state": "embedding_pending",
            "expected_entity_ids": ["event_capture-1"],
        },
    )
    runtime = SimpleNamespace(
        entity_manager=SimpleNamespace(
            get=AsyncMock(return_value=manifest),
            get_many=AsyncMock(),
        )
    )

    with (
        patch(
            "sibyl.api.routes.entities.get_entity_graph_runtime",
            AsyncMock(return_value=runtime),
        ),
        patch(
            "sibyl.api.routes.entities._require_entity_read_access",
            AsyncMock(return_value=set()),
        ),
        pytest.raises(
            HTTPException, match="recoverable operational embedding manifest"
        ) as exc_info,
    ):
        await requeue_entity_background_jobs(
            request=EntityBackgroundJobsRequeueRequest(
                manifest_id=manifest.id,
                jobs=["embedding_backfill"],
            ),
            org=_org(),
            ctx=_ctx(),
            content_session="session",
        )

    assert exc_info.value.status_code == 409
    runtime.entity_manager.get_many.assert_not_awaited()


@pytest.mark.asyncio
async def test_requeue_operational_manifest_hides_unreadable_metadata() -> None:
    manifest = Entity(
        id="artifact_capture-private",
        entity_type=EntityType.ARTIFACT,
        name="Operational experience manifest",
        content="capture-private",
        organization_id=str(_org().id),
        metadata={"projection_kind": "invalid"},
    )
    runtime = SimpleNamespace(
        entity_manager=SimpleNamespace(
            get=AsyncMock(return_value=manifest),
            get_many=AsyncMock(),
        )
    )

    with (
        patch(
            "sibyl.api.routes.entities.get_entity_graph_runtime",
            AsyncMock(return_value=runtime),
        ),
        patch(
            "sibyl.api.routes.entities._require_entity_read_access",
            AsyncMock(side_effect=HTTPException(status_code=404, detail="Entity not found")),
        ),
        pytest.raises(HTTPException) as exc_info,
    ):
        await requeue_entity_background_jobs(
            request=EntityBackgroundJobsRequeueRequest(
                manifest_id=manifest.id,
                jobs=["embedding_backfill"],
            ),
            org=_org(),
            ctx=_ctx(),
            content_session="session",
        )

    assert exc_info.value.status_code == 404
    runtime.entity_manager.get_many.assert_not_awaited()


@pytest.mark.asyncio
async def test_requeue_operational_embedding_job_resolves_large_manifest_inventory() -> None:
    org = _org()
    ctx = _ctx()
    events = [
        Entity(
            id=f"event_capture-{index}",
            entity_type=EntityType.EVENT,
            name=f"Incident state {index}",
            content="Incident INC001 is closed.",
            organization_id=str(org.id),
            metadata={"project_id": "project_shared"},
        )
        for index in range(129)
    ]
    manifest = Entity(
        id="artifact_capture-large",
        entity_type=EntityType.ARTIFACT,
        name="Operational experience manifest",
        content="capture-large",
        organization_id=str(org.id),
        metadata={
            "project_id": "project_shared",
            "projection_kind": "manifest",
            "operational_projection_state": "embedding_pending",
            "expected_entity_ids": [*[event.id for event in events], "artifact_capture-large"],
            "expected_relationship_ids": [],
        },
    )
    persisted = {entity.id: entity for entity in [*events, manifest]}
    runtime = SimpleNamespace(
        entity_manager=SimpleNamespace(
            get=AsyncMock(return_value=manifest),
            get_many=AsyncMock(
                side_effect=lambda entity_ids: [persisted[entity_id] for entity_id in entity_ids]
            ),
        ),
        relationship_manager=SimpleNamespace(get_for_entity=AsyncMock()),
    )

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
            "sibyl.api.routes.entities._require_entity_read_access",
            AsyncMock(return_value={"project_shared"}),
        ),
        patch(
            "sibyl.jobs.queue.enqueue_entity_embedding_backfill",
            AsyncMock(return_value="embed-restored"),
        ) as enqueue_embeddings,
    ):
        response = await requeue_entity_background_jobs(
            request=EntityBackgroundJobsRequeueRequest(
                manifest_id=manifest.id,
                jobs=["embedding_backfill"],
            ),
            org=org,
            ctx=ctx,
            content_session="session",
        )

    assert response.manifest_id == manifest.id
    assert response.background_jobs["embedding_backfill"]["queued_entities"] == len(events)
    assert len(enqueue_embeddings.await_args.args[0]) == len(events)
    runtime.relationship_manager.get_for_entity.assert_not_awaited()


@pytest.mark.asyncio
async def test_requeue_operational_embedding_job_requires_exact_inventory() -> None:
    org = _org()
    ctx = _ctx()
    event = Entity(
        id="event_capture-1",
        entity_type=EntityType.EVENT,
        name="Incident state",
        content="Incident INC001 is closed.",
        organization_id=str(org.id),
        metadata={"project_id": "project_shared"},
    )
    manifest = Entity(
        id="artifact_capture-1",
        entity_type=EntityType.ARTIFACT,
        name="Operational experience manifest",
        content="capture-1",
        organization_id=str(org.id),
        metadata={
            "project_id": "project_shared",
            "projection_kind": "manifest",
            "operational_projection_state": "embedding_pending",
            "expected_entity_ids": [event.id, "event_missing", "artifact_capture-1"],
        },
    )
    persisted = {event.id: event, manifest.id: manifest}
    relationship_manager = SimpleNamespace(get_for_entity=AsyncMock())
    runtime = SimpleNamespace(
        entity_manager=SimpleNamespace(
            get=AsyncMock(side_effect=lambda entity_id: persisted[entity_id]),
            get_many=AsyncMock(
                side_effect=lambda entity_ids: [
                    persisted[entity_id] for entity_id in entity_ids if entity_id in persisted
                ]
            ),
        ),
        relationship_manager=relationship_manager,
    )

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
            "sibyl.api.routes.entities._require_entity_read_access",
            AsyncMock(return_value={"project_shared"}),
        ),
        patch(
            "sibyl.jobs.queue.enqueue_entity_embedding_backfill",
            AsyncMock(),
        ) as enqueue_embeddings,
        pytest.raises(HTTPException, match="exact manifest inventory") as exc_info,
    ):
        await requeue_entity_background_jobs(
            request=EntityBackgroundJobsRequeueRequest(
                manifest_id=manifest.id,
                jobs=["embedding_backfill"],
            ),
            org=org,
            ctx=ctx,
            content_session="session",
        )

    assert exc_info.value.status_code == 409
    enqueue_embeddings.assert_not_awaited()
    relationship_manager.get_for_entity.assert_not_awaited()


@pytest.mark.asyncio
async def test_requeue_entity_background_jobs_hides_unreadable_entities() -> None:
    entity = Entity(
        id="session_private",
        entity_type=EntityType.SESSION,
        name="Private session",
        content="private memory",
        organization_id=str(_org().id),
        metadata={"memory_scope": "private"},
    )
    runtime = SimpleNamespace(entity_manager=SimpleNamespace(get=AsyncMock(return_value=entity)))

    with (
        patch(
            "sibyl.api.routes.entities.get_entity_graph_runtime",
            AsyncMock(return_value=runtime),
        ),
        patch(
            "sibyl.api.routes.entities._require_entity_read_access",
            AsyncMock(side_effect=HTTPException(status_code=404, detail="Entity not found")),
        ),
        patch(
            "sibyl.jobs.queue.enqueue_entity_embedding_backfill",
            AsyncMock(),
        ) as enqueue_embeddings,
        pytest.raises(HTTPException) as exc_info,
    ):
        await requeue_entity_background_jobs(
            request=EntityBackgroundJobsRequeueRequest(
                entity_ids=[entity.id],
                jobs=["embedding_backfill"],
            ),
            org=_org(),
            ctx=_ctx(),
            content_session="session",
        )

    assert exc_info.value.status_code == 404
    enqueue_embeddings.assert_not_awaited()


@pytest.mark.asyncio
async def test_requeue_entity_background_jobs_returns_not_found_for_deleted_entity() -> None:
    runtime = SimpleNamespace(
        entity_manager=SimpleNamespace(get=AsyncMock(side_effect=KeyError("session_deleted")))
    )

    with (
        patch(
            "sibyl.api.routes.entities.get_entity_graph_runtime",
            AsyncMock(return_value=runtime),
        ),
        pytest.raises(HTTPException, match="Entity not found: session_deleted") as exc_info,
    ):
        await requeue_entity_background_jobs(
            request=EntityBackgroundJobsRequeueRequest(
                entity_ids=["session_deleted"],
                jobs=["embedding_backfill"],
            ),
            org=_org(),
            ctx=_ctx(),
            content_session="session",
        )

    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_create_entities_bulk_enqueues_memory_projection() -> None:
    org = _org()
    ctx = _ctx()
    batch = EntityBulkCreateRequest(
        entities=[
            EntityCreate(
                name="Session one",
                content="I bought a Samsung TV for the den.",
                entity_type=EntityType.SESSION,
                skip_conflicts=True,
                metadata={"source": "import"},
            )
        ]
    )
    runtime = SimpleNamespace(
        entity_manager=SimpleNamespace(create_direct_bulk=AsyncMock(return_value=["session_one"])),
        relationship_manager=SimpleNamespace(create_bulk=AsyncMock(return_value=(0, 0))),
    )

    with (
        patch(
            "sibyl.api.routes.entities.get_entity_graph_runtime",
            AsyncMock(return_value=runtime),
        ),
        patch("sibyl.jobs.queue.enqueue_memory_projection", AsyncMock()) as enqueue_projection,
    ):
        await create_entities_bulk(
            batch=batch,
            org=org,
            ctx=ctx,
            content_session=None,
        )

    enqueue_projection.assert_awaited_once()
    payload, group_id = enqueue_projection.await_args.args
    assert group_id == str(org.id)
    assert payload[0]["content"] == "I bought a Samsung TV for the den."
    assert enqueue_projection.await_args.kwargs == {"created_source_ids": ["session_one"]}


@pytest.mark.asyncio
async def test_create_entities_bulk_returns_memory_extraction_jobs() -> None:
    org = _org()
    ctx = _ctx()
    batch = EntityBulkCreateRequest(
        entities=[
            EntityCreate(
                name="Session one",
                content="semantic memory content",
                entity_type=EntityType.SESSION,
                skip_conflicts=True,
                metadata={"source": "import"},
            )
        ]
    )
    runtime = SimpleNamespace(
        entity_manager=SimpleNamespace(create_direct_bulk=AsyncMock(return_value=["session_one"])),
        relationship_manager=SimpleNamespace(create_bulk=AsyncMock(return_value=(0, 0))),
    )
    enqueue_result = SimpleNamespace(
        status="queued",
        job_ids=("extract-memory-1",),
        queued_sources=1,
        skipped_sources=0,
        queue_depth=3,
        reason=None,
    )

    with (
        patch(
            "sibyl.api.routes.entities.get_entity_graph_runtime",
            AsyncMock(return_value=runtime),
        ),
        patch(
            "sibyl.jobs.memory_extraction.enqueue_memory_extraction_batches",
            AsyncMock(return_value=enqueue_result),
        ) as enqueue_extraction,
    ):
        response = await create_entities_bulk(
            batch=batch,
            org=org,
            ctx=ctx,
            content_session=None,
        )

    enqueue_extraction.assert_awaited_once()
    jobs = response.background_jobs["memory_extraction"]
    assert jobs["status"] == "queued"
    assert jobs["job_ids"] == ["extract-memory-1"]
    assert jobs["queued_sources"] == 1
    assert jobs["queue_depth"] == 3


@pytest.mark.asyncio
async def test_create_entities_bulk_reports_partial_memory_extraction() -> None:
    org = _org()
    ctx = _ctx()
    batch = EntityBulkCreateRequest(
        entities=[
            EntityCreate(
                name="Session one",
                content="semantic memory content",
                entity_type=EntityType.SESSION,
                skip_conflicts=True,
            )
        ]
    )
    runtime = SimpleNamespace(
        entity_manager=SimpleNamespace(create_direct_bulk=AsyncMock(return_value=["session_one"])),
        relationship_manager=SimpleNamespace(create_bulk=AsyncMock(return_value=(0, 0))),
    )
    enqueue_result = SimpleNamespace(
        status="partial",
        job_ids=("extract-memory-1",),
        queued_sources=1,
        skipped_sources=1,
        queue_depth=249,
        reason="queue_depth",
    )

    with (
        patch(
            "sibyl.api.routes.entities.get_entity_graph_runtime",
            AsyncMock(return_value=runtime),
        ),
        patch(
            "sibyl.jobs.memory_extraction.enqueue_memory_extraction_batches",
            AsyncMock(return_value=enqueue_result),
        ),
    ):
        response = await create_entities_bulk(
            batch=batch,
            org=org,
            ctx=ctx,
            content_session=None,
        )

    jobs = response.background_jobs["memory_extraction"]
    assert jobs["status"] == "partial"
    assert jobs["job_ids"] == ["extract-memory-1"]
    assert jobs["skipped_sources"] == 1
    assert jobs["reason"] == "queue_depth"


@pytest.mark.asyncio
async def test_create_entities_bulk_requires_explicit_conflict_skip() -> None:
    org = _org()
    ctx = _ctx()
    batch = EntityBulkCreateRequest(
        entities=[
            EntityCreate(
                name="Session one",
                content="semantic memory content",
                entity_type=EntityType.SESSION,
            )
        ]
    )
    runtime = SimpleNamespace(
        entity_manager=SimpleNamespace(create_direct_bulk=AsyncMock(return_value=[])),
        relationship_manager=SimpleNamespace(create_bulk=AsyncMock(return_value=(0, 0))),
    )

    with (
        patch(
            "sibyl.api.routes.entities.get_entity_graph_runtime",
            AsyncMock(return_value=runtime),
        ),
        pytest.raises(HTTPException) as exc,
    ):
        await create_entities_bulk(
            batch=batch,
            org=org,
            ctx=ctx,
            content_session=None,
        )

    assert exc.value.status_code == 400
    assert "skip_conflicts=true" in str(exc.value.detail)
    runtime.entity_manager.create_direct_bulk.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_entity_verifies_metadata_project_id_before_add() -> None:
    org = _org()
    ctx = _ctx()
    entity = EntityCreate(
        name="Scoped memory",
        content="Remember this only in a project the user can write.",
        entity_type=EntityType.DECISION,
        metadata={"project_id": "project_denied"},
    )
    add = AsyncMock()
    verify_access = AsyncMock(
        side_effect=ProjectAccessDeniedError(
            project_id="project_denied",
            required_role=ProjectRole.CONTRIBUTOR,
        )
    )

    with (
        patch("sibyl_core.tools.core.add", add),
        patch("sibyl.api.routes.entities.verify_entity_project_access", verify_access),
        pytest.raises(ProjectAccessDeniedError),
    ):
        await create_entity(
            request=_request(),
            entity=entity,
            org=org,
            ctx=ctx,
            content_session="session",
            sync=False,
        )

    verify_access.assert_awaited_once_with(
        "session",
        ctx,
        "project_denied",
        required_role=ProjectRole.CONTRIBUTOR,
        require_existing_project=True,
    )
    add.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_project_routes_through_runtime_project_record() -> None:
    org = _org()
    ctx = _ctx()
    existing = _project_entity(name="Old name", description="old")
    updated = _project_entity(name="New name", description="new")
    runtime = SimpleNamespace(
        entity_manager=SimpleNamespace(
            get=AsyncMock(return_value=existing),
            update=AsyncMock(return_value=updated),
        )
    )

    with (
        patch("sibyl.locks.entity_lock", _locked_entity),
        patch(
            "sibyl.api.routes.entities.get_entity_graph_runtime", AsyncMock(return_value=runtime)
        ),
        patch(
            "sibyl.api.routes.entities.verify_entity_project_access", AsyncMock()
        ) as verify_access,
        patch(
            "sibyl.api.routes.entities.list_accessible_project_graph_ids",
            AsyncMock(return_value={"project_new"}),
        ),
        patch("sibyl.api.routes.entities.broadcast_event", AsyncMock()),
        patch("sibyl.api.routes.entities.update_project_record", AsyncMock()) as update_project,
        patch("sibyl.api.routes.entities.log_audit_event", AsyncMock()) as audit_log,
    ):
        response = await update_entity(
            entity_id="project_new",
            update=EntityUpdate(name="New name", description="new"),
            request=_request(),
            org=org,
            ctx=ctx,
            content_session=None,
        )

    assert response.name == "New name"
    verify_access.assert_awaited_once_with(
        None,
        ctx,
        "project_new",
        required_role=ProjectRole.CONTRIBUTOR,
        require_existing_project=True,
    )
    update_project.assert_awaited_once_with(
        organization_id=org.id,
        graph_project_id="project_new",
        name="New name",
        description="new",
    )
    audit_log.assert_awaited_once()


@pytest.mark.asyncio
async def test_delete_project_routes_through_runtime_project_record() -> None:
    org = _org()
    ctx = _ctx()
    existing = _project_entity(name="Delete me", description="gone")
    runtime = SimpleNamespace(
        entity_manager=SimpleNamespace(
            get=AsyncMock(return_value=existing),
            delete=AsyncMock(return_value=True),
        )
    )

    with (
        patch("sibyl.locks.entity_lock", _locked_entity),
        patch(
            "sibyl.api.routes.entities.get_entity_graph_runtime", AsyncMock(return_value=runtime)
        ),
        patch(
            "sibyl.api.routes.entities.verify_entity_project_access", AsyncMock()
        ) as verify_access,
        patch(
            "sibyl.api.routes.entities.list_accessible_project_graph_ids",
            AsyncMock(return_value={"project_new"}),
        ),
        patch("sibyl.api.routes.entities.broadcast_event", AsyncMock()),
        patch("sibyl.api.routes.entities.delete_project_record", AsyncMock()) as delete_project,
        patch("sibyl.api.routes.entities.log_audit_event", AsyncMock()) as audit_log,
    ):
        await delete_entity(
            entity_id="project_new",
            request=_request(),
            org=org,
            ctx=ctx,
            content_session=None,
        )

    verify_access.assert_awaited_once_with(
        None,
        ctx,
        "project_new",
        required_role=ProjectRole.MAINTAINER,
        require_existing_project=True,
    )
    delete_project.assert_awaited_once_with(
        organization_id=org.id,
        graph_project_id="project_new",
    )
    audit_log.assert_awaited_once()


@pytest.mark.asyncio
async def test_create_entity_binds_scoped_metadata_to_authenticated_principal() -> None:
    """Scoped graph rows bind to the caller's identity, not the payload's claim.

    The CLI captures over HTTP and cannot know its own principal, so the route
    supplies it. Trusting the body instead would let a client plant a row that
    reads as another user's private memory.
    """
    org = _org()
    ctx = _ctx()
    entity = EntityCreate(
        name="Private capture",
        content="Capture this.",
        entity_type=EntityType.DECISION,
        metadata={
            "capture_mode": "remember",
            "memory_scope": "private",
            "principal_id": "victim",
        },
    )
    add_result = SimpleNamespace(success=True, id="decision_1", message="ok")
    add_mock = AsyncMock(return_value=add_result)

    with (
        patch("sibyl.api.routes.entities.verify_entity_project_access", AsyncMock()),
        patch("sibyl_core.tools.core.add", add_mock),
        patch("sibyl.api.routes.entities.get_entity_graph_runtime", AsyncMock()),
        patch("sibyl.api.routes.entities.broadcast_event", AsyncMock()),
        patch("sibyl.api.routes.entities.log_audit_event", AsyncMock()),
        patch("sibyl.api.routes.entities._archive_raw_capture", AsyncMock()),
    ):
        await create_entity(
            request=_request(),
            entity=entity,
            org=org,
            ctx=ctx,
            content_session=None,
            sync=False,
        )

    graph_metadata = add_mock.await_args.kwargs["metadata"]
    assert graph_metadata["memory_scope"] == "private"
    assert graph_metadata["principal_id"] == str(ctx.user.id)


@pytest.mark.asyncio
async def test_create_entity_drops_scope_principal_without_authenticated_user() -> None:
    org = _org()
    ctx = SimpleNamespace(user=None)
    entity = EntityCreate(
        name="Private capture",
        content="Capture this.",
        entity_type=EntityType.DECISION,
        metadata={
            "memory_scope": "private",
            "principal_id": "victim",
            "scope_key": "victim",
        },
    )
    add_result = SimpleNamespace(success=True, id="decision_1", message="ok")
    add_mock = AsyncMock(return_value=add_result)

    with (
        patch("sibyl.api.routes.entities.verify_entity_project_access", AsyncMock()),
        patch("sibyl_core.tools.core.add", add_mock),
        patch("sibyl.api.routes.entities.get_entity_graph_runtime", AsyncMock()),
        patch("sibyl.api.routes.entities.broadcast_event", AsyncMock()),
        patch("sibyl.api.routes.entities.log_audit_event", AsyncMock()),
        patch("sibyl.api.routes.entities._archive_raw_capture", AsyncMock()),
    ):
        await create_entity(
            request=_request(),
            entity=entity,
            org=org,
            ctx=ctx,
            content_session=None,
            sync=False,
        )

    # Retrieval resolves a private row's owner as principal_id or scope_key, so
    # an unauthenticated payload must not be able to supply either one.
    graph_metadata = add_mock.await_args.kwargs["metadata"]
    assert "principal_id" not in graph_metadata
    assert "scope_key" not in graph_metadata


@pytest.mark.asyncio
async def test_create_entity_sanitizes_raw_capture_scope_metadata() -> None:
    org = _org()
    ctx = _ctx()
    entity = EntityCreate(
        name="Scoped capture",
        content="Capture this.",
        entity_type=EntityType.DECISION,
        metadata={
            "capture_mode": "remember",
            "capture_surface": "dashboard",
            "memory_scope": "project",
            "scope_key": "project_forged",
            "principal_id": "victim",
            "project_id": "project_forged",
            "review_state": "accepted",
            "source_id": "source-forged",
            "raw_source_id": "raw-source-forged",
            "safe": "kept",
        },
    )
    add_result = SimpleNamespace(success=True, id="decision_1", message="ok")

    with (
        patch("sibyl.api.routes.entities.verify_entity_project_access", AsyncMock()),
        patch("sibyl_core.tools.core.add", AsyncMock(return_value=add_result)),
        patch("sibyl.api.routes.entities.get_entity_graph_runtime", AsyncMock()),
        patch("sibyl.api.routes.entities.broadcast_event", AsyncMock()),
        patch("sibyl.api.routes.entities.log_audit_event", AsyncMock()),
        patch("sibyl.api.routes.entities._archive_raw_capture", AsyncMock()) as archive_capture,
    ):
        await create_entity(
            request=_request(),
            entity=entity,
            org=org,
            ctx=ctx,
            content_session=None,
            sync=False,
        )

    sent_metadata = archive_capture.await_args.kwargs["metadata"]
    assert sent_metadata["capture_mode"] == "remember"
    assert sent_metadata["capture_surface"] == "dashboard"
    assert sent_metadata["safe"] == "kept"
    assert "memory_scope" not in sent_metadata
    assert "scope_key" not in sent_metadata
    assert "principal_id" not in sent_metadata
    assert "project_id" not in sent_metadata
    assert "review_state" not in sent_metadata
    assert "source_id" not in sent_metadata
    assert "raw_source_id" not in sent_metadata


@pytest.mark.asyncio
async def test_create_entity_drops_project_scope_key_without_a_verified_project() -> None:
    """A project scope key is only honored when the request proved access to it.

    Nothing verifies a bare scope_key, because the contributor check keys on
    project_id. Left intact it would address the row at a project the caller
    never proved membership on, and retrieval serves project rows by scope_key.
    """
    org = _org()
    ctx = _ctx()
    entity = EntityCreate(
        name="Planted decision",
        content="Reads as another project's memory.",
        entity_type=EntityType.DECISION,
        metadata={"memory_scope": "project", "scope_key": "project_victim"},
    )
    add_result = SimpleNamespace(success=True, id="decision_1", message="ok")
    add_mock = AsyncMock(return_value=add_result)
    verify_access = AsyncMock()

    with (
        patch("sibyl.api.routes.entities.verify_entity_project_access", verify_access),
        patch("sibyl_core.tools.core.add", add_mock),
        patch("sibyl.api.routes.entities.get_entity_graph_runtime", AsyncMock()),
        patch("sibyl.api.routes.entities.broadcast_event", AsyncMock()),
        patch("sibyl.api.routes.entities.log_audit_event", AsyncMock()),
    ):
        await create_entity(
            request=_request(),
            entity=entity,
            org=org,
            ctx=ctx,
            content_session=None,
            sync=False,
        )

    verify_access.assert_not_awaited()
    graph_metadata = add_mock.await_args.kwargs["metadata"]
    assert graph_metadata["memory_scope"] == "project"
    assert "scope_key" not in graph_metadata


def test_bulk_create_binds_scoped_metadata_to_the_authenticated_principal() -> None:
    entity = EntityCreate(
        name="Planted episode",
        content="Reads as the victim's private memory.",
        entity_type=EntityType.EPISODE,
        skip_conflicts=True,
        metadata={"memory_scope": "private", "principal_id": "victim", "scope_key": "victim"},
    )

    built = _entity_from_bulk_create(
        entity,
        group_id=str(_org().id),
        now=datetime.now(UTC),
        principal_id="attacker",
    )

    assert built.metadata["memory_scope"] == "private"
    assert built.metadata["principal_id"] == "attacker"
    assert "scope_key" not in built.metadata


def test_bulk_create_drops_a_scope_key_the_request_never_verified() -> None:
    entity = EntityCreate(
        name="Planted session",
        content="Reads as another project's memory.",
        entity_type=EntityType.SESSION,
        skip_conflicts=True,
        metadata={"memory_scope": "project", "scope_key": "project_victim"},
    )

    built = _entity_from_bulk_create(
        entity,
        group_id=str(_org().id),
        now=datetime.now(UTC),
        principal_id="attacker",
    )

    assert built.metadata["memory_scope"] == "project"
    assert "scope_key" not in built.metadata


def test_bulk_create_scopes_a_project_row_to_the_verified_project() -> None:
    entity = EntityCreate(
        name="Project session",
        content="Belongs to a project the request verified.",
        entity_type=EntityType.SESSION,
        skip_conflicts=True,
        metadata={
            "memory_scope": "project",
            "project_id": "project_real",
            "scope_key": "project_victim",
        },
    )

    built = _entity_from_bulk_create(
        entity,
        group_id=str(_org().id),
        now=datetime.now(UTC),
        principal_id="author",
    )

    assert built.metadata["scope_key"] == "project_real"


@pytest.mark.asyncio
async def test_create_entities_bulk_stamps_the_caller_onto_scoped_rows() -> None:
    org = _org()
    ctx = _ctx()
    batch = EntityBulkCreateRequest(
        entities=[
            EntityCreate(
                name="Imported episode",
                content="private import",
                entity_type=EntityType.EPISODE,
                skip_conflicts=True,
                metadata={"memory_scope": "private", "principal_id": "victim"},
            )
        ]
    )
    runtime = SimpleNamespace(
        entity_manager=SimpleNamespace(create_direct_bulk=AsyncMock(return_value=["episode_one"])),
        relationship_manager=SimpleNamespace(create_bulk=AsyncMock(return_value=(0, 0))),
    )

    with patch(
        "sibyl.api.routes.entities.get_entity_graph_runtime",
        AsyncMock(return_value=runtime),
    ):
        response = await create_entities_bulk(
            batch=batch,
            org=org,
            ctx=ctx,
            content_session=None,
        )

    persisted = runtime.entity_manager.create_direct_bulk.await_args.args[0][0]
    assert persisted.metadata["principal_id"] == str(ctx.user.id)
    assert response.entities[0].metadata["principal_id"] == str(ctx.user.id)


def _private_memory_entity(*, owner: str) -> SimpleNamespace:
    return SimpleNamespace(
        id="decision_private",
        entity_type=EntityType.DECISION,
        name="Private decision",
        description="secret rationale",
        content="secret rationale",
        category=None,
        languages=[],
        tags=[],
        metadata={"memory_scope": "private", "principal_id": owner},
        source_file=None,
        created_at=None,
        updated_at=None,
    )


@pytest.mark.asyncio
async def test_update_entity_refuses_a_private_row_owned_by_another_principal() -> None:
    """A co-member with contributor rights is still not the owner.

    The response echoes the stored content, so passing only the project check
    would both reassign and disclose a private memory.
    """
    org = _org()
    ctx = _ctx()
    existing = _private_memory_entity(owner="victim")
    update_mock = AsyncMock()
    runtime = SimpleNamespace(
        entity_manager=SimpleNamespace(
            get=AsyncMock(return_value=existing),
            update=update_mock,
        )
    )

    with (
        patch("sibyl.locks.entity_lock", _locked_entity),
        patch(
            "sibyl.api.routes.entities.get_entity_graph_runtime", AsyncMock(return_value=runtime)
        ),
        patch("sibyl.api.routes.entities.verify_entity_project_access", AsyncMock()),
        patch(
            "sibyl.api.routes.entities.list_accessible_project_graph_ids",
            AsyncMock(return_value=set()),
        ),
        patch("sibyl.api.routes.entities.broadcast_event", AsyncMock()),
        pytest.raises(HTTPException) as excinfo,
    ):
        await update_entity(
            entity_id="decision_private",
            update=EntityUpdate(metadata={"principal_id": str(ctx.user.id)}),
            request=_request(),
            org=org,
            ctx=ctx,
            content_session=None,
        )

    assert excinfo.value.status_code == 404
    update_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_entity_keeps_owner_metadata_out_of_the_caller_merge() -> None:
    org = _org()
    ctx = _ctx()
    owner = str(ctx.user.id)
    existing = _private_memory_entity(owner=owner)
    updated = _private_memory_entity(owner=owner)
    runtime = SimpleNamespace(
        entity_manager=SimpleNamespace(
            get=AsyncMock(return_value=existing),
            update=AsyncMock(return_value=updated),
        )
    )

    with (
        patch("sibyl.locks.entity_lock", _locked_entity),
        patch(
            "sibyl.api.routes.entities.get_entity_graph_runtime", AsyncMock(return_value=runtime)
        ),
        patch("sibyl.api.routes.entities.verify_entity_project_access", AsyncMock()),
        patch(
            "sibyl.api.routes.entities.list_accessible_project_graph_ids",
            AsyncMock(return_value=set()),
        ),
        patch("sibyl.api.routes.entities.broadcast_event", AsyncMock()),
    ):
        await update_entity(
            entity_id="decision_private",
            update=EntityUpdate(
                metadata={
                    "principal_id": "attacker",
                    "scope_key": "project_attacker",
                    "memory_scope": "organization",
                    "note": "kept",
                }
            ),
            request=_request(),
            org=org,
            ctx=ctx,
            content_session=None,
        )

    merged = runtime.entity_manager.update.await_args.args[1]["metadata"]
    assert merged["principal_id"] == owner
    assert merged["memory_scope"] == "private"
    assert "scope_key" not in merged
    assert merged["note"] == "kept"


@pytest.mark.asyncio
async def test_update_entity_refuses_a_project_row_keyed_outside_accessible_projects() -> None:
    org = _org()
    ctx = _ctx()
    existing = SimpleNamespace(
        id="decision_scoped",
        entity_type=EntityType.DECISION,
        name="Project decision",
        description="scoped rationale",
        content="scoped rationale",
        category=None,
        languages=[],
        tags=[],
        metadata={"memory_scope": "project", "scope_key": "project_victim"},
        source_file=None,
        created_at=None,
        updated_at=None,
    )
    update_mock = AsyncMock()
    runtime = SimpleNamespace(
        entity_manager=SimpleNamespace(
            get=AsyncMock(return_value=existing),
            update=update_mock,
        )
    )

    with (
        patch("sibyl.locks.entity_lock", _locked_entity),
        patch(
            "sibyl.api.routes.entities.get_entity_graph_runtime", AsyncMock(return_value=runtime)
        ),
        patch("sibyl.api.routes.entities.verify_entity_project_access", AsyncMock()),
        patch(
            "sibyl.api.routes.entities.list_accessible_project_graph_ids",
            AsyncMock(return_value={"project_mine"}),
        ),
        patch("sibyl.api.routes.entities.broadcast_event", AsyncMock()),
        pytest.raises(HTTPException) as excinfo,
    ):
        await update_entity(
            entity_id="decision_scoped",
            update=EntityUpdate(name="Renamed"),
            request=_request(),
            org=org,
            ctx=ctx,
            content_session=None,
        )

    assert excinfo.value.status_code == 404
    update_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_delete_entity_refuses_a_private_row_owned_by_another_principal() -> None:
    org = _org()
    ctx = _ctx()
    existing = _private_memory_entity(owner="victim")
    delete_mock = AsyncMock(return_value=True)
    runtime = SimpleNamespace(
        entity_manager=SimpleNamespace(
            get=AsyncMock(return_value=existing),
            delete=delete_mock,
        )
    )

    with (
        patch("sibyl.locks.entity_lock", _locked_entity),
        patch(
            "sibyl.api.routes.entities.get_entity_graph_runtime", AsyncMock(return_value=runtime)
        ),
        patch("sibyl.api.routes.entities.verify_entity_project_access", AsyncMock()),
        patch(
            "sibyl.api.routes.entities.list_accessible_project_graph_ids",
            AsyncMock(return_value=set()),
        ),
        patch("sibyl.api.routes.entities.broadcast_event", AsyncMock()),
        pytest.raises(HTTPException) as excinfo,
    ):
        await delete_entity(
            entity_id="decision_private",
            request=_request(),
            org=org,
            ctx=ctx,
            content_session=None,
        )

    assert excinfo.value.status_code == 404
    delete_mock.assert_not_awaited()


def test_project_screen_gates_a_note_carrying_only_a_project() -> None:
    """The API half of the note-gating pair asserted in sibyl-core.

    A note carries its audience as a project id rather than a memory scope, so
    the project screen is the only thing standing between it and an outsider.
    """
    from types import SimpleNamespace

    from sibyl.api.routes.entities import _entity_visible_to_projects

    note = SimpleNamespace(
        entity_type=SimpleNamespace(value="note"),
        metadata={"project_id": "proj-secret"},
    )

    assert not _entity_visible_to_projects(note, set())
    assert _entity_visible_to_projects(note, {"proj-secret"})
