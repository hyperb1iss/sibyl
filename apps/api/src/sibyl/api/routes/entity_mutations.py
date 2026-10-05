"""Canonical mutations ownership for entity routes."""

from datetime import UTC, datetime
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request

from sibyl.api.decorators import handle_workflow_errors
from sibyl.api.errors import (
    constraint_violation,
    entity_locked,
    revision_conflict,
    safe_error_payload,
    sanitize_error_text,
    unprocessable_entity,
)
from sibyl.api.event_types import WSEvent
from sibyl.api.idempotency import (
    replay_idempotent_response,
    save_idempotent_response,
    serialize_idempotent_request,
)
from sibyl.api.routes import (
    entity_captures as captures,
    entity_contracts as contracts,
    entity_policy as policy,
    entity_serialization as serialization,
)
from sibyl.api.schemas import (
    EntityCreate,
    EntityResponse,
    EntityUpdate,
)
from sibyl.api.websocket import broadcast_event, entity_change_payload
from sibyl.auth.authorization import verify_entity_project_access
from sibyl.auth.context import AuthContext
from sibyl.auth.dependencies import (
    get_auth_context,
    get_current_organization,
    require_org_role,
)
from sibyl.persistence.auth_runtime import (
    create_project_record,
    delete_project_record,
    log_audit_event,
    update_project_record,
)
from sibyl.persistence.content_runtime import (
    get_content_read_session_dependency,
)
from sibyl_core.auth import AuthOrganization, ProjectRole
from sibyl_core.auth.memory_policy import (
    SERVER_OWNED_METADATA_KEYS,
)
from sibyl_core.embeddings.provenance import without_client_embedding_stamp
from sibyl_core.errors import RevisionConflictError
from sibyl_core.memory_pipeline.structure import strip_structure_metadata
from sibyl_core.models.entities import EntityType
from sibyl_core.projection import (
    entity_scope_stamps,
    reproject_entity_passages,
    restamp_entity_passages,
    retire_entity_passages,
)

log = structlog.get_logger()

router = APIRouter(
    prefix="/entities",
    tags=["entities"],
    dependencies=[Depends(require_org_role(*contracts.READ_ROLES))],
)


async def _add_with_structure(**kwargs: Any) -> Any:
    """Call the graph writer, turning a refused cut plan into a 422.

    The message reaches the caller intact on purpose. A writing agent that got
    its offsets wrong has to know which span was out of bounds or where the gap
    was, and a generic rejection would leave it re-guessing the whole plan.
    """
    from sibyl_core.memory_pipeline.structure import MemoryStructureError
    from sibyl_core.tools.core import add

    try:
        return await add(**kwargs)
    except MemoryStructureError as exc:
        raise unprocessable_entity(
            str(exc),
            field=exc.field,
            remediation="Recompute the declared structure against the stored content.",
        ) from exc


def _resolved_update_structure(
    *,
    existing: Any,
    update: Any,
    new_content: str | None,
    base_metadata: dict[str, Any],
) -> dict[str, Any]:
    """Return the complete metadata an update leaves on the row.

    Complete rather than a patch: dropping a stored plan means the key must be
    absent from what gets written, and spreading a partial dict over the stored
    metadata can add keys but never remove them.

    Offsets belong to the text they were computed over, so a rewritten body
    invalidates a stored plan outright: without fresh spans the plan is dropped
    and the body goes back to the mechanical cutter. Refusing the update instead
    would make a plain content edit impossible for any memory that was ever
    spanned, and keeping the stale plan would cut the new body on the old seams.

    The atomic claim is about the memory rather than about offsets, so it
    survives a content change untouched unless the caller restates it. It is
    re-validated against whatever body ends up stored, since a rewrite can push
    a previously-atomic memory past the ceiling one passage row can hold.
    """
    from sibyl_core.memory_pipeline.spans import (
        AGENT_ATOMIC_METADATA_KEY,
        AGENT_SPANS_METADATA_KEY,
        MemoryStructureError,
        agent_atomic_from_metadata,
    )
    from sibyl_core.memory_pipeline.structure import (
        PROBE_LAST_REPLAY_METADATA_KEY,
        PROBE_REHEARSAL_METADATA_KEY,
        build_memory_structure,
        structure_metadata,
    )

    existing_metadata = getattr(existing, "metadata", {}) or {}
    stored_content = (getattr(existing, "content", "") or "").strip()
    content = new_content.strip() if new_content is not None else stored_content
    content_changed = new_content is not None and content != stored_content

    spans: list[dict[str, Any]] | None
    if update.spans is not None:
        # A fresh plan is the most specific instruction in the request, so it
        # withdraws a stored atomic claim rather than colliding with it.
        spans = [span.model_dump(exclude_none=True) for span in update.spans]
        atomic = bool(update.atomic)
    elif update.atomic:
        spans = None
        atomic = True
    else:
        spans = (
            None
            if content_changed
            else list(existing_metadata.get(AGENT_SPANS_METADATA_KEY) or ()) or None
        )
        atomic = False if update.atomic is False else agent_atomic_from_metadata(existing_metadata)

    try:
        structure = build_memory_structure(content, spans=spans, atomic=atomic)
    except MemoryStructureError as exc:
        raise unprocessable_entity(
            str(exc),
            field=exc.field,
            remediation="Recompute the declared structure against the updated content.",
        ) from exc

    declaration_keys = [AGENT_SPANS_METADATA_KEY, AGENT_ATOMIC_METADATA_KEY]
    if content_changed:
        # A receipt describes whether a particular body could be found. Once that
        # body is rewritten the verdict is about text that no longer exists, and a
        # stale pass is worse than no receipt: the probes survive, so the replay
        # job produces an honest one on its next run.
        declaration_keys += [PROBE_REHEARSAL_METADATA_KEY, PROBE_LAST_REPLAY_METADATA_KEY]
    resolved = {key: value for key, value in base_metadata.items() if key not in declaration_keys}
    stamped = structure_metadata(structure)
    # The entity update lands as `UPDATE entity MERGE $patch`, and a MERGE cannot
    # remove a key: one omitted from the patch keeps whatever the row already
    # held. So a withdrawn declaration is written as an explicit null, which the
    # readers treat as absent, rather than left out and silently preserved.
    for key in declaration_keys:
        resolved[key] = stamped.get(key)
    return resolved


@router.post(
    "",
    response_model=EntityResponse,
    status_code=201,
    dependencies=[Depends(require_org_role(*contracts.WRITE_ROLES))],
)
@handle_workflow_errors("create_entity")
@serialize_idempotent_request
async def create_entity(
    request: Request,
    entity: EntityCreate,
    org: AuthOrganization = Depends(get_current_organization),
    ctx: AuthContext = Depends(get_auth_context),
    content_session: Any = Depends(get_content_read_session_dependency),
    sync: bool = Query(
        default=False,
        description="Wait for entity creation to complete (slower but entity is immediately available)",
    ),
    protect_ownership: bool = Query(
        default=False, description="Atomically preserve existing owner and project authority"
    ),
    replay_interrupted: bool = Query(
        default=True, description="Allow re-execution when an earlier receipt is incomplete"
    ),
) -> EntityResponse:
    """Create a new entity.

    By default, entities are created asynchronously via a background worker.
    Set sync=true to wait for creation to complete (useful for tasks that need
    immediate workflow operations like start/complete).
    """

    group_id = str(org.id)
    if protect_ownership is True and (sync is not True or ctx.user is None):
        raise HTTPException(
            status_code=422, detail="Protected writes require authenticated sync=true"
        )

    # Extract task-specific fields from metadata if present
    project = entity.metadata.get("project_id") if entity.metadata else None
    epic = entity.metadata.get("epic_id") if entity.metadata else None
    priority = entity.metadata.get("priority") if entity.metadata else None
    assignees = entity.metadata.get("assignees") if entity.metadata else None
    technologies = entity.metadata.get("technologies") if entity.metadata else None
    depends_on = entity.metadata.get("depends_on") if entity.metadata else None
    if project:
        await verify_entity_project_access(
            content_session,
            ctx,
            project,
            required_role=ProjectRole.CONTRIBUTOR,
            require_existing_project=True,
        )
    runtime = await policy.get_entity_graph_runtime(group_id)

    # Use description as content fallback (frontend sends description, add() needs content)
    content = entity.content or entity.description or entity.name
    # The stamp is the server's to write; neither storage nor the response
    # repeats one the client sent.
    request_metadata: dict[str, object] = without_client_embedding_stamp(entity.metadata)
    idempotency_payload = {
        "body": entity.model_dump(mode="json"),
        "query": {"sync": sync},
    }

    if protect_ownership is True:
        idempotency_payload["query"]["protect_ownership"] = True
    if replay_interrupted is False:
        idempotency_payload["query"]["replay_interrupted"] = False

    if ctx.user is not None:
        replayed = await replay_idempotent_response(
            request,
            organization_id=org.id,
            principal_id=str(ctx.user.id),
            method="POST",
            path="/entities",
            payload=idempotency_payload,
            response_model=EntityResponse,
            content_session=content_session,
            replay_interrupted=replay_interrupted is not False,
        )
        if replayed is not None:
            return replayed

    # Target resolution runs only on the live path. A retry whose target was
    # deleted or whose access was revoked since the first call must still
    # return the response that first call stored, not a fresh 404.
    reader_scope = await policy.reader_scope(ctx) if entity.related_to else None
    if reader_scope is not None:
        await policy.validate_related_to_targets_for_write(
            entity_manager=runtime.entity_manager,
            related_to=entity.related_to,
            scope=reader_scope,
        )

    authorized_principal_id = str(ctx.user.id) if ctx.user is not None else None
    authorized_scope_key = str(project) if project else None
    declared_memory_scope = request_metadata.get("memory_scope")
    merged_metadata: dict[str, Any] = {
        **serialization.scoped_graph_metadata(
            request_metadata,
            principal_id=authorized_principal_id,
            verified_project_id=authorized_scope_key,
        ),
        "organization_id": group_id,
    }

    # Projects are always sync (foundational - tasks depend on them existing)
    # Other entities can be async unless caller explicitly requests sync
    # Probes force a synchronous write in the writer too, but the route decides
    # which response shape it returns, so both have to agree: a pending response
    # would drop the rehearsal receipt the caller asked for.
    is_sync = entity.entity_type.value == "project" or sync or bool(entity.probes)

    result = await _add_with_structure(
        title=entity.name,
        content=content,
        entity_type=entity.entity_type.value,
        category=entity.category,
        languages=entity.languages,
        tags=entity.tags,
        related_to=entity.related_to,
        metadata=merged_metadata,
        # Task-specific fields
        project=project,
        epic=epic,
        priority=priority,
        assignees=assignees,
        technologies=technologies,
        depends_on=depends_on,
        # Sync for projects, async for everything else
        sync=is_sync,
        protect_ownership=protect_ownership is True,
        skip_conflicts=entity.skip_conflicts,
        generate_embeddings=not entity.defer_embeddings,
        memory_scope=str(declared_memory_scope) if declared_memory_scope is not None else None,
        scope_key=authorized_scope_key,
        principal_id=authorized_principal_id,
        accessible_projects=reader_scope.accessible_projects if reader_scope else None,
        allowed_memory_scope_keys=reader_scope.memory_grants if reader_scope else None,
        retrieval_keys=entity.retrieval_keys,
        spans=[span.model_dump(exclude_none=True) for span in entity.spans]
        if entity.spans is not None
        else None,
        atomic=entity.atomic,
        probes=list(entity.probes) if entity.probes is not None else None,
    )

    if not result.success or not result.id:
        message = sanitize_error_text(str(result.message or "Entity creation failed"))
        if "duplicate" in message.lower() or "already exists" in message.lower():
            raise constraint_violation(
                "duplicate entity name in scope",
                remediation="Use a different title or update the existing entity.",
                details={
                    "field": "name",
                    "entity_type": entity.entity_type.value,
                },
            )
        raise HTTPException(status_code=400, detail=message)

    if request_metadata.get("capture_mode") in {"quick", "remember"}:
        raw_capture_metadata = serialization.sanitize_raw_capture_metadata(request_metadata)
        await captures.archive_raw_capture(
            content_session,
            organization_id=org.id,
            user_id=ctx.user.id if ctx.user else None,
            entity_id=result.id,
            entity_name=entity.name,
            entity_content=content,
            entity_type=entity.entity_type.value,
            tags=list(entity.tags or []),
            metadata=raw_capture_metadata,
            source_capture_id=str(request_metadata["raw_memory_id"])
            if request_metadata.get("raw_memory_id")
            else None,
        )

    result_background_jobs = getattr(result, "background_jobs", {})
    if not isinstance(result_background_jobs, dict):
        result_background_jobs = {}

    # For async creation, return immediately with pending response.
    # Entity creation continues in the native background job path.
    if not is_sync:
        response = EntityResponse(
            id=result.id,
            entity_type=entity.entity_type,
            name=entity.name,
            description=entity.description or "",
            content=content,
            category=entity.category,
            languages=entity.languages or [],
            tags=entity.tags or [],
            metadata=merged_metadata,
            source_file=None,
            created_at=None,
            updated_at=None,
            background_jobs=result_background_jobs,
        )
        # Broadcast pending creation event
        await broadcast_event(
            WSEvent.ENTITY_PENDING,
            entity_change_payload(result.id, entity.entity_type.value),
            org_id=str(org.id),
        )
        if ctx.user is not None:
            await save_idempotent_response(
                request,
                organization_id=org.id,
                principal_id=str(ctx.user.id),
                method="POST",
                path="/entities",
                payload=idempotency_payload,
                response=response,
                status_code=201,
                content_session=content_session,
            )
        return response

    response_timestamp = getattr(result, "timestamp", None) or datetime.now(UTC)
    response = EntityResponse(
        id=result.id,
        revision=getattr(result, "revision", None),
        entity_type=entity.entity_type,
        name=entity.name,
        description=entity.description or "",
        content=content,
        category=entity.category,
        languages=entity.languages or [],
        tags=entity.tags or [],
        metadata=merged_metadata,
        probe_rehearsal=getattr(result, "probe_rehearsal", None),
        source_file=None,
        created_at=response_timestamp,
        updated_at=response_timestamp,
        background_jobs=result_background_jobs,
    )

    # Broadcast creation event (scoped to org)
    await broadcast_event(
        WSEvent.ENTITY_CREATED,
        entity_change_payload(result.id, entity.entity_type.value),
        org_id=str(org.id),
    )

    if entity.entity_type == EntityType.PROJECT:
        await create_project_record(
            organization_id=org.id,
            owner_user_id=ctx.user.id,
            graph_project_id=result.id,
            name=entity.name,
            description=content,
        )
        await log_audit_event(
            action="project.create",
            user_id=ctx.user.id,
            organization_id=org.id,
            request=request,
            details={"project_id": result.id, "name": entity.name},
        )

    if ctx.user is not None:
        await save_idempotent_response(
            request,
            organization_id=org.id,
            principal_id=str(ctx.user.id),
            method="POST",
            path="/entities",
            payload=idempotency_payload,
            response=response,
            status_code=201,
            content_session=content_session,
        )

    return response


RAW_MEMORY_REFERENCE_PREFIX = "raw_memory:"


def _correct_hint(template: str, entity_id: str) -> str:
    """Name the real id in a hint when the error sanitizer will let it through.

    The sanitizer blanks any text naming a write verb or a path, which is also
    why these hints point at the correction command's help rather than at an
    action. An id that trips it falls back to the placeholder, so the hint
    survives either way.
    """
    specific = template.format(id=entity_id)
    return specific if sanitize_error_text(specific) == specific else template.format(id="<id>")


def _refuse_raw_memory_reference(entity_id: str) -> None:
    """Refuse a raw memory reference on a graph route with a 4xx that says where to go.

    Raw memories live in the content store and change only through the
    correction lifecycle. Sent here they used to fall through to a 500, which
    the CLI took for an outage and buffered for replay forever.
    """
    candidate = entity_id.strip()
    if candidate.lower().startswith(RAW_MEMORY_REFERENCE_PREFIX):
        raw_id = candidate[len(RAW_MEMORY_REFERENCE_PREFIX) :]
        raise unprocessable_entity(
            f"{entity_id} is a raw memory, not a graph entity",
            field="entity_id",
            remediation=_correct_hint(
                "Raw memories change through corrections, which keep a reason and a "
                "receipt. Run 'sibyl correct {id} --help' for the actions.",
                raw_id,
            ),
        )


async def _existing_entity_or_404(entity_manager: Any, entity_id: str) -> Any:
    """Load an entity, answering a missing one with a 404 instead of a 500.

    EntityManager.get raises KeyError for an unknown id, so a falsy check alone
    never fired and the route's catch-all turned every miss into a 500.
    """
    try:
        existing = await entity_manager.get(entity_id)
    except KeyError:
        existing = None
    if not existing:
        raise HTTPException(
            status_code=404,
            detail=safe_error_payload(
                error="not_found",
                message=f"Entity not found: {entity_id}",
                remediation=_correct_hint(
                    "If {id} is a raw memory ID, change it with 'sibyl correct {id}', "
                    "not the entity routes.",
                    entity_id,
                ),
            ),
        )
    return existing


@router.patch(
    "/{entity_id}",
    response_model=EntityResponse,
    dependencies=[Depends(require_org_role(*contracts.WRITE_ROLES))],
)
async def update_entity(
    entity_id: str,
    update: EntityUpdate,
    request: Request,
    org: AuthOrganization = Depends(get_current_organization),
    ctx: AuthContext = Depends(get_auth_context),
    content_session: Any = Depends(get_content_read_session_dependency),
) -> EntityResponse:
    """Update an existing entity."""
    from sibyl.locks import LockAcquisitionError, entity_lock

    _refuse_raw_memory_reference(entity_id)
    group_id = str(org.id)

    try:
        # Acquire distributed lock to prevent concurrent updates
        async with entity_lock(group_id, entity_id, blocking=True) as lock_token:
            if not lock_token:
                raise entity_locked()

            runtime = await policy.get_entity_graph_runtime(group_id)

            existing = await _existing_entity_or_404(runtime.entity_manager, entity_id)

            # Verify project access for entities with project_id
            project_id = policy.entity_read_project_id(existing)
            await verify_entity_project_access(
                content_session,
                ctx,
                project_id,
                required_role=ProjectRole.CONTRIBUTOR,
                require_existing_project=True,
            )
            # Project contribution says nothing about a private memory, and the
            # response echoes the stored content back, so an update has to clear
            # the same gate a read does.
            await policy.require_entity_scope_visible(ctx, existing, project_id=project_id)

            # Build update dict with only provided fields
            update_data: dict[str, Any] = {}
            if update.name is not None:
                update_data["name"] = update.name
            if update.description is not None:
                update_data["description"] = update.description
            if update.content is not None:
                # Stripped, because the graph writer strips on create and the
                # declared span offsets are validated against the stripped body.
                # Storing the padded string would put the plan one place at
                # validation and another at projection, so an accepted plan would
                # be quietly abandoned for the mechanical cutter.
                update_data["content"] = update.content.strip()
            if update.category is not None:
                update_data["category"] = update.category
            if update.languages is not None:
                update_data["languages"] = update.languages
            if update.tags is not None:
                update_data["tags"] = update.tags
            if update.metadata is not None:
                # Merge metadata, keeping the stored owner channels: reassigning
                # them would hand the row to a different principal or project.
                # Capture provenance is kept for the same reason, because the
                # correction write-through retires rows by querying it, so a
                # patch that could rewrite it could nominate this row to be
                # retired later by a correction on an unrelated capture. The
                # structure keys go the same way, so a forwarded body cannot
                # plant a plan the server never validated.
                existing_meta = getattr(existing, "metadata", {}) or {}
                update_data["metadata"] = {
                    **existing_meta,
                    **{
                        key: value
                        for key, value in strip_structure_metadata(update.metadata).items()
                        if key not in SERVER_OWNED_METADATA_KEYS
                    },
                }

            structure_metadata_changed = (
                update.spans is not None or update.atomic is not None or update.content is not None
            )
            if structure_metadata_changed:
                update_data["metadata"] = _resolved_update_structure(
                    existing=existing,
                    update=update,
                    new_content=update.content,
                    base_metadata=update_data.get(
                        "metadata", dict(getattr(existing, "metadata", {}) or {})
                    ),
                )

            # Update timestamp
            update_data["updated_at"] = datetime.now(UTC)
            if ctx.user is not None:
                update_data["modified_by"] = str(ctx.user.id)

            # Perform update
            updated = await runtime.entity_manager.update(entity_id, update_data)
            if not updated:
                raise HTTPException(status_code=500, detail="Update failed")

            if update.content is not None or update.spans is not None or update.atomic is not None:
                # A rewritten body invalidates every span cut from the old one,
                # and a re-declared plan changes where the seams belong. Either
                # way the stored spans keep serving the previous revision until
                # they are replaced, so the re-cut happens on the request path:
                # the only other caller of the reprojection is an arq job nothing
                # enqueues, which is why this rule was inert before.
                passage_result = await reproject_entity_passages(
                    entity_manager=runtime.entity_manager,
                    relationship_manager=runtime.relationship_manager,
                    source=updated,
                    group_id=group_id,
                    created_source_id=entity_id,
                )
                if passage_result.errors:
                    # The update landed, but stale spans of the previous body
                    # are still being served, so reporting a clean update would
                    # be reporting a re-cut that did not happen.
                    log.error(
                        "update_entity_passage_reprojection_failed",
                        entity_id=entity_id,
                        errors=passage_result.errors,
                    )
                    raise HTTPException(
                        status_code=500,
                        detail=(
                            "Entity updated, but its passages were not fully re-cut "
                            "and stale spans still serve the previous revision: "
                            f"{'; '.join(passage_result.errors)}"
                        ),
                    )
            elif entity_scope_stamps(existing) != entity_scope_stamps(updated):
                # The body did not change, so the cut is still valid, but the
                # audience did: spans inherit their reader checks from these
                # stamps, and a stale copy keeps serving a tightened memory's
                # text through search until it is refreshed. The managers
                # enable the failed-write recovery path: this trigger diffs
                # pre/post stamps, so once the parent carries the new stamps a
                # partial restamp would never re-fire.
                await restamp_entity_passages(
                    entity_manager=runtime.entity_manager,
                    source=updated,
                    created_source_id=entity_id,
                    relationship_manager=runtime.relationship_manager,
                    group_id=group_id,
                )

            response = EntityResponse(
                id=updated.id,
                entity_type=updated.entity_type,
                name=updated.name,
                description=updated.description or "",
                content=updated.content or "",
                category=getattr(updated, "category", None) or updated.metadata.get("category"),
                languages=getattr(updated, "languages", None)
                or updated.metadata.get("languages", [])
                or [],
                tags=getattr(updated, "tags", None) or updated.metadata.get("tags", []) or [],
                metadata=getattr(updated, "metadata", {}) or {},
                source_file=getattr(updated, "source_file", None),
                created_at=getattr(updated, "created_at", None),
                updated_at=getattr(updated, "updated_at", None),
            )

            # Broadcast update event (scoped to org)
            await broadcast_event(
                WSEvent.ENTITY_UPDATED,
                entity_change_payload(response.id, response.entity_type.value),
                org_id=str(org.id),
            )

            if existing.entity_type == EntityType.PROJECT:
                await update_project_record(
                    organization_id=org.id,
                    graph_project_id=existing.id,
                    name=response.name,
                    description=response.description,
                )
                await log_audit_event(
                    action="project.update",
                    user_id=ctx.user.id,
                    organization_id=org.id,
                    request=request,
                    details={"project_id": existing.id, "name": response.name},
                )

            return response

    except LockAcquisitionError as e:
        raise entity_locked() from e
    except HTTPException:
        raise
    except Exception as e:
        log.exception("update_entity_failed", entity_id=entity_id, error=str(e))
        raise HTTPException(
            status_code=500, detail="Failed to update entity. Please try again."
        ) from e


# Rows that never count as someone's work linking to an entity: the project
# itself and what the server derives from memories.
_UNSHARING_TYPES = frozenset({EntityType.PROJECT, "topic", "passage"})
_DERIVED_CATEGORIES = frozenset(
    {"memory_projection", "passage_projection", "memory_fact_projection"}
)


def _entity_author(entity: Any) -> str | None:
    author = (getattr(entity, "metadata", None) or {}).get("principal_id") or getattr(
        entity, "created_by", None
    )
    return str(author) if author else None


def _migration_origin(entity: Any) -> object:
    migration = (getattr(entity, "metadata", None) or {}).get("migration")
    if not isinstance(migration, dict):
        return None
    return migration.get("tool"), migration.get("origin_org")


async def _shared_reason(runtime: Any, existing: Any, author: str | None) -> str | None:
    """Why deleting this row would take someone else's work with it, if it would."""
    modified_by = getattr(existing, "modified_by", None)
    if modified_by and str(modified_by) != author:
        return "someone other than its author has edited it"
    relationships = await runtime.relationship_manager.get_for_entity(existing.id)
    linked_ids = {
        rel.target_id if rel.source_id == existing.id else rel.source_id for rel in relationships
    } - {existing.id}
    if not linked_ids:
        return None
    origin = _migration_origin(existing)
    for row in await runtime.entity_manager.get_many(sorted(linked_ids)):
        if row.entity_type in _UNSHARING_TYPES or getattr(row, "category", None) in (
            _DERIVED_CATEGORIES
        ):
            continue
        # Rows the same author migrated from the same source are this row's own
        # migration; the undo walks them itself and keeps what they need.
        if (
            origin is not None
            and _entity_author(row) == author
            and _migration_origin(row) == origin
        ):
            continue
        return "a row outside its migration links to it"
    return None


async def _check_delete(
    runtime: Any,
    existing: Any,
    *,
    ctx: AuthContext,
    content_session: Any,
    expected_revision: int | None,
    if_unshared: bool,
) -> None:
    """Raise unless the caller may delete this row under the conditions they named."""
    author = _entity_author(existing)
    # Deleting from a project takes a maintainer, except that an author may
    # remove their own row (not a project) at a revision they name, while
    # nobody else has written it or links to it: what a migration undo does,
    # with the same access that wrote the row.
    own_delete = (
        expected_revision is not None
        and if_unshared
        and ctx.user is not None
        and author is not None
        and author == str(ctx.user.id)
        and existing.entity_type != EntityType.PROJECT
    )
    project_id = policy.entity_read_project_id(existing)
    await verify_entity_project_access(
        content_session,
        ctx,
        project_id,
        required_role=ProjectRole.CONTRIBUTOR if own_delete else ProjectRole.MAINTAINER,
        require_existing_project=True,
    )
    # A project maintainer role does not extend over a co-member's private
    # memory, so deletion clears the same gate a read does.
    await policy.require_entity_scope_visible(ctx, existing, project_id=project_id)

    # Only a caller allowed to delete the row learns its revision.
    current_revision = getattr(existing, "revision", None)
    if expected_revision is not None and current_revision != expected_revision:
        raise revision_conflict(
            RevisionConflictError(existing.id, expected_revision, int(current_revision or 0))
        )
    if if_unshared and (reason := await _shared_reason(runtime, existing, author)):
        raise HTTPException(
            status_code=409,
            detail=safe_error_payload(error="entity_shared", message=f"Not removed: {reason}."),
        )


@router.get(
    "/{entity_id}/deletable",
    dependencies=[Depends(require_org_role(*contracts.WRITE_ROLES))],
)
async def check_entity_deletable(
    entity_id: str,
    org: AuthOrganization = Depends(get_current_organization),
    ctx: AuthContext = Depends(get_auth_context),
    content_session: Any = Depends(get_content_read_session_dependency),
    expected_revision: int = Query(ge=1, description="The revision the delete would name."),
) -> dict[str, Any]:
    """Whether a guarded, unshared delete at this revision would go through, without deleting.

    Runs the checks `DELETE ?expected_revision=N&if_unshared=true` runs, so a
    dry run can report what an undo would keep. Access failures answer as they
    would for the delete; a revision or sharing refusal answers 200 with its
    reason.
    """
    _refuse_raw_memory_reference(entity_id)
    runtime = await policy.get_entity_graph_runtime(str(org.id))
    existing = await _existing_entity_or_404(runtime.entity_manager, entity_id)
    try:
        await _check_delete(
            runtime,
            existing,
            ctx=ctx,
            content_session=content_session,
            expected_revision=expected_revision,
            if_unshared=True,
        )
    except HTTPException as exc:
        if exc.status_code != 409 or not isinstance(exc.detail, dict):
            raise
        return {
            "deletable": False,
            "error": exc.detail.get("error"),
            "reason": exc.detail.get("message"),
        }
    return {"deletable": True}


@router.delete(
    "/{entity_id}",
    status_code=204,
    dependencies=[Depends(require_org_role(*contracts.WRITE_ROLES))],
)
async def delete_entity(
    entity_id: str,
    request: Request,
    org: AuthOrganization = Depends(get_current_organization),
    ctx: AuthContext = Depends(get_auth_context),
    content_session: Any = Depends(get_content_read_session_dependency),
    expected_revision: int | None = Query(
        default=None,
        ge=1,
        description="Delete only while the entity is still at this revision.",
    ),
    if_unshared: bool = Query(
        default=False,
        description=(
            "Delete only while nobody but the entity's author has written it and no row "
            "outside its own migration links to it. With expected_revision, the author "
            "may delete it with contributor access."
        ),
    ),
) -> None:
    """Delete an entity."""
    from sibyl.locks import LockAcquisitionError, entity_lock

    _refuse_raw_memory_reference(entity_id)
    group_id = str(org.id)

    try:
        # Acquire distributed lock to prevent concurrent modifications
        async with entity_lock(group_id, entity_id, blocking=True) as lock_token:
            if not lock_token:
                raise entity_locked()

            runtime = await policy.get_entity_graph_runtime(group_id)

            existing = await _existing_entity_or_404(runtime.entity_manager, entity_id)
            # A direct call (not through FastAPI) passes the Query markers themselves.
            await _check_delete(
                runtime,
                existing,
                ctx=ctx,
                content_session=content_session,
                expected_revision=expected_revision if type(expected_revision) is int else None,
                if_unshared=if_unshared is True,
            )

            # Spans first, parent second. Spans are derived, so retiring them
            # while the memory still exists costs nothing that a reprojection
            # cannot rebuild, and it keeps this whole route re-enterable: a
            # failure here leaves the parent in place, so retrying the delete
            # finds it and resumes. Deleting the parent first and failing here
            # instead stranded the spans permanently, because the retry's own
            # existence check would 404 on the parent that was already gone
            # while the spans went on serving the deleted text.
            retirement = await retire_entity_passages(
                entity_manager=runtime.entity_manager,
                source_id=entity_id,
            )
            if retirement.retired:
                log.info(
                    "entity_delete_retired_passages",
                    entity_id=entity_id,
                    retired=retirement.retired,
                )
            if not retirement.complete:
                log.error(
                    "entity_delete_passages_stranded",
                    entity_id=entity_id,
                    stranded=retirement.failed_passage_ids,
                )
                raise HTTPException(
                    status_code=500,
                    detail=(
                        "Delete aborted: these passages still serve the memory's "
                        "text and could not be retired, so the memory was kept. "
                        f"Retry the delete: {', '.join(retirement.failed_passage_ids)}"
                    ),
                )

            # Delete from graph
            success = await runtime.entity_manager.delete(entity_id)
            if not success:
                raise HTTPException(status_code=500, detail="Delete failed")

            if existing.entity_type == EntityType.PROJECT:
                await delete_project_record(
                    organization_id=org.id,
                    graph_project_id=existing.id,
                )
                # Audited after the record is gone, not before: an audit written
                # ahead of the work claims a deletion that a later failure on
                # this route never performed.
                await log_audit_event(
                    action="project.delete",
                    user_id=ctx.user.id,
                    organization_id=org.id,
                    request=request,
                    details={"project_id": existing.id, "name": existing.name},
                )

            # Broadcast deletion event (scoped to org)
            await broadcast_event(
                WSEvent.ENTITY_DELETED,
                entity_change_payload(entity_id, existing.entity_type.value),
                org_id=str(org.id),
            )

    except LockAcquisitionError as e:
        raise entity_locked() from e
    except HTTPException:
        raise
    except Exception as e:
        log.exception("delete_entity_failed", entity_id=entity_id, error=str(e))
        raise HTTPException(
            status_code=500, detail="Failed to delete entity. Please try again."
        ) from e
