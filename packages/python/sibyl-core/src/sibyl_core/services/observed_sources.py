"""Resolve canonical source snapshots under the operation's current authority."""

from __future__ import annotations

from sibyl_core.memory_pipeline.observations import SourceIdentity, SourceKind
from sibyl_core.services.memory_source_validation import SourceReadAuthority
from sibyl_core.services.source_observations import (
    GraphSourceSnapshot,
    SourceUnavailableError,
    observe_graph_snapshot,
    observe_raw_capture,
)
from sibyl_core.services.source_state_store import RawSourceSnapshot, load_source_snapshot

type SourceSnapshot = GraphSourceSnapshot | RawSourceSnapshot


async def load_authorized_source_snapshot(
    source: SourceIdentity,
    authority: SourceReadAuthority,
    *,
    organization_id: str,
) -> SourceSnapshot:
    """Return the exact authorized evidence and its source-local observation."""
    if source.organization_id != organization_id:
        raise SourceUnavailableError()
    if source.kind is SourceKind.RAW_CAPTURE:
        from sibyl_core.services.content_client import surreal_content_client

        async with surreal_content_client() as client:
            snapshot = await load_source_snapshot(
                source, organization_id=organization_id, execute_query=client.execute_query
            )
        if not isinstance(snapshot, RawSourceSnapshot):
            raise SourceUnavailableError()
        observe_raw_capture(snapshot.memory, authority)
        return snapshot
    from sibyl_core.services.graph_runtime import get_surreal_graph_runtime

    runtime = await get_surreal_graph_runtime(organization_id)
    snapshot = await load_source_snapshot(
        source, organization_id=organization_id, execute_query=runtime.client.execute_query
    )
    if not isinstance(snapshot, GraphSourceSnapshot):
        raise SourceUnavailableError()
    observe_graph_snapshot(snapshot, source, authority)
    return snapshot
