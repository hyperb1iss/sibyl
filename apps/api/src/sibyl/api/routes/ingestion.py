"""Source ingestion API endpoints."""

from __future__ import annotations

from collections.abc import Iterable
from hashlib import sha256
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException

from sibyl.api.schemas import (
    DocumentCollectionListResponse,
    DocumentCollectionResponse,
    DocumentImportRequest,
    SourceAdapterListResponse,
    SourceAdapterResponse,
    SourceImportResumeRequest,
    SourceImportStartRequest,
    SourceImportStatusResponse,
)
from sibyl.auth.context import AuthContext
from sibyl.auth.dependencies import get_auth_context, get_current_organization, require_org_role
from sibyl.config import settings
from sibyl.jobs import queue as job_queue
from sibyl.jobs.source_imports import (
    cancel_source_import,
    get_source_import_status,
    memory_policy_context_payload,
    start_source_import,
)
from sibyl.persistence import content_runtime
from sibyl.persistence.auth_runtime import list_accessible_project_graph_ids
from sibyl.persistence.content_common import RawCaptureRecord
from sibyl.services.document_adapters import (
    DOCUMENT_FILE_ADAPTER_NAME,
    DOCUMENT_FOLDER_ADAPTER_NAME,
    DOCUMENT_TEXT_ADAPTER_NAME,
    DOCUMENT_URL_ADAPTER_NAME,
    ensure_document_adapters_registered,
)
from sibyl_core.auth import AuthOrganization, OrganizationRole
from sibyl_core.models.sources import SourceAdapterDescriptor
from sibyl_core.services.mailbox_adapter import IMAP_ADAPTER_NAME, ensure_mailbox_adapter_registered
from sibyl_core.services.source_adapters import list_source_adapters
from sibyl_core.services.transcript_adapters import ensure_transcript_adapters_registered

router = APIRouter(
    prefix="/ingestion",
    tags=["ingestion"],
    dependencies=[
        Depends(
            require_org_role(
                OrganizationRole.OWNER,
                OrganizationRole.ADMIN,
                OrganizationRole.MEMBER,
            )
        ),
    ],
)

_DOCUMENT_ADAPTERS_BY_KIND = {
    "file": DOCUMENT_FILE_ADAPTER_NAME,
    "folder": DOCUMENT_FOLDER_ADAPTER_NAME,
    "url": DOCUMENT_URL_ADAPTER_NAME,
    "text": DOCUMENT_TEXT_ADAPTER_NAME,
}
_NON_PATH_SOURCE_ADAPTERS = {IMAP_ADAPTER_NAME}


def _source_adapter_to_response(adapter: SourceAdapterDescriptor) -> SourceAdapterResponse:
    return SourceAdapterResponse(
        name=adapter.name,
        version=adapter.version,
        source_type=adapter.source_type,
        display_name=adapter.display_name,
        capabilities=[capability.value for capability in adapter.capabilities],
        default_privacy_class=adapter.default_privacy_class.value,
        transform_behavior=adapter.transform_behavior.value,
        metadata_schema=adapter.metadata_schema,
        supports_incremental=adapter.supports_incremental,
    )


async def _source_import_policy_context(
    *,
    ctx: AuthContext,
    memory_scope: str,
    scope_key: str | None,
) -> dict[str, Any]:
    if not ctx.user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    accessible_projects = None
    if memory_scope == "project":
        project_ids = await list_accessible_project_graph_ids(ctx) or set()
        accessible_projects = {str(project_id) for project_id in project_ids}
    return memory_policy_context_payload(
        ctx.to_memory_policy_context(
            memory_space=memory_scope,
            scope_key=scope_key,
            accessible_projects=accessible_projects,
            source_surface="source_import",
        )
    )


def _source_import_response(payload: dict[str, object]) -> SourceImportStatusResponse:
    return SourceImportStatusResponse.model_validate(payload)


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _current_principal_id(ctx: AuthContext) -> str:
    if not ctx.user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return ctx.user_id


def _source_import_http_error(exc: Exception) -> HTTPException:
    detail = str(exc).strip("'")
    if detail == "source_import_not_found":
        return HTTPException(status_code=404, detail=detail)
    if detail == "source_import_forbidden":
        return HTTPException(status_code=403, detail=detail)
    if detail in {
        "job_policy_context_missing",
        "job_policy_context_stale",
        "missing_actor",
        "missing_memory_space",
        "missing_organization",
        "source_import_canceled",
    }:
        return HTTPException(status_code=400, detail=detail)
    if detail in {
        "member_org_role_required",
        "principal_mismatch",
        "scope_not_enabled",
        "unverified_membership",
    }:
        return HTTPException(status_code=403, detail=detail)
    return HTTPException(status_code=400, detail=detail)


def _resolve_route_import_source_uri(source_uri: str) -> str:
    parsed = urlparse(source_uri)
    if parsed.scheme and parsed.scheme != "file":
        raise HTTPException(status_code=400, detail="unsupported_source_import_uri")
    raw_path = parsed.path if parsed.scheme == "file" else source_uri
    source_path = Path(raw_path).expanduser().resolve()
    import_root = settings.source_import_dir.expanduser().resolve()
    try:
        source_path.relative_to(import_root)
    except ValueError as exc:
        raise HTTPException(status_code=403, detail="source_import_path_denied") from exc
    return str(source_path)


def _resolve_route_import_source_uri_for_adapter(adapter_name: str, source_uri: str) -> str:
    if adapter_name in _NON_PATH_SOURCE_ADAPTERS:
        return source_uri
    return _resolve_route_import_source_uri(source_uri)


def _document_source_uri(request: DocumentImportRequest) -> str:
    if request.kind == "text":
        if request.text is None:
            raise ValueError("document_text_missing")
        text_hash = sha256(request.text.encode()).hexdigest()
        return request.source_uri or f"text://{text_hash}"
    if request.source_uri is None:
        raise ValueError("document_source_uri_missing")
    if request.kind in {"file", "folder"}:
        return _resolve_route_import_source_uri(request.source_uri)
    return request.source_uri.strip()


def _document_import_options(request: DocumentImportRequest) -> dict[str, object]:
    options: dict[str, object] = {
        "target_memory_scope": "project",
        "target_scope_key": request.target_scope_key,
    }
    if collection := _optional_str(request.collection):
        options["collection"] = collection
    if request.kind == "text":
        options["text"] = request.text or ""
    if title := _optional_str(request.title):
        options["title"] = title
    if request.allow_private_network:
        options["allow_private_network"] = True
    return options


def _capture_visible_to_projects(
    capture: RawCaptureRecord,
    *,
    accessible_project_ids: set[str],
) -> bool:
    if capture.memory_scope == "project":
        return bool(capture.scope_key and capture.scope_key in accessible_project_ids)
    return False


def _document_collection_name(capture: RawCaptureRecord) -> str | None:
    metadata = capture.metadata
    if metadata.get("source_type") != "document":
        return None
    record_metadata = metadata.get("source_record_metadata")
    if isinstance(record_metadata, dict):
        return _optional_str(record_metadata.get("collection"))
    return None


def _document_collections_from_captures(
    captures: Iterable[RawCaptureRecord],
    *,
    accessible_project_ids: set[str],
) -> list[DocumentCollectionResponse]:
    collections: dict[str, DocumentCollectionResponse] = {}
    for capture in captures:
        if not _capture_visible_to_projects(capture, accessible_project_ids=accessible_project_ids):
            continue
        name = _document_collection_name(capture)
        if not name:
            continue
        existing = collections.get(name)
        if existing is None:
            collections[name] = DocumentCollectionResponse(
                name=name,
                document_count=1,
                updated_at=capture.captured_at,
            )
            continue
        existing.document_count += 1
        if existing.updated_at is None or capture.captured_at > existing.updated_at:
            existing.updated_at = capture.captured_at
    return sorted(collections.values(), key=lambda item: item.name.casefold())


async def _load_document_collection_captures(
    *,
    organization_id: UUID,
) -> list[RawCaptureRecord]:
    captures: list[RawCaptureRecord] = []
    offset = 0
    page_size = 500
    async with content_runtime.get_content_read_session() as session:
        while True:
            page, has_more = await content_runtime.list_raw_captures(
                session,
                organization_id=organization_id,
                entity_type="raw_memory",
                capture_surface="source_import",
                review_state=None,
                limit=page_size,
                offset=offset,
            )
            captures.extend(page)
            if not has_more:
                return captures
            offset += page_size


@router.get("/import-adapters", response_model=SourceAdapterListResponse)
async def list_import_adapters() -> SourceAdapterListResponse:
    """List registered source import adapters."""
    ensure_mailbox_adapter_registered()
    ensure_transcript_adapters_registered()
    ensure_document_adapters_registered()
    return SourceAdapterListResponse(
        adapters=[_source_adapter_to_response(adapter) for adapter in list_source_adapters()],
    )


@router.post("/imports", response_model=SourceImportStatusResponse)
async def start_source_import_route(
    request: SourceImportStartRequest,
    org: AuthOrganization = Depends(get_current_organization),
    ctx: AuthContext = Depends(get_auth_context),
) -> SourceImportStatusResponse:
    """Create a source import run and enqueue its background drain."""
    policy_context = await _source_import_policy_context(
        ctx=ctx,
        memory_scope=request.target_memory_scope,
        scope_key=request.target_scope_key,
    )
    options = {
        **request.options,
        "target_memory_scope": request.target_memory_scope,
        "target_scope_key": request.target_scope_key,
    }
    try:
        principal_id = _current_principal_id(ctx)
        payload = await start_source_import(
            source_uri=_resolve_route_import_source_uri_for_adapter(
                request.adapter_name,
                request.source_uri,
            ),
            organization_id=str(org.id),
            principal_id=principal_id,
            policy_context=policy_context,
            adapter_name=request.adapter_name,
            options=options,
            batch_size=request.batch_size,
            promotion_preview_approved=request.promotion_preview_approved,
        )
        await job_queue.enqueue_source_import_drain(
            str(payload["import_id"]),
            organization_id=str(org.id),
            principal_id=principal_id,
            policy_context=policy_context,
            batch_size=request.batch_size,
            promotion_preview_approved=request.promotion_preview_approved,
        )
    except (KeyError, ValueError) as exc:
        raise _source_import_http_error(exc) from exc
    return _source_import_response(payload)


@router.post("/documents", response_model=SourceImportStatusResponse)
async def start_document_import_route(
    request: DocumentImportRequest,
    org: AuthOrganization = Depends(get_current_organization),
    ctx: AuthContext = Depends(get_auth_context),
) -> SourceImportStatusResponse:
    """Create a document import run and enqueue its background drain."""
    policy_context = await _source_import_policy_context(
        ctx=ctx,
        memory_scope="project",
        scope_key=request.target_scope_key,
    )
    options = _document_import_options(request)
    try:
        principal_id = _current_principal_id(ctx)
        payload = await start_source_import(
            source_uri=_document_source_uri(request),
            organization_id=str(org.id),
            principal_id=principal_id,
            policy_context=policy_context,
            adapter_name=_DOCUMENT_ADAPTERS_BY_KIND[request.kind],
            options=options,
            batch_size=request.batch_size,
            promotion_preview_approved=request.promotion_preview_approved,
        )
        await job_queue.enqueue_source_import_drain(
            str(payload["import_id"]),
            organization_id=str(org.id),
            principal_id=principal_id,
            policy_context=policy_context,
            batch_size=request.batch_size,
            promotion_preview_approved=request.promotion_preview_approved,
        )
    except (KeyError, PermissionError, ValueError) as exc:
        raise _source_import_http_error(exc) from exc
    return _source_import_response(payload)


@router.get("/collections", response_model=DocumentCollectionListResponse)
async def list_document_collections_route(
    org: AuthOrganization = Depends(get_current_organization),
    ctx: AuthContext = Depends(get_auth_context),
) -> DocumentCollectionListResponse:
    """List document collections visible to the current principal."""
    _current_principal_id(ctx)
    project_ids = await list_accessible_project_graph_ids(ctx) or set()
    captures = await _load_document_collection_captures(organization_id=org.id)
    return DocumentCollectionListResponse(
        collections=_document_collections_from_captures(
            captures,
            accessible_project_ids={str(project_id) for project_id in project_ids},
        )
    )


@router.get("/imports/{import_id:path}", response_model=SourceImportStatusResponse)
async def get_source_import_route(
    import_id: str,
    org: AuthOrganization = Depends(get_current_organization),
    ctx: AuthContext = Depends(get_auth_context),
) -> SourceImportStatusResponse:
    """Get source-safe import progress for the current organization."""
    try:
        principal_id = _current_principal_id(ctx)
        payload = await get_source_import_status(
            import_id,
            organization_id=str(org.id),
            principal_id=principal_id,
        )
    except (KeyError, PermissionError) as exc:
        raise _source_import_http_error(exc) from exc
    return _source_import_response(payload)


@router.post("/imports/{import_id:path}/resume", response_model=SourceImportStatusResponse)
async def resume_source_import_route(
    import_id: str,
    request: SourceImportResumeRequest,
    org: AuthOrganization = Depends(get_current_organization),
    ctx: AuthContext = Depends(get_auth_context),
) -> SourceImportStatusResponse:
    """Queue a source import drain from its last persisted checkpoint."""
    try:
        principal_id = _current_principal_id(ctx)
        status = await get_source_import_status(
            import_id,
            organization_id=str(org.id),
            principal_id=principal_id,
        )
        if status["status"] == "canceled":
            raise ValueError("source_import_canceled")
        if status["status"] == "completed":
            return _source_import_response(status)
        scope_key = status["target_scope_key"]
        policy_context = await _source_import_policy_context(
            ctx=ctx,
            memory_scope=str(status["target_memory_scope"] or "private"),
            scope_key=None if scope_key is None else str(scope_key),
        )
        await job_queue.enqueue_source_import_drain(
            import_id,
            organization_id=str(org.id),
            principal_id=principal_id,
            policy_context=policy_context,
            batch_size=request.batch_size,
            promotion_preview_approved=request.promotion_preview_approved,
        )
    except (KeyError, PermissionError, ValueError) as exc:
        raise _source_import_http_error(exc) from exc
    return _source_import_response(status)


@router.post("/imports/{import_id:path}/cancel", response_model=SourceImportStatusResponse)
async def cancel_source_import_route(
    import_id: str,
    org: AuthOrganization = Depends(get_current_organization),
    ctx: AuthContext = Depends(get_auth_context),
) -> SourceImportStatusResponse:
    """Cancel a resumable source import."""
    try:
        principal_id = _current_principal_id(ctx)
        payload = await cancel_source_import(
            import_id,
            organization_id=str(org.id),
            principal_id=principal_id,
        )
    except (KeyError, PermissionError) as exc:
        raise _source_import_http_error(exc) from exc
    return _source_import_response(payload)
