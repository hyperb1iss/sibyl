"""Shared link-graph status aggregation helpers."""

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sibyl_core.config import settings
from sibyl_core.services.surreal_content import load_search_scope


@dataclass(frozen=True)
class LinkGraphSourceStatusData:
    """Pending link-graph work for a single crawl source."""

    source_id: str
    name: str
    pending: int


@dataclass(frozen=True)
class LinkGraphStatusData:
    """Aggregated link-graph status for an organization."""

    total_chunks: int
    chunks_with_entities: int
    sources: list[LinkGraphSourceStatusData]

    @property
    def chunks_pending(self) -> int:
        return self.total_chunks - self.chunks_with_entities


def _organization_uuid(organization_id: UUID | str) -> UUID:
    if isinstance(organization_id, UUID):
        return organization_id
    return UUID(str(organization_id))


async def get_link_graph_status_data(
    session: Any,
    organization_id: UUID | str,
) -> LinkGraphStatusData:
    """Aggregate link-graph status for the given organization."""

    if settings.store == "surreal":
        sources, _, documents_by_id, chunks = await load_search_scope(
            organization_id=str(organization_id),
            source_id=None,
            source_name=None,
        )
        document_source_ids = {
            document.id: document.source_id for document in documents_by_id.values()
        }
        pending_by_source = {source.id: 0 for source in sources}
        chunks_with_entities = 0

        for chunk in chunks:
            if chunk.has_entities:
                chunks_with_entities += 1
                continue
            source_id = document_source_ids.get(chunk.document_id)
            if source_id is not None:
                pending_by_source[source_id] = pending_by_source.get(source_id, 0) + 1

        return LinkGraphStatusData(
            total_chunks=len(chunks),
            chunks_with_entities=chunks_with_entities,
            sources=[
                LinkGraphSourceStatusData(
                    source_id=source.id,
                    name=source.name,
                    pending=pending_by_source[source.id],
                )
                for source in sources
                if pending_by_source[source.id] > 0
            ],
        )

    from sibyl.db import CrawledDocument, CrawlSource, DocumentChunk
    from sqlalchemy import func, select
    from sqlmodel import col

    org_uuid = _organization_uuid(organization_id)

    total_result = await session.execute(
        select(func.count(DocumentChunk.id))
        .join(CrawledDocument, col(CrawledDocument.id) == col(DocumentChunk.document_id))
        .join(CrawlSource, col(CrawlSource.id) == col(CrawledDocument.source_id))
        .where(col(CrawlSource.organization_id) == org_uuid)
    )
    total_chunks = total_result.scalar() or 0

    linked_result = await session.execute(
        select(func.count(DocumentChunk.id))
        .join(CrawledDocument, col(CrawledDocument.id) == col(DocumentChunk.document_id))
        .join(CrawlSource, col(CrawlSource.id) == col(CrawledDocument.source_id))
        .where(col(CrawlSource.organization_id) == org_uuid)
        .where(col(DocumentChunk.has_entities) == True)  # noqa: E712
    )
    chunks_with_entities = linked_result.scalar() or 0

    pending_query = (
        select(  # type: ignore[call-overload]
            col(CrawlSource.id).label("source_id"),
            CrawlSource.name,
            func.count(DocumentChunk.id).label("pending"),
        )
        .join(CrawledDocument, col(CrawledDocument.source_id) == col(CrawlSource.id))
        .join(DocumentChunk, col(DocumentChunk.document_id) == col(CrawledDocument.id))
        .where(col(CrawlSource.organization_id) == org_uuid)
        .where(col(DocumentChunk.has_entities) == False)  # noqa: E712
        .group_by(CrawlSource.id, CrawlSource.name)
        .order_by(col(CrawlSource.name).asc(), col(CrawlSource.id).asc())
    )
    pending_result = await session.execute(pending_query)
    sources = [
        LinkGraphSourceStatusData(
            source_id=str(row.source_id),
            name=row.name,
            pending=row.pending,
        )
        for row in pending_result.all()
    ]

    return LinkGraphStatusData(
        total_chunks=total_chunks,
        chunks_with_entities=chunks_with_entities,
        sources=sources,
    )
