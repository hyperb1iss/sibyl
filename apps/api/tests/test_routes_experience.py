from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest

from sibyl.api.routes.experience import capture_operational_experience
from sibyl.api.schemas import OperationalExperienceCaptureRequest
from sibyl_core.auth import ProjectRole
from sibyl_core.models.entities import EntityType
from sibyl_core.models.experience import (
    OperationalEvidencePart,
    OperationalExperience,
    OperationalObservation,
)
from sibyl_core.projection import project_operational_experience


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
    runtime = SimpleNamespace(entity_manager=object(), relationship_manager=object())

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
    queued_entities = enqueue.await_args.args[0]
    assert queued_entities
    assert all(item["entity_type"] != EntityType.ARTIFACT for item in queued_entities)
    assert response.written_entities == len(projection.entities)
    assert response.deleted_entities == 1
    assert response.entity_ids == list(projection.manifest.entity_ids)
    assert response.background_jobs["embedding_backfill"]["job_ids"] == ["embed-1"]


@pytest.mark.asyncio
async def test_capture_can_embed_synchronously_without_background_job() -> None:
    org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
    ctx = SimpleNamespace(user_id="user-1")
    request = _request(project_id=None).model_copy(update={"defer_embeddings": False})
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
            AsyncMock(
                return_value=SimpleNamespace(
                    entity_manager=object(),
                    relationship_manager=object(),
                )
            ),
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

    verify_project.assert_not_awaited()
    assert persist.await_args.kwargs["generate_embeddings"] is True
    enqueue.assert_not_awaited()
    assert response.background_jobs == {}
