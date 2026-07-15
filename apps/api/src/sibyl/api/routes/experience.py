"""Operational experience capture endpoints."""

from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, status

from sibyl.api.schemas import (
    OperationalExperienceCaptureRequest,
    OperationalExperienceCaptureResponse,
)
from sibyl.auth.authorization import verify_entity_project_access
from sibyl.auth.context import AuthContext
from sibyl.auth.dependencies import get_auth_context, get_current_organization, require_org_role
from sibyl.locks import LockAcquisitionError, entity_lock
from sibyl_core.auth import AuthOrganization, OrganizationRole, ProjectRole
from sibyl_core.models.entities import EntityType
from sibyl_core.projection import (
    MANIFEST_STATE_COMPLETE,
    operational_experience_manifest_id,
    operational_experience_manifest_with_state,
    persist_operational_experience,
)

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
    if not experience.project_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Operational experience requires a project_id",
        )
    await verify_entity_project_access(
        None,
        ctx,
        experience.project_id,
        required_role=ProjectRole.CONTRIBUTOR,
        require_existing_project=True,
    )

    group_id = str(org.id)
    manifest_id = operational_experience_manifest_id(experience.source_id)
    try:
        async with entity_lock(group_id, manifest_id, blocking=True) as lock_token:
            if not lock_token:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Operational experience is being modified; retry the request",
                )
            runtime = await get_experience_graph_runtime(group_id)
            try:
                existing_manifest = await runtime.entity_manager.get(manifest_id)
            except KeyError:
                existing_manifest = None
            if existing_manifest is not None:
                existing_project = existing_manifest.metadata.get("project_id")
                if existing_project != experience.project_id:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail="Operational source_id is already bound to another project",
                    )
                if existing_manifest.created_by != ctx.user_id:
                    await verify_entity_project_access(
                        None,
                        ctx,
                        experience.project_id,
                        required_role=ProjectRole.MAINTAINER,
                        require_existing_project=True,
                    )
            try:
                result = await persist_operational_experience(
                    entity_manager=runtime.entity_manager,
                    relationship_manager=runtime.relationship_manager,
                    experience=experience,
                    organization_id=group_id,
                    created_by=ctx.user_id,
                    generate_embeddings=not payload.defer_embeddings,
                    commit_manifest=not payload.defer_embeddings,
                )
            except ValueError as exc:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail=str(exc),
                ) from exc

            background_jobs: dict[str, Any] = {}
            if payload.defer_embeddings and result.embedding_backfill_required:
                embeddable = [
                    entity
                    for entity in result.projection.entities
                    if entity.id != result.projection.manifest.manifest_entity_id
                    and entity.entity_type is not EntityType.ARTIFACT
                ]
                job_id: str | None = None
                try:
                    from sibyl.jobs.queue import enqueue_entity_embedding_backfill

                    job_id = await enqueue_entity_embedding_backfill(
                        [entity.model_dump(mode="json") for entity in embeddable],
                        group_id,
                        completion_manifest=operational_experience_manifest_with_state(
                            result.projection,
                            MANIFEST_STATE_COMPLETE,
                        ).model_dump(mode="json"),
                    )
                    background_jobs["embedding_backfill"] = {
                        "status": "queued",
                        "job_ids": [job_id],
                        "queued_entities": len(embeddable),
                        "queued_relationships": 0,
                    }
                except Exception as exc:
                    error_code = "enqueue_failed"
                    background_jobs["embedding_backfill"] = {
                        "status": "degraded",
                        "job_ids": [job_id] if job_id else [],
                        "queued_entities": len(embeddable) if job_id else 0,
                        "queued_relationships": 0,
                        "error": error_code,
                    }
                    log.warning(
                        "operational_experience_embedding_backfill_finalize_failed",
                        source_id=experience.source_id,
                        entities=len(embeddable),
                        error_code=error_code,
                        error=str(exc),
                    )
    except LockAcquisitionError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Operational experience is locked; retry the request",
        ) from exc

    return OperationalExperienceCaptureResponse(
        source_id=experience.source_id,
        manifest_id=result.projection.manifest.manifest_entity_id,
        content_hash=result.projection.manifest.content_hash,
        written_entities=len(result.written_entity_ids),
        written_relationships=len(result.written_relationship_ids),
        deleted_entities=len(result.deleted_entity_ids),
        deleted_relationships=len(result.deleted_relationship_ids),
        entity_ids=list(result.projection.manifest.entity_ids),
        relationship_ids=list(result.projection.manifest.relationship_ids),
        background_jobs=background_jobs,
    )
