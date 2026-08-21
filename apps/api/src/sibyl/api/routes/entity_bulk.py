"""Canonical bulk ownership for entity routes."""

import asyncio
from datetime import UTC, datetime
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException

from sibyl.api.routes import (
    entity_contracts as contracts,
    entity_policy as policy,
    entity_serialization as serialization,
)
from sibyl.api.schemas import (
    EntityBackgroundJobsRequeueRequest,
    EntityBackgroundJobsRequeueResponse,
    EntityBulkCreateRequest,
    EntityBulkCreateResponse,
)
from sibyl.auth.authorization import verify_entity_project_access
from sibyl.auth.context import AuthContext
from sibyl.auth.dependencies import (
    get_auth_context,
    get_current_organization,
    require_org_role,
)
from sibyl.persistence.content_runtime import (
    get_content_read_session_dependency,
)
from sibyl_core.auth import AuthOrganization, ProjectRole
from sibyl_core.models.entities import Entity, EntityType, Relationship
from sibyl_core.projection import (
    MANIFEST_STATE_COMPLETE,
    extract_projected_memory_entities,
)

log = structlog.get_logger()

router = APIRouter(
    prefix="/entities",
    tags=["entities"],
    dependencies=[Depends(require_org_role(*contracts.READ_ROLES))],
)


def _background_job_recovery(
    job: str,
    entity_ids: list[str],
) -> dict[str, Any]:
    return {
        "method": "POST",
        "endpoint": "/api/entities/bulk/requeue-background-jobs",
        "request": {"entity_ids": entity_ids, "jobs": [job]},
    }


def _memory_extraction_receipt(
    enqueue_result: Any,
    entity_ids: list[str],
) -> dict[str, Any]:
    receipt = {
        "status": enqueue_result.status,
        "job_ids": list(enqueue_result.job_ids),
        "queued_sources": enqueue_result.queued_sources,
        "skipped_sources": enqueue_result.skipped_sources,
        "queue_depth": enqueue_result.queue_depth,
        "reason": enqueue_result.reason,
    }
    if enqueue_result.status in {"partial", "backpressure"}:
        receipt["recovery"] = _background_job_recovery(
            "memory_extraction",
            entity_ids,
        )
    return receipt


async def _enqueue_bulk_memory_extraction(
    entities: list[Entity],
    created_ids: list[str],
    group_id: str,
) -> dict[str, Any]:
    try:
        from sibyl.jobs.memory_extraction import enqueue_memory_extraction_batches

        enqueue_result = await enqueue_memory_extraction_batches(
            [source.model_dump(mode="json") for source in entities],
            group_id,
            created_source_ids=created_ids,
        )
        log.info(
            "bulk_entity_memory_extraction_result",
            status=enqueue_result.status,
            jobs=len(enqueue_result.job_ids),
            queued_sources=enqueue_result.queued_sources,
            skipped_sources=enqueue_result.skipped_sources,
            reason=enqueue_result.reason,
        )
        return _memory_extraction_receipt(enqueue_result, created_ids)
    except Exception as exc:
        log.warning(
            "bulk_entity_memory_extraction_enqueue_failed",
            sources=len(entities),
            error=str(exc),
        )
        return {
            "status": "failed",
            "job_ids": [],
            "queued_sources": 0,
            "skipped_sources": len(entities),
            "reason": "enqueue_failed",
            "error_type": type(exc).__name__,
            "recovery": _background_job_recovery(
                "memory_extraction",
                created_ids,
            ),
        }


@router.post(
    "/bulk",
    response_model=EntityBulkCreateResponse,
    status_code=201,
    dependencies=[Depends(require_org_role(*contracts.WRITE_ROLES))],
)
async def create_entities_bulk(
    batch: EntityBulkCreateRequest,
    org: AuthOrganization = Depends(get_current_organization),
    ctx: AuthContext = Depends(get_auth_context),
    content_session: Any = Depends(get_content_read_session_dependency),
) -> EntityBulkCreateResponse:
    group_id = str(org.id)
    runtime = await policy.get_entity_graph_runtime(group_id)
    verified_project_ids: set[str] = set()
    # One snapshot of what this caller may read, taken before anything is
    # written and reused for every entry. Resolving per entry let a membership
    # change mid-request make sibling order matter, and re-resolving after
    # persistence meant an auth-store failure could raise with rows already in
    # the graph.
    reader_scope = (
        await policy.reader_scope(ctx)
        if any(entity.related_to for entity in batch.entities)
        else None
    )

    for entity in batch.entities:
        serialization.reject_unsupported_bulk_entry(entity)
        project = entity.metadata.get("project_id") if entity.metadata else None
        project_id = str(project) if project else None
        if project_id and project_id not in verified_project_ids:
            await verify_entity_project_access(
                content_session,
                ctx,
                project_id,
                required_role=ProjectRole.CONTRIBUTOR,
                require_existing_project=True,
            )
            verified_project_ids.add(project_id)
        if reader_scope is not None:
            await policy.validate_related_to_targets_for_write(
                entity_manager=runtime.entity_manager,
                related_to=entity.related_to,
                scope=reader_scope,
            )

    now = datetime.now(UTC)
    principal_id = str(ctx.user.id) if ctx.user is not None else None
    entities = [
        serialization.entity_from_bulk_create(
            entity, group_id=group_id, now=now, principal_id=principal_id
        )
        for entity in batch.entities
    ]
    created_ids = await runtime.entity_manager.create_direct_bulk(
        entities,
        generate_embeddings=not batch.defer_embeddings,
    )
    persisted_entities = [
        entity.model_copy(update={"id": created_id})
        for entity, created_id in zip(entities, created_ids, strict=True)
    ]

    # Read authority belongs to the caller, not to the batch. Passing the
    # accumulated `verified_project_ids` here made an edge's type depend on
    # which unrelated siblings rode along in the same request.
    relationships: list[Relationship] = []
    for created_id, entity in zip(created_ids, batch.entities, strict=True):
        relationships.extend(
            await policy.declared_bulk_relationships(
                created_id,
                entity.related_to,
                entity_manager=runtime.entity_manager,
                principal_id=principal_id,
                accessible_projects=(reader_scope.accessible_projects if reader_scope else set()),
                allowed_memory_scope_keys=(reader_scope.memory_grants if reader_scope else None),
                now=now,
            )
        )
    if relationships:
        create_direct_bulk = getattr(runtime.relationship_manager, "create_direct_bulk", None)
        if callable(create_direct_bulk):
            await create_direct_bulk(
                relationships,
                generate_embeddings=not batch.defer_embeddings,
            )
        else:
            await runtime.relationship_manager.create_bulk(relationships)

    background_jobs: dict[str, Any] = {}
    if batch.defer_embeddings:
        try:
            from sibyl.jobs.queue import enqueue_entity_embedding_backfill

            embedding_job_id = await enqueue_entity_embedding_backfill(
                [entity.model_dump(mode="json") for entity in persisted_entities],
                group_id,
                relationships=[
                    relationship.model_dump(mode="json") for relationship in relationships
                ],
            )
            log.info(
                "bulk_entity_embedding_backfill_enqueued",
                job_id=embedding_job_id,
                entities=len(entities),
                relationships=len(relationships),
            )
            background_jobs["embedding_backfill"] = {
                "status": "queued",
                "job_ids": [embedding_job_id],
                "queued_entities": len(entities),
                "queued_relationships": len(relationships),
            }
        except Exception as exc:
            log.warning(
                "bulk_entity_embedding_backfill_enqueue_failed",
                entities=len(entities),
                relationships=len(relationships),
                error=str(exc),
            )
            background_jobs["embedding_backfill"] = {
                "status": "failed",
                "job_ids": [],
                "queued_entities": 0,
                "queued_relationships": 0,
                "reason": "enqueue_failed",
                "error_type": type(exc).__name__,
                "recovery": _background_job_recovery(
                    "embedding_backfill",
                    list(created_ids),
                ),
            }
    projection_sources: list[Entity] = []
    projection_source_ids: list[str] = []
    for source, created_id in zip(entities, created_ids, strict=True):
        if extract_projected_memory_entities(source):
            projection_sources.append(source)
            projection_source_ids.append(created_id)

    if projection_sources:
        try:
            from sibyl.jobs.queue import enqueue_memory_projection

            projection_job_id = await enqueue_memory_projection(
                [source.model_dump(mode="json") for source in projection_sources],
                group_id,
                created_source_ids=projection_source_ids,
            )
            log.info(
                "bulk_entity_projection_enqueued",
                job_id=projection_job_id,
                sources=len(projection_sources),
            )
            background_jobs["memory_projection"] = {
                "status": "queued",
                "job_ids": [projection_job_id],
                "queued_sources": len(projection_sources),
                "skipped_sources": 0,
            }
        except Exception as exc:
            log.warning(
                "bulk_entity_projection_enqueue_failed",
                sources=len(projection_sources),
                error=str(exc),
            )
            background_jobs["memory_projection"] = {
                "status": "failed",
                "job_ids": [],
                "queued_sources": 0,
                "skipped_sources": len(projection_sources),
                "reason": "enqueue_failed",
                "error_type": type(exc).__name__,
                "recovery": _background_job_recovery(
                    "memory_projection",
                    list(projection_source_ids),
                ),
            }
    else:
        background_jobs["memory_projection"] = {
            "status": "skipped",
            "job_ids": [],
            "queued_sources": 0,
            "skipped_sources": len(entities),
            "reason": "no_projectable_sources",
        }

    background_jobs["memory_extraction"] = await _enqueue_bulk_memory_extraction(
        entities,
        list(created_ids),
        group_id,
    )

    responses = [
        serialization.entity_response_from_bulk_create(
            entity,
            entity_id=entity_id,
            group_id=group_id,
            now=now,
            principal_id=principal_id,
        )
        for entity_id, entity in zip(created_ids, batch.entities, strict=True)
    ]
    return EntityBulkCreateResponse(
        entities=responses,
        created=len(responses),
        failed=0,
        background_jobs=background_jobs,
    )


async def _resolve_background_job_recovery_entities(
    runtime: Any,
    request: EntityBackgroundJobsRequeueRequest,
    ctx: AuthContext,
    content_session: Any,
) -> tuple[list[Entity], bool]:
    verified_project_ids: set[str] = set()

    async def require_access(entity: Entity) -> None:
        await policy.require_entity_read_access(ctx, entity)
        project_id = (
            entity.id
            if entity.entity_type is EntityType.PROJECT
            else policy.entity_read_project_id(entity)
        )
        if project_id and project_id not in verified_project_ids:
            await verify_entity_project_access(
                content_session,
                ctx,
                project_id,
                required_role=ProjectRole.CONTRIBUTOR,
                require_existing_project=True,
            )
            verified_project_ids.add(project_id)

    if request.manifest_id:
        try:
            manifest = await runtime.entity_manager.get(request.manifest_id)
        except KeyError:
            manifest = None
        if manifest is None:
            raise HTTPException(status_code=404, detail="Entity not found")
        await require_access(manifest)
        expected_ids = manifest.metadata.get("expected_entity_ids")
        manifest_state = manifest.metadata.get("operational_projection_state")
        if (
            manifest.entity_type is not EntityType.ARTIFACT
            or manifest.metadata.get("projection_kind") != "manifest"
            or manifest_state not in {"embedding_pending", MANIFEST_STATE_COMPLETE}
            or not isinstance(expected_ids, list)
            or not expected_ids
            or any(not isinstance(entity_id, str) or not entity_id for entity_id in expected_ids)
            or request.manifest_id not in expected_ids
        ):
            raise HTTPException(
                status_code=409,
                detail="Entity is not a recoverable operational embedding manifest",
            )
        if manifest_state == MANIFEST_STATE_COMPLETE:
            return [manifest], True
        entities = await runtime.entity_manager.get_many(expected_ids)
        for entity in entities:
            if entity.id != manifest.id:
                await require_access(entity)
        return entities, False

    entities: list[Entity] = []
    for entity_id in dict.fromkeys(request.entity_ids):
        try:
            entity = await runtime.entity_manager.get(entity_id)
        except KeyError:
            entity = None
        if entity is None:
            raise HTTPException(status_code=404, detail=f"Entity not found: {entity_id}")
        await require_access(entity)
        entities.append(entity)
    return entities, False


@router.post(
    "/bulk/requeue-background-jobs",
    response_model=EntityBackgroundJobsRequeueResponse,
    dependencies=[Depends(require_org_role(*contracts.WRITE_ROLES))],
)
async def requeue_entity_background_jobs(
    request: EntityBackgroundJobsRequeueRequest,
    org: AuthOrganization = Depends(get_current_organization),
    ctx: AuthContext = Depends(get_auth_context),
    content_session: Any = Depends(get_content_read_session_dependency),
) -> EntityBackgroundJobsRequeueResponse:
    group_id = str(org.id)
    runtime = await policy.get_entity_graph_runtime(group_id)
    requested_jobs = set(request.jobs)
    entities, manifest_already_complete = await _resolve_background_job_recovery_entities(
        runtime,
        request,
        ctx,
        content_session,
    )
    if manifest_already_complete:
        return EntityBackgroundJobsRequeueResponse(
            entity_ids=[entity.id for entity in entities],
            manifest_id=request.manifest_id,
            background_jobs={
                "embedding_backfill": {
                    "status": "skipped",
                    "job_ids": [],
                    "reason": "manifest_complete",
                }
            },
        )
    background_jobs: dict[str, Any] = {}
    if "embedding_backfill" in requested_jobs:
        from sibyl.jobs.queue import enqueue_entity_embedding_backfill

        completion_manifest: dict[str, Any] | None = None
        embedding_entities = entities
        pending_manifests = [
            entity
            for entity in entities
            if entity.entity_type is EntityType.ARTIFACT
            and entity.metadata.get("projection_kind") == "manifest"
            and entity.metadata.get("operational_projection_state") == "embedding_pending"
        ]
        if pending_manifests:
            if len(pending_manifests) != 1:
                raise HTTPException(
                    status_code=409,
                    detail="Requeue each operational embedding manifest separately",
                )
            pending_manifest = pending_manifests[0]
            expected_ids = {
                str(entity_id)
                for entity_id in pending_manifest.metadata.get("expected_entity_ids") or ()
            }
            if expected_ids != {entity.id for entity in entities}:
                raise HTTPException(
                    status_code=409,
                    detail="Operational embedding recovery requires the exact manifest inventory",
                )
            embedding_entities = [
                entity
                for entity in entities
                if entity.id != pending_manifest.id
                and entity.entity_type is not EntityType.ARTIFACT
            ]
            storage_metadata_keys = {
                "_direct_insert",
                "description",
                "entity_type",
                "source_file",
                "updated_at",
            }
            completion_manifest = pending_manifest.model_copy(
                update={
                    "metadata": {
                        key: value
                        for key, value in pending_manifest.metadata.items()
                        if key not in storage_metadata_keys
                    }
                    | {"operational_projection_state": MANIFEST_STATE_COMPLETE}
                }
            ).model_dump(mode="json")

        serialized_relationships: list[dict[str, Any]] = []
        if completion_manifest is None:
            relationship_groups = await asyncio.gather(
                *(runtime.relationship_manager.get_for_entity(entity.id) for entity in entities)
            )
            relationships = {
                relationship.id: relationship
                for group in relationship_groups
                for relationship in group
            }
            serialized_relationships = [
                relationship.model_dump(mode="json") for relationship in relationships.values()
            ]
        embedding_job_kwargs: dict[str, Any] = {
            "relationships": serialized_relationships,
        }
        if completion_manifest is not None:
            embedding_job_kwargs["completion_manifest"] = completion_manifest
        job_id = await enqueue_entity_embedding_backfill(
            [entity.model_dump(mode="json") for entity in embedding_entities],
            group_id,
            **embedding_job_kwargs,
        )
        background_jobs["embedding_backfill"] = {
            "status": "queued",
            "job_ids": [job_id],
            "queued_entities": len(embedding_entities),
            "queued_relationships": len(serialized_relationships),
        }
    if "memory_projection" in requested_jobs:
        from sibyl.jobs.queue import enqueue_memory_projection

        projection_sources = [
            entity for entity in entities if extract_projected_memory_entities(entity)
        ]
        if projection_sources:
            projection_job_id = await enqueue_memory_projection(
                [entity.model_dump(mode="json") for entity in projection_sources],
                group_id,
                created_source_ids=[entity.id for entity in projection_sources],
            )
            background_jobs["memory_projection"] = {
                "status": "queued",
                "job_ids": [projection_job_id],
                "queued_sources": len(projection_sources),
                "skipped_sources": len(entities) - len(projection_sources),
            }
        else:
            background_jobs["memory_projection"] = {
                "status": "skipped",
                "job_ids": [],
                "queued_sources": 0,
                "skipped_sources": len(entities),
            }
    if "memory_extraction" in requested_jobs:
        from sibyl.jobs.memory_extraction import enqueue_memory_extraction_batches

        extraction_enqueue = await enqueue_memory_extraction_batches(
            [entity.model_dump(mode="json") for entity in entities],
            group_id,
            created_source_ids=[entity.id for entity in entities],
        )
        background_jobs["memory_extraction"] = _memory_extraction_receipt(
            extraction_enqueue,
            [entity.id for entity in entities],
        )
    return EntityBackgroundJobsRequeueResponse(
        entity_ids=[entity.id for entity in entities],
        manifest_id=request.manifest_id,
        background_jobs=background_jobs,
    )
