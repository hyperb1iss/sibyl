"""Relational crawl-source helpers used by legacy tool seams."""

from typing import Any
from uuid import UUID

from sibyl_core.config import settings
from sibyl_core.models import CrawlStatus, SourceType
from sibyl_core.services import surreal_content


def _normalize_pattern_list(value: Any) -> list[str]:
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
    data: dict[str, Any],
    *,
    organization_id: str,
) -> tuple[str, bool]:
    """Create or reuse a relational crawl source for the given URL."""

    if settings.store == "surreal":
        source, created = await surreal_content.get_or_create_source(
            url,
            depth,
            data,
            organization_id=organization_id,
        )
        return source.id, created

    from sibyl.db import CrawlSource, get_session
    from sqlalchemy import select
    from sqlmodel import col

    normalized_url = url.rstrip("/")
    source_name = str(data.get("name") or normalized_url.split("//")[-1].split("/")[0])
    source_type = str(data.get("source_type") or "website").lower()

    try:
        source_type_enum = SourceType(source_type)
    except ValueError:
        source_type_enum = SourceType.WEBSITE

    include_patterns = _normalize_pattern_list(data.get("include_patterns") or data.get("patterns"))
    exclude_patterns = _normalize_pattern_list(data.get("exclude_patterns") or data.get("exclude"))

    async with get_session() as session:
        result = await session.execute(
            select(CrawlSource).where(
                col(CrawlSource.organization_id) == UUID(organization_id),
                col(CrawlSource.url) == normalized_url,
            )
        )
        source = result.scalar_one_or_none()
        if source is not None:
            return str(source.id), False

        source = CrawlSource(
            name=source_name,
            url=normalized_url,
            organization_id=UUID(organization_id),
            source_type=source_type_enum,
            description=data.get("description"),
            crawl_depth=max(0, min(int(depth), 10)),
            include_patterns=include_patterns,
            exclude_patterns=exclude_patterns,
        )
        session.add(source)
        await session.flush()
        await session.refresh(source)
        return str(source.id), True


async def _crawl_source_exists(source_id: str, organization_id: str) -> bool:
    """Return whether a crawl source exists within the organization."""

    if settings.store == "surreal":
        return await surreal_content.source_exists(source_id, organization_id)

    from sibyl.db import CrawlSource, get_session
    from sqlalchemy import select
    from sqlmodel import col

    async with get_session() as session:
        result = await session.execute(
            select(CrawlSource).where(
                col(CrawlSource.id) == UUID(source_id),
                col(CrawlSource.organization_id) == UUID(organization_id),
            )
        )
        return result.scalar_one_or_none() is not None


async def _list_crawl_source_ids(organization_id: str) -> list[str]:
    """List crawl source IDs for an organization."""

    if settings.store == "surreal":
        return await surreal_content.list_source_ids_for_org(organization_id)

    from sibyl.db import CrawlSource, get_session
    from sqlalchemy import select
    from sqlmodel import col

    async with get_session() as session:
        result = await session.execute(
            select(CrawlSource).where(col(CrawlSource.organization_id) == UUID(organization_id))
        )
        return [str(source.id) for source in result.scalars().all()]


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

    if settings.store == "surreal":
        await surreal_content.set_source_job_state(
            source_id,
            organization_id=organization_id,
            job_id=job_id,
            crawl_status="pending",
            last_error=None,
        )
        return job_id

    from sibyl.db import CrawlSource, get_session
    from sqlalchemy import select
    from sqlmodel import col

    async with get_session() as session:
        result = await session.execute(
            select(CrawlSource).where(
                col(CrawlSource.id) == UUID(source_id),
                col(CrawlSource.organization_id) == UUID(organization_id),
            )
        )
        source = result.scalar_one_or_none()
        if source is not None:
            source.current_job_id = job_id
            source.crawl_status = CrawlStatus.PENDING
            source.last_error = None
            session.add(source)

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
) -> list[Any]:
    """List unlinked document chunks for an organization or source."""

    if settings.store == "surreal":
        return await surreal_content.list_unlinked_document_chunks(
            organization_id=organization_id,
            source_id=source_id,
            limit=limit,
        )

    from sibyl.db import CrawledDocument, CrawlSource, DocumentChunk, get_session
    from sqlalchemy import select
    from sqlmodel import col

    query = (
        select(DocumentChunk)
        .join(CrawledDocument, col(CrawledDocument.id) == col(DocumentChunk.document_id))
        .join(CrawlSource, col(CrawlSource.id) == col(CrawledDocument.source_id))
        .where(col(CrawlSource.organization_id) == UUID(organization_id))
        .where(col(DocumentChunk.has_entities) == False)  # noqa: E712
        .limit(limit)
    )

    if source_id:
        query = query.where(CrawlSource.id == UUID(source_id))  # type: ignore[arg-type]

    async with get_session() as session:
        result = await session.execute(query)
        return list(result.scalars().all())
