"""Crawler API endpoints for documentation ingestion.

Provides REST API for:
- Managing crawl sources
- Triggering crawl jobs
- Listing crawled documents
- Crawler health and stats
"""

import re
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from uuid import UUID

import httpx
import structlog
from fastapi import APIRouter, Depends, HTTPException

from sibyl.api.event_types import WSEvent
from sibyl.api.schemas import (
    CrawlDocumentListResponse,
    CrawlDocumentResponse,
    CrawlHealthResponse,
    CrawlIngestRequest,
    CrawlIngestResponse,
    CrawlSourceCreate,
    CrawlSourceListResponse,
    CrawlSourceResponse,
    CrawlSourceUpdate,
    CrawlStatsResponse,
    LinkGraphRequest,
    LinkGraphResponse,
    LinkGraphSourceStatus as LinkGraphSourceStatusResponse,
    LinkGraphStatusResponse,
    SourceAdapterListResponse,
    SourceAdapterResponse,
    SourceImportResumeRequest,
    SourceImportStartRequest,
    SourceImportStatusResponse,
)
from sibyl.api.websocket import broadcast_event
from sibyl.auth.context import AuthContext
from sibyl.auth.dependencies import get_auth_context, get_current_organization, require_org_role
from sibyl.config import settings
from sibyl.crawler.service import SourceAlreadyExistsError
from sibyl.jobs.source_imports import (
    cancel_source_import,
    get_source_import_status,
    memory_policy_context_payload,
    resume_source_import,
    start_source_import,
)
from sibyl.persistence.auth_runtime import list_accessible_project_graph_ids
from sibyl.persistence.content_common import (
    CrawledDocumentRecord,
    CrawlSourceRecord,
)
from sibyl.persistence.content_runtime import (
    count_remaining_unlinked_chunks,
    create_crawl_source_record,
    delete_crawl_source_record,
    delete_crawled_document_record,
    get_content_read_session,
    get_crawl_stats_payload,
    get_crawled_document_for_org,
    get_link_graph_status_payload,
    get_org_crawl_source,
    get_source_sync_counts,
    list_crawl_sources_for_org,
    list_crawled_documents_for_org,
    list_document_chunks,
    list_source_documents_page,
    list_sources_for_graph_linking,
    list_unlinked_source_chunks,
    save_crawl_source_record,
)
from sibyl_core.auth import AuthOrganization, OrganizationRole
from sibyl_core.models import CrawlStatus, SourceType
from sibyl_core.models.sources import SourceAdapterDescriptor
from sibyl_core.services.mailbox_adapter import ensure_mailbox_adapter_registered
from sibyl_core.services.source_adapters import list_source_adapters

log = structlog.get_logger()


def _utcnow_naive() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _enum_value(value: object) -> str:
    enum_value = getattr(value, "value", None)
    return str(enum_value if enum_value is not None else value)


async def _get_org_source(
    session: object, source_id: str, org: AuthOrganization
) -> CrawlSourceRecord:
    """Get a source and verify it belongs to the organization.

    Args:
        session: Database session
        source_id: Source UUID string
        org: Current organization

    Returns:
        The source record if found and owned by org

    Raises:
        HTTPException: 404 if not found or not owned by org
    """
    source = await get_org_crawl_source(
        session,
        source_id=UUID(source_id),
        organization_id=org.id,
    )
    if not source:
        raise HTTPException(status_code=404, detail=f"Source not found: {source_id}")
    return source


router = APIRouter(
    prefix="/sources",
    tags=["sources"],
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


def _source_to_response(source: CrawlSourceRecord) -> CrawlSourceResponse:
    """Convert source record to response schema."""
    return CrawlSourceResponse(
        id=str(source.id),
        name=source.name,
        url=source.url,
        source_type=_enum_value(source.source_type),
        description=source.description,
        crawl_depth=source.crawl_depth,
        crawl_status=_enum_value(source.crawl_status),
        document_count=source.document_count,
        chunk_count=source.chunk_count,
        last_crawled_at=source.last_crawled_at,
        last_error=source.last_error,
        created_at=source.created_at,
        include_patterns=source.include_patterns or [],
        exclude_patterns=source.exclude_patterns or [],
    )


def _document_to_response(doc: CrawledDocumentRecord) -> CrawlDocumentResponse:
    """Convert document record to response schema."""
    return CrawlDocumentResponse(
        id=str(doc.id),
        source_id=str(doc.source_id),
        url=doc.url,
        title=doc.title,
        word_count=doc.word_count,
        has_code=doc.has_code,
        is_index=doc.is_index,
        depth=doc.depth,
        crawled_at=doc.crawled_at,
        headings=doc.headings or [],
        code_languages=doc.code_languages or [],
    )


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


# =============================================================================
# Stats & Health (MUST come before /{source_id} routes)
# =============================================================================


@router.get("/stats", response_model=CrawlStatsResponse)
async def get_stats(
    org: AuthOrganization = Depends(get_current_organization),
) -> CrawlStatsResponse:
    """Get crawler statistics for the current organization."""
    async with get_content_read_session() as session:
        stats = await get_crawl_stats_payload(session, organization_id=org.id)

    return CrawlStatsResponse(
        total_sources=stats.total_sources,
        total_documents=stats.total_documents,
        total_chunks=stats.total_chunks,
        chunks_with_embeddings=stats.chunks_with_embeddings,
        sources_by_status=stats.sources_by_status,
    )


@router.get("/health", response_model=CrawlHealthResponse)
async def get_health() -> CrawlHealthResponse:
    """Check crawler system health."""
    # Check Crawl4AI availability
    crawl4ai_available = False
    try:
        from crawl4ai import AsyncWebCrawler  # noqa: F401

        crawl4ai_available = True
    except ImportError:
        pass

    return CrawlHealthResponse(
        relational_backend_enabled=False,
        relational_backend_healthy=True,
        relational_backend_version=None,
        vector_extension_version=None,
        crawl4ai_available=crawl4ai_available,
        error=None,
    )


# =============================================================================
# Documents (MUST come before /{source_id} routes)
# =============================================================================


@router.get("/documents", response_model=CrawlDocumentListResponse)
async def list_documents(
    limit: int = 50,
    offset: int = 0,
    org: AuthOrganization = Depends(get_current_organization),
) -> CrawlDocumentListResponse:
    """List crawled documents for the current organization."""
    async with get_content_read_session() as session:
        documents, total = await list_crawled_documents_for_org(
            session,
            organization_id=org.id,
            limit=limit,
            offset=offset,
        )

    return CrawlDocumentListResponse(
        documents=[_document_to_response(d) for d in documents],
        total=total,
    )


@router.get("/documents/{document_id}", response_model=CrawlDocumentResponse)
async def get_document(
    document_id: str,
    org: AuthOrganization = Depends(get_current_organization),
) -> CrawlDocumentResponse:
    """Get a crawled document by ID with full content (org-scoped)."""
    # Strip 'doc:' prefix if present
    uuid_str = document_id.removeprefix("doc:")
    async with get_content_read_session() as session:
        doc = await get_crawled_document_for_org(
            session,
            document_id=UUID(uuid_str),
            organization_id=org.id,
        )
        if not doc:
            raise HTTPException(status_code=404, detail=f"Document not found: {document_id}")

        # Detail view includes raw_content
        response = _document_to_response(doc)
        response.raw_content = doc.raw_content

        # Fetch chunks and assemble markdown content
        chunks = await list_document_chunks(session, document_id=doc.id)
        if chunks:
            response.markdown_content = "\n\n".join(c.content for c in chunks)

        return response


@router.delete("/documents/{document_id}")
async def delete_document(
    document_id: str,
    org: AuthOrganization = Depends(get_current_organization),
) -> dict[str, object]:
    """Delete a crawled document and its chunks (org-scoped)."""
    async with get_content_read_session() as session:
        deleted = await delete_crawled_document_record(
            session,
            document_id=UUID(document_id),
            organization_id=org.id,
        )
        if deleted is None:
            raise HTTPException(status_code=404, detail=f"Document not found: {document_id}")
        _doc, chunks_deleted = deleted

        log.info(
            "Deleted document",
            document_id=document_id,
            chunks_deleted=chunks_deleted,
        )

    await broadcast_event(
        WSEvent.ENTITY_DELETED,
        {"type": "crawled_document", "id": document_id},
        org_id=str(org.id),
    )
    return {"deleted": document_id, "chunks_deleted": chunks_deleted}


# =============================================================================
# URL Preview
# =============================================================================


@router.get("/preview")
async def preview_url(url: str) -> dict[str, str | None]:
    """Fetch metadata from a URL to help with source naming.

    Returns the page title and suggested name for use when creating a source.
    """
    try:
        parsed = urlparse(url)
        if not parsed.scheme or not parsed.netloc:
            raise HTTPException(status_code=400, detail="Invalid URL")

        # Fetch the page
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            response = await client.get(url, headers={"User-Agent": "Sibyl/1.0"})
            response.raise_for_status()

        html = response.text[:50000]  # Only check first 50KB

        # Extract title
        title_match = re.search(r"<title[^>]*>([^<]+)</title>", html, re.IGNORECASE)
        title = title_match.group(1).strip() if title_match else None

        # Clean up title for use as name
        suggested_name = None
        if title:
            # Remove common suffixes like "| Company" or "- Docs"
            suggested_name = re.sub(
                r"\s*[\|\-\u2013\u2014]\s*[^|\-\u2013\u2014]+$", "", title
            ).strip()
            # If still too generic, use domain + title
            if len(suggested_name) < 3:
                suggested_name = f"{parsed.netloc} - {title}"

        return {
            "url": url,
            "title": title,
            "suggested_name": suggested_name or parsed.netloc,
            "domain": parsed.netloc,
        }

    except httpx.HTTPStatusError as e:
        log.warning("URL preview failed", url=url, status=e.response.status_code)
        return {
            "url": url,
            "title": None,
            "suggested_name": urlparse(url).netloc,
            "domain": urlparse(url).netloc,
            "error": f"HTTP {e.response.status_code}",
        }
    except Exception as e:
        log.warning("URL preview failed", url=url, error=str(e))
        return {
            "url": url,
            "title": None,
            "suggested_name": urlparse(url).netloc,
            "domain": urlparse(url).netloc,
            "error": "Failed to preview URL",
        }


@router.get("/import-adapters", response_model=SourceAdapterListResponse)
async def list_import_adapters() -> SourceAdapterListResponse:
    """List registered source import adapters."""
    ensure_mailbox_adapter_registered()
    return SourceAdapterListResponse(
        adapters=[_source_adapter_to_response(adapter) for adapter in list_source_adapters()],
    )


@router.post("/imports", response_model=SourceImportStatusResponse)
async def start_source_import_route(
    request: SourceImportStartRequest,
    org: AuthOrganization = Depends(get_current_organization),
    ctx: AuthContext = Depends(get_auth_context),
) -> SourceImportStatusResponse:
    """Start a bounded source import and persist the first checkpoint."""
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
            source_uri=_resolve_route_import_source_uri(request.source_uri),
            organization_id=str(org.id),
            principal_id=principal_id,
            policy_context=policy_context,
            adapter_name=request.adapter_name,
            options=options,
            batch_size=request.batch_size,
            promotion_preview_approved=request.promotion_preview_approved,
        )
    except (KeyError, ValueError) as exc:
        raise _source_import_http_error(exc) from exc
    return _source_import_response(payload)


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
    """Resume a source import from its last persisted checkpoint."""
    try:
        principal_id = _current_principal_id(ctx)
        status = await get_source_import_status(
            import_id,
            organization_id=str(org.id),
            principal_id=principal_id,
        )
        scope_key = status["target_scope_key"]
        policy_context = await _source_import_policy_context(
            ctx=ctx,
            memory_scope=str(status["target_memory_scope"] or "private"),
            scope_key=None if scope_key is None else str(scope_key),
        )
        payload = await resume_source_import(
            import_id,
            organization_id=str(org.id),
            principal_id=principal_id,
            policy_context=policy_context,
            batch_size=request.batch_size,
            promotion_preview_approved=request.promotion_preview_approved,
        )
    except (KeyError, PermissionError, ValueError) as exc:
        raise _source_import_http_error(exc) from exc
    return _source_import_response(payload)


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


# =============================================================================
# Source CRUD
# =============================================================================


@router.post("", response_model=CrawlSourceResponse)
async def create_source(
    request: CrawlSourceCreate,
    org: AuthOrganization = Depends(get_current_organization),
) -> CrawlSourceResponse:
    """Create a new crawl source."""
    try:
        async with get_content_read_session() as session:
            source = await create_crawl_source_record(
                session,
                name=request.name,
                url=request.url,
                organization_id=org.id,
                source_type=SourceType(request.source_type),
                description=request.description,
                crawl_depth=request.crawl_depth,
                include_patterns=request.include_patterns,
                exclude_patterns=request.exclude_patterns,
            )
    except SourceAlreadyExistsError as exc:
        raise HTTPException(
            status_code=409, detail=f"Source with URL {request.url} already exists"
        ) from exc

    response = _source_to_response(source)

    await broadcast_event(
        WSEvent.ENTITY_CREATED,
        {"type": "crawl_source", "id": str(source.id)},
        org_id=str(org.id),
    )
    return response


@router.get("", response_model=CrawlSourceListResponse)
async def list_sources(
    status: str | None = None,
    limit: int = 50,
    org: AuthOrganization = Depends(get_current_organization),
) -> CrawlSourceListResponse:
    """List crawl sources for the current organization."""
    async with get_content_read_session() as session:
        sources, total = await list_crawl_sources_for_org(
            session,
            organization_id=org.id,
            status=CrawlStatus(status) if status else None,
            limit=limit,
        )

    return CrawlSourceListResponse(
        sources=[_source_to_response(source) for source in sources],
        total=total,
    )


@router.get("/link-graph/status", response_model=LinkGraphStatusResponse)
async def get_link_graph_status(
    org: AuthOrganization = Depends(get_current_organization),
) -> LinkGraphStatusResponse:
    """Get status of pending graph linking work (org-scoped).

    Shows how many chunks still need entity extraction per source.
    """
    async with get_content_read_session() as session:
        status = await get_link_graph_status_payload(session, organization_id=org.id)

    return LinkGraphStatusResponse(
        total_chunks=status.total_chunks,
        chunks_with_entities=status.chunks_with_entities,
        chunks_pending=status.chunks_pending,
        sources=[LinkGraphSourceStatusResponse(**asdict(source)) for source in status.sources],
    )


@router.post("/link-graph", response_model=LinkGraphResponse)
async def link_all_sources_to_graph(
    request: LinkGraphRequest,
    org: AuthOrganization = Depends(get_current_organization),
) -> LinkGraphResponse:
    """Extract entities from all source chunks and link to knowledge graph.

    Processes chunks that haven't been entity-linked yet (has_entities=False).
    Uses LLM to extract entities and matches them to existing graph entities.
    """
    return await _process_graph_linking(
        source_id=None, request=request, organization_id=str(org.id)
    )


@router.get("/{source_id}", response_model=CrawlSourceResponse)
async def get_source(
    source_id: str,
    org: AuthOrganization = Depends(get_current_organization),
) -> CrawlSourceResponse:
    """Get a crawl source by ID (org-scoped)."""
    async with get_content_read_session() as session:
        source = await _get_org_source(session, source_id, org)
        return _source_to_response(source)


@router.patch("/{source_id}", response_model=CrawlSourceResponse)
async def update_source(
    source_id: str,
    request: CrawlSourceUpdate,
    org: AuthOrganization = Depends(get_current_organization),
) -> CrawlSourceResponse:
    """Update a crawl source (org-scoped)."""
    async with get_content_read_session() as session:
        source = await _get_org_source(session, source_id, org)

        # Update fields if provided
        update_data = request.model_dump(exclude_unset=True)
        for field, value in update_data.items():
            setattr(source, field, value)

        source = await save_crawl_source_record(session, source=source)

        log.info("Updated crawl source", id=str(source.id), fields=list(update_data.keys()))

        response = _source_to_response(source)

    await broadcast_event(
        WSEvent.ENTITY_UPDATED,
        {"type": "crawl_source", "id": str(source.id)},
        org_id=str(org.id),
    )
    return response


@router.delete("/{source_id}")
async def delete_source(
    source_id: str,
    org: AuthOrganization = Depends(get_current_organization),
) -> dict[str, object]:
    """Delete a crawl source and all its documents (org-scoped)."""
    async with get_content_read_session() as session:
        source = await delete_crawl_source_record(
            session,
            source_id=UUID(source_id),
            organization_id=org.id,
        )
        if source is None:
            raise HTTPException(status_code=404, detail=f"Source not found: {source_id}")

        log.info("Deleted crawl source", id=source_id, name=source.name)

    await broadcast_event(
        WSEvent.ENTITY_DELETED,
        {"type": "crawl_source", "id": source_id},
        org_id=str(org.id),
    )
    return {"deleted": True, "id": source_id}


# =============================================================================
# Ingestion (via arq job queue)
# =============================================================================


@router.post("/{source_id}/ingest", response_model=CrawlIngestResponse)
async def ingest_source(
    source_id: str,
    request: CrawlIngestRequest,
    org: AuthOrganization = Depends(get_current_organization),
) -> CrawlIngestResponse:
    """Start crawling a source via job queue (org-scoped).

    Jobs are processed by the arq worker for reliability and persistence.
    Run the worker with: uv run arq sibyl.jobs.WorkerSettings
    """
    from sibyl.jobs import enqueue_crawl

    # Verify source exists and belongs to org
    async with get_content_read_session() as session:
        source = await _get_org_source(session, source_id, org)

        # Check if already crawling
        if source.crawl_status == CrawlStatus.IN_PROGRESS:
            return CrawlIngestResponse(
                source_id=source_id,
                status="already_running",
                message="Crawl already in progress for this source",
            )

        source_name = source.name

    # Enqueue the crawl job (force=True clears old results for re-crawl)
    try:
        job_id = await enqueue_crawl(
            source_id,
            organization_id=str(org.id),
            max_pages=request.max_pages,
            max_depth=request.max_depth,
            generate_embeddings=request.generate_embeddings,
            force=True,
        )

        # Save job_id to source for cancellation support
        async with get_content_read_session() as session:
            source = await get_org_crawl_source(
                session,
                source_id=UUID(source_id),
                organization_id=org.id,
            )
            if source is not None:
                source.current_job_id = job_id
                await save_crawl_source_record(session, source=source)

        log.info(
            "Enqueued crawl job",
            source_id=source_id,
            job_id=job_id,
            max_pages=request.max_pages,
        )

        return CrawlIngestResponse(
            source_id=source_id,
            job_id=job_id,
            status="queued",
            message=f"Crawl job queued for {source_name}",
        )

    except Exception as e:
        log.exception("Failed to enqueue crawl job", source_id=source_id, error=str(e))
        raise HTTPException(
            status_code=503,
            detail="Failed to enqueue job. Is the job queue available?",
        ) from e


@router.get("/{source_id}/status")
async def get_ingestion_status(
    source_id: str,
    org: AuthOrganization = Depends(get_current_organization),
) -> dict[str, object]:
    """Get crawl status for a source (org-scoped).

    Returns both the source's crawl_status and any active job status.
    """
    # Get source status from DB (org-scoped)
    async with get_content_read_session() as session:
        source = await _get_org_source(session, source_id, org)

        return {
            "source_id": source_id,
            "crawl_status": _enum_value(source.crawl_status),
            "current_job_id": source.current_job_id,
            "document_count": source.document_count,
            "chunk_count": source.chunk_count,
            "last_crawled_at": source.last_crawled_at.isoformat()
            if source.last_crawled_at
            else None,
            "last_error": source.last_error,
        }


@router.post("/{source_id}/cancel", response_model=CrawlIngestResponse)
async def cancel_crawl(
    source_id: str,
    org: AuthOrganization = Depends(get_current_organization),
) -> CrawlIngestResponse:
    """Cancel an in-progress crawl for a source (org-scoped).

    Cancels the job if running and resets the source status.
    """
    from sibyl.jobs.queue import cancel_job

    async with get_content_read_session() as session:
        source = await _get_org_source(session, source_id, org)

        # Check if there's a job to cancel
        job_id = source.current_job_id
        if not job_id:
            return CrawlIngestResponse(
                source_id=source_id,
                status="no_job",
                message="No active crawl job to cancel",
            )

        # Try to cancel the job
        try:
            cancelled = await cancel_job(job_id)
        except Exception as e:
            log.warning("Failed to cancel job", job_id=job_id, error=str(e))
            cancelled = False

        # Reset source status regardless of cancel result
        source.crawl_status = CrawlStatus.PENDING
        source.current_job_id = None
        await save_crawl_source_record(session, source=source)

        log.info(
            "Cancelled crawl",
            source_id=source_id,
            job_id=job_id,
            job_cancelled=cancelled,
        )

        await broadcast_event(
            WSEvent.ENTITY_UPDATED,
            {"type": "crawl_source", "id": source_id},
            org_id=str(org.id),
        )

        return CrawlIngestResponse(
            source_id=source_id,
            job_id=job_id,
            status="cancelled",
            message=f"Crawl cancelled for {source.name}",
        )


@router.post("/{source_id}/sync")
async def sync_source(
    source_id: str,
    org: AuthOrganization = Depends(get_current_organization),
) -> dict[str, object]:
    """Sync source stats from actual document/chunk counts (org-scoped).

    Useful for fixing stuck sources or after manual data changes.
    Recalculates document_count, chunk_count, and fixes status if stuck.
    """
    async with get_content_read_session() as session:
        source = await _get_org_source(session, source_id, org)

        actual_doc_count, actual_chunk_count = await get_source_sync_counts(
            session,
            source_id=UUID(source_id),
        )

        # Determine correct status
        old_status = source.crawl_status
        if actual_doc_count > 0:
            # Has documents - should be completed or partial
            if source.crawl_status == CrawlStatus.IN_PROGRESS:
                source.crawl_status = CrawlStatus.COMPLETED
                if source.last_crawled_at is None:
                    source.last_crawled_at = _utcnow_naive()
        elif source.crawl_status == CrawlStatus.IN_PROGRESS:
            # No documents but stuck in progress - reset to pending
            source.crawl_status = CrawlStatus.PENDING

        # Update counts
        old_doc_count = source.document_count
        old_chunk_count = source.chunk_count
        source.document_count = actual_doc_count
        source.chunk_count = actual_chunk_count
        source = await save_crawl_source_record(session, source=source)

        # Capture values before session closes
        new_status = source.crawl_status

        log.info(
            "Synced source stats",
            source_id=source_id,
            old_status=_enum_value(old_status),
            new_status=_enum_value(new_status),
            old_doc_count=old_doc_count,
            new_doc_count=actual_doc_count,
            old_chunk_count=old_chunk_count,
            new_chunk_count=actual_chunk_count,
        )

    await broadcast_event(
        WSEvent.ENTITY_UPDATED,
        {"type": "crawl_source", "id": source_id},
        org_id=str(org.id),
    )

    return {
        "source_id": source_id,
        "synced": True,
        "document_count": actual_doc_count,
        "chunk_count": actual_chunk_count,
        "status": _enum_value(new_status),
        "changes": {
            "status": f"{_enum_value(old_status)} -> {_enum_value(new_status)}"
            if old_status != new_status
            else None,
            "document_count": f"{old_doc_count} -> {actual_doc_count}"
            if old_doc_count != actual_doc_count
            else None,
            "chunk_count": f"{old_chunk_count} -> {actual_chunk_count}"
            if old_chunk_count != actual_chunk_count
            else None,
        },
    }


# =============================================================================
# Graph Integration
# =============================================================================


@router.post("/{source_id}/link-graph", response_model=LinkGraphResponse)
async def link_source_to_graph(
    source_id: str,
    request: LinkGraphRequest,
    org: AuthOrganization = Depends(get_current_organization),
) -> LinkGraphResponse:
    """Extract entities from source chunks and link to knowledge graph.

    Processes chunks that haven't been entity-linked yet (has_entities=False).
    Uses LLM to extract entities and matches them to existing graph entities.
    """
    return await _process_graph_linking(
        source_id=source_id, request=request, organization_id=str(org.id)
    )


async def _process_graph_linking(
    source_id: str | None,
    request: LinkGraphRequest,
    organization_id: str,
) -> LinkGraphResponse:
    """Internal function to process graph linking for one or all sources."""
    from sibyl.crawler.graph_integration import create_graph_integration_service

    # Initialize integration service
    try:
        integration = await create_graph_integration_service(
            organization_id,
            create_new_entities=request.create_new_entities,
        )
    except ValueError as e:
        log.warning("Entity extraction configuration error", error=str(e))
        raise HTTPException(
            status_code=503,
            detail="Entity extraction not configured",
        ) from e
    except Exception as e:
        log.warning("Failed to connect to graph", error=str(e))
        raise HTTPException(status_code=503, detail="Graph service unavailable") from e

    org_uuid = UUID(organization_id)

    # Get sources to process (org-scoped)
    async with get_content_read_session() as session:
        sources = await list_sources_for_graph_linking(
            session,
            organization_id=org_uuid,
            source_id=UUID(source_id) if source_id else None,
        )
        if source_id and not sources:
            raise HTTPException(status_code=404, detail=f"Source not found: {source_id}")

        if not sources:
            return LinkGraphResponse(
                source_id=source_id,
                status="no_sources",
                message="No sources found to process",
            )

        total_chunks = 0
        total_extracted = 0
        total_linked = 0
        total_created = 0
        sources_processed = []

        for source in sources:
            # Get unprocessed chunks for this source
            chunks = await list_unlinked_source_chunks(
                session,
                source_id=source.id,
                limit=request.batch_size * 10,
            )

            if not chunks:
                continue

            sources_processed.append(source.name)

            if request.dry_run:
                total_chunks += len(chunks)
                continue

            # Process in batches
            for i in range(0, len(chunks), request.batch_size):
                batch = chunks[i : i + request.batch_size]
                stats = await integration.process_chunks(batch, source.name)
                total_chunks += len(batch)
                total_extracted += stats.entities_extracted
                total_linked += stats.entities_linked
                total_created += stats.new_entities_created

    # Count remaining unprocessed chunks
    async with get_content_read_session() as session:
        chunks_remaining = await count_remaining_unlinked_chunks(
            session,
            organization_id=org_uuid,
            source_id=UUID(source_id) if source_id else None,
        )

    if request.dry_run:
        return LinkGraphResponse(
            source_id=source_id,
            status="dry_run",
            chunks_processed=total_chunks,
            chunks_remaining=chunks_remaining,
            new_entities_created=0,
            sources_processed=sources_processed,
            message=f"Would process {total_chunks} chunks from {len(sources_processed)} source(s)",
        )

    if total_chunks == 0:
        return LinkGraphResponse(
            source_id=source_id,
            status="no_chunks",
            chunks_remaining=chunks_remaining,
            message="No unprocessed chunks found",
        )

    await broadcast_event(
        WSEvent.GRAPH_UPDATED,
        {
            "chunks_processed": total_chunks,
            "new_entities_created": total_created,
        },
        org_id=str(org_uuid),
    )

    message = f"Processed {total_chunks} chunks, extracted {total_extracted} entities"
    if total_created > 0:
        message += f", created {total_created} new graph entities"

    return LinkGraphResponse(
        source_id=source_id,
        status="completed",
        chunks_processed=total_chunks,
        chunks_remaining=chunks_remaining,
        entities_extracted=total_extracted,
        entities_linked=total_linked,
        new_entities_created=total_created,
        sources_processed=sources_processed,
        message=message,
    )


@router.get("/{source_id}/documents", response_model=CrawlDocumentListResponse)
async def list_source_documents(
    source_id: str,
    limit: int = 50,
    offset: int = 0,
    org: AuthOrganization = Depends(get_current_organization),
) -> CrawlDocumentListResponse:
    """List documents for a source."""
    async with get_content_read_session() as session:
        await _get_org_source(session, source_id, org)
        documents, total = await list_source_documents_page(
            session,
            source_id=UUID(source_id),
            limit=limit,
            offset=offset,
        )

    return CrawlDocumentListResponse(
        documents=[_document_to_response(d) for d in documents],
        total=total,
    )
