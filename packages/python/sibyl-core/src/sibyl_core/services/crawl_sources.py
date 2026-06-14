"""Crawl-source helpers used by manage tool seams."""

from typing import Any

from sibyl_core.runtime_ports import get_content_port, get_queue_port


async def _create_or_get_crawl_source(
    url: str,
    depth: int,
    data: dict[str, object],
    *,
    organization_id: str,
) -> tuple[str, bool]:
    """Create or reuse a relational crawl source for the given URL."""
    return await get_content_port().create_or_get_crawl_source(
        url=url,
        depth=depth,
        data=data,
        organization_id=organization_id,
    )


async def _crawl_source_exists(source_id: str, organization_id: str) -> bool:
    """Return whether a crawl source exists within the organization."""
    return await get_content_port().crawl_source_exists(
        source_id=source_id,
        organization_id=organization_id,
    )


async def _list_crawl_source_ids(organization_id: str) -> list[str]:
    """List crawl source IDs for an organization."""
    return await get_content_port().list_crawl_source_ids(organization_id=organization_id)


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
    job_id = await get_queue_port().enqueue_crawl(
        source_id,
        organization_id=organization_id,
        max_pages=max_pages,
        max_depth=max_depth,
        generate_embeddings=generate_embeddings,
        force=force,
    )
    await get_content_port().mark_crawl_pending(
        source_id=source_id,
        organization_id=organization_id,
        job_id=job_id,
    )
    return job_id


async def _enqueue_source_sync(source_id: str, *, organization_id: str) -> str:
    """Enqueue a source-stat sync job."""
    return await get_queue_port().enqueue_sync(source_id, organization_id=organization_id)


async def list_unlinked_document_chunks(
    *,
    organization_id: str,
    source_id: str | None = None,
    limit: int = 1000,
) -> list[Any]:
    """List unlinked document chunks for an organization or source."""
    return await get_content_port().list_unlinked_document_chunks(
        organization_id=organization_id,
        source_id=source_id,
        limit=limit,
    )
