from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest
from fastapi import HTTPException
from fastapi.routing import APIRoute

from sibyl.api.routes import experience as experience_routes
from sibyl.api.routes.experience import capture_operational_experience
from sibyl.api.schemas import OperationalExperienceCaptureRequest
from sibyl_core.auth import OrganizationRole, ProjectRole
from sibyl_core.models.entities import EntityType
from sibyl_core.models.experience import (
    OperationalEvidencePart,
    OperationalExperience,
    OperationalObservation,
)
from sibyl_core.projection import project_operational_experience


@asynccontextmanager
async def _acquired_lock(*_args: object, **_kwargs: object):
    yield "lock-token"


@pytest.fixture(autouse=True)
def _lock_operational_source(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(experience_routes, "entity_lock", _acquired_lock)


def _runtime(existing_manifest: object | None = None) -> SimpleNamespace:
    get = (
        AsyncMock(return_value=existing_manifest)
        if existing_manifest is not None
        else AsyncMock(side_effect=KeyError("missing"))
    )
    return SimpleNamespace(
        entity_manager=SimpleNamespace(
            get=get,
            create_direct_bulk=AsyncMock(
                side_effect=lambda entities, **_: [entity.id for entity in entities]
            ),
        ),
        relationship_manager=object(),
    )


def _request(*, project_id: str | None = "project-1") -> OperationalExperienceCaptureRequest:
    return OperationalExperienceCaptureRequest(
        experience=OperationalExperience(
            source_id="capture-1",
            goal="Close the incident",
            outcome="success",
            project_id=project_id,
            observations=(
                OperationalObservation(
                    id="state-0",
                    ordinal=0,
                    evidence=(
                        OperationalEvidencePart(
                            id="tree-0",
                            content="Incident INC001 is open",
                        ),
                    ),
                ),
                OperationalObservation(
                    id="state-1",
                    ordinal=1,
                    action="select('state', 'Closed')",
                    evidence=(
                        OperationalEvidencePart(
                            id="tree-0",
                            content="Incident INC001 is closed",
                        ),
                    ),
                ),
            ),
        )
    )


@pytest.mark.asyncio
async def test_capture_persists_authorized_experience_and_queues_embeddings() -> None:
    org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
    ctx = SimpleNamespace(user_id="user-1")
    projection = project_operational_experience(
        _request().experience,
        organization_id=str(org.id),
        created_by=ctx.user_id,
    )
    write_result = SimpleNamespace(
        projection=projection,
        written_entity_ids=projection.manifest.entity_ids,
        written_relationship_ids=projection.manifest.relationship_ids,
        deleted_entity_ids=("session-old",),
        deleted_relationship_ids=("rel-old",),
    )
    runtime = _runtime()

    with (
        patch(
            "sibyl.api.routes.experience.verify_entity_project_access",
            AsyncMock(),
        ) as verify_project,
        patch(
            "sibyl.api.routes.experience.get_experience_graph_runtime",
            AsyncMock(return_value=runtime),
        ),
        patch(
            "sibyl.api.routes.experience.persist_operational_experience",
            AsyncMock(return_value=write_result),
        ) as persist,
        patch(
            "sibyl.jobs.queue.enqueue_entity_embedding_backfill",
            AsyncMock(return_value="embed-1"),
        ) as enqueue,
    ):
        response = await capture_operational_experience(
            payload=_request(),
            org=org,
            ctx=ctx,
        )

    verify_project.assert_awaited_once_with(
        None,
        ctx,
        "project-1",
        required_role=ProjectRole.CONTRIBUTOR,
        require_existing_project=True,
    )
    assert persist.await_args.kwargs["organization_id"] == str(org.id)
    assert persist.await_args.kwargs["created_by"] == "user-1"
    assert persist.await_args.kwargs["generate_embeddings"] is False
    assert persist.await_args.kwargs["commit_manifest"] is False
    queued_entities = enqueue.await_args.args[0]
    assert queued_entities
    assert all(item["entity_type"] != EntityType.ARTIFACT for item in queued_entities)
    assert response.written_entities == len(projection.entities)
    assert response.deleted_entities == 1
    assert response.entity_ids == list(projection.manifest.entity_ids)
    assert response.relationship_ids == list(projection.manifest.relationship_ids)
    assert response.background_jobs["embedding_backfill"]["job_ids"] == ["embed-1"]
    manifest_write = runtime.entity_manager.create_direct_bulk.await_args.args[0]
    assert manifest_write[0].metadata["operational_projection_state"] == "complete"


@pytest.mark.asyncio
async def test_capture_can_embed_synchronously_without_background_job() -> None:
    org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
    ctx = SimpleNamespace(user_id="user-1")
    request = _request().model_copy(update={"defer_embeddings": False})
    projection = project_operational_experience(request.experience)
    write_result = SimpleNamespace(
        projection=projection,
        written_entity_ids=projection.manifest.entity_ids,
        written_relationship_ids=projection.manifest.relationship_ids,
        deleted_entity_ids=(),
        deleted_relationship_ids=(),
    )

    with (
        patch(
            "sibyl.api.routes.experience.verify_entity_project_access",
            AsyncMock(),
        ) as verify_project,
        patch(
            "sibyl.api.routes.experience.get_experience_graph_runtime",
            AsyncMock(return_value=_runtime()),
        ),
        patch(
            "sibyl.api.routes.experience.persist_operational_experience",
            AsyncMock(return_value=write_result),
        ) as persist,
        patch(
            "sibyl.jobs.queue.enqueue_entity_embedding_backfill",
            AsyncMock(),
        ) as enqueue,
    ):
        response = await capture_operational_experience(
            payload=request,
            org=org,
            ctx=ctx,
        )

    verify_project.assert_awaited_once()
    assert persist.await_args.kwargs["generate_embeddings"] is True
    enqueue.assert_not_awaited()
    assert response.background_jobs == {}


@pytest.mark.asyncio
async def test_capture_unchanged_replay_does_not_requeue_embeddings() -> None:
    org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
    ctx = SimpleNamespace(user_id="user-1")
    projection = project_operational_experience(_request().experience)
    write_result = SimpleNamespace(
        projection=projection,
        written_entity_ids=(),
        written_relationship_ids=(),
        deleted_entity_ids=(),
        deleted_relationship_ids=(),
    )

    with (
        patch(
            "sibyl.api.routes.experience.verify_entity_project_access",
            AsyncMock(),
        ),
        patch(
            "sibyl.api.routes.experience.get_experience_graph_runtime",
            AsyncMock(return_value=_runtime()),
        ),
        patch(
            "sibyl.api.routes.experience.persist_operational_experience",
            AsyncMock(return_value=write_result),
        ),
        patch(
            "sibyl.jobs.queue.enqueue_entity_embedding_backfill",
            AsyncMock(),
        ) as enqueue,
    ):
        response = await capture_operational_experience(
            payload=_request(),
            org=org,
            ctx=ctx,
        )

    enqueue.assert_not_awaited()
    assert response.written_entities == 0
    assert response.entity_ids == list(projection.manifest.entity_ids)
    assert response.background_jobs == {}


@pytest.mark.asyncio
async def test_capture_requires_project_scope() -> None:
    with pytest.raises(HTTPException) as exc_info:
        await capture_operational_experience(
            payload=_request(project_id=None),
            org=SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111")),
            ctx=SimpleNamespace(user_id="user-1"),
        )

    assert exc_info.value.status_code == 422


@pytest.mark.asyncio
async def test_capture_requires_maintainer_to_replace_another_authors_source() -> None:
    org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
    ctx = SimpleNamespace(user_id="user-2")
    existing = next(
        entity
        for entity in project_operational_experience(
            _request().experience,
            created_by="user-1",
        ).entities
        if entity.entity_type is EntityType.ARTIFACT
    )
    write_result = SimpleNamespace(
        projection=project_operational_experience(_request().experience),
        written_entity_ids=(),
        written_relationship_ids=(),
        deleted_entity_ids=(),
        deleted_relationship_ids=(),
    )

    with (
        patch(
            "sibyl.api.routes.experience.verify_entity_project_access",
            AsyncMock(),
        ) as verify_project,
        patch(
            "sibyl.api.routes.experience.get_experience_graph_runtime",
            AsyncMock(return_value=_runtime(existing)),
        ),
        patch(
            "sibyl.api.routes.experience.persist_operational_experience",
            AsyncMock(return_value=write_result),
        ),
    ):
        await capture_operational_experience(payload=_request(), org=org, ctx=ctx)

    assert verify_project.await_count == 2
    assert verify_project.await_args_list[1].kwargs["required_role"] is ProjectRole.MAINTAINER


@pytest.mark.asyncio
async def test_capture_rejects_rebinding_source_to_another_project() -> None:
    org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
    ctx = SimpleNamespace(user_id="user-1")
    existing = next(
        entity
        for entity in project_operational_experience(
            _request(project_id="project-old").experience,
            created_by="user-1",
        ).entities
        if entity.entity_type is EntityType.ARTIFACT
    )

    with (
        patch(
            "sibyl.api.routes.experience.verify_entity_project_access",
            AsyncMock(),
        ),
        patch(
            "sibyl.api.routes.experience.get_experience_graph_runtime",
            AsyncMock(return_value=_runtime(existing)),
        ),
        patch(
            "sibyl.api.routes.experience.persist_operational_experience",
            AsyncMock(),
        ) as persist,
        pytest.raises(HTTPException) as exc_info,
    ):
        await capture_operational_experience(payload=_request(), org=org, ctx=ctx)

    assert exc_info.value.status_code == 409
    persist.assert_not_awaited()


@pytest.mark.asyncio
async def test_capture_reports_degraded_embedding_enqueue() -> None:
    org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
    ctx = SimpleNamespace(user_id="user-1")
    projection = project_operational_experience(_request().experience)
    write_result = SimpleNamespace(
        projection=projection,
        written_entity_ids=projection.manifest.entity_ids,
        written_relationship_ids=projection.manifest.relationship_ids,
        deleted_entity_ids=(),
        deleted_relationship_ids=(),
    )

    runtime = _runtime()
    with (
        patch(
            "sibyl.api.routes.experience.verify_entity_project_access",
            AsyncMock(),
        ),
        patch(
            "sibyl.api.routes.experience.get_experience_graph_runtime",
            AsyncMock(return_value=runtime),
        ),
        patch(
            "sibyl.api.routes.experience.persist_operational_experience",
            AsyncMock(return_value=write_result),
        ),
        patch(
            "sibyl.jobs.queue.enqueue_entity_embedding_backfill",
            AsyncMock(side_effect=RuntimeError("queue unavailable")),
        ),
    ):
        response = await capture_operational_experience(
            payload=_request(),
            org=org,
            ctx=ctx,
        )

    assert response.background_jobs["embedding_backfill"]["status"] == "degraded"
    assert response.background_jobs["embedding_backfill"]["error"] == "enqueue_failed"
    runtime.entity_manager.create_direct_bulk.assert_not_awaited()


@pytest.mark.asyncio
async def test_capture_reports_degraded_manifest_commit_after_enqueue() -> None:
    org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
    ctx = SimpleNamespace(user_id="user-1")
    projection = project_operational_experience(_request().experience)
    write_result = SimpleNamespace(
        projection=projection,
        written_entity_ids=projection.manifest.entity_ids,
        written_relationship_ids=projection.manifest.relationship_ids,
        deleted_entity_ids=(),
        deleted_relationship_ids=(),
    )
    runtime = _runtime()
    runtime.entity_manager.create_direct_bulk.side_effect = None
    runtime.entity_manager.create_direct_bulk.return_value = []

    with (
        patch(
            "sibyl.api.routes.experience.verify_entity_project_access",
            AsyncMock(),
        ),
        patch(
            "sibyl.api.routes.experience.get_experience_graph_runtime",
            AsyncMock(return_value=runtime),
        ),
        patch(
            "sibyl.api.routes.experience.persist_operational_experience",
            AsyncMock(return_value=write_result),
        ),
        patch(
            "sibyl.jobs.queue.enqueue_entity_embedding_backfill",
            AsyncMock(return_value="embed-1"),
        ),
    ):
        response = await capture_operational_experience(
            payload=_request(),
            org=org,
            ctx=ctx,
        )

    job = response.background_jobs["embedding_backfill"]
    assert job["status"] == "degraded"
    assert job["error"] == "manifest_commit_failed"
    assert job["job_ids"] == ["embed-1"]
    assert job["queued_entities"] > 0


@pytest.mark.asyncio
async def test_experience_router_rejects_viewer_role() -> None:
    route = next(
        route
        for route in experience_routes.router.routes
        if isinstance(route, APIRoute) and route.path.endswith("/experience")
    )
    dependency = route.dependencies[0].dependency

    with pytest.raises(HTTPException) as exc_info:
        await dependency(OrganizationRole.VIEWER)

    assert exc_info.value.status_code == 403
    await dependency(OrganizationRole.MEMBER)
