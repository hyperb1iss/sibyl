"""Shared link-graph status aggregation helpers."""

from dataclasses import dataclass
from uuid import UUID

from sibyl_core.services import content_client, content_documents, content_models


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


async def get_link_graph_status_data(
    session: object,
    organization_id: UUID | str,
) -> LinkGraphStatusData:
    """Aggregate link-graph status for the given organization."""

    async with content_client.surreal_content_client() as client:
        sources = await content_documents.load_sources_for_org(
            client, organization_id=str(organization_id)
        )
        total_row = await content_client.select_one(
            client,
            "SELECT count() AS total FROM document_chunks "
            "WHERE organization_id = $organization_id GROUP ALL;",
            organization_id=str(organization_id),
        )
        linked_row = await content_client.select_one(
            client,
            "SELECT count() AS total FROM document_chunks "
            "WHERE organization_id = $organization_id AND has_entities = true GROUP ALL;",
            organization_id=str(organization_id),
        )
        pending_rows = await content_client.select_many(
            client,
            "SELECT source_id, count() AS pending FROM document_chunks "
            "WHERE organization_id = $organization_id AND source_id != NONE "
            "AND (has_entities = false OR has_entities = NONE) "
            "GROUP BY source_id;",
            organization_id=str(organization_id),
        )

    pending_by_source = {source.id: 0 for source in sources}
    for row in pending_rows:
        source_id = content_models.coerce_optional_str(row.get("source_id"))
        if source_id:
            pending_by_source[source_id] = content_models.coerce_int(row.get("pending"))

    return LinkGraphStatusData(
        total_chunks=content_models.coerce_int(
            total_row.get("total") if total_row is not None else None
        ),
        chunks_with_entities=content_models.coerce_int(
            linked_row.get("total") if linked_row is not None else None
        ),
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
