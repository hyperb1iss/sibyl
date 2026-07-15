"""Operational experience capture endpoints."""

from typing import Any

import structlog
from fastapi import APIRouter, Depends

from sibyl.api.schemas import (
    OperationalExperienceCaptureRequest,
    OperationalExperienceCaptureResponse,
)
from sibyl.auth.authorization import verify_entity_project_access
from sibyl.auth.context import AuthContext
from sibyl.auth.dependencies import get_auth_context, get_current_organization, require_org_role
from sibyl_core.auth import AuthOrganization, OrganizationRole, ProjectRole
from sibyl_core.models.entities import EntityType
from sibyl_core.projection import persist_operational_experience

log = structlog.get_logger()
_WRITE_ROLES = (
    OrganizationRole.OWNER,
    OrganizationRole.ADMIN,
    OrganizationRole.MEMBER,
)

router = APIRouter(
    prefix="/memory",
    tags=["memory"],
    dependencies=[Depends(require_org_role(*_WRITE_ROLES))],
)


async def get_experience_graph_runtime(group_id: str):
    from sibyl.persistence.graph_runtime import get_entity_graph_runtime

    return await get_entity_graph_runtime(group_id)


@router.post(
    "/experience",
    response_model=OperationalExperienceCaptureResponse,
    status_code=201,
)
async def capture_operational_experience(
    payload: OperationalExperienceCaptureRequest,
    org: AuthOrganization = Depends(get_current_organization),
    ctx: AuthContext = Depends(get_auth_context),
) -> OperationalExperienceCaptureResponse:
    """Persist source evidence and its deterministic typed projections."""
    experience = payload.experience
    if experience.project_id:
        await verify_entity_project_access(
            None,
            ctx,
            experience.project_id,
            required_role=ProjectRole.CONTRIBUTOR,
            require_existing_project=True,
        )

    group_id = str(org.id)
    runtime = await get_experience_graph_runtime(group_id)
    result = await persist_operational_experience(
        entity_manager=runtime.entity_manager,
        relationship_manager=runtime.relationship_manager,
        experience=experience,
        organization_id=group_id,
        created_by=ctx.user_id,
        generate_embeddings=not payload.defer_embeddings,
    )

    background_jobs: dict[str, Any] = {}
    if payload.defer_embeddings:
        embeddable = [
            entity
            for entity in result.projection.entities
            if entity.id != result.projection.manifest.manifest_entity_id
            and entity.entity_type is not EntityType.ARTIFACT
        ]
        try:
            from sibyl.jobs.queue import enqueue_entity_embedding_backfill

            job_id = await enqueue_entity_embedding_backfill(
                [entity.model_dump(mode="json") for entity in embeddable],
                group_id,
            )
            background_jobs["embedding_backfill"] = {
                "status": "queued",
                "job_ids": [job_id],
                "queued_entities": len(embeddable),
                "queued_relationships": 0,
            }
        except Exception as exc:
            log.warning(
                "operational_experience_embedding_backfill_enqueue_failed",
                source_id=experience.source_id,
                entities=len(embeddable),
                error=str(exc),
            )

    return OperationalExperienceCaptureResponse(
        source_id=experience.source_id,
        manifest_id=result.projection.manifest.manifest_entity_id,
        content_hash=result.projection.manifest.content_hash,
        written_entities=len(result.written_entity_ids),
        written_relationships=len(result.written_relationship_ids),
        deleted_entities=len(result.deleted_entity_ids),
        deleted_relationships=len(result.deleted_relationship_ids),
        background_jobs=background_jobs,
    )
