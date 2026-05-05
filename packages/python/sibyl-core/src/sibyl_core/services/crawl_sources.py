"""Crawl-source helpers used by manage tool seams."""

from typing import TYPE_CHECKING
from uuid import UUID

from sibyl_core.models import CrawlStatus, SourceType

if TYPE_CHECKING:
    from sibyl.persistence.content_common import DocumentChunkRecord


def _normalize_pattern_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, list | tuple | set):
        return [str(item) for item in value if item]
    return [str(value)]


async def _create_or_get_crawl_source(
    url: str,
    depth: int,
    data: dict[str, object],
    *,
    organization_id: str,
) -> tuple[str, bool]:
    """Create or reuse a relational crawl source for the given URL."""

    from sibyl.persistence.content_runtime import (
        create_crawl_source_record,
        get_content_read_session,
        list_sources_for_graph_linking,
    )

    normalized_url = url.rstrip("/")
    source_name = str(data.get("name") or normalized_url.split("//")[-1].split("/")[0])
    source_type = str(data.get("source_type") or "website").lower()
    org_uuid = UUID(organization_id)

    try:
        source_type_enum = SourceType(source_type)
    except ValueError:
        source_type_enum = SourceType.WEBSITE

    include_patterns = _normalize_pattern_list(data.get("include_patterns") or data.get("patterns"))
    exclude_patterns = _normalize_pattern_list(data.get("exclude_patterns") or data.get("exclude"))

    async with get_content_read_session() as session:
        sources = await list_sources_for_graph_linking(
            session,
            organization_id=org_uuid,
            source_id=None,
        )
        for source in sources:
            if source.url.rstrip("/") == normalized_url:
                return str(source.id), False

        source = await create_crawl_source_record(
            session,
            name=source_name,
            url=normalized_url,
            organization_id=org_uuid,
            source_type=source_type_enum,
            description=str(data["description"]) if data.get("description") else None,
            crawl_depth=max(0, min(int(depth), 10)),
            include_patterns=include_patterns,
            exclude_patterns=exclude_patterns,
        )
        return str(source.id), True


async def _crawl_source_exists(source_id: str, organization_id: str) -> bool:
    """Return whether a crawl source exists within the organization."""

    from sibyl.persistence.content_runtime import get_content_read_session, get_org_crawl_source

    async with get_content_read_session() as session:
        source = await get_org_crawl_source(
            session,
            source_id=UUID(source_id),
            organization_id=UUID(organization_id),
        )
        return source is not None


async def _list_crawl_source_ids(organization_id: str) -> list[str]:
    """List crawl source IDs for an organization."""

    from sibyl.persistence.content_runtime import (
        get_content_read_session,
        list_sources_for_graph_linking,
    )

    async with get_content_read_session() as session:
        sources = await list_sources_for_graph_linking(
            session,
            organization_id=UUID(organization_id),
            source_id=None,
        )
        return [str(source.id) for source in sources]


async def _enqueue_source_crawl(
    source_id: str,
    *,
    organization_id: str,
    max_pages: int = 50,
    max_depth: int = 3,
    generate_embeddings: bool = True,
    force: bool = False,
) -> str:
    """Enqueue a crawl job and sync its pending state to the relational source."""

    from sibyl.jobs.queue import enqueue_crawl

    job_id = await enqueue_crawl(
        source_id,
        organization_id=organization_id,
        max_pages=max_pages,
        max_depth=max_depth,
        generate_embeddings=generate_embeddings,
        force=force,
    )

    from sibyl.persistence.content_runtime import (
        get_content_read_session,
        get_org_crawl_source,
        save_crawl_source_record,
    )

    async with get_content_read_session() as session:
        source = await get_org_crawl_source(
            session,
            source_id=UUID(source_id),
            organization_id=UUID(organization_id),
        )
        if source is not None:
            source.current_job_id = job_id
            source.crawl_status = CrawlStatus.PENDING
            source.last_error = None
            await save_crawl_source_record(session, source=source)

    return job_id


async def _enqueue_source_sync(source_id: str, *, organization_id: str) -> str:
    """Enqueue a source-stat sync job."""

    from sibyl.jobs.queue import enqueue_sync

    return await enqueue_sync(source_id, organization_id=organization_id)


async def list_unlinked_document_chunks(
    *,
    organization_id: str,
    source_id: str | None = None,
    limit: int = 1000,
) -> list["DocumentChunkRecord"]:
    """List unlinked document chunks for an organization or source."""

    from sibyl.persistence.content_runtime import (
        get_content_read_session,
        list_sources_for_graph_linking,
        list_unlinked_source_chunks,
    )

    org_uuid = UUID(organization_id)
    requested_source_id = UUID(source_id) if source_id else None

    async with get_content_read_session() as session:
        sources = await list_sources_for_graph_linking(
            session,
            organization_id=org_uuid,
            source_id=requested_source_id,
        )
        chunks: list[DocumentChunkRecord] = []
        for source in sources:
            remaining = limit - len(chunks)
            if remaining <= 0:
                break
            chunks.extend(
                await list_unlinked_source_chunks(
                    session,
                    source_id=source.id,
                    limit=remaining,
                )
            )
        return chunks
