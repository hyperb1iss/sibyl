"""Capture source-local observations at promotion input materialization."""

from sibyl_core.memory_pipeline.observations import SourceIdentity, SourceKind
from sibyl_core.services import content_client
from sibyl_core.services.source_state_store import RawSourceSnapshot, load_source_snapshot


async def load_promotion_source(organization_id: str, memory_id: str) -> RawSourceSnapshot | None:
    async with content_client.surreal_content_client() as client:
        snapshot = await load_source_snapshot(
            SourceIdentity(organization_id, SourceKind.RAW_CAPTURE, memory_id),
            organization_id=organization_id,
            execute_query=client.execute_query,
        )
    return snapshot if isinstance(snapshot, RawSourceSnapshot) else None


async def promotion_observations_current(observations, authority, organization_id: str) -> bool:
    from sibyl_core.services.memory_derivations import validate_observations

    return bool(observations) and await validate_observations(
        observations, authority, organization_id=organization_id
    )
