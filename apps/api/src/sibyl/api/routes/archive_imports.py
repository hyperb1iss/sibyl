"""Actor-scoped checks and redacted status for personal archive uploads."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Annotated
from uuid import UUID, uuid4

from anyio import CancelScope
from anyio.lowlevel import checkpoint
from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.requests import ClientDisconnect

from sibyl.api.idempotency import idempotency_key
from sibyl.api.routes.archive_import_authority import refresh_archive_authority
from sibyl.api.routes.archive_import_limits import (
    archive_import_budgets,
    validate_archive_plan_capacity,
)
from sibyl.api.routes.archive_import_preview import (
    authorize_archive_plan,
    build_archive_preview,
)
from sibyl.api.routes.archive_import_upload import StagedArchiveUpload, stage_archive_upload
from sibyl.api.schemas.archive_imports import ArchiveCheckStatus
from sibyl.auth.context import AuthContext
from sibyl.auth.dependencies import get_auth_context
from sibyl.persistence.surreal.archive_import_runs import (
    ArchiveCheckConflictError,
    CheckedArchiveArtifact,
    SavedArchiveCheck,
    SurrealArchiveImportRunRepository,
)
from sibyl.persistence.surreal.content import surreal_content_client
from sibyl_core.migrate.personal_archive_intake import (
    ArchiveIntakeCapacityError,
    ArchiveIntakeError,
    ParsedPersonalArchive,
    parse_personal_archive,
)
from sibyl_core.migrate.personal_archive_plan import (
    ArchiveCredentialCeiling,
    CheckedArchivePlan,
    PlannedArchiveRow,
    archive_digest,
    preview_counts,
)

router = APIRouter(prefix="/archive-imports", tags=["archive-imports"])


def _principal(context: AuthContext) -> tuple[str, str]:
    organization_id, actor_id = context.organization_id, context.user_id
    if organization_id is None or actor_id is None:
        raise HTTPException(status_code=403, detail="Archive access requires current membership")
    return organization_id, actor_id


async def _owned_archive_work[T](operation: Callable[..., T], *args: object) -> T:
    work = asyncio.create_task(asyncio.to_thread(operation, *args))
    try:
        result = await asyncio.shield(work)
        await checkpoint()
        return result
    except asyncio.CancelledError as cancellation:
        # The directory owner must outlive the parser thread. Shield cleanup
        # from ASGI cancellation and retain explicit task cancellation too.
        with CancelScope(shield=True):
            while not work.done():
                try:
                    await asyncio.shield(work)
                except asyncio.CancelledError:
                    continue
                except Exception as exc:
                    raise cancellation from exc
            try:
                work.result()
            except Exception as exc:
                raise cancellation from exc
        raise


def _operation_identity(context: AuthContext, key: str | None) -> str:
    organization_id, actor_id = _principal(context)
    return archive_digest(
        "sibyl-archive-operation-v1",
        [organization_id, actor_id, key if key is not None else str(uuid4())],
    )


def _request_digest(
    parsed: ParsedPersonalArchive,
    upload: StagedArchiveUpload,
    context: AuthContext,
    ceiling: ArchiveCredentialCeiling,
) -> str:
    return archive_digest(
        "sibyl-archive-check-request-v1",
        {
            "organization_id": context.organization_id,
            "actor_id": context.user_id,
            "archive_sha256": parsed.archive_sha256,
            "options_sha256": upload.options_sha256,
            "origin": parsed.origin.model_dump(mode="json"),
            "mappings": upload.options.mappings.model_dump(mode="json"),
            "conflict_policy": upload.options.conflict_policy,
            "credential_kind": ceiling.credential_kind,
            "api_key_id": ceiling.api_key_id,
        },
    )


def _build_checked_plan(
    parsed: ParsedPersonalArchive,
    upload: StagedArchiveUpload,
    context: AuthContext,
    ceiling: ArchiveCredentialCeiling,
    rows: tuple[PlannedArchiveRow, ...],
) -> CheckedArchivePlan:
    organization_id, actor_id = _principal(context)
    return CheckedArchivePlan(
        organization_id=organization_id,
        actor_id=actor_id,
        archive_sha256=parsed.archive_sha256,
        artifact_sha256=parsed.artifact_sha256,
        origin=parsed.origin,
        mappings=upload.options.mappings,
        credential=ceiling,
        conflict_policy=upload.options.conflict_policy,
        rows=rows,
        counts=preview_counts(rows),
    )


def _with_credential_ceiling(
    plan: CheckedArchivePlan, ceiling: ArchiveCredentialCeiling
) -> CheckedArchivePlan:
    payload = plan.model_dump(mode="python")
    payload["credential"] = ceiling.model_dump(mode="python")
    return CheckedArchivePlan.model_validate(payload)


def _bound_plan(saved: SavedArchiveCheck, context: AuthContext) -> CheckedArchivePlan:
    try:
        plan = saved.plan
    except (ValueError, TypeError, KeyError) as exc:
        raise HTTPException(status_code=503, detail="Archive status is unavailable") from exc
    if plan.organization_id != context.organization_id or plan.actor_id != context.user_id:
        raise HTTPException(status_code=404, detail="Archive check not found")
    return plan


async def _authorize_plan(
    request: Request, context: AuthContext, plan: CheckedArchivePlan
) -> ArchiveCredentialCeiling:
    current, ceiling = await refresh_archive_authority(
        request, context, original_ceiling=plan.credential
    )
    await authorize_archive_plan(plan=plan, context=current, request=request)
    return ceiling


def _status(
    record: Mapping[str, object], plan: CheckedArchivePlan, *, replayed: bool
) -> ArchiveCheckStatus:
    # Counts come from the digest-verified plan. No archived body, mapping or
    # target witness is copied into the public status projection.
    return ArchiveCheckStatus.model_validate(
        {
            "run_id": record["uuid"],
            "status": record["status"],
            "contract_version": plan.contract_version,
            "archive_sha256": plan.archive_sha256,
            "artifact_sha256": plan.artifact_sha256,
            "checked_plan_sha256": record["checked_plan_sha256"],
            "mappings_sha256": archive_digest("sibyl-archive-mappings-v1", plan.mappings),
            "revision": record["revision"],
            "created_at": record["created_at"],
            "preview_counts": {
                kind: counts.model_dump(mode="json") for kind, counts in plan.counts.items()
            },
            "replayed": replayed,
        }
    )


@router.post("/check", response_model=ArchiveCheckStatus)
async def check_archive(
    request: Request, initial_context: Annotated[AuthContext, Depends(get_auth_context)]
) -> ArchiveCheckStatus:
    """Stage an immutable checked plan without applying any archived data."""
    operation_key = idempotency_key(request)
    context, original_ceiling = await refresh_archive_authority(request, initial_context)
    intake_budget, upload_budget = archive_import_budgets()
    operation = _operation_identity(context, operation_key)
    try:
        with TemporaryDirectory(prefix="sibyl-archive-check-") as directory:
            spool = Path(directory) / "archive.spool"
            upload = await stage_archive_upload(
                request, spool=spool, budget=upload_budget, intake_budget=intake_budget
            )
            parsed = await _owned_archive_work(parse_personal_archive, spool, intake_budget)
            if parsed.archive_sha256 != upload.compressed_sha256:
                raise ArchiveIntakeError("archive spool changed after upload")

            # Refresh after streaming/parsing and before reading destination
            # facts. Keep the earlier ceiling even if current grants expand.
            context, ceiling = await refresh_archive_authority(
                request, context, original_ceiling=original_ceiling
            )
            organization_id, actor_id = _principal(context)
            request_sha256 = await _owned_archive_work(
                _request_digest, parsed, upload, context, ceiling
            )
            async with surreal_content_client() as client:
                repository = SurrealArchiveImportRunRepository(client)
                existing = await repository.load_operation(
                    operation, organization_id=organization_id, actor_id=actor_id
                )
                if existing is not None:
                    if existing["request_sha256"] != request_sha256:
                        raise ArchiveCheckConflictError("archive operation owns another request")
                    saved = SavedArchiveCheck(record=existing, replayed=True)
                    plan = await _owned_archive_work(_bound_plan, saved, context)
                    await _authorize_plan(request, context, plan)
                    return _status(existing, plan, replayed=True)

                rows = await build_archive_preview(
                    parsed=parsed,
                    mappings=upload.options.mappings,
                    context=context,
                    request=request,
                )
                plan = await _owned_archive_work(
                    _build_checked_plan, parsed, upload, context, ceiling, rows
                )
                final_ceiling = await _authorize_plan(request, context, plan)
                plan = await _owned_archive_work(_with_credential_ceiling, plan, final_ceiling)
                await _owned_archive_work(validate_archive_plan_capacity, plan, intake_budget)
                saved = await repository.create_checked(
                    plan=plan,
                    artifact=CheckedArchiveArtifact(
                        archive_sha256=parsed.archive_sha256,
                        artifact_sha256=parsed.artifact_sha256,
                        member_inventory_json=parsed.member_inventory_json,
                        staged_payload_json=parsed.staged_payload_json,
                        measured_sizes_json=parsed.measured_sizes_json,
                    ),
                    intake_identity=operation,
                    request_sha256=request_sha256,
                    metadata_transaction_bytes=intake_budget.metadata_transaction_bytes,
                )
                # A concurrent winner owns the original plan. Reauthorize that
                # plan before exposing its diagnostics, including on replay.
                accepted_plan = await _owned_archive_work(_bound_plan, saved, context)
                await _authorize_plan(request, context, accepted_plan)
                return _status(saved.record, accepted_plan, replayed=saved.replayed)
    except ArchiveIntakeCapacityError as exc:
        raise HTTPException(status_code=413, detail="Archive resource budget exceeded") from exc
    except ArchiveIntakeError as exc:
        raise HTTPException(status_code=422, detail="Archive upload is invalid") from exc
    except ArchiveCheckConflictError as exc:
        raise HTTPException(status_code=409, detail="Archive operation conflicts") from exc
    except ClientDisconnect as exc:
        raise HTTPException(status_code=400, detail="Archive upload was interrupted") from exc


@router.get("/{run_id}", response_model=ArchiveCheckStatus)
async def archive_check_status(
    run_id: UUID,
    request: Request,
    initial_context: Annotated[AuthContext, Depends(get_auth_context)],
) -> ArchiveCheckStatus:
    """Return the caller's saved counts and digests under current read authority."""
    context, _ = await refresh_archive_authority(request, initial_context)
    organization_id, actor_id = _principal(context)
    async with surreal_content_client() as client:
        record = await SurrealArchiveImportRunRepository(client).load(
            str(run_id), organization_id=organization_id, actor_id=actor_id
        )
    if record is None:
        raise HTTPException(status_code=404, detail="Archive check not found")
    plan = await _owned_archive_work(
        _bound_plan, SavedArchiveCheck(record=record, replayed=False), context
    )
    await refresh_archive_authority(request, context)
    return _status(record, plan, replayed=False)
